"""
weekly_recap.py — Friday end-of-week alert performance recap.

Posts one consolidated Discord message every Friday at market close (or
the last real trading day if Friday is a holiday) summarizing how the
past 5 trading days' alerts performed, using underlying spot price
movement (high/low/close vs entry spot) — not option premium.

Scope, by design:
  - Spot price movement only, no premium/Greeks estimation
  - Direction-aware win/loss: a put profiting from a price drop is a win
  - Top 3 winners and top 3 losers shown in full detail; everything else
    folded into aggregate stats only, so the card never grows unbounded
    even on a busy week
  - Every quote failure is shown explicitly ("no data"), never silently
    dropped
  - Does not touch scoring, gating, dedup, or the entry-card formatter —
    this is a separate, additive feature reading from flow_events after
    the fact

Does NOT track entries beyond this 5-trading-day window — there is no
day-to-day carryover. Each Friday's recap is self-contained.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

import httpx

from market_hours import most_recent_trading_day, trading_days_before
from db import get_flow_events_in_range

logger = logging.getLogger("options-weekly-recap")

FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
TRADING_DAYS_IN_WINDOW = 5
TOP_N_HIGHLIGHTS = 3


class QuoteUnavailable(Exception):
    """Raised when a ticker's current quote can't be fetched. Callers
    surface this as an explicit '⚠️ no data' line — never silently
    dropped, per design."""
    def __init__(self, ticker: str, reason: str):
        self.ticker = ticker
        self.reason = reason
        super().__init__(f"{ticker}: {reason}")


# ---------------------------------------------------------------------------
# Finnhub quote fetch
# ---------------------------------------------------------------------------

def get_quote(ticker: str, api_key: str, timeout: float = 10.0) -> dict[str, float]:
    """
    Fetch current/high/low quote for a ticker from Finnhub.

    Returns {"current": float, "high": float, "low": float}.
    Raises QuoteUnavailable on any failure.
    """
    if not api_key:
        raise QuoteUnavailable(ticker, "FINNHUB_API_KEY not configured")

    try:
        resp = httpx.get(
            FINNHUB_QUOTE_URL,
            params={"symbol": ticker, "token": api_key},
            timeout=timeout,
        )
    except httpx.RequestError as e:
        raise QuoteUnavailable(ticker, f"network error: {e}")

    if resp.status_code == 429:
        raise QuoteUnavailable(ticker, "rate limited (429)")
    if resp.status_code != 200:
        raise QuoteUnavailable(ticker, f"HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError:
        raise QuoteUnavailable(ticker, "invalid JSON response")

    current = data.get("c")
    high = data.get("h")
    low = data.get("l")

    # Finnhub returns all-zero fields for an unrecognized/delisted symbol
    # rather than an error status — this is the real "bad symbol" failure
    # mode and needs an explicit check, not just a None check.
    if current is None or current == 0:
        raise QuoteUnavailable(ticker, "no data returned (symbol may be invalid)")

    return {"current": current, "high": high, "low": low}


# ---------------------------------------------------------------------------
# Window resolution
# ---------------------------------------------------------------------------

def resolve_recap_window(as_of: date | None = None) -> tuple[date, date]:
    """
    Resolve the (start_date, end_date) window for the recap.

    end_date is the most recent real trading day on or before as_of
    (defaults to today) — this is what makes the Friday-is-a-holiday case
    work correctly: if as_of falls on a holiday, end_date rolls back to
    the last actual trading day rather than producing an empty/wrong window.

    start_date is TRADING_DAYS_IN_WINDOW trading days before end_date.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc).date()
    end_date = most_recent_trading_day(as_of)
    start_date = trading_days_before(end_date, TRADING_DAYS_IN_WINDOW)
    return start_date, end_date


# ---------------------------------------------------------------------------
# Win/loss + per-alert result computation
# ---------------------------------------------------------------------------

def compute_alert_result(row: dict[str, Any], quote: dict[str, float]) -> dict[str, Any]:
    """
    Given a flow_events row and its current quote, compute the
    direction-aware result for display.

    Win definition: a CALL wins if close > entry spot; a PUT wins if
    close < entry spot. This matches the green/red logic used elsewhere —
    a put profiting from a price drop is a win, not a loss.
    """
    entry_spot = row.get("spot_price")
    close = quote["current"]
    put_call = (row.get("put_call") or "").upper()

    if entry_spot is None or entry_spot == 0:
        pct_move = None
        is_win = None
    else:
        pct_move = (close - entry_spot) / entry_spot * 100
        if put_call == "PUT":
            is_win = close < entry_spot
            # For display, flip the sign for puts so "+X%" always means
            # "moved favorably for this position", matching the win flag.
            display_pct = -pct_move
        else:
            is_win = close > entry_spot
            display_pct = pct_move

    return {
        "ticker": row["ticker"],
        "contract": row["contract"],
        "put_call": put_call,
        "entry_spot": entry_spot,
        "high": quote.get("high"),
        "low": quote.get("low"),
        "close": close,
        "pct_move_display": display_pct if entry_spot else None,
        "is_win": is_win,
        "score": row.get("score"),
        "alert_hash": row.get("alert_hash"),
        "quote_failed": False,
    }


def compute_failed_result(row: dict[str, Any], reason: str) -> dict[str, Any]:
    """Result entry for a row whose quote lookup failed — shown
    explicitly in the card rather than dropped."""
    return {
        "ticker": row["ticker"],
        "contract": row["contract"],
        "put_call": (row.get("put_call") or "").upper(),
        "entry_spot": row.get("spot_price"),
        "high": None,
        "low": None,
        "close": None,
        "pct_move_display": None,
        "is_win": None,
        "score": row.get("score"),
        "alert_hash": row.get("alert_hash"),
        "quote_failed": True,
        "fail_reason": reason,
    }


# ---------------------------------------------------------------------------
# Recap assembly
# ---------------------------------------------------------------------------

def build_weekly_recap(api_key: str, as_of: date | None = None) -> dict[str, Any]:
    """
    Pull this window's flow_events, fetch quotes, compute results, and
    return a structured recap (results, highlights, stats, message text).

    Does not post to Discord — pure computation, so it can be tested and
    previewed independently of any network side effects.
    """
    start_date, end_date = resolve_recap_window(as_of)
    rows = get_flow_events_in_range(start_date.isoformat(), end_date.isoformat())

    # One quote call per unique ticker, not per alert — avoids redundant
    # calls when the same ticker fired multiple times in the window.
    unique_tickers = sorted({r["ticker"] for r in rows})
    quotes: dict[str, dict] = {}
    quote_failures: dict[str, str] = {}

    for ticker in unique_tickers:
        try:
            quotes[ticker] = get_quote(ticker, api_key)
        except QuoteUnavailable as e:
            quote_failures[ticker] = e.reason
            logger.warning("weekly_recap quote_failed ticker=%s reason=%s", ticker, e.reason)

    results = []
    for row in rows:
        ticker = row["ticker"]
        if ticker in quotes:
            results.append(compute_alert_result(row, quotes[ticker]))
        else:
            results.append(compute_failed_result(row, quote_failures.get(ticker, "unknown error")))

    # Split into resolved (has a real pct_move) vs failed, for ranking
    resolved = [r for r in results if not r["quote_failed"]]
    failed = [r for r in results if r["quote_failed"]]

    resolved_sorted = sorted(resolved, key=lambda r: r["pct_move_display"], reverse=True)
    winners = resolved_sorted[:TOP_N_HIGHLIGHTS]
    losers = resolved_sorted[-TOP_N_HIGHLIGHTS:] if resolved_sorted else []
    # avoid double-listing the same alerts as both winners and losers on a
    # very small week (e.g. with only 2 results, winners[:3] and
    # losers[-3:] would otherwise both grab the same rows)
    highlighted_hashes = {r["alert_hash"] for r in winners} | {r["alert_hash"] for r in losers}
    remainder = [r for r in resolved_sorted if r["alert_hash"] not in highlighted_hashes]

    total = len(results)
    win_count = sum(1 for r in resolved if r["is_win"])
    loss_count = sum(1 for r in resolved if r["is_win"] is False)
    win_rate = (win_count / len(resolved) * 100) if resolved else None
    avg_move = (sum(r["pct_move_display"] for r in resolved) / len(resolved)) if resolved else None

    return {
        "start_date": start_date,
        "end_date": end_date,
        "total_alerts": total,
        "resolved_count": len(resolved),
        "failed_count": len(failed),
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": win_rate,
        "avg_move": avg_move,
        "winners": winners,
        "losers": losers,
        "remainder": remainder,
        "failed": failed,
        "message": format_weekly_recap_message(
            start_date, end_date, total, win_rate, avg_move,
            winners, losers, remainder, failed,
        ),
    }


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _fmt_result_line(r: dict[str, Any]) -> str:
    pc = "C" if r["put_call"] == "CALL" else "P" if r["put_call"] == "PUT" else "?"
    emoji = "🟢" if r["is_win"] else "🔴" if r["is_win"] is False else "⚪"
    return (
        f"{emoji} {r['ticker']} {pc} · entry spot ${r['entry_spot']:,.2f} "
        f"→ High: ${r['high']:,.2f} · Low: ${r['low']:,.2f} · Close: ${r['close']:,.2f} "
        f"({r['pct_move_display']:+.1f}%)"
    )


def _fmt_failed_line(r: dict[str, Any]) -> str:
    return f"⚠️ {r['ticker']} · no current quote available ({r.get('fail_reason', 'unknown')})"


def format_weekly_recap_message(
    start_date: date,
    end_date: date,
    total_alerts: int,
    win_rate: float | None,
    avg_move: float | None,
    winners: list[dict],
    losers: list[dict],
    remainder: list[dict],
    failed: list[dict],
) -> str:
    date_range = f"{start_date.strftime('%b %d')}–{end_date.strftime('%b %d')}"
    win_rate_str = f"{win_rate:.0f}%" if win_rate is not None else "N/A"
    avg_move_str = f"{avg_move:+.1f}%" if avg_move is not None else "N/A"

    lines = [
        "─────────────────────────",
        f"📊 **Weekly Recap — {date_range}**",
        "",
        f"{total_alerts} alerts tracked · {win_rate_str} win rate · {avg_move_str} avg move",
        "",
    ]

    # Only label a bucket "Winners"/"Laggards" if it actually contains a
    # favorable/unfavorable move — on an all-losing week, "winners" would
    # otherwise just be the least-bad losses mislabeled as wins.
    actual_winners = [r for r in winners if r["pct_move_display"] is not None and r["pct_move_display"] > 0]
    actual_losers = [r for r in losers if r["pct_move_display"] is not None and r["pct_move_display"] < 0]

    if actual_winners:
        lines.append("**🏆 Top Winners**")
        for r in actual_winners:
            lines.append(_fmt_result_line(r))
        lines.append("")

    if actual_losers:
        lines.append("**📉 Biggest Laggards**")
        for r in actual_losers:
            lines.append(_fmt_result_line(r))
        lines.append("")

    if remainder:
        rem_wins = sum(1 for r in remainder if r["is_win"])
        rem_avg = sum(r["pct_move_display"] for r in remainder) / len(remainder)
        lines.append(
            f"**+ {len(remainder)} other alerts this week** — "
            f"{rem_wins}/{len(remainder)} favorable, avg {rem_avg:+.1f}%"
        )
        lines.append("")

    if failed:
        for r in failed:
            lines.append(_fmt_failed_line(r))
        lines.append("")

    # trim trailing blank line
    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

async def post_weekly_recap(webhook_url: str, message: str) -> bool:
    """Post the recap message to Discord. Separate from build_weekly_recap()
    so the message can be built and previewed without any network call."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(webhook_url, json={"content": message})
    return resp.status_code in (200, 204)
