"""
bmt_setup_results_tracker.py — Results grading for bmt_nightly_setups.py
ideas (NEW, 2026-08-16).

WHY THIS EXISTS
Discord doesn't tell you whether a trade idea ever actually worked, and
nobody's forced to take an idea for BMT to ever find out. This script
closes that loop: it grades every published setup against what the
underlying stock actually did, and reports the result the day the
contract would have expired.

HOW GRADING WORKS (locked with the user 2026-08-16, using the GTLB
Aug 14 -> Aug 21 example as the reference case)

1. ENTRY TIMING is tracked day-by-day, not assumed on the publish date.
   Starting from the setup's publish_date (the trading day the idea was
   FOR), each subsequent daily bar's [low, high] range is checked for
   overlap with the setup's [entry_low, entry_high] entry zone. The
   FIRST day that overlaps is the entry date -- this is the day someone
   actually could have gotten filled inside the stated entry zone, not
   just the day the idea was posted.
   - If the zone is never touched by expiry, the setup is marked
     "never_triggered" -- explicitly NOT a loss. A trade that was never
     entered can't be graded as won or lost; reporting it as a loss
     would understate the strategy's real hit rate on ideas people
     could actually have taken. Per direct user request, never-
     triggered setups are graded and saved to the DB (so they're
     resolved and stop being re-checked) but are excluded ENTIRELY from
     the results report -- there's no trade to show a result for.

2. WIN/LOSS is evaluated only over the window from entry_date through
   expiry_date (inclusive), not the full publish-to-expiry window:
   - CALL: win if the underlying's HIGH reached or exceeded the strike
     at any point in that window.
   - PUT: win if the underlying's LOW reached or beat (at or below) the
     strike at any point in that window.
   - Otherwise, loss.
   period_high / period_low are the actual highest-high and lowest-low
   across that same entry-to-expiry window, and are ALWAYS reported for
   every row -- e.g. a CALL that won on its high still shows the low it
   dipped to first. This is deliberate: it shows the real volatility
   the trade lived through, not just the number that decided the
   verdict.

3. TIMING: results for a given setup are only ever graded and posted on
   its own expiry date, after market close (see start_scheduler() below
   -- 4:15pm ET, after the 4:00pm close). Multiple setups from the same
   night usually share one expiry date and are graded and reported
   together in one batch; setups from different nights land in whatever
   batch matches their own expiry date, which may or may not be the
   same run.

REPORTING FORMAT (revised 2026-08-16): originally built as one Discord
embed per ticker, which worked fine for 3-5 results but became
unreadable at the real expected volume of 25-30 graded setups per
batch -- a wall of 25+ stacked cards, or a truncated "+X more" summary
that dropped rows entirely, neither of which the user wanted (results
must show ALL graded setups, every time, with both high and low always
visible). Discord's embed/content fields don't render markdown tables
at all (same limitation bmt_trade_journal.py works around), so the fix
is the same pattern already used elsewhere in this codebase: render a
single PNG table (render_results_table(), matplotlib) with one row per
graded setup and post it as an image attachment. A table image scales
to any row count without truncation, unlike a stack of Discord embeds
capped at 10 per message.

DATA SOURCE: yfinance daily OHLC bars, same as bmt_nightly_setups.py
uses for its own chart rendering -- no new data dependency introduced.

PERSISTENCE: reads/writes the nightly_setup_ideas table that
bmt_nightly_setups.py's save_setup_ideas() creates and inserts into
(see ensure_schema() in that file). This script only ever UPDATEs rows
where status = 'pending' AND expiry_date = today -- it never touches
rows for setups that haven't expired yet, and never re-grades a setup
that's already been resolved.

RESULTS_MODE (local testing only, mirrors bmt_trade_journal.py's
JOURNAL_MODE pattern):
  run       -> runs the check immediately and exits
  (unset)   -> starts the persistent APScheduler service

Run locally:
  $env:RESULTS_MODE = "run"
  C:\\Python314\\python.exe bmt_setup_results_tracker.py
"""

import os
import sys
import time
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
import matplotlib.patches as patches
from apscheduler.schedulers.background import BackgroundScheduler
import pg8000.native as _pg8000

DATABASE_URL     = os.environ.get("DATABASE_URL", "")
# Same webhook as bmt_nightly_setups.py -- results post to the same
# channel the original ideas went out in, per direct user request.
DISCORD_WEBHOOK  = os.environ["NIGHTLY_SETUPS_DISCORD_WEBHOOK"]
ET               = ZoneInfo("America/New_York")


def log(msg: str):
    print(f"[RESULTS] {msg}", flush=True)


# ── DB ────────────────────────────────────────────────────────────────────
def _connect():
    parsed = urlparse(DATABASE_URL)
    return _pg8000.Connection(
        host=parsed.hostname, port=parsed.port or 5432,
        database=parsed.path.lstrip("/"),
        user=parsed.username, password=parsed.password,
    )


def ensure_schema():
    """Mirrors bmt_nightly_setups.py's ensure_schema() exactly -- both
    scripts call this independently on startup so either one can run
    first without erroring if the table doesn't exist yet."""
    if not DATABASE_URL:
        log("DATABASE_URL not set -- results tracking is fully disabled.")
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
        log(f"[DB WARN] ensure_schema failed: {e}")
    finally:
        conn.close()


def fetch_pending_expiring_today() -> list:
    today = datetime.now(ET).date()
    conn = _connect()
    try:
        rows = conn.run("""
            SELECT id, ticker, direction, strike, entry_low, entry_high,
                   expiry_label, expiry_date, publish_date
            FROM nightly_setup_ideas
            WHERE status = 'pending' AND expiry_date = :today
            ORDER BY ticker
        """, today=today)
        return [
            {
                "id": r[0], "ticker": r[1], "direction": r[2], "strike": float(r[3]),
                "entry_low": float(r[4]), "entry_high": float(r[5]),
                "expiry_label": r[6],
                "expiry_date": r[7], "publish_date": r[8],
            }
            for r in rows
        ]
    except Exception as e:
        log(f"[DB WARN] fetch_pending_expiring_today failed: {e}")
        return []
    finally:
        conn.close()


def save_result(setup_id: int, status: str, entry_date, period_high, period_low):
    conn = _connect()
    try:
        conn.run("""
            UPDATE nightly_setup_ideas
            SET status = :status, entry_date = :entry_date,
                period_high = :period_high, period_low = :period_low,
                resolved_at = now()
            WHERE id = :id
        """, status=status, entry_date=entry_date,
             period_high=period_high, period_low=period_low, id=setup_id)
    except Exception as e:
        log(f"[DB WARN] save_result failed for id={setup_id}: {e}")
    finally:
        conn.close()


# ── Price data ───────────────────────────────────────────────────────────
def fetch_daily_bars(ticker: str, start_date, end_date) -> list:
    """Daily OHLC from start_date through end_date, INCLUSIVE. yfinance's
    own `end` param is exclusive, so it's pushed one day past end_date
    here to make sure the expiry day's own bar is included."""
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


# ── Grading ──────────────────────────────────────────────────────────────
def grade_setup(row: dict) -> dict:
    """Returns None if price data isn't available yet (caller should
    retry on a later run) -- otherwise a dict with status/entry_date/
    period_high/period_low, per the rules in the module docstring."""
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
                "period_high": None, "period_low": None}

    window = [b for b in bars if b["date"] >= entry_bar["date"]]
    period_high = max(b["high"] for b in window)
    period_low = min(b["low"] for b in window)

    if row["direction"].upper() == "CALL":
        won = period_high >= row["strike"]
    else:
        won = period_low <= row["strike"]

    return {
        "status": "win" if won else "loss",
        "entry_date": entry_bar["date"],
        "period_high": period_high,
        "period_low": period_low,
    }


# ── Results table rendering (2026-08-16) ────────────────────────────────
# One row per graded, reportable setup (never_triggered excluded before
# this is called). Renders every row -- no truncation regardless of
# count -- and always shows both period_high and period_low, even
# though only one of them decided the win/loss for a given direction.
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
    """reportable: list of (row, result) tuples, already sorted by the
    caller. row has ticker/direction/strike/expiry_label/entry_low/
    entry_high; result has status/entry_date/period_high/period_low."""
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
    ax.text(MARGIN + 0.25, cursor + HDR_H / 2, f"Setup Results \u2014 {expiry_label} Expiry",
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


# ── Main job ──────────────────────────────────────────────────────────────
def run_results_check():
    now = datetime.now(ET)
    log(f"[{now.isoformat()}] Checking for setups expiring today ({now.date()})...")

    if not DATABASE_URL:
        log("DATABASE_URL not set -- nothing to check.")
        return

    ensure_schema()
    pending = fetch_pending_expiring_today()
    if not pending:
        log("No pending setups expire today -- nothing to report.")
        return

    log(f"{len(pending)} setup(s) expiring today, grading each...")
    graded = []
    for row in pending:
        result = grade_setup(row)
        if result is None:
            log(f"  [SKIP] {row['ticker']}: price data unavailable this run -- will retry next scheduled run")
            continue
        # Saved regardless of status -- this is what marks the row
        # resolved so it stops showing up in
        # fetch_pending_expiring_today() on future runs. Only the
        # reporting step below excludes never_triggered.
        save_result(row["id"], result["status"], result["entry_date"],
                    result["period_high"], result["period_low"])
        graded.append((row, result))
        log(f"  {row['ticker']}: {result['status']}"
            + (f" (entered {result['entry_date']}, range ${result['period_low']:.2f}-${result['period_high']:.2f})"
               if result["entry_date"] else ""))

    if not graded:
        log("Nothing could be graded this run (price data unavailable for all) -- will retry.")
        return

    wins = sum(1 for _, r in graded if r["status"] == "win")
    losses = sum(1 for _, r in graded if r["status"] == "loss")
    never = sum(1 for _, r in graded if r["status"] == "never_triggered")
    log(f"Graded {len(graded)}: {wins} win, {losses} loss, {never} never triggered (not reported).")

    # Never-triggered setups are graded and saved above, but excluded
    # entirely from the report per direct user request -- there's no
    # trade to show a result for.
    reportable = [(row, result) for row, result in graded if result["status"] != "never_triggered"]
    if not reportable:
        log("All graded setups were never triggered -- nothing to post today.")
        return

    # Most recently entered first.
    reportable.sort(key=lambda pair: pair[1]["entry_date"], reverse=True)

    expiry_label = reportable[0][0]["expiry_label"] or now.strftime("%b %d")
    out_path = "bmt_setup_results.png"
    render_results_table(reportable, expiry_label, out_path)

    wins_r = sum(1 for _, r in reportable if r["status"] == "win")
    losses_r = len(reportable) - wins_r
    wr = round(wins_r / len(reportable) * 100)
    caption = f"**Setup Results \u2014 {expiry_label} Expiry**   {wins_r}W / {losses_r}L  \u00b7  {wr}% win rate"

    posted = post_image_to_discord(out_path, message=caption)
    if posted:
        log("\u2713 Results table posted to Discord.")
    else:
        log("\u2717 Results table post FAILED.")


run_results_job = run_results_check


# ── Scheduler ─────────────────────────────────────────────────────────────
def start_scheduler():
    scheduler = BackgroundScheduler(timezone="America/New_York")
    # 4:15pm ET -- after the 4:00pm market close, so the expiry day's
    # own high/low is fully known before grading runs.
    scheduler.add_job(run_results_job, "cron", day_of_week="mon-fri", hour=16, minute=15,
                       id="setup_results_check", replace_existing=True, max_instances=1)
    scheduler.start()
    log("Scheduler started: setup results check fires daily at 4:15pm ET Mon-Fri.")

    def heartbeat():
        while True:
            time.sleep(900)
            log(f"[HEARTBEAT] scheduler running={scheduler.running}")

    threading.Thread(target=heartbeat, daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    mode = os.environ.get("RESULTS_MODE", "scheduler").lower()
    log(f"BMT Setup Results Tracker starting (mode={mode})...")
    if mode == "run":
        run_results_job()
    else:
        start_scheduler()