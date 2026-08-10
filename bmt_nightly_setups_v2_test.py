"""
bmt_nightly_setups_v2_test.py — EXPERIMENTAL / TEST BUILD

Standalone sibling of bmt_nightly_setups.py, built to trial a richer,
subscriber-facing post format driven by real Discord embeds (colored
cards with structured fields) instead of the plain-text digest the
production script uses.

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

FORMAT HISTORY (locked 2026-08-10 after review via a standalone test
script, test_v2_embed_sample.py, run directly against the test
webhook before any of this was wired back into the pipeline):
- v1 asked one LLM call for a single long markdown document (ranking
  table, 5 full-paragraph setups, a "Card Copy" section). This posted
  as 8 sequential Discord messages and the markdown table rendered as
  broken literal pipe characters -- Discord's plain `content` field
  does not render tables at all.
- v2 compressed each setup to 2 lines to fix the length problem, but
  lost the reasoning ("why it made the list" / "why choose this over
  the others") that made the format useful in the first place.
- v3 (this version) fixes both at once: write_setup_narratives() asks
  for compact, plain-English JSON per setup instead of a markdown
  document, and that JSON drives real Discord `embeds` -- colored
  left-bar cards with a Role / Risk level / Best for / Why it made the
  list / Why choose this over the others field grid. Embed color is
  mapped to risk level consistently (green/blue/amber/red) so the
  color always carries the same meaning; the top pick gets a star
  instead of hijacking that color scale. The old markdown table is
  gone entirely -- Role and Risk level already live on each setup's
  own card, so a separate summary table was pure duplication. The
  blanket "Expiry / Direction: Calls" header line is gone too, since a
  given night isn't guaranteed to be all-calls or one expiry (each
  setup's own title carries its own expiry and C/P). The final
  contract list is still built deterministically in Python
  (build_contract_list_embed()), not written by the model, guaranteeing
  it's byte-for-byte accurate to what was actually selected.
- The card image (reusing the same renderer as production) stays
  watermarked "V2 TEST -- INTERNAL REVIEW" so it can never be mistaken
  for the live production card while this is being trialed. Sender
  username on every post is explicitly set to "BMT".
"""

import os
import sys
import json
import re
import time
import threading
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Force line-buffered stdout. When output is piped rather than
# attached to a real terminal (e.g. `railway run ... | ...`, or some
# IDE integrated terminals), Python silently switches from
# line-buffered to block-buffered stdout -- prints sit in a buffer and
# don't appear until it fills or the process exits, which looks
# exactly like the script hanging even though it's running normally.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

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
# LOCKED FORMAT (2026-08-10): rich, subscriber-ready content, built as
# real Discord embeds instead of a giant plain-text/markdown string.
#
# History: v1 of this generator asked the model for one long markdown
# document (table + 5 full-paragraph setups + a "Card Copy" section).
# That rendered as 8 sequential Discord messages and a broken markdown
# table (Discord's plain `content` field does not render tables at
# all -- it dumps the pipe characters literally). v2 compressed each
# setup to 2 lines, which lost the reasoning that made the format
# useful in the first place. This version -- confirmed and locked with
# the user via a standalone test script -- fixes both: real Discord
# `embeds` (colored left-bar cards with a field grid) replace the
# table entirely, each setup keeps Role / Risk level / Best for / Why
# it made the list / Why choose this over the others as distinct
# fields (trimmed to plain, short, actionable language), and a
# deterministic (not model-written) contract list closes it out.
# Also dropped: the blanket "Expiry / Direction: Calls" line, since
# not every night is all-calls or one expiry -- each setup's own
# title already carries its own expiry and C/P.
# ─────────────────────────────────────────────────────────────────────

SENDER_USERNAME = "BMT"

# Discord embed colors (decimal, not hex string), mapped to risk level
# consistently across every setup -- the color always means the same
# thing, with the legend implicit in the mapping itself (a reader sees
# green/blue/amber/red enough nights running to learn it fast).
COLOR_LOW = 0x3BA55D          # green
COLOR_MODERATE = 0x5865F2     # blue
COLOR_ELEVATED = 0xE5A012     # amber
COLOR_SPECULATIVE = 0xED4245  # red
COLOR_GOLD = 0xFBBF24         # Best Choice Tonight highlight
COLOR_NEUTRAL = 0x2B2D31      # header / contract list / unrecognized risk label

RISK_COLOR_MAP = {
    "Low": COLOR_LOW,
    "Moderate": COLOR_MODERATE,
    "Elevated": COLOR_ELEVATED,
    "High": COLOR_SPECULATIVE,
    "Speculative": COLOR_SPECULATIVE,
}

VALID_ROLES = [
    "Best Overall",
    "Lowest-Move Setup",
    "Best Balanced Setup",
    "Flow-Backed Momentum Setup",
    "Speculative / High-Risk Breakout Setup",
]
VALID_RISK_LEVELS = ["Low", "Moderate", "Elevated", "High", "Speculative"]

HOW_TO_USE_LINE = "These are five independent triggered setups, not a basket to buy blindly \u2014 pick what fits your risk tolerance."


def build_narrative_source_data(selected: list, market_context: dict, target_date: datetime) -> str:
    lines = []
    lines.append("Market summary and index performance (as of last close):")
    for t in MARKET_CONTEXT_TICKERS:
        m = market_context.get(t, {})
        lines.append(f"- ${t}: ${m.get('price', 'N/A')} ({m.get('pct', 'N/A')}%) -- {get_tone_phrase(m)}")
    lines.append("")
    lines.append(f"Ideas are for the next trading session: {target_date.strftime('%A, %B %d, %Y')}.")
    lines.append("")
    lines.append(f"Five scanned trade ideas, in descending conviction/ranking-score order (setup #1 scored highest by the deterministic flow-intensity/pricing model -- a useful input, but re-rank based on the full picture if the data supports it):")
    lines.append("")
    for i, c in enumerate(selected):
        tp = c.get("time_pressure") or build_time_pressure(c)
        lines.append(f"---- SETUP #{i+1} (model rank order, not necessarily final rank) ----")
        lines.append(f"Ticker: ${c['ticker']}   Company: {c.get('company_name', 'Not provided')}")
        lines.append(f"Contract: {c['direction']} ${c['strike']:g} strike, expiring {c['next_expiry']} ({tp['dte']} calendar days to expiration)")
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


NARRATIVE_PROMPT_TEMPLATE = """You are an expert options-trading newsletter editor. Convert tonight's scanned trade ideas into short, plain-English content for a Discord post that a complete beginner can read and act on in under a minute per setup. This is NOT a long-form document -- every field below is going into a compact, colored embed card, so brevity is a hard requirement, not a style preference.

## What to produce, per setup

1. **role** -- assign each of the five setups exactly one of these five roles, no duplicates: "Best Overall", "Lowest-Move Setup", "Best Balanced Setup", "Flow-Backed Momentum Setup", "Speculative / High-Risk Breakout Setup". If the data doesn't cleanly support one of these for every setup, still assign the closest truthful match -- every setup needs exactly one role and every role is used exactly once.
2. **risk** -- exactly one of: "Low", "Moderate", "Elevated", "High", "Speculative". Base this on required move size, how far out the option is, and how much conviction the data supports -- not on the role name.
3. **best_for** -- ONE short sentence: what kind of trader or objective this setup suits.
4. **why_made_list** -- ONE, at most TWO, SHORT plain-English sentences. This is the single most important field -- combine chart structure + options flow + option pricing/value + catalyst (if any) into one tight, beginner-friendly explanation. NO jargon: never write "IV/RV", "implied volatility", "realized volatility", "call-weighted", "OTM/ATM", "Higher Lows Base", "conviction rank", or similar. Translate instead -- e.g. "the options are priced cheap for how much this stock actually moves" instead of an IV/RV ratio; "big options traders have been buying calls" instead of "call-weighted flow"; "the stock has been climbing steadily" instead of "Higher Lows Base". Do not restate exact dollar flow figures or percentages already implied elsewhere -- keep this readable, not data-dense.
5. **why_choose** -- ONE short sentence: the single clearest reason to pick this over the other four tonight.

## Also produce

- **market_backdrop**: ONE plain-English sentence citing real $SPY/$QQQ/$IWM levels and moves by number.
- **top_pick_ticker**: the ticker of whichever setup should be the single best pick tonight (usually, but not required to be, the "Best Overall" role).
- **top_pick_why**: ONE short sentence on why that's the top pick.

## Non-negotiable rules

- Every ticker mention anywhere in your output must be prefixed with "$" (e.g. "$AXTI"), every time.
- Do NOT include any URLs, website names, or "according to [source]" citations anywhere -- write facts in plain prose without naming or linking sources.
- Base every claim only on the source data provided below -- you do not have web search for this task, so do not reference any external event, date, or catalyst you were not explicitly given.
- These five setups have ALREADY been screened for reasonable option pricing and a clean chart pattern -- do not write as if reconsidering whether they're worth trading.
- Never say "guaranteed", "easy money", "cannot lose", or similar.
- Do not invent catalysts, data, or reasoning not in the source data below. If something is missing, write "Not provided".

## Source data for tonight ({target_date_str})

{source_data}

## Output format

Return ONLY valid JSON, nothing else, no markdown code fences, in exactly this shape (ticker keys must match the source data tickers exactly):

{{
  "market_backdrop": "...",
  "top_pick_ticker": "TICKER",
  "top_pick_why": "...",
  "setups": {{
    "TICKER1": {{"role": "...", "risk": "...", "best_for": "...", "why_made_list": "...", "why_choose": "..."}},
    "TICKER2": {{"role": "...", "risk": "...", "best_for": "...", "why_made_list": "...", "why_choose": "..."}},
    "TICKER3": {{"role": "...", "risk": "...", "best_for": "...", "why_made_list": "...", "why_choose": "..."}},
    "TICKER4": {{"role": "...", "risk": "...", "best_for": "...", "why_made_list": "...", "why_choose": "..."}},
    "TICKER5": {{"role": "...", "risk": "...", "best_for": "...", "why_made_list": "...", "why_choose": "..."}}
  }}
}}"""


def write_setup_narratives(selected: list, market_context: dict, target_date: datetime) -> dict:
    source_data = build_narrative_source_data(selected, market_context, target_date)
    prompt = NARRATIVE_PROMPT_TEMPLATE.format(
        target_date_str=target_date.strftime('%A, %B %d, %Y'),
        source_data=source_data,
    )

    # NOTE (2026-08-10 fix): the web-search plugin was removed from
    # this call. This task only rephrases data ALREADY supplied in
    # source_data (flow, pricing, targets) into plain English -- it
    # doesn't need to research new facts, and the web plugin could
    # trigger several search round-trips before returning, which was
    # making this step look "stuck" for minutes at a time. Timeout
    # dropped accordingly now that there's no open-ended search step.
    print("  [NARRATIVE] calling OpenRouter (no web search, should be well under a minute)...", flush=True)
    call_started = time.time()
    resp = requests.post(
        f"{OPENROUTER_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json={"model": "moonshotai/kimi-k2.6", "max_tokens": 8000, "temperature": 0,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90
    )
    print(f"  [NARRATIVE] response received after {time.time() - call_started:.1f}s", flush=True)
    raw = resp.json()
    if "choices" not in raw:
        print("  [NARRATIVE ERROR] unexpected response (no 'choices' key):")
        print(f"  {json.dumps(raw, indent=2)[:1000]}")
        raise ValueError("write_setup_narratives: unexpected API response shape")
    message = raw["choices"][0]["message"]
    content = message.get("content")
    if not content:
        print("  [NARRATIVE ERROR] empty/None content -- likely an incomplete tool call:")
        print(f"  {json.dumps(message, indent=2)[:1000]}")
        raise ValueError("write_setup_narratives: empty content in API response")
    content = content.strip()
    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"```\s*$", "", content)
    return json.loads(content.strip())


def clean_text_field(text: str, tickers: list) -> str:
    if not text:
        return "Not provided"
    text = strip_urls_and_domains(text)
    text = ensure_dollar_prefixed_tickers(text, tickers)
    return text.strip()


def build_header_embed(market_backdrop: str, target_date: datetime) -> dict:
    return {
        "title": f"TRADE IDEAS \u2014 {target_date.strftime('%A, %B %d').upper()}",
        "description": f"{market_backdrop}\n\n{HOW_TO_USE_LINE}",
        "color": COLOR_NEUTRAL,
    }


def build_best_choice_embed(top_pick_ticker: str, top_pick_why: str) -> dict:
    return {
        "title": "Best Choice Tonight",
        "color": COLOR_GOLD,
        "description": f"**${top_pick_ticker}**\n{top_pick_why}",
    }


def build_setup_embed(c: dict, rank: int, is_top_pick: bool) -> dict:
    risk = c.get("risk", "Moderate")
    color = RISK_COLOR_MAP.get(risk, COLOR_NEUTRAL)
    star = "\u2b50 " if is_top_pick else ""
    return {
        "title": f"{star}{rank}. ${c['ticker']} \u2014 {c['next_expiry']} ${c['strike']:g}{c['direction'][0]}",
        "color": color,
        "fields": [
            {"name": "Role", "value": c.get("role", "Not provided"), "inline": True},
            {"name": "Risk level", "value": risk, "inline": True},
            {"name": "Best for", "value": c.get("best_for", "Not provided"), "inline": False},
            {"name": "Why it made the list", "value": c.get("why_made_list", "Not provided"), "inline": False},
            {"name": "Why choose this over the others", "value": c.get("why_choose", "Not provided"), "inline": False},
        ],
    }


def build_contract_list_embed(selected: list) -> dict:
    """
    Deterministic, code-generated contract list -- not written by the
    model, so it is guaranteed byte-for-byte accurate to what was
    actually selected. Same pattern as contract_lines in the
    production script's format_discord_digest().
    """
    lines = "\n".join(f"\u2022 ${c['ticker']} {c['next_expiry']} ${c['strike']:g}{c['direction'][0]}" for c in selected)
    return {
        "title": "Tonight's Contracts",
        "color": COLOR_NEUTRAL,
        "description": lines,
        "footer": {"text": "Not financial advice. See card image for entry/stop/target levels."},
    }


def post_embeds_to_discord(embeds: list) -> bool:
    """
    Discord allows up to 10 embeds and ~6000 combined characters per
    message. This payload is header + best-choice + up to 5 setups +
    contract list = 8 embeds, comfortably under both limits, so it
    goes out as a single POST.
    """
    payload = {"username": SENDER_USERNAME, "embeds": embeds}
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=30)
        print(f"  [DISCORD] embeds post: {r.status_code} ({len(embeds)} embeds)")
        if r.status_code not in (200, 204):
            print(f"    body: {r.text[:500]}")
        return r.status_code in (200, 204)
    except Exception as e:
        print(f"  [DISCORD] embeds post FAILED: {e}")
        return False


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
        # Guard against iv_rv_ratio being exactly 0.0 (not None) --
        # this happens when atm_iv rounds to a very small number
        # relative to realized_vol and round(..., 2) lands on 0.0.
        # "is not None" alone lets 0.0 through, and 1.0 / 0.0 crashes.
        if c["iv_rv_ratio"]:
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

    print(f"\nGenerating narrative content for {len(selected)} setup(s) (locked embed format)...")
    narrative_result = write_setup_narratives(selected, market_context, target_date)

    all_tickers = [c["ticker"] for c in selected] + list(MARKET_CONTEXT_TICKERS)
    market_backdrop = clean_text_field(narrative_result.get("market_backdrop", ""), all_tickers)
    top_pick_ticker = narrative_result.get("top_pick_ticker", "").upper().lstrip("$")
    top_pick_why = clean_text_field(narrative_result.get("top_pick_why", ""), all_tickers)
    setups_by_ticker = narrative_result.get("setups", {})

    # Merge model output onto each selected setup, validating role/risk
    # against the fixed vocab -- fall back to a safe neutral value
    # rather than letting an unexpected label silently break the color
    # mapping or leave a field blank.
    used_roles = set()
    for c in selected:
        s = setups_by_ticker.get(c["ticker"], {})
        role = s.get("role", "")
        if role not in VALID_ROLES or role in used_roles:
            role = next((r for r in VALID_ROLES if r not in used_roles), "Best Balanced Setup")
        used_roles.add(role)
        risk = s.get("risk", "")
        if risk not in VALID_RISK_LEVELS:
            risk = "Moderate"
        c["role"] = role
        c["risk"] = risk
        c["best_for"] = clean_text_field(s.get("best_for", ""), all_tickers)
        c["why_made_list"] = clean_text_field(s.get("why_made_list", ""), all_tickers)
        c["why_choose"] = clean_text_field(s.get("why_choose", ""), all_tickers)
        print(f"  {c['ticker']}: {c['role']} | {c['risk']}")

    if top_pick_ticker not in {c["ticker"] for c in selected}:
        top_pick_ticker = selected[0]["ticker"]
        top_pick_why = top_pick_why if top_pick_why != "Not provided" else "Top-ranked setup tonight by the model."

    embeds = [
        build_header_embed(market_backdrop, target_date),
        build_best_choice_embed(top_pick_ticker, top_pick_why),
    ]
    for i, c in enumerate(selected):
        embeds.append(build_setup_embed(c, rank=i + 1, is_top_pick=(c["ticker"] == top_pick_ticker)))
    embeds.append(build_contract_list_embed(selected))

    print(f"\n=== EMBEDS PREVIEW ===\n{json.dumps(embeds, indent=2)}\n")

    # Simple market_theme/risk_notes for the card image -- kept short,
    # separate from the embeds above.
    spy = market_context.get("SPY", {})
    qqq = market_context.get("QQQ", {})
    market_theme = (f"$SPY closed at ${spy.get('price', 'N/A')} ({spy.get('pct', 'N/A')}%) and "
                     f"$QQQ at ${qqq.get('price', 'N/A')} ({qqq.get('pct', 'N/A')}%).")
    risk_notes = "See the write-up below for the reasoning, and this image for exact entry/stop/target levels."

    out_path = "bmt_nightly_setups_v2_test.png"
    render_card(selected, market_theme, risk_notes, market_context, target_date, data_date, out_path)
    print(f"\nCard saved to {out_path}")

    posted_card = post_image_to_discord(out_path, message="🧪 **V2 TEST BUILD** — internal review only, not the live production post")
    posted_embeds = post_embeds_to_discord(embeds)

    if posted_embeds and posted_card:
        print("✓ V2 TEST: card + embeds posted to Discord!")
    else:
        if not posted_embeds:
            print("✗ V2 TEST: embeds post FAILED")
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