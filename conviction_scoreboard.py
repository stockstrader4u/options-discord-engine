"""
conviction_scoreboard.py -- does "Very High" actually beat "High"?

For each rule-based conviction tier (conviction.py), over the trailing 7 days AND cumulatively since the
tiers went live: n, median peak %, % reaching +50%, % that hit +50% before -50%, % up at last.
Every rate prints next to its n, and no tier verdict is offered below MIN_N_FOR_VERDICT flows.

Inputs are flow_option_outcomes (written by weekly_recap.build_weekly_recap) joined to flow_events for the
tier. Only rows with conviction_rule_version = 1 count: older rows carry the score-derived label, which
means something different. Run the weekly recap (or `python conviction_scoreboard.py --refresh`) first so
the outcomes table is current.

    python conviction_scoreboard.py
    python conviction_scoreboard.py --refresh        # recompute the last recap window, then print
"""

from __future__ import annotations

import statistics
import sys
from datetime import datetime, timedelta, timezone

TIERS = ("Very High", "High", "Standard")
MIN_N_FOR_VERDICT = 100


def cumulative_rows(since: str | None = None, until: str | None = None, expired_only: bool = False) -> list[dict]:
    """Outcome rows for rule-based-tier flows that had option bars, optionally limited to alerted_at in
    [since, until) (ISO 'YYYY-MM-DD HH:MM:SS' UTC strings). expired_only keeps only contracts that have settled,
    so "up at last" is a final result, not a mid-flight price. Fields: conviction_label, high_pct, last_pct, hit."""
    from db import db_cursor, ensure_conviction_schema, _POSTGRES
    ensure_conviction_schema()
    p = "%s" if _POSTGRES else "?"
    true_ = "TRUE" if _POSTGRES else "1"
    where = [f"e.conviction_rule_version = 1", f"e.was_published = {true_}", f"o.bars_ok = {true_}"]
    if expired_only:
        where.append(f"o.expired = {true_}")
    params: list = []
    if since:
        where.append(f"o.alerted_at >= {p}")
        params.append(since)
    if until:
        where.append(f"o.alerted_at < {p}")
        params.append(until)
    sql = f"""
        SELECT e.conviction_label, o.high_pct, o.last_pct, o.hit_50_before_minus_50 AS hit
        FROM flow_option_outcomes o JOIN flow_events e ON e.id = o.flow_event_id
        WHERE {' AND '.join(where)}"""
    with db_cursor() as (conn, cur):
        cur.execute(sql, tuple(params))
        return [dict(r) if _POSTGRES else dict(zip(("conviction_label", "high_pct", "last_pct", "hit"), r))
                for r in cur.fetchall()]


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "n_reach_50": 0, "n_hit_50_first": 0, "n_up_last": 0,
                "median_peak": None, "pct_reach_50": None, "pct_hit_50_first": None, "pct_up_last": None}
    return {
        "n": n,
        "n_reach_50": sum(r["high_pct"] >= 50 for r in rows),
        "n_hit_50_first": sum(r["hit"] == "yes" for r in rows),
        "n_up_last": sum(r["last_pct"] > 0 for r in rows),
        "median_peak": statistics.median(r["high_pct"] for r in rows),
        "pct_reach_50": sum(r["high_pct"] >= 50 for r in rows) / n * 100,
        "pct_hit_50_first": sum(r["hit"] == "yes" for r in rows) / n * 100,
        "pct_up_last": sum(r["last_pct"] > 0 for r in rows) / n * 100,
    }


def _fmt(s: dict) -> str:
    if not s["n"]:
        return "n=0"
    return (f"n={s['n']:<4} median peak {s['median_peak']:+.0f}% | +50% reached {s['pct_reach_50']:.0f}% (n={s['n']}) | "
            f"+50% before -50%: {s['pct_hit_50_first']:.0f}% (n={s['n']}) | up at last {s['pct_up_last']:.0f}% (n={s['n']})")


def main(argv: list[str]) -> int:
    if "--refresh" in argv:
        import os
        from dotenv import load_dotenv
        load_dotenv()
        from weekly_recap import build_weekly_recap, alpaca_configured
        if not alpaca_configured():
            print("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY not set -- cannot refresh outcomes.")
            return 1
        build_weekly_recap(os.getenv("FINNHUB_API_KEY", ""))

    from db import db_cursor, ensure_conviction_schema, _POSTGRES
    ensure_conviction_schema()
    with db_cursor() as (conn, cur):
        cur.execute("SELECT MIN(ingested_at) AS d FROM flow_events WHERE conviction_rule_version = 1")
        row = cur.fetchone()
        first = (row["d"] if _POSTGRES else row[0]) if row else None
    if not first:
        print("No flows carry a rule-based conviction tier yet (conviction_rule_version = 1). Nothing to score.")
        return 0

    now = datetime.now(timezone.utc)
    week_since = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    print(f"Rule-based tiers live since {str(first)[:10]}. Option outcomes from flow_option_outcomes "
          f"(only flows the recap has priced).\n")
    for title, since in (("TRAILING 7 DAYS", week_since), ("CUMULATIVE SINCE LIVE", None)):
        rows = cumulative_rows(since=since)
        print(f"== {title} ==")
        for tier in TIERS:
            s = summarize([r for r in rows if r["conviction_label"] == tier])
            note = ""
            if s["n"] and s["n"] < MIN_N_FOR_VERDICT:
                note = f"   [n<{MIN_N_FOR_VERDICT}: no verdict]"
            print(f"  {tier:<10} {_fmt(s)}{note}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
