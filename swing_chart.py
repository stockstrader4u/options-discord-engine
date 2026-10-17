"""
swing_chart.py -- the Trend Swing idea chart. Same visual language as the nightly setup chart (setup_chart.py): light theme, header + key-number
strips, volume by price, EMA cloud, right-edge tags, RSI and relative-strength panels, "In plain English" notes -- but on DAILY candles, because
these are ~4-week swings. It only draws what the idea already owns: breakout level, the contract strike and break-even, the sell-by date, the
exit line (50d), and the options-implied 4-week range from the contract's own IV.

    render(idea, contract, mk, earn, out_path)      idea = a row from bmt_trend_swing.scan(), contract = pick_long_call() result
"""
from __future__ import annotations

import datetime as dt
import math
import textwrap

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

from setup_chart import BG, TXT, MUTE, BOX, UP, DN, BLUE, AMB, VIO, RES, SUP, EMC, PD, ENTRYC, STOPC, T1C, _spread

plt.rcParams["text.parse_math"] = False
N = 120                      # daily bars shown
FUT = 24                     # bars of right margin (sell-by marker, implied range, tags)
HOLD_BARS = 20
pc = lambda a, b: (a / b - 1) * 100 if b else 0.0
pcs = lambda a, b: "at the price" if abs(pc(a, b)) < 0.05 else f"{pc(a, b):+.1f}%"


def _notes(r: dict, c: dict, mk: dict, earn, f: pd.DataFrame, rsi: float, sell_by: dt.date, em_pct: float) -> list:
    last = r["spot"]
    s50, s150, s200 = (float(f[k].iat[-1]) for k in ("sma50", "sma150", "sma200"))
    atr = float(f["atr"].iat[-1])
    hi52 = float(f["hi252"].iat[-1])
    n = []
    n.append(("Trend", f"Price is {pc(last, s50):+.1f}% from its 50-day average and {pc(last, s200):+.1f}% from the 200-day. The 50-day sits above the 150-day, "
                       f"which sits above the 200-day, and the 50-day is rising: the textbook uptrend stack."))
    mom = f"RSI {rsi:.0f}. The stock is {r['ret63'] * 100:+.0f}% over 63 trading days."
    if rsi >= 75:
        mom += " That is stretched: pullbacks inside a trend are normal here, so the contract is sized small and the exit line matters."
    elif rsi <= 55:
        mom += " Momentum is calm, not stretched."
    n.append(("Momentum", mom))
    sec = ""
    if r.get("sector_etf") and r.get("sector_rs") is not None:
        sec = f" Its sector ({r['sector_etf']}) is {r['sector_rs'] * 100:+.0f}% vs SPY over the same stretch, so the group is helping."
    n.append(("Versus the market", f"{r['ticker']} is {r['rs63'] * 100:+.0f}% ahead of SPY over 63 days.{sec}"))
    n.append(("Key levels", f"Above: the 20-day high {r['breakout_level']:,.2f} ({pcs(r['breakout_level'], last)}) and the 52-week high {hi52:,.2f} ({pc(hi52, last):+.1f}%). "
                            f"Below: the 50-day {s50:,.2f} ({pc(s50, last):+.1f}%) is the exit line; the 200-day {s200:,.2f} ({pc(s200, last):+.1f}%)."))
    n.append(("The idea", f"{r['ticker']} {c['expiry']} ${c['strike']:g} call, {c['dte']} days to expiry, about ${c['mid']:.2f} (${c['mid'] * 100:,.0f} per contract). "
                          f"It needs {pc(c['breakeven'], last):+.1f}% by expiry to break even; sold earlier, it needs less because time value is still left."))
    n.append(("The plan", f"Hold about four weeks and sell by {sell_by.strftime('%b %d')}. Leave sooner if the stock closes under its 50-day ({s50:,.2f}). "
                         f"The most you can lose is what you pay."))
    n.append(("How far it normally moves", f"{r['ticker']} typically swings {atr:,.2f} ({atr / last * 100:.1f}%) a day. The options market prices about "
                                           f"±{em_pct:.1f}% over four weeks, shown as the shaded band on the right."))
    if earn:
        d = (earn - dt.date.today()).days
        n.append(("Earnings", f"Earnings are {earn.strftime('%b %d')} (in {d} days), {'inside' if earn <= sell_by else 'after'} the planned hold. "
                              + ("Expect a gap either way; consider waiting until after the report or sizing down." if earn <= sell_by else "")))
    if mk.get("etf"):
        up = [t for t in ("SPY", "QQQ", "IWM", "DIA") if mk["etf"].get(t) and mk["etf"][t]["above50"]]
        n.append(("The tape", f"{len(up)} of the 4 major index ETFs are above their 50-day average. "
                              + ("A weak tape makes even strong stocks harder to hold." if len(up) < 3 else "The tape is supportive.")))
    return n


def render(r: dict, c: dict, mk: dict, earn, out_path: str, dpi: int = 110) -> str:
    f = r["f"]
    v = f.iloc[-N:]
    x = np.arange(N)
    last = r["spot"]
    close = f["c"]
    e21, e50 = close.ewm(span=21, adjust=False).mean(), close.ewm(span=50, adjust=False).mean()
    d = close.diff()
    upm, dnm = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(), (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = (100 - 100 / (1 + upm / dnm.replace(0, np.nan))).fillna(50.0)
    chg = float(close.iat[-1] / close.iat[-2] - 1)
    sell_by = dt.date.today() + dt.timedelta(days=28)
    em_pct = c["iv"] * math.sqrt(HOLD_BARS / 252) * 100
    em_lo, em_hi = last * (1 - em_pct / 100), last * (1 + em_pct / 100)

    lo, hi = float(v["l"].min()), float(v["h"].max())
    levels = [c["strike"], c["breakeven"], r["breakout_level"], float(f["sma50"].iat[-1]), em_lo, em_hi]
    ymin, ymax = min([lo] + [p for p in levels if p > lo * .8]), max([hi] + [p for p in levels if p < hi * 1.25])
    # volume by price
    bins = np.linspace(lo, hi, 31); mid = (bins[:-1] + bins[1:]) / 2
    vp = np.histogram(((v["h"] + v["l"] + v["c"]) / 3).values, bins=bins, weights=v["v"].values)[0]
    order = np.argsort(vp)[::-1]
    va = set(order[: int(np.searchsorted(np.cumsum(vp[order]) / max(vp.sum(), 1), .7)) + 1])
    poc = float(mid[vp.argmax()])

    notes = _notes(r, c, mk, earn, f, float(rsi.iat[-1]), sell_by, em_pct)
    WRAP, LH = 66, 0.158
    heights = [LH * (1 + len(textwrap.wrap(b, WRAP))) + .075 for _, b in notes]
    half = int(np.ceil(len(notes) / 2))
    notes_h = max(sum(heights[:half]), sum(heights[half:])) + .1

    W = 10.4
    HEAD = 0.95 + 0.62 + 0.82
    MAIN, PAN, GAP, XLAB, FOOT, NTOP = 6.6, 1.25, .1, .32, .42, .42
    H = HEAD + MAIN + GAP + PAN + GAP + PAN + XLAB + NTOP + notes_h + FOOT
    fig = plt.figure(figsize=(W, H), dpi=dpi, facecolor=BG)
    fy = lambda t: 1 - t / H
    fx = lambda i: i / W
    LEFT, AXW = 0.5, 7.9

    def axes_at(top, h):
        a = fig.add_axes([fx(LEFT), fy(top + h), AXW / W, h / H], facecolor=BG)
        a.grid(False); [s.set_visible(False) for s in a.spines.values()]
        a.tick_params(colors=MUTE, labelsize=8, length=0)
        a.set_xlim(-1, N + FUT); a.set_xticklabels([])
        return a

    ax, ar, av = axes_at(HEAD, MAIN), axes_at(HEAD + MAIN + GAP, PAN), axes_at(HEAD + MAIN + GAP + PAN + GAP, PAN)
    rngv = ymax - ymin; padv = rngv * .06; vol_h = rngv * .13
    ax.set_ylim(ymin - padv - vol_h, ymax + padv)
    ylo, yhi = ax.get_ylim(); yr = yhi - ylo

    # options-implied 4-week range, drawn from today to the sell-by bar
    ax.fill_between([N - 1, N - 1 + HOLD_BARS], em_lo, em_hi, color=EMC, alpha=.10, lw=0, zorder=0)
    for p in (em_lo, em_hi):
        ax.plot([N - 1, N - 1 + HOLD_BARS], [p, p], color=EMC, lw=.9, ls=(0, (1, 2)), zorder=1)
    ax.axvline(N - 1 + HOLD_BARS, color=ENTRYC, lw=1.0, ls=(0, (4, 3)), alpha=.8, zorder=1)
    ax.text(N - 1 + HOLD_BARS - .6, ylo + vol_h * 1.15, f"sell by {sell_by.strftime('%b %d')}", color=ENTRYC, fontsize=7.5, fontweight="bold", ha="right", va="bottom",
            rotation=90, bbox=dict(fc=BG, ec="none", pad=.6, alpha=.85))
    ax.text(N + 1, em_hi, f"options-implied 4-week range\n{em_lo:,.2f} to {em_hi:,.2f}", color=EMC, fontsize=6.8, va="bottom", ha="left", fontweight="bold")

    # volume by price (right edge)
    w = vp / max(vp.max(), 1) * 7
    ax.barh(mid, w, height=(bins[1] - bins[0]) * .9, left=N + FUT - 1 - w, color=["#3b5bdb" if k in va else "#a5b4e8" for k in range(len(vp))], alpha=.55, lw=0, zorder=1)
    ax.text(N + FUT - 1.2, poc, "POC", color="#3b5bdb", fontsize=7, va="center", ha="right", fontweight="bold", bbox=dict(fc=BG, ec="none", pad=.6))

    # EMA cloud + 200d + volume bars
    a_, b_ = e21.iloc[-N:].values, e50.iloc[-N:].values
    ax.fill_between(x, a_, b_, where=a_ >= b_, color=BLUE, alpha=.14, lw=0, interpolate=True)
    ax.fill_between(x, a_, b_, where=a_ < b_, color=AMB, alpha=.16, lw=0, interpolate=True)
    ax.plot(x, a_, color=BLUE, lw=1.2); ax.plot(x, b_, color=AMB, lw=1.2)
    s200 = v["sma200"].values
    ax.plot(x, s200, color="#c2255c", lw=1.1, alpha=.85)
    ax.text(N - 1.5, s200[-1], "200d", color="#c2255c", fontsize=7, va="top", ha="right", bbox=dict(fc=BG, ec="none", pad=.4, alpha=.8))
    vmax = v["v"].max()
    for k, (op, cl, vol) in enumerate(zip(v["o"], v["c"], v["v"])):
        ax.add_patch(plt.Rectangle((k - .36, ylo), .72, vol / vmax * vol_h * .95, color=UP if cl >= op else DN, alpha=.5, lw=0, zorder=1))
    # candles
    for k, (op, h, l, cl) in enumerate(zip(v["o"], v["h"], v["l"], v["c"])):
        col = UP if cl >= op else DN
        ax.plot([k, k], [l, h], color=col, lw=.9, zorder=3)
        ax.add_patch(plt.Rectangle((k - .33, min(op, cl)), .66, max(abs(cl - op), rngv * .0015), color=col, zorder=3, lw=0))
    # breakout / signal bar
    b = min(max(len(f) - 1 - r["days_ago"] - (len(f) - N), 0), N - 1)
    ax.annotate(r.get("label", "breakout on volume"), xy=(b, float(v["h"].iat[b])), xytext=(max(2, b - 22), float(v["h"].iat[b]) + rngv * .09),
                color=TXT, fontsize=8, fontweight="bold", arrowprops=dict(arrowstyle="->", color=TXT, lw=.9), zorder=7,
                bbox=dict(fc=BG, ec="none", pad=1.2, alpha=.9))

    # levels + right-edge tags
    tags = []
    lvl = r["breakout_level"]
    ax.axhline(lvl, color=RES, lw=1.2, ls=(0, (5, 3)), alpha=.9, zorder=2)
    tags.append([lvl, lvl, f"20d high  {lvl:,.2f}", RES, "key"])
    s50v = float(f["sma50"].iat[-1])
    ax.axhline(s50v, color=STOPC, lw=1.6, alpha=.9, zorder=2)
    tags.append([s50v, s50v, f"EXIT under 50d  {s50v:,.2f}  ({pc(s50v, last):+.1f}%)", STOPC, "idea"])
    ax.axhline(c["strike"], color=T1C, lw=1.4, ls=(0, (6, 3)), alpha=.9, zorder=2)
    tags.append([c["strike"], c["strike"], f"STRIKE  {c['strike']:g}  ({pc(c['strike'], last):+.1f}%)", T1C, "idea"])
    if ymin <= c["breakeven"] <= ymax * 1.02:
        ax.axhline(c["breakeven"], color=ENTRYC, lw=1.1, ls=(0, (2, 2)), alpha=.8, zorder=2)
        tags.append([c["breakeven"], c["breakeven"], f"BREAK-EVEN at expiry  {c['breakeven']:,.2f}", ENTRYC, "idea"])
    tags.append([last, last, f"{last:,.2f}", TXT, "last"])
    _spread(tags, yr * .031, ylo + yr * .012, yhi - yr * .012)
    for p, ty, txt, col, kind in tags:
        if kind == "last":
            bb, tc, fw = dict(boxstyle="round,pad=0.25", fc=TXT, ec="none"), "white", "bold"
        elif kind == "idea":
            bb, tc, fw = dict(boxstyle="round,pad=0.25", fc=BG, ec=col, lw=1.4), col, "bold"
        else:
            bb, tc, fw = dict(boxstyle="round,pad=0.25", fc=col, ec="none"), "white", "bold"
        ax.text(N + FUT + .4, ty, txt, color=tc, fontsize=7.6, va="center", ha="left", fontweight=fw, bbox=bb, clip_on=False, zorder=7)
    ax.axhline(last, color=TXT, lw=.7, ls=":", alpha=.8, zorder=2)

    # header
    fig.text(fx(LEFT), fy(.30), r["ticker"], color=TXT, fontsize=17, fontweight="bold", va="center")
    fig.text(fx(LEFT + .30 + .17 * len(r["ticker"])), fy(.30), f"{last:,.2f}   {chg * 100:+.2f}% today", color=UP if chg >= 0 else DN, fontsize=11, va="center")
    fig.text(fx(W - LEFT), fy(.30), f"CALL ${c['strike']:g}  ·  {dt.date.fromisoformat(c['expiry']).strftime('%b %d')}", color="white", fontsize=11, fontweight="bold",
             va="center", ha="right", bbox=dict(boxstyle="round,pad=0.35", fc=UP, ec="none"))
    kind = "Trend continuation" if r.get("kind") == "cont" else "Trend breakout"
    fig.text(fx(LEFT), fy(.60), f"{kind} · {str(r['industry']).lower()} · data through the {f.index[-1]:%b %d} close · daily candles · EMA 21/50 cloud · 200d line · "
             "blue bars = volume by price", color=MUTE, fontsize=7.8, va="center")

    def strip(top, title, items):
        fig.text(fx(LEFT), fy(top), title, color=MUTE, fontsize=7.2, fontweight="bold", va="center")
        cw_ = (W - 2 * LEFT) / len(items)
        for i, (lab, val, sub, col) in enumerate(items):
            x0 = LEFT + i * cw_
            fig.patches.append(FancyBboxPatch((fx(x0 + .03), fy(top + .08 + .44)), (cw_ - .06) / W, .44 / H, boxstyle="round,pad=0,rounding_size=.006",
                                              transform=fig.transFigure, fc=BOX, ec="none", zorder=0))
            fig.text(fx(x0 + .12), fy(top + .08 + .12), lab, color=MUTE, fontsize=6.4, fontweight="bold", va="center")
            fig.text(fx(x0 + .12), fy(top + .08 + .30), val, color=col, fontsize=11, fontweight="bold", va="center")
            if sub:
                fig.text(fx(x0 + cw_ - .12), fy(top + .08 + .30), sub, color=MUTE, fontsize=6.4, va="center", ha="right")

    strip(.88, "THE IDEA · strike, break-even, exit line and sell-by date are on the chart",
          [("CONTRACT", f"${c['strike']:g} call", c["expiry"], TXT), ("PREMIUM", f"${c['mid']:.2f}", f"${c['mid'] * 100:,.0f} / contract", TXT),
           ("BREAK-EVEN", f"{c['breakeven']:,.2f}", f"{pc(c['breakeven'], last):+.1f}% at expiry", ENTRYC),
           ("EXIT IF CLOSE UNDER", f"{s50v:,.2f}", f"50d · {pc(s50v, last):+.1f}%", STOPC), ("SELL BY", sell_by.strftime("%b %d"), "about 4 weeks", T1C)])
    cells = [("DELTA", f"{c['delta']:.2f}", f"{c['dte']} days left", TXT), ("IMPLIED VOL", f"{c['iv'] * 100:.0f}%", f"stock realised {r['hv'] * 100:.0f}%", EMC),
             ("OPEN INTEREST", f"{c['oi']:,}", f"spread {c['spread'] * 100:.1f}%", TXT),
             ("4-WEEK RANGE", f"±{em_pct:.1f}%", "options-implied", EMC),
             ("EARNINGS", earn.strftime("%b %d") if earn else "none soon", (f"in {(earn - dt.date.today()).days} days" if earn else ""), STOPC if (earn and earn <= sell_by) else TXT)]
    strip(.88 + .62 + .20, "CONTRACT MAP · live quote from the option chain", cells)

    # panels
    ar.plot(x, rsi.iloc[-N:].values, color=VIO, lw=1.4); ar.set_ylim(10, 90); ar.set_yticks([30, 70])
    ar.axhspan(70, 90, color=DN, alpha=.07, lw=0); ar.axhspan(10, 30, color=UP, alpha=.07, lw=0)
    for lv in (30, 50, 70):
        ar.axhline(lv, color=MUTE, lw=.5, ls=(0, (3, 3)), alpha=.6)
    ar.text(.005, .93, f"RSI 14   {rsi.iloc[-1]:.0f}", transform=ar.transAxes, color=TXT, fontsize=8.5, va="top", bbox=dict(fc=BG, ec="none", pad=1.5, alpha=.9), zorder=6)
    rs = (f["rs"] / f["rs"].iloc[-N] * 100).iloc[-N:].values
    av.plot(x, rs, color=BLUE, lw=1.4, label="stock")
    av.fill_between(x, rs, 100, where=rs >= 100, color=UP, alpha=.2, lw=0, interpolate=True)
    av.fill_between(x, rs, 100, where=rs < 100, color=DN, alpha=.2, lw=0, interpolate=True)
    se = (mk.get("etf_px") or {}).get(r.get("sector_etf") or "")
    spy_px = mk.get("spy_px")
    ys = [rs]
    if se is not None and spy_px is not None:
        sr = (se / spy_px.reindex(se.index).ffill()).reindex(f.index).ffill()
        sr = (sr / sr.iloc[-N] * 100).iloc[-N:].values
        av.plot(x, sr, color=AMB, lw=1.1, ls=(0, (4, 2))); ys.append(sr)
    av.axhline(100, color=MUTE, lw=.6, ls=(0, (3, 3))); av.set_yticks([100])
    lo_r, hi_r = min(min(np.nanmin(y) for y in ys), 100), max(max(np.nanmax(y) for y in ys), 100)
    pad_r = max(hi_r - lo_r, 0.5); av.set_ylim(lo_r - pad_r * .1, hi_r + pad_r * .55)
    av.text(.005, .93, f"Relative strength vs SPY   {rs[-1] - 100:+.1f}% over the window ({'outperforming' if rs[-1] > rs[-20] else 'lagging'} the last 20 days)"
            + (f"   · dashed = {r['sector_etf']}" if len(ys) > 1 else ""), transform=av.transAxes, color=TXT, fontsize=8.5, va="top",
            bbox=dict(fc=BG, ec="none", pad=1.5, alpha=.9), zorder=6)
    idx = pd.to_datetime(v.index)
    ticks = [k for k in range(8, N) if idx[k].month != idx[k - 1].month]
    av.set_xticks(ticks); av.set_xticklabels([idx[k].strftime("%b %d") for k in ticks])

    # notes
    ny = HEAD + MAIN + GAP + PAN + GAP + PAN + XLAB + NTOP
    fig.text(fx(LEFT), fy(ny - .12), "IN PLAIN ENGLISH · written fresh from this chart's numbers", color=TXT, fontsize=8.2, fontweight="bold", va="top")

    def block(x0, items):
        y = ny + .08
        for name, body in items:
            fig.text(fx(x0), fy(y), name, color=TXT, fontsize=8, fontweight="bold", va="top"); y += LH
            lines = textwrap.wrap(body, WRAP)
            fig.text(fx(x0), fy(y), "\n".join(lines), color=MUTE, fontsize=7.6, va="top", linespacing=1.25)
            y += LH * len(lines) + .075

    block(LEFT, notes[:half]); block(LEFT + 4.85, notes[half:])
    fig.text(fx(LEFT), fy(H - .12), "Reference levels and readings from price history and live option quotes, not a forecast or advice. The 4-week range is the "
             "contract's own implied volatility, not a prediction.", color=MUTE, fontsize=6.6, va="bottom")
    fig.savefig(out_path, facecolor=BG, dpi=dpi)
    plt.close(fig)
    return out_path
