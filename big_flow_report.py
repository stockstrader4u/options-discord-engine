"""
big_flow_report.py -- the 3:30 PM ET "$1M+ Flow Report Card".

Every out-of-the-money, BOUGHT print of $1M+ premium that JarvisFlow saw today, market-wide: no watchlist
restriction and no expiry filter. Each print is priced from its contract's own Alpaca option bars (price when
the flow hit, then the high, low and latest price since), so the card shows how the flow has traded so far.

How the list is built:
  * One market-wide JarvisFlow call (OTM, BOUGHT, today's trade date) sorted by `Premium`. That sort orders by
    each CONTRACT's total premium for the day, not by print size. A $1M+ print means its contract's day total
    is at least $1M, so those contracts sort ahead of everything smaller. The call returns at most 300 rows;
    when the last row's contract total is still under $1M, every $1M+ print is provably in the result. If a
    day ever fills all 300 rows above $1M, the query is repeated for SWEEP and BLOCK separately and merged.
  * "At or above the ask" cannot be checked directly: the payload has no bid/ask. JarvisFlow's own BOUGHT
    classification (its tool docs map "hit the ask" to BOUGHT) is the proxy, same as the conviction baseline.
  * Premium is per print, like every other premium figure in this engine.
Contract prices come from Alpaca's 5-minute option bars, which run ~15 minutes behind, so a 3:30 PM post
reflects trading through about 3:14 PM.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

import weekly_recap as w

logger = logging.getLogger("options-big-flow")

ET = ZoneInfo("America/New_York")
JARVIS_MCP_URL = os.getenv("JARVIS_MCP_URL", "https://api.jarvisflow.io/.well-known/mcp")
MIN_PREMIUM = 1_000_000
JARVIS_ROW_CAP = 300                       # JarvisFlow returns at most this many rows per call


# ---------------------------------------------------------------------------
# JarvisFlow fetch
# ---------------------------------------------------------------------------

def _parse_mcp(text: str) -> list[dict]:
    for line in text.splitlines():
        if line.startswith("data:"):
            data = json.loads(line[5:].strip())
            content = data.get("result", {}).get("content", [])
            if content and content[0].get("type") == "text":
                try:
                    inner = json.loads(content[0]["text"])
                except json.JSONDecodeError:
                    raise RuntimeError(f"JarvisFlow error: {content[0]['text'][:200]}")
                tool_result = inner.get("toolResult", inner)
                if isinstance(tool_result, dict):
                    return tool_result.get("optionsFlow") or []
                if isinstance(tool_result, list):
                    return tool_result
    return []


async def _fetch(args: dict, api_key: str) -> list[dict]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "stock_ticker_unusual_options_data", "arguments": args}}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(JARVIS_MCP_URL, json=payload, headers=headers)
    resp.raise_for_status()
    return _parse_mcp(resp.text)


def _item_premium(it: dict) -> float:
    try:
        return float(it.get("total_Option_Premium_For_Trade") or it.get("totalOptionPremiumForTrade") or 0)
    except (TypeError, ValueError):
        return 0.0


def _contract_day_total(it: dict) -> float:
    try:
        return float(it.get("total_Option_Premium_for_Same_Contract_Today") or 0)
    except (TypeError, ValueError):
        return 0.0


def trade_time_et(it: dict) -> datetime | None:
    """Jarvis stamps trades with naive ET wall-clock time."""
    raw = it.get("transaction_DateTime") or it.get("transactionDateTime")
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ET)
    except ValueError:
        return None


def qualifies(it: dict, day: date, cutoff_et: datetime) -> bool:
    """OTM, BOUGHT, >= $1M on one print, traded on `day` at or before the cutoff. Re-checked locally even
    though the request already filters, so the card never depends on the server honouring a filter."""
    if str(it.get("moneyNess") or "").strip().upper() != "OTM":
        return False
    if str(it.get("implied_Bought_Or_Sold") or it.get("impliedBoughtOrSold") or "").strip().upper() != "BOUGHT":
        return False
    if _item_premium(it) < MIN_PREMIUM:
        return False
    t = trade_time_et(it)
    return t is not None and t.date() == day and t <= cutoff_et


def _key(it: dict) -> tuple:
    return (it.get("ticker"), it.get("put_Or_Call"), it.get("strike_Price"), str(it.get("expriation_Date"))[:10],
            it.get("transaction_DateTime"), _item_premium(it))


async def fetch_big_flows(day: date, cutoff_et: datetime, api_key: str) -> list[dict]:
    """All qualifying prints for `day`, largest premium first."""
    if not api_key:
        raise ValueError("JARVIS_API_KEY is missing")
    d = day.strftime("%m/%d/%Y")
    base = {"filter_by_moneyNess": "OTM", "filter_by_impliedAction": "BOUGHT",
            "filter_by_transaction_date_range_from": d, "filter_by_transaction_date_range_to": d,
            "sort_by": "Premium", "sort_direction": "Desc"}
    rows = await _fetch(base, api_key)
    if len(rows) >= JARVIS_ROW_CAP and _contract_day_total(rows[-1]) >= MIN_PREMIUM:
        logger.warning("big_flow result hit the %d-row cap with contract totals still >= $1M; splitting by sweep/block",
                       JARVIS_ROW_CAP)
        merged = {_key(r): r for r in rows}
        for kind in ("SWEEP", "BLOCK"):
            for r in await _fetch({**base, "filter_by_sweepOrBlock": kind}, api_key):
                merged[_key(r)] = r
        rows = list(merged.values())
    items = {_key(r): r for r in rows if qualifies(r, day, cutoff_et)}
    return sorted(items.values(), key=_item_premium, reverse=True)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def _row_from_item(it: dict) -> dict[str, Any]:
    t = trade_time_et(it)
    pc = str(it.get("put_Or_Call") or "").lower()
    strike = float(it["strike_Price"])
    expiry = str(it["expriation_Date"])[:10]
    return {
        "id": None, "alert_hash": None, "ticker": str(it["ticker"]).upper(),
        "contract": f"{str(it['ticker']).upper()} {expiry} {strike:g}{pc[:1].upper()}",
        "premium": int(_item_premium(it)), "sentiment": "bullish" if pc == "call" else "bearish",
        "put_call": pc, "strike": strike, "expiry_date": expiry,
        "spot_price": float(it["spot_Price"]) if it.get("spot_Price") is not None else None,
        "ingested_at": t.astimezone(timezone.utc).replace(tzinfo=None),
        "sweep_or_block": str(it.get("sweep_Or_Block") or "").lower(),
        "print_px": _print_price(it),
    }


def _print_price(it: dict) -> float | None:
    """The contract price the flow actually printed at (JarvisFlow's price_Of_Contract): the true tape price."""
    try:
        px = float(it.get("price_Of_Contract") if it.get("price_Of_Contract") is not None else it.get("priceOfContract"))
    except (TypeError, ValueError):
        return None
    return px if px > 0 else None


def rebase_to_print(f: dict[str, Any], print_px: float | None) -> None:
    """Make the flow's price journey start from the price the flow printed at. compute_flow_result's own entry
    is the close of the 5-minute bar containing the print, which can be up to 5 minutes after it. High and low
    are bar extremes after the print (never lower/higher than the entry itself); every percentage is re-based.
    The entry price is kept even when no later contract trades exist, so the card can still show it."""
    entry = print_px if print_px else f.get("alert_px")
    f["entry_px"] = entry
    if not f.get("ok") or not entry:
        return
    f["high_px"] = max(f["high_px"], entry)
    f["low_px"] = min(f["low_px"], entry)
    for key, px in (("high_pct", f["high_px"]), ("low_pct", f["low_px"]), ("last_pct", f["last_px"])):
        f[key] = (px / entry - 1.0) * 100.0
    f["alert_px"] = entry


def group_by_ticker(flows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group priced flows by stock symbol, biggest total premium first. Every print keeps its own row (its own
    time, premium and entry price), so a ticker's header count equals its number of rows and its rows add up to
    its total. Within a ticker, prints on the same contract sit together, the contract with the most premium
    first and its prints in time order."""
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for f in flows:
        by_ticker.setdefault(f["ticker"], []).append(f)
    groups = []
    for ticker, fs in by_ticker.items():
        contracts: dict[tuple, list[dict[str, Any]]] = {}
        for f in sorted(fs, key=lambda x: x["alerted_at"]):
            contracts.setdefault((f["put_call"], f["strike"], f["expiry"]), []).append(f)
        ordered = sorted(contracts.values(), key=lambda prints: sum(p["premium"] for p in prints), reverse=True)
        rows = [p for prints in ordered for p in prints]
        calls = sum(f["premium"] for f in fs if f["put_call"] == "CALL")
        puts = sum(f["premium"] for f in fs if f["put_call"] == "PUT")
        groups.append({"ticker": ticker, "rows": rows, "n_flows": len(rows), "total_premium": calls + puts,
                       "call_premium": calls, "put_premium": puts})
    groups.sort(key=lambda g: g["total_premium"], reverse=True)
    return groups


def build_big_flow_report(items: list[dict], day: date, now_et: datetime, api_key: str = "") -> dict[str, Any]:
    """Price every qualifying print and group them by stock symbol. Pure computation plus Alpaca reads
    (no DB, no Discord)."""
    rows = [_row_from_item(it) for it in items]
    flows = w.compute_flows_for_rows(rows, api_key, now_et, day) if rows else []
    for f, r in zip(flows, rows):                       # compute_flows_for_rows keeps the input order
        rebase_to_print(f, r["print_px"])
    flows.sort(key=lambda f: f["premium"], reverse=True)
    ok = [f for f in flows if f["ok"]]
    calls = [f for f in flows if f["put_call"] == "CALL"]
    puts = [f for f in flows if f["put_call"] == "PUT"]
    prem = lambda xs: sum(f["premium"] for f in xs)
    stats = {
        "count": len(flows), "with_data": len(ok), "calls": len(calls), "puts": len(puts),
        "call_premium": prem(calls), "put_premium": prem(puts), "total_premium": prem(flows),
        "unique_tickers": len({f["ticker"] for f in flows}),
        "median_high_pct": statistics.median(f["high_pct"] for f in ok) if ok else None,
        "median_low_pct": statistics.median(f["low_pct"] for f in ok) if ok else None,
    }
    return {"day": day, "as_of": now_et, "flows": flows, "groups": group_by_ticker(flows), "stats": stats,
            "caption": format_caption(day, now_et, stats)}


def format_caption(day: date, now_et: datetime, stats: dict[str, Any]) -> str:
    clock = now_et.strftime("%I:%M %p").lstrip("0")
    return (f"**$1M+ Flow Report Card — {day.strftime('%a %b %d')}**   {stats['count']} flows · "
            f"{w._fmt_prem(stats['total_premium'])} premium · {stats['calls']} calls / {stats['puts']} puts\n"
            f"*Out-of-the-money, bought (at/above the ask per JarvisFlow), single prints of $1M+, market-wide, "
            f"through {clock} ET. How each contract has traded since — not trade recommendations.*")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

TITLE = "$1M+ FLOW REPORT CARD"
DISCLAIMER = ("ENTRY = contract price when the flow printed · HIGH / LOW / NOW = traded since, vs. entry\n"
              "OTM · bought · single prints of $1M+ · Alpaca prices ~15 min delayed · not trade recommendations")
COLS = {"time": w.LEFT + 0.25, "contract": w.LEFT + 0.95, "prem": w.LEFT + 3.05, "entry": w.LEFT + 3.7,
        "high": w.LEFT + 4.6, "low": w.LEFT + 5.5, "now": w.LEFT + 6.4}
BAND_H, ROW_H, GROUP_GAP = 0.40, 0.58, 0.10
PAGE1_CAP, PAGE_CAP = 5.4, 9.3          # inches of table per page (page 1 sits under the summary tiles)


def _clock12(ts: datetime) -> str:
    h = ts.hour % 12 or 12
    return f"{h}:{ts.minute:02d}{'a' if ts.hour < 12 else 'p'}"


def _pct_color(v: float | None) -> str:
    if v is None:
        return w.GRAY
    return w.UP if v > 0.05 else w.DOWN if v < -0.05 else w.TXT_DIM


def _footer(T, ax, H, page_label: str):
    ax.plot([w.LEFT, w.RIGHT], [H - 0.82, H - 0.82], color=w.PANEL_BORDER, linewidth=1)
    T(w.CENTER, H - 0.74, DISCLAIMER, ha="center", fontsize=7.4, color=w.GRAY, fontfamily=w.HEADER_FONT, linespacing=1.5)
    T(w.CENTER, H - 0.28, page_label, ha="center", fontsize=8.5, color=w.TXT_DIM, fontfamily=w.HEADER_FONT)


def _header(T, report: dict, page_no: int, pages: int, big: bool) -> None:
    day = report["day"]
    if big:
        T(w.CENTER, 0.40, TITLE, ha="center", fontsize=24, color=w.TXT_LIGHT, fontweight="bold", fontfamily=w.HEADER_FONT)
        T(w.CENTER, 0.98, day.strftime("%A, %B %d, %Y"), ha="center", fontsize=14, color=w.TXT_DIM, fontfamily=w.HEADER_FONT)
        T(w.CENTER, 1.38, f"Grouped by stock · priced through ~{(report['as_of'] - timedelta(minutes=16)).strftime('%I:%M %p').lstrip('0')} ET",
          ha="center", fontsize=10.5, color=w.GRAY, fontfamily=w.HEADER_FONT)
    else:
        T(w.LEFT, 0.28, TITLE, fontsize=15, color=w.TXT_LIGHT, fontweight="bold", fontfamily=w.HEADER_FONT)
        T(w.LEFT, 0.62, day.strftime("%A, %B %d, %Y"), fontsize=10.5, color=w.TXT_DIM, fontfamily=w.HEADER_FONT)
        T(w.RIGHT, 0.28, f"Page {page_no} of {pages}", ha="right", fontsize=11, color=w.TXT_DIM, fontfamily=w.HEADER_FONT)


def _column_headers(ax, T, y: float) -> float:
    for key, lab, ha in (("time", "TIME", "left"), ("contract", "CONTRACT", "left"), ("prem", "PREMIUM", "right"),
                         ("entry", "ENTRY", "center"), ("high", "HIGH", "center"), ("low", "LOW", "center"),
                         ("now", "NOW", "center")):
        T(COLS[key], y, lab, ha=ha, fontsize=8, color=w.GRAY, fontweight="bold", fontfamily=w.HEADER_FONT)
    ax.plot([w.LEFT, w.RIGHT], [y + 0.28, y + 0.28], color=w.PANEL_BORDER, linewidth=1)
    return y + 0.36


def _draw_band(ax, T, g: dict, y: float, cont: bool) -> None:
    """Ticker header: symbol, flow count, which side(s) it was on, and (only when the ticker has more than one
    contract) the ticker's total premium at the right edge. A single-contract ticker's premium is already on its row."""
    ax.add_patch(FancyBboxPatch((w.LEFT - 0.08, y), w.RIGHT - w.LEFT + 0.16, BAND_H - 0.04,
                                boxstyle="round,pad=0,rounding_size=0.05", linewidth=0, facecolor=w.PANEL_BG, zorder=0))
    ax.plot([w.LEFT - 0.08, w.LEFT - 0.08], [y + 0.04, y + BAND_H - 0.08], color=w.TXT_DIM, linewidth=3, solid_capstyle="butt")
    T(w.LEFT + 0.12, y + 0.04, g["ticker"] + (" (cont.)" if cont else ""), fontsize=13.5, color=w.TXT_LIGHT,
      fontweight="bold", fontfamily=w.DATA_FONT)
    T(w.LEFT + 1.85, y + 0.08, f"{g['n_flows']} flow{'s' if g['n_flows'] != 1 else ''}", fontsize=9.5, color=w.TXT_DIM, fontfamily=w.DATA_FONT)
    x = w.LEFT + 2.75
    if g["call_premium"] and g["put_premium"]:
        T(x, y + 0.08, f"C {w._fmt_prem(g['call_premium'])}", fontsize=9.5, color=w.CALL_HUE, fontweight="bold", fontfamily=w.DATA_FONT)
        T(x + 0.7, y + 0.08, "·", fontsize=9.5, color=w.GRAY, fontfamily=w.DATA_FONT)
        T(x + 0.88, y + 0.08, f"P {w._fmt_prem(g['put_premium'])}", fontsize=9.5, color=w.PUT_HUE, fontweight="bold", fontfamily=w.DATA_FONT)
    else:
        calls = bool(g["call_premium"])
        T(x, y + 0.08, "calls" if calls else "puts", fontsize=9.5, color=w.CALL_HUE if calls else w.PUT_HUE,
          fontweight="bold", fontfamily=w.DATA_FONT)
    if len(g["rows"]) > 1:
        T(w.RIGHT - 0.12, y + 0.06, f"total {w._fmt_prem(g['total_premium'])}", ha="right", fontsize=11, color=w.TXT_LIGHT,
          fontweight="bold", fontfamily=w.DATA_FONT)


def _draw_contract_row(ax, T, f: dict, y: float) -> None:
    """One contract: time, contract, premium, the price it printed at, then HIGH / LOW / NOW each as a percentage
    versus that entry price with the dollar price underneath."""
    cp = f["put_call"] or "?"
    hue = w.CALL_HUE if cp == "CALL" else w.PUT_HUE if cp == "PUT" else w.GRAY
    k = w._fmt_strike(f["strike"]) if f.get("strike") is not None else "?"
    exp = f["expiry"].strftime("%b %d") if f.get("expiry") else "?"
    T(COLS["time"], y + 0.07, _clock12(f["alerted_at"]), fontsize=9.5, color=w.TXT_DIM, fontfamily=w.DATA_FONT)
    T(COLS["contract"], y + 0.06, f"{cp[:1]} {k} · {exp}", fontsize=10.5, color=hue, fontweight="bold", fontfamily=w.DATA_FONT)
    T(COLS["prem"], y + 0.05, w._fmt_prem(f["premium"]), ha="right", fontsize=11, color=w.TXT_LIGHT, fontfamily=w.DATA_FONT)
    T(COLS["entry"], y + 0.05, w._fmt_px(f.get("entry_px")), ha="center", fontsize=11, color=w.TXT_LIGHT, fontweight="bold", fontfamily=w.DATA_FONT)
    if not f["ok"]:
        for key in ("high", "low", "now"):
            T(COLS[key], y + 0.06, "—", ha="center", fontsize=11, color=w.GRAY, fontfamily=w.DATA_FONT)
        return
    for key, pc, px in (("high", f["high_pct"], f["high_px"]), ("low", f["low_pct"], f["low_px"]), ("now", f["last_pct"], f["last_px"])):
        T(COLS[key], y + 0.05, w._fmt_pct(pc), ha="center", fontsize=11, color=_pct_color(pc), fontweight="bold", fontfamily=w.DATA_FONT)
        T(COLS[key], y + 0.32, w._fmt_px(px), ha="center", fontsize=8.4, color=w.TXT_DIM, fontfamily=w.DATA_FONT)


def _summary_tiles(ax, T, stats: dict, y: float) -> float:
    tw, th, tg = (w.RIGHT - w.LEFT - 0.2) / 2, 1.2, 0.2

    def tile(col: int, row: int):
        x0, y0 = w.LEFT + col * (tw + tg), y + row * (th + tg)
        ax.add_patch(FancyBboxPatch((x0, y0), tw, th, boxstyle="round,pad=0,rounding_size=0.09",
                                    linewidth=1, edgecolor=w.PANEL_BORDER, facecolor=w.PANEL_BG))
        return x0 + tw / 2, y0

    cx, y0 = tile(0, 0)
    T(cx, y0 + 0.13, str(stats["count"]), ha="center", fontsize=28, color=w.TXT_LIGHT, fontweight="bold", fontfamily=w.HEADER_FONT)
    T(cx, y0 + 0.80, f"FLOWS · {stats['unique_tickers']} TICKERS", ha="center", fontsize=9.5, color=w.TXT_DIM, fontweight="bold", fontfamily=w.HEADER_FONT)
    cx, y0 = tile(1, 0)
    T(cx, y0 + 0.13, w._fmt_prem(stats["total_premium"]), ha="center", fontsize=28, color=w.TXT_LIGHT, fontweight="bold", fontfamily=w.HEADER_FONT)
    T(cx, y0 + 0.80, "TOTAL PREMIUM", ha="center", fontsize=9.5, color=w.TXT_DIM, fontweight="bold", fontfamily=w.HEADER_FONT)
    cx, y0 = tile(0, 1)
    tot = max(stats["call_premium"] + stats["put_premium"], 1)
    bx0, bx1 = cx - tw / 2 + 0.3, cx + tw / 2 - 0.3
    cw = (bx1 - bx0) * stats["call_premium"] / tot
    T(cx, y0 + 0.12, f"{stats['calls']} calls  ·  {stats['puts']} puts", ha="center", fontsize=16, color=w.TXT_LIGHT, fontweight="bold", fontfamily=w.HEADER_FONT)
    ax.add_patch(plt.Rectangle((bx0, y0 + 0.62), cw, 0.14, facecolor=w.CALL_HUE, edgecolor="none"))
    ax.add_patch(plt.Rectangle((bx0 + cw, y0 + 0.62), (bx1 - bx0) - cw, 0.14, facecolor=w.PUT_HUE, edgecolor="none"))
    T(cx, y0 + 0.86, f"BY PREMIUM  {stats['call_premium'] / tot * 100:.0f}% CALLS / {stats['put_premium'] / tot * 100:.0f}% PUTS",
      ha="center", fontsize=8.8, color=w.TXT_DIM, fontweight="bold", fontfamily=w.HEADER_FONT)
    cx, y0 = tile(1, 1)
    mh, ml = stats["median_high_pct"], stats["median_low_pct"]
    if mh is not None:
        T(cx - 0.12, y0 + 0.15, w._fmt_pct(mh), ha="right", fontsize=22, color=w.UP, fontweight="bold", fontfamily=w.HEADER_FONT)
        T(cx, y0 + 0.15, "/", ha="center", fontsize=22, color=w.GRAY, fontfamily=w.HEADER_FONT)
        T(cx + 0.12, y0 + 0.15, w._fmt_pct(ml), ha="left", fontsize=22, color=w.DOWN, fontweight="bold", fontfamily=w.HEADER_FONT)
    T(cx, y0 + 0.80, "TYPICAL HIGH / LOW SINCE", ha="center", fontsize=9.5, color=w.TXT_DIM, fontweight="bold", fontfamily=w.HEADER_FONT)
    return y + 2 * th + tg


def paginate(groups: list[dict[str, Any]]) -> list[list[tuple]]:
    """Lay ticker groups out onto pages as ('band', group, cont) / ('row', row) / ('gap',) blocks. A ticker's
    header always has at least one contract under it on the same page; a ticker too long for the space left
    continues on the next page under a repeated '(cont.)' header; a ticker that would fit whole on a fresh page
    starts there instead of being split when the current page is mostly used."""
    pages: list[list[tuple]] = [[]]
    used = 0.0

    def cap() -> float:
        return PAGE1_CAP if len(pages) == 1 else PAGE_CAP

    def new_page():
        nonlocal used
        pages.append([])
        used = 0.0

    for g in groups:
        whole = BAND_H + len(g["rows"]) * ROW_H + GROUP_GAP
        if used + whole > cap() and whole <= PAGE_CAP and (cap() - used) < 0.5 * cap() and pages[-1]:
            new_page()
        cont = False
        i = 0
        while i < len(g["rows"]):
            if used + BAND_H + ROW_H > cap() + 1e-9 and pages[-1]:
                new_page()
                cont = i > 0
            pages[-1].append(("band", g, cont))
            used += BAND_H
            while i < len(g["rows"]) and used + ROW_H <= cap() + 1e-9:
                pages[-1].append(("row", g["rows"][i]))
                used += ROW_H
                i += 1
            if i < len(g["rows"]):
                new_page()
                cont = True
        pages[-1].append(("gap",))
        used += GROUP_GAP
    return [p for p in pages if p]


def render_report_cards(report: dict[str, Any], out_dir: str) -> list[str]:
    """Page 1 = header + summary tiles + the first tickers; further pages continue the grouped table."""
    os.makedirs(out_dir, exist_ok=True)
    pages = paginate(report["groups"])
    paths = []
    for n, blocks in enumerate(pages, start=1):
        body = sum(BAND_H if b[0] == "band" else ROW_H if b[0] == "row" else GROUP_GAP for b in blocks)
        note = n == len(pages) and report["stats"]["with_data"] < report["stats"]["count"]
        H = (1.8 + 2.6 + 0.55 if n == 1 else 1.25 + 0.4) + body + (0.3 if note else 0) + 1.15
        fig, ax, T = w._canvas(H)
        _header(T, report, n, len(pages), big=(n == 1))
        y = _summary_tiles(ax, T, report["stats"], 1.85) + 0.40 if n == 1 else 1.2
        y = _column_headers(ax, T, y)
        for b in blocks:
            if b[0] == "band":
                _draw_band(ax, T, b[1], y, b[2])
                y += BAND_H
            elif b[0] == "row":
                _draw_contract_row(ax, T, b[1], y)
                y += ROW_H
            else:
                y += GROUP_GAP
        if note:
            T(w.LEFT, y + 0.04, "— = no contract trades found for that flow yet",
              fontsize=8.6, color=w.GRAY, fontfamily=w.HEADER_FONT)
        _footer(T, ax, H, f"Page {n} of {len(pages)}" if len(pages) > 1 else "$1M+ flow report card")
        pth = os.path.join(out_dir, f"bigflow_{report['day'].isoformat()}_{n:02d}.png")
        fig.savefig(pth, facecolor=w.DARK_BG, dpi=w.DPI)
        plt.close(fig)
        paths.append(pth)
    return paths
