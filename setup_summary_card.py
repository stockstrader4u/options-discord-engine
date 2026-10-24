"""
setup_summary_card.py -- cover page for the nightly setup results.

Record, how many ideas triggered an entry, results by entry day, and how high the contracts got.
Drawn with the same canvas and palette as weekly_recap's summary card.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

import weekly_recap as wr
from setup_journey import LOSS_COLOR, WIN_COLOR, WIN_GAIN


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def render_setup_summary_card(group: dict[str, Any], results: list[dict[str, Any]], published: int,
                              out_path: str, pages: int = 1) -> None:
    """published = every idea for this expiry, including the ones that never triggered."""
    scored = [f for f in results if f.get("ok")]
    n_w = sum(f["status"] == "win" for f in scored)
    n_l = len(scored) - n_w
    rate = round(n_w / len(scored) * 100) if scored else 0
    peaks = [f["high_pct"] for f in scored]
    best = max(scored, key=lambda f: f["high_pct"], default=None)

    by_day: dict[date, list] = {}
    for f in scored:
        by_day.setdefault(f["alerted_at"].date(), []).append(f)
    gain = WIN_GAIN * 100
    buckets = [(f"Never reached +{gain:.0f}%", lambda p: p < gain, wr.DOWN),
               (f"+{gain:.0f}% to +50%", lambda p: gain <= p < 50, wr.UP),
               ("+50% to +100%", lambda p: 50 <= p < 100, wr.UP),
               ("+100% or more", lambda p: p >= 100, wr.UP)]

    H = 1.9 + 2.75 + (0.62 + len(by_day) * 0.52) + (0.62 + len(buckets) * 0.52) + 0.95
    fig, ax, T = wr._canvas(H)
    LEFT, RIGHT, CENTER = wr.LEFT, wr.RIGHT, wr.CENTER
    HF, DF = wr.HEADER_FONT, wr.DATA_FONT

    T(CENTER, 0.40, group.get("title", "SETUP RESULTS"), ha="center", fontsize=26, color=wr.TXT_LIGHT,
      fontweight="bold", fontfamily=HF)
    T(CENTER, 1.00, group.get("expiry_text", ""), ha="center", fontsize=15, color=wr.TXT_DIM, fontfamily=HF)
    T(CENTER, 1.45, "The trade ideas we published,\nand how each option contract traded after the entry triggered",
      ha="center", fontsize=11.5, color=wr.GRAY, fontfamily=HF, linespacing=1.4)

    tw, th, tg = (RIGHT - LEFT - 0.2) / 2, 1.25, 0.2
    ty = 1.95

    def tile(col, row):
        x0, y0 = LEFT + col * (tw + tg), ty + row * (th + tg)
        ax.add_patch(FancyBboxPatch((x0, y0), tw, th, boxstyle="round,pad=0,rounding_size=0.09",
                                    linewidth=1, edgecolor=wr.PANEL_BORDER, facecolor=wr.PANEL_BG))
        return x0 + tw / 2, y0

    def big(cx, y0, text, color=wr.TXT_LIGHT, **kw):
        T(cx, y0 + 0.15, text, ha=kw.pop("ha", "center"), fontsize=30, color=color, fontweight="bold", fontfamily=HF)

    def cap(cx, y0, text):
        T(cx, y0 + 0.82, text, ha="center", fontsize=9.5, color=wr.TXT_DIM, fontweight="bold", fontfamily=HF)

    cx, y0 = tile(0, 0)
    big(cx - 0.1, y0, f"{n_w}W", WIN_COLOR, ha="right")
    big(cx, y0, "-", wr.GRAY)
    big(cx + 0.1, y0, f"{n_l}L", LOSS_COLOR, ha="left")
    cap(cx, y0, f"{rate}% WIN RATE")
    cx, y0 = tile(1, 0)
    big(cx, y0, f"{len(scored)} of {published}")
    cap(cx, y0, "IDEAS TRIGGERED AN ENTRY")
    cx, y0 = tile(0, 1)
    big(cx, y0, wr._fmt_pct(_median(peaks)) if peaks else "N/A", wr.UP)
    cap(cx, y0, "TYPICAL PEAK AFTER ENTRY")
    cx, y0 = tile(1, 1)
    big(cx, y0, wr._fmt_pct(best["high_pct"]) if best else "N/A", wr.UP)
    cap(cx, y0, f"BEST PEAK · {best['ticker']} {best['put_call']} {wr._fmt_strike(best['strike'])}" if best else "BEST PEAK")

    y = ty + 2 * th + tg + 0.40
    T(LEFT, y, "RESULTS BY ENTRY DAY", fontsize=12, color=wr.TXT_LIGHT, fontweight="bold", fontfamily=HF)
    y += 0.50
    max_n = max((len(v) for v in by_day.values()), default=1)
    bar_x0, bar_max = LEFT + 1.55, 2.9
    for d in sorted(by_day):
        fs = by_day[d]
        w_ = sum(f["status"] == "win" for f in fs)
        T(LEFT, y, f"{d:%a %b} {d.day}", fontsize=12, color=wr.TXT_LIGHT, fontfamily=DF)
        wl, ll = w_ / max_n * bar_max, (len(fs) - w_) / max_n * bar_max
        ax.add_patch(plt.Rectangle((bar_x0, y + 0.03), wl, 0.24, facecolor=WIN_COLOR, edgecolor="none", alpha=0.9))
        ax.add_patch(plt.Rectangle((bar_x0 + wl, y + 0.03), ll, 0.24, facecolor=LOSS_COLOR, edgecolor="none", alpha=0.9))
        T(bar_x0 + wl + ll + 0.12, y, f"{w_}W-{len(fs) - w_}L", fontsize=11.5, color=wr.TXT_DIM, fontfamily=DF)
        y += 0.52
    y += 0.30
    T(LEFT, y, "HOW HIGH THE CONTRACTS GOT AFTER ENTRY", fontsize=12, color=wr.TXT_LIGHT, fontweight="bold", fontfamily=HF)
    y += 0.50
    counts = [sum(1 for p in peaks if test(p)) for _, test, _ in buckets]
    max_c = max(counts, default=1) or 1
    bar_x0, bar_max = LEFT + 3.05, 2.6
    for (label, _, color), count in zip(buckets, counts):
        T(LEFT, y, label, fontsize=11.5, color=wr.TXT_LIGHT, fontfamily=HF)
        if count:
            ax.add_patch(plt.Rectangle((bar_x0, y + 0.03), count / max_c * bar_max, 0.24, facecolor=color,
                                       edgecolor="none", alpha=0.9))
        T(bar_x0 + (count / max_c * bar_max if count else 0) + 0.12, y, str(count), fontsize=12,
          color=wr.TXT_DIM, fontweight="bold", fontfamily=DF)
        y += 0.52

    label = (f"Summary · setup-by-setup detail on the next {pages - 1} page{'s' if pages != 2 else ''}"
             if pages > 1 else "Summary")
    wr._footer(T, ax, H, label, group.get("disclaimer", wr.DISCLAIMER))
    fig.savefig(out_path, facecolor=wr.DARK_BG, dpi=wr.DPI)
    plt.close(fig)
