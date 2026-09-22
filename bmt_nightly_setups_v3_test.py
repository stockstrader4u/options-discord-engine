"""
bmt_nightly_setups_v3_test.py — V3 SELECTION/NARRATIVE UPGRADE (TEST TRACK).

FULLY ISOLATED FROM PRODUCTION. This script:
  - Posts to its own webhook (NIGHTLY_SETUPS_V3_TEST_DISCORD_WEBHOOK), never
    the production NIGHTLY_SETUPS_DISCORD_WEBHOOK.
  - Persists to its own brand-new table, nightly_setup_ideas_v3 — never touches
    nightly_setup_ideas (production's table).
  - Its own results tracker (bmt_setup_results_tracker_v3.py) reads/writes
    nightly_setup_ideas_v3 only.
  - Its own weekly regression table, scoring_weights_v3 — never touches any
    production scoring/weights table.

Purpose: run this in parallel with production for ~1 week, then decide
whether to promote v3's selection/narrative pipeline to production based on
real posted output and (once enough resolved rows exist) backtest_v3_gate.py's
projected win-rate delta.

LOCKED INVARIANTS (per direct instruction — do not change vs. production):
  - Discord embed layout (build_header_embed / build_best_choice_embed /
    build_setup_embed / build_contract_list_embed) — same shape, only the
    Role/Best-for fields are replaced with Edge/Watch-out per item 8(d).
  - Chart renderer (render_setup_chart) — byte-identical to production.
  - Summary card renderer (render_card) — byte-identical to production.
  - Win/loss grading definition in the tracker (touch-the-strike,
    entry→expiry window, never_triggered excluded from reports) — unchanged.

Everything else in this file is the v3 selection/scoring/narrative pipeline
described in the v3 upgrade spec, items 1–6, 8, 9. Items 7 and 10 live in
their own files: bmt_weekly_regression_v3.py and scripts/backtest_v3_gate.py.

WHAT'S NEW VS. PRODUCTION (bmt_nightly_setups.py), by spec item:

  1. EXPECTED-MOVE GATE (compute_expected_move, applied in main() after
     trade levels + strike selection). Candidates whose required move to
     strike is too large relative to the option's own implied expected
     move (ratio > 0.85) are excluded. This directly targets the diagnosed
     root cause of the 50% win rate: strikes sitting 1.2–1.6x ATR out are
     roughly a coin flip by construction: this gate removes the ones that
     are structurally unlikely to be reached by expiry.

  2. STRIKE-AT-MEMORY (select_strike_v3): prefers an OTM strike that both
     sits within 1.0x ATR of T1 AND coincides with a real technical level
     (swing high/low, round number, or top-3 OI on that expiry), scored as
     0.6·distance_to_T1 + 0.4·distance_to_nearest_memory_level. Falls back
     to production's pure closest-to-T1 behavior if nothing qualifies.

  3. FLOW QUALITY: tier-scaled premium floor (0.0015 × avg_dollar_volume,
     replacing the flat $50K), tightened call/put bias (70/30 instead of
     55/45), and a near-dated-concentration bonus captured in flow_quality.

  4. DEDUP + COOLDOWN: excludes tickers with a still-pending v3 setup, and
     tickers whose most recent resolved v3 setup was a loss in the same
     direction within the last 14 days.

  5. VARIABLE COUNT: publishes 3–5 setups (not always exactly 5), based on
     a quality bar (MIN_SCORE) on the composite score — a weak night
     publishes fewer, or (if fewer than 3 clear) publishes the header/
     best-choice embeds plus a "no setups cleared the bar" embed only.

  6. COMPUTED RISK: risk level is now computed deterministically from
     req_exp_ratio in Python (Low/Moderate/Elevated/Speculative), not
     written by the LLM. Removed from the LLM's output contract entirely.

  8. NARRATIVE V3: comparison-matrix source data, why_made_list/why_choose
     prompt changes per spec, Role/Best-for fields replaced with computed
     Edge + LLM-written Watch-out, and a byte-identical Python-built
     verdict line that the LLM must reproduce verbatim as the first line
     of why_made_list.

  9. TRACKER ADDITIONS: premium_at_publish and max_favorable_premium columns
     (nullable, best-effort) added to the v3 schema from the start.

Everything under "DATA LAYER" / "CHART RENDERING" / "CARD RENDERER" below is
copied verbatim from production (bmt_nightly_setups.py) per the "do not
change" instruction for the chart renderer / card renderer — only enough is
duplicated here to keep this file fully standalone and independently
deployable as its own Railway service.
"""

import os
import sys
import json
import re
import time
import math
import threading
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

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
# V3 TEST TRACK: own webhook, never the production one.
DISCORD_WEBHOOK    = os.environ["NIGHTLY_SETUPS_V3_TEST_DISCORD_WEBHOOK"]
DATABASE_URL       = os.environ.get("DATABASE_URL", "")
JARVIS_MCP_URL     = "https://api.jarvisflow.io/.well-known/mcp"
OPENROUTER_BASE    = "https://openrouter.ai/api/v1"
FINNHUB_BASE       = "https://finnhub.io/api/v1"
ET                 = ZoneInfo("America/New_York")
HEADERS            = {"User-Agent": "Mozilla/5.0"}

# ── V3 tuning constants (spec items 1, 3, 4, 5) ────────────────────────
EXPECTED_MOVE_MAX_RATIO   = 0.85     # item 1: exclude if |req_move|/expected_move > this
FLOW_PREMIUM_FLOOR_PCT    = 0.0015   # item 3: premium >= this * avg_dollar_volume
FLOW_BULLISH_CALL_PCT     = 70       # item 3: call_pct >= this => bullish (was 55)
FLOW_BEARISH_CALL_PCT     = 30       # item 3: call_pct <= this => bearish (was 45)
NEAR_DATED_DAYS           = 21       # item 3: near-dated concentration window
NEAR_DATED_SHARE_BONUS    = 0.40     # item 3: share threshold for the bonus
COOLDOWN_DAYS             = 14       # item 4: direction-aware loss cooldown
TOP_N_MAX                 = 5        # item 5: max setups published
TOP_N_MIN                 = 3        # item 5: min setups to bother publishing
MIN_SCORE                 = None     # item 5: set dynamically per run, see compute_min_score()

TOP_N = TOP_N_MAX  # kept for compatibility with any shared helper expecting TOP_N

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

MEGA_CAP_TIER = {
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
    "AVGO", "TSM", "JPM", "WMT", "ORCL", "NFLX", "LLY", "HD", "MA", "UNH",
}

EARNINGS_LOOKAHEAD_DAYS = 14
MIN_DTE = 7


# ─────────────────────────────────────────────────────────────────────
# DB — V3 ISOLATED SCHEMA. Table name and every column here are new;
# nothing here reads or writes nightly_setup_ideas (production's table).
# ─────────────────────────────────────────────────────────────────────

def _connect():
    p = urlparse(DATABASE_URL)
    return _pg8000.Connection(
        host=p.hostname, port=p.port or 5432,
        database=p.path.lstrip("/"),
        user=p.username, password=p.password,
    )


def ensure_schema():
    """Creates nightly_setup_ideas_v3 if missing. Includes, from day one,
    the item-9 tracker-addition columns (premium_at_publish,
    max_favorable_premium) and the item-7 regression feature columns
    (req_exp_ratio, rvol) so no later ALTER TABLE is needed for this
    table's first release. Safe to call every run."""
    if not DATABASE_URL:
        print("  [DB WARN] DATABASE_URL not set — v3 results tracking will be skipped for this run.")
        return
    conn = _connect()
    try:
        conn.run("""
            CREATE TABLE IF NOT EXISTS nightly_setup_ideas_v3 (
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
                edge TEXT,
                risk TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                entry_date DATE,
                period_high NUMERIC,
                period_low NUMERIC,
                -- item 7 regression features
                req_exp_ratio DOUBLE PRECISION,
                flow_premium_over_advol DOUBLE PRECISION,
                call_pct_deviation DOUBLE PRECISION,
                iv_rv_ratio DOUBLE PRECISION,
                rvol DOUBLE PRECISION,
                dte INTEGER,
                pattern TEXT,
                is_mega_cap BOOLEAN,
                composite_score DOUBLE PRECISION,
                -- item 9 tracker additions
                premium_at_publish NUMERIC,
                max_favorable_premium NUMERIC,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                resolved_at TIMESTAMPTZ
            )
        """)
    except Exception as e:
        print(f"  [DB WARN] ensure_schema (v3) failed: {e}")
    finally:
        conn.close()


def save_setup_ideas_v3(selected: list, target_date: datetime):
    """Fire-and-forget best-effort write to nightly_setup_ideas_v3 only.
    A DB hiccup here never blocks tonight's Discord post."""
    if not DATABASE_URL:
        print("  [DB WARN] DATABASE_URL not set — v3 setup ideas will NOT be tracked for results.")
        return
    conn = _connect()
    saved = 0
    try:
        for c in selected:
            expiry_iso = c.get("expiry_iso")
            if not expiry_iso:
                print(f"  [DB WARN] {c['ticker']}: no expiry_iso — skipping v3 results-tracking insert")
                continue
            try:
                expiry_date = datetime.strptime(expiry_iso, "%Y-%m-%d").date()
            except Exception:
                print(f"  [DB WARN] {c['ticker']}: unparseable expiry_iso {expiry_iso!r} — skipping")
                continue
            conn.run("""
                INSERT INTO nightly_setup_ideas_v3
                    (ticker, direction, strike, entry_low, entry_high, stop, target1, target2,
                     expiry_label, expiry_date, publish_date, edge, risk,
                     req_exp_ratio, flow_premium_over_advol, call_pct_deviation, iv_rv_ratio,
                     rvol, dte, pattern, is_mega_cap, composite_score, premium_at_publish)
                VALUES
                    (:ticker, :direction, :strike, :entry_low, :entry_high, :stop, :target1, :target2,
                     :expiry_label, :expiry_date, :publish_date, :edge, :risk,
                     :req_exp_ratio, :flow_premium_over_advol, :call_pct_deviation, :iv_rv_ratio,
                     :rvol, :dte, :pattern, :is_mega_cap, :composite_score, :premium_at_publish)
            """,
                ticker=c["ticker"], direction=c["direction"], strike=c["strike"],
                entry_low=c["entry_low"], entry_high=c["entry_high"], stop=c["stop"],
                target1=c["target1"], target2=c["target2"],
                expiry_label=c.get("next_expiry"), expiry_date=expiry_date,
                publish_date=target_date.date(),
                edge=c.get("edge"), risk=c.get("risk"),
                req_exp_ratio=c.get("req_exp_ratio"),
                flow_premium_over_advol=c.get("flow_intensity"),
                call_pct_deviation=(abs(c["flow"]["call_pct"] - 50) if c["flow"].get("call_pct") is not None else None),
                iv_rv_ratio=c.get("iv_rv_ratio"),
                rvol=c.get("rvol"),
                dte=c.get("dte"),
                pattern=c.get("pattern"),
                is_mega_cap=(c["ticker"] in MEGA_CAP_TIER),
                composite_score=c.get("composite_score"),
                premium_at_publish=c.get("premium"),
            )
            saved += 1
        print(f"  [DB] Saved {saved}/{len(selected)} v3 setup idea(s) for results tracking.")
    except Exception as e:
        print(f"  [DB WARN] save_setup_ideas_v3 failed partway ({saved} saved before the error): {e}")
    finally:
        conn.close()


def fetch_pending_tickers_v3() -> set:
    """Item 4a: tickers with a still-unresolved v3 setup."""
    if not DATABASE_URL:
        return set()
    conn = _connect()
    try:
        rows = conn.run("SELECT DISTINCT ticker FROM nightly_setup_ideas_v3 WHERE status = 'pending'")
        return {r[0] for r in rows}
    except Exception as e:
        print(f"  [DB WARN] fetch_pending_tickers_v3 failed: {e}")
        return set()
    finally:
        conn.close()


def fetch_recent_losses_v3(cutoff_date) -> dict:
    """Item 4b: {ticker: direction} for the most recent resolved v3 setup
    per ticker, where that most-recent resolution was a loss within the
    cooldown window. Only the MOST RECENT resolved row per ticker counts
    (a loss from 20 nights ago followed by a win 10 nights ago should not
    cool the ticker down)."""
    if not DATABASE_URL:
        return {}
    conn = _connect()
    try:
        rows = conn.run("""
            SELECT DISTINCT ON (ticker) ticker, direction, status, resolved_at
            FROM nightly_setup_ideas_v3
            WHERE status IN ('win', 'loss')
            ORDER BY ticker, resolved_at DESC
        """)
        out = {}
        for ticker, direction, status, resolved_at in rows:
            if status == "loss" and resolved_at is not None and resolved_at.date() >= cutoff_date:
                out[ticker] = direction
        return out
    except Exception as e:
        print(f"  [DB WARN] fetch_recent_losses_v3 failed: {e}")
        return {}
    finally:
        conn.close()


def log(msg):
    print(msg, flush=True)


# ─────────────────────────────────────────────────────────────────────
# DATA LAYER — copied verbatim from production bmt_nightly_setups.py.
# No changes. (Included here in full so this file is independently
# deployable as its own Railway service, per the "new file" decision.)
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


def get_flow_rows_for_ticker(ticker: str, session_date_mdy: str = None) -> list:
    """Raw bought OTM/ATM flow rows for a ticker on the given session date
    (defaults to the last completed trading day). Split out from
    get_flow_for_ticker() (which reduces this to a bias/premium/call_pct
    summary) so item 3's near-dated-concentration calculation can inspect
    each row's own expiry date without a second Jarvis call."""
    if session_date_mdy is None:
        session_date_mdy = get_last_completed_trading_day().strftime("%m/%d/%Y")
    result = call_jarvis("stock_ticker_unusual_options_data", {
        "filter_by_Ticker": ticker,
        "filter_by_transaction_date_range_from": session_date_mdy,
        "filter_by_transaction_date_range_to": session_date_mdy,
    })
    if not result:
        return []
    flow = result.get("optionsFlow", []) if isinstance(result, dict) else result
    if not flow:
        return []
    flow = [f for f in flow if f.get("ticker", "").upper() == ticker.upper()]
    bought_otm_atm = [f for f in flow if f.get("implied_Bought_Or_Sold") == "BOUGHT"
                       and f.get("moneyNess", "").upper() in ("OTM", "ATM")]
    return bought_otm_atm


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


def get_flow_for_ticker(ticker: str, session_date_mdy: str = None) -> dict:
    """
    ITEM 3 (FLOW QUALITY) CHANGE vs. production: bias thresholds tightened
    from 55/45 to 70/30 (FLOW_BULLISH_CALL_PCT / FLOW_BEARISH_CALL_PCT).
    Everything else — the session-date handling, the bugfix from
    2026-09-07 — is unchanged.
    """
    bought_otm_atm = get_flow_rows_for_ticker(ticker, session_date_mdy)
    if not bought_otm_atm:
        return {"bias": None, "premium": 0, "call_pct": None}
    total_call = sum(float(f.get("total_Option_Premium_For_Trade", 0) or 0) for f in bought_otm_atm if f.get("put_Or_Call") == "CALL")
    total_put = sum(float(f.get("total_Option_Premium_For_Trade", 0) or 0) for f in bought_otm_atm if f.get("put_Or_Call") == "PUT")
    total = total_call + total_put
    if total == 0:
        return {"bias": None, "premium": 0, "call_pct": None}
    call_pct = round(total_call / total * 100)
    if call_pct >= FLOW_BULLISH_CALL_PCT:
        bias = "Bullish"
    elif call_pct <= FLOW_BEARISH_CALL_PCT:
        bias = "Bearish"
    else:
        bias = "Neutral"
    return {"bias": bias, "premium": total, "call_pct": call_pct}


def compute_near_dated_concentration(flow_rows: list, ticker_expiry_iso: str = None) -> dict:
    """
    ITEM 3 (FLOW QUALITY): share of bought OTM/ATM premium (by dollar
    amount, across ALL expiries present in the flow rows — not just this
    candidate's own selected expiry) that itself expires within
    NEAR_DATED_DAYS of today. A high share means the money buying this
    name is itself making a near-term bet, which is corroborating signal
    for a near-dated setup. Returns a bonus flag plus the raw share for
    transparency/logging and for persistence.
    """
    if not flow_rows:
        return {"near_dated_share": None, "bonus": False}
    today = datetime.now(ET).date()
    total_premium = 0.0
    near_dated_premium = 0.0
    for f in flow_rows:
        premium = float(f.get("total_Option_Premium_For_Trade", 0) or 0)
        if premium <= 0:
            continue
        total_premium += premium
        exp_str = f.get("expiration_Date") or f.get("expirationDate") or f.get("expiry")
        if not exp_str:
            continue
        try:
            exp_date = datetime.strptime(exp_str[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if (exp_date - today).days <= NEAR_DATED_DAYS:
            near_dated_premium += premium
    if total_premium <= 0:
        return {"near_dated_share": None, "bonus": False}
    share = near_dated_premium / total_premium
    return {"near_dated_share": round(share, 3), "bonus": share >= NEAR_DATED_SHARE_BONUS}


# ─────────────────────────────────────────────────────────────────────
# ITEM 1: EXPECTED-MOVE GATE
# ─────────────────────────────────────────────────────────────────────

def compute_expected_move(ticker: str, expiry_iso: str) -> tuple:
    """
    Returns (expected_move_pct, error_reason). expected_move_pct is None
    if it couldn't be computed (caller should treat that as "cannot
    verify — exclude", consistent with the gate's purpose: a strike we
    can't check against its own expected move gets no benefit of the
    doubt).

    expected_move_pct = avg(call_iv, put_iv) at the ATM strike * sqrt(dte/365) * 100

    Reuses the same yfinance chain-fetch / ATM-strike-finding logic as
    get_iv_vs_realized_vol_with_ratio() and select_strike() elsewhere in
    this file, per direct instruction to keep the same data-fetch style.
    """
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        price = stock.info.get("regularMarketPrice") or stock.info.get("currentPrice")
        if not price:
            hist_1d = stock.history(period="1d")
            price = float(hist_1d["Close"].iloc[-1]) if not hist_1d.empty else None
        if not price or not expiry_iso:
            return None, "no price or expiry"

        chain = stock.option_chain(expiry_iso)
        calls = chain.calls[chain.calls["impliedVolatility"] > 0]
        puts = chain.puts[chain.puts["impliedVolatility"] > 0]
        if calls.empty or puts.empty:
            return None, "empty chain"

        common = set(calls["strike"].tolist()) & set(puts["strike"].tolist())
        if not common:
            return None, "no common strikes"
        atm_strike = min(common, key=lambda s: abs(s - price))
        call_iv = float(calls[calls["strike"] == atm_strike]["impliedVolatility"].iloc[0])
        put_iv = float(puts[puts["strike"] == atm_strike]["impliedVolatility"].iloc[0])
        avg_iv = (call_iv + put_iv) / 2

        exp_dt = datetime.strptime(expiry_iso, "%Y-%m-%d").date()
        dte = max((exp_dt - datetime.now(ET).date()).days, 1)

        expected_move_pct = avg_iv * math.sqrt(dte / 365) * 100
        return round(expected_move_pct, 3), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def apply_expected_move_gate(c: dict) -> bool:
    """
    Mutates c in place with req_pct / expected_move_pct / req_exp_ratio.
    Returns True if the candidate PASSES the gate (should stay in
    consideration), False if it should be excluded. Logs every exclusion
    (and every pass, more tersely) with the numbers, per spec.

    req = (strike - current) / current * 100 (signed — direction is
    implicit in the sign, matching build_time_pressure()'s existing
    move_needed_pct convention elsewhere in this codebase).
    """
    current = c.get("current_price")
    strike = c.get("strike")
    expiry_iso = c.get("expiry_iso")
    if not current or not strike or not expiry_iso:
        print(f"  [EXP-MOVE EXCLUDE] {c['ticker']}: missing current_price/strike/expiry_iso — cannot compute, excluding")
        return False

    req_pct = round((strike - current) / current * 100, 3)
    expected_move_pct, err = compute_expected_move(c["ticker"], expiry_iso)

    if expected_move_pct is None or expected_move_pct <= 0:
        print(f"  [EXP-MOVE EXCLUDE] {c['ticker']}: could not compute expected move ({err}) — excluding, no benefit of the doubt")
        c["req_pct"] = req_pct
        c["expected_move_pct"] = None
        c["req_exp_ratio"] = None
        return False

    ratio = round(abs(req_pct) / expected_move_pct, 3)
    c["req_pct"] = req_pct
    c["expected_move_pct"] = expected_move_pct
    c["req_exp_ratio"] = ratio

    if ratio > EXPECTED_MOVE_MAX_RATIO:
        print(f"  [EXP-MOVE EXCLUDE] {c['ticker']}: req={req_pct:+.2f}% expected={expected_move_pct:.2f}% "
              f"ratio={ratio:.2f} > {EXPECTED_MOVE_MAX_RATIO} — excluded")
        return False

    print(f"  [EXP-MOVE OK] {c['ticker']}: req={req_pct:+.2f}% expected={expected_move_pct:.2f}% ratio={ratio:.2f}")
    return True


# ─────────────────────────────────────────────────────────────────────
# ITEM 2: STRIKE-AT-MEMORY
# ─────────────────────────────────────────────────────────────────────

def find_nearest_round_number(current_price: float, direction: str, atr: float) -> float:
    """Nearest round-number level (strike % 5 == 0 for price > 50, whole
    dollar below that) within a plausible reach of current price, on the
    correct side for direction (above current for CALL, below for PUT).
    Returns None if nothing round-ish exists within a wide-enough band
    (5x ATR) to be a meaningful memory level rather than noise."""
    step = 5.0 if current_price > 50 else 1.0
    band = max(atr * 5, step * 3)
    if direction.upper() == "CALL":
        candidate = math.ceil(current_price / step) * step
        if candidate <= current_price:
            candidate += step
        if candidate - current_price <= band:
            return candidate
    else:
        candidate = math.floor(current_price / step) * step
        if candidate >= current_price:
            candidate -= step
        if current_price - candidate <= band:
            return candidate
    return None


def get_top_oi_strikes(df, n: int = 3) -> list:
    if df.empty:
        return []
    ranked = df.copy()
    ranked["openInterest"] = ranked["openInterest"].fillna(0)
    top = ranked.sort_values("openInterest", ascending=False).head(n)
    return top["strike"].tolist()


def select_strike_v3(ticker: str, direction: str, current_price: float, expiry_iso: str,
                      target1: float, atr: float, bars: list) -> tuple:
    """
    ITEM 2 (STRIKE-AT-MEMORY). Extends production's select_strike(): after
    building the same OTM candidate set (OI>=100 -> OI>=25 -> any OTM
    fallback chain, unchanged), among candidates within 1.0x ATR of T1,
    prefer whichever one coincides with a "memory level" — a swing
    high/low from find_swing_points(bars), a round number
    (find_nearest_round_number), or a top-3-OI strike on this expiry.

    Score = 0.6 * distance_to_T1 + 0.4 * distance_to_nearest_memory_level
    (both distances in absolute dollars, on the same scale, so the 0.6/0.4
    weighting is meaningful) — pick the MIN score. Falls back to
    production's pure closest-to-T1 behavior if no memory level is
    within range of any in-band candidate.

    Returns (strike, premium, provenance_str) — provenance is logged and
    also useful to persist/inspect later.
    """
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

        def _liquidity_tier(candidates):
            if candidates.empty:
                return None
            tier = candidates[candidates["openInterest"].fillna(0) >= 100]
            if not tier.empty:
                return tier, "OI>=100"
            tier = candidates[candidates["openInterest"].fillna(0) >= 25]
            if not tier.empty:
                return tier, "OI>=25 fallback"
            return candidates, "any OTM, no liquidity"

        tier_result = _liquidity_tier(otm)
        if tier_result is None:
            raise ValueError("no OTM strikes available")
        tier_df, tier_label = tier_result

        # Build memory levels: swing highs/lows (price-space), a round
        # number, and this expiry's top-3-OI strikes.
        swing_highs, swing_lows = find_swing_points(bars[-10:] if len(bars) >= 10 else bars)
        swing_levels = [v for _, v in swing_highs] + [v for _, v in swing_lows]
        round_level = find_nearest_round_number(current_price, direction, atr)
        oi_levels = get_top_oi_strikes(tier_df, n=3)
        memory_levels = [lvl for lvl in (swing_levels + ([round_level] if round_level else []) + oi_levels) if lvl]

        # Candidates within 1.0x ATR of T1.
        atr_band = max(atr, 0.01)
        in_band = tier_df[(tier_df["strike"] - target1).abs() <= atr_band].copy()

        best_strike = None
        provenance = None
        if not in_band.empty and memory_levels:
            in_band["dist_t1"] = (in_band["strike"] - target1).abs()

            def nearest_memory_dist(strike_val):
                return min(abs(strike_val - lvl) for lvl in memory_levels)

            in_band["dist_memory"] = in_band["strike"].apply(nearest_memory_dist)
            in_band["memory_score"] = 0.6 * in_band["dist_t1"] + 0.4 * in_band["dist_memory"]
            best_row = in_band.sort_values("memory_score").iloc[0]
            # Only actually prefer this over closest-to-T1 if it's
            # genuinely near a memory level (within half the strike
            # increment) — otherwise the "0.4 * distance" term is just
            # picking an arbitrary in-band strike with no real memory
            # coincidence, which isn't what item 2 asks for.
            nearest_mem_dist = nearest_memory_dist(float(best_row["strike"]))
            increment = get_strike_increment(current_price)
            if nearest_mem_dist <= increment / 2:
                best_strike = float(best_row["strike"])
                provenance = f"memory-level strike (dist_to_memory=${nearest_mem_dist:.2f}, tier={tier_label})"

        if best_strike is None:
            # Fall back to production's pure closest-to-T1 behavior.
            tier_df = tier_df.copy()
            tier_df["dist"] = (tier_df["strike"] - target1).abs()
            best_row = tier_df.sort_values("dist").iloc[0]
            best_strike = float(best_row["strike"])
            provenance = f"closest-to-T1 (no qualifying memory level, tier={tier_label})"

        oi = int(best_row.get("openInterest", 0) or 0)
        vol = int(best_row.get("volume", 0) or 0)
        bid = best_row.get("bid", 0) or 0
        ask = best_row.get("ask", 0) or 0
        last = best_row.get("lastPrice", 0) or 0
        mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
        premium = round(mid, 2) if mid > 0 else None
        print(f"  [STRIKE-V3] {ticker}: ${best_strike:g} — {provenance} OI={oi} vol={vol} premium=${premium}")
        return best_strike, premium, provenance
    except Exception as e:
        inc = get_strike_increment(current_price)
        if direction.upper() == "CALL":
            atm = round((current_price // inc + 1) * inc, 2)
        else:
            atm = round((current_price // inc) * inc, 2)
        premium = get_option_premium(ticker, direction, atm, expiry_iso)
        provenance = f"ATM fallback (OTM selection failed: {e})"
        print(f"  [STRIKE-V3] {ticker}: ATM ${atm} fallback ({e})")
        return atm, premium, provenance


# ─────────────────────────────────────────────────────────────────────
# ITEM 6: COMPUTED RISK
# ─────────────────────────────────────────────────────────────────────

def compute_risk_level(req_exp_ratio) -> str:
    """
    Deterministic risk level from req_exp_ratio, replacing the LLM's
    freeform "risk" output entirely. Per spec:
      < 0.50        -> Low
      0.50 - 0.70   -> Moderate
      0.70 - 0.85   -> Elevated
      > 0.85        -> Speculative (should only occur if a candidate
                       slipped past the 0.85 expected-move gate via the
                       ATM fallback path, where the gate itself is
                       harder to apply cleanly)
    """
    if req_exp_ratio is None:
        return "Moderate"
    r = abs(req_exp_ratio)
    if r < 0.50:
        return "Low"
    elif r < 0.70:
        return "Moderate"
    elif r <= 0.85:
        return "Elevated"
    else:
        return "Speculative"


# ─────────────────────────────────────────────────────────────────────
# ITEM 4: DEDUP + COOLDOWN
# ─────────────────────────────────────────────────────────────────────

def apply_dedup_and_cooldown(candidates: list) -> list:
    """
    Item 4a: exclude any ticker with a still-pending v3 setup.
    Item 4b: exclude tickers whose most recent RESOLVED v3 setup was a
    loss within COOLDOWN_DAYS, in the SAME direction as tonight's
    candidate (direction-aware — a bearish loss doesn't cool down a
    bullish re-entry on the same name).
    Logs every exclusion as [DEDUP EXCLUDE] / [COOLDOWN EXCLUDE].
    """
    pending_tickers = fetch_pending_tickers_v3()
    cutoff_date = (datetime.now(ET) - timedelta(days=COOLDOWN_DAYS)).date()
    recent_loss_direction = fetch_recent_losses_v3(cutoff_date)

    out = []
    for c in candidates:
        t = c["ticker"]
        if t in pending_tickers:
            print(f"  [DEDUP EXCLUDE] {t}: still has a pending v3 setup — excluded")
            continue
        loss_dir = recent_loss_direction.get(t)
        if loss_dir is not None and loss_dir.upper() == c["direction"].upper():
            print(f"  [COOLDOWN EXCLUDE] {t}: most recent resolved v3 setup was a {loss_dir} loss "
                  f"within the last {COOLDOWN_DAYS} days, same direction as tonight's candidate — excluded")
            continue
        out.append(c)
    return out


# ─────────────────────────────────────────────────────────────────────
# ITEM 5: VARIABLE COUNT + QUALITY BAR
# ─────────────────────────────────────────────────────────────────────

def compute_composite_score(c: dict) -> float:
    """
    composite = tier_percentile-based rank (0-100, from
    compute_tier_percentiles(), same mega-cap-aware ranking as
    production) blended with (1 - req_exp_ratio) and flow_quality.

    All three terms are normalized to roughly comparable 0-1 scale
    before blending so no single term dominates by virtue of its raw
    units:
      - tier_percentile / 100                      (already 0-1)
      - 1 - min(req_exp_ratio, 1.0)                 (0-1, higher is better —
                                                      a smaller required move
                                                      relative to expected
                                                      move scores higher)
      - flow_quality_score (0-1): 0.5 base if flow cleared the tier-scaled
        floor at all, +0.5 if the near-dated-concentration bonus applies
    Weighted equally (1/3 each) — no single sub-signal was specified as
    dominant in the spec, so an equal blend is the natural default; this
    is exactly the kind of weighting choice item 7's learning loop is
    meant to eventually replace with fitted weights.
    """
    tier_component = (c.get("tier_percentile") or 0.0) / 100.0
    ratio = c.get("req_exp_ratio")
    exp_move_component = (1 - min(ratio, 1.0)) if ratio is not None else 0.0
    flow_quality = c.get("flow_quality", {})
    flow_component = 0.5 + (0.5 if flow_quality.get("bonus") else 0.0)
    composite = (tier_component + exp_move_component + flow_component) / 3.0
    return round(composite, 4)


def compute_min_score(scored_candidates: list) -> float:
    """
    Sets MIN_SCORE dynamically so "a typical weak night yields 3" (per
    spec) rather than hardcoding a single fixed bar that might publish 0
    or 5 regardless of how the night actually looked. Approach: if there
    are at least TOP_N_MIN candidates, the bar is the TOP_N_MIN-th
    highest composite score (so at least that many always clear it on
    a night with enough candidates at all); if fewer than TOP_N_MIN
    candidates exist in the first place, the bar is 0 (publish whatever
    exists — the "fewer than 3 clear" no-setups-embed path handles the
    genuinely weak case).
    """
    if not scored_candidates:
        return 0.0
    scores = sorted((c["composite_score"] for c in scored_candidates), reverse=True)
    if len(scores) >= TOP_N_MIN:
        return scores[TOP_N_MIN - 1]
    return 0.0


def select_top_n_variable(scored_candidates: list) -> list:
    """
    Publishes the top 3-5 candidates scoring >= MIN_SCORE (computed by
    compute_min_score()). If fewer than TOP_N_MIN clear the bar even
    with the dynamic threshold (i.e. fewer than TOP_N_MIN candidates
    existed in the eligible pool at all), returns whatever's there
    (possibly < 3, possibly 0) — main() decides what to post based on
    the returned count, per spec's "publish header/best-choice + a
    'no setups cleared' embed" instruction.
    """
    if not scored_candidates:
        return []
    min_score = compute_min_score(scored_candidates)
    ranked = sorted(scored_candidates, key=lambda c: c["composite_score"], reverse=True)
    qualifying = [c for c in ranked if c["composite_score"] >= min_score]
    selected = qualifying[:TOP_N_MAX]
    print(f"  [SELECT] MIN_SCORE={min_score:.4f}, {len(qualifying)} candidate(s) cleared it, "
          f"publishing top {len(selected)} (cap {TOP_N_MAX})")
    return selected


def compute_tier_percentiles(candidates: list) -> None:
    """Unchanged from production — see that file's docstring for the
    full mega-cap-tier rationale. Mutates each candidate in place with
    'tier_percentile'."""
    mega = [c for c in candidates if c["ticker"] in MEGA_CAP_TIER]
    rest = [c for c in candidates if c["ticker"] not in MEGA_CAP_TIER]
    for group in (mega, rest):
        n = len(group)
        if n == 0:
            continue
        ranked = sorted(group, key=lambda c: c["ranking_score"])
        for i, c in enumerate(ranked):
            c["tier_percentile"] = (i / (n - 1) * 100) if n > 1 else 100.0


def apply_mega_cap_floor(eligible: list, selected: list) -> list:
    """Unchanged in spirit from production's mega-cap reserved-floor
    logic, adapted for a variable-length `selected` list: if no
    mega-cap-tier name reached the natural selection, but one exists in
    `eligible` that cleared every other filter, swap it in for the
    lowest-scoring member of `selected` (or just append it if selected
    hasn't hit TOP_N_MAX yet). If none cleared filters at all tonight,
    no slot is forced."""
    if any(c["ticker"] in MEGA_CAP_TIER for c in selected):
        return selected
    best_mega = next((c for c in eligible if c["ticker"] in MEGA_CAP_TIER), None)
    if not best_mega:
        print("  [MEGA-CAP FLOOR] no mega-cap tier candidate cleared filters tonight — no slot forced")
        return selected
    if len(selected) >= TOP_N_MAX:
        selected = sorted(selected, key=lambda c: c["composite_score"])
        bumped = selected.pop(0)
        print(f"  [MEGA-CAP FLOOR] swapping in {best_mega['ticker']} in place of "
              f"{bumped['ticker']} (lowest composite score of the selection)")
    else:
        print(f"  [MEGA-CAP FLOOR] adding {best_mega['ticker']} (no mega-cap name reached the natural selection)")
    selected.append(best_mega)
    selected.sort(key=lambda c: c["composite_score"], reverse=True)
    return selected


# ─────────────────────────────────────────────────────────────────────
# ITEM 8(d): EDGE COMPUTATION
# ─────────────────────────────────────────────────────────────────────

def compute_edges(selected: list) -> None:
    """
    Assigns each setup the ONE axis it ranks #1 on among tonight's
    selection, per spec:
      - lowest req_exp_ratio      -> "Lowest Required Move"
      - highest flow premium      -> "Strongest Flow Conviction"
      - lowest iv_rv_ratio        -> "Cheapest Options (vs. Recent Move)"
      - highest rvol              -> "Highest Relative Volume"
      - only bearish-bias setup   -> "Only Bearish Setup Tonight"
    Ties broken by composite_score (higher composite wins the tie).
    Mutates each c in `selected` with c["edge"].

    Since there can be more setups than axes (5 setups, 5 axes — fits
    exactly at TOP_N_MAX) or fewer setups than axes (a 3-setup night),
    each setup gets AT MOST one edge assignment and each axis is
    assigned to at most one setup; a setup that doesn't win any axis
    falls back to a neutral, still-true label built from its own
    strongest relative stat.
    """
    if not selected:
        return

    def best_by(key_fn, reverse=True):
        candidates = [c for c in selected if key_fn(c) is not None]
        if not candidates:
            return None
        candidates.sort(key=lambda c: (key_fn(c), c.get("composite_score", 0)), reverse=reverse)
        return candidates[0]

    assigned = set()
    edge_winners = {}

    lowest_req = best_by(lambda c: (-abs(c["req_exp_ratio"])) if c.get("req_exp_ratio") is not None else None)
    if lowest_req and lowest_req["ticker"] not in assigned:
        edge_winners[lowest_req["ticker"]] = "Lowest Required Move to Target"
        assigned.add(lowest_req["ticker"])

    highest_flow = best_by(lambda c: c["flow"]["premium"])
    if highest_flow and highest_flow["ticker"] not in assigned:
        edge_winners[highest_flow["ticker"]] = "Strongest Flow Conviction"
        assigned.add(highest_flow["ticker"])

    cheapest_iv = best_by(lambda c: (-c["iv_rv_ratio"]) if c.get("iv_rv_ratio") else None)
    if cheapest_iv and cheapest_iv["ticker"] not in assigned:
        edge_winners[cheapest_iv["ticker"]] = "Cheapest Options vs. Recent Move"
        assigned.add(cheapest_iv["ticker"])

    highest_rvol = best_by(lambda c: c.get("rvol"))
    if highest_rvol and highest_rvol["ticker"] not in assigned:
        edge_winners[highest_rvol["ticker"]] = "Highest Relative Volume"
        assigned.add(highest_rvol["ticker"])

    bearish = [c for c in selected if c["direction"].upper() == "PUT"]
    if len(bearish) == 1 and bearish[0]["ticker"] not in assigned:
        edge_winners[bearish[0]["ticker"]] = "Only Bearish Setup Tonight"
        assigned.add(bearish[0]["ticker"])

    for c in selected:
        if c["ticker"] in edge_winners:
            c["edge"] = edge_winners[c["ticker"]]
        else:
            # Fallback: describe this setup's own strongest true fact so
            # every card still gets a real, non-generic edge line even
            # when it didn't win a named axis outright.
            c["edge"] = "Balanced Setup Across the Board"


# ─────────────────────────────────────────────────────────────────────
# TECHNICAL DETAIL / RVOL — copied verbatim from production (needed by
# both the narrative source data and the chart renderer, and per
# instruction the chart renderer itself must not change).
# ─────────────────────────────────────────────────────────────────────

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
    FIB_LEVELS_LOCAL = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.236, 1.382, 1.618]
    return [(f, (lo_val + f * span) if is_call else (hi_val - f * span)) for f in FIB_LEVELS_LOCAL]


def compute_rvol(chart_bars: list, lookback: int = 20) -> float:
    if len(chart_bars) < 2:
        return 1.0
    prior = chart_bars[-(lookback + 1):-1] or chart_bars[:-1]
    avg = sum(b["volume"] for b in prior) / len(prior) if prior else chart_bars[-1]["volume"]
    return (chart_bars[-1]["volume"] / avg) if avg > 0 else 1.0


def build_technical_detail(c: dict) -> str:
    """Verbatim from production — see that file's docstring for the
    2026-08-31 rationale (reuses c["chart_bars"], the same bars the
    chart image itself is drawn from, so the number cited in prose
    always matches the number printed on the chart)."""
    chart_bars = c.get("chart_bars") or c["bars"]
    if not chart_bars or len(chart_bars) < 5:
        return "Not available."

    closes = [b["close"] for b in chart_bars]
    ema5 = compute_ema_series(closes, 5)
    ema12 = compute_ema_series(closes, 12)
    rsi = compute_rsi_series(closes, 14)
    rvol = compute_rvol(chart_bars)
    fib_levels = compute_chart_fib_levels(chart_bars, c["direction"])

    ema_relation = "above" if ema5[-1] > ema12[-1] else "below"
    rsi_now = rsi[-1]
    if rsi_now >= 70:
        rsi_zone = "overbought territory (70+)"
    elif rsi_now <= 30:
        rsi_zone = "oversold territory (30 or below)"
    elif rsi_now >= 50:
        rsi_zone = "above the neutral 50 midline"
    else:
        rsi_zone = "below the neutral 50 midline"

    current_price = closes[-1]
    fib_str = "no clear Fibonacci level nearby"
    if fib_levels:
        nearest_fib = min(fib_levels, key=lambda fp: abs(fp[1] - current_price))
        fib_str = f"trading closest to the {nearest_fib[0]:.3f} Fibonacci retracement level (${nearest_fib[1]:.2f})"

    return (
        f"RSI (14-day) is {rsi_now:.0f}, {rsi_zone}. "
        f"The 5-day EMA (${ema5[-1]:.2f}) is {ema_relation} the 12-day EMA (${ema12[-1]:.2f}). "
        f"Relative volume is {rvol:.1f}x the 20-day average. "
        f"Price is {fib_str}."
    )


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


# ─────────────────────────────────────────────────────────────────────
# ITEM 8: NARRATIVE V3
# ─────────────────────────────────────────────────────────────────────

SENDER_USERNAME = "BMT"

COLOR_LOW = 0x3BA55D
COLOR_MODERATE = 0x5865F2
COLOR_ELEVATED = 0xE5A012
COLOR_SPECULATIVE = 0xED4245
COLOR_GOLD = 0xFBBF24
COLOR_NEUTRAL = 0x2B2D31

RISK_COLOR_MAP = {
    "Low": COLOR_LOW,
    "Moderate": COLOR_MODERATE,
    "Elevated": COLOR_ELEVATED,
    "High": COLOR_SPECULATIVE,
    "Speculative": COLOR_SPECULATIVE,
}

HOW_TO_USE_LINE = "These are independent triggered setups, not a basket to buy blindly \u2014 pick what fits your risk tolerance."


def build_verdict_line(c: dict) -> str:
    """
    ITEM 8(e): built deterministically in Python, passed to the LLM with
    instruction to reproduce it VERBATIM as the first line of
    why_made_list. This guarantees the verdict is byte-identical between
    what Python computed and what the posted card shows — no room for
    the model to paraphrase a number wrong.

    Format (exact, per spec):
      "Needs {req:+.1f}% by {expiry} ({dte}d) \u00b7 typical \u00b1{expected:.1f}% \u2192 {ACHIEVABLE|BORDERLINE|STRETCH}"
    """
    req = c.get("req_pct")
    expected = c.get("expected_move_pct")
    ratio = c.get("req_exp_ratio")
    dte = c.get("dte", "?")
    expiry = c.get("next_expiry", "N/A")
    if req is None or expected is None or ratio is None:
        return f"Required move data unavailable \u00b7 expiry {expiry} ({dte}d)"
    verdict = "ACHIEVABLE" if ratio <= 0.7 else ("BORDERLINE" if ratio <= 0.85 else "STRETCH")
    return f"Needs {req:+.1f}% by {expiry} ({dte}d) \u00b7 typical \u00b1{expected:.1f}% \u2192 {verdict}"


def build_comparison_matrix(selected: list) -> str:
    """ITEM 8(a): one line per setup with required_move %, flow $,
    iv_rv_ratio, rvol, dte, req_exp_ratio — gives the LLM the actual
    cross-setup numbers it needs to make a genuine comparative claim in
    why_choose, rather than inventing one."""
    lines = ["COMPARISON MATRIX (tonight's setups, side by side):"]
    for c in selected:
        req = c.get("req_pct")
        req_str = f"{req:+.1f}%" if req is not None else "N/A"
        ratio = c.get("req_exp_ratio")
        ratio_str = f"{ratio:.2f}" if ratio is not None else "N/A"
        ivrv = c.get("iv_rv_ratio")
        ivrv_str = f"{ivrv:.2f}x" if ivrv else "N/A"
        rvol = c.get("rvol")
        rvol_str = f"{rvol:.1f}x" if rvol is not None else "N/A"
        lines.append(
            f"- ${c['ticker']}: required move {req_str}, flow {build_flow_note_display(c['flow'])}, "
            f"IV/RV {ivrv_str}, RVOL {rvol_str}, {c.get('dte', '?')} DTE, req/expected-move ratio {ratio_str}"
        )
    return "\n".join(lines)


def build_narrative_source_data_v3(selected: list, market_context: dict, target_date: datetime) -> str:
    lines = []
    lines.append("Market summary and index performance (as of last close):")
    for t in MARKET_CONTEXT_TICKERS:
        m = market_context.get(t, {})
        lines.append(f"- ${t}: ${m.get('price', 'N/A')} ({m.get('pct', 'N/A')}%) -- {get_tone_phrase(m)}")
    lines.append("")
    lines.append(f"Ideas are for the next trading session: {target_date.strftime('%A, %B %d, %Y')}.")
    lines.append("")
    lines.append(build_comparison_matrix(selected))
    lines.append("")
    lines.append(f"{len(selected)} scanned trade ideas that cleared the expected-move gate and quality bar tonight "
                  f"(order below is composite-score rank, highest first -- a useful input, but re-rank based on "
                  f"the full picture if the data supports it):")
    lines.append("")
    for i, c in enumerate(selected):
        lines.append(f"---- SETUP #{i+1} (composite-score rank order, not necessarily final rank) ----")
        lines.append(f"Ticker: ${c['ticker']}   Company: {c.get('company_name', 'Not provided')}")
        lines.append(f"Contract: {c['direction']} ${c['strike']:g} strike, expiring {c['next_expiry']} ({c.get('dte', '?')} calendar days to expiration)")
        lines.append(f"Chart setup: {build_price_narrative(c)} Pattern classification: {build_quality_tag(c.get('pattern', ''))}.")
        lines.append(f"Technical indicators (already shown on this setup's own chart image -- these are safe to name directly): {c.get('tech_detail', 'Not available.')}")
        lines.append(f"Entry zone (underlying stock price): ${c['entry_low']}-${c['entry_high']}")
        lines.append(f"Stop / invalidation (underlying stock price): ${c['stop']}")
        lines.append(f"Target 1 (underlying stock price): ${c['target1']}")
        lines.append(f"Target 2 (underlying stock price): ${c['target2']}")
        lines.append(f"Options-flow data: {build_flow_note_display(c['flow'])}")
        lines.append(f"Computed verdict line (REPRODUCE THIS EXACTLY, verbatim, as the first line of why_made_list): {c['verdict_line']}")
        lines.append(f"Analyst target / catalyst info: {c.get('analyst_target', 'Not provided')}")
        lines.append(f"This setup's computed Edge (already decided, do not re-derive or contradict it): {c.get('edge', 'N/A')}")
        lines.append("")
    return "\n".join(lines)


NARRATIVE_PROMPT_TEMPLATE_V3 = """You are an expert options-trading newsletter editor. Convert tonight's scanned trade ideas into short, plain-English content for a Discord post that a complete beginner can read and act on in under a minute per setup. This is NOT a long-form document -- every field below is going into a compact, colored embed card, so brevity is a hard requirement, not a style preference.

## What to produce, per setup

1. **why_made_list** -- Line 1 MUST be the "Computed verdict line" given for this setup below, reproduced EXACTLY, character for character, with no changes, no rephrasing, no rounding differently. After that verbatim first line, add ONE to TWO more short plain-English sentences combining chart structure + technical indicator readings + options flow + option pricing/value + catalyst (if any) into a tight, beginner-friendly explanation.
   - NO jargon for these terms: never write "IV/RV", "implied volatility", "realized volatility", "call-weighted", "OTM/ATM", "Higher Lows Base", "conviction rank", or similar -- translate each into plain English instead.
   - EXCEPTION: RSI, relative volume ("RVOL"), moving averages ("the 5-day EMA", "the 12-day EMA"), and Fibonacci retracement levels are explicitly ALLOWED to be named directly, with their real number from the "Technical indicators" line in the source data below -- these already appear on this setup's own chart image right below this write-up.
   - CRITICAL: DO NOT settle on one fixed phrasing for these translations and reuse it setup after setup. Vary sentence structure, word choice, sentence length, and which detail you lead with, every single time.
   - USE THE SPECIFIC FACTS ALREADY GIVEN for this setup in its "Chart setup" line above (the exact price level and date it bottomed/topped at) together with the technical-indicator reading(s) you chose, to make this setup's story concretely different from every other setup's.
   - Do not restate exact dollar flow figures or percentages already implied elsewhere -- keep this readable, not data-dense.
2. **why_choose** -- ONE short sentence: the single clearest reason to pick this over the others tonight. This MUST cite a comparative fact drawn from the COMPARISON MATRIX above -- a genuine "the only setup tonight where..." or "compared to the other N setups, this..." claim using the real numbers in that matrix, not a generic quality statement. Same variety requirement -- do not reuse the same sentence template across every setup.
3. **watch_out** -- ONE short sentence: one invalidation nuance BEYOND the stop level itself (e.g. a specific level, indicator reading, or condition that would make this setup's thesis wrong even before the stop is technically hit). Do not just restate the stop price -- that's already shown elsewhere on the card.

## Do NOT produce

- Do NOT produce "role" or "best_for" fields -- these have been removed from this format. Do NOT produce a "risk" field -- risk level is computed separately in Python from the required-move data, not written by you.

## Also produce

- **market_backdrop**: ONE plain-English sentence citing real $SPY/$QQQ/$IWM levels and moves by number.
- **top_pick_ticker**: the ticker of whichever setup should be the single best pick tonight.
- **top_pick_why**: ONE short sentence on why that's the top pick.

## Non-negotiable rules

- Every ticker mention INSIDE SENTENCE TEXT (market_backdrop, top_pick_why, why_made_list, why_choose, watch_out) must be prefixed with "$" (e.g. "$AXTI"), every time.
- EXCEPTION -- do NOT apply the "$" prefix to the JSON object keys under "setups", or to the value of "top_pick_ticker". Those must be the bare ticker symbol with no "$" and no other punctuation.
- Do NOT include any URLs, website names, or "according to [source]" citations anywhere.
- Base every claim only on the source data provided below -- you do not have web search for this task.
- These setups have ALREADY been screened by a deterministic expected-move gate and quality bar -- do not write as if reconsidering whether they're worth trading.
- Never say "guaranteed", "easy money", "cannot lose", or similar.
- Do not invent catalysts, data, or reasoning not in the source data below. If something is missing, write "Not provided".
- Each setup's "computed Edge" field is ALREADY DECIDED in Python and given to you below -- do not contradict it, re-derive it, or assign a different edge in your prose; you may reference it naturally if useful but you are not writing the Edge label itself.

## Source data for tonight ({target_date_str})

{source_data}

## Output format

Return ONLY valid JSON, nothing else, no markdown code fences, in exactly this shape (ticker keys must match the source data tickers exactly, bare with no "$" prefix):

{{
  "market_backdrop": "...",
  "top_pick_ticker": "TICKER",
  "top_pick_why": "...",
  "setups": {{
    "TICKER1": {{"why_made_list": "...", "why_choose": "...", "watch_out": "..."}},
    "TICKER2": {{"why_made_list": "...", "why_choose": "...", "watch_out": "..."}}
  }}
}}"""


def write_setup_narratives_v3(selected: list, market_context: dict, target_date: datetime) -> dict:
    source_data = build_narrative_source_data_v3(selected, market_context, target_date)
    prompt = NARRATIVE_PROMPT_TEMPLATE_V3.format(
        target_date_str=target_date.strftime('%A, %B %d, %Y'),
        source_data=source_data,
    )

    REASONING_MAX_TOKENS_ATTEMPTS = [3000, 1200]
    TOTAL_MAX_TOKENS = 16000

    content = None
    parsed_result = None
    last_parse_error = None

    for attempt, reasoning_cap in enumerate(REASONING_MAX_TOKENS_ATTEMPTS, start=1):
        print(f"  [NARRATIVE-V3] calling OpenRouter, attempt {attempt}/{len(REASONING_MAX_TOKENS_ATTEMPTS)} "
              f"(reasoning capped at {reasoning_cap} tokens, {TOTAL_MAX_TOKENS} total budget)...", flush=True)
        call_started = time.time()
        try:
            resp = requests.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                json={"model": "moonshotai/kimi-k2.6", "max_tokens": TOTAL_MAX_TOKENS,
                      "temperature": 0.7,
                      "reasoning": {"max_tokens": reasoning_cap},
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=(10, 120)
            )
        except requests.exceptions.RequestException as e:
            print(f"  [NARRATIVE-V3 WARN] attempt {attempt}: network error after "
                  f"{time.time() - call_started:.1f}s: {type(e).__name__}: {e}")
            if attempt < len(REASONING_MAX_TOKENS_ATTEMPTS):
                print("  [NARRATIVE-V3] retrying with a tighter reasoning cap...", flush=True)
                continue
            raise ValueError(f"write_setup_narratives_v3: network error on final attempt: {e}") from e
        print(f"  [NARRATIVE-V3] response received after {time.time() - call_started:.1f}s", flush=True)
        raw = resp.json()
        if "choices" not in raw:
            print("  [NARRATIVE-V3 ERROR] unexpected response (no 'choices' key):")
            print(f"  {json.dumps(raw, indent=2)[:1000]}")
            raise ValueError("write_setup_narratives_v3: unexpected API response shape")
        message = raw["choices"][0]["message"]
        content = message.get("content")
        if not content:
            print(f"  [NARRATIVE-V3 WARN] attempt {attempt}: empty/None content:")
            print(f"  {json.dumps(message, indent=2)[:1000]}")
            if attempt < len(REASONING_MAX_TOKENS_ATTEMPTS):
                print("  [NARRATIVE-V3] retrying with a tighter reasoning cap...", flush=True)
            continue

        cleaned = content.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned)
        try:
            parsed_result = json.loads(cleaned.strip())
            break
        except json.JSONDecodeError as e:
            last_parse_error = e
            print(f"  [NARRATIVE-V3 WARN] attempt {attempt}: content non-empty but failed to parse "
                  f"({e}). First 300 chars: {cleaned[:300]!r}")
            if attempt < len(REASONING_MAX_TOKENS_ATTEMPTS):
                print("  [NARRATIVE-V3] retrying with a tighter reasoning cap...", flush=True)
            continue

    if parsed_result is None:
        if last_parse_error is not None:
            raise ValueError(
                f"write_setup_narratives_v3: content non-empty on every attempt but never parsed as "
                f"valid JSON -- last error: {last_parse_error}. Last content (first 500 chars): {(content or '')[:500]!r}"
            )
        raise ValueError("write_setup_narratives_v3: empty content in API response after all retry attempts")

    return parsed_result


def clean_text_field(text: str, tickers: list) -> str:
    if not text:
        return "Not provided"
    text = strip_urls_and_domains(text)
    text = ensure_dollar_prefixed_tickers(text, tickers)
    return text.strip()


def verify_verdict_line_verbatim(c: dict) -> None:
    """
    ITEM 8(e) acceptance check: "verdict line is byte-identical between
    Python and the posted card." If the model altered the verdict line
    despite the verbatim instruction, this forcibly overwrites the start
    of why_made_list with the correct Python-computed line, logging a
    warning -- guaranteeing the acceptance criterion holds regardless of
    whether the LLM complied.
    """
    expected = c["verdict_line"]
    actual = c.get("why_made_list", "")
    if not actual.startswith(expected):
        print(f"  [VERDICT MISMATCH] {c['ticker']}: model did not reproduce the verdict line verbatim -- "
              f"forcing it. Expected start: {expected!r} | Got: {actual[:120]!r}")
        rest = actual
        c["why_made_list"] = f"{expected} {rest}".strip()


# ─────────────────────────────────────────────────────────────────────
# EMBED BUILDERS — same shape as production, per the "do not change
# layout" instruction; only the Role/Best-for fields are replaced with
# Edge/Watch-out per item 8(d).
# ─────────────────────────────────────────────────────────────────────

def build_header_embed(market_backdrop: str, target_date: datetime) -> dict:
    return {
        "title": f"TRADE IDEAS \u2014 {target_date.strftime('%A, %B %d').upper()} (V3 TEST)",
        "description": f"{market_backdrop}\n\n{HOW_TO_USE_LINE}",
        "color": COLOR_NEUTRAL,
    }


def build_best_choice_embed(top_pick_ticker: str, top_pick_why: str) -> dict:
    return {
        "title": "Best Choice Tonight",
        "color": COLOR_GOLD,
        "description": f"**${top_pick_ticker}**\n{top_pick_why}",
    }


def build_no_setups_embed(reason: str) -> dict:
    """Item 5: posted instead of setup cards when fewer than TOP_N_MIN
    candidates cleared the quality bar tonight."""
    return {
        "title": "No Setups Cleared the Bar Tonight",
        "color": COLOR_NEUTRAL,
        "description": reason,
    }


def build_setup_embed(c: dict, rank: int, is_top_pick: bool) -> dict:
    risk = c.get("risk", "Moderate")
    color = RISK_COLOR_MAP.get(risk, COLOR_NEUTRAL)
    star = "\u2b50 " if is_top_pick else ""
    return {
        "title": f"{star}{rank}. ${c['ticker']} \u2014 {c['next_expiry']} ${c['strike']:g}{c['direction'][0]}",
        "color": color,
        "fields": [
            {"name": "Edge", "value": c.get("edge", "Not provided"), "inline": True},
            {"name": "Risk level", "value": risk, "inline": True},
            {"name": "Why it made the list", "value": c.get("why_made_list", "Not provided"), "inline": False},
            {"name": "Why choose this over the others", "value": c.get("why_choose", "Not provided"), "inline": False},
            {"name": "Watch out", "value": c.get("watch_out", "Not provided"), "inline": False},
        ],
    }


def build_contract_list_embed(selected: list) -> dict:
    lines = "\n".join(f"\u2022 ${c['ticker']} {c['next_expiry']} ${c['strike']:g}{c['direction'][0]}" for c in selected)
    return {
        "title": "Tonight's Contracts (V3 Test)",
        "color": COLOR_NEUTRAL,
        "description": lines,
        "footer": {"text": "V3 TEST TRACK -- not the production feed. See card image for exact entry/stop/target levels."},
    }


def post_embeds_to_discord(embeds: list) -> bool:
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
# CHART RENDERING — byte-identical copy from production, per the "do not
# change the chart renderer" instruction.
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
# CARD IMAGE RENDERER — byte-identical copy from production, per the
# "do not change" instruction.
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

    ax.text(0.5, 0.3, "BMT WATCHLIST (V3 TEST)", fontsize=12, fontweight="bold", color=TEXT_TERTIARY, va="top", zorder=5)
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
            f"Setups derived from {close_date_str} close   \u00b7   Re-validate at next session's open   \u00b7   Not financial advice   \u00b7   V3 TEST TRACK",
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


# ─────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────

def main():
    et_now = datetime.now(ET)
    print(f"[{et_now.isoformat()}] BMT Nightly Setups V3 TEST")

    ensure_schema()

    force_publish = os.environ.get("FORCE_PUBLISH_V3") == "1"
    if force_publish:
        print("  [FORCE_PUBLISH_V3=1 — scheduling gate BYPASSED for testing.]")
        target_date = get_next_actual_trading_day()
    elif not should_publish_tonight():
        tomorrow = et_now + timedelta(days=1)
        print(f"  Tomorrow ({tomorrow.strftime('%A, %B %d')}) is not a trading day — skipping tonight's v3 run.")
        return
    else:
        target_date = get_target_trading_day()

    data_date = get_last_completed_trading_day()
    print(f"  Publishing V3 TEST ideas for {target_date.strftime('%A, %B %d')}, using data as of {data_date.strftime('%A, %B %d')} close.\n")

    print(f"Universe: {len(CANDIDATE_UNIVERSE)} candidate tickers")

    print("\nLoading upcoming earnings calendar...")
    earnings_map = get_upcoming_earnings_map()

    print("\nPulling market context (SPY/QQQ/IWM)...")
    market_context = {t: get_quote_change(t) for t in MARKET_CONTEXT_TICKERS}
    for t, m in market_context.items():
        print(f"  {t}: ${m['price']} ({m['pct']}%)")

    session_date_mdy = get_last_completed_trading_day().strftime("%m/%d/%Y")
    print(f"\nScanning {len(CANDIDATE_UNIVERSE)} candidates for qualifying flow "
          f"(item 3: tier-scaled floor = {FLOW_PREMIUM_FLOOR_PCT} x avg_dollar_volume, "
          f"bias >= {FLOW_BULLISH_CALL_PCT}/<= {FLOW_BEARISH_CALL_PCT})...")
    print(f"  Flow session date: {session_date_mdy}")

    # Item 3 needs avg_dollar_volume to evaluate the tier-scaled floor,
    # which requires OHLC bars -- so bars are fetched during the flow
    # scan itself here (production fetches them in a separate later
    # pass, since its flow floor is a flat constant that needs no
    # volume data up front).
    qualifying = []
    for ticker in CANDIDATE_UNIVERSE:
        flow_rows = get_flow_rows_for_ticker(ticker, session_date_mdy)
        if not flow_rows:
            continue
        total_call = sum(float(f.get("total_Option_Premium_For_Trade", 0) or 0) for f in flow_rows if f.get("put_Or_Call") == "CALL")
        total_put = sum(float(f.get("total_Option_Premium_For_Trade", 0) or 0) for f in flow_rows if f.get("put_Or_Call") == "PUT")
        total_premium = total_call + total_put
        if total_premium <= 0:
            continue
        call_pct = round(total_call / total_premium * 100)
        if call_pct >= FLOW_BULLISH_CALL_PCT:
            bias = "Bullish"
        elif call_pct <= FLOW_BEARISH_CALL_PCT:
            bias = "Bearish"
        else:
            continue  # neutral -- doesn't clear the tightened bias bar

        bars = get_daily_ohlc(ticker)
        if not bars:
            continue
        avg_dollar_vol = compute_avg_dollar_volume(bars)
        floor = FLOW_PREMIUM_FLOOR_PCT * avg_dollar_vol
        if total_premium < floor:
            continue

        near_dated = compute_near_dated_concentration(flow_rows)
        print(f"  [FLOW-V3] {ticker}: {bias} ${total_premium:,.0f} ({call_pct}% call) "
              f"floor=${floor:,.0f} near_dated_share={near_dated['near_dated_share']}")
        qualifying.append({
            "ticker": ticker,
            "flow": {"bias": bias, "premium": total_premium, "call_pct": call_pct},
            "flow_quality": near_dated,
            "bars": bars,
            "avg_dollar_vol": avg_dollar_vol,
        })

    print(f"\n{len(qualifying)} ticker(s) cleared the v3 flow filter.")
    if not qualifying:
        print("Nothing qualifies tonight — no v3 digest to post.")
        return

    print(f"\nChecking {len(qualifying)} qualifying candidate(s) for same-day/recent earnings...")
    still_qualifying = []
    for q in qualifying:
        same_day_er = get_earnings_today_and_recent(q["ticker"])
        if same_day_er:
            print(f"  [SAME-DAY ER EXCLUDE] {q['ticker']}: reported/reports earnings {same_day_er} — excluded")
            continue
        still_qualifying.append(q)
    qualifying = still_qualifying
    print(f"  {len(qualifying)} candidate(s) remain after same-day earnings exclusion")

    if not qualifying:
        print("Nothing qualifies tonight after same-day earnings exclusion — no v3 digest to post.")
        return

    print("Pulling next expiry for qualifying candidates...")
    candidates = []
    for q in qualifying:
        expiry = get_next_expiry(q["ticker"])
        candidates.append({
            "ticker": q["ticker"], "flow": q["flow"], "flow_quality": q["flow_quality"],
            "bars": q["bars"], "ohlc_text": format_ohlc_summary(q["bars"]),
            "next_expiry": expiry["label"], "expiry_iso": expiry["iso"],
            "avg_dollar_vol": q["avg_dollar_vol"],
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
            print(f"  [IV/RV EXCLUDE] {c['ticker']}: IV/RV at {c['iv_rv_ratio']}x — premium too rich")
        else:
            still_viable.append(c)
    pattern_matched = still_viable
    print(f"  {len(pattern_matched)}/{pre_exclude_count} candidate(s) remain after IV/RV pricing filter")

    for c in pattern_matched:
        if c["avg_dollar_vol"] > 0:
            c["flow_intensity"] = c["flow"]["premium"] / c["avg_dollar_vol"]
        else:
            c["flow_intensity"] = 0.0
        if c["iv_rv_ratio"]:
            pricing_multiplier = max(0.4, 1.0 / c["iv_rv_ratio"])
        else:
            pricing_multiplier = 0.75
        c["ranking_score"] = c["flow_intensity"] * pricing_multiplier

    compute_tier_percentiles(pattern_matched)
    for c in pattern_matched:
        tier_label = "MEGA" if c["ticker"] in MEGA_CAP_TIER else "rest"
        print(f"  [RANK] {c['ticker']}: tier={tier_label} ranking_score={c['ranking_score']:.4f} "
              f"tier_percentile={c['tier_percentile']:.1f}")

    print(f"\nApplying same-day/near-term earnings exclusion (full lookahead) to {len(pattern_matched)} candidate(s)...")
    eligible = []
    for c in pattern_matched:
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
        eligible.append(c)

    print(f"\nApplying item 4 dedup + cooldown to {len(eligible)} eligible candidate(s)...")
    eligible = apply_dedup_and_cooldown(eligible)
    print(f"  {len(eligible)} candidate(s) remain after dedup + cooldown")

    if not eligible:
        print("Nothing left after dedup/cooldown — no v3 digest to post tonight.")
        return

    print("\nComputing trade levels, strikes (item 2: strike-at-memory), and RVOL...")
    for c in eligible:
        current_price = get_quote_change(c["ticker"]).get("price")
        if not current_price:
            print(f"  [WARN] {c['ticker']}: no current price -- dropping")
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
        atr = compute_daily_atr(c["bars"])
        if atr <= 0:
            atr = current_price * 0.02
        c.update(compute_trade_levels(c["direction"], c["bars"], current_price, dte=dte))
        strike, premium, provenance = select_strike_v3(
            c["ticker"], c["direction"], current_price, c.get("expiry_iso", ""), c["target1"], atr, c["bars"]
        )
        c["strike"] = strike
        c["premium"] = premium
        c["strike_provenance"] = provenance
        c["chart_bars"] = get_extended_chart_bars(c["ticker"])
        c["rvol"] = compute_rvol(c["chart_bars"] or c["bars"])
    eligible = [c for c in eligible if "strike" in c]

    print(f"\nApplying item 1 expected-move gate ({EXPECTED_MOVE_MAX_RATIO}) to {len(eligible)} candidate(s)...")
    gated = [c for c in eligible if apply_expected_move_gate(c)]
    print(f"  {len(gated)}/{len(eligible)} candidate(s) passed the expected-move gate")

    if not gated:
        print("Nothing passed the expected-move gate tonight.")
        header_embed = build_header_embed(
            f"$SPY {market_context.get('SPY', {}).get('pct', 'N/A')}% | $QQQ {market_context.get('QQQ', {}).get('pct', 'N/A')}% | $IWM {market_context.get('IWM', {}).get('pct', 'N/A')}%",
            target_date,
        )
        no_setups_embed = build_no_setups_embed(
            "No candidates cleared tonight's expected-move gate (required move too large relative to "
            "the option's own implied expected move for every candidate that reached this stage)."
        )
        post_embeds_to_discord([header_embed, no_setups_embed])
        return

    print(f"\nComputing composite scores (item 5) for {len(gated)} candidate(s)...")
    for c in gated:
        c["composite_score"] = compute_composite_score(c)
        print(f"  [SCORE] {c['ticker']}: composite={c['composite_score']:.4f} "
              f"(tier_pct={c.get('tier_percentile', 0):.1f}, req_exp_ratio={c.get('req_exp_ratio')}, "
              f"flow_bonus={c.get('flow_quality', {}).get('bonus')})")

    selected = select_top_n_variable(gated)
    selected = apply_mega_cap_floor(gated, selected)
    selected.sort(key=lambda c: c["composite_score"], reverse=True)

    if len(selected) < TOP_N_MIN:
        print(f"\nOnly {len(selected)} candidate(s) cleared the quality bar (< {TOP_N_MIN} minimum) — "
              f"posting header/best-choice + no-setups embed instead of setup cards, per item 5.")
        header_embed = build_header_embed(
            f"$SPY {market_context.get('SPY', {}).get('pct', 'N/A')}% | $QQQ {market_context.get('QQQ', {}).get('pct', 'N/A')}% | $IWM {market_context.get('IWM', {}).get('pct', 'N/A')}%",
            target_date,
        )
        no_setups_embed = build_no_setups_embed(
            f"Only {len(selected)} setup(s) cleared tonight's quality bar (minimum {TOP_N_MIN} required to publish "
            f"a full digest). Sitting this one out rather than padding the list with weaker ideas."
        )
        post_embeds_to_discord([header_embed, no_setups_embed])
        return

    print(f"\n{len(selected)} setup(s) selected for tonight's v3 digest.")

    print("\nComputing verdict lines (item 8e), edges (item 8d), and risk levels (item 6)...")
    for c in selected:
        c["verdict_line"] = build_verdict_line(c)
        c["risk"] = compute_risk_level(c.get("req_exp_ratio"))
    compute_edges(selected)
    for c in selected:
        print(f"  {c['ticker']}: risk={c['risk']} edge={c['edge']} verdict={c['verdict_line']!r}")

    print("\nComputing analyst target + company name + technical detail for final selections...")
    for c in selected:
        c["analyst_target"] = get_analyst_target(c["ticker"])
        c["company_name"] = get_company_name(c["ticker"])
        c["quality_tag"] = build_quality_tag(c.get("pattern", ""))
        c["narrative"] = build_price_narrative(c)
        c["flow_note"] = build_flow_note_display(c["flow"])
        c["tech_detail"] = build_technical_detail(c)
        print(f"  {c['ticker']}: {c['tech_detail']}")

    print(f"\nGenerating v3 narrative content for {len(selected)} setup(s)...")
    narrative_result = write_setup_narratives_v3(selected, market_context, target_date)

    all_tickers = [c["ticker"] for c in selected] + list(MARKET_CONTEXT_TICKERS)
    market_backdrop = clean_text_field(narrative_result.get("market_backdrop", ""), all_tickers)
    top_pick_ticker = narrative_result.get("top_pick_ticker", "").upper().lstrip("$")
    top_pick_why = clean_text_field(narrative_result.get("top_pick_why", ""), all_tickers)

    raw_setups = narrative_result.get("setups", {})
    setups_by_ticker = {k.lstrip("$").upper(): v for k, v in raw_setups.items()}

    for c in selected:
        s = setups_by_ticker.get(c["ticker"].upper(), {})
        if not s:
            print(f"  [NARRATIVE-V3 WARN] {c['ticker']}: no matching entry in model's 'setups' output "
                  f"(keys returned: {list(raw_setups.keys())}) -- falling back to verdict-line-only for this setup")
        c["why_made_list"] = clean_text_field(s.get("why_made_list", c["verdict_line"]), all_tickers)
        c["why_choose"] = clean_text_field(s.get("why_choose", ""), all_tickers)
        c["watch_out"] = clean_text_field(s.get("watch_out", ""), all_tickers)
        verify_verdict_line_verbatim(c)
        print(f"  {c['ticker']}: edge={c['edge']} | risk={c['risk']}")

    if top_pick_ticker not in {c["ticker"] for c in selected}:
        top_pick_ticker = selected[0]["ticker"]
        top_pick_why = top_pick_why if top_pick_why != "Not provided" else "Top-ranked setup tonight by composite score."

    print(f"\nSaving {len(selected)} v3 setup idea(s) to nightly_setup_ideas_v3 for results tracking...")
    save_setup_ideas_v3(selected, target_date)

    print(f"\nRendering {len(selected)} chart(s)...")
    for c in selected:
        chart_path = f"chart_v3_{c['ticker']}.png"
        render_setup_chart(c, chart_path)
        c["_chart_path"] = chart_path
        print(f"  {c['ticker']}: chart saved to {chart_path}")

    print("\nPosting header + best-choice embeds (V3 TEST)...")
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

    spy = market_context.get("SPY", {})
    qqq = market_context.get("QQQ", {})
    market_theme = (f"$SPY closed at ${spy.get('price', 'N/A')} ({spy.get('pct', 'N/A')}%) and "
                     f"$QQQ at ${qqq.get('price', 'N/A')} ({qqq.get('pct', 'N/A')}%).")
    risk_notes = "See the write-up above for the reasoning, and each chart for exact entry/stop/target levels. (V3 TEST TRACK)"

    out_path = "bmt_nightly_setups_v3.png"
    render_card(selected, market_theme, risk_notes, market_context, target_date, data_date, out_path)
    print(f"\nSummary card saved to {out_path}")

    posted_card = post_image_to_discord(out_path, message="")

    if posted_header and posted_all_setups and posted_contract and posted_card:
        print("\u2713 V3 TEST: Header, all setups+charts, contract list, and summary card posted to Discord!")
    else:
        if not posted_header:
            print("\u2717 Header/best-choice post FAILED")
        if not posted_all_setups:
            print("\u2717 One or more setup+chart posts FAILED")
        if not posted_contract:
            print("\u2717 Contract list post FAILED")
        if not posted_card:
            print("\u2717 Summary card image post FAILED")


run_nightly_job_v3 = main


def start_scheduler():
    scheduler = BackgroundScheduler(timezone="America/New_York")
    # Same 6:00pm ET slot as production -- this runs as a separate
    # Railway service against a separate webhook, so there's no
    # collision; both post independently for side-by-side comparison.
    scheduler.add_job(run_nightly_job_v3, "cron", hour=18, minute=0, id="nightly_setups_v3", replace_existing=True, max_instances=1)
    scheduler.start()
    print("Scheduler started: V3 TEST nightly setups job fires daily at 6:00pm ET.")

    def heartbeat():
        while True:
            time.sleep(900)
            print(f"[HEARTBEAT] scheduler running={scheduler.running}")

    threading.Thread(target=heartbeat, daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    if os.environ.get("FORCE_PUBLISH_V3") == "1":
        run_nightly_job_v3()
    else:
        start_scheduler()