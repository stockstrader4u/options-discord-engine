"""
bmt_watchlist_card_v3.py — Nightly BMT watchlist card renderer (V3 ONLY).

Renders the nightly watchlist as a single 3705x1656 PNG using Pillow, per
the landscape spec (five setup cards in one horizontal row, dark terminal
theme). This REPLACES render_card()'s matplotlib implementation for the v3
test track only -- production's bmt_nightly_setups.py is untouched and
keeps its own matplotlib render_card().

Data contract: render_watchlist_card(setups, market, note, title_date,
subtitle, footer, out_path) where `setups` is a list of up to 5 dicts, each
built by build_setup_dict_from_v3(c) from a v3 pipeline candidate `c`. See
that function for the exact field mapping from v3's internal `c` dict to
this renderer's documented data contract (ticker, company, close, strike,
expiry, dte, pattern, entry, stop, t1, t2, flow, is_call, and the optional
exp_move_pct/delta/iv_rank fields).

No fabricated numbers anywhere: every figure on the card is parsed from the
setup dict or computed from it via the formulas below. Optional fields
(exp_move_pct, delta, iv_rank, breakeven) render only when present in the
input dict -- never as a placeholder or dash.

QA: assert_no_overlaps() checks bounding boxes of every rendered text
element pairwise within each card and flags any that intersect.
assert_r_multiples_roundtrip() recomputes R1/R2 from entry/stop/targets
and confirms they match the values used for the diverging bar within 0.05.
Both run automatically inside render_watchlist_card() before saving; a
failed assertion raises, so a bad render can never silently go out.
"""

import math
from PIL import Image, ImageDraw, ImageFont

# ── Canvas & palette (per spec, verbatim) ──────────────────────────────
CANVAS_W, CANVAS_H = 3705, 1656
BG = (11, 13, 16)            # #0b0d10
CARD_FILL = (18, 21, 26)     # #12151a
BORDER = (35, 38, 43)        # #23262b
TEXT_WHITE = (242, 244, 246)  # #f2f4f6
TEXT_MUTED = (154, 160, 168)  # #9aa0a8
TEXT_DIM = (107, 112, 120)    # #6b7078
GREEN = (47, 191, 113)        # #2fbf71
RED = (255, 93, 93)           # #ff5d5d
GREEN_BG = (20, 46, 34)       # dark-green tint for pill backgrounds
RED_BG = (56, 24, 24)         # dark-red tint for pill backgrounds

FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
FONT_REGULAR_PATH = FONT_DIR + "DejaVuSans.ttf"
FONT_BOLD_PATH = FONT_DIR + "DejaVuSans-Bold.ttf"

# FONT-PATH BUGFIX (confirmed on Railway, 2026-09-23): a real deploy
# crashed with "OSError: cannot open resource" trying to load
# /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf -- that hardcoded path
# is specific to the Debian/Ubuntu desktop font layout used during
# development and testing. A live `find / -iname "*.ttf"` on the actual
# Railway container confirmed there is NO system font directory at all --
# the only DejaVu TTFs anywhere on the machine are matplotlib's own
# bundled copies, at a venv-relative path like
# /app/.venv/lib/python3.13/site-packages/matplotlib/mpl-data/fonts/ttf/.
# This crashed the ENTIRE nightly run at the very last step (after every
# setup had already posted successfully to Discord) -- the whole pipeline
# is worthless if the one thing it can't do is finish rendering the
# summary card.
#
# Fixed by resolving the font path via matplotlib.get_data_path() --
# matplotlib is ALREADY a hard dependency of this file (used for the
# per-setup chart renderer), so its bundled fonts are guaranteed present
# wherever this script runs at all, regardless of venv path, Python
# version, or which base OS image Railway happens to be using this
# week. This is far more reliable than guessing OS-level system-font
# paths, which was the root cause here. A short list of old system-path
# guesses is kept as a fallback in case matplotlib's own data path ever
# changes shape, and a last-resort fallback to Pillow's bundled default
# bitmap font means this renderer can never again crash the whole run
# over a missing font file -- worst case it renders with plainer
# typography instead of not rendering at all.
def _matplotlib_font_paths():
    """Returns {False: <regular ttf path>, True: <bold ttf path>} from
    matplotlib's own bundled DejaVu Sans fonts, or {False: None, True: None}
    if matplotlib isn't importable for some reason (shouldn't happen, since
    this file already hard-imports it above, but defensive regardless)."""
    try:
        import matplotlib
        import os
        base = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
        regular = os.path.join(base, "DejaVuSans.ttf")
        bold = os.path.join(base, "DejaVuSans-Bold.ttf")
        return {
            False: regular if os.path.exists(regular) else None,
            True: bold if os.path.exists(bold) else None,
        }
    except Exception:
        return {False: None, True: None}


_CANDIDATE_FONT_PATHS = {
    False: [  # regular weight
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/DejaVuSans.ttf",
        "/usr/local/share/fonts/DejaVuSans.ttf",
        "/app/.fonts/DejaVuSans.ttf",
    ],
    True: [  # bold weight
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/DejaVuSans-Bold.ttf",
        "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
        "/app/.fonts/DejaVuSans-Bold.ttf",
    ],
}
_resolved_font_path = {False: None, True: None}  # cached per-weight, resolved lazily


def _resolve_font_path(bold: bool):
    """Finds a real, loadable DejaVu Sans TTF on this machine. Tries
    matplotlib's bundled copy FIRST (reliable, guaranteed present since
    matplotlib is a hard dependency of this codebase already), then falls
    back to the old system-path guesses, then gives up (caller falls back
    to Pillow's built-in default font in that case). Caches the first
    path that actually opens."""
    if _resolved_font_path[bold] is not None:
        return _resolved_font_path[bold]
    import os

    mpl_path = _matplotlib_font_paths()[bold]
    candidates = ([mpl_path] if mpl_path else []) + _CANDIDATE_FONT_PATHS[bold]

    for path in candidates:
        if path and os.path.exists(path):
            try:
                ImageFont.truetype(path, 10)  # cheap load test
                _resolved_font_path[bold] = path
                return path
            except Exception:
                continue
    return None

_font_cache = {}


_warned_fallback = {False: False, True: False}


def font(size: int, bold: bool = False):
    key = (size, bold)
    if key not in _font_cache:
        path = _resolve_font_path(bold)
        if path:
            _font_cache[key] = ImageFont.truetype(path, size)
        else:
            if not _warned_fallback[bold]:
                print(f"  [FONT WARN] No DejaVu Sans TTF found on this machine "
                      f"(tried: {_CANDIDATE_FONT_PATHS[bold]}) -- falling back to "
                      f"Pillow's built-in default font. Card will render, but "
                      f"typography will not match the design spec.")
                _warned_fallback[bold] = True
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


# ── Small drawing helpers ───────────────────────────────────────────────

def text_w(draw, s, f):
    bbox = draw.textbbox((0, 0), s, font=f)
    return bbox[2] - bbox[0]


def text_h(draw, s, f):
    bbox = draw.textbbox((0, 0), s, font=f)
    return bbox[3] - bbox[1]


def draw_text(draw, xy, s, f, fill, anchor="la", track_boxes=None, label=None):
    """Draws text and, if track_boxes is given, records its bounding box
    (in absolute canvas coords) tagged with `label` for the overlap QA
    check. anchor follows Pillow's anchor convention (default left/ascender)."""
    draw.text(xy, s, font=f, fill=fill, anchor=anchor)
    if track_boxes is not None:
        bbox = draw.textbbox(xy, s, font=f, anchor=anchor)
        track_boxes.append((label or s, bbox))


def rounded_rect(draw, box, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def letterspaced(s: str, spaces: int = 1) -> str:
    return (" " * spaces).join(list(s))


# ── Parsing / formulas (per spec) ───────────────────────────────────────

def parse_entry_range(entry_str: str):
    """Parses 'entry' field on '-' or '\u2013' (en dash), per spec."""
    s = entry_str.replace("\u2013", "-")
    parts = s.split("-")
    if len(parts) != 2:
        raise ValueError(f"Cannot parse entry range: {entry_str!r}")
    lo, hi = float(parts[0]), float(parts[1])
    return lo, hi


def compute_derived(setup: dict) -> dict:
    """Computes every formula-derived value the card needs, per spec:
    otm%, entry_mid, risk_pct, t2_pct, R1, R2, 1R$."""
    close = setup["close"]
    strike = setup["strike_num"]
    entry_lo, entry_hi = parse_entry_range(setup["entry"])
    entry_mid = (entry_lo + entry_hi) / 2
    stop = setup["stop"]
    t1 = setup["t1"]
    t2 = setup["t2"]

    otm = (strike - close) / close * 100

    risk_pct = (stop - entry_mid) / entry_mid * 100
    t2_pct = (t2 - entry_mid) / entry_mid * 100
    t1_pct = (t1 - entry_mid) / entry_mid * 100

    one_r_dollars = abs(entry_mid - stop)
    # R multiples: guard divide-by-zero (entry_mid == stop is a
    # degenerate/invalid setup, but never crash the renderer over it --
    # surface it as None and let the caller's QA assertion catch it).
    if one_r_dollars != 0:
        r1 = (t1 - entry_mid) / (entry_mid - stop)
        r2 = (t2 - entry_mid) / (entry_mid - stop)
    else:
        r1 = r2 = None

    return {
        "entry_lo": entry_lo, "entry_hi": entry_hi, "entry_mid": entry_mid,
        "otm": otm, "risk_pct": risk_pct, "t2_pct": t2_pct, "t1_pct": t1_pct,
        "r1": r1, "r2": r2, "one_r_dollars": one_r_dollars,
    }


def assert_valid_trade_geometry(setup: dict, derived: dict):
    """Sanity check beyond pure round-trip: for a CALL, stop must be below
    entry and targets above; for a PUT, the reverse. A setup violating this
    would still 'round-trip' numerically (the formulas are internally
    consistent) but represents impossible/nonsensical trade geometry, so
    it's caught separately here rather than silently rendered."""
    entry_mid = derived["entry_mid"]
    stop = setup["stop"]
    t1, t2 = setup["t1"], setup["t2"]
    is_call = setup["is_call"]
    if is_call:
        if not (stop < entry_mid < t1 < t2 or stop < entry_mid <= t1 <= t2):
            raise AssertionError(
                f"{setup['ticker']}: invalid CALL geometry -- expected stop < entry < T1 <= T2, "
                f"got stop={stop}, entry_mid={entry_mid}, t1={t1}, t2={t2}"
            )
    else:
        if not (stop > entry_mid > t1 > t2 or stop > entry_mid >= t1 >= t2):
            raise AssertionError(
                f"{setup['ticker']}: invalid PUT geometry -- expected stop > entry > T1 >= T2, "
                f"got stop={stop}, entry_mid={entry_mid}, t1={t1}, t2={t2}"
            )


def assert_r_multiples_roundtrip(setup: dict, derived: dict, tol: float = 0.05):
    """QA: recomputes R1/R2 independently from entry/stop/targets (same
    formula, computed a second time from the raw inputs rather than reusing
    the already-computed derived dict) and confirms they match within tol.
    This guards against a future edit to compute_derived() introducing a
    silent formula drift between what's computed and what's displayed."""
    entry_lo, entry_hi = parse_entry_range(setup["entry"])
    entry_mid = (entry_lo + entry_hi) / 2
    stop = setup["stop"]
    risk = entry_mid - stop
    if risk == 0:
        raise AssertionError(f"{setup['ticker']}: entry_mid equals stop -- degenerate R multiple, cannot render")
    r1_check = (setup["t1"] - entry_mid) / risk
    r2_check = (setup["t2"] - entry_mid) / risk
    if derived["r1"] is None or abs(r1_check - derived["r1"]) > tol:
        raise AssertionError(f"{setup['ticker']}: R1 round-trip mismatch ({r1_check} vs {derived['r1']})")
    if derived["r2"] is None or abs(r2_check - derived["r2"]) > tol:
        raise AssertionError(f"{setup['ticker']}: R2 round-trip mismatch ({r2_check} vs {derived['r2']})")


def assert_no_overlaps(all_boxes: list, card_label: str = ""):
    """QA: pairwise bounding-box intersection check across every tracked
    text element within a card. Adjacent lines that merely touch (share an
    edge) are not overlaps; only genuine rectangle intersection with
    positive area counts."""
    def overlaps(a, b):
        ax0, ay0, ax1, ay1 = a
        bx0, by0, bx1, by1 = b
        ix0, iy0 = max(ax0, bx0), max(ay0, by0)
        ix1, iy1 = min(ax1, bx1), min(ay1, by1)
        return ix1 > ix0 and iy1 > iy0

    for i in range(len(all_boxes)):
        for j in range(i + 1, len(all_boxes)):
            label_a, box_a = all_boxes[i]
            label_b, box_b = all_boxes[j]
            if overlaps(box_a, box_b):
                raise AssertionError(
                    f"{card_label}: text overlap between {label_a!r} {box_a} and {label_b!r} {box_b}"
                )


# ── Header rendering ─────────────────────────────────────────────────────

def render_header(draw, market: dict, note: str, title_date: str, subtitle: str):
    cursor_y = 40
    kicker_font = font(34, bold=False)
    draw_text(draw, (60, cursor_y), letterspaced("BMT WATCHLIST", 2), kicker_font, TEXT_DIM)
    cursor_y += 52

    title_font = font(110, bold=True)
    draw_text(draw, (56, cursor_y), title_date, title_font, TEXT_WHITE)
    cursor_y += 128

    subtitle_font = font(40, bold=False)
    draw_text(draw, (60, cursor_y), subtitle, subtitle_font, TEXT_MUTED)
    cursor_y += 56

    draw.line([(60, cursor_y), (CANVAS_W - 60, cursor_y)], fill=BORDER, width=2)
    cursor_y += 32

    # Market band: three equal columns, each with its own ticker+price on
    # the left and its own pill right-aligned WITHIN that same column
    # (not the far right of the canvas) so three tickers don't visually
    # collide into one cluster.
    col_w = (CANVAS_W - 120) / 3
    tick_font = font(34, bold=False)
    price_font = font(84, bold=True)
    note_font = font(32, bold=False)
    pill_font = font(38, bold=True)

    band_top = cursor_y
    for i, sym in enumerate(("spy", "qqq", "iwm")):
        m = market[sym]
        x0 = 60 + i * col_w
        col_right = x0 + col_w - 30  # 30px gutter before next column starts
        y = band_top
        draw_text(draw, (x0, y), f"${sym.upper()}", tick_font, TEXT_DIM)

        pct = m["pct"]
        is_up = pct >= 0
        arrow = "\u2191" if is_up else "\u2193"
        pill_text = f"{arrow} {abs(pct):.2f}%"
        pill_color = GREEN if is_up else RED
        pill_bg = GREEN_BG if is_up else RED_BG
        pw = text_w(draw, pill_text, pill_font)
        pad_x = 26
        pill_h = 62
        pill_x1 = col_right
        pill_x0 = pill_x1 - (pw + pad_x * 2)
        rounded_rect(draw, (pill_x0, y, pill_x1, y + pill_h), radius=14, fill=pill_bg)
        draw_text(draw, ((pill_x0 + pill_x1) / 2, y + pill_h / 2), pill_text, pill_font,
                  pill_color, anchor="mm")

        y += 50
        draw_text(draw, (x0, y), f"${m['price']:g}", price_font, TEXT_WHITE)
        y += 100
        draw_text(draw, (x0, y), m["note"], note_font, TEXT_MUTED)

    cursor_y = band_top + 190

    # Green-left-bar note line -- bar vertically centered on the text's
    # cap-height, not the text's full ascender/descender box.
    note_font_obj = font(36, bold=False)
    bar_w = 6
    bar_h = 34
    text_top = cursor_y
    draw.rectangle((60, text_top + 4, 60 + bar_w, text_top + 4 + bar_h), fill=GREEN)
    draw_text(draw, (60 + bar_w + 24, text_top), note, note_font_obj, TEXT_WHITE)
    cursor_y += 62

    return cursor_y


# ── Risk/reward diverging bar panel ─────────────────────────────────────

def render_risk_reward_panel(draw, x0, y0, w, h, setup, derived, boxes):
    """
    LAYOUT BUGFIX (confirmed 2026-09-23 from real posted output, then
    revised after a first fix attempt was judged still visually weak):
    the diverging bar's red (STOP) segment was scaled against
    max(risk_pct, reward_pct) -- since T2's percentage is almost always
    3-4x STOP's, the red segment only filled a small fraction of its own
    half of the track, leaving a long stretch of visibly empty track. A
    first fix attempt (a thin outlined "lane" spanning the full
    half-width) didn't meaningfully help -- at this bar's thickness the
    outline was too subtle to register, and the underlying issue wasn't
    really about a missing visual anchor, it was that a genuinely
    3-4x-different pair of values will always make a pure linear-scale
    bar look lopsided and sparse on one side, no matter how the empty
    remainder is decorated.

    Fix: apply a square-root compression to both values before scaling
    (sqrt is a common, well-understood technique for exactly this --
    compressing a wide dynamic range so the SMALLER value still gets a
    visually meaningful share of the track, while still preserving true
    ordering and directional signal: risk always looks smaller than
    reward when it IS smaller, just not by an exaggerated linear ratio).
    The exact numbers remain fully accurate in the text below the bar
    (STOP -X.X%, T2 Y.YR (+Z.Z%)) -- only the BAR'S length mapping is
    compressed, purely a visual-legibility choice, never the underlying
    data.
    """
    label_font = font(26, bold=False)
    draw_text(draw, (x0, y0), "RISK / REWARD vs ENTRY", label_font, TEXT_DIM,
              track_boxes=boxes, label="rr_title")

    bar_y = y0 + 50
    bar_h = 28
    bar_x0 = x0
    bar_x1 = x0 + w
    bar_mid_x = (bar_x0 + bar_x1) / 2

    risk_pct = abs(derived["risk_pct"])
    reward_pct = abs(derived["t2_pct"])
    # Square-root compression: both values pass through sqrt() before
    # scaling, so a 4x difference in the raw percentages becomes only a
    # 2x difference in bar length -- enough to still show which side is
    # bigger, without letting the smaller side collapse to a sliver.
    risk_compressed = math.sqrt(risk_pct)
    reward_compressed = math.sqrt(reward_pct)
    scale = max(risk_compressed, reward_compressed, 0.01)
    half_w = (w / 2) * 0.92  # leave a little margin so bars don't touch the edges

    # Track background (full width).
    rounded_rect(draw, (bar_x0, bar_y, bar_x1, bar_y + bar_h), radius=bar_h // 2, fill=CARD_FILL, outline=BORDER, width=2)

    red_len = (risk_compressed / scale) * half_w
    green_len = (reward_compressed / scale) * half_w

    rounded_rect(draw, (bar_mid_x - red_len, bar_y, bar_mid_x, bar_y + bar_h), radius=bar_h // 2, fill=RED)
    rounded_rect(draw, (bar_mid_x, bar_y, bar_mid_x + green_len, bar_y + bar_h), radius=bar_h // 2, fill=GREEN)

    # Center tick (dark, at entry) and T1 tick (white). T1's position
    # uses the SAME sqrt-compressed scale as the bar segments, so the
    # tick lands at the correct visual position relative to the
    # (compressed) green segment, not at a position implying a different
    # scale than what's actually drawn.
    tick_w = 4
    draw.rectangle((bar_mid_x - tick_w / 2, bar_y - 6, bar_mid_x + tick_w / 2, bar_y + bar_h + 6), fill=(20, 22, 26))
    t1_compressed = math.sqrt(abs(derived["t1_pct"]))
    t1_frac = t1_compressed / scale
    t1_x = bar_mid_x + t1_frac * half_w
    draw.rectangle((t1_x - tick_w / 2, bar_y - 10, t1_x + tick_w / 2, bar_y + bar_h + 10), fill=TEXT_WHITE)

    row_y = bar_y + bar_h + 28
    stop_font = font(26, bold=True)
    stop_text = f"STOP {derived['risk_pct']:+.1f}%"
    draw_text(draw, (x0, row_y), stop_text, stop_font, RED, track_boxes=boxes, label="rr_stop")

    t2_font = font(26, bold=True)
    t2_text = f"T2 {derived['r2']:.1f}R ({derived['t2_pct']:+.1f}%)"
    tw = text_w(draw, t2_text, t2_font)
    draw_text(draw, (x0 + w - tw, row_y), t2_text, t2_font, GREEN, track_boxes=boxes, label="rr_t2")

    row_y += 46
    bottom_font = font(28, bold=False)
    bottom_text = f"1R = ${derived['one_r_dollars']:.2f} / share \u00b7 T1 at {derived['r1']:.1f}R"
    draw_text(draw, (x0, row_y), bottom_text, bottom_font, TEXT_MUTED, track_boxes=boxes, label="rr_bottom")

    return y0 + h


# ── 2x2 stat grid ────────────────────────────────────────────────────────


def render_stat_grid(draw, x0, y0, w, setup, derived, boxes):
    label_font = font(24, bold=False)
    value_font = font(34, bold=True)

    cells = [
        ("ENTRY", setup["entry"], TEXT_WHITE),
        ("STOP", f"{setup['stop']:g}", RED),
        ("TARGET 1", f"{setup['t1']:g}", GREEN),
        ("TARGET 2", f"{setup['t2']:g}", GREEN),
    ]
    col_w = w / 2
    row_h = 108
    for i, (label, value, color) in enumerate(cells):
        row, col = divmod(i, 2)
        cx = x0 + col * col_w
        cy = y0 + row * row_h
        draw_text(draw, (cx, cy), label, label_font, TEXT_DIM, track_boxes=boxes, label=f"stat_{label}_label")
        draw_text(draw, (cx, cy + 34), value, value_font, color, track_boxes=boxes, label=f"stat_{label}_value")
    return y0 + row_h * 2


# ── Single setup card ────────────────────────────────────────────────────

def render_setup_card(draw, x0, y0, w, h, setup, boxes_out: list):
    """Renders one setup card. Appends (label, bbox) tuples for every text
    element to boxes_out for the caller's overlap QA pass (scoped per-card
    since boxes_out is fresh per call from render_watchlist_card)."""
    is_call = setup["is_call"]
    accent = GREEN if is_call else RED

    rounded_rect(draw, (x0, y0, x0 + w, y0 + h), radius=28, fill=CARD_FILL, outline=BORDER, width=2)
    # 4px accent bar on the call/put side.
    draw.rectangle((x0, y0 + 4, x0 + 4, y0 + h - 4), fill=accent)

    pad = 44
    cx0 = x0 + pad
    cw = w - pad * 2
    cy = y0 + 40

    derived = compute_derived(setup)
    assert_r_multiples_roundtrip(setup, derived)
    assert_valid_trade_geometry(setup, derived)

    # Ticker header row: $TICKER left, pill right.
    ticker_font = font(64, bold=True)
    draw_text(draw, (cx0, cy), f"${setup['ticker']}", ticker_font, TEXT_WHITE, anchor="la",
              track_boxes=boxes_out, label="ticker")

    pill_font = font(36, bold=True)
    arrow = "\u25b2" if is_call else "\u25bc"
    pill_text = f"{arrow} {'CALL' if is_call else 'PUT'} {setup['strike']}"
    pw = text_w(draw, pill_text, pill_font)
    pad_x = 24
    pill_h = 56
    pill_x1 = x0 + w - pad
    pill_x0 = pill_x1 - (pw + pad_x * 2)
    pill_bg = GREEN_BG if is_call else RED_BG
    pill_color = GREEN if is_call else RED
    rounded_rect(draw, (pill_x0, cy, pill_x1, cy + pill_h), radius=14, fill=pill_bg)
    draw_text(draw, ((pill_x0 + pill_x1) / 2, cy + pill_h / 2), pill_text, pill_font, pill_color,
              anchor="mm", track_boxes=boxes_out, label="direction_pill")
    cy += 90

    sub_font = font(28, bold=False)
    close_line = f"${setup['close']:g} close \u00b7 {setup['company']}"
    draw_text(draw, (cx0, cy), close_line, sub_font, TEXT_MUTED, track_boxes=boxes_out, label="close_line")
    cy += 40
    expiry_line = f"{setup['expiry']} \u00b7 {setup['dte']}"
    draw_text(draw, (cx0, cy), expiry_line, sub_font, TEXT_MUTED, track_boxes=boxes_out, label="expiry_line")
    cy += 48

    # Pattern line: green dot + label.
    dot_r = 7
    dot_y = cy + 14
    draw.ellipse((cx0, dot_y - dot_r, cx0 + dot_r * 2, dot_y + dot_r), fill=accent)
    pattern_font = font(27, bold=True)
    draw_text(draw, (cx0 + dot_r * 2 + 14, cy), setup["pattern"], pattern_font, accent,
              track_boxes=boxes_out, label="pattern")
    cy += 48

    # Strike line: dim "STRIKE" label + bold white value + OTM%, with
    # optional delta/IV-rank appended directly onto the same line per
    # spec ("if delta present, append ... to the strike line").
    strike_label_font = font(28, bold=False)
    strike_value_font = font(28, bold=True)
    strike_label = "STRIKE  "
    draw_text(draw, (cx0, cy), strike_label, strike_label_font, TEXT_DIM,
              track_boxes=boxes_out, label="strike_label")
    lw = text_w(draw, strike_label, strike_label_font)
    strike_value = f"{setup['strike']} \u00b7 {derived['otm']:.1f}% OTM"
    draw_text(draw, (cx0 + lw, cy), strike_value, strike_value_font, TEXT_WHITE,
              track_boxes=boxes_out, label="strike_value")

    extra_bits = []
    if setup.get("delta") is not None:
        extra_bits.append(f"\u0394 {setup['delta']:.2f}")
    if setup.get("iv_rank") is not None:
        extra_bits.append(f"IV {setup['iv_rank']:.0f}pct")
    if extra_bits:
        extra_font = font(28, bold=False)
        extra_text = "  \u00b7 " + " \u00b7 ".join(extra_bits)
        vw = text_w(draw, strike_value, strike_value_font)
        draw_text(draw, (cx0 + lw + vw, cy), extra_text, extra_font, TEXT_MUTED,
                  track_boxes=boxes_out, label="strike_extras")
    cy += 44

    # Optional edge strip: only when chain data present, per spec. Drawn
    # on its OWN line below the strike line (not overlapping it).
    if setup.get("exp_move_pct") is not None:
        need_pct = derived["t1_pct"] / setup["exp_move_pct"]
        edge_font = font(28, bold=False)
        edge_line = f"T1 needs {need_pct:.0%} of exp. move (\u00b1{setup['exp_move_pct']:.1f}%)"
        draw_text(draw, (cx0, cy), edge_line, edge_font, TEXT_MUTED,
                  track_boxes=boxes_out, label="edge_strip")
        cy += 40

    cy += 20
    draw.line([(cx0, cy), (cx0 + cw, cy)], fill=BORDER, width=2)
    cy += 40

    # Risk/reward panel.
    rr_h = 230
    cy = render_risk_reward_panel(draw, cx0, cy, min(cw, 590), rr_h, setup, derived, boxes_out)
    cy += 36

    # 2x2 stat grid.
    cy = render_stat_grid(draw, cx0, cy, cw, setup, derived, boxes_out)
    cy += 30

    # FLOW line: amount bold white, rest muted, wrap to 2 lines max.
    flow_font = font(26, bold=False)
    flow_bold_font = font(26, bold=True)
    flow_text = setup["flow"]
    # Parse **bold** markdown-style markers per spec ("FLOW · **$544K** OTM/ATM...")
    draw_flow_line(draw, cx0, cy, cw, flow_text, flow_font, flow_bold_font, boxes_out)


def draw_flow_line(draw, x0, y0, max_w, flow_text, regular_font, bold_font, boxes_out):
    """Renders 'FLOW · **$X** rest of text', bolding only the **...**
    segment, wrapping to at most 2 lines if it doesn't fit in max_w."""
    prefix = "FLOW \u00b7 "
    body = flow_text
    segments = []  # list of (text, font)
    while "**" in body:
        before, _, rest = body.partition("**")
        bold_part, _, after = rest.partition("**")
        if before:
            segments.append((before, regular_font))
        segments.append((bold_part, bold_font))
        body = after
    if body:
        segments.append((body, regular_font))

    all_segments = [(prefix, regular_font)] + segments

    # Simple greedy word-wrap across segments, capped at 2 lines.
    lines = [[]]
    cur_w = 0
    line_h = 34
    for seg_text, seg_font in all_segments:
        words = seg_text.split(" ")
        for wi, word in enumerate(words):
            piece = word + (" " if wi < len(words) - 1 else "")
            pw = text_w(draw, piece, seg_font)
            if cur_w + pw > max_w and cur_w > 0:
                if len(lines) >= 2:
                    # Truncate rather than overflow a 3rd line.
                    break
                lines.append([])
                cur_w = 0
            lines[-1].append((piece, seg_font))
            cur_w += pw
        else:
            continue
        break

    y = y0
    for line in lines[:2]:
        x = x0
        for piece, seg_font in line:
            color = TEXT_WHITE if seg_font == bold_font else TEXT_DIM
            draw_text(draw, (x, y), piece, seg_font, color, track_boxes=boxes_out, label="flow_text")
            x += text_w(draw, piece, seg_font)
        y += line_h


# ── Footer ───────────────────────────────────────────────────────────────

def render_footer(draw, footer_text: str):
    f = font(30, bold=False)
    fw = text_w(draw, footer_text, f)
    draw_text(draw, ((CANVAS_W - fw) / 2, CANVAS_H - 70), footer_text, f, TEXT_DIM)


# ── Top-level entry point ────────────────────────────────────────────────

def render_watchlist_card(setups: list, market: dict, note: str, title_date: str,
                           subtitle: str, footer: str, out_path: str):
    """
    setups: list of up to 5 dicts, each with required fields (ticker,
    company, close, strike, strike_num, expiry, dte, pattern, entry, stop,
    t1, t2, flow, is_call) and optional fields (exp_move_pct, delta,
    iv_rank, breakeven). See build_setup_dict_from_v3() for how v3's
    pipeline `c` dict maps onto this contract.
    market: {"spy": {"price":.., "pct":.., "note":..}, "qqq": {...}, "iwm": {...}}
    """
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
    draw = ImageDraw.Draw(img)

    cards_top = render_header(draw, market, note, title_date, subtitle)

    n = len(setups)
    side_margin = 60
    gutter = 36
    card_w = (CANVAS_W - 2 * side_margin - gutter * (n - 1)) / n if n > 0 else 0
    footer_reserve = 90  # space reserved below cards for the footer line
    card_h = CANVAS_H - cards_top - footer_reserve

    for i, setup in enumerate(setups):
        x0 = side_margin + i * (card_w + gutter)
        boxes = []
        render_setup_card(draw, x0, cards_top, card_w, card_h, setup, boxes)
        assert_no_overlaps(boxes, card_label=setup["ticker"])

    render_footer(draw, footer)

    img.save(out_path, "PNG")
    return out_path


# ── V3 pipeline adapter ───────────────────────────────────────────────────

def build_setup_dict_from_v3(c: dict) -> dict:
    """Maps a v3 pipeline candidate dict (as built up through main() in
    bmt_nightly_setups_v3_test.py) onto this renderer's documented data
    contract. Only maps fields v3 actually has -- optional fields
    (exp_move_pct, delta, iv_rank, breakeven) are included only when the
    v3 dict actually has the underlying data, per the "no fabricated
    numbers / omit silently when absent" rule.
    """
    is_call = c["direction"].upper() == "CALL"
    setup = {
        "ticker": c["ticker"],
        "company": c.get("company_name") or "",
        "close": c["current_price"],
        "strike": f"${c['strike']:g}",
        "strike_num": c["strike"],
        "expiry": c["next_expiry"],
        "dte": f"{c.get('dte', '?')} DTE",
        "pattern": build_quality_tag_upper(c.get("pattern", "")),
        "entry": f"{c['entry_low']:g}-{c['entry_high']:g}",
        "stop": c["stop"],
        "t1": c["target1"],
        "t2": c["target2"],
        "flow": build_flow_markdown(c["flow"]),
        "is_call": is_call,
    }
    # Optional edge-strip fields: v3's req_exp_ratio pipeline already
    # computes expected_move_pct (item 1's expected-move gate) -- reuse it
    # here rather than recomputing, so the card and the gate always agree
    # on the same number. delta/iv_rank are not currently computed
    # anywhere in the v3 pipeline, so they're correctly omitted (never
    # fabricated).
    if c.get("expected_move_pct") is not None:
        setup["exp_move_pct"] = c["expected_move_pct"]
    return setup


def build_quality_tag_upper(pattern: str) -> str:
    mapping = {
        "V-recovery": "V-RECOVERY BOUNCE",
        "higher lows": "HIGHER LOWS BASE",
        "lower highs": "LOWER HIGHS BREAKDOWN",
        "breakdown": "CLEAN BREAKDOWN",
    }
    return mapping.get(pattern, pattern.upper() if pattern else "PATTERN MATCH")


def build_flow_markdown(flow: dict) -> str:
    premium = flow["premium"]
    premium_str = f"${premium / 1_000_000:.2f}M" if premium >= 1_000_000 else f"${premium / 1_000:.0f}K"
    return f"**{premium_str}** OTM/ATM {flow['bias'].lower()}, {flow['call_pct']}% call-weighted"