r"""
gex_vex_unified_daily.py -- PRODUCTION single-card GEX/VEX dashboard.

RESTYLE v2 (2026-09-28) -- the render + Today's Focus logic described in
the sections below were REPLACED by a verdict-first, 2560px-wide layout
(pure presentation change; data pipeline, history logic, webhook, cron,
market-holiday gate and output filename are unchanged):
  header -> VERDICT band (deterministic headline + plain-English body) ->
  SPY/QQQ/IWM cards -> Mag 7 positioning bars -> Today's Focus (1-3
  deterministic picks: regime flips first, then widest expected move) ->
  one-line footer. The "Key Terms" strip is gone from the image; the
  glossary lives in a pinned channel message. Sections below that talk
  about the old matplotlib card, the KEY LEVELS / BREAKOUT WATCH focus
  tags, the 2-item flip cap and the flip-marker bounds guard are
  historical: the shared tick_bar() now widens its range to include the
  flip (and spot), so a marker can never leave its own bar. Everything
  from "theme" down to build_day() is the renderer; it is fed one `day`
  dict (see DATA CONTRACT in build_day) and every number drawn traces to it.


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
band-matching fix and the 2026-09-13 near-term-expiry redesign,
documented in that file's own module docstring, which change the
VALUES compute_gex_vex() returns, not this script's own logic).

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

MARKET-HOLIDAY GATE (2026-09-13): confirmed in production that this
script has NO check anywhere for whether TODAY is actually a trading
day before running -- the cron schedule itself (30 14 * * 1-5, weekday-
only) has no concept of market holidays, so a run on e.g. Labor Day
would fire normally and post a full dashboard built from whatever stale
data Alpaca happens to return for a day the market never opened. This
is the same category of bug already found and fixed twice elsewhere in
this same BMT stack this week (bmt_nightly_setups.py's flow-session-
date defaulting to a dead day, bmt_weekly_insights.py's week-boundary
title/day-labeling). Fixed by importing and checking
market_hours.market_closed_reason() -- the SAME shared holiday-
detection utility already used correctly elsewhere in this repo (e.g.
weekly_recap.py's resolve_recap_window() already rolls back to the last
real trading day using this exact module, which is why that script
never had this bug) -- at the very top of main(), before any data is
fetched. A closed-market day now logs the reason and returns
immediately, posting nothing, rather than publishing a dashboard for a
day that never traded.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import matplotlib
from PIL import Image, ImageDraw, ImageFont

import gex_vex
import gex_vex_history
from market_hours import market_closed_reason

DISCORD_WEBHOOK = os.environ["GEX_DISCORD_WEBHOOK"]
ET = ZoneInfo("America/New_York")

MAG7_TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]
CORE_TICKERS = ["SPY", "QQQ", "IWM"]


# ---------------------------------------------------------------- theme
BG, CARD, BORDER = "#0A0D14", "#141925", "#262F42"
TEXT, MUTED = "#E8ECF1", "#8B93A3"
RED, GREEN, AMBER, BLUE, BARMID = "#FF5D6C", "#2FD08C", "#F5B942", "#6EA0E6", "#3A4258"
WHITE = "#FFFFFF"

W = 2560
MARGIN = 60
MIN_FONT = 24
MAX_BYTES = 8 * 1024 * 1024
LABEL_GAP = 16

_FONT_DIR = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
_font_cache = {}


def font(size, bold=False):
    key = (size, bold)
    if key not in _font_cache:
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        _font_cache[key] = ImageFont.truetype(os.path.join(_FONT_DIR, name), size)
    return _font_cache[key]


def rgb(hex_):
    hex_ = hex_.lstrip("#")
    return tuple(int(hex_[i:i + 2], 16) for i in (0, 2, 4))


def blend(fg, bg, a):
    f, b = rgb(fg), rgb(bg)
    return tuple(int(f[i] * a + b[i] * (1 - a)) for i in range(3))


def tw(draw, text, f):
    return draw.textlength(text, font=f)


def fmt_px(v):
    """Price-like number: integers bare, otherwise 2 decimals."""
    return f"{v:,.0f}" if float(v) == int(v) else f"{v:,.2f}"


def money(v):
    return f"${v:,.2f}"


def ref_level(i):
    """A price to quote in prose for 'near X': the put wall if there is one, else the gamma flip,
    else the call wall, else spot. Walls can be None when gex_vex.py rejects a weak wall."""
    for k in ("put_wall", "gamma_flip", "call_wall"):
        if i.get(k) is not None:
            return i[k]
    return i["spot"]


# ------------------------------------------------------- derived helpers
def is_short(net_gex):
    return net_gex < 0


def regime_of(net_gex):
    return "SHORT" if is_short(net_gex) else "LONG"


def day_change_pct(ix):
    if ix.get("prev_close") in (None, 0):
        return None
    return (ix["spot"] / ix["prev_close"] - 1) * 100


def exp_move_dollar(x):
    return x["spot"] * x["exp_move_pct"] / 100


def join_names(names):
    return " and ".join(names) if len(names) <= 2 else ", ".join(names[:-1]) + " and " + names[-1]


# ---------------------------------------------------------- verdict engine
def verdict(day):
    ixs = day["indexes"]
    short = [i for i in ixs if is_short(i["net_gex"])]
    long_ = [i for i in ixs if not is_short(i["net_gex"])]
    spy = next((i for i in ixs if i["ticker"] == "SPY"), None)

    if len(short) == len(ixs):
        headline, accent = "FRAGILE DAY — BREAKS CAN RUN", RED
    elif len(long_) == len(ixs):
        headline, accent = "STICKY DAY — MOVES LIKELY TO FADE", GREEN
    elif spy is not None and is_short(spy["net_gex"]) and long_:
        headline, accent = "MIXED DAY — FRAGILE UNDERNEATH", AMBER
    elif spy is not None and not is_short(spy["net_gex"]) and short:
        headline, accent = "MIXED DAY — SPY ANCHORED", AMBER
    else:
        headline, accent = "MIXED DAY", AMBER

    sentences = []
    if short:
        names = join_names([i["ticker"] for i in short])
        sentences.append(f"{names} {'is' if len(short) == 1 else 'are'} short gamma: "
                         f"once a level breaks, the move can run further than usual.")
    if long_:
        def near(i):
            return fmt_px(i["gamma_flip"] if i["gamma_flip"] is not None else ref_level(i))

        def flipped(i):
            return regime_of(i["net_gex"]) != i["regime_prev"]

        groups = []
        for verb, sel in (("flipped into", [i for i in long_ if flipped(i)]),
                          ("sits in", [i for i in long_ if not flipped(i)])):
            if sel:
                groups.append((verb, sel))
        parts = []
        for verb, sel in groups:
            names = join_names([i["ticker"] for i in sel])
            where = (f"near {near(sel[0])}" if len(sel) == 1
                     else "near " + join_names([f"{near(i)} ({i['ticker']})" for i in sel]))
            parts.append(f"{names} {verb} long gamma {where}")
        sentences.append(" and ".join(parts) + " — stickier, so fast moves are more likely "
                         "to fade than follow through.")
    sentences.append("Trade smaller than usual and respect the walls.")
    return headline, accent, " ".join(s for s in sentences if s)


# ------------------------------------------------------------ focus picker
def pick_focus(day):
    """Returns up to 3 dicts: tag, tag_color, ticker, blurb (priority order)."""
    picks = []
    for ix in day["indexes"]:
        now = regime_of(ix["net_gex"])
        if now == ix["regime_prev"]:
            continue
        chg = day_change_pct(ix)
        t = ix["ticker"]
        if now == "SHORT":
            color = RED
            move = ""
            if chg is not None:
                move = f"{t} {'down' if chg < 0 else 'up'} {'barely ' if abs(chg) < 0.3 else ''}{abs(chg):.1f}%, but"
            lead = move if move else f"{t}:"
            blurb = (f"{lead} this setup got less stable — near {fmt_px(ref_level(ix))} "
                     f"there's less to slow a move down. Swings could run further than a normal "
                     f"session: trade smaller, and don't expect dips to get bought back fast.")
        else:
            color = AMBER
            move = ""
            if chg is not None:
                move = f"{t} {'down' if chg < 0 else 'up'} {abs(chg):.1f}% yet"
            lead = move if move else f"{t}"
            near = (f"near the {fmt_px(ix['gamma_flip'])} flip" if ix["gamma_flip"] is not None
                    else f"near {fmt_px(ref_level(ix))}")
            blurb = (f"{lead} flipped into a steadier setup {near} — price has a natural "
                     f"brake now. Fast moves in either direction are more likely to fade than "
                     f"follow through: lean on the range, don't chase breakouts.")
        picks.append(dict(tag="REGIME FLIP", tag_color=color, ticker=t, blurb=blurb))

    if day["mag7"]:
        top = max(day["mag7"], key=lambda m: m["exp_move_pct"])
        blurb = (f"Widest expected move of the group at ±{top['exp_move_pct']:.2f}% "
                 f"(±{money(exp_move_dollar(top))}). Bigger swings cut both ways here "
                 f"— size accordingly.")
        picks.append(dict(tag="HIGH RISK", tag_color=AMBER, ticker=top["ticker"], blurb=blurb))
    return picks[:3]


# --------------------------------------------------------------- primitives
def rrect(draw, box, radius, fill=None, outline=None, width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def pill(draw, x, y, h, text, color, size, bg, min_w=0):
    """Outlined pill, left-anchored at x, top at y. Returns width."""
    f = font(size, True)
    w = max(min_w, tw(draw, text, f) + 44)
    rrect(draw, (x, y, x + w, y + h), h // 2, fill=blend(color, bg, 0.14), outline=rgb(color), width=3)
    draw.text((x + w / 2, y + h / 2 + 1), text, font=f, fill=rgb(color), anchor="mm")
    return w


def wrap(draw, text, f, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if tw(draw, trial, f) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def gradient_bar(img, x, y, w, h):
    stops = [rgb(RED), rgb(BARMID), rgb(GREEN)]
    strip = Image.new("RGB", (w, 1))
    px = strip.load()
    for i in range(w):
        t = i / max(w - 1, 1) * 2
        k = min(int(t), 1)
        f = t - k
        px[i, 0] = tuple(int(stops[k][c] * (1 - f) + stops[k + 1][c] * f) for c in range(3))
    strip = strip.resize((w, h))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=255)
    img.paste(strip, (int(x), int(y)), mask)


def dashed_vline(draw, x, y0, y1, color, width=4, on=8, off=6):
    y = y0
    while y < y1:
        draw.line((x, y, x, min(y + on, y1)), fill=color, width=width)
        y += on + off


def decollide(widths, centers, lo, hi, gap=LABEL_GAP):
    """Left edges for labels (already sorted by centre). Push right to keep
    `gap`, clamp inside [lo, hi], then re-resolve leftwards."""
    n = len(widths)
    left = [c - w / 2 for c, w in zip(centers, widths)]
    left = [max(l, lo) for l in left]
    for i in range(1, n):
        left[i] = max(left[i], left[i - 1] + widths[i - 1] + gap)
    if n and left[-1] + widths[-1] > hi:
        left[-1] = hi - widths[-1]
        for i in range(n - 2, -1, -1):
            left[i] = min(left[i], left[i + 1] - widths[i] - gap)
    return left


def tick_bar(img, draw, x, y, w, h, *, spot, put_wall, call_wall, gamma_flip,
             label_size, bg):
    """Shared put-wall -> call-wall bar. (x, y) = top-left of the bar.
    Returns dict with the ticks drawn and label boxes (for invariants)."""
    # A wall can legitimately be None (gex_vex.py rejects a wall holding < MIN_WALL_SHARE of the
    # band's gamma -- e.g. QQQ's put wall on 2026-10-05). Draw only the levels that exist.
    vals = [v for v in (put_wall, call_wall, gamma_flip) if v is not None]
    lo, hi = min(vals + [spot]), max(vals + [spot])
    span = (hi - lo) or abs(spot) * 0.01 or 1.0
    lo -= 0.07 * span
    hi += 0.03 * span

    def X(v):
        return x + (v - lo) / (hi - lo) * w

    gradient_bar(img, x, y, w, h)
    cy = y + h / 2
    ext = 9
    drawn = {}

    if gamma_flip is not None:
        dashed_vline(draw, X(gamma_flip), y - ext, y + h + ext, rgb(AMBER))
        drawn["flip"] = gamma_flip
    for key, v, col in (("put", put_wall, RED), ("call", call_wall, GREEN)):
        if v is None:
            continue
        draw.line((X(v), y - ext, X(v), y + h + ext), fill=rgb(col), width=5)
        drawn[key] = v

    r = h * 0.62
    draw.ellipse((X(spot) - r, cy - r, X(spot) + r, cy + r), fill=rgb(WHITE),
                 outline=rgb(bg), width=4)

    ticks = ([("put", put_wall, RED)] if put_wall is not None else []) + \
            ([("flip", gamma_flip, AMBER)] if gamma_flip is not None else []) + \
            ([("call", call_wall, GREEN)] if call_wall is not None else [])
    ticks.sort(key=lambda t: X(t[1]))
    f = font(label_size, True)
    texts = [fmt_px(t[1]) for t in ticks]
    widths = [tw(draw, s, f) for s in texts]
    lefts = decollide(widths, [X(t[1]) for t in ticks], x, x + w)
    ly = y + h + ext + 8
    boxes = []
    for (key, v, col), s, wd, l in zip(ticks, texts, widths, lefts):
        draw.text((l, ly), s, font=f, fill=rgb(col), anchor="la")
        boxes.append((l, l + wd))

    # invariants: no overlap, inside the bar
    for (a0, a1), (b0, b1) in zip(boxes, boxes[1:]):
        assert b0 - a1 >= LABEL_GAP - 0.01, "tick labels overlap"
    assert not boxes or (boxes[0][0] >= x - 0.01 and boxes[-1][1] <= x + w + 0.01), \
        "tick labels escape the bar"
    assert ("flip" in drawn) == (gamma_flip is not None)
    assert ("put" in drawn) == (put_wall is not None)
    assert ("call" in drawn) == (call_wall is not None)
    return dict(drawn=drawn, boxes=boxes)


def segments(draw, x, y, parts, size, anchor_h="l"):
    """Draw [(text, color, bold)] left to right at baseline-middle y."""
    for text, color, bold in parts:
        f = font(size, bold)
        draw.text((x, y), text, font=f, fill=rgb(color), anchor="lm")
        x += tw(draw, text, f)


def segments_width(draw, parts, size):
    return sum(tw(draw, t, font(size, b)) for t, _, b in parts)


# ------------------------------------------------------------------ render
def render_day(day, out_path):
    tall = Image.new("RGB", (W, 6000), rgb(BG))
    d = ImageDraw.Draw(tall)
    inner_w = W - 2 * MARGIN
    y = 44

    # 1. header
    d.text((W / 2, y), "DAILY GEX / VEX", font=font(84, True), fill=rgb(TEXT), anchor="ma")
    y += 108
    d.text((W / 2, y), f"{day['week_label']}  ·  {day['brand']}", font=font(34),
           fill=rgb(BLUE), anchor="ma")
    y += 78

    # 2. verdict band
    headline, accent, body = verdict(day)
    bf = font(31)
    lines = wrap(d, body, bf, inner_w - 2 * 44 - 16)
    vh = 30 + 50 + 12 + len(lines) * 44 + 24
    rrect(d, (MARGIN, y, W - MARGIN, y + vh), 18, fill=rgb(CARD), outline=rgb(BORDER), width=2)
    d.rounded_rectangle((MARGIN, y + 14, MARGIN + 12, y + vh - 14), radius=6, fill=rgb(accent))
    d.text((MARGIN + 44, y + 28), headline, font=font(42, True), fill=rgb(accent), anchor="la")
    ty = y + 28 + 50 + 12
    for ln in lines:
        d.text((MARGIN + 44, ty), ln, font=bf, fill=rgb(TEXT), anchor="la")
        ty += 44
    y += vh + 30

    # 3. index cards
    gap = 30
    cw = (inner_w - 2 * gap) / 3
    pad = 36
    ch = 440
    for n, ix in enumerate(day["indexes"]):
        cx = MARGIN + n * (cw + gap)
        rrect(d, (cx, y, cx + cw, y + ch), 22, fill=rgb(CARD), outline=rgb(BORDER), width=2)
        short = is_short(ix["net_gex"])
        rc = RED if short else GREEN
        # row 1
        tf = font(52, True)
        tick_s = f"${ix['ticker']}"
        d.text((cx + pad, y + 28), tick_s, font=tf, fill=rgb(TEXT), anchor="la")
        pill(d, cx + pad + tw(d, tick_s, tf) + 26, y + 34, 54,
             "SHORT GAMMA" if short else "LONG GAMMA", rc, 26, CARD)
        d.text((cx + cw - pad, y + 24), f"{ix['spot']:,.2f}", font=font(46, True),
               fill=rgb(TEXT), anchor="ra")
        chg = day_change_pct(ix)
        if chg is not None:
            arrow = "▲" if chg >= 0 else "▼"
            d.text((cx + cw - pad, y + 84), f"{arrow} {abs(chg):.1f}% vs yesterday", font=font(26),
                   fill=rgb(GREEN if chg >= 0 else RED), anchor="ra")
        # row 2
        by = y + 192
        put_hd = "PUT WALL" if ix["put_wall"] is not None else "PUT WALL: none clear"
        call_hd = "CALL WALL" if ix["call_wall"] is not None else "CALL WALL: none clear"
        d.text((cx + pad, y + 150), put_hd, font=font(24, True),
               fill=rgb(RED if ix["put_wall"] is not None else MUTED), anchor="la")
        d.text((cx + cw - pad, y + 150), call_hd, font=font(24, True),
               fill=rgb(GREEN if ix["call_wall"] is not None else MUTED), anchor="ra")
        tick_bar(tall, d, int(cx + pad), by, int(cw - 2 * pad), 28, spot=ix["spot"],
                 put_wall=ix["put_wall"], call_wall=ix["call_wall"],
                 gamma_flip=ix["gamma_flip"], label_size=28, bg=CARD)
        # row 3
        em_d = exp_move_dollar({"spot": ix["spot"], "exp_move_pct": ix["exp_move_pct"]})
        gex_s = f"{'-' if ix['net_gex'] < 0 else '+'}${abs(ix['net_gex']) / 1e9:.2f}B"
        vex_s = f"{ix['net_vex'] / 1e9:+.2f}B"
        line1 = [("Gamma flip ", MUTED, False),
                 (fmt_px(ix["gamma_flip"]) if ix["gamma_flip"] is not None else "N/A", TEXT, True),
                 ("   Expected move ", MUTED, False),
                 (f"±{ix['exp_move_pct']:.2f}% (±{money(em_d)})", AMBER, True)]
        line2 = [("Net GEX ", MUTED, False), (gex_s, RED if ix["net_gex"] < 0 else GREEN, True),
                 ("   Net VEX ", MUTED, False), (vex_s, RED if ix["net_vex"] < 0 else GREEN, True)]
        size = 25
        avail = cw - 2 * pad
        if max(segments_width(d, line1, size), segments_width(d, line2, size)) > avail:
            size = MIN_FONT
        assert max(segments_width(d, line1, size), segments_width(d, line2, size)) <= avail, \
            "stat lines do not fit card"
        segments(d, cx + pad, y + ch - 92, line1, size)
        segments(d, cx + pad, y + ch - 46, line2, size)
    y += ch + 30

    # 4. mag 7
    rows = day["mag7"]
    row_h = 126
    head_h = 150
    mh = head_h + row_h * len(rows) + 20
    rrect(d, (MARGIN, y, W - MARGIN, y + mh), 22, fill=rgb(CARD), outline=rgb(BORDER), width=2)
    px0, px1 = MARGIN + 44, W - MARGIN - 44
    d.text((px0, y + 26), "MAG 7 POSITIONING", font=font(42, True), fill=rgb(TEXT), anchor="la")
    # legend (right-aligned, laid out right -> left)
    lf = font(26)
    ly = y + 46
    t3 = "red = put wall · green = call wall"
    xr = px1
    d.text((xr, ly), t3, font=lf, fill=rgb(MUTED), anchor="rm")
    xr -= tw(d, t3, lf) + 40
    d.text((xr, ly), "gamma flip", font=lf, fill=rgb(MUTED), anchor="rm")
    xr -= tw(d, "gamma flip", lf) + 12
    dashed_vline(d, xr - 30, ly - 14, ly + 14, rgb(AMBER), width=3, on=6, off=4)  # dashed marker
    xr -= 60
    d.text((xr, ly), "spot price", font=lf, fill=rgb(MUTED), anchor="rm")
    xr -= tw(d, "spot price", lf) + 12
    d.ellipse((xr - 20, ly - 10, xr, ly + 10), fill=rgb(MUTED))

    col_ticker, col_regime, col_spot, col_bar = px0, px0 + 270, px0 + 520, px0 + 780
    bar_w = 1180
    cf = font(24, True)
    cy0 = y + 108
    for label, cxp, anc in (("TICKER", col_ticker, "l"), ("REGIME", col_regime, "l"),
                            ("SPOT", col_spot, "l"), ("POSITIONING", col_bar, "l")):
        d.text((cxp, cy0), label, font=cf, fill=rgb(MUTED), anchor="lm")
    d.text((px1, cy0), "EXP MOVE", font=cf, fill=rgb(MUTED), anchor="rm")

    ry = y + head_h
    for i, m in enumerate(rows):
        if i:
            d.line((MARGIN + 20, ry - 4, W - MARGIN - 20, ry - 4), fill=rgb(BORDER), width=1)
        short = is_short(m["net_gex"])
        rc = RED if short else GREEN
        d.text((col_ticker, ry + 34), f"${m['ticker']}", font=font(40, True), fill=rgb(TEXT), anchor="lm")
        pill(d, col_regime, ry + 10, 48, "SHORT" if short else "LONG", rc, 26, CARD, min_w=150)
        d.text((col_spot, ry + 34), f"{m['spot']:,.2f}", font=font(36), fill=rgb(TEXT), anchor="lm")
        tick_bar(tall, d, col_bar, ry + 22, bar_w, 26, spot=m["spot"], put_wall=m["put_wall"],
                 call_wall=m["call_wall"], gamma_flip=m["gamma_flip"], label_size=26, bg=CARD)
        dollar = f" (±{money(exp_move_dollar(m))})"
        d_w = tw(d, dollar, font(26))
        d.text((px1, ry + 34), dollar, font=font(26), fill=rgb(MUTED), anchor="rm")
        d.text((px1 - d_w, ry + 34), f"±{m['exp_move_pct']:.2f}%", font=font(36, True),
               fill=rgb(AMBER), anchor="rm")
        ry += row_h
    y += mh + 40

    # 5. today's focus
    picks = pick_focus(day)
    assert 0 <= len(picks) <= 3
    if picks:
        d.text((MARGIN + 12, y), "TODAY'S FOCUS", font=font(42, True), fill=rgb(TEXT), anchor="la")
        y += 76
        bf = font(30)
        wrapped = [wrap(d, p["blurb"], bf, cw - 2 * pad) for p in picks]
        fh = 34 + 62 + 18 + max(len(w) for w in wrapped) * 42 + 30
        for n, (p, lines) in enumerate(zip(picks, wrapped)):
            cx = MARGIN + n * (cw + gap)
            rrect(d, (cx, y, cx + cw, y + fh), 22, fill=rgb(CARD), outline=rgb(BORDER), width=2)
            tag_w = pill(d, cx + pad, y + 30, 54, p["tag"], p["tag_color"], 24, CARD)
            d.text((cx + pad + tag_w + 26, y + 57), f"${p['ticker']}", font=font(46, True),
                   fill=rgb(TEXT), anchor="lm")
            ty = y + 34 + 62 + 18
            for ln in lines:
                d.text((cx + pad, ty), ln, font=bf, fill=rgb(TEXT), anchor="la")
                ty += 42
        y += fh + 34

    # 6. footer
    d.line((MARGIN, y, W - MARGIN, y), fill=rgb(BORDER), width=2)
    foot = ("Walls = heaviest positioning, act as floor & ceiling · Short gamma = breaks can "
            "run · Long gamma = moves fade · Full glossary pinned in this channel")
    fs = 28
    while tw(d, foot, font(fs)) > inner_w and fs > MIN_FONT:
        fs -= 1
    assert tw(d, foot, font(fs)) <= inner_w, "footer does not fit"
    d.text((W / 2, y + 26), foot, font=font(fs), fill=rgb(MUTED), anchor="ma")
    y += 26 + fs + 40

    out = tall.crop((0, 0, W, y))
    out.save(out_path, optimize=True)
    if os.path.getsize(out_path) >= MAX_BYTES:
        out.quantize(colors=128).save(out_path, optimize=True)
    assert os.path.getsize(out_path) < MAX_BYTES, "PNG exceeds Discord 8MB limit"
    return out_path


# ------------------------------------------------------------- adapter
def build_day(core_results, mag7_results, comparisons, week_label, brand="BlueMoonTrades"):
    """gex_vex.compute_gex_vex() results + gex_vex_history comparisons -> day dict.
    prev_close is back-derived from the stored day-over-day % change;
    regime_prev is the opposite regime iff history flagged a regime flip."""
    def em_pct(r):
        em = r.get("expected_move") or {}
        return em.get("pct")

    indexes = []
    for r in core_results:
        if "error" in r:
            continue
        c = comparisons.get(r["ticker"], {})
        pct = c.get("spot_change_pct") if c.get("has_comparison") else None
        now = regime_of(r["net_gex"])
        prev = ("LONG" if now == "SHORT" else "SHORT") if c.get("regime_flipped") else now
        indexes.append(dict(
            ticker=r["ticker"], spot=r["spot"],
            prev_close=(r["spot"] / (1 + pct / 100)) if pct is not None else None,
            put_wall=r["put_wall"], call_wall=r["call_wall"], gamma_flip=r.get("gamma_flip"),
            exp_move_pct=em_pct(r) or 0.0, net_gex=r["net_gex"], net_vex=r["net_vex"],
            regime_prev=prev))
    mag7 = [dict(ticker=r["ticker"], spot=r["spot"], put_wall=r["put_wall"],
                 call_wall=r["call_wall"], gamma_flip=r.get("gamma_flip"),
                 exp_move_pct=em_pct(r) or 0.0, net_gex=r["net_gex"])
            for r in mag7_results if "error" not in r]
    return dict(week_label=week_label, brand=brand, indexes=indexes, mag7=mag7)



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

    # MARKET-HOLIDAY GATE (2026-09-13) -- see module docstring for the
    # full incident this fixes. Must run BEFORE any data is fetched or
    # any Discord post happens.
    closed_reason = market_closed_reason()
    if closed_reason:
        print(f"  Market closed today ({closed_reason}) -- skipping this run entirely, nothing to post.")
        return

    week_label = get_week_label()

    print("Fetching real data for all 10 tickers...")
    core_results = [gex_vex.compute_gex_vex(t, expiries=None) for t in CORE_TICKERS]
    mag7_results = [gex_vex.compute_gex_vex(t, expiries=None) for t in MAG7_TICKERS]

    for r in core_results + mag7_results:
        if "error" in r:
            print(f"  {r.get('ticker', '?')}: ERROR -- {r['error']}")
        else:
            print(f"  {r['ticker']}: OK -- spot=${r['spot']:.2f} net_gex={r['net_gex']/1e9:+.2f}B "
                  f"expiry={r['expiries'][0]}")

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

    print("\nRendering unified card (v2 verdict-first layout)...")
    day = build_day(core_results, mag7_results, comparisons, week_label)
    for p in pick_focus(day):
        print(f"  focus: {p['ticker']}: {p['tag']}")
    out_path = "gex_unified_test.png"
    render_day(day, out_path)
    print(f"  saved to {out_path}")

    print("\nPosting to test webhook...")
    header = f"\U0001F4CA **DAILY GEX / VEX DASHBOARD \u2014 {week_label}**"
    post_text_to_discord(header)
    post_image_to_discord(out_path)

    print("\n=== DONE ===")


if __name__ == "__main__":
    main()