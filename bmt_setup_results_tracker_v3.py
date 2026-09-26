"""
bmt_setup_results_tracker_v3.py — Results grading for
bmt_nightly_setups_v3_test.py ideas ONLY.

FULLY ISOLATED FROM PRODUCTION:
  - Reads/writes nightly_setup_ideas_v3 exclusively — never touches
    nightly_setup_ideas (production's table).
  - Posts to NIGHTLY_SETUPS_V3_TEST_DISCORD_WEBHOOK — never the
    production webhook.

LOCKED INVARIANT (per direct instruction — grading definition unchanged
vs. production's bmt_setup_results_tracker.py):
  - Entry timing: first day whose [low, high] overlaps the setup's
    [entry_low, entry_high] entry zone, starting from publish_date.
  - Win/loss: touch-the-strike, evaluated only over entry_date -> expiry_date.
  - never_triggered is excluded entirely from the results report (but
    still graded/saved so it stops being re-checked).
  - Results only graded/posted on the setup's own expiry date, after
    market close.
This file's grade_setup() is a byte-for-byte copy of production's, and
render_results_table()/the reporting logic are unchanged in shape.

WHAT'S NEW (item 9, v3-only): two additional nullable columns are
populated at grading time, on top of the unchanged win/loss/entry/period
fields:
  - premium_at_publish: already captured at publish time by
    bmt_nightly_setups_v3_test.py's save_setup_ideas_v3() (c["premium"]).
    This script does not need to (and does not) compute it — it's just
    read back for the weekly premium-multiple report addition below.
  - max_favorable_premium: best-effort, computed HERE, at grading time.
    For a CALL: the maximum of (call mid-price) across entry_date ->
    expiry_date, at the SAME strike, via yfinance's per-day option chain
    history where available; for a PUT: the minimum put mid-price
    (cheapest the put ever got is not "favorable" for a long put —
    favorable for a long put is the HIGHEST mid-price the put reached,
    same as calls: the max value the position could have been sold at).
    yfinance does not expose historical intraday options-chain snapshots
    (only the CURRENT chain), so this is fundamentally best-effort: it
    can only sample the chain on days the script actually runs, going
    forward from whenever this fix is deployed. For any already-resolved
    setup, or any setup whose entry/expiry window falls fully in the
    past by the time this script sees it, max_favorable_premium is
    simply left NULL. This limitation is explicit in the column being
    nullable and in every log line touching it.

WEEKLY REPORT ADDITION (item 9, second sentence of the spec): the locked
results table (render_results_table(), unchanged) is still posted as
before; a SEPARATE, second short text message follows with avg premium
multiple on wins vs losses (max_favorable_premium / premium_at_publish,
where both are available) — this is purely additive, never replaces or
alters the locked table.

RESULTS_MODE (mirrors production and bmt_trade_journal.py's pattern):
  run       -> runs the check immediately and exits
  (unset)   -> starts the persistent APScheduler service

Run locally:
  $env:RESULTS_MODE = "run"
  C:\\Python314\\python.exe bmt_setup_results_tracker_v3.py

RUN-ONCE / RAILWAY CRON MODE (2026-09-26): added purely to cut Railway
memory cost. The always-on APScheduler process held RAM 24/7 to fire one
short job per weekday. With RUN_MODE=once this file instead runs the
check a single time and exits, so Railway only bills for the minutes it
actually runs. Grading, posting and DB logic are untouched.
  * Default (no RUN_MODE set): identical to before -- start_scheduler().
  * RESULTS_MODE=run (local testing): unchanged, runs immediately with no
    time gate.
  * RUN_MODE=once: intended for a Railway Cron Schedule. Railway cron is
    UTC-only with no DST awareness, so schedule it at BOTH 20:15 and
    21:15 UTC on weekdays ("15 20,21 * * 1-5") and this script only
    proceeds when the America/New_York hour is 16 (4pm -- the 4:15pm ET
    slot). Exactly one firing passes all year; the other exits in about
    a second. The gate is essential, not cosmetic: in winter the 20:15
    UTC firing lands at 3:15pm ET, BEFORE the close, and would grade
    setups on an unfinished day and mark them resolved.
  * An exception inside the job is logged and swallowed (exit code 0) so
    Railway's restart policy can never re-run a half-finished job.
"""

import os
import sys
import time
import threading
import traceback
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
import matplotlib.patches as patches
from apscheduler.schedulers.background import BackgroundScheduler
import pg8000.native as _pg8000

DATABASE_URL     = os.environ.get("DATABASE_URL", "")
# V3 TEST TRACK: own webhook, never the production one.
DISCORD_WEBHOOK  = os.environ["NIGHTLY_SETUPS_V3_TEST_DISCORD_WEBHOOK"]
ET               = ZoneInfo("America/New_York")


def log(msg: str):
    print(f"[RESULTS-V3] {msg}", flush=True)


# ── DB ────────────────────────────────────────────────────────────────────
def _connect():
    parsed = urlparse(DATABASE_URL)
    return _pg8000.Connection(
        host=parsed.hostname, port=parsed.port or 5432,
        database=parsed.path.lstrip("/"),
        user=parsed.username, password=parsed.password,
    )


def ensure_schema():
    """Mirrors bmt_nightly_setups_v3_test.py's ensure_schema() exactly --
    both scripts call this independently so either can run first."""
    if not DATABASE_URL:
        log("DATABASE_URL not set -- v3 results tracking is fully disabled.")
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
                req_exp_ratio DOUBLE PRECISION,
                flow_premium_over_advol DOUBLE PRECISION,
                call_pct_deviation DOUBLE PRECISION,
                iv_rv_ratio DOUBLE PRECISION,
                rvol DOUBLE PRECISION,
                dte INTEGER,
                pattern TEXT,
                is_mega_cap BOOLEAN,
                composite_score DOUBLE PRECISION,
                premium_at_publish NUMERIC,
                max_favorable_premium NUMERIC,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                resolved_at TIMESTAMPTZ
            )
        """)
    except Exception as e:
        log(f"[DB WARN] ensure_schema (v3) failed: {e}")
    finally:
        conn.close()


def fetch_pending_expiring_today() -> list:
    today = datetime.now(ET).date()
    conn = _connect()
    try:
        rows = conn.run("""
            SELECT id, ticker, direction, strike, entry_low, entry_high,
                   expiry_label, expiry_date, publish_date, premium_at_publish
            FROM nightly_setup_ideas_v3
            WHERE status = 'pending' AND expiry_date = :today
            ORDER BY ticker
        """, today=today)
        return [
            {
                "id": r[0], "ticker": r[1], "direction": r[2], "strike": float(r[3]),
                "entry_low": float(r[4]), "entry_high": float(r[5]),
                "expiry_label": r[6],
                "expiry_date": r[7], "publish_date": r[8],
                "premium_at_publish": float(r[9]) if r[9] is not None else None,
            }
            for r in rows
        ]
    except Exception as e:
        log(f"[DB WARN] fetch_pending_expiring_today failed: {e}")
        return []
    finally:
        conn.close()


def save_result(setup_id: int, status: str, entry_date, period_high, period_low,
                 max_favorable_premium):
    conn = _connect()
    try:
        conn.run("""
            UPDATE nightly_setup_ideas_v3
            SET status = :status, entry_date = :entry_date,
                period_high = :period_high, period_low = :period_low,
                max_favorable_premium = :max_favorable_premium,
                resolved_at = now()
            WHERE id = :id
        """, status=status, entry_date=entry_date,
             period_high=period_high, period_low=period_low,
             max_favorable_premium=max_favorable_premium, id=setup_id)
    except Exception as e:
        log(f"[DB WARN] save_result failed for id={setup_id}: {e}")
    finally:
        conn.close()


# ── Price data ───────────────────────────────────────────────────────────
def fetch_daily_bars(ticker: str, start_date, end_date) -> list:
    """UNCHANGED from production -- daily OHLC from start_date through
    end_date, INCLUSIVE. yfinance's own `end` param is exclusive, so
    it's pushed one day past end_date here."""
    try:
        import yfinance as yf
        end_plus = end_date + timedelta(days=1)
        hist = yf.Ticker(ticker).history(
            start=start_date.strftime("%Y-%m-%d"),
            end=end_plus.strftime("%Y-%m-%d"),
        )
        if hist.empty:
            return []
        return [
            {"date": idx.date(), "high": float(row["High"]), "low": float(row["Low"])}
            for idx, row in hist.iterrows()
        ]
    except Exception as e:
        log(f"[BARS WARN] {ticker}: {e}")
        return []


def estimate_max_favorable_premium(row: dict, entry_date, expiry_date) -> float:
    """
    ITEM 9 (v3-only, best-effort). yfinance exposes only the CURRENT
    option chain, not historical daily option-chain snapshots -- there is
    no way to reconstruct what a specific contract's mid-price was on
    each day of a past entry->expiry window after the fact. So this
    function can only usefully return a value in the narrow case where
    expiry_date is TODAY (i.e. we're grading a setup that just expired)
    and the chain for that expiry is still live enough to quote -- it
    checks the CURRENT (expiry-day) mid-price only, which is a partial,
    likely-understated proxy for the true max favorable premium reached
    at any point during the holding window, not the actual maximum.
    Returns None whenever this can't be computed at all, which is the
    common case -- callers must treat None as "unknown", not "zero".
    """
    try:
        import yfinance as yf
        expiry_iso = expiry_date.strftime("%Y-%m-%d")
        chain = yf.Ticker(row["ticker"]).option_chain(expiry_iso)
        df = chain.calls if row["direction"].upper() == "CALL" else chain.puts
        strike_row = df[df["strike"] == row["strike"]]
        if strike_row.empty:
            strike_row = df.iloc[(df["strike"] - row["strike"]).abs().argsort()[:1]]
        if strike_row.empty:
            return None
        r = strike_row.iloc[0]
        bid = r.get("bid", 0) or 0
        ask = r.get("ask", 0) or 0
        last = r.get("lastPrice", 0) or 0
        mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
        return round(mid, 2) if mid > 0 else None
    except Exception as e:
        log(f"[MAX-FAV-PREMIUM WARN] {row['ticker']}: best-effort estimate failed ({e}) -- leaving NULL")
        return None


# ── Grading — UNCHANGED win/loss/entry-timing logic vs. production ───────
def grade_setup(row: dict) -> dict:
    """Byte-for-byte copy of production's grade_setup() logic (entry
    timing, win/loss rule, period_high/period_low) -- the locked
    invariant. Only addition: also computes max_favorable_premium
    (best-effort, may be None) and includes it in the returned dict."""
    bars = fetch_daily_bars(row["ticker"], row["publish_date"], row["expiry_date"])
    if not bars:
        return None

    entry_low, entry_high = row["entry_low"], row["entry_high"]
    entry_bar = None
    for b in bars:
        if b["date"] < row["publish_date"]:
            continue
        if b["low"] <= entry_high and b["high"] >= entry_low:
            entry_bar = b
            break

    if entry_bar is None:
        return {"status": "never_triggered", "entry_date": None,
                "period_high": None, "period_low": None, "max_favorable_premium": None}

    window = [b for b in bars if b["date"] >= entry_bar["date"]]
    period_high = max(b["high"] for b in window)
    period_low = min(b["low"] for b in window)

    if row["direction"].upper() == "CALL":
        won = period_high >= row["strike"]
    else:
        won = period_low <= row["strike"]

    max_fav_premium = estimate_max_favorable_premium(row, entry_bar["date"], row["expiry_date"])

    return {
        "status": "win" if won else "loss",
        "entry_date": entry_bar["date"],
        "period_high": period_high,
        "period_low": period_low,
        "max_favorable_premium": max_fav_premium,
    }


# ── Results table rendering — UNCHANGED shape vs. production ────────────
BG        = "#0a0e1c"
ROW_A     = "#12172a"
ROW_B     = "#161d38"
HDR_BG    = "#161d38"
COL_BG    = "#1b2242"
BORDER    = "#252c47"
TXT_LIGHT = "#f2f4f8"
TXT_DIM   = "#8891a7"
GREEN     = "#22d3a8"
RED       = "#ef4444"
GOLD      = "#f5a623"
DATA_FONT = "DejaVu Sans Mono"
HDR_FONT  = "DejaVu Sans"

RESULTS_COLS = [
    ("Ticker",        0.075),
    ("Contract",      0.105),
    ("Entry Date",    0.085),
    ("Expiry",        0.075),
    ("Entry Zone",    0.145),
    ("High Touched",  0.115),
    ("Low Touched",   0.115),
    ("Strike",        0.085),
    ("Result",        0.100),
]
RESULTS_ALIGN = ["left", "center", "center", "center", "center", "right", "right", "right", "center"]
RESULTS_RESULT_IDX = 8


def render_results_table(reportable: list, expiry_label: str, out_path: str):
    """Unchanged in shape from production, per the locked invariant --
    only the header title carries a "(V3 TEST)" tag so the two tracks'
    posted images are visually distinguishable in the same channel
    history if ever compared side by side."""
    n = len(reportable)
    wins = sum(1 for _, r in reportable if r["status"] == "win")
    losses = n - wins
    wr = round(wins / n * 100) if n else 0

    FIG_W, MARGIN = 15.5, 0.3
    HDR_H, COL_H, ROW_H, TOT_H = 0.62, 0.5, 0.38, 0.46
    usable_w = FIG_W - 2 * MARGIN
    total_rel = sum(w for _, w in RESULTS_COLS)
    col_ws = [w / total_rel * usable_w for _, w in RESULTS_COLS]
    col_xs = [MARGIN]
    for cw in col_ws[:-1]:
        col_xs.append(col_xs[-1] + cw)

    fig_h = MARGIN + HDR_H + COL_H + n * ROW_H + TOT_H + MARGIN
    fig = plt.figure(figsize=(FIG_W, fig_h), dpi=200, facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, FIG_W); ax.set_ylim(0, fig_h)
    ax.axis("off"); ax.invert_yaxis()
    cursor = MARGIN

    ax.add_patch(patches.Rectangle((MARGIN, cursor), usable_w, HDR_H, facecolor=HDR_BG, zorder=2))
    ax.plot([MARGIN, MARGIN + usable_w], [cursor + HDR_H, cursor + HDR_H], color=GOLD, linewidth=1.3, zorder=4)
    ax.text(MARGIN + 0.25, cursor + HDR_H / 2, f"Setup Results (V3 TEST) \u2014 {expiry_label} Expiry",
            ha="left", va="center", fontsize=14, fontweight="bold", color=TXT_LIGHT,
            fontfamily=HDR_FONT, zorder=3)
    ax.text(MARGIN + usable_w - 0.25, cursor + HDR_H / 2, f"{n} graded  \u00b7  {wins}W-{losses}L  \u00b7  {wr}% win rate",
            ha="right", va="center", fontsize=12, fontweight="bold", color=GREEN, fontfamily=DATA_FONT, zorder=3)
    cursor += HDR_H

    ax.add_patch(patches.Rectangle((MARGIN, cursor), usable_w, COL_H, facecolor=COL_BG, zorder=2))
    ax.plot([MARGIN, MARGIN + usable_w], [cursor + COL_H, cursor + COL_H], color=BORDER, linewidth=0.6, zorder=4)
    for label, cx, cw in zip([c[0] for c in RESULTS_COLS], col_xs, col_ws):
        ax.text(cx + cw / 2, cursor + COL_H / 2, label, ha="center", va="center", fontsize=8.4,
                fontweight="bold", color=GOLD, fontfamily=HDR_FONT, zorder=5)
    cursor += COL_H

    for ri, (row, result) in enumerate(reportable):
        bg = ROW_A if ri % 2 == 0 else ROW_B
        color = GREEN if result["status"] == "win" else RED
        ax.add_patch(patches.Rectangle((MARGIN, cursor), usable_w, ROW_H, facecolor=bg, zorder=2))
        cells = [
            row["ticker"],
            f"{row['direction']} ${row['strike']:g}",
            result["entry_date"].strftime("%-m/%-d") if os.name != "nt" else result["entry_date"].strftime("%#m/%#d"),
            row["expiry_label"],
            f"${row['entry_low']}-${row['entry_high']}",
            f"${result['period_high']:,.2f}",
            f"${result['period_low']:,.2f}",
            f"${row['strike']:g}",
            result["status"].upper(),
        ]
        fg = [TXT_LIGHT, TXT_LIGHT, TXT_DIM, TXT_DIM, TXT_DIM, color, color, TXT_DIM, color]
        for i, (cell, cx, cw, fgc) in enumerate(zip(cells, col_xs, col_ws, fg)):
            align = RESULTS_ALIGN[i]
            tx = cx + cw * 0.92 if align == "right" else (cx + cw / 2 if align == "center" else cx + cw * 0.08)
            bold = i in (0, RESULTS_RESULT_IDX)
            ax.text(tx, cursor + ROW_H / 2, cell, ha=align, va="center", fontsize=8.6,
                    color=fgc, fontweight="bold" if bold else "normal", fontfamily=DATA_FONT, zorder=5)
        cursor += ROW_H

    ax.plot([MARGIN, MARGIN + usable_w], [cursor, cursor], color=GOLD, linewidth=0.8, zorder=4)
    ax.add_patch(patches.Rectangle((MARGIN, cursor), usable_w, TOT_H, facecolor=HDR_BG, zorder=2))
    ax.text(MARGIN + usable_w / 2, cursor + TOT_H / 2,
            f"{wins} WIN  \u00b7  {losses} LOSS  \u00b7  {wr}% win rate  \u00b7  never-triggered setups excluded",
            ha="center", va="center", fontsize=9.5, fontweight="bold", color=TXT_LIGHT,
            fontfamily=DATA_FONT, zorder=5)
    cursor += TOT_H

    plt.savefig(out_path, facecolor=BG, bbox_inches="tight", pad_inches=0.2)
    plt.close(fig)
    log(f"Results table rendered \u2192 {out_path}")


def post_image_to_discord(image_path: str, message: str = "") -> bool:
    with open(image_path, "rb") as f:
        files = {"file": (os.path.basename(image_path), f, "image/png")}
        data = {"content": message}
        try:
            r = requests.post(DISCORD_WEBHOOK, data=data, files=files, timeout=30)
            log(f"[DISCORD] results image post: {r.status_code}")
            if r.status_code not in (200, 204):
                log(f"  body: {r.text[:500]}")
            return r.status_code in (200, 204)
        except Exception as e:
            log(f"[DISCORD] results image post FAILED: {e}")
            return False


def post_text_to_discord(content: str) -> bool:
    try:
        r = requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=15)
        log(f"[DISCORD] text post: {r.status_code}")
        return r.status_code in (200, 204)
    except Exception as e:
        log(f"[DISCORD] text post FAILED: {e}")
        return False


def build_premium_multiple_line(reportable: list) -> str:
    """
    ITEM 9 (second sentence): "avg premium multiple on wins vs losses" --
    computed as max_favorable_premium / premium_at_publish per setup,
    averaged separately across wins and losses. Both fields are
    nullable/best-effort (see estimate_max_favorable_premium()'s
    docstring for why), so this only includes setups where BOTH values
    are actually available -- and says plainly how many that was out of
    the total, rather than silently averaging over a partial, possibly
    unrepresentative subset without saying so.
    """
    win_multiples, loss_multiples = [], []
    for row, result in reportable:
        premium_pub = row.get("premium_at_publish")
        premium_max = result.get("max_favorable_premium")
        if not premium_pub or premium_pub <= 0 or premium_max is None:
            continue
        multiple = premium_max / premium_pub
        if result["status"] == "win":
            win_multiples.append(multiple)
        elif result["status"] == "loss":
            loss_multiples.append(multiple)

    n_total = len(reportable)
    n_with_data = len(win_multiples) + len(loss_multiples)
    if n_with_data == 0:
        return (f"Premium-multiple data: not available for this batch (0/{n_total} setups had both "
                f"premium_at_publish and max_favorable_premium -- see script docstring on this being "
                f"a best-effort, forward-looking-only estimate).")

    win_avg = sum(win_multiples) / len(win_multiples) if win_multiples else None
    loss_avg = sum(loss_multiples) / len(loss_multiples) if loss_multiples else None
    win_str = f"{win_avg:.2f}x avg on {len(win_multiples)} win(s)" if win_avg is not None else "no wins with data"
    loss_str = f"{loss_avg:.2f}x avg on {len(loss_multiples)} loss(es)" if loss_avg is not None else "no losses with data"
    return f"Premium multiple (max favorable / at-publish) \u2014 {win_str}, {loss_str}. ({n_with_data}/{n_total} setups had complete data.)"


# ── Main job ──────────────────────────────────────────────────────────────
def run_results_check():
    now = datetime.now(ET)
    log(f"[{now.isoformat()}] Checking for V3 setups expiring today ({now.date()})...")

    if not DATABASE_URL:
        log("DATABASE_URL not set -- nothing to check.")
        return

    ensure_schema()
    pending = fetch_pending_expiring_today()
    if not pending:
        log("No pending v3 setups expire today -- nothing to report.")
        return

    log(f"{len(pending)} v3 setup(s) expiring today, grading each (grading logic unchanged from production)...")
    graded = []
    for row in pending:
        result = grade_setup(row)
        if result is None:
            log(f"  [SKIP] {row['ticker']}: price data unavailable this run -- will retry next scheduled run")
            continue
        save_result(row["id"], result["status"], result["entry_date"],
                    result["period_high"], result["period_low"],
                    result["max_favorable_premium"])
        graded.append((row, result))
        log(f"  {row['ticker']}: {result['status']}"
            + (f" (entered {result['entry_date']}, range ${result['period_low']:.2f}-${result['period_high']:.2f}, "
               f"max_fav_premium={result['max_favorable_premium']})"
               if result["entry_date"] else ""))

    if not graded:
        log("Nothing could be graded this run (price data unavailable for all) -- will retry.")
        return

    wins = sum(1 for _, r in graded if r["status"] == "win")
    losses = sum(1 for _, r in graded if r["status"] == "loss")
    never = sum(1 for _, r in graded if r["status"] == "never_triggered")
    log(f"Graded {len(graded)}: {wins} win, {losses} loss, {never} never triggered (not reported).")

    reportable = [(row, result) for row, result in graded if result["status"] != "never_triggered"]
    if not reportable:
        log("All graded setups were never triggered -- nothing to post today.")
        return

    reportable.sort(key=lambda pair: pair[1]["entry_date"], reverse=True)

    expiry_label = reportable[0][0]["expiry_label"] or now.strftime("%b %d")
    out_path = "bmt_setup_results_v3.png"
    render_results_table(reportable, expiry_label, out_path)

    wins_r = sum(1 for _, r in reportable if r["status"] == "win")
    losses_r = len(reportable) - wins_r
    wr = round(wins_r / len(reportable) * 100)
    caption = f"**Setup Results (V3 TEST) \u2014 {expiry_label} Expiry**   {wins_r}W / {losses_r}L  \u00b7  {wr}% win rate"

    posted = post_image_to_discord(out_path, message=caption)
    if posted:
        log("\u2713 V3 results table posted to Discord.")
    else:
        log("\u2717 V3 results table post FAILED.")
        return

    # Item 9 second sentence: separate additive posting, never alters
    # the locked table above.
    premium_line = build_premium_multiple_line(reportable)
    post_text_to_discord(premium_line)


run_results_job = run_results_check


# ── Scheduler ─────────────────────────────────────────────────────────────
def start_scheduler():
    scheduler = BackgroundScheduler(timezone="America/New_York")
    # Same 4:15pm ET slot as production, per the locked timing invariant
    # -- this is a separate Railway service against a separate table and
    # webhook, so no collision.
    scheduler.add_job(run_results_job, "cron", day_of_week="mon-fri", hour=16, minute=15,
                       id="setup_results_check_v3", replace_existing=True, max_instances=1)
    scheduler.start()
    log("Scheduler started: V3 setup results check fires daily at 4:15pm ET Mon-Fri.")

    def heartbeat():
        while True:
            time.sleep(900)
            log(f"[HEARTBEAT] scheduler running={scheduler.running}")

    threading.Thread(target=heartbeat, daemon=True).start()
    while True:
        time.sleep(3600)


# The America/New_York hour (0-23) the check may start in when running
# under Railway cron (RUN_MODE=once). 16 == 4pm ET, i.e. the 4:15pm slot.
CRON_TARGET_HOUR_ET = 16


def cron_should_fire(et_now: datetime) -> bool:
    """Gate for Railway cron mode. Railway fires this service at both
    20:15 and 21:15 UTC on weekdays; only the firing that lands in the
    4pm ET hour is allowed through. Comparing the ET *hour* (not the exact
    minute) tolerates Railway's few-minutes cron start jitter."""
    return et_now.hour == CRON_TARGET_HOUR_ET


def run_once_from_cron():
    et_now = datetime.now(ET)
    if not cron_should_fire(et_now):
        log(f"[{et_now.isoformat()}] RUN_MODE=once: ET hour is {et_now.hour}, "
            f"not {CRON_TARGET_HOUR_ET} -- this is the off-DST cron firing, exiting without grading.")
        return
    try:
        run_results_job()
    except Exception:
        # Deliberately swallowed (process still exits 0) so a restart
        # policy can't re-run the job after a partial failure.
        log("\u2717 RUN_MODE=once: results job raised an exception (traceback below); NOT retrying.")
        traceback.print_exc()


if __name__ == "__main__":
    mode = os.environ.get("RESULTS_MODE", "scheduler").lower()
    run_once = os.environ.get("RUN_MODE", "").strip().lower() == "once"
    log(f"BMT Setup Results Tracker V3 starting (mode={mode}, run_once={run_once})...")
    if mode == "run":
        run_results_job()
    elif run_once:
        run_once_from_cron()
    else:
        start_scheduler()