r"""
gex_vex_html_render.py -- TEST-ONLY HTML/CSS renderer for the GEX/VEX
dashboard, using headless Chromium (Playwright) instead of matplotlib.

WHY THIS EXISTS: the matplotlib-rendered card (gex_vex_unified_daily.py's
render_unified_card()) works, but font rendering, spacing precision, and
anti-aliasing don't match a browser-rendered card. This script builds the
exact same data into an HTML/CSS layout and screenshots it with a headless
browser instead -- pixel quality much closer to a real web dashboard.

STATUS: TEST ONLY. This script posts EXCLUSIVELY to GEX_TEST_DISCORD_WEBHOOK
(a separate test channel) -- it never reads or writes GEX_DISCORD_WEBHOOK,
the production channel gex_vex_unified_daily.py posts to. Do not point this
at the production webhook until the visual output has been reviewed and
you've explicitly decided to cut over.

SETUP (one-time, on your machine):
    pip install playwright
    playwright install chromium
    (playwright install may also ask for OS-level deps on some machines --
    follow whatever it prints)

ENV VARS REQUIRED:
    GEX_TEST_DISCORD_WEBHOOK  -- a TEST channel/webhook URL, not production.

SUBTITLE-SCOPE FIX (vs. the earlier mockup): the global header subtitle no
longer says "weekly + monthly OpEx combined" -- that applied to ALL ten
tickers visually even though only SPY/QQQ/IWM used multi-expiry aggregation
at the time. See the 2026-09-13 note below for why that blend is now gone
entirely, not just correctly scoped.

NEAR-TERM-EXPIRY REDESIGN (2026-09-13): confirmed by direct user decision
that this dashboard's actual audience is day traders and 2-3 DTE swing
traders -- meaning EVERY ticker (not just SPY/QQQ/IWM) should reflect a
consistent "next couple of trading sessions" view, not a mix of near-term
Mag7 and a weekly+monthly-OpEx blend for the three indices. Per that
decision:
  - The SPY/QQQ/IWM weekly+monthly-OpEx multi-expiry construction
    (get_monthly_opex_expiry(), the `expiries = sorted(set(weekly,
    monthly))` block in main()) is REMOVED entirely, not just left as an
    option. Monthly OI reflects medium-term institutional positioning,
    not what's pinning or releasing price in the next 2-3 sessions --
    blending it into the near-term view actively worked against this
    dashboard's actual audience.
  - ALL 10 tickers (core + Mag7 alike) now call
    gex_vex.compute_gex_vex(t, expiries=None) uniformly, which
    internally resolves to gex_vex.get_near_term_expiry() -- the
    nearest listed expiry within 2-3 calendar days, regardless of what
    day of the week this runs (see gex_vex.py's own 2026-09-13 module
    docstring note for the full rationale).
  - Per-card multi-expiry footnotes ("Exp: weekly X + monthly Y") are
    removed from core_card_html() -- every ticker's `expiries` list is
    now always length 1, so that footnote could never fire again
    anyway; removed rather than left as dead code.
  - get_monthly_opex_expiry() itself is removed from this file -- no
    remaining caller.

This script calls the SAME compute_gex_vex() from gex_vex.py that
production uses -- no data logic is duplicated or reimplemented here, only
presentation.
"""

import os
from datetime import date, datetime, timedelta

import requests

import gex_vex
import gex_vex_history

TEST_WEBHOOK = os.environ.get("GEX_TEST_DISCORD_WEBHOOK", "")

# NOTE: deliberately NOT importing from gex_vex_unified_daily.py -- that
# module reads GEX_DISCORD_WEBHOOK (the PRODUCTION webhook) at import time
# (module-level `os.environ["GEX_DISCORD_WEBHOOK"]`), which means merely
# importing it for its constants/helpers would require production config
# to be present just to run this TEST script. CORE_TICKERS, MAG7_TICKERS,
# get_week_label(), and pick_todays_focus() are duplicated below instead,
# so this script has zero dependency on gex_vex_unified_daily.py or on
# any production env var. Keep these in sync manually if the production
# versions change.

CORE_TICKERS = ["SPY", "QQQ", "IWM"]
MAG7_TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]


def get_week_label(today=None):
    if today is None:
        today = date.today()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    if monday.month == friday.month:
        return f"Week of {monday.strftime('%b %d')} - {friday.strftime('%d, %Y')}"
    return f"Week of {monday.strftime('%b %d')} - {friday.strftime('%b %d, %Y')}"


def pick_todays_focus(core_results, mag7_results, comparisons):
    """
    Copied from gex_vex_unified_daily.py's pick_todays_focus() -- see that
    module's docstring for the full heuristic rationale. Logic UNCHANGED,
    only relocated so this test script has no import-time dependency on
    the production module. Keep in sync manually if that logic changes.
    """
    all_valid = [r for r in (core_results + mag7_results) if "error" not in r]
    picked_tickers = set()
    focus = []

    flipped = [r for r in all_valid if comparisons.get(r["ticker"], {}).get("regime_flipped")]
    for r in flipped[:2]:
        comp = comparisons[r["ticker"]]
        color = GOLD if comp["flip_direction"] == "to_long" else RED
        focus.append((r, color, "REGIME FLIP", comp["plain_text"]))
        picked_tickers.add(r["ticker"])

    with_em = [r for r in all_valid if r.get("expected_move") and r["ticker"] not in picked_tickers]
    if with_em and len(focus) < 3:
        top_em = max(with_em, key=lambda r: r["expected_move"]["pct"])
        em = top_em["expected_move"]
        desc = (f"Widest expected move of the group (\u00b1{em['pct']}%, \u00b1${em['dollar']:.2f} by Friday). "
                f"Bigger swings cut both ways here -- size accordingly.")
        focus.append((top_em, GOLD, "HIGH RISK", desc))
        picked_tickers.add(top_em["ticker"])

    near_flip = [r for r in all_valid if r.get("gamma_flip") and r["ticker"] not in picked_tickers]
    if near_flip and len(focus) < 3:
        closest = min(near_flip, key=lambda r: abs(r["spot"] - r["gamma_flip"]) / r["spot"])
        desc = (f"Sitting right at its {fmt(closest['gamma_flip'])} pivot level -- which way it breaks "
                f"from here matters more than usual today.")
        focus.append((closest, PURPLE, "KEY LEVELS", desc))
        picked_tickers.add(closest["ticker"])

    short_names = [r for r in all_valid if r["net_gex"] < 0 and r["ticker"] not in picked_tickers]
    if short_names and len(focus) < 3:
        s = short_names[0]
        desc = ("Short gamma while most of the group is long -- less cushion against a big move here. "
                "Keep position sizes tighter than the rest of the list.")
        focus.append((s, RED, "BREAKOUT WATCH", desc))
        picked_tickers.add(s["ticker"])

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

BG, CARD_BG, BORDER = "#080b12", "#131928", "#232d42"
TEXT1, TEXT2, TEXT3 = "#f5f7fa", "#9aa4b8", "#6b7488"
GREEN, RED, GOLD, BLUE, PURPLE = "#2dd4a8", "#f26a7d", "#f5b942", "#5b9df5", "#b088f5"


def fmt(v):
    if v is None:
        return "N/A"
    return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"


def bar_pct(spot, put_wall, call_wall):
    if not put_wall or not call_wall or call_wall == put_wall:
        return 50.0
    pct = (spot - put_wall) / (call_wall - put_wall) * 100
    return max(2.0, min(98.0, pct))


def core_card_html(r: dict, comp: dict) -> str:
    if "error" in r:
        return f'<div class="card"><div class="ticker">${r.get("ticker","?")}</div><div class="dim">data unavailable this run</div></div>'

    ticker, spot = r["ticker"], r["spot"]
    is_short = r["net_gex"] < 0
    badge_txt = "SHORT GAMMA" if is_short else "LONG GAMMA"
    badge_color = RED if is_short else GREEN
    put_wall, call_wall = r.get("put_wall"), r.get("call_wall")
    dot_pct = bar_pct(spot, put_wall, call_wall)

    delta_html = ""
    if comp.get("has_comparison") and comp.get("spot_change_pct") is not None:
        pct = comp["spot_change_pct"]
        arrow = "&#9650;" if pct >= 0 else "&#9660;"
        dcolor = GREEN if pct >= 0 else RED
        delta_html = f'<span style="color:{dcolor};font-size:9px;margin-left:6px;">{arrow} {abs(pct):.1f}% vs yesterday</span>'

    em = r.get("expected_move") or {}
    gf = r.get("gamma_flip")

    return f'''
    <div class="card">
      <div class="card-top">
        <div class="ticker">${ticker}</div>
        <div class="badge" style="background:{badge_color}2e;border-color:{badge_color};color:{badge_color};">{badge_txt}</div>
      </div>
      <div class="dim label">SPOT PRICE</div>
      <div class="spot" style="color:{GREEN};">{spot:,.2f}{delta_html}</div>
      <div class="wall-labels">
        <span style="color:{RED};">PUT WALL</span><span style="color:{GREEN};">CALL WALL</span>
      </div>
      <div class="bar">
        <div class="dot" style="left:{dot_pct}%;"></div>
      </div>
      <div class="wall-values">
        <span style="color:{RED};">{fmt(put_wall)}</span>
        <span style="color:{TEXT1};">{spot:,.2f}</span>
        <span style="color:{GREEN};">{fmt(call_wall)}</span>
      </div>
      <div class="stat-grid">
        <div><div class="dim">Gamma Flip</div><div class="val">{fmt(gf)}</div></div>
        <div><div class="dim">Expected Move (1D)</div><div class="val" style="color:{GOLD};">&plusmn;{em.get('pct','N/A')}%</div></div>
        <div><div class="dim">Net GEX</div><div class="val" style="color:{RED if is_short else GREEN};">{'-' if r['net_gex']<0 else '+'}${abs(r['net_gex'])/1e9:.2f}B</div></div>
        <div><div class="dim">Implied Range (1D)</div><div class="val">{em.get('min','N/A')}-{em.get('max','N/A')}</div></div>
        <div><div class="dim">Net VEX</div><div class="val" style="color:{RED if r['net_vex']<0 else GREEN};">{r['net_vex']/1e9:+.2f}B</div></div>
        <div><div class="dim">Put/Call Wall</div><div class="val" style="color:{TEXT2};">{fmt(put_wall)} / {fmt(call_wall)}</div></div>
      </div>
    </div>'''


def mag7_row_html(r: dict) -> str:
    if "error" in r:
        return f'<tr><td colspan="8" class="dim">${r.get("ticker","?")}: data unavailable</td></tr>'
    is_long = r["net_gex"] >= 0
    rc = GREEN if is_long else RED
    em = r.get("expected_move") or {}
    return f'''<tr>
      <td class="ticker-cell">${r["ticker"]}</td>
      <td>{r["spot"]:,.2f}</td>
      <td><span class="regime-pill" style="background:{rc}2e;border-color:{rc};color:{rc};">{'LONG' if is_long else 'SHORT'}</span></td>
      <td style="color:{RED};">{fmt(r.get('put_wall'))}</td>
      <td style="color:{GREEN};">{fmt(r.get('call_wall'))}</td>
      <td style="color:{GOLD};">{fmt(r.get('gamma_flip'))}</td>
      <td>&plusmn;{em.get('pct','N/A')}%</td>
      <td style="color:{rc};font-weight:700;">{'-' if r['net_gex']<0 else '+'}${abs(r['net_gex'])/1e9:.2f}B</td>
    </tr>'''


def focus_item_html(item) -> str:
    r, color, tag, desc = item
    ticker = r["ticker"]
    return f'''
    <div class="focus-item" style="border-left-color:{color};">
      <div class="focus-top">
        <div class="focus-left">
          <div class="focus-badge" style="background:{color};">{ticker[0]}</div>
          <span class="focus-ticker">${ticker}</span>
        </div>
        <span class="tag" style="background:{color}2e;border-color:{color};color:{color};">{tag}</span>
      </div>
      <div class="focus-desc">{desc}</div>
    </div>'''


def build_dashboard_html(core_results, mag7_results, focus_items, comparisons, week_label) -> str:
    core_cards = "\n".join(core_card_html(r, comparisons.get(r.get("ticker"), {})) for r in core_results)
    mag7_rows = "\n".join(mag7_row_html(r) for r in mag7_results)
    focus_html = "\n".join(focus_item_html(item) for item in focus_items)

    return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background:{BG}; font-family:-apple-system,"Segoe UI",Roboto,sans-serif; padding:28px; width:1280px; }}
  .header {{ text-align:center; margin-bottom:22px; }}
  .title {{ color:{TEXT1}; font-size:26px; font-weight:700; }}
  .subtitle {{ color:{BLUE}; font-size:13px; margin-top:6px; }}
  .row3 {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-bottom:14px; }}
  .card {{ background:{CARD_BG}; border:1px solid {BORDER}; border-radius:12px; padding:18px; }}
  .card-top {{ display:flex; justify-content:space-between; align-items:flex-start; }}
  .ticker {{ color:{TEXT1}; font-size:24px; font-weight:700; }}
  .ticker-cell {{ color:{TEXT1}; font-weight:700; }}
  .badge {{ font-size:11px; font-weight:700; padding:5px 12px; border-radius:7px; border:1px solid; }}
  .label {{ font-size:11px; margin-top:12px; }}
  .dim {{ color:{TEXT3}; font-size:11px; }}
  .spot {{ font-size:20px; font-weight:700; margin-top:2px; }}
  .wall-labels {{ display:flex; justify-content:space-between; font-size:11px; font-weight:700; margin-top:16px; }}
  .bar {{ height:11px; border-radius:6px; margin:6px 0; position:relative;
          background:linear-gradient(90deg,{RED},#3a3f4e,{GREEN}); }}
  .dot {{ position:absolute; top:-4px; width:15px; height:15px; border-radius:50%;
          background:#fff; border:2px solid {TEXT1}; transform:translateX(-50%); }}
  .wall-values {{ display:flex; justify-content:space-between; font-size:11px; font-weight:700; }}
  .stat-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:16px; font-size:12px; }}
  .val {{ color:{TEXT1}; font-weight:700; margin-top:2px; }}
  .lower {{ display:grid; grid-template-columns:1.55fr 1fr; gap:14px; margin-bottom:14px; }}
  .panel {{ background:{CARD_BG}; border:1px solid {BORDER}; border-radius:12px; padding:18px; }}
  .panel-title {{ color:{TEXT1}; font-size:15px; font-weight:700; margin-bottom:12px; }}
  table {{ width:100%; border-collapse:collapse; font-size:12px; }}
  th {{ color:{TEXT3}; text-align:left; font-weight:700; padding-bottom:8px; font-size:10px; }}
  td {{ color:{TEXT1}; padding:6px 0; }}
  .regime-pill {{ font-size:10px; font-weight:700; padding:3px 8px; border-radius:6px; border:1px solid; }}
  .focus-item {{ border-left:3px solid; padding-left:12px; margin-bottom:16px; }}
  .focus-top {{ display:flex; justify-content:space-between; align-items:center; }}
  .focus-left {{ display:flex; align-items:center; gap:8px; }}
  .focus-badge {{ width:26px; height:26px; border-radius:50%; color:{BG}; font-weight:700;
                  font-size:12px; display:flex; align-items:center; justify-content:center; }}
  .focus-ticker {{ color:{TEXT1}; font-size:14px; font-weight:700; }}
  .tag {{ font-size:10px; font-weight:700; padding:4px 9px; border-radius:6px; border:1px solid; }}
  .focus-desc {{ color:{TEXT2}; font-size:11px; margin-top:6px; line-height:1.5; }}
  .terms {{ background:{CARD_BG}; border:1px solid {BORDER}; border-radius:12px; padding:16px 20px; }}
  .terms-title {{ color:{TEXT3}; font-size:11px; font-weight:700; margin-bottom:8px; }}
  .terms-body {{ color:{TEXT2}; font-size:10.5px; line-height:1.7; }}
</style></head>
<body>
  <div class="header">
    <div class="title">DAILY GEX / VEX DASHBOARD</div>
    <div class="subtitle">{week_label} &middot; BlueMoonTrades &middot; <span style="color:{TEXT2};">Near-term view (2-3 DTE) \u2014 all 10 tickers</span></div>
  </div>
  <div class="row3">{core_cards}</div>
  <div class="lower">
    <div class="panel">
      <div class="panel-title">MAG 7 POSITIONING</div>
      <table>
        <tr><th>TICKER</th><th>SPOT</th><th>REGIME</th><th>PUT WALL</th><th>CALL WALL</th><th>GAMMA FLIP</th><th>EXP MOVE</th><th>NET GEX</th></tr>
        {mag7_rows}
      </table>
    </div>
    <div class="panel">
      <div class="panel-title">TODAY'S FOCUS</div>
      {focus_html}
    </div>
  </div>
  <div class="terms">
    <div class="terms-title">KEY TERMS</div>
    <div class="terms-body">
      LONG GAMMA: price tends to get pulled back toward the range if it swings too far -- moves stay more contained. &middot;
      SHORT GAMMA: less cushion against big moves -- once a level breaks, price can run further than usual. &middot;
      GAMMA FLIP: the price level where that behavior switches from one to the other. &middot;
      PUT WALL / CALL WALL: strikes where options positioning is heaviest -- tend to act like a floor or ceiling. &middot;
      NET GEX: total gamma exposure -- the sign shows long or short gamma overall. &middot;
      NET VEX: how sensitive that positioning is to changes in volatility. &middot;
      EXPECTED MOVE: how far the options market is pricing this to move by Friday.
    </div>
  </div>
</body></html>'''


def build_sample_data():
    """
    Hand-built sample data for visual-only testing when live Alpaca data
    isn't available (e.g. outside market hours -- compute_gex_vex() then
    correctly returns "error" for every ticker rather than fabricate a
    number, same defensive behavior documented throughout gex_vex.py).

    This function exists ONLY to validate that the HTML/CSS rendering
    pipeline looks right -- it must never be used as a stand-in for real
    data once this moves anywhere near production.
    """
    def core(ticker, spot, put_wall, call_wall, gamma_flip, net_gex_b, net_vex_b,
              em_pct, em_min, em_max):
        return {
            "ticker": ticker, "spot": spot, "put_wall": put_wall, "call_wall": call_wall,
            "gamma_flip": gamma_flip, "net_gex": net_gex_b * 1e9, "net_vex": net_vex_b * 1e9,
            "expected_move": {"pct": em_pct, "dollar": round(spot * em_pct / 100, 2),
                               "min": em_min, "max": em_max},
            "expiries": ["near-term"],
        }

    def mag7(ticker, spot, put_wall, call_wall, gamma_flip, net_gex_b, em_pct):
        return {
            "ticker": ticker, "spot": spot, "put_wall": put_wall, "call_wall": call_wall,
            "gamma_flip": gamma_flip, "net_gex": net_gex_b * 1e9, "net_vex": 0.02e9,
            "expected_move": {"pct": em_pct, "dollar": round(spot * em_pct / 100, 2),
                               "min": round(spot * (1 - em_pct / 100), 2),
                               "max": round(spot * (1 + em_pct / 100), 2)},
            "expiries": ["near-term"],
        }

    core_results = [
        core("SPY", 765.32, 755, 780, 758.40, -0.65, 0.17, 0.93, 758.20, 772.43),
        core("QQQ", 709.02, 700, 715, 706.85, -0.94, 0.12, 1.4, 699.10, 718.95),
        core("IWM", 299.05, 294, 302, 297.60, -0.51, 0.03, 1.17, 295.56, 302.54),
    ]
    mag7_results = [
        mag7("AAPL", 311.75, 310, 315, 288.45, 0.19, 1.71),
        mag7("MSFT", 489.30, 480, 500, 447.74, 0.06, 1.97),
        mag7("GOOGL", 342.63, 338, 355, 348.67, 0.07, 1.94),
        mag7("AMZN", 259.13, 255, 262.50, 262.83, 0.07, 2.02),
        mag7("NVDA", 210.45, 200, 220, 220.19, 0.21, 5.85),
        mag7("META", 573.42, 550, 585, 558.51, 0.14, 3.04),
        mag7("TSLA", 346.50, 340, 365, 353.51, 0.09, 3.05),
    ]
    comparisons = {
        "SPY": {"has_comparison": True, "spot_change_pct": 0.1, "regime_flipped": False},
        "QQQ": {"has_comparison": True, "spot_change_pct": -0.1, "regime_flipped": False},
        "IWM": {"has_comparison": True, "spot_change_pct": 0.1, "regime_flipped": False},
    }
    return core_results, mag7_results, comparisons


def render_html_to_png(html: str, out_path: str):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.set_content(html, wait_until="networkidle")
        page.screenshot(path=out_path, full_page=True)
        browser.close()


def post_text_to_test_webhook(content):
    r = requests.post(TEST_WEBHOOK, json={"content": content}, timeout=15)
    print(f"  [DISCORD TEST] text post: {r.status_code}")
    return r.status_code in (200, 204)


def post_image_to_test_webhook(image_path, caption=""):
    with open(image_path, "rb") as f:
        files = {"file": (os.path.basename(image_path), f, "image/png")}
        data = {"content": caption}
        r = requests.post(TEST_WEBHOOK, data=data, files=files, timeout=30)
    print(f"  [DISCORD TEST] image post: {r.status_code}")
    return r.status_code in (200, 204)


def main():
    if not TEST_WEBHOOK:
        print("ERROR: GEX_TEST_DISCORD_WEBHOOK is not set. Refusing to run -- "
              "this script must never fall back to the production webhook.")
        return

    print("=== HTML/CSS GEX/VEX DASHBOARD -- TEST RENDER ONLY ===\n")
    week_label = get_week_label()

    use_sample = os.environ.get("GEX_SAMPLE_DATA", "").strip() == "1"

    if use_sample:
        print("GEX_SAMPLE_DATA=1 -- using hand-built sample data, NOT live Alpaca data. "
              "This validates rendering only, not real numbers.\n")
        core_results, mag7_results, comparisons = build_sample_data()
    else:
        # NEAR-TERM-EXPIRY REDESIGN (2026-09-13): ALL 10 tickers now use
        # the same expiries=None default (-> gex_vex.get_near_term_expiry()
        # internally) -- the old weekly+monthly-OpEx blend built
        # specifically for SPY/QQQ/IWM here is REMOVED, per direct user
        # decision that this dashboard's audience is day traders and 2-3
        # DTE swing traders across every ticker, not just the three
        # indices. See this module's own docstring for the full
        # rationale.
        print("Fetching all 10 tickers (near-term expiry, uniform treatment)...")
        core_results = [gex_vex.compute_gex_vex(t, expiries=None) for t in CORE_TICKERS]
        mag7_results = [gex_vex.compute_gex_vex(t, expiries=None) for t in MAG7_TICKERS]

        for r in core_results + mag7_results:
            if "error" in r:
                print(f"  {r.get('ticker', '?')}: ERROR -- {r['error']}")
            else:
                print(f"  {r['ticker']}: OK -- spot=${r['spot']:.2f} net_gex={r['net_gex']/1e9:+.2f}B "
                      f"expiry={r['expiries'][0]} flip={r.get('gamma_flip')}")

        print("\nComputing Since Yesterday comparisons...")
        gex_vex_history.ensure_table()
        today_date = date.today()
        comparisons = {}
        for r in core_results + mag7_results:
            if "error" in r:
                continue
            try:
                gex_vex_history.save_snapshot(r, today_date)
                comparisons[r["ticker"]] = gex_vex_history.get_comparison_summary(r, today_date)
            except Exception as e:
                print(f"  {r['ticker']}: comparison failed: {e}")
                comparisons[r["ticker"]] = {"has_comparison": False, "spot_change_pct": None}

    focus_items = pick_todays_focus(core_results, mag7_results, comparisons)

    print("\nBuilding HTML and rendering via headless Chromium...")
    html = build_dashboard_html(core_results, mag7_results, focus_items, comparisons, week_label)
    out_path = "gex_html_test.png"
    render_html_to_png(html, out_path)
    print(f"  saved to {out_path}")

    print("\nPosting to TEST webhook (production webhook NOT touched)...")
    post_text_to_test_webhook(f"\U0001F9EA **[TEST] HTML-RENDERED GEX/VEX DASHBOARD \u2014 {week_label}**")
    post_image_to_test_webhook(out_path)

    print("\n=== DONE (test webhook only) ===")


if __name__ == "__main__":
    main()