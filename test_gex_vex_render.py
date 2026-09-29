r"""
Regression test for the v2 GEX/VEX render in gex_vex_unified_daily.py.

Run:  python test_gex_vex_render.py     (or: pytest test_gex_vex_render.py)

Offline only: no Alpaca, no DB, no Discord. Feeds fixed `day` dicts to the
pure functions (verdict / pick_focus / render_day / decollide) and checks
text, invariants and PNG properties. Production code is imported, not
modified; a dummy webhook env var is set only so the module can import.
"""

import copy
import os
import tempfile

os.environ.setdefault("GEX_DISCORD_WEBHOOK", "http://localhost/unused")

from PIL import Image  # noqa: E402

import gex_vex_unified_daily as g  # noqa: E402


def ix(t, s, chg_pct, pw, cw, gf, em, gex, vex, prev):
    prev_close = s / (1 + chg_pct / 100) if chg_pct is not None else None
    return dict(ticker=t, spot=s, prev_close=prev_close, put_wall=pw, call_wall=cw, gamma_flip=gf,
                exp_move_pct=em, net_gex=gex, net_vex=vex, regime_prev=prev)


def mg(t, s, pw, cw, gf, em, gex):
    return dict(ticker=t, spot=s, put_wall=pw, call_wall=cw, gamma_flip=gf, exp_move_pct=em, net_gex=gex)


# Fixed sample = the numbers in the approved mockup (2026-09-28).
DAY = dict(
    week_label="Week of Sep 28 - Oct 02, 2026", brand="BlueMoonTrades",
    indexes=[ix("SPY", 767.10, -0.1, 761, 775, None, 0.83, -1.28e9, 0.35e9, "LONG"),
             ix("QQQ", 736.18, -0.7, 730, 745, 725.06, 1.24, 0.87e9, 0.07e9, "SHORT"),
             ix("IWM", 280.55, -0.1, 280, 285, None, 1.25, -0.61e9, 0.04e9, "SHORT")],
    mag7=[mg("AAPL", 341.27, 335, 350, 317.77, 1.55, 1), mg("MSFT", 507.63, 500, 515, 499.23, 1.89, 1),
          mg("GOOGL", 342.16, 340, 350, 346.14, 2.06, 1), mg("AMZN", 246.77, 245, 255, None, 1.95, -1),
          mg("NVDA", 231.13, 222.5, 232.5, 224.09, 2.26, 1), mg("META", 722.31, 705, 750, None, 3.08, -1),
          mg("TSLA", 361.35, 360, 380, None, 2.71, -1)],
)


def with_regimes(day, gex_signs, prevs=None):
    d = copy.deepcopy(day)
    for i, s in enumerate(gex_signs):
        d["indexes"][i]["net_gex"] = s * abs(d["indexes"][i]["net_gex"])
        d["indexes"][i]["regime_prev"] = prevs[i] if prevs else g.regime_of(d["indexes"][i]["net_gex"])
    return d


# ------------------------------------------------------------ verdict
def test_verdict_mixed_fragile_underneath():
    h, accent, body = g.verdict(DAY)
    assert h == "MIXED DAY — FRAGILE UNDERNEATH" and accent == g.AMBER
    assert body == ("SPY and IWM are short gamma: once a level breaks, the move can run further than "
                    "usual. QQQ flipped into long gamma near 725.06 — stickier, so fast moves are "
                    "more likely to fade than follow through. Trade smaller than usual and respect "
                    "the walls.")


def test_verdict_all_short():
    h, accent, body = g.verdict(with_regimes(DAY, [-1, -1, -1]))
    assert h == "FRAGILE DAY — BREAKS CAN RUN" and accent == g.RED
    assert body.startswith("SPY, QQQ and IWM are short gamma") and "long gamma" not in body


def test_verdict_all_long_sits_in_uses_flip_or_put_wall():
    d = with_regimes(DAY, [1, 1, 1])
    h, accent, body = g.verdict(d)
    assert h == "STICKY DAY — MOVES LIKELY TO FADE" and accent == g.GREEN
    assert "sits in long gamma" in body and "flipped into" not in body
    assert "short gamma" not in body
    assert "761 (SPY)" in body and "725.06 (QQQ)" in body  # flip if present else put wall


def test_verdict_spy_anchored():
    h, accent, _ = g.verdict(with_regimes(DAY, [1, -1, -1]))
    assert h == "MIXED DAY — SPY ANCHORED" and accent == g.AMBER


# -------------------------------------------------------------- focus
def test_focus_sample():
    picks = g.pick_focus(DAY)
    assert [(p["ticker"], p["tag"]) for p in picks] == [("SPY", "REGIME FLIP"), ("QQQ", "REGIME FLIP"),
                                                         ("META", "HIGH RISK")]
    assert [p["tag_color"] for p in picks] == [g.RED, g.AMBER, g.AMBER]
    assert picks[0]["blurb"].startswith("SPY down barely 0.1%, but this setup got less stable")
    assert "near 761 there's less to slow" in picks[0]["blurb"]
    assert picks[1]["blurb"].startswith("QQQ down 0.7% yet flipped into a steadier setup near the 725.06 flip")
    assert picks[2]["blurb"] == ("Widest expected move of the group at ±3.08% (±$22.25). "
                                 "Bigger swings cut both ways here — size accordingly.")


def test_focus_quiet_day_is_only_high_risk():
    d = with_regimes(DAY, [-1, 1, -1])
    picks = g.pick_focus(d)
    assert [p["tag"] for p in picks] == ["HIGH RISK"]


def test_focus_capped_at_three_flips_first():
    d = with_regimes(DAY, [-1, 1, -1], prevs=["LONG", "SHORT", "LONG"])
    picks = g.pick_focus(d)
    assert len(picks) == 3 and [p["ticker"] for p in picks] == ["SPY", "QQQ", "IWM"]
    assert all(p["tag"] == "REGIME FLIP" for p in picks)  # HIGH RISK bumped, never padded


def test_focus_without_prev_close_omits_move_clause():
    d = copy.deepcopy(DAY)
    d["indexes"][0]["prev_close"] = None
    assert g.pick_focus(d)[0]["blurb"].startswith("SPY: this setup got less stable")


# ---------------------------------------------------- tick label logic
def test_decollide_no_overlap_and_inside_bounds():
    widths = [90, 90, 90]
    for centers in ([100, 105, 110], [100, 500, 900], [990, 995, 1000], [0, 5, 10]):
        lefts = g.decollide(widths, centers, 0, 1000)
        for a, b, w in zip(lefts, lefts[1:], widths):
            assert b - (a + w) >= g.LABEL_GAP - 1e-6
        assert lefts[0] >= -1e-6 and lefts[-1] + widths[-1] <= 1000 + 1e-6


# ------------------------------------------------------------- render
def render(day):
    out = os.path.join(tempfile.mkdtemp(), "gex.png")
    g.render_day(day, out)  # its internal asserts are part of the test
    return out


def test_render_sample_png_properties():
    out = render(DAY)
    assert os.path.getsize(out) < 8 * 1024 * 1024
    im = Image.open(out)
    assert im.width == 2560 and 2400 <= im.height <= 2900
    assert im.getpixel((5, 5)) == g.rgb(g.BG)  # background colour
    # verdict accent bar is amber on this sample (left band, below header)
    amber = g.rgb(g.AMBER)
    assert any(im.getpixel((g.MARGIN + 5, y)) == amber for y in range(200, 420))


def test_render_flip_outside_walls_and_no_flip_and_collisions():
    d = copy.deepcopy(DAY)
    d["mag7"][0].update(gamma_flip=300.0)                 # flip far below put wall
    d["mag7"][1].update(gamma_flip=520.0)                 # flip above call wall
    d["mag7"][2].update(gamma_flip=340.02)                # flip ~ on top of put wall
    d["indexes"][2].update(put_wall=280.0, call_wall=280.1, gamma_flip=280.05)  # very tight range
    render(d)


def test_render_short_universe_and_extremes():
    d = copy.deepcopy(DAY)
    d["mag7"] = d["mag7"][:3]
    d["indexes"][0]["prev_close"] = None                  # no comparison available
    render(d)
    d2 = with_regimes(DAY, [-1, -1, -1])                  # all short: no LONG sentence
    render(d2)


def test_flip_drawn_iff_present():
    # tick_bar asserts this internally; make sure both branches execute
    from PIL import ImageDraw
    img = Image.new("RGB", (1200, 200), g.rgb(g.CARD))
    d = ImageDraw.Draw(img)
    a = g.tick_bar(img, d, 50, 50, 1000, 26, spot=10, put_wall=9, call_wall=12, gamma_flip=None,
                   label_size=26, bg=g.CARD)
    b = g.tick_bar(img, d, 50, 120, 1000, 26, spot=10, put_wall=9, call_wall=12, gamma_flip=10.5,
                   label_size=26, bg=g.CARD)
    assert "flip" not in a["drawn"] and "flip" in b["drawn"]


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} passed")
