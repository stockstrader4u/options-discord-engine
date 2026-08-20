"""
bmt_nightly_setups.py — Nightly top-5 trade-ideas digest (PRODUCTION).

PROMOTED TO PRODUCTION (2026-08-11): this file was developed and
trialed as bmt_nightly_setups_v2_test.py, a richer, subscriber-facing
post format driven by real Discord embeds (colored cards with
structured fields) instead of the older plain-text digest format. It
has now been promoted to be the live production script running on the
bmt-trade-ideas Railway service.

Changes made specifically for this promotion, vs. the v2 test file:
  1. DISCORD_WEBHOOK now reads NIGHTLY_SETUPS_DISCORD_WEBHOOK (the
     variable actually configured on the bmt-trade-ideas service),
     not NIGHTLY_SETUPS_V2_TEST_DISCORD_WEBHOOK.
  2. The "V2 TEST -- INTERNAL REVIEW" watermark on the card image and
     the "internal review only, not the live production post" message
     prefix on the image post have both been removed -- this output
     now goes straight to subscribers.
  3. Scheduler moved from 6:10pm ET (offset to avoid colliding with
     the old separate prod script) back to 6:00pm ET, matching what
     the previous plain-text production script ran at, since this is
     now the only nightly setups job.
  4. File renamed to bmt_nightly_setups.py to match the Railway
     service's configured Custom Start Command
     ("python bmt_nightly_setups.py").

Everything else -- the full data pipeline, the embed format, and the
two bugfixes below -- is unchanged from what was tested as v2.

WHAT'S REUSED, UNCHANGED, FROM the original plain-text
bmt_nightly_setups.py:
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
- During the trial period, the card image carried a "V2 TEST --
  INTERNAL REVIEW" watermark so it could never be mistaken for the
  live production card. That watermark has been removed now that this
  format IS production (see the PROMOTED TO PRODUCTION note above).
  Sender username on every post is explicitly set to "BMT".

BUGFIX (2026-08-11): every setup posted with "Not provided" for
best_for/why_made_list/why_choose, risk defaulted to "Moderate", and
roles assigned in flat VALID_ROLES order for every ticker -- the
telltale signature of setups_by_ticker.get(c["ticker"], {}) missing
on every single lookup. market_backdrop and top_pick_why (top-level
JSON fields) came through fine, so the response was valid JSON and
the API call succeeded -- only the nested "setups" object's keys
failed to match. Root cause: the prompt's blanket instruction to
prefix every ticker mention with "$" was being over-applied by the
model to the JSON object keys themselves (returning "$AXTI" instead
of "AXTI" as the key), while c["ticker"] is always the bare symbol,
so every dict lookup silently missed and fell through to the {}
default -- no exception, no visible error, just silent full-fallback
for every field on every setup.

Fix, two parts:
  1. Prompt now explicitly carves out an exception for JSON keys /
     top_pick_ticker: those must be the bare ticker, no "$" prefix.
     The "$" prefix rule is scoped to ticker mentions inside prose.
  2. Defensive normalization on the Python side regardless -- keys
     under "setups" are stripped of any leading "$" and uppercased
     before lookup, so a future run where the model ignores the
     prompt instruction degrades gracefully instead of silently
     falling back on every field again.

RESULTS TRACKING (2026-08-16): every setup this script publishes is
now also persisted to a Postgres table (nightly_setup_ideas) right
after it's finalized, so a separate script (bmt_setup_results_tracker.py)
can look it up on its expiry date and report whether the strike was
ever touched between entry and expiry. See save_setup_ideas() and
ensure_schema() below, and bmt_setup_results_tracker.py for the
read/grade/report side. This script itself does not grade or report
results -- it only writes the record.
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
from urllib.parse import urlparse

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
import pg8000.native as _pg8000

JARVIS_API_KEY     = os.environ["JARVIS_API_KEY"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
FINNHUB_API_KEY    = os.environ["FINNHUB_API_KEY"]
DISCORD_WEBHOOK    = os.environ["NIGHTLY_SETUPS_DISCORD_WEBHOOK"]
# DATABASE_URL is optional at the app level (matches the pattern used
# in bmt_trade_journal.py) so a missing var degrades to "results
# tracking is skipped" rather than crashing the whole nightly post.
DATABASE_URL        = os.environ.get("DATABASE_URL", "")
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
# DB -- results-tracking persistence layer (2026-08-16)
# ─────────────────────────────────────────────────────────────────────

def _connect():
    parsed = urlparse(DATABASE_URL)
    return _pg8000.Connection(
        host=parsed.hostname, port=parsed.port or 5432,
        database=parsed.path.lstrip("/"),
        user=parsed.username, password=parsed.password,
    )


def ensure_schema():
    """Creates nightly_setup_ideas if it doesn't already exist. Safe to
    call on every run -- CREATE TABLE IF NOT EXISTS is a no-op once the
    table is there. status starts 'pending' and is only ever updated by
    bmt_setup_results_tracker.py once the setup's expiry date arrives."""
    if not DATABASE_URL:
        print("  [DB WARN] DATABASE_URL not set -- results tracking will be skipped for this run.")
        return
    conn = _connect()
    try:
        conn.run("""
            CREATE TABLE IF NOT EXISTS nightly_setup_ideas (
                id SERIAL PRIMARY KEY,
                ticker TEXT NOT NULL,
                direction TEXT NOT NULL,
                strike NUMERIC NOT NULL,
                entry_low NUMERIC NOT NULL,
                entry_high NUMERIC NOT NULL,
                stop NUMERIC NOT NULL,
                target1 NUMERIC NOT NULL,
                target2 NUMERIC NOT NULL,
                expiry_label TEXT,
                expiry_date DATE NOT NULL,
                publish_date DATE NOT NULL,
                role TEXT,
                risk TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                entry_date DATE,
                period_high NUMERIC,
                period_low NUMERIC,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                resolved_at TIMESTAMPTZ
            )
        """)
    except Exception as e:
        print(f"  [DB WARN] ensure_schema failed: {e}")
    finally:
        conn.close()


def save_setup_ideas(selected: list, target_date: datetime):
    """Persists tonight's setups so bmt_setup_results_tracker.py can grade
    them once their expiry date arrives. This is intentionally a
    fire-and-forget best-effort write -- a DB hiccup here should never
    stop tonight's Discord post, so failures are logged and swallowed,
    not raised."""
    if not DATABASE_URL:
        print("  [DB WARN] DATABASE_URL not set -- setup ideas will NOT be tracked for results.")
        return
    conn = _connect()
    saved = 0
    try:
        for c in selected:
            expiry_iso = c.get("expiry_iso")
            if not expiry_iso:
                print(f"  [DB WARN] {c['ticker']}: no expiry_iso -- skipping results-tracking insert")
                continue
            try:
                expiry_date = datetime.strptime(expiry_iso, "%Y-%m-%d").date()
            except Exception:
                print(f"  [DB WARN] {c['ticker']}: unparseable expiry_iso {expiry_iso!r} -- skipping")
                continue
            conn.run("""
                INSERT INTO nightly_setup_ideas
                    (ticker, direction, strike, entry_low, entry_high, stop, target1, target2,
                     expiry_label, expiry_date, publish_date, role, risk)
                VALUES
                    (:ticker, :direction, :strike, :entry_low, :entry_high, :stop, :target1, :target2,
                     :expiry_label, :expiry_date, :publish_date, :role, :risk)
            """,
                ticker=c["ticker"], direction=c["direction"], strike=c["strike"],
                entry_low=c["entry_low"], entry_high=c["entry_high"], stop=c["stop"],
                target1=c["target1"], target2=c["target2"],
                expiry_label=c.get("next_expiry"), expiry_date=expiry_date,
                publish_date=target_date.date(),
                role=c.get("role"), risk=c.get("risk"),
            )
            saved += 1
        print(f"  [DB] Saved {saved}/{len(selected)} setup idea(s) for results tracking.")
    except Exception as e:
        print(f"  [DB WARN] save_setup_ideas failed partway ({saved} saved before the error): {e}")
    finally:
        conn.close()


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

- Every ticker mention INSIDE SENTENCE TEXT (market_backdrop, top_pick_why, best_for, why_made_list, why_choose) must be prefixed with "$" (e.g. "$AXTI"), every time.
- EXCEPTION -- do NOT apply the "$" prefix to the JSON object keys under "setups", or to the value of "top_pick_ticker". Those must be the bare ticker symbol with no "$" and no other punctuation (e.g. the key "AXTI", not "$AXTI"; top_pick_ticker: "AXTI", not "$AXTI"). The "$" prefix rule applies only to prose sentences, never to JSON keys or to top_pick_ticker's value.
- Do NOT include any URLs, website names, or "according to [source]" citations anywhere -- write facts in plain prose without naming or linking sources.
- Base every claim only on the source data provided below -- you do not have web search for this task, so do not reference any external event, date, or catalyst you were not explicitly given.
- These five setups have ALREADY been screened for reasonable option pricing and a clean chart pattern -- do not write as if reconsidering whether they're worth trading.
- Never say "guaranteed", "easy money", "cannot lose", or similar.
- Do not invent catalysts, data, or reasoning not in the source data below. If something is missing, write "Not provided".

## Source data for tonight ({target_date_str})

{source_data}

## Output format

Return ONLY valid JSON, nothing else, no markdown code fences, in exactly this shape (ticker keys must match the source data tickers exactly, bare with no "$" prefix -- see the non-negotiable rules above):

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
    # making this step look "stuck" for minutes at a time.
    #
    # REASONING-BUDGET BUGFIX (2026-08-11): confirmed in a real
    # production run that this call failed with "empty content in API
    # response" -- the logged message showed content: null alongside a
    # non-empty `reasoning` field that was cut off MID-SENTENCE ("...I
    # should avoid exact dollar amounts or percentages in why_m"). Kimi
    # K2.6 is a reasoning model, and OpenRouter counts reasoning tokens
    # against the same max_tokens budget as the actual answer by
    # default -- it spent the ENTIRE 8000-token budget working through
    # the rules in the prompt and ran out of room before writing a
    # single character of the actual JSON answer. This wasn't a fluke
    # of that one run's input; it's a structural risk any time the
    # model's reasoning happens to run long, since nothing was capping
    # it.
    #
    # FIX, two parts:
    #   1. `reasoning.max_tokens` explicitly caps how many tokens the
    #      model can spend thinking before it must move on to the
    #      actual answer -- this task is a straightforward "rephrase
    #      already-supplied data into plain JSON" job, not one that
    #      needs deep reasoning, so a modest cap is appropriate.
    #   2. Overall max_tokens raised well above the reasoning cap, so
    #      even a run that uses its full reasoning allowance still has
    #      generous room left over for the 5-setup JSON response.
    #   3. If content still comes back empty (belt-and-suspenders), one
    #      automatic retry with an even tighter reasoning cap runs
    #      before giving up -- a single bad reasoning-length roll no
    #      longer crashes the entire nightly run.
    REASONING_MAX_TOKENS_ATTEMPTS = [3000, 1200]
    TOTAL_MAX_TOKENS = 16000

    raw = None
    message = None
    content = None
    parsed_result = None
    last_parse_error = None

    for attempt, reasoning_cap in enumerate(REASONING_MAX_TOKENS_ATTEMPTS, start=1):
        print(f"  [NARRATIVE] calling OpenRouter, attempt {attempt}/{len(REASONING_MAX_TOKENS_ATTEMPTS)} "
              f"(reasoning capped at {reasoning_cap} tokens, {TOTAL_MAX_TOKENS} total budget)...", flush=True)
        call_started = time.time()
        # NETWORK-HANG BUGFIX (2026-08-11): confirmed via direct curl
        # test that OpenRouter itself responds instantly -- the hang
        # was specifically inside `railway run`'s local proxy/tunnel
        # layer, which can stall a connection in a way that a single
        # float `timeout=` doesn't reliably bound (it can behave
        # inconsistently if the stall happens at connect/handshake
        # time rather than mid-read). Splitting into an explicit
        # (connect_timeout, read_timeout) tuple guarantees a hard
        # failure within connect_timeout seconds if the connection
        # can't even be established, regardless of what's stalling
        # underneath requests -- this protects both local `railway
        # run` testing and the actual deployed Railway service from
        # hanging forever on a bad network layer.
        try:
            resp = requests.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                json={"model": "moonshotai/kimi-k2.6", "max_tokens": TOTAL_MAX_TOKENS, "temperature": 0,
                      "reasoning": {"max_tokens": reasoning_cap},
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=(10, 120)
            )
        except requests.exceptions.RequestException as e:
            # A connect/read timeout (or any other network failure) is
            # now caught explicitly instead of crashing the run with
            # an uncaught traceback -- treated the same as an
            # empty-content response: log it and fall through to the
            # retry-with-tighter-cap attempt (or raise cleanly if this
            # was the last attempt).
            print(f"  [NARRATIVE WARN] attempt {attempt}: network error after "
                  f"{time.time() - call_started:.1f}s: {type(e).__name__}: {e}")
            if attempt < len(REASONING_MAX_TOKENS_ATTEMPTS):
                print("  [NARRATIVE] retrying with a tighter reasoning cap...", flush=True)
                continue
            raise ValueError(f"write_setup_narratives: network error on final attempt: {e}") from e
        print(f"  [NARRATIVE] response received after {time.time() - call_started:.1f}s", flush=True)
        raw = resp.json()
        if "choices" not in raw:
            print("  [NARRATIVE ERROR] unexpected response (no 'choices' key):")
            print(f"  {json.dumps(raw, indent=2)[:1000]}")
            raise ValueError("write_setup_narratives: unexpected API response shape")
        message = raw["choices"][0]["message"]
        content = message.get("content")
        if not content:
            print(f"  [NARRATIVE WARN] attempt {attempt}: empty/None content -- likely the model used its "
                  f"whole reasoning budget before writing an answer:")
            print(f"  {json.dumps(message, indent=2)[:1000]}")
            if attempt < len(REASONING_MAX_TOKENS_ATTEMPTS):
                print("  [NARRATIVE] retrying with a tighter reasoning cap...", flush=True)
            continue

        # TRUNCATED-JSON BUGFIX (2026-08-20): confirmed in production
        # that this exact call can return NON-EMPTY content that is
        # still cut off mid-JSON-string (real incident: "Unterminated
        # string starting at: line 6 column 152 (char 598)" -- only 598
        # characters in, nowhere near a complete 5-setup JSON object).
        # The retry loop above only ever checked for EMPTY content
        # (`if content: break`), so truncated-but-non-empty content
        # counted as success, exited the loop, and json.loads() below
        # ran completely unprotected -- a parse failure propagated as
        # an uncaught JSONDecodeError all the way up through main(),
        # killing the entire scheduled run with ZERO Discord post that
        # night, not even a partial one. This is the same underlying
        # failure mode as the empty-content bug already fixed on
        # 2026-08-11 (the model spending too much of its token budget
        # before finishing) -- it just manifests as truncated-but-
        # present text instead of no text at all, and needs the exact
        # same fix: treat it as retry-worthy, not fatal.
        #
        # FIX: json.loads() now runs INSIDE this retry loop, wrapped in
        # try/except. A parse failure on a non-final attempt logs a
        # warning and retries with the next (tighter) reasoning cap,
        # exactly like empty content already did. Only after every
        # attempt has produced unparseable content does this raise --
        # and when it does, the raised error now includes the actual
        # parse error and a snippet of the bad content, not just "empty
        # content", since content was NOT empty this time.
        cleaned = content.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned)
        try:
            parsed_result = json.loads(cleaned.strip())
            break
        except json.JSONDecodeError as e:
            last_parse_error = e
            print(f"  [NARRATIVE WARN] attempt {attempt}: content was non-empty but failed to parse as "
                  f"JSON ({e}) -- likely truncated mid-response. First 300 chars: {cleaned[:300]!r}")
            if attempt < len(REASONING_MAX_TOKENS_ATTEMPTS):
                print("  [NARRATIVE] retrying with a tighter reasoning cap...", flush=True)
            continue

    if parsed_result is None:
        if last_parse_error is not None:
            raise ValueError(
                f"write_setup_narratives: content was non-empty on every attempt but never parsed as "
                f"valid JSON after all retries -- last error: {last_parse_error}. "
                f"Last content (first 500 chars): {(content or '')[:500]!r}"
            )
        raise ValueError("write_setup_narratives: empty content in API response after all retry attempts")

    return parsed_result


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
# PER-SETUP CHART RENDERING (merged into production 2026-08-12)
# Pattern trendline uses the same swing geometry as is_clean_uptrend()/
# is_clean_downtrend() above. Trade levels are drawn as plain unlabeled
# lines -- the numbers live in the "TRADE PLAN" sidebar card instead.
# Fib is anchored to trade direction and filtered to the visible
# y-range. Volume/RSI are dedicated subplots with live value readouts.
# CONFIRMATION checklist is computed from real data, not decorative.
# ─────────────────────────────────────────────────────────────────────

CHART_BG = "#0d1117"
CHART_SURFACE = "#161b22"
CHART_GRID = "#21262d"
CHART_TEXT_PRIMARY = "#f5f5f7"
CHART_TEXT_SECONDARY = "#9198a1"
CHART_BORDER = "#30363d"
CANDLE_UP = "#22d3ee"
CANDLE_DOWN = "#f43f5e"
EMA_FAST_COLOR = "#2dd4bf"
EMA_SLOW_COLOR = "#fb923c"
VOL_UP_COLOR = "#3b82f6"
VOL_DOWN_COLOR = "#ef4444"
CHART_GREEN = "#34d399"
CHART_RED = "#f87171"
CHART_GOLD = "#fbbf24"
CHART_BLUE = "#60a5fa"

FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.236, 1.382, 1.618]
SETUP_TYPE_LABELS = {
    ("CALL", "higher lows"): "BULLISH CONTINUATION",
    ("CALL", "V-recovery"): "BULLISH REVERSAL",
    ("PUT", "lower highs"): "BEARISH CONTINUATION",
    ("PUT", "breakdown"): "BEARISH REVERSAL",
}


def get_extended_chart_bars(ticker: str, sessions: int = 90) -> list:
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="6mo")
        if hist.empty:
            return []
        hist = hist.tail(sessions)
        return [{"date": date, "open": row["Open"], "high": row["High"], "low": row["Low"],
                  "close": row["Close"], "volume": row.get("Volume", 0) or 0}
                for date, row in hist.iterrows()]
    except Exception as e:
        print(f"  [CHART BARS WARN] {ticker}: {e}")
        return []


def compute_ema_series(closes: list, period: int) -> list:
    if not closes:
        return []
    k = 2 / (period + 1)
    ema = [closes[0]]
    for price in closes[1:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def compute_rsi_series(closes: list, period: int = 14) -> list:
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    rsi = [None] * len(closes)
    for i in range(period, len(closes)):
        window = deltas[i - period:i]
        gains = [d for d in window if d > 0]
        losses = [-d for d in window if d < 0]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100 - (100 / (1 + rs))
    first_valid = next((v for v in rsi if v is not None), 50.0)
    return [v if v is not None else first_valid for v in rsi]


def compute_chart_fib_levels(bars: list, direction: str) -> list:
    if not bars:
        return []
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    hi_val, lo_val = max(highs), min(lows)
    span = hi_val - lo_val
    if span <= 0:
        return []
    is_call = direction.upper() == "CALL"
    return [(f, (lo_val + f * span) if is_call else (hi_val - f * span)) for f in FIB_LEVELS]


def get_pattern_trendline(c: dict, chart_len: int):
    bars = c["bars"]
    window = bars[-10:] if len(bars) >= 10 else bars
    offset = chart_len - len(window)
    pattern = c.get("pattern")

    if pattern == "higher lows":
        lows = [b["low"] for b in window]
        swing_highs, swing_lows = find_swing_points(window)
        if len(swing_lows) >= 2:
            pts = [(offset + i, v) for i, v in swing_lows]
        else:
            pts = [(offset, lows[0]), (offset + len(window) - 1, lows[-1])]
        return pts, CHART_GREEN

    if pattern == "lower highs":
        highs = [b["high"] for b in window]
        swing_highs, swing_lows = find_swing_points(window)
        if len(swing_highs) >= 2:
            pts = [(offset + i, v) for i, v in swing_highs]
        else:
            pts = [(offset, highs[0]), (offset + len(window) - 1, highs[-1])]
        return pts, CHART_RED

    if pattern == "V-recovery":
        min_idx = min(range(len(window)), key=lambda i: window[i]["low"])
        pts = [(offset + min_idx, window[min_idx]["low"]), (offset + len(window) - 1, window[-1]["close"])]
        return pts, CHART_GREEN

    if pattern == "breakdown":
        max_idx = max(range(len(window)), key=lambda i: window[i]["high"])
        pts = [(offset + max_idx, window[max_idx]["high"]), (offset + len(window) - 1, window[-1]["close"])]
        return pts, CHART_RED

    return [], CHART_TEXT_SECONDARY


def compute_rvol(chart_bars: list, lookback: int = 20) -> float:
    if len(chart_bars) < 2:
        return 1.0
    prior = chart_bars[-(lookback + 1):-1] or chart_bars[:-1]
    avg = sum(b["volume"] for b in prior) / len(prior) if prior else chart_bars[-1]["volume"]
    return (chart_bars[-1]["volume"] / avg) if avg > 0 else 1.0


def compute_reward_risk(c: dict) -> tuple:
    entry_mid = (c["entry_low"] + c["entry_high"]) / 2
    is_call = c["direction"].upper() == "CALL"
    risk = (entry_mid - c["stop"]) if is_call else (c["stop"] - entry_mid)
    if risk <= 0:
        return None, None
    reward1 = (c["target1"] - entry_mid) if is_call else (entry_mid - c["target1"])
    reward2 = (c["target2"] - entry_mid) if is_call else (entry_mid - c["target2"])
    return round(reward1 / risk, 1), round(reward2 / risk, 1)


def build_setup_type_label(direction: str, pattern: str) -> str:
    label = SETUP_TYPE_LABELS.get((direction.upper(), pattern))
    if label:
        return label
    return "BULLISH SETUP" if direction.upper() == "CALL" else "BEARISH SETUP"


def render_setup_chart(c: dict, out_path: str):
    chart_bars = c.get("chart_bars") or c["bars"]
    n = len(chart_bars)
    is_call = c["direction"].upper() == "CALL"
    closes = [b["close"] for b in chart_bars]
    xs = list(range(n))

    fig = plt.figure(figsize=(15, 8.5), dpi=170, facecolor=CHART_BG)
    outer = fig.add_gridspec(1, 2, width_ratios=[3.3, 1], wspace=0.03,
                              left=0.045, right=0.98, top=0.85, bottom=0.07)
    left_gs = outer[0, 0].subgridspec(3, 1, height_ratios=[3.2, 0.85, 1.0], hspace=0.10)
    ax = fig.add_subplot(left_gs[0])
    vol_ax = fig.add_subplot(left_gs[1], sharex=ax)
    rsi_ax = fig.add_subplot(left_gs[2], sharex=ax)
    side_ax = fig.add_subplot(outer[0, 1])
    side_ax.axis("off")

    for a in (ax, vol_ax, rsi_ax):
        a.set_facecolor(CHART_BG)
        for spine in a.spines.values():
            spine.set_color(CHART_BORDER)
            spine.set_linewidth(0.6)
        a.tick_params(colors=CHART_TEXT_SECONDARY, labelsize=8, length=0)
        a.grid(color=CHART_GRID, linewidth=0.4, alpha=0.5)

    right_edge = n + 3.0

    ema5 = compute_ema_series(closes, 5)
    ema12 = compute_ema_series(closes, 12)
    ax.plot(xs, ema5, color=EMA_FAST_COLOR, linewidth=1.3, zorder=3)
    ax.plot(xs, ema12, color=EMA_SLOW_COLOR, linewidth=1.3, zorder=3)
    ax.plot([0.012, 0.032], [0.965, 0.965], transform=ax.transAxes, color=EMA_FAST_COLOR, linewidth=2.5, solid_capstyle="round")
    ax.text(0.038, 0.965, f"EMA 5   {ema5[-1]:,.2f}", transform=ax.transAxes, color=CHART_TEXT_PRIMARY,
            fontsize=8.5, va="center", ha="left")
    ax.plot([0.012, 0.032], [0.915, 0.915], transform=ax.transAxes, color=EMA_SLOW_COLOR, linewidth=2.5, solid_capstyle="round")
    ax.text(0.038, 0.915, f"EMA 12  {ema12[-1]:,.2f}", transform=ax.transAxes, color=CHART_TEXT_PRIMARY,
            fontsize=8.5, va="center", ha="left")

    for i, b in enumerate(chart_bars):
        color = CANDLE_UP if b["close"] >= b["open"] else CANDLE_DOWN
        ax.plot([i, i], [b["low"], b["high"]], color=color, linewidth=1, zorder=4)
        body_low, body_high = sorted([b["open"], b["close"]])
        ax.add_patch(plt.Rectangle((i - 0.3, body_low), 0.6, max(body_high - body_low, 0.01),
                                    facecolor=color, edgecolor=color, zorder=5))

    pts, trend_color = get_pattern_trendline(c, n)
    if len(pts) >= 2:
        xs_t = [p[0] for p in pts]
        ys_t = [p[1] for p in pts]
        ax.plot(xs_t, ys_t, color=trend_color, linewidth=1.8, marker="o", markersize=4, zorder=7)

    ax.axhspan(c["entry_low"], c["entry_high"], color=CHART_BLUE, alpha=0.18, zorder=1)
    ax.axhline(c["stop"], color=CHART_RED, linestyle="--", linewidth=1.3, zorder=3)
    ax.axhline(c["target1"], color=CHART_GREEN, linestyle="--", linewidth=1.3, zorder=3)
    ax.axhline(c["target2"], color=CHART_GREEN, linestyle=":", linewidth=1.3, zorder=3)

    all_vals = [b["low"] for b in chart_bars] + [b["high"] for b in chart_bars] + [c["stop"], c["target1"], c["target2"]]
    pad = (max(all_vals) - min(all_vals)) * 0.06
    y_min, y_max = min(all_vals) - pad, max(all_vals) + pad
    ax.set_ylim(y_min, y_max)
    ax.set_xlim(-1, right_edge)
    ax.tick_params(labelbottom=False)

    fib_levels = compute_chart_fib_levels(chart_bars, c["direction"])
    visible_fib = [(f, price) for f, price in fib_levels if y_min <= price <= y_max]
    for f, price in visible_fib:
        ax.plot([0, n - 1], [price, price], color=CHART_TEXT_SECONDARY, linewidth=0.6, alpha=0.4, zorder=3, clip_on=True)
        ax.text(n + 0.3, price, f"{f:.3f}", color=CHART_TEXT_SECONDARY, fontsize=7,
                va="center", zorder=6, alpha=0.85, clip_on=True)

    vols = [b["volume"] for b in chart_bars]
    for i, b in enumerate(chart_bars):
        color = VOL_UP_COLOR if b["close"] >= b["open"] else VOL_DOWN_COLOR
        vol_ax.bar(i, b["volume"], color=color, width=0.7, alpha=0.85, zorder=3)
    latest_vol = vols[-1] if vols else 0
    vol_str = f"{latest_vol / 1_000_000:.2f}M" if latest_vol >= 1_000_000 else f"{latest_vol / 1_000:.0f}K"
    vol_ax.text(0.01, 0.88, "Volume  ", transform=vol_ax.transAxes, color=CHART_TEXT_SECONDARY, fontsize=8.5, va="top")
    vol_ax.text(0.01 + 0.058, 0.88, vol_str, transform=vol_ax.transAxes, color=VOL_UP_COLOR, fontsize=8.5,
                fontweight="bold", va="top")
    vol_ax.set_xlim(-1, right_edge)
    vol_ax.tick_params(labelbottom=False)

    rsi = compute_rsi_series(closes, 14)
    rsi_ax.plot(xs, rsi, color="#c084fc", linewidth=1.1, zorder=3)
    rsi_ax.axhspan(30, 70, color="#c084fc", alpha=0.06, zorder=1)
    rsi_ax.axhline(70, color=CHART_TEXT_SECONDARY, linewidth=0.5, linestyle="--", alpha=0.5, zorder=2)
    rsi_ax.axhline(30, color=CHART_TEXT_SECONDARY, linewidth=0.5, linestyle="--", alpha=0.5, zorder=2)
    rsi_ax.text(0.01, 0.90, "RSI (14)  ", transform=rsi_ax.transAxes, color=CHART_TEXT_SECONDARY, fontsize=8.5, va="top")
    rsi_ax.text(0.01 + 0.11, 0.90, f"{rsi[-1]:.2f}", transform=rsi_ax.transAxes, color="#c084fc",
                fontsize=8.5, fontweight="bold", va="top")
    rsi_ax.set_ylim(0, 100)
    rsi_ax.set_xlim(-1, right_edge)

    date_labels = [b["date"].strftime("%b %d") for b in chart_bars]
    step = max(1, n // 8)
    tick_idx = list(range(0, n, step))
    rsi_ax.set_xticks(tick_idx)
    rsi_ax.set_xticklabels([date_labels[i] for i in tick_idx], rotation=0)

    card = FancyBboxPatch((0.02, 0.0), 0.96, 1.0, transform=side_ax.transAxes,
                           boxstyle="round,pad=0.01,rounding_size=0.02",
                           facecolor=CHART_SURFACE, edgecolor=CHART_BORDER, linewidth=1.0, clip_on=False)
    side_ax.add_patch(card)
    side_ax.text(0.12, 0.955, "TRADE PLAN", transform=side_ax.transAxes, color=CHART_TEXT_PRIMARY,
                 fontsize=13, fontweight="bold", va="top")
    side_ax.plot([0.10, 0.90], [0.915, 0.915], transform=side_ax.transAxes, color=CHART_BORDER, linewidth=0.8)

    rr1, rr2 = compute_reward_risk(c)
    rr_str = f"{rr1:.1f} / {rr2:.1f}" if rr1 is not None else "N/A"
    rows = [
        ("ENTRY", f"${c['entry_low']:,.2f}\u2013${c['entry_high']:,.2f}", CHART_BLUE),
        ("STOP", f"${c['stop']:,.2f}", CHART_RED),
        ("TARGET 1", f"${c['target1']:,.2f}", CHART_GREEN),
        ("TARGET 2", f"${c['target2']:,.2f}", CHART_GREEN),
        ("R:R", rr_str, CHART_TEXT_PRIMARY),
    ]
    row_y = 0.86
    for label, value, color in rows:
        side_ax.text(0.12, row_y, label, transform=side_ax.transAxes, color=color,
                     fontsize=9.5, fontweight="bold", va="center")
        side_ax.text(0.90, row_y, value, transform=side_ax.transAxes, color=CHART_TEXT_PRIMARY,
                     fontsize=10.5, fontweight="bold", va="center", ha="right")
        row_y -= 0.105

    side_ax.plot([0.10, 0.90], [row_y + 0.02, row_y + 0.02], transform=side_ax.transAxes, color=CHART_BORDER, linewidth=0.8)
    row_y -= 0.05
    side_ax.text(0.12, row_y, "CONFIRMATION", transform=side_ax.transAxes, color=CHART_TEXT_PRIMARY,
                 fontsize=10.5, fontweight="bold", va="top")
    row_y -= 0.06

    rvol = compute_rvol(chart_bars)
    rsi_now = rsi[-1]
    latest_close = closes[-1]
    if c["entry_low"] <= latest_close <= c["entry_high"]:
        entry_bullet = f"Price within entry zone (${latest_close:,.2f})"
    elif latest_close > c["entry_high"]:
        entry_bullet = f"Price above entry zone (${latest_close:,.2f}) \u2014 wait for a pullback"
    else:
        entry_bullet = f"Price below entry zone (${latest_close:,.2f}) \u2014 wait for confirmation"
    bullets = [
        entry_bullet,
        f"RVOL {'expansion' if rvol >= 1.2 else 'below average'} ({rvol:.1f}x)",
        f"RSI {'above' if rsi_now >= 50 else 'below'} 50 ({rsi_now:.0f})",
    ]
    for bullet in bullets:
        side_ax.text(0.13, row_y, "\u2022", transform=side_ax.transAxes, color=CHART_TEXT_SECONDARY, fontsize=9, va="top")
        side_ax.text(0.18, row_y, bullet, transform=side_ax.transAxes, color=CHART_TEXT_SECONDARY,
                     fontsize=9, va="top", wrap=True)
        row_y -= 0.07

    arrow = "\u25b2" if is_call else "\u25bc"
    quality_tag = build_quality_tag(c.get("pattern", ""))
    setup_type = build_setup_type_label(c["direction"], c.get("pattern", ""))
    dir_color = CHART_GREEN if is_call else CHART_RED

    fig.text(0.045, 0.975, f"${c['ticker']}", fontsize=20, fontweight="bold", color=CHART_TEXT_PRIMARY,
              ha="left", va="top", family="sans-serif")
    fig.text(0.16, 0.975, f" {setup_type} ", fontsize=11, fontweight="bold", color=dir_color,
              ha="left", va="top",
              bbox=dict(facecolor="none", edgecolor=dir_color, alpha=0.95, pad=5, linewidth=1.3,
                        boxstyle="round,pad=0.35"))
    fig.text(0.40, 0.975, f" {arrow} {c['direction']} ${c['strike']:g}  \u00b7  {c['next_expiry'].upper()} ",
              fontsize=11, fontweight="bold", color="#0d1117", ha="left", va="top",
              bbox=dict(facecolor=dir_color, edgecolor="none", alpha=1.0, pad=5, boxstyle="round,pad=0.35"))

    rvol_str = f"{rvol:.1f}x"
    stats_line = f"Close ${c.get('current_price', closes[-1]):,.2f}   |   RVOL {rvol_str}   |   RSI {rsi_now:.0f}   |   {quality_tag}"
    fig.text(0.045, 0.925, stats_line, fontsize=10, color=CHART_TEXT_SECONDARY, ha="left", va="top")

    plt.savefig(out_path, facecolor=CHART_BG, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)


def post_setup_with_chart(setup_embed: dict, chart_path: str, ticker: str, risk: str) -> bool:
    """One Discord message per setup: the fields embed immediately
    followed, in the SAME message, by an embed whose image is the
    attached chart PNG -- this is what makes the chart render directly
    under its own card instead of as a separately-ordered post."""
    color = RISK_COLOR_MAP.get(risk, COLOR_NEUTRAL)
    filename = os.path.basename(chart_path)
    chart_embed = {"title": f"${ticker} \u2014 Chart", "color": color,
                    "image": {"url": f"attachment://{filename}"}}
    payload = {"username": SENDER_USERNAME, "embeds": [setup_embed, chart_embed]}
    try:
        with open(chart_path, "rb") as f:
            files = {"file": (filename, f, "image/png")}
            data = {"payload_json": json.dumps(payload)}
            r = requests.post(DISCORD_WEBHOOK, data=data, files=files, timeout=30)
            print(f"  [DISCORD] ${ticker} setup+chart posted: {r.status_code}")
            if r.status_code not in (200, 204):
                print(f"    body: {r.text[:500]}")
            return r.status_code in (200, 204)
    except Exception as e:
        print(f"  [DISCORD] ${ticker} setup+chart post FAILED: {e}")
        return False



# ─────────────────────────────────────────────────────────────────────
# Card image renderer -- reused from the original plain-text
# bmt_nightly_setups.py, adapted for this embed-format version.
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
            f"Setups derived from {close_date_str} close   \u00b7   Re-validate at next session's open   \u00b7   Not financial advice",
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
    print(f"[{et_now.isoformat()}] BMT Nightly Setups")

    ensure_schema()

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
    print(f"  Publishing ideas for {target_date.strftime('%A, %B %d')}, using data as of {data_date.strftime('%A, %B %d')} close.\n")

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

    # BUGFIX (2026-08-11): normalize "setups" dict keys defensively --
    # strip any leading "$" and uppercase, regardless of what the
    # model actually returned. Previously a plain
    # narrative_result.get("setups", {}) lookup against c["ticker"]
    # (always bare, e.g. "AXTI") silently missed on every single
    # ticker whenever the model prefixed its JSON keys with "$" (e.g.
    # "$AXTI"), because dict.get() with a default doesn't raise -- it
    # just quietly returned {} for every setup, and every text field
    # fell through to "Not provided" with risk defaulting to
    # "Moderate" and roles assigned in flat VALID_ROLES order. The
    # prompt above now explicitly tells the model not to prefix JSON
    # keys, but this normalization is a second, independent layer so
    # a stray future response can't reproduce the same silent failure.
    raw_setups = narrative_result.get("setups", {})
    setups_by_ticker = {k.lstrip("$").upper(): v for k, v in raw_setups.items()}

    # Merge model output onto each selected setup, validating role/risk
    # against the fixed vocab -- fall back to a safe neutral value
    # rather than letting an unexpected label silently break the color
    # mapping or leave a field blank.
    used_roles = set()
    for c in selected:
        s = setups_by_ticker.get(c["ticker"].upper(), {})
        if not s:
            print(f"  [NARRATIVE WARN] {c['ticker']}: no matching entry in model's 'setups' output "
                  f"(keys returned: {list(raw_setups.keys())}) -- falling back to defaults for this setup")
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

    print(f"\nSaving {len(selected)} setup idea(s) to DB for results tracking...")
    save_setup_ideas(selected, target_date)

    print(f"\nRendering {len(selected)} chart(s)...")
    for c in selected:
        c["chart_bars"] = get_extended_chart_bars(c["ticker"])
        chart_path = f"chart_{c['ticker']}.png"
        render_setup_chart(c, chart_path)
        c["_chart_path"] = chart_path
        print(f"  {c['ticker']}: chart saved to {chart_path}")

    print("\nPosting header + best-choice embeds...")
    header_embed = build_header_embed(market_backdrop, target_date)
    best_choice_embed = build_best_choice_embed(top_pick_ticker, top_pick_why)
    posted_header = post_embeds_to_discord([header_embed, best_choice_embed])

    print("Posting each setup, immediately followed by its own chart...")
    posted_all_setups = True
    for i, c in enumerate(selected):
        setup_embed = build_setup_embed(c, rank=i + 1, is_top_pick=(c["ticker"] == top_pick_ticker))
        ok = post_setup_with_chart(setup_embed, c["_chart_path"], c["ticker"], c["risk"])
        posted_all_setups = posted_all_setups and ok

    print("Posting contract list...")
    contract_embed = build_contract_list_embed(selected)
    posted_contract = post_embeds_to_discord([contract_embed])

    # Summary overview card image -- unchanged, still posted at the end.
    spy = market_context.get("SPY", {})
    qqq = market_context.get("QQQ", {})
    market_theme = (f"$SPY closed at ${spy.get('price', 'N/A')} ({spy.get('pct', 'N/A')}%) and "
                     f"$QQQ at ${qqq.get('price', 'N/A')} ({qqq.get('pct', 'N/A')}%).")
    risk_notes = "See the write-up above for the reasoning, and each chart for exact entry/stop/target levels."

    out_path = "bmt_nightly_setups.png"
    render_card(selected, market_theme, risk_notes, market_context, target_date, data_date, out_path)
    print(f"\nSummary card saved to {out_path}")

    posted_card = post_image_to_discord(out_path, message="")

    if posted_header and posted_all_setups and posted_contract and posted_card:
        print("✓ Header, all setups+charts, contract list, and summary card posted to Discord!")
    else:
        if not posted_header:
            print("✗ Header/best-choice post FAILED")
        if not posted_all_setups:
            print("✗ One or more setup+chart posts FAILED")
        if not posted_contract:
            print("✗ Contract list post FAILED")
        if not posted_card:
            print("✗ Summary card image post FAILED")


run_nightly_job = main


def start_scheduler():
    scheduler = BackgroundScheduler(timezone="America/New_York")
    # Runs at 6:00pm ET -- this is now the only nightly setups job
    # (the old separate plain-text prod script has been replaced by
    # this embed-format version), matching the time subscribers are
    # already used to seeing posts land.
    scheduler.add_job(run_nightly_job, "cron", hour=18, minute=0, id="nightly_setups", replace_existing=True, max_instances=1)
    scheduler.start()
    print("Scheduler started: nightly setups job fires daily at 6:00pm ET (should_publish_tonight() internally skips weekends/holidays).")

    def heartbeat():
        while True:
            time.sleep(900)
            print(f"[HEARTBEAT] scheduler running={scheduler.running}")

    threading.Thread(target=heartbeat, daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    if os.environ.get("FORCE_PUBLISH") == "1":
        run_nightly_job()
    else:
        start_scheduler()