"""
flow_heatmap.py — EOD options flow heatmap (image version).

Pulls the day's flow_events (already filtered/scored by the same pipeline
that generates Discord alerts), dedupes repeated contract entries, aggregates
call vs put premium per ticker, and renders a real matplotlib PNG — same
dark-theme visual language as chart_generator.py's build_chart() — posted
to Discord via webhook, same pattern as post_chart().

Usage:
    python flow_heatmap.py                  # yesterday's data, posts to Discord
    python flow_heatmap.py --date 2026-06-18 --top 20
    python flow_heatmap.py --no-post --out heatmap.png   # build only, don't post
"""

from __future__ import annotations

import argparse
import io
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import requests

from db import db_cursor, is_postgres

logger = logging.getLogger("options-discord-engine")
ET = ZoneInfo("America/New_York")

HEATMAP_WEBHOOK_URL = os.environ.get("HEATMAP_WEBHOOK_URL") or os.environ.get("CHART_WEBHOOK_URL")

# Match chart_generator.py's palette
BG      = "#0d1117"
GRID    = "#1e2530"
UP      = "#26a69a"   # calls
DOWN    = "#ef5350"   # puts
TEXT    = "#c9d1d9"
SUBTEXT = "#8b949e"


# ---------------------------------------------------------------------------
# Data pull + dedup
# ---------------------------------------------------------------------------

def fetch_deduped_flow(date_str: str) -> list[dict]:
    """
    Pull flow_events for the given date, deduped by (ticker, contract,
    sentiment) keeping max premium seen — collapses the same trade
    resurfacing across multiple poll cycles into one entry.
    """
    if is_postgres():
        sql = """
            SELECT ticker, contract, sentiment, MAX(premium) AS premium
            FROM flow_events
            WHERE ingested_at >= %s::date
              AND ingested_at < (%s::date + INTERVAL '1 day')
            GROUP BY ticker, contract, sentiment
            ORDER BY premium DESC
        """
        params = (date_str, date_str)
    else:
        sql = """
            SELECT ticker, contract, sentiment, MAX(premium) AS premium
            FROM flow_events
            WHERE date(ingested_at) = date(?)
            GROUP BY ticker, contract, sentiment
            ORDER BY premium DESC
        """
        params = (date_str,)

    with db_cursor() as (conn, cur):
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_by_ticker(rows: list[dict], top_n: int = 20) -> list[dict]:
    totals = defaultdict(lambda: {"call_premium": 0, "put_premium": 0})

    for row in rows:
        ticker = row["ticker"]
        sentiment = row["sentiment"]
        premium = row["premium"]
        if sentiment == "bullish":
            totals[ticker]["call_premium"] += premium
        elif sentiment == "bearish":
            totals[ticker]["put_premium"] += premium

    results = []
    for ticker, vals in totals.items():
        call_p = vals["call_premium"]
        put_p = vals["put_premium"]
        total = call_p + put_p
        net = call_p - put_p
        if total == 0:
            bias = "neutral"
        elif net > total * 0.2:
            bias = "bullish"
        elif net < -total * 0.2:
            bias = "bearish"
        else:
            bias = "mixed"
        results.append({
            "ticker": ticker, "call_premium": call_p, "put_premium": put_p,
            "total_premium": total, "net_skew": net, "bias": bias,
        })

    results.sort(key=lambda r: r["total_premium"], reverse=True)
    return results[:top_n]


def fmt_premium(n: int) -> str:
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"${round(n/1000)}K"
    return f"${n}"


# ---------------------------------------------------------------------------
# Chart rendering (matplotlib, matches chart_generator.py's visual language)
# ---------------------------------------------------------------------------

def build_heatmap_chart(ranked: list[dict], date_label: str) -> io.BytesIO:
    """
    Single horizontal stacked bar per ticker (put portion + call portion in
    one bar, put on the left / call on the right), ranked top to bottom by
    total premium. Bias tag per row. Legend at the bottom. Compact spacing.
    """
    n = len(ranked) or 1
    fig_height = 1.05 + 0.42 * n  # tighter header + rows + legend
    fig = plt.figure(figsize=(8.6, fig_height), facecolor=BG)

    header_h = 0.80 / fig_height
    legend_h = 0.40 / fig_height
    ax = fig.add_axes([0.0, legend_h, 1.0, 1.0 - header_h - legend_h])
    ax.set_facecolor(BG)

    rows = ranked[::-1]  # reverse so #1 renders at the top
    y = np.arange(n)
    max_total = max((r["total_premium"] for r in ranked), default=1) or 1

    bias_colors = {
        "bullish": "#2ecc71",
        "bearish": "#e74c3c",
        "mixed":   "#f1c40f",
        "neutral": "#999999",
    }

    bar_h = 0.78
    ax.set_ylim(-0.55, n - 0.45)
    ax.set_xlim(0, 1.0)
    ax.set_yticks([])
    ax.set_xticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    BAR_LEFT = 0.30
    BAR_RIGHT = 0.84

    for i, r in enumerate(rows):
        total = r["total_premium"]
        bar_full_width = BAR_RIGHT - BAR_LEFT
        width = bar_full_width * (total / max_total) if max_total else 0
        put_frac = (r["put_premium"] / total) if total else 0
        call_frac = 1 - put_frac
        put_w = width * put_frac
        call_w = width * call_frac

        ax.barh(y[i], bar_full_width, left=BAR_LEFT, height=bar_h, color="#1a1d24", zorder=1)
        ax.barh(y[i], put_w, left=BAR_LEFT, height=bar_h, color=DOWN, zorder=3)
        ax.barh(y[i], call_w, left=BAR_LEFT + put_w, height=bar_h, color=UP, zorder=3)

    for i, r in enumerate(rows):
        rank_num = n - i
        ax.text(0.012, y[i], f"{rank_num}", va="center", ha="left",
                 color="#4a4f5a", fontsize=8.5, fontweight="bold")
        ax.text(0.045, y[i], r["ticker"], va="center", ha="left",
                 color="#ffffff", fontsize=11.5, fontweight="bold")
        bias = r["bias"]
        ax.text(0.165, y[i], bias.upper(), va="center", ha="left",
                 color=bias_colors[bias], fontsize=7.5, fontweight="bold",
                 bbox=dict(boxstyle="round,pad=0.3", facecolor=bias_colors[bias],
                            alpha=0.15, edgecolor="none"))
        ax.text(0.852, y[i], fmt_premium(r["total_premium"]), va="center", ha="left",
                 color="#d4d7dc", fontsize=10, fontweight="bold")

    # Header — own axes so it never collides with row content, tight to subheader
    header_ax = fig.add_axes([0.0, 1.0 - header_h, 1.0, header_h])
    header_ax.set_facecolor(BG)
    header_ax.axis("off")
    header_ax.text(0.035, 0.78, f"Options Flow Heatmap — {date_label}",
                    color=TEXT, fontsize=13.5, fontweight="bold", va="center", ha="left")
    header_ax.text(0.035, 0.42, "Top tickers by total options premium · Calls vs Puts",
                    color=SUBTEXT, fontsize=8.5, va="center", ha="left")
    header_ax.text(0.035, 0.10, "BMT's custom watchlist · filtered through BMT's conviction scoring rules",
                    color="#5d6373", fontsize=7.5, va="center", ha="left", style="italic")

    # Legend — own axes at the bottom
    legend_ax = fig.add_axes([0.0, 0.0, 1.0, legend_h])
    legend_ax.set_facecolor(BG)
    legend_ax.axis("off")
    legend_ax.add_patch(mpatches.Rectangle((0.03, 0.32), 0.016, 0.34, color=UP, transform=legend_ax.transAxes))
    legend_ax.text(0.055, 0.5, "Call Premium", color=TEXT, fontsize=8.5, va="center", ha="left")
    legend_ax.add_patch(mpatches.Rectangle((0.20, 0.32), 0.016, 0.34, color=DOWN, transform=legend_ax.transAxes))
    legend_ax.text(0.225, 0.5, "Put Premium", color=TEXT, fontsize=8.5, va="center", ha="left")
    legend_ax.text(0.97, 0.5, "BlueMoonTrades", color="#4d8fd4", fontsize=8.5,
                    fontweight="bold", va="center", ha="right")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=BG, edgecolor="none")
    buf.seek(0)
    plt.close(fig)
    return buf


# ---------------------------------------------------------------------------
# Discord posting (same pattern as chart_generator.py's post_chart)
# ---------------------------------------------------------------------------

def post_heatmap(chart_buf: io.BytesIO, date_label: str, ranked: list[dict],
                  webhook_url: str | None = None) -> bool:
    url = webhook_url or HEATMAP_WEBHOOK_URL
    if not url:
        print("[WARN] No webhook URL configured (HEATMAP_WEBHOOK_URL / CHART_WEBHOOK_URL)")
        return False

    top_ticker = ranked[0]["ticker"] if ranked else "—"
    top_total = fmt_premium(ranked[0]["total_premium"]) if ranked else "$0"

    caption = (
        f"🔥 **Options Flow Heatmap — {date_label}**\n"
        f"Biggest mover: **${top_ticker}** ({top_total} total premium)"
    )

    files = {"file": (f"flow_heatmap_{date_label.replace(' ', '_')}.png", chart_buf, "image/png")}
    data = {"content": caption}
    resp = requests.post(url, files=files, data=data, timeout=30)
    if resp.status_code in (200, 204):
        print("[OK] Heatmap posted")
        return True
    print(f"[WARN] Discord {resp.status_code}: {resp.text[:200]}")
    return False


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_heatmap(date_str: str | None = None, top_n: int = 20):
    """
    Build the heatmap for a given date. Defaults to today (Eastern Time) —
    this is meant to run at 4:30pm ET, right after market close, covering
    the day that just ended.
    """
    if date_str is None:
        date_str = datetime.now(ET).strftime("%Y-%m-%d")
    raw = fetch_deduped_flow(date_str)
    ranked = aggregate_by_ticker(raw, top_n=top_n)
    date_label = datetime.strptime(date_str, "%Y-%m-%d").strftime("%b %d, %Y").upper()
    chart_buf = build_heatmap_chart(ranked, date_label)
    return chart_buf, ranked, date_label


async def heatmap_job():
    """
    Scheduled job — fires daily at 4:30pm ET, builds today's heatmap and
    posts it to Discord. Registered in main.py's lifespan alongside
    scheduled_poll_job and weekly_recap_job. Logs every outcome, including
    the "no flow today" case, so it's debuggable from Railway logs alone.
    """
    if not HEATMAP_WEBHOOK_URL:
        logger.warning("heatmap_skipped reason=webhook_url_missing")
        return

    try:
        chart_buf, ranked, date_label = build_heatmap()
    except Exception as e:
        logger.exception("heatmap_build_failed error=%s", str(e))
        return

    logger.info("heatmap_built date=%s tickers=%d", date_label, len(ranked))

    if not ranked:
        logger.info("heatmap_skipped reason=no_qualifying_flow date=%s", date_label)
        return

    try:
        posted = post_heatmap(chart_buf, date_label, ranked)
    except Exception as e:
        logger.exception("heatmap_post_failed error=%s", str(e))
        return

    if posted:
        logger.info("heatmap_posted ok date=%s top_ticker=%s", date_label, ranked[0]["ticker"])
    else:
        logger.warning("heatmap_post_failed reason=non_2xx_response date=%s", date_label)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build/post the EOD options flow heatmap")
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD, defaults to yesterday")
    parser.add_argument("--top", type=int, default=20, help="Top N tickers by premium")
    parser.add_argument("--out", type=str, default="heatmap_output.png", help="Output PNG path (when --no-post)")
    parser.add_argument("--no-post", action="store_true", help="Build only, skip Discord post")
    args = parser.parse_args()

    chart_buf, ranked, date_label = build_heatmap(date_str=args.date, top_n=args.top)

    print(f"Built heatmap with {len(ranked)} tickers for {date_label}")
    for i, r in enumerate(ranked, start=1):
        print(f"{i:>2}. {r['ticker']:<6} calls={fmt_premium(r['call_premium']):>8}  "
              f"puts={fmt_premium(r['put_premium']):>8}  total={fmt_premium(r['total_premium']):>8}  "
              f"bias={r['bias']}")

    if args.no_post:
        with open(args.out, "wb") as f:
            f.write(chart_buf.getvalue())
        print(f"Saved PNG -> {args.out}")
    else:
        post_heatmap(chart_buf, date_label, ranked)
