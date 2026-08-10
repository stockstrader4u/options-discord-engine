"""
bmt_nightly_setups_v2_test.py — EXPERIMENTAL / TEST BUILD

Standalone sibling of bmt_nightly_setups.py, built to trial a much
richer, subscriber-facing post format (ranking table, per-setup trade
plans, execution rules, risk disclosure, card copy) driven by a single
long-form editor-style prompt, instead of the short JSON-field digest
the production script uses.

This is a SEPARATE Railway service pointed at a SEPARATE test webhook
(NIGHTLY_SETUPS_V2_TEST_DISCORD_WEBHOOK). It does not touch, import,
or modify bmt_nightly_setups.py or its production webhook in any way.
The intent is to let this run in parallel for a review period; if the
richer format proves better, the production script gets updated
deliberately later -- this file is not wired into that decision.

WHAT'S REUSED, UNCHANGED, FROM bmt_nightly_setups.py:
All data-layer logic is copied verbatim -- flow scanning, the same-day
earnings exclusion gate, deterministic chart-pattern matching, IV/RV
pricing screen, strike selection, ATR-based trade-level math, analyst
target lookup. None of the underlying setup-selection logic changes.

WHAT'S NEW:
- write_rich_post(): one LLM call that generates the ENTIRE
  subscriber-ready post as raw text, in the exact structure supplied
  by the user (ranking table, Best Choice Tonight, five full setup
  write-ups with trade plans, Execution Rules, Risk Disclosure, and
  per-ticker Card Copy blocks) -- not a JSON digest of a few fields.
- Because that document is far longer than Discord's 2000-character
  message limit, it is chunked on paragraph/section boundaries (never
  mid-sentence) and posted as several sequential messages via
  chunk_markdown_for_discord() / post_text_chunks_to_discord().
- A handful of hard-won production rules are layered ON TOP of the
  user's exact prompt (without altering its requested structure):
  no URLs/source names in the output (breaks Discord's auto-embed
  formatting), any catalyst date must be stated as before/after that
  contract's expiry, and every ticker mention is prefixed with "$".
- The card image (reusing the same renderer as production) is
  watermarked "V2 TEST -- INTERNAL REVIEW" so it can never be mistaken
  for the live production card while this is being trialed.
"""

import os
import json
import re
import time
import threading
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from apscheduler.schedulers.background import BackgroundScheduler

JARVIS_API_KEY     = os.environ["JARVIS_API_KEY"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
FINNHUB_API_KEY    = os.environ["FINNHUB_API_KEY"]
DISCORD_WEBHOOK    = os.environ["NIGHTLY_SETUPS_V2_TEST_DISCORD_WEBHOOK"]
JARVIS_MCP_URL      = "https://api.jarvisflow.io/.well-known/mcp"
OPENROUTER_BASE    = "https://openrouter.ai/api/v1"
FINNHUB_BASE       = "https://finnhub.io/api/v1"
ET                 = ZoneInfo("America/New_York")
HEADERS            = {"User-Agent": "Mozilla/5.0"}

MIN_PREMIUM = 50_000
TOP_N       = 5

FULL_WATCHLIST = [
    "TDOC","DDOG","DOCU","MDB","ANET","TWLO","ETSY","CRM","UBER","ROKU",
    "NFLX","NVDA","OKTA","SBUX","FTNT","SHOP","AAPL","Z","TSLA","MA",
    "AMZN","ZS","DIS","SE","NOW","CRWD","SNAP","BABA","UPST","QRVO",
    "QCOM","AMD","BA","PINS","CELH","DKNG","PLTR","CHWY","LULU","COIN",
    "MRNA","SNOW","AFRM","MSFT","ABNB","ADSK","MRVL","RBLX","SOFI","SPOT",
    "META","WMT","TGT","HD","TSM","AI","MU","NET","U","GOOGL",
    "RIVN","JNJ","INTC","MARA","RIOT","XOM","OXY","CVX","CVNA","ENPH",
    "FDX","SMCI","ARM","LRCX","PANW","BIDU","JD","XPEV","PDD","FUTU",
    "MSTR","ORCL","HOOD","CMG","UPS","DELL","LMT","CAT","CAVA","RDDT",
    "CART","DASH","HIMS","AVGO","ADBE","MMM","NKE","GS","RTX","GTLB",
    "CLSK","IBM","TEAM","LLY","RGTI","QUBT","IBIT","TEM","VST","UAL",
    "OKLO","NNE","RKLB","NBIS","CEG","IONQ","XYZ","PYPL","QBTS","APP",
    "CRWV","GME","UNH","CRCL","FSLR","SMR","OSCR","ACHR","ASTS","BMNR",
    "FIG","GLXY","SBET","VKTX","IREN","UUUU","BLSH","SNPS","FLY","POET",
    "CIFR","BE","EOSE","ONDS","SNDK","PATH","LMND","JPM","ZM","AMAT",
    "RKT","NVO","DUOL","AXTI","FIGR","RBRK","ALAB","CAR","QS","CSCO",
    "AAOI","SPCX","AEHR","SKHY","AKAM","FISV","LUV",
]

EXCLUDE_FROM_CANDIDATES = {
    "IWM", "QQQ", "SPY", "UVXY", "SQQQ", "TQQQ", "NUGT", "SLV", "USO",
    "IBIT", "NVDL", "OKEX:ETHUSD", "COINBASE:^BTCUSD",
}
MARKET_CONTEXT_TICKERS = ["SPY", "QQQ", "IWM"]

CANDIDATE_UNIVERSE = [t for t in FULL_WATCHLIST if t not in EXCLUDE_FROM_CANDIDATES]

EARNINGS_LOOKAHEAD_DAYS = 14


# ─────────────────────────────────────────────────────────────────────
# DATA LAYER -- copied verbatim from bmt_nightly_setups.py. No changes.
# ─────────────────────────────────────────────────────────────────────

def get_upcoming_earnings_map() -> dict:
    today = datetime.now(ET).strftime("%Y-%m-%d")
    end = (datetime.now(ET) + timedelta(days=EARNINGS_LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")
    for attempt in range(3):
        try:
            resp = requests.get(
                f"{FINNHUB_BASE}/calendar/earnings",
                params={"from": today, "to": end, "token": FINNHUB_API_KEY},
                timeout=20,
            )
            calendar = resp.json().get("earningsCalendar", [])
            er_map = {}
            for e in calendar:
                sym = e.get("symbol", "").upper()
                d = e.get("date", "")
                if not sym or not d:
                    continue
                if sym not in er_map or d < er_map[sym]:
                    er_map[sym] = d
            print(f"  [ER FILTER] Loaded {len(er_map)} upcoming earnings dates ({today} to {end})")
            return er_map
        except Exception as e:
            print(f"  [ER FILTER WARN] Finnhub calendar attempt {attempt+1}: {e}")
    print("  [ER FILTER WARN] Could not load Finnhub earnings calendar after 3 attempts.")
    return {}


def get_upcoming_earnings_date(ticker: str) -> str:
    try:
        import yfinance as yf
        edf = yf.Ticker(ticker).get_earnings_dates(limit=8)
        if edf is None or edf.empty:
            return None
        now = datetime.now(ET).replace(tzinfo=None)
        future = sorted(idx.strftime("%Y-%m-%d") for idx in edf.index if idx.replace(tzinfo=None) > now)
        return future[0] if future else None
    except Exception as e:
        print(f"  [ER FILTER WARN] {ticker}: {type(e).__name__}: {e}")
        return None


def get_earnings_today_and_recent(ticker: str, lookback_days: int = 2) -> str:
    try:
        import yfinance as yf
        edf = yf.Ticker(ticker).get_earnings_dates(limit=8)
        if edf is None or edf.empty:
            return None
        today = datetime.now(ET).date()
        window_start = today - timedelta(days=lookback_days)
        for idx in edf.index:
            idx_date = idx.replace(tzinfo=None).date() if idx.tzinfo else idx.date()
            if window_start <= idx_date <= today:
                return idx_date.strftime("%Y-%m-%d")
        return None
    except Exception as e:
        print(f"  [ER SAME-DAY WARN] {ticker}: {type(e).__name__}: {e}")
        return None


def call_jarvis(tool_name, arguments={}):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool_name, "arguments": arguments}}
    try:
        resp = requests.post(JARVIS_MCP_URL,
            headers={"Authorization": f"Bearer {JARVIS_API_KEY}", "Content-Type": "application/json"},
            json=payload, timeout=15)
        if resp.status_code != 200:
            return None
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                data = json.loads(line[5:].strip())
                content = data.get("result", {}).get("content", [])
                if content and content[0].get("type") == "text":
                    text = content[0]["text"]
                    if not text or text.startswith("An error"):
                        return None
                    inner = json.loads(text)
                    return inner.get("toolResult", inner)
    except Exception as e:
        print(f"  [JARVIS WARN] {tool_name}: {e}")
    return None


def get_flow_for_ticker(ticker: str) -> dict:
    result = call_jarvis("stock_ticker_unusual_options_data", {"filter_by_Ticker": ticker})
    if not result:
        return {"bias": None, "premium": 0, "call_pct": None}
    flow = result.get("optionsFlow", []) if isinstance(result, dict) else result
    if not flow:
        return {"bias": None, "premium": 0, "call_pct": None}
    flow = [f for f in flow if f.get("ticker", "").upper() == ticker.upper()]
    if not flow:
        return {"bias": None, "premium": 0, "call_pct": None}
    bought_otm_atm = [f for f in flow if f.get("implied_Bought_Or_Sold") == "BOUGHT"
                       and f.get("moneyNess", "").upper() in ("OTM", "ATM")]
    if not bought_otm_atm:
        return {"bias": None, "premium": 0, "call_pct": None}
    total_call = sum(float(f.get("total_Option_Premium_For_Trade", 0) or 0) for f in bought_otm_atm if f.get("put_Or_Call") == "CALL")
    total_put = sum(float(f.get("total_Option_Premium_For_Trade", 0) or 0) for f in bought_otm_atm if f.get("put_Or_Call") == "PUT")
    total = total_call + total_put
    if total == 0:
        return {"bias": None, "premium": 0, "call_pct": None}
    call_pct = round(total_call / total * 100)
    bias = "Bullish" if call_pct > 55 else "Bearish" if call_pct < 45 else "Neutral"
    return {"bias": bias, "premium": total, "call_pct": call_pct}


def get_daily_ohlc(ticker: str, sessions: int = 15) -> list:
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        hist = stock.history(period="2mo")
        if hist.empty:
            return []
        hist = hist.tail(sessions)
        return [{"date": date, "open": row["Open"], "high": row["High"], "low": row["Low"],
                  "close": row["Close"], "volume": row.get("Volume", 0) or 0}
                for date, row in hist.iterrows()]
    except Exception as e:
        print(f"  [OHLC WARN] {ticker}: {e}")
        return []


def compute_avg_dollar_volume(bars: list) -> float:
    vals = [b["close"] * b["volume"] for b in bars if b.get("close") and b.get("volume")]
    return sum(vals) / len(vals) if vals else 0.0


def format_ohlc_summary(bars: list) -> str:
    return "\n".join(f"{b['date'].strftime('%b %d')}: O={b['open']:.2f} H={b['high']:.2f} L={b['low']:.2f} C={b['close']:.2f}" for b in bars)


def get_strike_increment(price):
    if price < 50: return 1.0
    elif price < 200: return 2.5
    else: return 5.0


def compute_daily_atr(bars, period=10):
    recent = bars[-period:] if len(bars) >= period else bars
    if not recent: return 0.0
    return sum(b["high"] - b["low"] for b in recent) / len(recent)


def get_option_premium(ticker, direction, strike, expiry_iso):
    try:
        import yfinance as yf
        chain = yf.Ticker(ticker).option_chain(expiry_iso)
        df = chain.calls if direction.upper() == "CALL" else chain.puts
        row = df[df["strike"] == strike]
        if row.empty:
            row = df.iloc[(df["strike"] - strike).abs().argsort()[:1]]
        if row.empty:
            return None
        r = row.iloc[0]
        bid = r.get("bid", 0) or 0
        ask = r.get("ask", 0) or 0
        last = r.get("lastPrice", 0) or 0
        mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
        return round(mid, 2) if mid > 0 else None
    except Exception as e:
        print(f"  [OPTION PREMIUM WARN] {ticker}: {e}")
        return None


def select_strike(ticker: str, direction: str, current_price: float, expiry_iso: str, target1: float) -> tuple:
    try:
        import yfinance as yf
        chain = yf.Ticker(ticker).option_chain(expiry_iso)
        df = chain.calls if direction.upper() == "CALL" else chain.puts
        if df.empty:
            raise ValueError("empty chain")
        if direction.upper() == "CALL":
            otm = df[df["strike"] > current_price].copy()
        else:
            otm = df[df["strike"] < current_price].copy()

        def _pick_from(candidates, tier_label):
            if candidates.empty:
                return None
            candidates = candidates.copy()
            candidates["dist"] = (candidates["strike"] - target1).abs()
            best = candidates.sort_values("dist").iloc[0]
            strike = float(best["strike"])
            oi = int(best.get("openInterest", 0) or 0)
            vol = int(best.get("volume", 0) or 0)
            bid = best.get("bid", 0) or 0
            ask = best.get("ask", 0) or 0
            last = best.get("lastPrice", 0) or 0
            mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
            premium = round(mid, 2) if mid > 0 else None
            pct_str = f"{premium / current_price * 100:.1f}%" if premium else "N/A"
            print(f"  [STRIKE] {ticker}: ${strike} ({tier_label}) OI={oi} vol={vol} premium=${premium} ({pct_str} of price) closest to T1 ${target1}")
            return strike, premium

        result = _pick_from(otm[otm["openInterest"].fillna(0) >= 100], "OI>=100")
        if result: return result
        result = _pick_from(otm[otm["openInterest"].fillna(0) >= 25], "OI>=25 fallback")
        if result: return result
        result = _pick_from(otm, "any OTM, no liquidity")
        if result: return result
        raise ValueError("no OTM strikes available")
    except Exception as e:
        inc = get_strike_increment(current_price)
        if direction.upper() == "CALL":
            atm = round((current_price // inc + 1) * inc, 2)
        else:
            atm = round((current_price // inc) * inc, 2)
        premium = get_option_premium(ticker, direction, atm, expiry_iso)
        print(f"  [STRIKE] {ticker}: ATM ${atm} fallback (OTM selection failed: {e})")
        return atm, premium


def compute_trade_levels(direction, bars, current_price, dte=9):
    atr = compute_daily_atr(bars)
    if atr <= 0:
        atr = current_price * 0.02
    entry_low = round(current_price * 0.995, 2)
    entry_high = round(current_price * 1.005, 2)
    dte_factor = max(1.0, (max(dte, 1) / 5) ** 0.5)
    move = round(atr * dte_factor, 2)
    if direction.upper() == "CALL":
        stop = round(current_price - atr * 0.75, 2)
        target1 = round(current_price + move, 2)
        target2 = round(current_price + move * 2, 2)
    else:
        stop = round(current_price + atr * 0.75, 2)
        target1 = round(current_price - move, 2)
        target2 = round(current_price - move * 2, 2)
    return {"entry_low": entry_low, "entry_high": entry_high, "stop": stop, "target1": target1, "target2": target2}


def find_swing_points(bars: list) -> tuple:
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    swing_highs, swing_lows = [], []
    for i in range(1, len(bars) - 1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            swing_highs.append((i, highs[i]))
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            swing_lows.append((i, lows[i]))
    return swing_highs, swing_lows


def is_clean_uptrend(bars: list) -> dict:
    window = bars[-10:] if len(bars) >= 10 else bars
    if len(window) < 5:
        return {"clean": False, "pattern": None}
    lows = [b["low"] for b in window]
    if all(lows[i + 1] >= lows[i] for i in range(len(lows) - 1)) and lows[-1] > lows[0]:
        return {"clean": True, "pattern": "higher lows"}
    swing_highs, swing_lows = find_swing_points(window)
    if len(swing_lows) >= 2:
        lows_seq = [v for _, v in swing_lows]
        if all(lows_seq[i] < lows_seq[i + 1] for i in range(len(lows_seq) - 1)):
            return {"clean": True, "pattern": "higher lows"}
    min_idx = min(range(len(window)), key=lambda i: window[i]["low"])
    if min_idx < len(window) - 2:
        low_val = window[min_idx]["low"]
        current_close = window[-1]["close"]
        recovery_pct = (current_close - low_val) / low_val if low_val else 0
        subsequent_lows = [window[i]["low"] for i in range(min_idx + 1, len(window))]
        if recovery_pct > 0.03 and all(l >= low_val for l in subsequent_lows):
            return {"clean": True, "pattern": "V-recovery"}
    return {"clean": False, "pattern": None}


def is_clean_downtrend(bars: list) -> dict:
    window = bars[-10:] if len(bars) >= 10 else bars
    if len(window) < 5:
        return {"clean": False, "pattern": None}
    highs = [b["high"] for b in window]
    if all(highs[i + 1] <= highs[i] for i in range(len(highs) - 1)) and highs[-1] < highs[0]:
        return {"clean": True, "pattern": "lower highs"}
    swing_highs, swing_lows = find_swing_points(window)
    if len(swing_highs) >= 2:
        highs_seq = [v for _, v in swing_highs]
        if all(highs_seq[i] > highs_seq[i + 1] for i in range(len(highs_seq) - 1)):
            return {"clean": True, "pattern": "lower highs"}
    max_idx = max(range(len(window)), key=lambda i: window[i]["high"])
    if max_idx < len(window) - 2:
        high_val = window[max_idx]["high"]
        current_close = window[-1]["close"]
        breakdown_pct = (high_val - current_close) / high_val if high_val else 0
        subsequent_highs = [window[i]["high"] for i in range(max_idx + 1, len(window))]
        if breakdown_pct > 0.03 and all(h <= high_val for h in subsequent_highs):
            return {"clean": True, "pattern": "breakdown"}
    return {"clean": False, "pattern": None}


def get_iv_vs_realized_vol_with_ratio(ticker: str, expiry_iso: str) -> tuple:
    try:
        import yfinance as yf
        import math
        stock = yf.Ticker(ticker)
        price = stock.info.get("regularMarketPrice") or stock.info.get("currentPrice")
        if not price:
            hist_1d = stock.history(period="1d")
            price = float(hist_1d["Close"].iloc[-1]) if not hist_1d.empty else None
        if not price or not expiry_iso:
            return "N/A", None
        chain = stock.option_chain(expiry_iso)
        calls = chain.calls[chain.calls["impliedVolatility"] > 0]
        puts = chain.puts[chain.puts["impliedVolatility"] > 0]
        if calls.empty or puts.empty:
            return "N/A", None
        common = set(calls["strike"].tolist()) & set(puts["strike"].tolist())
        if not common:
            return "N/A", None
        atm_strike = min(common, key=lambda s: abs(s - price))
        call_iv = float(calls[calls["strike"] == atm_strike]["impliedVolatility"].iloc[0])
        put_iv = float(puts[puts["strike"] == atm_strike]["impliedVolatility"].iloc[0])
        atm_iv = (call_iv + put_iv) / 2
        hist = stock.history(period="1mo")
        if hist.empty or len(hist) < 10:
            return "N/A", None
        closes = hist["Close"].values
        log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
        if len(log_returns) < 5:
            return "N/A", None
        mean_r = sum(log_returns) / len(log_returns)
        variance = sum((r - mean_r) ** 2 for r in log_returns) / (len(log_returns) - 1)
        realized_vol = math.sqrt(variance) * math.sqrt(252)
        if realized_vol <= 0:
            return "N/A", None
        ratio = round(atm_iv / realized_vol, 2)
        atm_iv_pct = round(atm_iv * 100, 1)
        realized_vol_pct = round(realized_vol * 100, 1)
        label = "richly priced" if ratio >= 1.5 else ("cheaply priced" if ratio <= 0.8 else "fairly priced")
        display_str = f"IV {atm_iv_pct}% vs {realized_vol_pct}% realized ({ratio}x, {label} vs recent movement)"
        return display_str, ratio
    except Exception as e:
        print(f"  [IV/RV WARN] {ticker}: {e}")
        return "N/A", None


def check_chart_pattern(flow_bias: str, bars: list) -> dict:
    if flow_bias == "Bullish":
        result = is_clean_uptrend(bars)
        return {"direction": "CALL", "clean": result["clean"], "pattern": result["pattern"]}
    elif flow_bias == "Bearish":
        result = is_clean_downtrend(bars)
        return {"direction": "PUT", "clean": result["clean"], "pattern": result["pattern"]}
    else:
        return {"direction": None, "clean": False, "pattern": None}


def get_quote_change(ticker: str) -> dict:
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                          params={"interval": "1d", "range": "10d"}, headers=HEADERS, timeout=10)
        result = r.json()["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]
        closes, opens, highs, lows = quote["close"], quote["open"], quote["high"], quote["low"]
        valid_idxs = [i for i in range(len(closes)) if closes[i] is not None]
        if len(valid_idxs) < 2:
            return {"price": None, "pct": None, "open": None, "high": None, "low": None}
        last_idx, prev_idx = valid_idxs[-1], valid_idxs[-2]
        price = closes[last_idx]
        prev_close = closes[prev_idx]
        pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else None
        return {"price": round(price, 2), "pct": pct,
                "open": round(opens[last_idx], 2) if opens[last_idx] else None,
                "high": round(highs[last_idx], 2) if highs[last_idx] else None,
                "low": round(lows[last_idx], 2) if lows[last_idx] else None}
    except Exception as e:
        print(f"  [QUOTE WARN] {ticker}: {e}")
        return {"price": None, "pct": None, "open": None, "high": None, "low": None}


def get_tone_phrase(m: dict) -> str:
    price, o, h, l, pct = m.get("price"), m.get("open"), m.get("high"), m.get("low"), m.get("pct")
    if not all([price, o, h, l]) or h == l:
        return "N/A"
    range_pos = (price - l) / (h - l)
    gapped = pct is not None and abs(pct) > 0.3
    if pct is not None and pct < 0:
        if range_pos < 0.3:
            return "Gap down, faded into the close" if gapped else "Weak close near session low"
        elif range_pos > 0.7:
            return "Gap down, recovered off the lows"
        else:
            return "Gap down, choppy session"
    elif pct is not None and pct > 0:
        if range_pos > 0.7:
            return "Gapped up, held gains" if gapped else "Firm close near session high"
        elif range_pos < 0.3:
            return "Gapped up, faded into the close"
        else:
            return "Mild grind higher"
    return "Flat, inside day"


MIN_DTE = 7

def get_next_expiry(ticker: str, min_dte: int = MIN_DTE) -> dict:
    try:
        import yfinance as yf
        expirations = yf.Ticker(ticker).options
        if not expirations:
            return {"label": "N/A", "iso": None}
        today = datetime.now(ET)
        today_str = today.strftime("%Y-%m-%d")
        for exp in expirations:
            if exp < today_str:
                continue
            exp_dt = datetime.strptime(exp, "%Y-%m-%d")
            dte = (exp_dt - today.replace(tzinfo=None)).days
            if dte >= min_dte:
                return {"label": exp_dt.strftime("%b %d"), "iso": exp}
        return {"label": "N/A", "iso": None}
    except Exception:
        return {"label": "N/A", "iso": None}


QUALITY_TAG_MAP = {
    "V-recovery": "V-Recovery Bounce",
    "higher lows": "Higher Lows Base",
    "lower highs": "Lower Highs Breakdown",
    "breakdown": "Clean Breakdown",
}


def build_quality_tag(pattern: str) -> str:
    return QUALITY_TAG_MAP.get(pattern, pattern.title() if pattern else "Pattern Match")


def build_price_narrative(c: dict) -> str:
    bars = c["bars"]
    if c["direction"] == "CALL":
        extreme_bar = min(bars, key=lambda b: b["low"])
        extreme_val, verb = extreme_bar["low"], "bottomed"
    else:
        extreme_bar = max(bars, key=lambda b: b["high"])
        extreme_val, verb = extreme_bar["high"], "topped"
    latest = bars[-1]
    extreme_date_str = extreme_bar["date"].strftime("%b %d").replace(" 0", " ")
    latest_date_str = latest["date"].strftime("%b %d").replace(" 0", " ")
    return f"{c['ticker']} {verb} at ${extreme_val:.2f} on {extreme_date_str}, closing at ${latest['close']:.2f} on {latest_date_str}."


def build_flow_note_display(flow: dict) -> str:
    premium = flow["premium"]
    premium_str = f"${premium / 1_000_000:.2f}M" if premium >= 1_000_000 else f"${premium / 1_000:.0f}K"
    return f"{premium_str} OTM/ATM {flow['bias'].lower()}, {flow['call_pct']}% call-weighted"


def build_time_pressure(c: dict) -> dict:
    try:
        expiry_dt = datetime.strptime(c["expiry_iso"], "%Y-%m-%d").date() if c.get("expiry_iso") else None
        today = datetime.now(ET).date()
        dte = (expiry_dt - today).days if expiry_dt else c.get("dte", "?")
    except Exception:
        dte = c.get("dte", "?")
    if c.get("strike") and c.get("current_price"):
        move_needed_pct = round((c["strike"] - c["current_price"]) / c["current_price"] * 100, 1)
        summary = f"needs {move_needed_pct:+.1f}% by {c['next_expiry']} ({dte} calendar days) to reach the ${c['strike']:g} strike"
    else:
        move_needed_pct = None
        summary = f"{dte} calendar days to {c['next_expiry']} expiry"
    return {"dte": dte, "move_needed_pct": move_needed_pct, "summary": summary}


def get_analyst_target(ticker: str) -> str:
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        target_mean = info.get("targetMeanPrice")
        current = info.get("regularMarketPrice") or info.get("currentPrice")
        rec_key = info.get("recommendationKey", "")
        if not target_mean or not current:
            return "Not provided"
        upside_pct = round((target_mean - current) / current * 100, 1)
        consensus = rec_key.replace("_", " ").title() if rec_key else "Not provided"
        return f"Analyst target ${target_mean:.2f} ({upside_pct:+.1f}% from current), consensus: {consensus}"
    except Exception as e:
        print(f"  [ANALYST WARN] {ticker}: {e}")
        return "Not provided"


def get_company_name(ticker: str) -> str:
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return info.get("shortName") or info.get("longName") or "Not provided"
    except Exception as e:
        print(f"  [NAME WARN] {ticker}: {e}")
        return "Not provided"


# ─────────────────────────────────────────────────────────────────────
# NEW: rich, subscriber-ready post generation (single long-form prompt)
# ─────────────────────────────────────────────────────────────────────

# The exact editor-style template supplied by the user. Kept verbatim
# so the requested structure/wording is preserved exactly.
RICH_POST_TEMPLATE = r"""You are an expert options-trading newsletter editor and trade-plan designer.

Your job is to convert my nightly “Top 5 Options Trade Ideas” scan output into a clear, practical, subscriber-ready post that both experienced traders and laymen can understand and act on.

The audience receives these ideas after market close for trading the next session. These are short-dated options setups, typically 1–3 weeks to expiration. The goal is not to create hype or imply certainty. The goal is to help subscribers choose the setup that best fits their risk tolerance and execute it with discipline.

## Core Rules

1. Do not treat all five setups as equal.
   - Rank them by practical trade quality and explain the difference.
   - Identify:
     - Best Overall Setup
     - Lowest-Move / Easiest Path Setup
     - Best Balanced Setup
     - Flow-Backed Momentum Setup
     - Speculative / High-Risk Breakout Setup
   - If the supplied data does not support one of these labels, use the closest truthful label.

2. Use plain English.
   - Translate technical language into what it means for the trader.
   - For example:
     - “Cheap implied volatility” → “The option premium looks relatively inexpensive compared with how much the stock has recently moved.”
     - “Bullish flow” → “Large traders recently positioned bullishly; it supports the setup but does not guarantee a move.”
     - “Higher lows” → “Buyers have repeatedly stepped in at higher prices, which is constructive until the stop breaks.”
   - Define unavoidable jargon briefly the first time it appears.

3. Focus on decision-making and execution.
   For every trade, clearly answer:
   - Why is this a setup now?
   - Why should a subscriber choose this trade over the other four?
   - What price action confirms entry?
   - How much does the stock need to move to reach the strike?
   - What makes the idea invalid?
   - What should the trader do at Target 1 and Target 2?
   - What makes the setup higher or lower risk?

4. Never imply that a trade is guaranteed.
   - State that bullish flow, analyst targets, technical patterns, and model rankings are supportive evidence—not certainty.
   - Avoid hype, exaggerated language, and unsupported claims.
   - Do not frame analyst price targets as the main reason to buy a short-dated call.

5. Emphasize short-dated option risk.
   - Include days to expiration.
   - Remind readers that time decay accelerates as expiration approaches.
   - Include a time-stop guideline: if the stock fails to show momentum within a defined number of sessions, consider reducing or exiting.
   - State that all stops are based on the underlying stock price unless explicitly specified otherwise.

6. Make it actionable for a layman.
   - Clearly distinguish “entry trigger” from “entry price.”
   - Explain that traders should not automatically buy at the open.
   - Include a no-chase rule: if price gaps materially above entry, wait for a pullback/hold or skip the trade.
   - Include a concise risk-management note: do not allocate so much to one idea that a stopped-out trade materially damages the account.

## Required Output Format

Create the post in this exact structure:

# TRADE IDEAS — [DAY, DATE]

**Expiry:** [DATE]
**Direction:** Calls / Puts
**Market backdrop:** [1–2 plain-English sentences about SPY/QQQ/IWM and the market environment.]
**How to use tonight’s list:** [1–2 sentences explaining that these are independent setups and subscribers should select trades based on setup quality, risk, and triggered entry—not buy all five automatically.]

## Tonight’s Trade Map

Create a concise table:

| Role | Ticker / Contract | Why Choose It | Risk Level |
|---|---|---|---|
| Best Overall | | | |
| Lowest-Move Setup | | | |
| Best Balanced Setup | | | |
| Flow-Backed Setup | | | |
| Speculative Breakout | | | |

Use the available data only. If a role cannot be assigned honestly, use a more accurate label.

## Best Choice Tonight

Write 2–4 concise sentences naming the best overall setup and explaining why it ranks first. Include the strongest combination of:
- Technical structure
- Options-flow support
- Required move to strike
- Option pricing/value
- Catalyst or relative strength, if available

Then add:

**If choosing one trade tonight:** [TICKER / CONTRACT]
**Why:** [one-sentence explanation]

## The Five Setups

Use this exact layout for each idea:

### [Rank]. [TICKER] — [EXPIRY] [STRIKE][C/P]
**Role:** [Best Overall / Lower-Move Setup / Balanced / Flow-Backed / Speculative Breakout]
**Risk level:** [Low / Moderate / High / Speculative]
**Best for:** [one concise sentence describing the kind of trader or objective this suits.]

**Why it made the list:**
[2–3 plain-English sentences combining chart structure, options flow, volatility/value, sector strength, catalyst, analyst context, or other provided evidence. Explain what each data point means rather than merely listing it.]

**Why choose this over the others:**
[One sentence highlighting the key differentiator.]

**Trade plan:**
- **Entry trigger:** [Example: “Enter only if price holds above $X” or “Enter on a confirmed break and hold above $X.”]
- **Avoid chasing:** [Specific instruction based on supplied levels; if unavailable, say not to chase a sharp opening gap and wait for confirmation.]
- **Underlying entry zone:** [price/range]
- **Stop / invalidation:** [price; clarify it is based on underlying stock price]
- **Target 1:** [price and action, e.g., “Consider taking partial profits.”]
- **Target 2:** [price and action, e.g., “Hold remaining position only if momentum remains intact.”]
- **Move needed to strike:** [X% and/or $X]
- **Days to expiration:** [X]
- **Time-stop:** [Example: “If it has not begun moving in your favor within 3–4 trading sessions, reassess or reduce.”]

**Plain-English risk:**
[One sentence describing the primary risk. Example: “The stock needs a large move in limited time, so this is a smaller-size, confirmation-only trade.”]

## Execution Rules

Include 5–7 concise bullets:

- These are triggered setups, not automatic buy-at-open alerts.
- Use the underlying stock entry and stop levels; option premiums can move differently because of implied volatility and time decay.
- Do not chase a large opening gap; wait for the price to hold above the trigger or pull back constructively.
- Consider taking partial profits at Target 1 and managing the remaining position toward Target 2.
- Use smaller size for high-risk or speculative trades.
- If the trade does not develop within a few sessions, time decay can become a major headwind.
- Broad-market strength can help, but each setup needs its own chart and/or catalyst to work.

## Risk Disclosure

Write one short, plain-English paragraph:

“These are informational trade ideas, not financial advice. Options can lose value quickly, especially near expiration. Use position sizing, follow stops, and only take trades that fit your own risk tolerance.”

## Card Copy

After the written post, create concise copy for a visual trade card for each ticker. Use this exact structure:

### Card: [TICKER]

**[Rank Label, e.g., #1 TOP PICK]**
**[CALL/PUT] | [EXPIRY] | $[STRIKE] Strike**
**Risk:** [Low / Moderate / High / Speculative]
**Why now:** [One short sentence]
**Trigger:** [Enter only above/below $X]
**Stop:** [$X underlying price]
**Target 1:** [$X — take partials]
**Target 2:** [$X — trail remaining position]
**Move to strike:** [X%]
**Time remaining:** [X DTE]
**Key note:** [One short, simple risk or execution reminder]

## Style Requirements

- Write like a disciplined professional trader, not a marketer.
- Keep paragraphs short.
- Use direct language.
- Make all numbers easy to scan.
- Do not over-explain every metric.
- Do not say “this is guaranteed,” “easy money,” “cannot lose,” or similar language.
- Do not invent catalysts, data, prices, or reasoning not included in the source data.
- If data is missing, label it “Not provided” rather than guessing.
- Preserve all supplied entry, stop, target, flow, option pricing, and expiration data accurately.
- Prioritize clarity over sophistication.

## Source Data

I will provide:
1. Market summary and index performance
2. Five scanned trade ideas
3. Contract details
4. Chart setup details
5. Entry, stop, Target 1, and Target 2
6. Options-flow data
7. Required move / expected move / implied volatility data
8. Analyst target or catalyst information when available

Using only that information, generate the complete subscriber-ready post and the five card-copy blocks."""

# Non-negotiable operational rules from the production script, layered
# on top of the template above without changing its requested
# structure. These exist because of real Discord-formatting and
# accuracy bugs already fixed in bmt_nightly_setups.py.
ADDITIONAL_OPERATIONAL_RULES = """- Every single ticker mention, anywhere in the post or the card copy blocks, must be prefixed with a dollar sign (e.g. "$AXTI", "$SPY"), every time it appears -- not just on first mention.
- Do NOT include any URLs, website names, or "according to [source]" citations anywhere in the output -- write facts in plain prose without naming or linking sources. Discord auto-generates broken-looking link preview embeds from any URL or domain-like text, so citing a source by name or link breaks the formatting badly.
- If you reference any event or date found via search, you MUST state whether it falls BEFORE or AFTER that specific contract's expiry date. An event after expiry is not a direct catalyst for that trade -- omit it or frame it explicitly as pre-positioning context only.
- The five setups supplied below have ALREADY been screened for reasonable option pricing and a clean chart pattern before you saw them -- do not write as if reconsidering whether they are worth trading; lead with what supports each idea, not doubt about it, unless something genuinely concerning turns up in search.
- Return ONLY the post and the five card-copy blocks, starting directly with "# TRADE IDEAS —". No preamble, no commentary, no markdown code fences before or after."""


def build_source_data_block(selected: list, market_context: dict, target_date: datetime) -> str:
    lines = []
    lines.append(f"### 1. Market summary and index performance (as of last close)")
    for t in MARKET_CONTEXT_TICKERS:
        m = market_context.get(t, {})
        lines.append(f"- ${t}: ${m.get('price', 'N/A')} ({m.get('pct', 'N/A')}%) -- {get_tone_phrase(m)}")
    lines.append("")
    lines.append(f"Ideas are for the next trading session: {target_date.strftime('%A, %B %d, %Y')}.")
    lines.append("")
    lines.append("### 2-8. Five scanned trade ideas, in descending conviction/ranking-score order (setup #1 scored highest by the deterministic flow-intensity/pricing model; this ordering is a useful input to your ranking but you should still evaluate and re-rank based on the full picture, per the Core Rules above)")
    lines.append("")
    for i, c in enumerate(selected):
        tp = c.get("time_pressure") or build_time_pressure(c)
        lines.append(f"---- SETUP #{i+1} (model rank order, not necessarily your final rank) ----")
        lines.append(f"Ticker: ${c['ticker']}   Company: {c.get('company_name', 'Not provided')}")
        lines.append(f"Contract: {c['direction']} ${c['strike']:g} strike, expiring {c['next_expiry']} ({tp['dte']} calendar days / days to expiration from today)")
        lines.append(f"Chart setup: {build_price_narrative(c)} Pattern classification: {build_quality_tag(c.get('pattern', ''))}.")
        lines.append(f"Entry zone (underlying stock price): ${c['entry_low']}-${c['entry_high']}")
        lines.append(f"Stop / invalidation (underlying stock price): ${c['stop']}")
        lines.append(f"Target 1 (underlying stock price): ${c['target1']}")
        lines.append(f"Target 2 (underlying stock price): ${c['target2']}")
        lines.append(f"Options-flow data: {build_flow_note_display(c['flow'])}")
        lines.append(f"Required move / implied volatility data: {tp['summary']}. {c.get('iv_rv_str', 'Not provided')}")
        lines.append(f"Analyst target / catalyst info: {c.get('analyst_target', 'Not provided')}")
        lines.append("")
    return "\n".join(lines)


def write_rich_post(selected: list, market_context: dict, target_date: datetime) -> str:
    source_data = build_source_data_block(selected, market_context, target_date)
    prompt = f"""{RICH_POST_TEMPLATE}

---

ADDITIONAL OPERATIONAL RULES (apply on top of everything above -- do not deviate from these, and do not let them change the structure/format requested above):
{ADDITIONAL_OPERATIONAL_RULES}

---

SOURCE DATA FOR TONIGHT ({target_date.strftime('%A, %B %d, %Y')}):

{source_data}

Now generate the complete output exactly as specified above (the full post, then the five card-copy blocks). Return ONLY that content."""

    resp = requests.post(
        f"{OPENROUTER_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json={"model": "moonshotai/kimi-k2.6", "max_tokens": 16000, "temperature": 0,
              "plugins": [{"id": "web"}],
              "messages": [{"role": "user", "content": prompt}]},
        timeout=180
    )
    raw = resp.json()
    if "choices" not in raw:
        print("  [RICH POST ERROR] unexpected response (no 'choices' key):")
        print(f"  {json.dumps(raw, indent=2)[:1000]}")
        raise ValueError("write_rich_post: unexpected API response shape")
    message = raw["choices"][0]["message"]
    content = message.get("content")
    if not content:
        print("  [RICH POST ERROR] empty/None content -- likely an incomplete tool call:")
        print(f"  {json.dumps(message, indent=2)[:1000]}")
        raise ValueError("write_rich_post: empty content in API response")
    content = content.strip()
    content = re.sub(r"^```(?:markdown|md)?\s*", "", content)
    content = re.sub(r"```\s*$", "", content)
    return content.strip()


# ─────────────────────────────────────────────────────────────────────
# Card image renderer -- reused from bmt_nightly_setups.py, with a
# visible "V2 TEST" watermark so it can't be mistaken for production.
# ─────────────────────────────────────────────────────────────────────

import textwrap

def fit_value_fontsize(text: str, col_w_units: float, base_fontsize: float, min_fontsize: float = 6.5, margin: float = 0.85) -> float:
    bold_factor = 1.75
    avail_px = col_w_units * 150 * margin
    needed_px = len(text) * base_fontsize * bold_factor
    if needed_px <= avail_px:
        return base_fontsize
    return max(base_fontsize * avail_px / needed_px, min_fontsize)


def wrap_lines(text: str, width_chars: int, max_lines: int) -> list:
    lines = textwrap.wrap(text, width=width_chars)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + "..."
    return lines


def escape_dollars_for_matplotlib(text: str) -> str:
    return text.replace("$", r"\$") if text else text


def render_card(accepted: list, market_theme: str, risk_notes: str,
                 market_context: dict, target_date: datetime, data_date: datetime, out_path: str):
    BG = "#0a0a0f"
    SURFACE = "#131318"
    BORDER = "#232329"
    BORDER_SOFT = "#1c1c22"
    TEXT_PRIMARY = "#f5f5f7"
    TEXT_SECONDARY = "#9a9aa5"
    TEXT_TERTIARY = "#5f5f68"
    GREEN = "#34d399"
    RED = "#f87171"
    GOLD = "#fbbf24"
    BLUE = "#60a5fa"

    n = len(accepted)
    n_puts = sum(1 for s in accepted if s["direction"].upper() == "PUT")
    n_calls = n - n_puts
    dir_summary = "All puts" if n_puts == n else "All calls" if n_calls == n else f"{n_calls} calls, {n_puts} puts"
    expiries = set(s.get("next_expiry", "") for s in accepted)
    expiry_summary = f"All {list(expiries)[0]} expiry" if len(expiries) == 1 else "Mixed expiries"

    today_str = target_date.strftime("%A, %B %d")
    close_date_str = data_date.strftime("%-m/%-d") if os.name != "nt" else data_date.strftime("%#m/%#d")

    fig_w = 24.0
    CARD_GAP = 0.22
    MAX_CARD_W = 8.0
    max_available_w = fig_w - 1.0
    dynamic_w = (max_available_w - CARD_GAP * (max(n, 1) - 1)) / max(n, 1)
    card_w = min(dynamic_w, MAX_CARD_W)
    row_w = max(n, 1) * card_w + (max(n, 1) - 1) * CARD_GAP
    row_start_x = 0.5 + (max_available_w - row_w) / 2
    placeholder_h = 8.0
    fig_h = 5.4 + placeholder_h
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=150, facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, fig_w); ax.set_ylim(0, fig_h); ax.axis("off"); ax.invert_yaxis()

    ax.text(0.5, 0.3, "BMT WATCHLIST", fontsize=12, fontweight="bold", color=TEXT_TERTIARY, va="top", zorder=5)
    ax.text(fig_w - 0.5, 0.3, "V2 TEST -- INTERNAL REVIEW", fontsize=12, fontweight="bold", color=GOLD, va="top", ha="right", zorder=5)
    ax.text(0.5, 0.85, today_str, fontsize=32, fontweight="bold", color=TEXT_PRIMARY, va="top", zorder=5)
    ax.text(0.5, 1.55, f"{n} setups   \u00b7   {dir_summary}   \u00b7   {expiry_summary}   \u00b7   Based on {close_date_str} close",
            fontsize=12, color=TEXT_SECONDARY, va="top", zorder=5)

    ctx_y = 2.15
    ctx_w = (fig_w - 1.0 - 0.5 * 2) / 3
    for i, t in enumerate(MARKET_CONTEXT_TICKERS):
        x = 0.5 + i * (ctx_w + 0.5)
        m = market_context.get(t, {})
        pct = m.get("pct")
        color = GREEN if (pct or 0) >= 0 else RED
        arrow = "\u2191" if (pct or 0) >= 0 else "\u2193"
        ax.text(x, ctx_y, f"${t}", fontsize=13, fontweight="bold", color=TEXT_SECONDARY, va="top", zorder=5)
        ax.text(x, ctx_y + 0.4, f"${m.get('price', '?')}", fontsize=20, fontweight="bold", color=TEXT_PRIMARY, va="top", zorder=5)
        ax.text(x, ctx_y + 0.88, get_tone_phrase(m), fontsize=9, color=TEXT_TERTIARY, va="top", zorder=5)
        ax.text(x + ctx_w, ctx_y, f"{arrow} {abs(pct):.2f}%" if pct is not None else "N/A",
                fontsize=14, fontweight="bold", color=color, va="top", ha="right", zorder=5)
        if i > 0:
            divider_x = x - 0.25
            ax.plot([divider_x, divider_x], [ctx_y, ctx_y + 1.1], color=BORDER, linewidth=1, zorder=4)
    rule_y = ctx_y + 1.3
    ax.plot([0.5, fig_w - 0.5], [rule_y, rule_y], color=BORDER, linewidth=1, zorder=3)
    cursor_y = rule_y + 0.35

    theme_color = RED if n_puts > n_calls else GREEN if n_calls > n_puts else BLUE
    theme_lines = wrap_lines(escape_dollars_for_matplotlib(market_theme), width_chars=160, max_lines=3)
    for i, line in enumerate(theme_lines):
        if i == 0:
            ax.add_patch(plt.Rectangle((0.5, cursor_y + 0.02), 0.06, 0.26, facecolor=theme_color, linewidth=0, zorder=4))
        ax.text(0.72, cursor_y, line, fontsize=11.5, color=TEXT_PRIMARY, va="top", zorder=5)
        cursor_y += 0.3
    cursor_y += 0.25

    risk_lines = wrap_lines(escape_dollars_for_matplotlib(risk_notes), width_chars=160, max_lines=4)
    for i, line in enumerate(risk_lines):
        if i == 0:
            ax.add_patch(plt.Rectangle((0.5, cursor_y + 0.02), 0.06, 0.26, facecolor=GOLD, linewidth=0, zorder=4))
        ax.text(0.72, cursor_y, line, fontsize=10.5, color=TEXT_SECONDARY, va="top", zorder=5)
        cursor_y += 0.28
    cursor_y += 0.45

    PAD_L = 0.4
    PAD_TOP = 0.35
    HEADER_H = 0.55
    SUBTITLE_H = 0.4
    GAP1 = 0.2
    BADGE_H = 0.32
    GAP2 = 0.14
    NARRATIVE_LINE_H = 0.23
    GAP3 = 0.28
    STAT_LABEL_H = 0.24
    STAT_VALUE_H = 0.4
    GAP4 = 0.32
    FLOW_H = 0.3
    PAD_BOTTOM = 0.3

    narrative_line_counts = []
    for s in accepted:
        lines = wrap_lines(escape_dollars_for_matplotlib(s.get("narrative", "")), width_chars=48, max_lines=6)
        s["_narrative_lines"] = lines
        narrative_line_counts.append(len(lines))
    max_narrative_lines = max(narrative_line_counts) if narrative_line_counts else 1

    card_h = (PAD_TOP + HEADER_H + SUBTITLE_H + GAP1 + BADGE_H + GAP2
              + max_narrative_lines * NARRATIVE_LINE_H + GAP3
              + STAT_LABEL_H + STAT_VALUE_H + GAP4 + FLOW_H + PAD_BOTTOM)

    fig_h = cursor_y + card_h + 1.0
    fig.set_size_inches(fig_w, fig_h)
    ax.set_ylim(0, fig_h); ax.invert_yaxis()

    cards_top = cursor_y
    for idx, s in enumerate(accepted):
        x = row_start_x + idx * (card_w + CARD_GAP)
        is_call = s["direction"].upper() == "CALL"
        accent = GREEN if is_call else RED

        card_bg = FancyBboxPatch((x, cards_top), card_w, card_h, boxstyle="round,pad=0,rounding_size=0.06",
                                  linewidth=1, edgecolor=BORDER_SOFT, facecolor=SURFACE, zorder=2)
        ax.add_patch(card_bg)
        ax.add_patch(plt.Rectangle((x, cards_top + 0.15), 0.06, card_h - 0.3, facecolor=accent, linewidth=0, zorder=3))

        cx = x + PAD_L
        yy = cards_top + PAD_TOP

        ax.text(cx, yy, f"${s['ticker']}", fontsize=21, fontweight="bold", color=TEXT_PRIMARY, va="top", zorder=5)
        arrow = "\u25b2" if is_call else "\u25bc"
        ax.text(x + card_w - 0.3, yy + 0.02, f"{arrow} {s['direction']} ${s['strike']:g}",
                fontsize=13, fontweight="bold", color=accent, va="top", ha="right", zorder=5)
        yy += HEADER_H

        ax.text(cx, yy, f"${s.get('current_price', '?')} close  \u00b7  {s.get('company_name', '')}",
                fontsize=8.7, color=TEXT_TERTIARY, va="top", zorder=5)
        ax.text(x + card_w - 0.3, yy, f"{s.get('next_expiry', '')} \u00b7 {s.get('dte', '?')} DTE",
                fontsize=8.7, color=TEXT_TERTIARY, va="top", ha="right", zorder=5)
        yy += SUBTITLE_H + GAP1

        ax.scatter([cx + 0.05], [yy + 0.16], s=18, color=accent, zorder=5)
        ax.text(cx + 0.2, yy, escape_dollars_for_matplotlib(s.get("quality_tag", "")).upper(), fontsize=8.5, fontweight="bold",
                color=accent, va="top", zorder=5)
        yy += BADGE_H + GAP2

        for line in s["_narrative_lines"]:
            ax.text(cx, yy, line, fontsize=9, color=TEXT_SECONDARY, va="top", zorder=5)
            yy += NARRATIVE_LINE_H
        yy += (max_narrative_lines - len(s["_narrative_lines"])) * NARRATIVE_LINE_H
        yy += GAP3

        total_w = card_w - 2 * (PAD_L - 0.1)
        col_weights = [1.3, 0.9, 0.9, 0.9]
        col_widths = [total_w * w / sum(col_weights) for w in col_weights]
        labels = ["ENTRY", "STOP", "TARGET 1", "TARGET 2"]
        values = [f"${s['entry_low']}\u2013${s['entry_high']}", f"${s['stop']}", f"${s['target1']}", f"${s['target2']}"]
        colors = [TEXT_PRIMARY, RED, GREEN, GREEN]
        base_sizes = [10, 12, 12, 12]
        col_x = cx - 0.1
        for ci, (lab, val, vc, base_sz, cw) in enumerate(zip(labels, values, colors, base_sizes, col_widths)):
            if ci > 0:
                ax.plot([col_x, col_x], [yy, yy + STAT_LABEL_H + STAT_VALUE_H - 0.05], color=BORDER, linewidth=0.8, zorder=4)
            fitted_sz = fit_value_fontsize(val, cw, base_sz)
            ax.text(col_x + cw / 2, yy, lab, fontsize=6.8, color=TEXT_TERTIARY, va="top", ha="center", zorder=5)
            ax.text(col_x + cw / 2, yy + STAT_LABEL_H, val, fontsize=fitted_sz, fontweight="bold", color=vc, va="top", ha="center", zorder=5)
            col_x += cw
        yy += STAT_LABEL_H + STAT_VALUE_H + GAP4

        ax.text(cx, yy, f"FLOW   {escape_dollars_for_matplotlib(s.get('flow_note', ''))}", fontsize=8, color=TEXT_TERTIARY, va="top", zorder=5)

    footer_y = cards_top + card_h + 0.4
    ax.plot([0.5, fig_w - 0.5], [footer_y, footer_y], color=BORDER, linewidth=1, zorder=3)
    ax.text(fig_w / 2, footer_y + 0.3,
            f"Setups derived from {close_date_str} close   \u00b7   Re-validate at next session's open   \u00b7   V2 TEST BUILD, not the live production post",
            fontsize=9, color=TEXT_TERTIARY, va="top", ha="center", zorder=5)

    plt.savefig(out_path, facecolor=BG, bbox_inches="tight", pad_inches=0.35)
    plt.close(fig)


def post_image_to_discord(image_path: str, message: str = ""):
    with open(image_path, "rb") as f:
        files = {"file": (os.path.basename(image_path), f, "image/png")}
        data = {"content": message}
        r = requests.post(DISCORD_WEBHOOK, data=data, files=files, timeout=30)
        print(f"Discord image post: {r.status_code}")
        return r.status_code in (200, 204)


US_MARKET_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}
US_MARKET_EARLY_CLOSE_2026 = {"2026-11-27", "2026-12-24"}


def _check_holiday_list_freshness():
    current_year = datetime.now(ET).year
    if current_year != 2026:
        print(f"  [HOLIDAY LIST WARNING] Running in {current_year}, but US_MARKET_HOLIDAYS_2026 is hardcoded for 2026 only — THIS LIST MUST BE UPDATED for {current_year}.")


def is_trading_day(d: datetime) -> bool:
    if d.weekday() >= 5:
        return False
    if d.strftime("%Y-%m-%d") in US_MARKET_HOLIDAYS_2026:
        return False
    return True


def should_publish_tonight() -> bool:
    _check_holiday_list_freshness()
    today = datetime.now(ET)
    tomorrow = today + timedelta(days=1)
    return is_trading_day(tomorrow)


def get_next_actual_trading_day() -> datetime:
    d = datetime.now(ET) + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def get_target_trading_day() -> datetime:
    return datetime.now(ET) + timedelta(days=1)


def get_last_completed_trading_day() -> datetime:
    d = datetime.now(ET)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def strip_urls_and_domains(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\b[a-zA-Z0-9][a-zA-Z0-9-]*\.(com|net|org|io|ai|co|gov)\b", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def ensure_dollar_prefixed_tickers(text: str, tickers: list) -> str:
    if not text:
        return text
    for t in sorted(set(tickers), key=len, reverse=True):
        text = re.sub(rf"(?<!\$)\b{re.escape(t)}\b", f"${t}", text)
    return text


def chunk_markdown_for_discord(text: str, limit: int = 1900) -> list:
    """
    Splits the rich post into Discord-safe (<=2000 char) chunks,
    breaking only on paragraph (blank-line) boundaries so no sentence,
    bullet, or table row is ever cut mid-way. Falls back to line-level
    splitting only for a single paragraph that itself exceeds the
    limit (shouldn't normally happen given the template's structure).
    """
    paragraphs = re.split(r"\n\s*\n", text.strip())
    chunks = []
    current = ""
    for p in paragraphs:
        candidate = f"{current}\n\n{p}" if current else p
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(p) <= limit:
            current = p
            continue
        sub_current = ""
        for line in p.split("\n"):
            sub_candidate = f"{sub_current}\n{line}" if sub_current else line
            if len(sub_candidate) <= limit:
                sub_current = sub_candidate
            else:
                if sub_current:
                    chunks.append(sub_current)
                sub_current = line[:limit]
        current = sub_current
    if current:
        chunks.append(current)
    return chunks


def post_text_chunks_to_discord(chunks: list) -> bool:
    ok_all = True
    n = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        content = chunk if n == 1 else f"{chunk}\n\n_(part {i}/{n})_"
        try:
            r = requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=30)
            ok = r.status_code in (200, 204)
            print(f"  [DISCORD] part {i}/{n}: {r.status_code} ({len(content)} chars)")
            if not ok:
                print(f"    body: {r.text[:300]}")
            ok_all = ok_all and ok
        except Exception as e:
            print(f"  [DISCORD] part {i}/{n} FAILED: {e}")
            ok_all = False
        time.sleep(1)
    return ok_all


def main():
    et_now = datetime.now(ET)
    print(f"[{et_now.isoformat()}] BMT Nightly Setups V2 TEST")

    force_publish = os.environ.get("FORCE_PUBLISH") == "1"
    if force_publish:
        print("  [FORCE_PUBLISH=1 — scheduling gate BYPASSED for testing. This must NEVER happen on a real production run.]")
        target_date = get_next_actual_trading_day()
    elif not should_publish_tonight():
        tomorrow = et_now + timedelta(days=1)
        print(f"  Tomorrow ({tomorrow.strftime('%A, %B %d')}) is not a trading day (weekend or market holiday) — skipping tonight's run entirely.")
        return
    else:
        target_date = get_target_trading_day()

    data_date = get_last_completed_trading_day()
    print(f"  [V2 TEST] Publishing ideas for {target_date.strftime('%A, %B %d')}, using data as of {data_date.strftime('%A, %B %d')} close.\n")

    print(f"Universe: {len(CANDIDATE_UNIVERSE)} candidate tickers (ETFs/leveraged/crypto excluded)")

    print("\nLoading upcoming earnings calendar for the exclusion filter...")
    earnings_map = get_upcoming_earnings_map()

    print("\nPulling market context (SPY/QQQ/IWM)...")
    market_context = {t: get_quote_change(t) for t in MARKET_CONTEXT_TICKERS}
    for t, m in market_context.items():
        print(f"  {t}: ${m['price']} ({m['pct']}%)")

    print(f"\nScanning {len(CANDIDATE_UNIVERSE)} candidates for qualifying flow (>= ${MIN_PREMIUM:,})...")
    qualifying = []
    for ticker in CANDIDATE_UNIVERSE:
        flow = get_flow_for_ticker(ticker)
        if flow["bias"] and flow["premium"] >= MIN_PREMIUM:
            print(f"  [FLOW] {ticker}: {flow['bias']} ${flow['premium']:,.0f} ({flow['call_pct']}% call)")
            qualifying.append({"ticker": ticker, "flow": flow})

    print(f"\n{len(qualifying)} ticker(s) cleared the flow filter.")
    if not qualifying:
        print("Nothing qualifies tonight — no digest to post.")
        return

    print(f"\nChecking {len(qualifying)} qualifying candidate(s) for same-day/recent earnings...")
    still_qualifying = []
    for q in qualifying:
        same_day_er = get_earnings_today_and_recent(q["ticker"])
        if same_day_er:
            print(f"  [SAME-DAY ER EXCLUDE] {q['ticker']}: reported/reports earnings {same_day_er} — excluded regardless of flow strength")
            continue
        still_qualifying.append(q)
    qualifying = still_qualifying
    print(f"  {len(qualifying)} candidate(s) remain after same-day earnings exclusion")

    if not qualifying:
        print("Nothing qualifies tonight after same-day earnings exclusion — no digest to post.")
        return

    print("Pulling daily OHLC + next expiry for qualifying candidates...")
    candidates = []
    for q in qualifying:
        bars = get_daily_ohlc(q["ticker"])
        if not bars:
            print(f"  [SKIP] {q['ticker']}: no price history")
            continue
        expiry = get_next_expiry(q["ticker"])
        avg_dollar_vol = compute_avg_dollar_volume(bars)
        candidates.append({
            "ticker": q["ticker"], "flow": q["flow"], "bars": bars,
            "ohlc_text": format_ohlc_summary(bars),
            "next_expiry": expiry["label"], "expiry_iso": expiry["iso"],
            "avg_dollar_vol": avg_dollar_vol,
        })

    print(f"\nApplying deterministic chart-pattern filter to {len(candidates)} candidate(s)...")
    pattern_matched = []
    for c in candidates:
        pattern = check_chart_pattern(c["flow"]["bias"], c["bars"])
        if pattern["clean"]:
            c["direction"] = pattern["direction"]
            c["pattern"] = pattern["pattern"]
            pattern_matched.append(c)
            print(f"  [PATTERN OK] {c['ticker']}: {pattern['direction']} — {pattern['pattern']}")

    IV_RV_HARD_EXCLUDE_RATIO = 2.5

    print(f"\nChecking IV vs realized volatility for {len(pattern_matched)} pattern-matched candidate(s)...")
    for c in pattern_matched:
        c["iv_rv_str"], c["iv_rv_ratio"] = get_iv_vs_realized_vol_with_ratio(c["ticker"], c.get("expiry_iso"))
        print(f"  {c['ticker']}: {c['iv_rv_str']}")

    pre_exclude_count = len(pattern_matched)
    still_viable = []
    for c in pattern_matched:
        if c["iv_rv_ratio"] is not None and c["iv_rv_ratio"] > IV_RV_HARD_EXCLUDE_RATIO:
            print(f"  [IV/RV EXCLUDE] {c['ticker']}: IV/RV at {c['iv_rv_ratio']}x — premium too rich to justify entry")
        else:
            still_viable.append(c)
    pattern_matched = still_viable
    print(f"  {len(pattern_matched)}/{pre_exclude_count} candidate(s) remain after IV/RV pricing filter")

    for c in pattern_matched:
        if c["avg_dollar_vol"] > 0:
            c["flow_intensity"] = c["flow"]["premium"] / c["avg_dollar_vol"]
        else:
            c["flow_intensity"] = 0.0
        if c["iv_rv_ratio"] is not None:
            pricing_multiplier = max(0.4, 1.0 / c["iv_rv_ratio"])
        else:
            pricing_multiplier = 0.75
        c["ranking_score"] = c["flow_intensity"] * pricing_multiplier

    pattern_matched.sort(key=lambda c: (c["ranking_score"], c["flow"]["premium"]), reverse=True)

    selected = []
    for c in pattern_matched:
        if len(selected) >= TOP_N:
            break
        yf_er = get_upcoming_earnings_date(c["ticker"])
        finnhub_er = earnings_map.get(c["ticker"])
        er_dates = [d for d in (yf_er, finnhub_er) if d]
        er_date = min(er_dates) if er_dates else None
        if er_date:
            if c.get("expiry_iso"):
                blocks = er_date <= c["expiry_iso"]
            else:
                cutoff = (datetime.now(ET) + timedelta(days=7)).strftime("%Y-%m-%d")
                blocks = er_date <= cutoff
            if blocks:
                print(f"  [ER EXCLUDE] {c['ticker']}: reports earnings {er_date}")
                continue
        selected.append(c)

    print(f"\n{len(selected)} of {len(candidates)} passed filters; taking top {len(selected)} by flow intensity.")

    if not selected:
        print("Nothing passed the deterministic chart-pattern filter tonight — no digest to post.")
        return

    print("\nComputing trade levels and selecting strikes...")
    for c in selected:
        current_price = get_quote_change(c["ticker"]).get("price")
        if not current_price:
            print(f"  [WARN] {c['ticker']}: no current price -- dropping from selected")
            continue
        c["current_price"] = current_price
        dte = 9
        if c.get("expiry_iso"):
            try:
                exp_dt = datetime.strptime(c["expiry_iso"], "%Y-%m-%d")
                dte = max((exp_dt - datetime.now(ET).replace(tzinfo=None)).days, 0)
            except Exception:
                pass
        c["dte"] = dte
        c.update(compute_trade_levels(c["direction"], c["bars"], current_price, dte=dte))
        strike, premium = select_strike(c["ticker"], c["direction"], current_price, c.get("expiry_iso", ""), c["target1"])
        c["strike"] = strike
        c["premium"] = premium
    selected = [c for c in selected if "strike" in c]

    print("\nComputing analyst target + company name + time-pressure context for final selections...")
    for c in selected:
        c["analyst_target"] = get_analyst_target(c["ticker"])
        c["company_name"] = get_company_name(c["ticker"])
        c["time_pressure"] = build_time_pressure(c)
        c["quality_tag"] = build_quality_tag(c.get("pattern", ""))
        c["narrative"] = build_price_narrative(c)
        c["flow_note"] = build_flow_note_display(c["flow"])
        print(f"  {c['ticker']}: {c['time_pressure']['summary']} | {c['analyst_target']}")

    print(f"\nGenerating rich subscriber-ready post for {len(selected)} setup(s) (V2 TEST format)...")
    rich_post = write_rich_post(selected, market_context, target_date)

    all_tickers = [c["ticker"] for c in selected] + list(MARKET_CONTEXT_TICKERS)
    rich_post = strip_urls_and_domains(rich_post)
    rich_post = ensure_dollar_prefixed_tickers(rich_post, all_tickers)

    print(f"\n=== RICH POST ({len(rich_post)} chars total) ===\n{rich_post}\n")

    # Simple market_theme/risk_notes for the card image -- kept short,
    # separate from (and much simpler than) the rich post above.
    spy = market_context.get("SPY", {})
    qqq = market_context.get("QQQ", {})
    market_theme = (f"$SPY closed at ${spy.get('price', 'N/A')} ({spy.get('pct', 'N/A')}%) and "
                     f"$QQQ at ${qqq.get('price', 'N/A')} ({qqq.get('pct', 'N/A')}%).")
    risk_notes = "See the full write-up below for entry triggers, stops, and risk notes on each setup."

    out_path = "bmt_nightly_setups_v2_test.png"
    render_card(selected, market_theme, risk_notes, market_context, target_date, data_date, out_path)
    print(f"\nCard saved to {out_path}")

    posted_card = post_image_to_discord(out_path, message="🧪 **V2 TEST BUILD** — internal review only, not the live production post")

    chunks = chunk_markdown_for_discord(rich_post, limit=1900)
    print(f"\nPosting rich post as {len(chunks)} Discord message(s)...")
    posted_text = post_text_chunks_to_discord(chunks)

    if posted_text and posted_card:
        print("✓ V2 TEST: card + rich post posted to Discord!")
    else:
        if not posted_text:
            print("✗ V2 TEST: rich post FAILED")
        if not posted_card:
            print("✗ V2 TEST: card image post FAILED")


run_nightly_job = main


def start_scheduler():
    scheduler = BackgroundScheduler(timezone="America/New_York")
    # Offset 10 minutes after the production job (6:00pm ET) so the two
    # services never hit Jarvis/Finnhub/yfinance at the exact same
    # moment, and so V2 TEST posts land clearly after the production
    # post if you're watching both channels side by side.
    scheduler.add_job(run_nightly_job, "cron", hour=18, minute=10, id="nightly_setups_v2_test", replace_existing=True, max_instances=1)
    scheduler.start()
    print("Scheduler started: V2 TEST nightly setups job fires daily at 6:10pm ET (should_publish_tonight() internally skips weekends/holidays).")

    def heartbeat():
        while True:
            time.sleep(900)
            print(f"[HEARTBEAT] V2 TEST scheduler running={scheduler.running}")

    threading.Thread(target=heartbeat, daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    if os.environ.get("FORCE_PUBLISH") == "1":
        run_nightly_job()
    else:
        start_scheduler()