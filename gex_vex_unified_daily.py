r"""
gex_vex_unified_daily.py -- PRODUCTION single-card GEX/VEX dashboard.

PROMOTED TO PRODUCTION (2026-08-13): this script was developed and
tested as gex_vex_unified_test.py, posted to a separate test webhook
and iterated on over several rounds directly against a reference
mockup, before being promoted here. It REPLACES
gex_vex_combined_daily.py, which used to post 7 separate Mag 7 cards +
1 separate SPY/QQQ/IWM dashboard -- 8 images and 8+ Discord messages
every day, confirmed by the user to be too much to scroll through and
digest. This script posts ONE consolidated image instead: SPY/QQQ/IWM
gradient-bar cards + a Mag 7 positioning table + a "Today's Focus"
callout panel + a plain-English "Key Terms" definitions strip.

Changes made specifically for this promotion, vs. the test file:
  1. DISCORD_WEBHOOK now reads GEX_DISCORD_WEBHOOK -- the SAME shared
     production GEX channel gex_vex_combined_daily.py already posted
     to (this replaces that channel's content, it isn't a new
     channel), not GEX_UNIFIED_TEST_DISCORD_WEBHOOK.
  2. gex_vex_combined_daily.py's cron trigger should be DISABLED on
     Railway (per direct user confirmation) now that this script
     covers the same daily GEX/VEX post.
  3. Runs on its own schedule, 10:30am ET Mon-Fri (30 14 * * 1-5 UTC
     during EDT; becomes 30 15 * * 1-5 during EST after the November
     DST changeover) -- deliberately not the market open OR the close,
     both confirmed separately to produce unreliable bid/ask data (see
     gex_vex.py's get_spot_price(), which skips a ticker outright
     rather than publish a price built from an incomplete quote).

"SINCE YESTERDAY" DAY-OVER-DAY COMPARISON -- FINAL DESIGN (2026-08-13):
the original plan was a separate Discord text message listing every
ticker's delta. Confirmed with the user that, once this pipeline is
the ONLY daily post (no more 8-message spread to absorb it), a
separate comparison message for up to 10 tickers would itself become
the wall-of-text problem this whole redesign was meant to solve.
Final design instead:
  - A compact "X% vs yesterday" delta is drawn directly on each
    SPY/QQQ/IWM card next to its spot price -- glanceable data, no
    prose.
  - A small warning icon appears next to a ticker's regime badge
    (on the core cards and in the Mag 7 table) if its long/short gamma
    regime flipped since the prior snapshot.
  - Any ticker with a regime flip today is automatically promoted to
    the TOP of "Today's Focus" (capped at 2, to leave room for at
    least one normal heuristic-selected item) -- this is the ONE place
    the full plain-English explanation (from
    gex_vex_history.build_since_yesterday_line(), via the new
    get_comparison_summary() helper) actually appears, and only for
    tickers where something is genuinely notable, not all ten every
    day. gex_vex_history.py itself required one small, purely additive
    change (get_comparison_summary()) to support this -- no existing
    function in that file was modified.

DATA: real, live numbers from gex_vex.compute_gex_vex() for all 10
tickers (Mag 7 + SPY/QQQ/IWM) -- the same function
gex_vex_combined_daily.py used, unmodified. gex_vex.py is NOT touched
by this script (aside from the separate 2026-08-19 gamma-flip
band-matching fix documented in that file's own module docstring,
which changes the VALUES compute_gex_vex() returns for gamma_flip, not
this script's own logic).

DEPLOYMENT NOTES: deploy this file alongside gex_vex.py and the
updated gex_vex_history.py in the same service (Custom Start Command
"python gex_vex_unified_daily.py", Cron Schedule "30 14 * * 1-5").
Once confirmed running, disable or remove gex_vex_combined_daily.py's
cron trigger so the old fragmented 8-post output stops firing
alongside this one.

"TODAY'S FOCUS" SELECTION (initial heuristic, not final business
logic -- flagged clearly since this is exactly the kind of judgment
call worth reviewing during the test period, not locking in silently):
  - REGIME FLIP today (see above) -- always top priority when present.
  - Highest expected-move ticker  -> "HIGH RISK" tag
  - Ticker whose spot sits closest (in %) to its own gamma flip level
    -> "KEY LEVELS" tag (i.e. "right at the pivot, watch which way it breaks")
  - First SHORT-gamma ticker found (if any) -> "BREAKOUT WATCH" tag,
    since being the outlier regime in an otherwise-long-gamma group is
    itself the notable fact
If fewer than 3 distinct tickers qualify, the list is padded with the
next-highest expected-move tickers so the panel is never left with
fewer than 3 items in a normal run.

FLIP-MARKER BOUNDS GUARD (2026-08-19): confirmed in production, on
IWM specifically and TWICE (once with a flip value of 266.97 landing
inside a neighboring ticker's card region, once with 255.17 landing
almost entirely off the left edge of the whole image), that this
file's render_unified_card() drew the gamma-flip dashed line and label
UNCONDITIONALLY -- with no check that the flip value actually fell
within the card's own visible bar range (range_min to range_max). Both
of gex_vex.py's own per-ticker card renderers already had this exact
guard (`if gamma_flip is not None and range_min <= gamma_flip <=
range_max`); this file's version of the same per-ticker card loop was
simply missing it. The root cause -- find_gamma_flip() in gex_vex.py
being called with a search band far wider than the wall-selection
band, so it could return a real-but-distant crossing -- is fixed
separately at the source (see gex_vex.py's matching 2026-08-19 module
docstring note). This guard is added here as a second, independent
layer regardless: even if some future data path ever hands this
renderer an out-of-range gamma_flip again, it can now never be drawn
outside its own card's bar, let alone bleed into a neighboring card or
off the image entirely.
"""

import os
import textwrap
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Circle
from matplotlib.colors import LinearSegmentedColormap
import numpy as np

import gex_vex
import gex_vex_history

DISCORD_WEBHOOK = os.environ["GEX_DISCORD_WEBHOOK"]
ET = ZoneInfo("America/New_York")

MAG7_TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]
CORE_TICKERS = ["SPY", "QQQ", "IWM"]

BG      = "#080b12"
SURFACE = "#0f1420"
CARD_BG = "#131928"
BORDER  = "#232d42"
TEXT1   = "#f5f7fa"
TEXT2   = "#9aa4b8"
TEXT3   = "#6b7488"
GREEN   = "#2dd4a8"
RED     = "#f26a7d"
GOLD    = "#f5b942"
BLUE    = "#5b9df5"
PURPLE  = "#b088f5"


def fmt(v):
    if v is None:
        return "N/A"
    return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"


def esc(text):
    return text.replace("$", r"\$") if text else text


def measure_text_width(fig, ax, text, fontsize, fontweight="bold"):
    probe = ax.text(0, 0, esc(text), fontsize=fontsize, fontweight=fontweight, alpha=0)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    bbox = probe.get_window_extent(renderer=renderer)
    inv = ax.transData.inverted()
    (x0, _), (x1, _) = inv.transform((bbox.x0, bbox.y0)), inv.transform((bbox.x1, bbox.y1))
    probe.remove()
    return abs(x1 - x0)


def measure_text_height(fig, ax, text, fontsize, fontweight="normal"):
    probe = ax.text(0, 0, esc(text), fontsize=fontsize, fontweight=fontweight, va="top", alpha=0)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    bbox = probe.get_window_extent(renderer=renderer)
    inv = ax.transData.inverted()
    (_, y0), (_, y1) = inv.transform((bbox.x0, bbox.y0)), inv.transform((bbox.x1, bbox.y1))
    probe.remove()
    return abs(y1 - y0)


def pill(ax, x, y, w, h, color, text, fontsize=8, fill_alpha=0.18, zorder=3):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.015,rounding_size=0.05",
                                facecolor=color, alpha=fill_alpha, edgecolor="none", zorder=zorder))
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.015,rounding_size=0.05",
                                facecolor="none", edgecolor=color, linewidth=1.2, zorder=zorder+1))
    ax.text(x + w/2, y + h/2, esc(text), fontsize=fontsize, fontweight="bold", color=color,
            va="center", ha="center", zorder=zorder+2)


def pill_right_aligned(ax, fig, right_edge_x, y, h, color, text, fontsize=8, fill_alpha=0.2, h_pad=0.28, zorder=3):
    text_w = measure_text_width(fig, ax, text, fontsize)
    w = text_w + h_pad
    pill(ax, right_edge_x - w, y, w, h, color, text, fontsize=fontsize, fill_alpha=fill_alpha, zorder=zorder)
    return w


def pick_todays_focus(core_results, mag7_results, comparisons):
    """
    Initial heuristic -- see module docstring. Returns up to 3
    (ticker_result, color, tag, description) tuples.

    `comparisons`: dict of ticker -> gex_vex_history.get_comparison_summary()
    result. A REGIME FLIP today is now the #1 priority signal (ahead of
    the three heuristics below) -- this is what REPLACES the separate
    "Since Yesterday" text message: instead of a wall of prose covering
    every ticker's delta, only tickers where something actually
    regime-changed get a slot in this panel, using the same real
    plain-English paragraph gex_vex_history already writes.
    """
    all_valid = [r for r in (core_results + mag7_results) if "error" not in r]
    picked_tickers = set()
    focus = []

    # 0. REGIME FLIP TODAY -- highest priority, replaces the separate
    # Since Yesterday text post entirely. Only tickers with an actual
    # flip get a slot here; a quiet day with no flips means this rule
    # contributes nothing, and the panel falls through to the normal
    # heuristics below.
    flipped = [r for r in all_valid if comparisons.get(r["ticker"], {}).get("regime_flipped")]
    for r in flipped[:2]:  # cap at 2 so there's still room for at least one heuristic pick
        comp = comparisons[r["ticker"]]
        color = GOLD if comp["flip_direction"] == "to_long" else RED
        focus.append((r, color, "REGIME FLIP", comp["plain_text"]))
        picked_tickers.add(r["ticker"])

    # 1. Highest expected move -> HIGH RISK
    with_em = [r for r in all_valid if r.get("expected_move") and r["ticker"] not in picked_tickers]
    if with_em and len(focus) < 3:
        top_em = max(with_em, key=lambda r: r["expected_move"]["pct"])
        em = top_em["expected_move"]
        desc = (f"Widest expected move of the group (\u00b1{em['pct']}%, \u00b1${em['dollar']:.2f} by Friday). "
                f"Bigger swings cut both ways here -- size accordingly.")
        focus.append((top_em, GOLD, "HIGH RISK", desc))
        picked_tickers.add(top_em["ticker"])

    # 2. Closest to its own gamma flip -> KEY LEVELS
    near_flip = [r for r in all_valid if r.get("gamma_flip") and r["ticker"] not in picked_tickers]
    if near_flip and len(focus) < 3:
        closest = min(near_flip, key=lambda r: abs(r["spot"] - r["gamma_flip"]) / r["spot"])
        desc = (f"Sitting right at its {fmt(closest['gamma_flip'])} pivot level -- which way it breaks "
                f"from here matters more than usual today.")
        focus.append((closest, PURPLE, "KEY LEVELS", desc))
        picked_tickers.add(closest["ticker"])

    # 3. First short-gamma name (the regime outlier) -> BREAKOUT WATCH
    short_names = [r for r in all_valid if r["net_gex"] < 0 and r["ticker"] not in picked_tickers]
    if short_names and len(focus) < 3:
        s = short_names[0]
        desc = ("Short gamma while most of the group is long -- less cushion against a big move here. "
                "Keep position sizes tighter than the rest of the list.")
        focus.append((s, RED, "BREAKOUT WATCH", desc))
        picked_tickers.add(s["ticker"])

    # Pad with next-highest expected-move tickers if fewer than 3 qualified
    if len(focus) < 3:
        remaining = sorted([r for r in with_em if r["ticker"] not in picked_tickers],
                            key=lambda r: r["expected_move"]["pct"], reverse=True)
        for r in remaining:
            if len(focus) >= 3:
                break
            em = r["expected_move"]
            desc = f"Expected move \u00b1{em['pct']}% this week -- worth a look alongside the rest of the group."
            focus.append((r, BLUE, "WATCH", desc))
            picked_tickers.add(r["ticker"])

    return focus[:3]


def render_unified_card(core_results, mag7_results, focus_items, comparisons, week_label, out_path):
    FIG_W = 15.5
    fig = plt.figure(figsize=(FIG_W, 20), dpi=170, facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, FIG_W); ax.set_ylim(0, 20)
    ax.axis("off"); ax.invert_yaxis()

    ax.text(FIG_W/2, 0.45, "DAILY GEX / VEX DASHBOARD", fontsize=25, fontweight="bold",
            color=TEXT1, va="top", ha="center")
    ax.text(FIG_W/2, 0.98, f"{week_label}  \u00b7  BlueMoonTrades", fontsize=11.5,
            color=BLUE, va="top", ha="center")

    cy = 1.65
    card_w = 4.95
    card_gap = 0.25
    x0 = 0.35
    gradient_cmap = LinearSegmentedColormap.from_list("rg", [RED, "#3a3f4e", GREEN])

    STAT_FONTSIZE = 10.5
    LABEL_FONTSIZE = 8.2
    row_gap = 0.60
    stat_value_h = measure_text_height(fig, ax, "-0.91B", STAT_FONTSIZE, fontweight="bold")
    bottom_pad = 0.30

    py_off = 0.32
    spot_label_off = py_off + 0.48
    spot_val_off = py_off + 0.72
    bar_y_off = py_off + 1.55
    bar_h = 0.24
    legend_y_off = bar_y_off + bar_h + 0.56
    sy_off = legend_y_off + 0.42
    last_left_row_label_off = sy_off + 2 * row_gap
    last_left_row_val_off = last_left_row_label_off + 0.24
    card_h = (last_left_row_val_off + stat_value_h) + bottom_pad

    valid_core = [r for r in core_results if "error" not in r]
    for i, t in enumerate(valid_core):
        cx = x0 + i * (card_w + card_gap)
        ax.add_patch(FancyBboxPatch((cx, cy), card_w, card_h,
                                    boxstyle="round,pad=0.02,rounding_size=0.10",
                                    facecolor=CARD_BG, edgecolor=BORDER, linewidth=1.0, zorder=2))

        px = cx + 0.30
        ax.text(px, cy + py_off, f"${t['ticker']}", fontsize=22, fontweight="bold", color=TEXT1, va="top", zorder=3)
        ax.text(px, cy + spot_label_off, "SPOT PRICE", fontsize=7.5, color=TEXT3, va="top", zorder=3)
        ax.text(px, cy + spot_val_off, f"{t['spot']:,.2f}", fontsize=14, fontweight="bold", color=GREEN, va="top", zorder=3)

        # Compact "vs yesterday" delta -- glanceable data, no prose.
        # This is what replaced the separate Since Yesterday text post.
        comp = comparisons.get(t["ticker"], {})
        if comp.get("has_comparison") and comp.get("spot_change_pct") is not None:
            pct = comp["spot_change_pct"]
            arrow = "\u25b2" if pct >= 0 else "\u25bc"
            delta_color = GREEN if pct >= 0 else RED
            spot_val_w = measure_text_width(fig, ax, f"{t['spot']:,.2f}", 14)
            ax.text(px + spot_val_w + 0.18, cy + spot_val_off + 0.06, f"{arrow} {abs(pct):.1f}% vs yesterday",
                    fontsize=8.0, color=delta_color, va="top", zorder=3)

        is_long = t["net_gex"] >= 0
        badge_color = GREEN if is_long else RED
        badge_txt = "LONG GAMMA" if is_long else "SHORT GAMMA"
        bw = 1.75
        badge_x = cx + card_w - bw - 0.28
        pill(ax, badge_x, cy + 0.30, bw, 0.40, badge_color, badge_txt, fontsize=8.5)
        if comp.get("regime_flipped"):
            ax.text(badge_x - 0.10, cy + 0.50, "\u26A0", fontsize=13, color=GOLD, va="center", ha="right", zorder=4)

        bar_y = cy + bar_y_off
        bar_x0 = px
        bar_w = card_w - 0.60
        put_wall = t.get("put_wall") or t["spot"] * 0.99
        call_wall = t.get("call_wall") or t["spot"] * 1.01
        grad = np.linspace(0, 1, 256).reshape(1, -1)
        ax.imshow(grad, extent=[bar_x0, bar_x0 + bar_w, bar_y, bar_y + bar_h],
                  cmap=gradient_cmap, aspect="auto", zorder=3, alpha=0.9)
        ax.add_patch(FancyBboxPatch((bar_x0, bar_y), bar_w, bar_h,
                                    boxstyle="round,pad=0,rounding_size=0.12",
                                    facecolor="none", edgecolor=BORDER, linewidth=0.9, zorder=4))

        rng_lo, rng_hi = put_wall * 0.995, call_wall * 1.005
        def to_x(v, lo=rng_lo, hi=rng_hi, x0_=bar_x0, w=bar_w):
            return x0_ + (v - lo) / (hi - lo) * w

        spot_x = to_x(t["spot"])
        ax.plot([spot_x], [bar_y + bar_h/2], marker="o", markersize=11,
                markerfacecolor="#ffffff", markeredgecolor=TEXT1, markeredgewidth=1.4, zorder=6)

        # FLIP-MARKER BOUNDS GUARD (2026-08-19): previously drew this
        # dashed line and "FLIP $X" label UNCONDITIONALLY, with no
        # check that gamma_flip actually fell within THIS ticker's own
        # bar range (rng_lo to rng_hi) -- confirmed in production
        # (twice) that a gamma_flip value from find_gamma_flip()'s
        # previously-too-wide search band could land far outside a
        # ticker's own put-wall/call-wall/spot range, causing to_x() to
        # compute an x-position well outside this card's bar -- in one
        # case bleeding into a neighboring card, in another landing
        # almost entirely off the left edge of the whole image. The
        # root cause (find_gamma_flip()'s search band not matching the
        # wall-selection band) is fixed at the source in gex_vex.py's
        # compute_gex_vex() as of the same date, but this guard is kept
        # here regardless, as a second, independent layer -- exactly
        # matching the guard gex_vex.py's own two card renderers
        # (render_single_ticker_gex_card, render_gex_dashboard_card)
        # already had. A flip marker can now never be drawn outside its
        # own card's visible bar, no matter what value is returned
        # upstream.
        if t.get("gamma_flip") is not None and rng_lo <= t["gamma_flip"] <= rng_hi:
            flip_x = to_x(t["gamma_flip"])
            ax.plot([flip_x, flip_x], [bar_y - 0.07, bar_y + bar_h + 0.07],
                    color=GOLD, linewidth=1.5, linestyle="--", zorder=5)
            ax.text(flip_x, bar_y - 0.12, f"FLIP {fmt(t['gamma_flip'])}", fontsize=7.0, color=GOLD,
                    va="bottom", ha="center", fontweight="bold", zorder=6)

        ax.text(bar_x0, bar_y - 0.32, "PUT WALL", fontsize=7.3, color=RED, va="bottom", fontweight="bold", zorder=6)
        ax.text(bar_x0 + bar_w, bar_y - 0.32, "CALL WALL", fontsize=7.3, color=GREEN, va="bottom",
                ha="right", fontweight="bold", zorder=6)

        quarter_lo = rng_lo + (rng_hi - rng_lo) * 0.25
        quarter_hi = rng_lo + (rng_hi - rng_lo) * 0.75
        scale_points = [
            (0.00, fmt(put_wall), RED, True), (0.25, fmt(quarter_lo), TEXT3, False),
            (0.50, fmt(t["spot"]), TEXT1, True), (0.75, fmt(quarter_hi), TEXT3, False),
            (1.00, fmt(call_wall), GREEN, True),
        ]
        for frac, label, color, bold in scale_points:
            tx = bar_x0 + frac * bar_w
            ha = "left" if frac == 0 else ("right" if frac == 1 else "center")
            ax.text(tx, bar_y + bar_h + 0.24, label, fontsize=9.0 if bold else 7.8, color=color,
                    va="top", ha=ha, fontweight="bold" if bold else "normal", zorder=6)

        legend_y = cy + legend_y_off
        ax.plot([bar_x0], [legend_y + 0.045], marker="o", markersize=6,
                markerfacecolor="#ffffff", markeredgecolor=TEXT1, markeredgewidth=0.8, zorder=6)
        ax.text(bar_x0 + 0.16, legend_y, "CURRENT PRICE", fontsize=6.6, color=TEXT3, va="top", zorder=6)
        ax.plot([bar_x0 + 1.55, bar_x0 + 1.80], [legend_y + 0.045, legend_y + 0.045],
                color=GOLD, linewidth=1.3, linestyle="--", zorder=6)
        ax.text(bar_x0 + 1.90, legend_y, "GAMMA FLIP", fontsize=6.6, color=TEXT3, va="top", zorder=6)

        sy = cy + sy_off
        em = t.get("expected_move") or {}
        left_col = [("Gamma Flip", fmt(t.get("gamma_flip")), TEXT1),
                    ("Net GEX", f"+${t['net_gex']/1e9:.2f}B" if t["net_gex"] >= 0 else f"-${abs(t['net_gex'])/1e9:.2f}B", badge_color),
                    ("Net VEX", f"{t['net_vex']/1e9:+.2f}B", RED if t["net_vex"] < 0 else GREEN)]
        right_col = [("Expected Move (1D)", f"\u00b1{em.get('pct', 0)}%" if em else "N/A", GOLD),
                     ("Implied Range (1D)", f"{em.get('min', 0):.2f}\u2013{em.get('max', 0):.2f}" if em else "N/A", TEXT1),
                     ("Put Wall / Call Wall", f"{fmt(put_wall)}  /  {fmt(call_wall)}", TEXT2)]

        col2_x = px + (card_w - 0.60) / 2 + 0.20
        _y = sy
        for label, val, color in left_col:
            ax.text(px, _y, label, fontsize=LABEL_FONTSIZE, color=TEXT2, va="top", zorder=3)
            ax.text(px, _y + 0.24, esc(val), fontsize=STAT_FONTSIZE, fontweight="bold", color=color, va="top", zorder=3)
            _y += row_gap
        _y = sy
        for label, val, color in right_col:
            ax.text(col2_x, _y, label, fontsize=LABEL_FONTSIZE, color=TEXT2, va="top", zorder=3)
            ax.text(col2_x, _y + 0.24, esc(val), fontsize=9.5, fontweight="bold", color=color, va="top", zorder=3)
            _y += row_gap

    cy += card_h + 0.35

    left_w = 9.6
    right_x = x0 + left_w + 0.3
    right_w = FIG_W - right_x - 0.35

    valid_mag7 = [r for r in mag7_results if "error" not in r]
    table_row_sample_h = measure_text_height(fig, ax, "$AAPL", 10.5, fontweight="bold")
    regime_pill_h = 0.34
    row_h = max(table_row_sample_h, regime_pill_h) + 0.30
    table_header_h = measure_text_height(fig, ax, "TICKER", 7.3, fontweight="bold") + 0.55
    mag7_content_h = table_header_h + row_h * max(len(valid_mag7), 1)

    # --- Pre-compute Today's Focus real content height BEFORE fixing --
    # the shared section height. BUGFIX (2026-08-13): the shared height
    # for both bottom panels was previously derived ONLY from the Mag 7
    # table's row count -- on a run where some Mag 7 tickers errored
    # out (fewer valid rows -> shorter table), the shared height shrank
    # below what Today's Focus actually needs for its fixed 3 items,
    # and the last item's content rendered PAST the card's own bottom
    # border (confirmed in a real screenshot: $AMZN's badge circle and
    # description text hung below the panel's rounded border entirely).
    # Computing both panels' real needed height first, then using
    # whichever is LARGER as the shared section_h, guarantees neither
    # panel is ever truncated below its own content regardless of how
    # many Mag 7 tickers happen to error out on a given run.
    desc_fontsize = 9.5
    ticker_fontsize = 15
    tag_fontsize = 8.0
    badge_r = 0.34
    title_row_h = 0.46
    tag_desc_gap = 0.22
    stacked_extra_h = 0.42 + tag_desc_gap
    bottom_margin = 0.20

    sample = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ "
    avg_char_w = measure_text_width(fig, ax, sample, desc_fontsize, fontweight="normal") / len(sample)
    desc_x_offset = 0.65 + badge_r + 0.38
    avail_w = right_w - desc_x_offset - 0.35
    chars_per_line = max(20, int(avail_w / avg_char_w))

    min_gap = 0.30
    item_heights, wrapped_texts, stack_tag = [], [], []
    for t, color, tag, text in focus_items:
        ticker = t["ticker"]
        tx_est = right_x + 0.65 + badge_r + 0.38
        name_w = measure_text_width(fig, ax, f"${ticker}", ticker_fontsize)
        tag_pill_w = measure_text_width(fig, ax, tag, tag_fontsize) + 0.28
        tag_right_edge = right_x + right_w - 0.32
        fits_same_row = (tx_est + name_w + min_gap) <= (tag_right_edge - tag_pill_w)
        stack_tag.append(not fits_same_row)

        wrapped = "\n".join(textwrap.wrap(text, width=chars_per_line))
        wrapped_texts.append(wrapped)
        desc_h = measure_text_height(fig, ax, wrapped, desc_fontsize)
        extra = tag_desc_gap if fits_same_row else stacked_extra_h
        item_heights.append(title_row_h + extra + desc_h + bottom_margin)

    focus_natural_total = sum(item_heights) if item_heights else 0
    focus_content_h = 0.88 + focus_natural_total  # header + items, no leftover yet

    pad = 0.45
    section_h = max(mag7_content_h, focus_content_h) + pad

    ax.add_patch(FancyBboxPatch((x0, cy), left_w, section_h,
                                boxstyle="round,pad=0.02,rounding_size=0.07",
                                facecolor=CARD_BG, edgecolor=BORDER, linewidth=1.0, zorder=2))
    ax.add_patch(FancyBboxPatch((right_x, cy), right_w, section_h,
                                boxstyle="round,pad=0.02,rounding_size=0.07",
                                facecolor=CARD_BG, edgecolor=BORDER, linewidth=1.0, zorder=2))

    ax.text(x0 + 0.32, cy + 0.32, "MAG 7 POSITIONING", fontsize=14, fontweight="bold", color=TEXT1, va="top", zorder=3)

    th_y = cy + 0.88
    cols = [x0 + 0.30, x0 + 1.55, x0 + 2.75, x0 + 4.10, x0 + 5.25, x0 + 6.40, x0 + 7.65, x0 + 8.75]
    headers = ["TICKER", "SPOT", "REGIME", "PUT WALL", "CALL WALL", "GAMMA FLIP", "EXP MOVE", "NET GEX"]
    for x, h in zip(cols, headers):
        ax.text(x, th_y, h, fontsize=7.3, fontweight="bold", color=TEXT3, va="top", zorder=3)
    th_y += 0.26
    ax.plot([x0 + 0.22, x0 + left_w - 0.22], [th_y, th_y], color=BORDER, linewidth=0.7, zorder=3)

    ry = th_y + 0.16
    for i, t in enumerate(valid_mag7):
        if i % 2 == 0:
            ax.add_patch(plt.Rectangle((x0 + 0.15, ry - 0.06), left_w - 0.30, row_h - 0.05,
                                        facecolor=SURFACE, edgecolor="none", zorder=1))
        is_long = t["net_gex"] >= 0
        rc = GREEN if is_long else RED
        em = t.get("expected_move") or {}
        ax.text(cols[0], ry, f"${t['ticker']}", fontsize=10.5, fontweight="bold", color=TEXT1, va="top", zorder=3)
        ax.text(cols[1], ry, f"{t['spot']:,.2f}", fontsize=9.3, color=TEXT2, va="top", zorder=3)
        pill(ax, cols[2], ry - 0.01, 1.10, 0.34, rc, "LONG" if is_long else "SHORT", fontsize=7.3)
        comp = comparisons.get(t["ticker"], {})
        if comp.get("regime_flipped"):
            ax.text(cols[2] - 0.16, ry + 0.16, "\u26A0", fontsize=10, color=GOLD, va="center", ha="right", zorder=4)
        ax.text(cols[3], ry, fmt(t.get("put_wall")), fontsize=9.3, color=RED, va="top", zorder=3)
        ax.text(cols[4], ry, fmt(t.get("call_wall")), fontsize=9.3, color=GREEN, va="top", zorder=3)
        ax.text(cols[5], ry, fmt(t.get("gamma_flip")), fontsize=9.3, color=GOLD, va="top", zorder=3)
        ax.text(cols[6], ry, f"\u00b1{em.get('pct', 0)}%" if em else "N/A", fontsize=9.3, color=TEXT1, va="top", zorder=3)
        gex_str = f"+${t['net_gex']/1e9:.2f}B" if t["net_gex"] >= 0 else f"-${abs(t['net_gex'])/1e9:.2f}B"
        ax.text(cols[7], ry, gex_str, fontsize=9.3, fontweight="bold", color=rc, va="top", zorder=3)
        ry += row_h

    ax.text(right_x + 0.32, cy + 0.32, "TODAY'S FOCUS", fontsize=14, fontweight="bold", color=TEXT1, va="top", zorder=3)

    usable_h = section_h - 1.0
    leftover = max(0, usable_h - focus_natural_total)
    extra_gap = leftover / (len(focus_items) + 1) if focus_items else 0

    fy = cy + 0.88 + extra_gap
    for idx, (t, color, tag, text) in enumerate(focus_items):
        ticker = t["ticker"]
        row_top = fy
        item_h = item_heights[idx]

        ax.add_patch(plt.Rectangle((right_x + 0.18, row_top + 0.05), 0.05, item_h - 0.10,
                                    facecolor=color, linewidth=0, zorder=3))

        bx = right_x + 0.65
        by = row_top + 0.50
        ax.add_patch(Circle((bx, by), badge_r, facecolor=color, alpha=0.85, edgecolor=color, linewidth=1.2, zorder=4))
        ax.text(bx, by, ticker[0], fontsize=16, fontweight="bold", color=BG, va="center", ha="center", zorder=5)

        tx = bx + badge_r + 0.38
        ax.text(tx, row_top + 0.20, f"${ticker}", fontsize=ticker_fontsize, fontweight="bold", color=TEXT1, va="top", zorder=4)

        tag_right_edge = right_x + right_w - 0.32
        if stack_tag[idx]:
            tag_w = measure_text_width(fig, ax, tag, tag_fontsize) + 0.28
            pill(ax, tx, row_top + 0.60, tag_w, 0.36, color, tag, fontsize=tag_fontsize, fill_alpha=0.2)
            desc_y = row_top + 0.60 + stacked_extra_h
        else:
            pill_right_aligned(ax, fig, tag_right_edge, row_top + 0.22, 0.42, color, tag, fontsize=tag_fontsize, fill_alpha=0.2)
            desc_y = row_top + 0.46 + tag_desc_gap

        ax.text(tx, desc_y, esc(wrapped_texts[idx]), fontsize=desc_fontsize, color=TEXT2, va="top", zorder=4)

        fy = row_top + item_h + extra_gap
        if idx < len(focus_items) - 1:
            divider_y = row_top + item_h + extra_gap / 2
            ax.plot([right_x + 0.32, right_x + right_w - 0.32], [divider_y, divider_y],
                    color=BORDER, linewidth=0.6, zorder=3)

    cy += section_h + 0.35

    # --- Definitions strip -- same precedent as gex_vex.py's existing --
    # per-ticker card renderers (which already carry a compact GEX/VEX/
    # Walls/Gamma Flip key), extended here to cover every jargon term
    # actually used on THIS card. Sized to the real wrapped text height,
    # not guessed, same discipline as every other section of this file.
    definitions = (
        "LONG GAMMA: price tends to get pulled back toward the range if it swings too far -- moves stay more contained.  \u00b7  "
        "SHORT GAMMA: less cushion against big moves -- once a level breaks, price can run further than usual.  \u00b7  "
        "GAMMA FLIP: the price level where that behavior switches from one to the other.  \u00b7  "
        "PUT WALL / CALL WALL: strikes where options positioning is heaviest -- tend to act like a floor or ceiling.  \u00b7  "
        "NET GEX: total gamma exposure -- the sign shows long or short gamma overall.  \u00b7  "
        "NET VEX: how sensitive that positioning is to changes in volatility.  \u00b7  "
        "EXPECTED MOVE: how far the options market is pricing this to move by Friday."
    )
    def_fontsize = 7.6
    def_x0 = x0
    def_w = FIG_W - 2 * x0
    def_sample = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ "
    def_avg_char_w = measure_text_width(fig, ax, def_sample, def_fontsize, fontweight="normal") / len(def_sample)
    def_inner_pad = 0.35
    def_chars_per_line = max(30, int((def_w - 2 * def_inner_pad) / def_avg_char_w))
    def_wrapped = "\n".join(textwrap.wrap(definitions, width=def_chars_per_line))
    def_text_h = measure_text_height(fig, ax, def_wrapped, def_fontsize)

    def_header_h = 0.32
    def_box_h = def_header_h + def_text_h + 0.30
    ax.add_patch(FancyBboxPatch((def_x0, cy), def_w, def_box_h,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=CARD_BG, edgecolor=BORDER, linewidth=0.9, zorder=2))
    ax.text(def_x0 + 0.30, cy + 0.18, "KEY TERMS", fontsize=9.0, fontweight="bold", color=TEXT3, va="top", zorder=3)
    ax.text(def_x0 + 0.30, cy + def_header_h + 0.14, esc(def_wrapped), fontsize=def_fontsize,
            color=TEXT2, va="top", zorder=3)

    cy += def_box_h + 0.35

    fig.set_size_inches(FIG_W, cy)
    ax.set_ylim(cy, 0)
    ax.set_xlim(0, FIG_W)

    plt.savefig(out_path, facecolor=BG, bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)


def get_week_label(today=None):
    from datetime import date, timedelta
    if today is None:
        today = date.today()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    if monday.month == friday.month:
        return f"Week of {monday.strftime('%b %d')} - {friday.strftime('%d, %Y')}"
    return f"Week of {monday.strftime('%b %d')} - {friday.strftime('%b %d, %Y')}"


def post_text_to_discord(content):
    r = requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=15)
    print(f"  [DISCORD] text post: {r.status_code}")
    return r.status_code in (200, 204)


def post_image_to_discord(image_path, caption=""):
    with open(image_path, "rb") as f:
        files = {"file": (os.path.basename(image_path), f, "image/png")}
        data = {"content": caption}
        r = requests.post(DISCORD_WEBHOOK, data=data, files=files, timeout=30)
    print(f"  [DISCORD] image post: {r.status_code}")
    return r.status_code in (200, 204)


def main():
    print("=== DAILY GEX/VEX UNIFIED DASHBOARD -- production run ===\n")
    et_now = datetime.now(ET)
    today_date = et_now.date()
    week_label = get_week_label()

    print("Fetching real data for all 10 tickers...")
    core_results = [gex_vex.compute_gex_vex(t, expiries=None) for t in CORE_TICKERS]
    mag7_results = [gex_vex.compute_gex_vex(t, expiries=None) for t in MAG7_TICKERS]

    for r in core_results + mag7_results:
        if "error" in r:
            print(f"  {r.get('ticker', '?')}: ERROR -- {r['error']}")
        else:
            print(f"  {r['ticker']}: OK -- spot=${r['spot']:.2f} net_gex={r['net_gex']/1e9:+.2f}B")

    errored_mag7 = [r.get("ticker", "?") for r in mag7_results if "error" in r]
    if errored_mag7:
        print(f"\n  WARNING: {len(errored_mag7)}/{len(MAG7_TICKERS)} Mag 7 ticker(s) errored out this run "
              f"and will be MISSING from the table: {', '.join(errored_mag7)} "
              f"-- see the ERROR lines above for the specific reason.")

    # REDESIGNED (2026-08-13): the separate "Since Yesterday" text
    # message is GONE -- confirmed with the user it would have become
    # a wall of text bombardment stacked on top of the card for up to
    # 10 tickers, defeating the entire point of consolidating the old
    # 8-post pipeline into one card. Instead: (1) a compact delta +
    # flip icon is drawn directly on each ticker's own card/table row
    # (glanceable data, no prose), and (2) any REGIME FLIP today
    # becomes a top-priority "Today's Focus" item using the real
    # plain-English paragraph -- so the "what should I do about this"
    # explanation still exists, but only for what's actually notable,
    # in the ONE panel already designed to carry that kind of text.
    print("\nComputing Since Yesterday comparisons (real gex_vex_history, unchanged logic)...")
    gex_vex_history.ensure_table()
    comparisons = {}
    for r in core_results + mag7_results:
        if "error" in r:
            continue
        try:
            gex_vex_history.save_snapshot(r, today_date)
            comparisons[r["ticker"]] = gex_vex_history.get_comparison_summary(r, today_date)
        except Exception as e:
            print(f"  {r['ticker']}: comparison failed: {e}")
            comparisons[r["ticker"]] = {"has_comparison": False, "spot_change_pct": None,
                                          "regime_flipped": False, "flip_direction": None, "plain_text": ""}
        c = comparisons[r["ticker"]]
        if c["regime_flipped"]:
            print(f"  {r['ticker']}: REGIME FLIP ({c['flip_direction']})")

    print("\nPicking Today's Focus...")
    focus_items = pick_todays_focus(core_results, mag7_results, comparisons)
    for t, color, tag, desc in focus_items:
        print(f"  {t['ticker']}: {tag}")

    print("\nRendering unified card...")
    out_path = "gex_unified_test.png"
    render_unified_card(core_results, mag7_results, focus_items, comparisons, week_label, out_path)
    print(f"  saved to {out_path}")

    print("\nPosting to test webhook...")
    header = f"\U0001F4CA **DAILY GEX / VEX DASHBOARD \u2014 {week_label}**"
    post_text_to_discord(header)
    post_image_to_discord(out_path)

    print("\n=== DONE ===")


if __name__ == "__main__":
    main()