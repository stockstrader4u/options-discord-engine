"""
daily_flow_summary.py — "Today's Flow at a Glance" end-of-day recap.

Posted standalone at 4:35pm ET, right after the existing 4:30pm heatmap
job (same scheduler, same cron pattern as weekly_recap_job / heatmap_job
in main.py).

PURPOSE: subscribers see 10-20 individual alerts a day, each presented
with equal visual weight. This recap synthesizes the day into the few
things that actually distinguish one alert from the next — same-ticker
clustering, genuine Vol/OI outliers (fresh positioning vs. routine
churn), and the day's highest-conviction calls. It does NOT recommend a
direction or rate the alerts as good/bad trades — it only describes
patterns in what already posted. Per the engine's standing rule, this
never tells subscribers how to size or place a position, and nothing
here crosses that line.

VOL/OI THRESHOLD: 3x, chosen as the "stood out" cutoff — confirmed with
the operator as the line between routine churn and genuinely fresh
positioning. Hardcoded here (STAND_OUT_VOL_OI_THRESHOLD) rather than
buried in logic, so it's a one-line change if it ever needs tuning.

DATA CAVEAT: vol_oi_ratio is only available for alerts posted AFTER the
add_vol_oi_columns_migration() migration shipped — see db.py for details.
Older rows will have vol_oi_ratio = NULL and are silently excluded from
the "stood out" section (not treated as 0x, which would be misleading).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from db import get_flow_events_for_day

# Vol/OI ratio at or above this is treated as "genuinely fresh
# positioning" rather than routine churn. Confirmed with the operator
# 2026-06-24 against a real day's batch (4 of 16 alerts cleared this bar
# that day: 8.5x, 12.4x, 13.7x, 3.5x).
STAND_OUT_VOL_OI_THRESHOLD = 3.0

DIVIDER = "─────────────────────────"


def build_daily_flow_summary(date_str: str | None = None) -> dict[str, Any]:
    """
    Build the daily flow summary from published flow_events for a single
    day. Returns a dict with the formatted message plus the raw counts,
    so the caller can decide to skip posting on a quiet day (e.g. zero
    alerts) without re-deriving anything.

    Args:
        date_str: ISO date string "YYYY-MM-DD". If None, uses today.

    Returns:
        {
            "message": str | None,   # formatted Discord text, or None if nothing to report
            "total_alerts": int,
            "bullish_count": int,
            "bearish_count": int,
            "date_str": str,
        }
    """
    events = get_flow_events_for_day(date_str)
    total = len(events)

    if total == 0:
        return {
            "message": None,
            "total_alerts": 0,
            "bullish_count": 0,
            "bearish_count": 0,
            "date_str": date_str or "today",
        }

    bullish = [e for e in events if e["sentiment"] == "bullish"]
    bearish = [e for e in events if e["sentiment"] == "bearish"]

    # --- Ticker clustering: same ticker + same direction, 2+ times today ---
    # Grouped by (ticker, sentiment) since a ticker getting both bullish
    # AND bearish flow today is a different, more ambiguous signal than
    # one-directional clustering — worth keeping separate rather than
    # just counting raw ticker frequency.
    cluster_counts: dict[tuple[str, str], int] = defaultdict(int)

    for e in events:
        key = (e["ticker"], e["sentiment"])
        cluster_counts[key] += 1

    # Heaviest cluster = highest count, ties broken by ticker name for
    # stable/deterministic output rather than dict-ordering luck.
    heaviest_cluster = None
    if cluster_counts:
        heaviest_key = max(cluster_counts.items(), key=lambda kv: (kv[1], kv[0][0]))[0]
        heaviest_count = cluster_counts[heaviest_key]
        if heaviest_count >= 2:  # only worth calling out if it's an actual pattern
            heaviest_cluster = {
                "ticker": heaviest_key[0],
                "sentiment": heaviest_key[1],
                "count": heaviest_count,
            }

    # --- Vol/OI outliers: ratio >= threshold, ratio must be a real number ---
    outliers = [
        e for e in events
        if e.get("vol_oi_ratio") is not None and e["vol_oi_ratio"] >= STAND_OUT_VOL_OI_THRESHOLD
    ]
    outliers.sort(key=lambda e: e["vol_oi_ratio"], reverse=True)

    # --- Highest conviction: top score, ties included (not just first) ---
    top_score = max(e["score"] for e in events)
    top_alerts = [e for e in events if e["score"] == top_score]

    # --- Build the message ---
    lines = [
        DIVIDER,
        "📊 Today's Flow at a Glance",
        "",
        f"{total} {'alert' if total == 1 else 'alerts'} · {len(bullish)} bullish · {len(bearish)} bearish",
        "",
    ]

    if heaviest_cluster:
        direction_word = "bearish" if heaviest_cluster["sentiment"] == "bearish" else "bullish"
        style_word = "hedge" if heaviest_cluster["sentiment"] == "bearish" else "speculative"
        lines.append(
            f"🎯 Heaviest activity: {heaviest_cluster['ticker']} "
            f"({heaviest_cluster['count']} {direction_word} {style_word} alerts today)"
        )
        lines.append("   — same-direction flow, not a single new signal")
        lines.append("")

    if outliers:
        lines.append(
            f"📈 Stood out from the crowd (Vol/OI ≥{STAND_OUT_VOL_OI_THRESHOLD:.0f}x — "
            f"real fresh positioning, not routine churn):"
        )
        for e in outliers[:6]:  # cap the list so a noisy day doesn't run long
            contract_short = _short_contract(e["contract"])
            lines.append(f"   • {e['ticker']} {contract_short} — {e['vol_oi_ratio']:.1f}x · Score {e['score']}")
        lines.append("")

    if top_alerts:
        names_list = [f"{e['ticker']} {_short_contract(e['contract'])}" for e in top_alerts[:3]]
        if len(names_list) == 1:
            names = names_list[0]
        elif len(names_list) == 2:
            names = " and ".join(names_list)
        else:
            names = ", ".join(names_list[:-1]) + f", and {names_list[-1]}"
        lines.append(f"🏆 Highest conviction: {names} (Score {top_score})")

    lines.append(DIVIDER)

    message = "\n".join(lines)

    return {
        "message": message,
        "total_alerts": total,
        "bullish_count": len(bullish),
        "bearish_count": len(bearish),
        "date_str": date_str or "today",
    }


def _short_contract(contract: str) -> str:
    """
    Extract just the strike+type from a full contract string, e.g.
    "CRWV 2026-06-26 94P" -> "$94P". Falls back to the raw contract
    string if the format isn't recognised, rather than raising.
    """
    parts = contract.split()
    if len(parts) >= 3:
        strike_type = parts[-1]
        try:
            num = strike_type[:-1]
            pc = strike_type[-1].upper()
            return f"${float(num):.0f}{pc}"
        except (ValueError, IndexError):
            return strike_type
    return contract


async def post_daily_flow_summary(webhook_url: str, message: str) -> bool:
    """
    Post the daily flow summary to Discord. Separate function (not
    reusing post_to_discord from main.py) to keep this module
    self-contained and independently testable, same pattern as
    weekly_recap.py's post_weekly_recap().
    """
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.post(webhook_url, json={"content": message})
    return response.status_code in (200, 204)
