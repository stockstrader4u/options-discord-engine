"""
gex_vex_daily.py — Production daily GEX/VEX post for BlueMoonTrades
subscribers. Runs once per trigger (Railway cron, 3:45pm ET Mon-Fri),
computes GEX/VEX for SPY/QQQ/IWM via Alpaca, and posts a compact
Discord embed + rendered dashboard card to the real subscriber webhook.

Required env vars (set on Railway, not locally):
  ALPACA_API_KEY_ID
  ALPACA_API_SECRET_KEY
  OPENROUTER_API_KEY      (optional — falls back to data-derived
                            watch lines if unset, never blank)
  GEX_DISCORD_WEBHOOK     (the REAL subscriber-facing webhook — a
                            DIFFERENT env var name than the test
                            script's GEX_TEST_WEBHOOK_URL, so test and
                            production can never accidentally collide
                            even if both happen to be set at once)

Deploy gex_vex.py alongside this file.

PRODUCTION HARDENING vs. the test script (test_gex_vex_live.py):
  - If ALL tickers fail, the run exits WITHOUT posting anything at all,
    rather than publishing a broken/near-empty snapshot to subscribers.
  - If SOME tickers fail, it still posts with the working ones (errored
    tickers show as "skipped" in the Regime field, same as tested) —
    partial real data beats no post, but total failure must not post
    silently degraded content.
  - Every run logs a clear per-ticker status line and an overall
    success/failure summary, formatted for Railway's log viewer.
  - Exits with a non-zero code on any failure, so Railway's own
    monitoring can flag a bad run.

IMPORT SAFETY (added 2026-07-31): a corrupted/duplicated gex_vex.py
(confirmed in production — the file had gotten pasted twice back-to-
back during a repo update, producing a SyntaxError at the seam) caused
this container to crash immediately at "import gex_vex" with nothing
more than a bare traceback pointing at this line — no indication of
WHY the import failed unless you already knew to go read gex_vex.py's
own error. The import is now wrapped so a future import failure of any
kind (syntax error, missing dependency, etc.) logs a clear, loud
diagnostic via this module's own log() format BEFORE the traceback,
so it's immediately visible in Railway's log viewer without having to
cross-reference a separate file.
"""

import os
import sys
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo

import requests


def log(msg: str):
    print(f"[GEX-DAILY] {msg}", flush=True)


try:
    import gex_vex
except Exception as e:
    log(f"FATAL: failed to import gex_vex.py — {type(e).__name__}: {e}")
    log("This usually means gex_vex.py has a syntax error or was pasted/")
    log("uploaded incorrectly (e.g. duplicated content, truncated file).")
    log("Check the file directly before assuming this is a data/API issue.")
    raise

WEBHOOK_URL = os.environ.get("GEX_DISCORD_WEBHOOK", "")
TICKERS = ["SPY", "QQQ", "IWM"]
OUT_PATH = "gex_dashboard.png"
ET = ZoneInfo("America/New_York")


def post_embed_to_discord(webhook_url: str, embed: dict) -> bool:
    try:
        r = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
        ok = r.status_code in (200, 204)
        if not ok:
            log(f"Embed post FAILED: {r.status_code} {r.text[:300]}")
        return ok
    except Exception as e:
        log(f"Embed post exception: {e}")
        return False


def post_image_to_discord(webhook_url: str, image_path: str, caption: str = "") -> bool:
    try:
        with open(image_path, "rb") as f:
            files = {"file": (os.path.basename(image_path), f, "image/png")}
            data = {"content": caption}
            r = requests.post(webhook_url, data=data, files=files, timeout=30)
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


def main():
    et_now = datetime.now(ET)
    log(f"Starting run at {et_now.isoformat()}")

    if not WEBHOOK_URL:
        log("FATAL: GEX_DISCORD_WEBHOOK is not set. Exiting without posting.")
        sys.exit(1)

    results = []
    for t in TICKERS:
        result = gex_vex.compute_gex_vex(t, expiries=None)
        if "error" in result:
            log(f"{t}: ERROR — {result['error']}")
        else:
            flip = result.get("gamma_flip")
            log(f"{t}: OK — spot=${result['spot']:.2f} "
                f"net_gex={result['net_gex']/1e9:+.2f}B flip={flip}")
            # Same safety-net diagnostic as testing — this doesn't block
            # the post, it just flags a suspicious value in the logs so
            # a future regression is visible instead of silently trusted.
            spot = result.get("spot")
            if flip is not None and spot and abs(flip - spot) / spot > 0.08:
                log(f"  WARNING: {t} gamma flip {flip} is >8% from spot {spot} — "
                    f"worth spot-checking with dump_cumulative_table if this recurs")
        results.append(result)

    valid_count = sum(1 for r in results if "error" not in r)
    if valid_count == 0:
        log("FATAL: all tickers failed — skipping the post entirely rather than "
            "publishing a broken/empty snapshot to subscribers.")
        sys.exit(1)

    if valid_count < len(TICKERS):
        log(f"WARNING: only {valid_count}/{len(TICKERS)} tickers succeeded — "
            f"posting partial results (errored tickers show as skipped).")

    week_label = get_week_label()

    watch_lines = gex_vex.generate_gex_watch_lines(results)
    embed = gex_vex.build_gex_embed(results, week_label, watch_lines)
    gex_vex.render_gex_dashboard_card(results, week_label, OUT_PATH)

    embed_ok = post_embed_to_discord(WEBHOOK_URL, embed)
    image_ok = post_image_to_discord(WEBHOOK_URL, OUT_PATH)

    if embed_ok and image_ok:
        log("Done — both posts succeeded.")
    else:
        log(f"Done with issues — embed_ok={embed_ok} image_ok={image_ok}")
        sys.exit(1)


if __name__ == "__main__":
    main()