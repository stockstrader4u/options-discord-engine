"""
bmt_nightly_setups.py — Nightly top-5 trade-ideas digest.

Reuses ONLY proven, already-working pieces from the BMT stack:
  - JarvisFlow options flow: same call_jarvis() pattern as er_lotto_scanner.py
  - Daily OHLC: same yfinance pattern as get_historical_er_move()/get_options_skew()
  - Earnings calendar: same Finnhub /calendar/earnings forward-window shape
    already proven in er_lotto_scanner.py's get_earnings_calendar()
  - Ranking/reasoning: Grok via OpenRouter, same model already running the
    ER lotto pipeline — no new model to validate
  - Card image: matplotlib dark theme, same family as the ER lotto recap card
  - Posting: Discord webhook (multipart image upload)

No new data sources, no new AI models, no new validation needed.

UNIVERSE (SYNCED 2026-07-21): BMT's single MASTER SCAN watchlist — the
same 167-ticker curated list now used by er_lotto_scanner.py, so both
products scan one list instead of drifting apart. ETFs/leveraged
products/crypto pairs are excluded from the tradeable candidate pool
(SPY/QQQ/IWM are fetched separately for the market-context strip only).

FLOW NOTE: uses the SAME aggregate bullish/bearish flow summary already
proven in er_lotto_scanner.py's get_flow_for_ticker() — NOT individual
sweep/contract-level detail (that level of granularity isn't available
to a standalone script; it required a live MCP tool connection). This is
a real, acknowledged simplification vs. BMT's manual process, not hidden.

EXPIRY NOTE: strikes are suggested against the nearest available weekly
options expiry (computed dynamically per ticker, same pattern as
get_next_expiry() in the scanner) rather than manually re-picking
"Jul 17 or Jul 24" — this generalizes correctly week to week without
needing a hardcoded date.

Run locally:
    C:\\Python314\\python.exe bmt_nightly_setups.py

Requires: JARVIS_API_KEY, OPENROUTER_API_KEY, FINNHUB_API_KEY,
NIGHTLY_SETUPS_DISCORD_WEBHOOK (set to the TEST webhook for this run)
in your environment.
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

# ── Config ────────────────────────────────────────────────────────────────
JARVIS_API_KEY     = os.environ["JARVIS_API_KEY"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
# NEW (2026-07-21): needed for the upcoming-earnings exclusion filter —
# same Finnhub forward-window calendar shape already proven working in
# er_lotto_scanner.py. Must be added to the bmt-trade-ideas Railway
# service's Variables before deploying this version.
FINNHUB_API_KEY    = os.environ["FINNHUB_API_KEY"]
# NAMED DELIBERATELY (not "DISCORD_WEBHOOK") — if this script is deployed
# into a Railway service/repo that already runs other Discord-posting
# jobs (e.g. options-discord-engine's own flow alerts), a generically-
# named env var risks silently colliding with an existing one. This name
# is scoped specifically to this job.
DISCORD_WEBHOOK    = os.environ["NIGHTLY_SETUPS_DISCORD_WEBHOOK"]
JARVIS_MCP_URL      = "https://api.jarvisflow.io/.well-known/mcp"
OPENROUTER_BASE    = "https://openrouter.ai/api/v1"
FINNHUB_BASE       = "https://finnhub.io/api/v1"
ET                 = ZoneInfo("America/New_York")
HEADERS            = {"User-Agent": "Mozilla/5.0"}

MIN_PREMIUM = 50_000   # matches BMT's manual "min $50K premium" filter
TOP_N       = 5

# ── Universe (SYNCED 2026-07-21 to the single BMT MASTER SCAN list) ────────
# This is now the SAME 167-ticker curated list deployed to
# er_lotto_scanner.py on 2026-07-21 — one master list for both products,
# no more drift. Changes vs the old copy of this list: added TDOC, ROKU,
# Z, MA, CHWY, XPEV, IBIT, SPOT-adjacent adds, plus the big_mover_screen-
# validated FISV / AKAM / LUV / PYPL; dropped CRML and MP (removed from
# the master list); SPY/QQQ/IWM removed from the list itself since they
# are fetched separately for the market-context strip and were never
# tradeable candidates anyway.
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

# ETFs, leveraged products, and non-equity symbols — excluded from the
# tradeable candidate pool per BMT's own manual prompt rule. SPY/QQQ/IWM
# are fetched separately for the market-context strip; IBIT is on the
# master list (it reports nothing and trends like BTC) but is an ETF and
# therefore stays out of the single-name setup pool.
EXCLUDE_FROM_CANDIDATES = {
    "IWM", "QQQ", "SPY", "UVXY", "SQQQ", "TQQQ", "NUGT", "SLV", "USO",
    "IBIT", "NVDL", "OKEX:ETHUSD", "COINBASE:^BTCUSD",
}
MARKET_CONTEXT_TICKERS = ["SPY", "QQQ", "IWM"]

CANDIDATE_UNIVERSE = [t for t in FULL_WATCHLIST if t not in EXCLUDE_FROM_CANDIDATES]


# ── Upcoming-earnings exclusion (NEW 2026-07-21) ───────────────────────────
# GAP FIX: this digest previously had zero earnings-date awareness. During
# earnings season that's dangerous — a "clean lower highs" chart on a
# ticker reporting within the trade's expiry window is a fundamentally
# different trade: the structure gets vaporized by the print, the stop is
# meaningless through a gap, and IV crush hits the option even when the
# direction is right. Earnings plays are the ER lotto product's job, not
# this digest's. Any candidate whose confirmed upcoming earnings date
# falls ON OR BEFORE its suggested expiry is excluded outright (and named
# in the rejected list so Grok's risk notes can reference it).
#
# PRIMARY SOURCE — yfinance get_earnings_dates() per ticker (see the
# full dead-end history in get_upcoming_earnings_date()'s docstring:
# Finnhub's calendar is capped at 1,500 rows on the free tier, and
# Yahoo's chart-meta earningsTimestamp fields are confirmed empty).
#
# PLACEMENT — the check runs AFTER ranking, walking the ranked list
# top-down and skipping blocked tickers until TOP_N clean setups are
# found. Rationale: get_earnings_dates() is the same endpoint that
# triggered a real YFRateLimitError during big_mover_screen.py's heavy
# usage, so checking all ~100-130 flow-qualifiers per night (on top of
# the OHLC pulls already hitting Yahoo) invites rate limiting. Checking
# in rank order needs only ~5-10 calls per night and produces the
# identical outcome — an excluded ticker simply lets the next-ranked
# one slide in, and still gets named in the rejected list.
#
# SECONDARY NET — the Finnhub forward-window map is still loaded and
# consulted (its confirmed-working shape: short forward window, no
# symbol filter). If EITHER source reports an earnings date on or before
# expiry, the candidate is excluded. A ticker absent from both sources
# is treated as "no earnings in window."
#
# FAIL-OPEN BY DESIGN, LOUDLY: if a source fails, the run proceeds with
# whatever earnings data IS available (with loud warnings) rather than
# skipping the whole digest — a weakened safety net for one night is
# preferable to silently never posting.
EARNINGS_LOOKAHEAD_DAYS = 14  # comfortably covers any weekly-expiry window

def get_upcoming_earnings_map() -> dict:
    """Returns {ticker: 'YYYY-MM-DD' earliest upcoming earnings date} for
    the next EARNINGS_LOOKAHEAD_DAYS, or {} on total failure (logged)."""
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
            print(f"  [ER FILTER] Loaded {len(er_map)} upcoming earnings dates "
                  f"({today} to {end})")
            return er_map
        except Exception as e:
            print(f"  [ER FILTER WARN] Finnhub calendar attempt {attempt+1}: {e}")
    print("  [ER FILTER WARN] Could not load Finnhub earnings calendar after 3 attempts — "
          "falling back to Yahoo per-ticker checks only for tonight's run.")
    return {}


def get_upcoming_earnings_date(ticker: str) -> str:
    """
    PRIMARY earnings-date source (REVISED 2026-07-21, same night, after
    TWO confirmed-dead cheap sources — see comment block above):
      1. Finnhub forward calendar: capped at 1,500 rows on the free
         tier, silently missing TSLA/GOOGL/IBM/INTC/NOW/CRM/MMM during
         a peak-season window (confirmed via live diagnostic query).
      2. Yahoo chart-meta earningsTimestamp fields: confirmed live to
         be None across the board (TSLA/GOOGL/IBM/MMM/AAPL all empty) —
         consistent with the same morning's ER lotto run needing the
         Grok fallback for every single date verification.
    FINAL: yfinance's get_earnings_dates() — the SAME function already
    extensively validated in production for get_historical_er_move()'s
    fix, which returns FUTURE scheduled dates in the same dataframe as
    past ones. Confirmed live before adopting: TSLA/GOOGL/IBM all
    correctly showed 2026-07-22, BE/CAR showed 2026-07-28. Requires
    lxml (added to this repo's requirements.txt — it was previously
    only in -bmt-market-mcp's, per the different-repos-different-
    packages convention).
    Returns the EARLIEST future 'YYYY-MM-DD' date, or None.
    """
    try:
        import yfinance as yf
        edf = yf.Ticker(ticker).get_earnings_dates(limit=8)
        if edf is None or edf.empty:
            return None
        now = datetime.now(ET).replace(tzinfo=None)
        future = sorted(
            idx.strftime("%Y-%m-%d") for idx in edf.index
            if idx.replace(tzinfo=None) > now
        )
        return future[0] if future else None
    except Exception as e:
        print(f"  [ER FILTER WARN] {ticker}: yfinance earnings-date check failed "
              f"({type(e).__name__}: {e}) — relying on Finnhub map alone for this ticker")
        return None


# ── JarvisFlow (EXACT same pattern as er_lotto_scanner.py) ─────────────────
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
    """Same logic/thresholds as er_lotto_scanner.py's get_flow_for_ticker(),
    but returns structured data (bias, premium, pct) instead of a
    pre-formatted string, since we need the raw premium number to filter
    on MIN_PREMIUM here."""
    result = call_jarvis("stock_ticker_unusual_options_data", {"filter_by_Ticker": ticker})
    if not result:
        return {"bias": None, "premium": 0, "call_pct": None}
    flow = result.get("optionsFlow", []) if isinstance(result, dict) else result
    if not flow:
        return {"bias": None, "premium": 0, "call_pct": None}
    flow = [f for f in flow if f.get("ticker", "").upper() == ticker.upper()]
    if not flow:
        return {"bias": None, "premium": 0, "call_pct": None}

    bought_otm_atm = [
        f for f in flow
        if f.get("implied_Bought_Or_Sold") == "BOUGHT"
        and f.get("moneyNess", "").upper() in ("OTM", "ATM")
    ]
    if not bought_otm_atm:
        return {"bias": None, "premium": 0, "call_pct": None}

    total_call = sum(float(f.get("total_Option_Premium_For_Trade", 0) or 0)
                      for f in bought_otm_atm if f.get("put_Or_Call") == "CALL")
    total_put = sum(float(f.get("total_Option_Premium_For_Trade", 0) or 0)
                     for f in bought_otm_atm if f.get("put_Or_Call") == "PUT")
    total = total_call + total_put
    if total == 0:
        return {"bias": None, "premium": 0, "call_pct": None}

    call_pct = round(total_call / total * 100)
    bias = "Bullish" if call_pct > 55 else "Bearish" if call_pct < 45 else "Neutral"
    return {"bias": bias, "premium": total, "call_pct": call_pct}


# ── Daily OHLC (same yfinance pattern proven elsewhere in the stack) ───────
def get_daily_ohlc(ticker: str, sessions: int = 15) -> list:
    """Returns structured daily bars: [{date, open, high, low, close,
    volume}, ...] oldest-to-newest. Needed for deterministic swing
    high/low calculation, not just the formatted text summary. Volume
    added 2026-07-21 for the flow-intensity ranking (see main())."""
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        hist = stock.history(period="2mo")
        if hist.empty:
            return []
        hist = hist.tail(sessions)
        return [
            {"date": date, "open": row["Open"], "high": row["High"],
             "low": row["Low"], "close": row["Close"],
             "volume": row.get("Volume", 0) or 0}
            for date, row in hist.iterrows()
        ]
    except Exception as e:
        print(f"  [OHLC WARN] {ticker}: {e}")
        return []


def compute_avg_dollar_volume(bars: list) -> float:
    """Average daily dollar volume (close x share volume) across the
    supplied bars. Used to normalize flow premium into a size-relative
    'intensity' — computed entirely from bars already fetched, zero
    extra network calls."""
    vals = [b["close"] * b["volume"] for b in bars if b.get("close") and b.get("volume")]
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def format_ohlc_summary(bars: list) -> str:
    return "\n".join(
        f"{b['date'].strftime('%b %d')}: O={b['open']:.2f} H={b['high']:.2f} "
        f"L={b['low']:.2f} C={b['close']:.2f}"
        for b in bars
    )


# ── Deterministic trade-level calculation (NEW 2026-07-18) ────────────────
# BUGFIX CONTEXT: confirmed via a back-to-back same-data test that Grok,
# even at temperature=0, produced DIFFERENT entry/stop/target numbers for
# the identical AMD setup across two runs with byte-identical input flow
# data (entry $480-490/stop $505 vs entry $490-495/stop $510). The chart-
# quality JUDGMENT (is this clean, does flow align) is exactly the kind
# of task an LLM is suited for — but the specific PRICE LEVELS shouldn't
# be left to the model's own arithmetic/discretion, same principle as the
# Beat Signal Strength and R/R fixes elsewhere in the BMT stack. Grok's
# job now is ONLY to accept/reject and describe WHY (quality_tag,
# narrative) — every price number (strike, entry, stop, targets) is
# computed here in Python from the real swing high/low in the actual
# daily OHLC data, so the same input always produces the same output.
def get_strike_increment(price: float) -> float:
    if price < 50:
        return 1.0
    elif price < 200:
        return 2.5
    else:
        return 5.0

def compute_strike(direction: str, current_price: float) -> float:
    inc = get_strike_increment(current_price)
    if direction.upper() == "CALL":
        return round((current_price // inc + 1) * inc, 2)
    else:
        return round((current_price // inc) * inc, 2)

def compute_trade_levels(direction: str, bars: list, current_price: float) -> dict:
    """
    Entry: tight range around current price.
    Stop: just beyond the real recent swing low (calls) / swing high
    (puts) — deliberately a SHORT lookback (the last 2 sessions), since
    including the origin of a multi-day move produces unrealistic stops
    for a short-DTE options play. Confirmed via testing: a 5-session
    lookback on a real AMD breakdown (peak $548 four days back, current
    $495.76) produced a 55-point/~11% stop distance — nothing like the
    2-4.5% stop distances seen on BMT's real sample cards (BE: entry
    $214.50/stop $224 = 4.4%, SNOW: $268.80/$275 = 2.3%, DDOG:
    $258.60/$265 = 2.5%). A 2-session window captures the most recent,
    structurally relevant swing point instead of a stale origin.
    Targets: fixed R-multiples (1.5R / 2.5R) off the real entry-to-stop
    risk distance, so R/R is mathematically guaranteed consistent rather
    than something the model has to get right on its own.
    """
    recent = bars[-2:] if len(bars) >= 2 else bars
    entry_mid = current_price
    entry_low = round(current_price * 0.995, 2)
    entry_high = round(current_price * 1.005, 2)

    if direction.upper() == "CALL":
        swing_low = min(b["low"] for b in recent)
        stop = round(min(swing_low * 0.995, entry_low * 0.98), 2)
        risk = entry_mid - stop
        target1 = round(entry_mid + risk * 1.5, 2)
        target2 = round(entry_mid + risk * 2.5, 2)
    else:
        swing_high = max(b["high"] for b in recent)
        stop = round(max(swing_high * 1.005, entry_high * 1.02), 2)
        risk = stop - entry_mid
        target1 = round(entry_mid - risk * 1.5, 2)
        target2 = round(entry_mid - risk * 2.5, 2)

    return {
        "entry_low": entry_low, "entry_high": entry_high,
        "stop": stop, "target1": target1, "target2": target2,
    }


# ── Deterministic chart-pattern detection (NEW 2026-07-18) ────────────────
# BUGFIX CONTEXT: the price-level fix above solved HALF the problem —
# confirmed via a real back-to-back test that Grok, even at temperature=0,
# produced different accept/reject decisions AND different top-5 picks on
# byte-identical flow data (e.g. AMD/TWLO/OKTA vs AMD/TWLO/DKNG across two
# runs with nothing else changed). The price-level determinism fix never
# touched THIS — Grok was still the one deciding which tickers "have a
# clean chart," and that judgment call was exactly what varied.
#
# Fixed the same way as the price levels: real swing-point analysis on the
# actual daily bars, computed in Python, not eyeballed by an LLM reading
# formatted OHLC text. Grok's role now narrows to writing narrative prose
# for tickers Python has ALREADY selected — it no longer decides accept/
# reject or ranking at all, which removes the actual source of the
# non-determinism rather than just its downstream price-number symptoms.
def find_swing_points(bars: list) -> tuple:
    """Returns (swing_highs, swing_lows) as lists of (index, price) for
    local extremes — a bar whose high/low exceeds both immediate
    neighbors. Standard, deterministic technical-analysis definition."""
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
    """
    Returns {"clean": bool, "pattern": str or None}. Checks for CALLS,
    in order:
    (a) a smooth monotonic rise in daily lows — the cleanest possible
        structure, but one with ZERO interior swing points by
        definition (a pure rise has no local minima to detect), so this
        has to be checked directly rather than via swing-point analysis
    (b) a genuine higher-lows sequence across real swing points (for
        trends with minor pullbacks along the way)
    (c) a clean V-recovery (a real prior low, followed by recovery with
        no subsequent lower low)
    Fails closed on insufficient data — never guesses a chart is clean.
    """
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
    """Mirror of is_clean_uptrend() for PUTS: smooth monotonic fall in
    daily highs, a lower-highs swing sequence, or a clean breakdown."""
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


def check_chart_pattern(flow_bias: str, bars: list) -> dict:
    """
    Single entry point: given the flow bias (Bullish/Bearish/Neutral) and
    real daily bars, deterministically decides direction + whether the
    chart structure genuinely supports it. Neutral-flow tickers are
    excluded outright — same "flow direction must align with chart
    direction" rule from BMT's original manual prompt, just enforced in
    code instead of left to an LLM's discretion.
    """
    if flow_bias == "Bullish":
        result = is_clean_uptrend(bars)
        return {"direction": "CALL", "clean": result["clean"], "pattern": result["pattern"]}
    elif flow_bias == "Bearish":
        result = is_clean_downtrend(bars)
        return {"direction": "PUT", "clean": result["clean"], "pattern": result["pattern"]}
    else:
        return {"direction": None, "clean": False, "pattern": None}


def get_quote_change(ticker: str) -> dict:
    """
    BUGFIX (2026-07-18): originally trusted Yahoo's meta.previousClose /
    meta.chartPreviousClose fields directly for the % change calculation.
    Confirmed wrong in production: script reported SPY -1.53% and QQQ
    -3.12% for July 17, 2026, when the REAL close-to-close moves that
    day were S&P 500 -1.01% and Nasdaq -1.40% (per real index data).
    QQQ's reported move was roughly DOUBLE the real one — consistent
    with the meta field accidentally referencing a close from 2 sessions
    back rather than the immediately preceding session (a known Yahoo
    quirk that gets worse when queried outside regular trading hours,
    e.g. over a weekend, which is exactly when this was caught).

    Fixed: pull actual daily OHLC bars directly (same real chart data
    used elsewhere in this file) and compute % change from the two most
    recent REAL closing bars ourselves, rather than trusting a
    single meta field that can silently reference the wrong day.
    """
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "1d", "range": "10d"},
            headers=HEADERS, timeout=10
        )
        result = r.json()["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        closes, opens, highs, lows = quote["close"], quote["open"], quote["high"], quote["low"]

        # Keep only bars with a real close (last bar of day can be None
        # intraday/pre-market) — walk from the end to find the two most
        # recent COMPLETE sessions.
        valid_idxs = [i for i in range(len(closes)) if closes[i] is not None]
        if len(valid_idxs) < 2:
            return {"price": None, "pct": None, "open": None, "high": None, "low": None}

        last_idx, prev_idx = valid_idxs[-1], valid_idxs[-2]
        price = closes[last_idx]
        prev_close = closes[prev_idx]
        pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else None

        return {
            "price": round(price, 2), "pct": pct,
            "open": round(opens[last_idx], 2) if opens[last_idx] else None,
            "high": round(highs[last_idx], 2) if highs[last_idx] else None,
            "low": round(lows[last_idx], 2) if lows[last_idx] else None,
        }
    except Exception as e:
        print(f"  [QUOTE WARN] {ticker}: {e}")
        return {"price": None, "pct": None, "open": None, "high": None, "low": None}


def get_tone_phrase(m: dict) -> str:
    """Deterministic tone phrase from daily O/H/L/C — a coarser stand-in
    for BMT's real intraday-based tone text (e.g. 'afternoon fade'),
    since this script only has daily bars, not minute-level data. Close
    position within the day's range is used as the best available proxy
    for how the session actually traded."""
    price, o, h, l, pct = m.get("price"), m.get("open"), m.get("high"), m.get("low"), m.get("pct")
    if not all([price, o, h, l]) or h == l:
        return "N/A"
    range_pos = (price - l) / (h - l)  # 0 = closed at low, 1 = closed at high
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


def get_next_expiry(ticker: str) -> dict:
    """Returns {"label": "Jul 24", "iso": "2026-07-24"} for the nearest
    available expiry, or {"label": "N/A", "iso": None}. ISO date added
    2026-07-21 so the earnings-exclusion filter can compare the real
    expiry date against a ticker's confirmed upcoming earnings date."""
    try:
        import yfinance as yf
        expirations = yf.Ticker(ticker).options
        if not expirations:
            return {"label": "N/A", "iso": None}
        today = datetime.now(ET).strftime("%Y-%m-%d")
        for exp in expirations:
            if exp >= today:
                dt = datetime.strptime(exp, "%Y-%m-%d")
                return {"label": dt.strftime("%b %d"), "iso": exp}
        return {"label": "N/A", "iso": None}
    except Exception:
        return {"label": "N/A", "iso": None}


# ── Grok narrative call (ONE call per night, same model as the ER lotto
# pipeline) — NARROWED SCOPE (2026-07-18): Grok no longer decides WHICH
# tickers qualify or how they rank. That decision is now 100%
# deterministic (see check_chart_pattern() and its callers in main()).
# Grok's only remaining job is writing readable narrative prose and a
# short quality tag for tickers Python has ALREADY selected — a task
# where run-to-run wording variance is cosmetic, not decision-changing.
def write_narratives(selected: list, rejected_summary: list, market_context: dict) -> dict:
    context_block = "\n".join(
        f"{t}: ${market_context[t]['price']} ({market_context[t]['pct']:+.2f}%) "
        f"[O={market_context[t].get('open')} H={market_context[t].get('high')} "
        f"L={market_context[t].get('low')} C={market_context[t]['price']}]"
        for t in MARKET_CONTEXT_TICKERS if market_context.get(t, {}).get("price")
    )

    setup_blocks = []
    for c in selected:
        setup_blocks.append(
            f"### {c['ticker']} — ALREADY SELECTED as a {c['direction']} "
            f"(deterministic pattern match: {c['pattern']})\n"
            f"Options Flow: {c['flow']['bias']} — ${c['flow']['premium']:,.0f} bought OTM/ATM premium "
            f"({c['flow']['call_pct']}% call-weighted)\n"
            f"Next expiry: {c['next_expiry']}\n"
            f"Last {len(c['ohlc_text'].splitlines())} sessions (daily OHLC):\n{c['ohlc_text']}\n"
        )
    setups_text = "\n".join(setup_blocks)
    rejected_text = "; ".join(rejected_summary) if rejected_summary else "none notable"

    prompt = f"""You are writing subscriber-facing narrative copy for options setups that have ALREADY been selected by a deterministic screen — you are NOT deciding which tickers qualify, only describing why the ones given to you are good setups, using the real daily price data.

MARKET CONTEXT (today's SPY/QQQ/IWM open/high/low/close):
{context_block}

ALREADY-SELECTED SETUPS (do not second-guess these, just write accurate narrative for each):
{setups_text}

TICKERS THE DETERMINISTIC SCREEN REJECTED (for your risk-notes reference only): {rejected_text}

TASK:
For each already-selected setup, write:
- quality_tag: a 2-4 word badge summarizing the pattern (e.g. "Clean breakdown", "Higher lows pullback")
- narrative: 2 sentences MAXIMUM, under 260 characters total, citing SPECIFIC price levels and dates from the daily data given — must be a complete thought that fits, not a longer narrative cut short
- flow_note: short phrase summarizing the flow, e.g. "$855K at-ask puts, ~2% OTM"

Also write:
- market_theme: ONE sentence describing today's overall market theme/bias, citing the real SPY/QQQ/IWM O/H/L/C given above
- risk_notes: 1-2 sentences on risk for tomorrow (gap risk, theta given DTE, etc.) that names 2-3 of the rejected tickers listed above with brief reasons

Return ONLY valid JSON, this exact shape, nothing else, one entry per selected ticker in the SAME ORDER given:
{{
  "market_theme": "one sentence, cites real O/H/L/C levels",
  "risk_notes": "1-2 sentences naming specific rejected tickers",
  "setups": [
    {{"ticker": "XXXX", "quality_tag": "...", "narrative": "...", "flow_note": "..."}}
  ]
}}"""

    resp = requests.post(
        f"{OPENROUTER_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json={"model": "x-ai/grok-4.3", "max_tokens": 3000, "temperature": 0,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90
    )
    content = resp.json()["choices"][0]["message"]["content"].strip()
    content = content.replace("```json", "").replace("```", "").strip()
    return json.loads(content)


import textwrap

def fit_value_fontsize(text: str, col_w_units: float, base_fontsize: float,
                        min_fontsize: float = 6.5, margin: float = 0.85) -> float:
    """
    BUGFIX (2026-07-18): fixed-width stat-table columns with a fixed
    font size will always eventually collide once a price gets wide
    enough — confirmed in production on SNDK ($1348.05-1361.50 entry
    overlapping its $1559.39 stop) and similar cases on BE/AVGO/TSM/MU.
    Rather than guess a smaller fixed size and hope, this computes the
    actual available pixel width for the column (this figure is built
    at dpi=150 with data units == inches, so 1 data unit = 150px exactly
    — no guessing) and shrinks the font size down from base_fontsize
    only as much as needed for the specific text to physically fit,
    so short numbers ($24) stay full-size and wide ones ($1348.05-
    1361.50) shrink just enough to never overlap, regardless of price
    magnitude. bold_factor accounts for bold text being visibly wider
    per character than regular weight.
    """
    bold_factor = 1.75
    avail_px = col_w_units * 150 * margin
    needed_px = len(text) * base_fontsize * bold_factor
    if needed_px <= avail_px:
        return base_fontsize
    return max(base_fontsize * avail_px / needed_px, min_fontsize)


def wrap_lines(text: str, width_chars: int, max_lines: int) -> list:
    """
    BUGFIX (2026-07-18): matplotlib's built-in wrap=True does NOT respect
    a local column width — it estimates wrapping against the full figure
    width, which is fine for a single full-width text block but breaks
    completely in a multi-card side-by-side layout like this one. In
    production this caused every card's narrative text to render as one
    long unwrapped line that visually bled across into neighboring
    cards, producing an unreadable overlapping mess. Fixed: wrap text
    manually to a known character width (calibrated to this card's
    actual pixel width) using Python's own textwrap, then render each
    line as a separate positioned text call — full control, no reliance
    on matplotlib guessing a width it doesn't actually know.
    """
    lines = textwrap.wrap(text, width=width_chars)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + "..."
    return lines


# ── Card rendering (matplotlib, matches BMT's real watchlist card layout) ──
def compute_rr(entry_low, entry_high, target1, stop) -> float:
    """R/R computed in Python from real numbers, not trusted from Grok's
    own math — same principle as the deterministic Beat Signal Strength
    fix elsewhere in the BMT stack."""
    entry_mid = (entry_low + entry_high) / 2
    reward = abs(target1 - entry_mid)
    risk = abs(entry_mid - stop)
    if risk == 0:
        return 0.0
    return round(reward / risk, 1)


def render_card(accepted: list, rejected: list, market_theme: str, risk_notes: str,
                 market_context: dict, target_date: datetime, data_date: datetime, out_path: str):
    # ── Design tokens ──────────────────────────────────────────────────
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
    MAX_CARD_W = 8.0  # only clamps the extreme 1-2 card case — the natural
                       # fill width at 3, 4, and 5 cards (4.4-7.5) stays
                       # well under this and is untouched
    max_available_w = fig_w - 1.0
    dynamic_w = (max_available_w - CARD_GAP * (max(n, 1) - 1)) / max(n, 1)
    card_w = min(dynamic_w, MAX_CARD_W)
    row_w = max(n, 1) * card_w + (max(n, 1) - 1) * CARD_GAP
    row_start_x = 0.5 + (max_available_w - row_w) / 2  # only creates margin when the cap actually kicks in
    placeholder_h = 8.0
    fig_h = 5.4 + placeholder_h
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=150, facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, fig_w); ax.set_ylim(0, fig_h); ax.axis("off"); ax.invert_yaxis()

    # ── Header ──────────────────────────────────────────────────────────
    ax.text(0.5, 0.3, "BMT WATCHLIST", fontsize=12, fontweight="bold", color=TEXT_TERTIARY,
            va="top", zorder=5)
    ax.text(0.5, 0.85, today_str, fontsize=32, fontweight="bold", color=TEXT_PRIMARY, va="top", zorder=5)
    ax.text(0.5, 1.55, f"{n} setups   \u00b7   {dir_summary}   \u00b7   {expiry_summary}   \u00b7   "
            f"Based on {close_date_str} close", fontsize=12, color=TEXT_SECONDARY, va="top", zorder=5)

    # ── Market context strip — vertical dividers between tickers ──────
    ctx_y = 2.15
    ctx_w = (fig_w - 1.0 - 0.5 * 2) / 3
    for i, t in enumerate(MARKET_CONTEXT_TICKERS):
        x = 0.5 + i * (ctx_w + 0.5)
        m = market_context.get(t, {})
        pct = m.get("pct")
        color = GREEN if (pct or 0) >= 0 else RED
        arrow = "\u2191" if (pct or 0) >= 0 else "\u2193"
        ax.text(x, ctx_y, t, fontsize=13, fontweight="bold", color=TEXT_SECONDARY, va="top", zorder=5)
        ax.text(x, ctx_y + 0.4, f"${m.get('price', '?')}", fontsize=20, fontweight="bold",
                color=TEXT_PRIMARY, va="top", zorder=5)
        ax.text(x, ctx_y + 0.88, get_tone_phrase(m), fontsize=9, color=TEXT_TERTIARY, va="top", zorder=5)
        ax.text(x + ctx_w, ctx_y, f"{arrow} {abs(pct):.2f}%" if pct is not None else "N/A",
                fontsize=14, fontweight="bold", color=color, va="top", ha="right", zorder=5)
        if i > 0:
            divider_x = x - 0.25
            ax.plot([divider_x, divider_x], [ctx_y, ctx_y + 1.1], color=BORDER, linewidth=1, zorder=4)
    rule_y = ctx_y + 1.3
    ax.plot([0.5, fig_w - 0.5], [rule_y, rule_y], color=BORDER, linewidth=1, zorder=3)
    cursor_y = rule_y + 0.35

    # ── Theme / risk callouts — left accent bar, no full box outline ──
    theme_color = RED if n_puts > n_calls else GREEN if n_calls > n_puts else BLUE
    theme_lines = wrap_lines(market_theme, width_chars=160, max_lines=3)
    for i, line in enumerate(theme_lines):
        if i == 0:
            ax.add_patch(plt.Rectangle((0.5, cursor_y + 0.02), 0.06, 0.26, facecolor=theme_color,
                                        linewidth=0, zorder=4))
        ax.text(0.72, cursor_y, line, fontsize=11.5, color=TEXT_PRIMARY, va="top", zorder=5)
        cursor_y += 0.3
    cursor_y += 0.25

    risk_lines = wrap_lines(risk_notes, width_chars=160, max_lines=4)
    for i, line in enumerate(risk_lines):
        if i == 0:
            ax.add_patch(plt.Rectangle((0.5, cursor_y + 0.02), 0.06, 0.26, facecolor=GOLD,
                                        linewidth=0, zorder=4))
        ax.text(0.72, cursor_y, line, fontsize=10.5, color=TEXT_SECONDARY, va="top", zorder=5)
        cursor_y += 0.28
    cursor_y += 0.45

    # ── Individual setup cards ────────────────────────────────────────
    # Design: ONE accent element per card (a left-edge color bar), not a
    # box-in-box stack. Direction/strike lives in the header row next to
    # the ticker instead of its own bordered pill. Trade levels render as
    # a clean stat-table (thin vertical dividers between columns) instead
    # of a colored box, so color is used deliberately — accent bar,
    # direction text, stop/target numbers — rather than everywhere.
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
        lines = wrap_lines(s.get("narrative", ""), width_chars=48, max_lines=6)
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
        # single left accent bar — the ONE color identity element per card
        ax.add_patch(plt.Rectangle((x, cards_top + 0.15), 0.06, card_h - 0.3, facecolor=accent,
                                    linewidth=0, zorder=3))

        cx = x + PAD_L
        yy = cards_top + PAD_TOP

        # Header row: ticker left, direction+strike right, same baseline
        ax.text(cx, yy, s["ticker"], fontsize=21, fontweight="bold", color=TEXT_PRIMARY, va="top", zorder=5)
        arrow = "\u25b2" if is_call else "\u25bc"
        ax.text(x + card_w - 0.3, yy + 0.02, f"{arrow} {s['direction']} ${s['strike']:g}",
                fontsize=13, fontweight="bold", color=accent, va="top", ha="right", zorder=5)
        yy += HEADER_H

        ax.text(cx, yy, f"${s.get('current_price', '?')} close  \u00b7  {s.get('company_name', '')}",
                fontsize=8.7, color=TEXT_TERTIARY, va="top", zorder=5)
        ax.text(x + card_w - 0.3, yy, f"{s.get('next_expiry', '')} \u00b7 {s.get('dte', '?')} DTE",
                fontsize=8.7, color=TEXT_TERTIARY, va="top", ha="right", zorder=5)
        yy += SUBTITLE_H + GAP1

        # Quality tag as small colored text with a dot, not a bordered pill
        ax.scatter([cx + 0.05], [yy + 0.16], s=18, color=accent, zorder=5)
        ax.text(cx + 0.2, yy, s.get("quality_tag", "").upper(), fontsize=8.5, fontweight="bold",
                color=accent, va="top", zorder=5)
        yy += BADGE_H + GAP2

        for line in s["_narrative_lines"]:
            ax.text(cx, yy, line, fontsize=9, color=TEXT_SECONDARY, va="top", zorder=5)
            yy += NARRATIVE_LINE_H
        yy += (max_narrative_lines - len(s["_narrative_lines"])) * NARRATIVE_LINE_H
        yy += GAP3

        # Trade levels as a clean stat-table — thin vertical dividers,
        # no bordered box, color reserved for the stop/target values only.
        # BUGFIX (2026-07-18): fixed equal-width columns with fixed font
        # sizes overlapped once prices got wide (confirmed on SNDK:
        # $1348.05-1361.50 entry running into its $1559.39 stop, plus
        # similar collisions on BE/AVGO/TSM/MU). Fixed with (a) weighted
        # column widths — ENTRY gets 30% more room since its range format
        # is inherently wider than a single number — and (b) per-value
        # auto-fit font sizing via fit_value_fontsize(), so any value
        # shrinks just enough to physically fit its column regardless of
        # how large the price gets, instead of a fixed size that
        # eventually breaks.
        total_w = card_w - 2 * (PAD_L - 0.1)
        col_weights = [1.3, 0.9, 0.9, 0.9]
        col_widths = [total_w * w / sum(col_weights) for w in col_weights]
        labels = ["ENTRY", "STOP", "TARGET 1", "TARGET 2"]
        values = [f"${s['entry_low']}\u2013${s['entry_high']}", f"${s['stop']}",
                  f"${s['target1']}", f"${s['target2']}"]
        colors = [TEXT_PRIMARY, RED, GREEN, GREEN]
        base_sizes = [10, 12, 12, 12]
        col_x = cx - 0.1
        for ci, (lab, val, vc, base_sz, cw) in enumerate(zip(labels, values, colors, base_sizes, col_widths)):
            if ci > 0:
                ax.plot([col_x, col_x], [yy, yy + STAT_LABEL_H + STAT_VALUE_H - 0.05],
                        color=BORDER, linewidth=0.8, zorder=4)
            fitted_sz = fit_value_fontsize(val, cw, base_sz)
            ax.text(col_x + cw / 2, yy, lab, fontsize=6.8, color=TEXT_TERTIARY, va="top",
                    ha="center", zorder=5)
            ax.text(col_x + cw / 2, yy + STAT_LABEL_H, val, fontsize=fitted_sz, fontweight="bold",
                    color=vc, va="top", ha="center", zorder=5)
            col_x += cw
        yy += STAT_LABEL_H + STAT_VALUE_H + GAP4

        # NEVER show R:R to subscribers (BMT explicit instruction,
        # 2026-07-18) — flow note only, full width, no right-aligned stat
        ax.text(cx, yy, f"FLOW   {s.get('flow_note', '')}", fontsize=8, color=TEXT_TERTIARY, va="top", zorder=5)

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
        print(f"Discord post: {r.status_code}")
        return r.status_code in (200, 204)


# ── US Market Holiday Calendar + Scheduling Logic (NEW 2026-07-18) ────────
# MAINTENANCE WARNING: this list is specific to 2026 and MUST be updated
# every year — NYSE/NASDAQ holidays shift (e.g. Good Friday, observed
# dates) and are not computable from a simple formula. A stale list would
# cause this script to treat a real holiday as a normal trading day,
# publishing "next day" ideas using data that doesn't exist, or trying to
# run on a day the market never opened. The runtime check below prints a
# loud warning if the script is ever run outside 2026 without this list
# being updated, per the same fail-loud philosophy as the rest of this
# codebase (never silently assume stale config is still correct).
US_MARKET_HOLIDAYS_2026 = {
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # Martin Luther King Jr. Day
    "2026-02-16",  # Washington's Birthday (Presidents' Day)
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (observed — July 4 falls on a Saturday)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving Day
    "2026-12-25",  # Christmas Day
}
# Early-closure days (1:00pm ET) are NOT included above — the market IS
# open and trading occurs, just for fewer hours. A 6pm ET run happens
# comfortably after even an early close, so these need no special
# handling: they're valid trading days with fully settled data by the
# time this script runs.
US_MARKET_EARLY_CLOSE_2026 = {"2026-11-27", "2026-12-24"}


def _check_holiday_list_freshness():
    current_year = datetime.now(ET).year
    if current_year != 2026:
        print(f"  [HOLIDAY LIST WARNING] Running in {current_year}, but "
              f"US_MARKET_HOLIDAYS_2026 is hardcoded for 2026 only — "
              f"THIS LIST MUST BE UPDATED for {current_year} before trusting "
              f"any scheduling decision below. Holidays are NOT computable "
              f"from a formula; check nyse.com/markets/hours-calendars "
              f"and update the set manually.")


def is_trading_day(d: datetime) -> bool:
    """A real trading day: not a weekend, not a market holiday. Early-
    close days count as trading days (see note above)."""
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if d.strftime("%Y-%m-%d") in US_MARKET_HOLIDAYS_2026:
        return False
    return True


def should_publish_tonight() -> bool:
    """
    The core scheduling gate. Per BMT's exact worked example: a 6pm ET
    run publishes ideas for TOMORROW (the immediately next calendar
    date) — but ONLY if tomorrow is a real trading day. If tomorrow is a
    weekend day or a holiday, tonight's run is skipped entirely — it
    does NOT look further ahead to find some later trading day early.

    Confirmed against BMT's holiday example: Thu Jul 2 evening's next
    calendar day is Fri Jul 3 (a real holiday) -> skip. Fri Jul 3 and
    Sat Jul 4 evenings also skip (next day is a weekend day each time).
    Sun Jul 5 evening's next calendar day is Mon Jul 6 (a real trading
    day) -> publish, resuming the normal cadence.
    """
    _check_holiday_list_freshness()
    today = datetime.now(ET)
    tomorrow = today + timedelta(days=1)
    return is_trading_day(tomorrow)


def get_next_actual_trading_day() -> datetime:
    """
    TESTING ONLY — used only when FORCE_PUBLISH=1 bypasses the normal
    scheduling gate. Unlike get_target_trading_day() (which is always
    exactly 'tomorrow' in real production use, per BMT's spec), this
    walks FORWARD as many days as needed to find the next real trading
    day — so a forced test run on a weekend produces a meaningful card
    (e.g. targeting the next Monday) instead of a nonsensical one
    targeting a Saturday or Sunday.
    """
    d = datetime.now(ET) + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def get_target_trading_day() -> datetime:
    """
    The trading day these ideas are FOR — tomorrow, when
    should_publish_tonight() is True (this should only ever be called
    after that check passes). This is what the card's HEADLINE shows
    (e.g. "Monday, July 20"), distinct from the DATA date the ideas were
    computed from (e.g. "Based on 7/17 close") — confirmed these are two
    different dates on BMT's real sample card, previously conflated into
    one in this script.
    """
    return datetime.now(ET) + timedelta(days=1)


def get_last_completed_trading_day() -> datetime:
    """
    The most recent trading day with fully settled data as of THIS
    run (walks backward from today, inclusive, through weekends AND
    holidays). If today itself is a trading day, today's close counts
    (a 6pm ET run happens after today's 4pm close). If today is a
    weekend/holiday, walks back to the most recent real trading day —
    this is what correctly handles BMT's July 3 holiday example: a Sun
    Jul 5 run walks back through Sat Jul 4, Fri Jul 3 (holiday), landing
    correctly on Thu Jul 2 as the data source, not Friday.
    """
    d = datetime.now(ET)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def main():
    et_now = datetime.now(ET)
    print(f"[{et_now.isoformat()}] BMT Nightly Setups")

    # ── Scheduling gate — MUST run first, before any expensive work ────
    # Per BMT's exact spec: a 6pm ET run publishes ideas for TOMORROW,
    # but only if tomorrow is a real trading day (not a weekend, not a
    # market holiday). This check happens before any flow pulls,
    # OpenRouter calls, or Discord posts — a skip night costs nothing.
    #
    # TESTING OVERRIDE: set FORCE_PUBLISH=1 to bypass this gate for an
    # immediate test run (e.g. testing on a weekend, without waiting for
    # the next real trading-eligible evening). NEVER set this in
    # production — it exists only so changes can be verified end-to-end
    # without waiting up to several days for a normal scheduling window.
    # Always loudly logged so a forced run is never mistaken for a real
    # scheduled one.
    force_publish = os.environ.get("FORCE_PUBLISH") == "1"
    if force_publish:
        print("  [FORCE_PUBLISH=1 — scheduling gate BYPASSED for testing. "
              "This must NEVER happen on a real production run.]")
        target_date = get_next_actual_trading_day()
    elif not should_publish_tonight():
        tomorrow = et_now + timedelta(days=1)
        print(f"  Tomorrow ({tomorrow.strftime('%A, %B %d')}) is not a trading day "
              f"(weekend or market holiday) — skipping tonight's run entirely. No data pulled, no post made.")
        return
    else:
        target_date = get_target_trading_day()

    data_date = get_last_completed_trading_day()
    print(f"  Publishing ideas for {target_date.strftime('%A, %B %d')}, "
          f"using data as of {data_date.strftime('%A, %B %d')} close.\n")

    print(f"Universe: {len(CANDIDATE_UNIVERSE)} candidate tickers (ETFs/leveraged/crypto excluded)")

    # ── Upcoming-earnings map (NEW 2026-07-21 — see comment at the
    # get_upcoming_earnings_map() definition for the full rationale) ──
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

    print("Pulling daily OHLC + next expiry for qualifying candidates...")
    candidates = []
    rejected_summary = []
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

    # ── DETERMINISTIC chart-pattern filter + ranking (NEW 2026-07-18) ──
    # This is the actual fix for the non-determinism confirmed in
    # production: chart-quality accept/reject and ranking now happen
    # entirely in Python via check_chart_pattern() (real swing-point
    # analysis), BEFORE Grok is ever called. Grok's only remaining job is
    # writing narrative prose for whatever Python has already selected.
    print(f"\nApplying deterministic chart-pattern filter to {len(candidates)} candidate(s)...")
    pattern_matched = []
    for c in candidates:
        pattern = check_chart_pattern(c["flow"]["bias"], c["bars"])
        if pattern["clean"]:
            c["direction"] = pattern["direction"]
            c["pattern"] = pattern["pattern"]
            pattern_matched.append(c)
            print(f"  [PATTERN OK] {c['ticker']}: {pattern['direction']} — {pattern['pattern']}")
        else:
            reason = (f"{c['flow']['bias']} flow but no clean structural pattern"
                      if c["flow"]["bias"] != "Neutral" else "neutral flow, no clear direction")
            rejected_summary.append(f"{c['ticker']} ({reason})")

    # ── RANKING BY FLOW INTENSITY (CHANGED 2026-07-21) ─────────────────
    # Was: rank by raw flow premium. Confirmed bias in production: raw
    # premium structurally favors the same mega-cap names night after
    # night (a $9M AVGO flow always outranks a $2M flow on a mid-cap,
    # even when the mid-cap flow is far more unusual relative to how
    # that name normally trades). Fixed: rank by flow INTENSITY —
    # premium normalized by the ticker's own average daily dollar volume
    # (computed from the same daily bars already fetched, zero extra
    # network calls). A $2M flow on a name that trades $200M/day (1.0%
    # intensity) now correctly outranks a $9M flow on a name trading
    # $9B/day (0.1%). Still 100% deterministic. Raw premium remains the
    # tiebreaker, and any ticker whose volume data is missing falls back
    # to intensity 0 with a loud log (never silently mis-ranked).
    for c in pattern_matched:
        if c["avg_dollar_vol"] > 0:
            c["flow_intensity"] = c["flow"]["premium"] / c["avg_dollar_vol"]
        else:
            c["flow_intensity"] = 0.0
            print(f"  [RANK WARN] {c['ticker']}: no volume data — flow intensity set to 0, "
                  f"will rank below all tickers with real volume data")
    pattern_matched.sort(key=lambda c: (c["flow_intensity"], c["flow"]["premium"]), reverse=True)

    # ── EARNINGS EXCLUSION, applied in rank order (REVISED 2026-07-21) ─
    # Walk the ranked list top-down, checking each ticker's upcoming
    # earnings date (yfinance primary, Finnhub map secondary — see the
    # source comment block above get_upcoming_earnings_map() for the
    # full two-dead-sources history). A ticker reporting ON OR BEFORE
    # its expiry is an earnings play, not a swing setup — the chart
    # structure won't survive the print and the stop is meaningless
    # through a gap. Blocked tickers are skipped (and named in the
    # rejected list) and the next-ranked ticker slides in, until TOP_N
    # clean setups are selected. Checking in rank order keeps this to
    # ~5-10 yfinance calls per night instead of ~100+ (that endpoint
    # has a confirmed real rate limit under heavy same-session use).
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
                src = "yfinance" if er_date == yf_er else "Finnhub"
                print(f"  [ER EXCLUDE] {c['ticker']}: reports earnings {er_date} (per {src}, "
                      f"expiry {c.get('expiry_iso') or 'unknown'}) — earnings play, not a swing setup")
                rejected_summary.append(
                    f"{c['ticker']} (reports earnings {er_date}, before expiry — excluded as an earnings play)"
                )
                continue
        selected.append(c)

    print(f"\n{len(pattern_matched)} of {len(candidates)} passed the deterministic pattern filter; "
          f"taking top {len(selected)} by flow intensity (premium / avg daily $ volume).")
    for c in pattern_matched[:10]:
        print(f"  [RANK] {c['ticker']}: intensity={c['flow_intensity']*100:.2f}% "
              f"(${c['flow']['premium']:,.0f} premium / ${c['avg_dollar_vol']:,.0f} avg daily $ vol)")

    if not selected:
        print("Nothing passed the deterministic chart-pattern filter tonight — no digest to post.")
        return

    # Compute DETERMINISTIC price levels — strike, entry, stop, both
    # targets — from the real swing high/low in the actual daily bars,
    # never left to Grok's own arithmetic (see compute_trade_levels()).
    for c in selected:
        current_price = get_quote_change(c["ticker"]).get("price")
        if not current_price:
            print(f"  [WARN] {c['ticker']}: no current price — dropping from selected")
            continue
        c["current_price"] = current_price
        c["strike"] = compute_strike(c["direction"], current_price)
        c.update(compute_trade_levels(c["direction"], c["bars"], current_price))
        if c.get("expiry_iso"):
            try:
                exp_dt = datetime.strptime(c["expiry_iso"], "%Y-%m-%d")
                c["dte"] = max((exp_dt - datetime.now(ET).replace(tzinfo=None)).days, 0)
            except Exception:
                c["dte"] = "?"
        else:
            c["dte"] = "?"
    selected = [c for c in selected if "strike" in c]

    print(f"\nSending {len(selected)} pre-selected setup(s) to Grok for narrative only...")
    narrative_result = write_narratives(selected, rejected_summary, market_context)
    market_theme = narrative_result.get("market_theme", "")
    risk_notes = narrative_result.get("risk_notes", "")
    narrative_lookup = {s["ticker"]: s for s in narrative_result.get("setups", [])}

    accepted = []
    for c in selected:
        n = narrative_lookup.get(c["ticker"], {})
        c["quality_tag"] = n.get("quality_tag", c.get("pattern", "").title())
        c["narrative"] = n.get("narrative", f"Real {c['pattern']} pattern with {c['flow']['bias'].lower()} flow alignment.")
        c["flow_note"] = n.get("flow_note", f"${c['flow']['premium']:,.0f} bought OTM/ATM, {c['flow']['call_pct']}% call-weighted")
        c["company_name"] = c.get("company_name", "")
        accepted.append(c)

    print(f"\n=== MARKET THEME ===\n{market_theme}\n")
    print(f"=== RISK NOTES ===\n{risk_notes}\n")
    print(f"=== SELECTED ({len(accepted)}) — deterministic pattern + intensity ranking, Grok wrote narrative only ===")
    for s in accepted:
        print(f"  {s['ticker']} {s['direction']} ${s['strike']} [{s['pattern']}] "
              f"intensity {s['flow_intensity']*100:.2f}% "
              f"entry ${s['entry_low']}-${s['entry_high']} stop ${s['stop']} T1 ${s['target1']} T2 ${s['target2']}")
    print(f"\n=== DETERMINISTICALLY REJECTED ({len(rejected_summary)}) ===")
    for r in rejected_summary:
        print(f"  {r}")

    out_path = "bmt_nightly_setups.png"
    render_card(accepted, rejected_summary, market_theme, risk_notes, market_context, target_date, data_date, out_path)
    print(f"\nCard saved to {out_path}")

    posted = post_image_to_discord(out_path, message="**Top Trade Ideas**")
    print("Posted to Discord!" if posted else "Discord post FAILED — check webhook.")


run_nightly_job = main  # explicit alias — this function is called by both
                         # the APScheduler job below AND the FORCE_PUBLISH
                         # one-shot testing path


def start_scheduler():
    """
    Persistent-service entry point for Railway deployment. Uses the SAME
    pattern as BMT's other Railway jobs (e.g. bmt_alerts_engagement.py) —
    APScheduler with timezone="America/New_York", which auto-adjusts for
    daylight saving. This is deliberately NOT Railway's own raw cron
    feature, which is fixed-UTC and would silently drift by an hour at
    every DST change unless manually corrected twice a year.

    Fires EVERY day at 6:00pm ET, with no day-of-week restriction baked
    into the scheduler itself — should_publish_tonight() (checked first
    thing inside run_nightly_job()) already fully handles weekends and
    the 2026 holiday calendar, so the scheduler doesn't need to duplicate
    that logic. A skip night costs one no-op function call, nothing more.
    """
    scheduler = BackgroundScheduler(timezone="America/New_York")
    scheduler.add_job(
        run_nightly_job, "cron",
        hour=18, minute=0,
        id="nightly_setups", replace_existing=True, max_instances=1,
    )
    scheduler.start()
    print("Scheduler started: nightly setups job fires daily at 6:00pm ET "
          "(should_publish_tonight() internally skips weekends/holidays).")

    def heartbeat():
        while True:
            time.sleep(900)
            print(f"[HEARTBEAT] scheduler running={scheduler.running}")

    threading.Thread(target=heartbeat, daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    if os.environ.get("FORCE_PUBLISH") == "1":
        # One-shot local testing path — run once immediately and exit,
        # same as every FORCE_PUBLISH test run so far in this project.
        run_nightly_job()
    else:
        # Real Railway deployment path — persistent service, self-
        # scheduling, runs indefinitely.
        start_scheduler()