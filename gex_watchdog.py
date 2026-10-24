"""
gex_watchdog.py -- alert the ops webhook if today's GEX/VEX run left no
snapshot in Postgres. gex_vex_unified_daily.py saves a gex_vex_snapshots
row per ticker on every successful data fetch, so a missing SPY row on a
trading day means the job never ran or died early.

Schedule on Railway at 10:50 ET weekdays (cron `50 14 * * 1-5` UTC while
EDT; `50 15 * * 1-5` in winter -- use both and rely on the ET check below).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import gex_vex_history
from market_hours import market_closed_reason
from ops_alert import post_ops_alert

ET = ZoneInfo("America/New_York")


def main():
    now = datetime.now(ET)
    if now.hour * 60 + now.minute < 10 * 60 + 45:
        print("Before 10:45 ET, too early to judge, skipping.")
        return
    reason = market_closed_reason()
    if reason:
        print(f"Market closed ({reason}), nothing expected.")
        return
    try:
        conn = gex_vex_history._get_connection()
        rows = conn.run("SELECT COUNT(*) FROM gex_vex_snapshots WHERE ticker = 'SPY' AND snapshot_date = :d",
                        d=now.date())
        conn.close()
    except Exception as e:
        post_ops_alert(f"⚠️ GEX watchdog could not check the database: {e}")
        return
    if rows[0][0] == 0:
        post_ops_alert(f"\U0001F6A8 **No GEX/VEX snapshot for {now.date()}** as of {now:%H:%M} ET. "
                       f"The 10:30 ET job did not run or failed. Fallback: run_gex_local.ps1")
    else:
        print("GEX snapshot present for today, OK.")


if __name__ == "__main__":
    main()
