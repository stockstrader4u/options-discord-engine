"""
gex_vex_combined_daily.py — Production combined daily GEX/VEX post for
BlueMoonTrades subscribers. Runs ONCE per trigger (Railway cron,
3:00pm ET Mon-Fri) and posts, IN STRICT SEQUENCE, to a single shared
GEX Discord channel:

  1. Mag 7 GEX Snapshot -- intro message, then 7 individual cards
     (AAPL, MSFT, GOOGL, AMZN, NVDA, META, TSLA), one at a time.
  2. SPY/QQQ/IWM GEX/VEX Dashboard -- the existing 3-across card,
     completely unchanged from gex_vex_daily.py.

REPLACES both gex_vex_daily.py and gex_vex_mag7_daily.py as SEPARATE
scheduled jobs -- per direct user request, both pipelines now share
ONE Discord channel and must publish in a strict, guaranteed order
(Mag 7 first, then SPY/QQQ/IWM) every single day. Two independent
Railway cron triggers (e.g. 3:00 and 3:15) cannot strictly GUARANTEE
that ordering -- a slow run, a scheduling hiccup, or clock drift could
cause them to interleave or post out of order in a live channel. A
single script with a single trigger, running both pipelines back-to-
back in one process, is the only way to guarantee the sequence every
time.

Both pipelines' own logic (compute_gex_vex, generate_gex_watch_lines,
render_single_ticker_gex_card, render_gex_dashboard_card, etc.) are
UNCHANGED -- they still live in gex_vex.py and are called exactly as
before. This file only handles the ORCHESTRATION: which pipeline runs
first, shared webhook/pacing, and the single combined cron entry point.

Required env vars (set on Railway, not locally):
  ALPACA_API_KEY_ID
  ALPACA_API_SECRET_KEY
  OPENROUTER_API_KEY        (optional — falls back to data-derived
                              watch lines if unset, never blank)
  GEX_DISCORD_WEBHOOK       (the SINGLE shared GEX channel webhook --
                              both Mag 7 and SPY/QQQ/IWM now post here.
                              Same env var name gex_vex_daily.py already
                              used, so no Railway variable rename is
                              needed for the SPY/QQQ/IWM half of this
                              -- only the Mag 7 half is new.)

PRODUCTION HARDENING:
  - Mag 7 and SPY/QQQ/IWM are independent failure domains: if the
    Mag 7 half fails entirely, the SPY/QQQ/IWM half STILL RUNS -- a
    problem with one half must never silently take down the other,
    since subscribers still expect to see whichever half is healthy.
  - Within the Mag 7 half, a single failed ticker doesn't stop the
    other 6 from posting (same per-ticker isolation as before).
  - Exits non-zero only if BOTH halves fail completely, so Railway's
    monitoring flags a genuinely broken run without flagging every
    partial/single-ticker hiccup as a hard failure.
  - Every run logs a clear section header for each half and a final
    combined summary, so a glance at Railway's log viewer tells you
    exactly what happened without digging through interleaved output.

Deploy this file alongside the unchanged gex_vex.py in the same
service. Point the Railway cron trigger at this file instead of the
two old ones:
    python gex_vex_combined_daily.py
at 3:00pm ET Mon-Fri, and remove/disable the old separate 3:30pm and
3:45pm triggers for gex_vex_mag7_daily.py and gex_vex_daily.py.
"""

import os
import sys
import time
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo

import requests


def log(msg: str):
    print(f"[GEX-COMBINED-DAILY] {msg}", flush=True)


try:
    import gex_vex
except Exception as e:
    log(f"FATAL: failed to import gex_vex.py — {type(e).__name__}: {e}")
    log("This usually means gex_vex.py has a syntax error or was pasted/")
    log("uploaded incorrectly (e.g. duplicated content, truncated file).")
    log("Check the file directly before assuming this is a data/API issue.")
    raise

WEBHOOK_URL = os.environ.get("GEX_DISCORD_WEBHOOK", "")

MAG7_TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]
CORE_TICKERS = ("SPY", "QQQ", "IWM")

OUT_DIR = "."
ET = ZoneInfo("America/New_York")

# Pace delay between individual Discord posts -- avoids hammering the
# webhook with rapid-fire image uploads back to back.
POST_PACE_SECONDS = 1.0


# ── Shared Discord posting helpers ──────────────────────────────────────
def post_text_to_discord(content: str) -> bool:
    try:
        r = requests.post(WEBHOOK_URL, json={"content": content}, timeout=15)
        ok = r.status_code in (200, 204)
        if not ok:
            log(f"Text post FAILED: {r.status_code} {r.text[:300]}")
        return ok
    except Exception as e:
        log(f"Text post exception: {e}")
        return False


def post_embed_to_discord(embed: dict) -> bool:
    try:
        r = requests.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=15)
        ok = r.status_code in (200, 204)
        if not ok:
            log(f"Embed post FAILED: {r.status_code} {r.text[:300]}")
        return ok
    except Exception as e:
        log(f"Embed post exception: {e}")
        return False


def post_image_to_discord(image_path: str, caption: str = "") -> bool:
    try:
        with open(image_path, "rb") as f:
            files = {"file": (os.path.basename(image_path), f, "image/png")}
            data = {"content": caption}
            r = requests.post(WEBHOOK_URL, data=data, files=files, timeout=30)
        ok = r.status_code in (200, 204)
        if not ok:
            log(f"Image post FAILED: {r.status_code} {r.text[:300]}")
        return ok
    except Exception as e:
        log(f"Image post exception: {e}")
        return False


def get_week_label(today: date = None) -> str:
    if today is None:
        today = date.today()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    if monday.month == friday.month:
        return f"Week of {monday.strftime('%b %d')} - {friday.strftime('%d, %Y')}"
    return f"Week of {monday.strftime('%b %d')} - {friday.strftime('%b %d, %Y')}"


def build_single_ticker_embed(r: dict, week_label: str, watch_line: str) -> dict:
    """Single-ticker equivalent of gex_vex.build_gex_embed(), scoped to
    one ticker's Regime / Key Levels / Expected Move / What To Watch
    fields for the Mag 7 posts. Identical to the version originally
    built in gex_vex_mag7_daily.py -- kept here since that standalone
    script is being replaced by this combined one."""
    if "error" in r:
        return {
            "title": f"\u26A0\uFE0F {r.get('ticker', '?')} GEX/VEX \u2014 {week_label}",
            "color": 0x6B7280,
            "description": f"Data unavailable this run: {r['error']}",
            "footer": {"text": "Standard public-GEX approximation \u00b7 BlueMoonTrades"},
        }

    ticker = r["ticker"]
    is_short = r["net_gex"] < 0
    color = 0xDC2626 if is_short else 0x059669
    regime_word = "SHORT GAMMA" if is_short else "LONG GAMMA"
    dot = "\U0001F534" if is_short else "\U0001F7E2"

    em = r.get("expected_move") or {}
    # BUGFIX (2026-08-08): the Key Levels field required BOTH put_wall
    # AND call_wall to be present ("and" condition) before showing
    # EITHER one -- confirmed in production: AAPL had a real, valid
    # call wall ($310, correctly shown in the rendered card image) but
    # its put wall legitimately came back None (correctly excluded by
    # the new minimum-significance wall-selection fix), and this "and"
    # condition threw away the perfectly good call wall data along
    # with the missing put wall, showing a bare "N/A" for the whole
    # field. This happened on EVERY Mag 7 ticker in the run that
    # exposed it, since the same threshold fix made missing walls much
    # more common (correctly) than before. Fixed by formatting each
    # side independently, matching the same fix already applied to
    # gex_vex.py's build_gex_embed() for the SPY/QQQ/IWM dashboard.
    put_str = f"${r['put_wall']:,.0f}" if r.get("put_wall") is not None else "N/A"
    call_str = f"${r['call_wall']:,.0f}" if r.get("call_wall") is not None else "N/A"
    fields = [
        {"name": "\U0001F4CA Regime", "value": f"{dot} **{regime_word}**", "inline": True},
        {"name": "\U0001F3AF Key Levels",
         "value": f"Put {put_str} \u00b7 Call {call_str}",
         "inline": True},
        {"name": "\U0001F4CF Expected Move",
         "value": f"\u00b1${em['dollar']} ({em['pct']}%)" if em else "N/A",
         "inline": True},
    ]
    if watch_line:
        fields.append({"name": "\U0001F440 What To Watch \u2014 Action", "value": watch_line, "inline": False})

    return {
        "title": f"\U0001F4CA {ticker} GEX/VEX \u2014 {week_label}",
        "color": color,
        "fields": fields,
        "footer": {"text": "Standard public-GEX approximation \u00b7 BlueMoonTrades"},
    }


# ── Half 1: Mag 7 individual cards ──────────────────────────────────────
def run_mag7_section(week_label: str) -> int:
    """
    Returns the number of Mag 7 tickers successfully posted (0-7).
    Isolated in its own function/try-boundary so a failure here can
    never prevent the SPY/QQQ/IWM section from still running.
    """
    log("=" * 60)
    log("SECTION 1/2: Mag 7 individual cards")
    log("=" * 60)

    results = []
    for t in MAG7_TICKERS:
        result = gex_vex.compute_gex_vex(t, expiries=None)
        if "error" in result:
            log(f"{t}: ERROR — {result['error']}")
        else:
            flip = result.get("gamma_flip")
            log(f"{t}: OK — spot=${result['spot']:.2f} "
                f"net_gex={result['net_gex']/1e9:+.2f}B flip={flip}")
        results.append(result)

    valid_count = sum(1 for r in results if "error" not in r)
    if valid_count == 0:
        log("All 7 Mag 7 tickers failed — skipping the Mag 7 section entirely "
            "this run (SPY/QQQ/IWM section will still run separately below).")
        return 0

    if valid_count < len(MAG7_TICKERS):
        log(f"WARNING: only {valid_count}/{len(MAG7_TICKERS)} Mag 7 tickers succeeded — "
            f"posting the ones that worked, skipping the rest.")

    valid_results = [r for r in results if "error" not in r]
    watch_lines_by_ticker = {}
    for r in valid_results:
        lines = gex_vex.generate_gex_watch_lines([r])
        watch_lines_by_ticker[r["ticker"]] = lines.get(r["ticker"], "")

    intro = (
        f"\U0001F4CA **MAG 7 GEX SNAPSHOT \u2014 {week_label}**\n"
        f"Individual gamma positioning for each of the Magnificent 7, updated daily.\n"
        f"_Standard public-GEX approximation \u00b7 BlueMoonTrades_"
    )
    intro_ok = post_text_to_discord(intro)
    if not intro_ok:
        log("WARNING: Mag 7 intro message failed to post — continuing with individual ticker posts anyway.")
    time.sleep(POST_PACE_SECONDS)

    posted_count = 0
    for r in results:
        ticker = r.get("ticker", "?")
        watch_line = watch_lines_by_ticker.get(ticker, "")

        embed = build_single_ticker_embed(r, week_label, watch_line)
        embed_ok = post_embed_to_discord(embed)
        time.sleep(POST_PACE_SECONDS)

        image_ok = True
        if "error" not in r:
            out_path = os.path.join(OUT_DIR, f"gex_mag7_{ticker}.png")
            gex_vex.render_single_ticker_gex_card(r, week_label, out_path)
            image_ok = post_image_to_discord(out_path)
            time.sleep(POST_PACE_SECONDS)

        if embed_ok and image_ok:
            posted_count += 1
            log(f"{ticker}: posted successfully.")
        else:
            log(f"{ticker}: posted with issues — embed_ok={embed_ok} image_ok={image_ok}")

    log(f"Mag 7 section done — {posted_count}/{len(MAG7_TICKERS)} ticker(s) posted.")
    return posted_count


# ── Half 2: SPY/QQQ/IWM 3-across dashboard ──────────────────────────────
def run_core_section(week_label: str) -> bool:
    """
    Returns True if the SPY/QQQ/IWM dashboard posted successfully.
    Identical logic to the original gex_vex_daily.py -- isolated in its
    own function/try-boundary so a failure here can never retroactively
    affect the Mag 7 section, which already ran and posted above.
    """
    log("=" * 60)
    log("SECTION 2/2: SPY/QQQ/IWM GEX/VEX Dashboard")
    log("=" * 60)

    results = []
    for t in CORE_TICKERS:
        result = gex_vex.compute_gex_vex(t, expiries=None)
        if "error" in result:
            log(f"{t}: ERROR — {result['error']}")
        else:
            flip = result.get("gamma_flip")
            log(f"{t}: OK — spot=${result['spot']:.2f} "
                f"net_gex={result['net_gex']/1e9:+.2f}B flip={flip}")
            spot = result.get("spot")
            if flip is not None and spot and abs(flip - spot) / spot > 0.08:
                log(f"  WARNING: {t} gamma flip {flip} is >8% from spot {spot} — "
                    f"worth spot-checking with dump_cumulative_table if this recurs")
        results.append(result)

    valid_count = sum(1 for r in results if "error" not in r)
    if valid_count == 0:
        log("All 3 core tickers (SPY/QQQ/IWM) failed — skipping this section entirely this run.")
        return False

    if valid_count < len(CORE_TICKERS):
        log(f"WARNING: only {valid_count}/{len(CORE_TICKERS)} core tickers succeeded — "
            f"posting the dashboard with the tickers that worked.")

    watch_lines = gex_vex.generate_gex_watch_lines(results)

    out_path = os.path.join(OUT_DIR, "gex_vex_dashboard.png")
    gex_vex.render_gex_dashboard_card(results, week_label, out_path)

    embed = gex_vex.build_gex_embed(results, week_label, watch_lines)
    embed_ok = post_embed_to_discord(embed)
    time.sleep(POST_PACE_SECONDS)

    image_ok = post_image_to_discord(out_path)

    if embed_ok and image_ok:
        log("SPY/QQQ/IWM dashboard posted successfully.")
        return True
    log(f"SPY/QQQ/IWM dashboard posted with issues — embed_ok={embed_ok} image_ok={image_ok}")
    return embed_ok or image_ok


def main():
    et_now = datetime.now(ET)
    log(f"Starting combined run at {et_now.isoformat()}")

    if not WEBHOOK_URL:
        log("FATAL: GEX_DISCORD_WEBHOOK is not set. Exiting without posting.")
        sys.exit(1)

    week_label = get_week_label()

    # STRICT SEQUENCE, per direct user request: Mag 7 posts completely
    # (intro + all 7 tickers) BEFORE the SPY/QQQ/IWM section begins.
    # Each section is wrapped so a total failure in one can never
    # prevent the other from running.
    mag7_posted_count = 0
    try:
        mag7_posted_count = run_mag7_section(week_label)
    except Exception as e:
        log(f"Mag 7 section raised an unexpected exception: {type(e).__name__}: {e} — "
            f"continuing to the SPY/QQQ/IWM section regardless.")

    core_posted = False
    try:
        core_posted = run_core_section(week_label)
    except Exception as e:
        log(f"SPY/QQQ/IWM section raised an unexpected exception: {type(e).__name__}: {e}")

    log("=" * 60)
    log(f"RUN SUMMARY: Mag 7 {mag7_posted_count}/{len(MAG7_TICKERS)} posted \u00b7 "
        f"SPY/QQQ/IWM {'posted' if core_posted else 'FAILED'}")
    log("=" * 60)

    if mag7_posted_count == 0 and not core_posted:
        log("FATAL: both sections failed completely this run.")
        sys.exit(1)


if __name__ == "__main__":
    main()