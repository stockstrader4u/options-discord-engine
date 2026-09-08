"""
weekly_recap.py — Friday end-of-week alert performance recap.

Posts one consolidated Discord message every Friday at market close (or
the last real trading day if Friday is a holiday) summarizing how the
past 5 trading days' alerts performed, using underlying spot price
movement (high/low/close vs entry spot) — not option premium.

Scope, by design:
  - Spot price movement only, no premium/Greeks estimation
  - Direction-aware win/loss: a put profiting from a price drop is a win
  - Top 10 winners and top 10 losers shown in full detail; everything
    else folded into aggregate stats only, so the card never grows
    unbounded even on a busy week
  - Every quote failure is shown explicitly ("no data"), never silently
    dropped
  - Does not touch scoring, gating, dedup, or the entry-card formatter —
    this is a separate, additive feature reading from flow_events after
    the fact

Does NOT track entries beyond this 5-trading-day window — there is no
day-to-day carryover. Each Friday's recap is self-contained.

── VISUAL REDESIGN (2026-09-07) ─────────────────────────────────────────────
Per direct user feedback: the plain-text Discord message gave "very
little value" — only the top 3 winners/losers were shown in detail,
with everything else folded into a vague "+21 other alerts this week"
line, and even the shown rows were a dense wall of text.

Changes:
  1. TOP_N_PER_SIDE raised from 3 to 10 per side — with a typical ~25-30
     alert week, this now covers the large majority of alerts in full
     detail (entry/high/low/close), leaving only a handful in the
     aggregate remainder line instead of the bulk of the week.
  2. The recap is now rendered as a PNG card (render_weekly_recap_card())
     instead of a plain-text Discord message — colored left-accent rows
     per alert (green for winners, red for laggards), matching the
     visual language already established across BMT's other dashboards
     (bmt_weekly_insights.py / bmt_weekend_analytics.py's dark palette)
     and the same top-N-highlights row layout already validated with
     the user in er_lotto_report_card.py.
  3. format_weekly_recap_message() is replaced by
     format_weekly_recap_caption() — a single short line (date range,
     total alerts, win rate, avg move) posted as the image's caption,
     since the detail now lives in the card itself rather than in message
     text.
  4. post_weekly_recap() is replaced by post_weekly_recap_image(), which
     posts the rendered PNG + caption instead of a bare text message.

build_weekly_recap() remains PURE COMPUTATION -- no matplotlib, no
network -- so it stays independently testable exactly as before.
Rendering and posting are separate steps, same architectural split the
module already had between build/post.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

import httpx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

from market_hours import most_recent_trading_day, trading_days_before
from db import get_flow_events_in_range

logger = logging.getLogger("options-weekly-recap")

FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
TRADING_DAYS_IN_WINDOW = 5
# VISUAL REDESIGN (2026-09-07): raised from 3 -- see module docstring.
TOP_N_PER_SIDE = 10


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
    return a structured recap (results, highlights, stats, caption).

    Does not post to Discord and does not render any image — pure
    computation, so it can be tested and previewed independently of any
    network or matplotlib side effects. See render_weekly_recap_card()
    for turning this dict into the actual PNG.
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
    winners = resolved_sorted[:TOP_N_PER_SIDE]
    losers = resolved_sorted[-TOP_N_PER_SIDE:] if resolved_sorted else []
    # avoid double-listing the same alerts as both winners and losers on a
    # very small week (e.g. with only 2 results, winners[:10] and
    # losers[-10:] would otherwise both grab the same rows)
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
        "caption": format_weekly_recap_caption(start_date, end_date, total, win_rate, avg_move),
        # HOTFIX (2026-09-07): "message" kept as an alias of "caption",
        # purely so an old caller that hasn't been updated yet
        # (main.py was still doing recap["message"]) degrades to a
        # short plain-text post instead of crashing with a KeyError.
        # Remove once main.py is confirmed updated to use "caption"
        # and render_weekly_recap_card() directly.
        "message": format_weekly_recap_caption(start_date, end_date, total, win_rate, avg_move),
    }


# ---------------------------------------------------------------------------
# Caption (short, posted alongside the image — see render section below
# for where the actual detail now lives)
# ---------------------------------------------------------------------------

def format_weekly_recap_caption(
    start_date: date,
    end_date: date,
    total_alerts: int,
    win_rate: float | None,
    avg_move: float | None,
) -> str:
    date_range = f"{start_date.strftime('%b %d')}\u2013{end_date.strftime('%b %d')}"
    win_rate_str = f"{win_rate:.0f}%" if win_rate is not None else "N/A"
    avg_move_str = f"{avg_move:+.1f}%" if avg_move is not None else "N/A"
    return (f"**Weekly Recap \u2014 {date_range}**   "
            f"{total_alerts} alerts \u00b7 {win_rate_str} win rate \u00b7 {avg_move_str} avg move")


# ---------------------------------------------------------------------------
# Card rendering (2026-09-07 redesign)
# ---------------------------------------------------------------------------

DARK_BG      = "#0a0e1c"
PANEL_BG     = "#12172a"
PANEL_BORDER = "#252c47"
TXT_LIGHT    = "#f2f4f8"
TXT_DIM      = "#8891a7"
GREEN        = "#22d3a8"
RED          = "#ef4444"
GRAY         = "#5b6478"
GOLD         = "#f5a623"
HEADER_FONT  = "DejaVu Sans"
DATA_FONT    = "DejaVu Sans Mono"


def _esc(text: str) -> str:
    """Matplotlib treats a PAIR of unescaped '$' as LaTeX math-mode
    delimiters -- every row here has several dollar amounts, so this is
    not optional. Same fix already applied in er_lotto_report_card.py."""
    return text.replace("$", r"\$")


def _fmt_money(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "N/A"


def _row_line1(r: dict) -> str:
    pc = "CALL" if r["put_call"] == "CALL" else "PUT" if r["put_call"] == "PUT" else "?"
    return f"${r['ticker']}  {pc}  \u00b7  entry {_fmt_money(r['entry_spot'])}"


def _row_line2(r: dict) -> str:
    """No longer includes the trailing (+X.X%) -- that number now lives
    in the magnitude bar + bold label on the row's right side instead
    (see draw_section in render_weekly_recap_card), so it isn't stated
    twice in two different visual forms on the same row."""
    return f"High {_fmt_money(r['high'])}  \u00b7  Low {_fmt_money(r['low'])}  \u00b7  Close {_fmt_money(r['close'])}"


def _raw_move_pct(r: dict) -> float | None:
    """
    VISUAL FIX (2026-09-07, same-day follow-up): the actual stock price
    % move, direction as-stated -- deliberately NOT pct_move_display
    (which is sign-flipped for puts so a favorable move always reads
    positive, for ranking/section-placement purposes). Confirmed
    directly by the user: showing that flipped number on a green bar
    made a winning PUT on a FALLING stock look like the stock went UP,
    which is backwards from what green/positive means on every quote
    screen. This function recovers the real, unflipped move so the
    magnitude bar can answer "which way did the stock actually move"
    correctly regardless of call/put -- win/loss is shown separately
    now, via its own WIN/LOSS pill badge, never folded into this color
    or sign.
    """
    entry = r.get("entry_spot")
    close = r.get("close")
    if entry is None or close is None or entry == 0:
        return None
    return (close - entry) / entry * 100


def render_weekly_recap_card(recap: dict[str, Any], out_path: str) -> None:
    """
    Renders the top-N winners/losers as a dark-themed PNG card.

    VISUAL UPGRADE (2026-09-07, same-day follow-up): each row now also
    carries a length-proportional magnitude bar + bold %-move label on
    its right side, scaled against the single biggest move shown this
    week (winners and losers share one scale, so a +22% winner's bar is
    visibly ~4x a +5.7% winner's, and a -12% loser's bar is visibly the
    longest red bar) -- same "leaderboard" pattern already used for
    Calls vs Puts / Top Symbols in bmt_weekly_insights.py's dashboard,
    applied here so the reader can see relative magnitude before
    reading a single number, not just after. Entry/high/low/close text
    detail is unchanged -- this adds a visual on top of it, it doesn't
    replace it.

    Section headers now read "TOP N WINNERS"/"TOP N LAGGARDS" with the
    actual count shown (== TOP_N_PER_SIDE, i.e. "TOP 10", whenever the
    week has at least that many on that side) rather than a hardcoded
    label -- keeps the header honest on a lighter week with fewer than
    10 real winners or losers to show.
    """
    W = 12.2
    row_h = 0.60
    section_header_h = 0.42
    gap_between_sections = 0.32
    footer_h = 0.55

    TITLE_Y0 = 0.55
    SUBTITLE_GAP = 0.50
    STATS_GAP = 0.62
    STATS_BLOCK_H = 0.80
    HEADER_PAD_BOTTOM = 0.30

    header_h = TITLE_Y0 + SUBTITLE_GAP + STATS_GAP + STATS_BLOCK_H + HEADER_PAD_BOTTOM

    winners = recap["winners"]
    losers = recap["losers"]
    remainder = recap["remainder"]
    failed = recap["failed"]

    n_winners = len(winners)
    n_losers = len(losers)

    H = header_h
    if n_winners:
        H += section_header_h + n_winners * row_h + gap_between_sections
    if n_losers:
        H += section_header_h + n_losers * row_h
    if remainder:
        H += 0.40
    if failed:
        H += 0.30 + len(failed) * 0.26
    H += footer_h

    fig = plt.figure(figsize=(W, H), facecolor=DARK_BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)
    ax.axis("off")
    ax.set_facecolor(DARK_BG)

    LEFT, RIGHT = 0.75, W - 0.75
    CENTER = W / 2

    # Magnitude-bar geometry: text occupies the left portion of the row;
    # a fixed-width WIN/LOSS pill sits between the text and the bar; the
    # bar + bold %-move label occupy the remaining right portion. One
    # shared scale across BOTH winners and losers so bar lengths are
    # directly comparable across the whole card, not just within a
    # section.
    text_right_edge = LEFT + (RIGHT - LEFT) * 0.50
    pill_w, pill_h = 0.62, 0.24
    pill_x0 = text_right_edge + 0.15
    pct_label_w = 0.85
    bar_x0 = pill_x0 + pill_w + 0.30
    bar_max_w = (RIGHT - pct_label_w) - bar_x0
    all_shown = winners + losers
    max_abs_pct = max((abs(_raw_move_pct(r)) for r in all_shown
                        if _raw_move_pct(r) is not None), default=1.0) or 1.0

    y = TITLE_Y0
    ax.text(CENTER, y, "WEEKLY RECAP", ha="center", va="top",
             fontsize=15, color=TXT_DIM, fontweight="bold", fontfamily=HEADER_FONT)
    y += SUBTITLE_GAP
    date_range = f"{recap['start_date'].strftime('%b %d')}\u2013{recap['end_date'].strftime('%b %d, %Y')}"
    ax.text(CENTER, y, date_range, ha="center", va="top",
             fontsize=12.5, color=TXT_DIM, fontfamily=HEADER_FONT)
    y += STATS_GAP

    stat_y = y
    win_rate = recap["win_rate"]
    avg_move = recap["avg_move"]
    stats = [
        (str(recap["total_alerts"]), "ALERTS", TXT_LIGHT),
        (f"{win_rate:.0f}%" if win_rate is not None else "N/A", "WIN RATE",
         GREEN if (win_rate or 0) >= 50 else RED),
        (f"{avg_move:+.1f}%" if avg_move is not None else "N/A", "AVG MOVE",
         GREEN if (avg_move or 0) >= 0 else RED),
    ]
    col_w = (RIGHT - LEFT) / 3
    for i, (num, label, color) in enumerate(stats):
        cx = LEFT + col_w * i + col_w / 2
        ax.text(cx, stat_y, num, ha="center", va="top", fontsize=24,
                 color=color, fontweight="bold", fontfamily=HEADER_FONT)
        ax.text(cx, stat_y + 0.40, label, ha="center", va="top",
                 fontsize=9, color=TXT_DIM, fontweight="bold", fontfamily=HEADER_FONT)
        if i > 0:
            div_x = LEFT + col_w * i
            ax.plot([div_x, div_x], [stat_y - 0.05, stat_y + 0.55], color=PANEL_BORDER, linewidth=1)

    y = header_h - 0.20
    ax.plot([LEFT, RIGHT], [y, y], color=PANEL_BORDER, linewidth=1)
    y += 0.20

    def draw_section(suffix, color, items):
        nonlocal y
        title = f"TOP {len(items)} {suffix}"
        ax.add_patch(plt.Rectangle((LEFT, y + 0.02), 0.11, 0.11, facecolor=color, edgecolor="none"))
        ax.text(LEFT + 0.24, y, title, ha="left", va="top",
                 fontsize=12.5, color=color, fontweight="bold", fontfamily=HEADER_FONT)
        y += section_header_h + 0.12
        for r in items:
            bar_top = y - 0.06
            bar_bot = y + row_h - 0.22
            ax.plot([LEFT, LEFT], [bar_top, bar_bot], color=color, linewidth=3, solid_capstyle="butt")
            ax.text(LEFT + 0.2, y, _esc(_row_line1(r)), ha="left", va="top",
                     fontsize=10.5, color=TXT_LIGHT, fontweight="bold", fontfamily=DATA_FONT)
            ax.text(LEFT + 0.2, y + 0.30, _esc(_row_line2(r)), ha="left", va="top",
                     fontsize=9.5, color=TXT_DIM, fontfamily=DATA_FONT)

            pct = _raw_move_pct(r)
            row_cy = y + row_h / 2 - 0.11

            # WIN/LOSS pill -- independent of the bar's color/sign below.
            # This is the ONLY place trade outcome is shown; the bar to
            # its right is purely about which way the stock itself moved.
            if r["is_win"] is True:
                pill_color, pill_label = GREEN, "WIN"
            elif r["is_win"] is False:
                pill_color, pill_label = RED, "LOSS"
            else:
                pill_color, pill_label = GRAY, "N/A"
            ax.add_patch(FancyBboxPatch(
                (pill_x0, row_cy - pill_h / 2), pill_w, pill_h,
                boxstyle=f"round,pad=0,rounding_size={pill_h * 0.4}",
                linewidth=0, facecolor=pill_color, zorder=4))
            ax.text(pill_x0 + pill_w / 2, row_cy, pill_label, ha="center", va="center",
                     fontsize=8, color="#0a0e1c", fontweight="bold", fontfamily=HEADER_FONT, zorder=5)

            # Magnitude bar -- colored and signed by the ACTUAL stock
            # move (green=up, red=down), never by win/loss. See
            # _raw_move_pct()'s docstring for why these must stay separate.
            if pct is not None:
                bar_color = GREEN if pct >= 0 else RED
                bw = max((abs(pct) / max_abs_pct) * bar_max_w, 0.06)
                bar_h = 0.20
                ax.add_patch(plt.Rectangle((bar_x0, row_cy - bar_h / 2), bw, bar_h,
                                            facecolor=bar_color, edgecolor="none"))
                pct_str = f"{pct:+.1f}%"
                pct_color = bar_color
            else:
                pct_str = "N/A"
                pct_color = GRAY
            ax.text(RIGHT, row_cy, pct_str, ha="right", va="center",
                     fontsize=10.5, color=pct_color, fontweight="bold", fontfamily=DATA_FONT)
            y += row_h

    if n_winners:
        draw_section("WINNERS", GREEN, winners)
        y += gap_between_sections
    if n_losers:
        draw_section("LAGGARDS", RED, losers)

    if remainder:
        rem_wins = sum(1 for r in remainder if r["is_win"])
        rem_avg = sum(r["pct_move_display"] for r in remainder) / len(remainder)
        y += 0.10
        ax.text(LEFT, y, f"+ {len(remainder)} other alert(s) this week \u2014 "
                          f"{rem_wins}/{len(remainder)} favorable, avg {rem_avg:+.1f}%",
                 ha="left", va="top", fontsize=9, color=TXT_DIM, fontfamily=DATA_FONT)
        y += 0.30

    if failed:
        y += 0.05
        for r in failed:
            ax.text(LEFT, y, _esc(f"\u26a0 ${r['ticker']} \u00b7 no current quote available "
                                    f"({r.get('fail_reason', 'unknown')})"),
                     ha="left", va="top", fontsize=8.5, color=GRAY, fontfamily=DATA_FONT)
            y += 0.26

    footer_y = H - footer_h + 0.22
    ax.text(CENTER, footer_y, "Spot price movement only \u2014 not option premium. Not financial advice.",
             ha="center", va="top", fontsize=8.5, color=GRAY, fontfamily=HEADER_FONT)

    fig.savefig(out_path, facecolor=DARK_BG, dpi=200)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# HOTFIX (2026-09-07): post_weekly_recap() was removed in the visual
# redesign in favor of post_weekly_recap_image(), but main.py's
# top-level `from weekly_recap import build_weekly_recap,
# post_weekly_recap` is a MODULE-LEVEL import -- its absence crashed
# uvicorn at boot, taking down the entire service, not just the weekly
# recap feature. Restoring this function (old plain-text behavior)
# immediately unblocks the service from crashing on startup while
# main.py itself gets updated to call render_weekly_recap_card() +
# post_weekly_recap_image() instead. DELETE this function once main.py
# is confirmed updated -- it exists only to prevent an import crash,
# not because the plain-text format is coming back on purpose.
# ---------------------------------------------------------------------------

async def post_weekly_recap(webhook_url: str, message: str) -> bool:
    """DEPRECATED -- old plain-text poster, kept temporarily so an
    unmigrated caller's import doesn't crash the whole service. See
    post_weekly_recap_image() for the current image-card version."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(webhook_url, json={"content": message})
    return resp.status_code in (200, 204)


async def post_weekly_recap_image(webhook_url: str, image_path: str, caption: str) -> bool:
    """
    Posts the rendered recap card + a short caption. Replaces the old
    post_weekly_recap() (plain-text message) -- per the 2026-09-07
    visual redesign, the detail lives in the image now, so the message
    itself only needs the one-line headline the caption already is.
    """
    with open(image_path, "rb") as f:
        files = {"file": ("weekly_recap.png", f, "image/png")}
        data = {"content": caption}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(webhook_url, data=data, files=files)
    return resp.status_code in (200, 204)