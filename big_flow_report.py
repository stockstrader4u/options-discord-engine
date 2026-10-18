"""
big_flow_report.py -- the 4:32 PM ET (data through the 4:00 close) "$1M+ Flow Card".

Every out-of-the-money, BOUGHT print of $1M+ premium that JarvisFlow saw today, market-wide: no watchlist
restriction and no expiry filter, shown as ONE image with one row per stock, largest total premium first.
This is a flow card, not a performance card: it describes the flow (the contract, how far out of the money it
is, what was paid, the breakeven, volume vs open interest, size, sweep or block) and says nothing about how the
contract has traded since.

How the list is built:
  * One market-wide JarvisFlow call (OTM, BOUGHT, today's trade date) sorted by `Premium`. That sort orders by
    each CONTRACT's total premium for the day, not by print size. A $1M+ print means its contract's day total
    is at least $1M, so those contracts sort ahead of everything smaller. The call returns at most 300 rows;
    when the last row's contract total is still under $1M, every $1M+ print is provably in the result. If a
    day ever fills all 300 rows above $1M, the query is repeated for SWEEP and BLOCK separately and merged.
  * "At or above the ask" cannot be checked directly: the payload has no bid/ask. JarvisFlow's own BOUGHT
    classification (its tool docs map "hit the ask" to BOUGHT) is the proxy, same as the conviction baseline.
  * Premium is per print, like every other premium figure in this engine.

Each stock's row describes its LARGEST print, so every figure on it can be traced to one trade:
  DTE        days from the print to expiry
  OTM        how far the strike is from the stock price at the print
  paid       the contract price the print traded at
  BE         the stock's move from its price at the print to break even at expiry (strike +/- paid)
  V/OI       the contract's volume vs its open interest when the print traded ("new" when OI was 0)
  size       contracts in the print
  today      the contract's total premium so far today (its latest running total), shown when it is larger than the print
The stock's current price (PRICE) is Alpaca's latest trade at publish time; it is the only Alpaca call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from PIL import Image, ImageDraw, ImageFont

import weekly_recap as w

logger = logging.getLogger("options-big-flow")

ET = ZoneInfo("America/New_York")
JARVIS_MCP_URL = os.getenv("JARVIS_MCP_URL", "https://api.jarvisflow.io/.well-known/mcp")
MIN_PREMIUM = 1_000_000
JARVIS_ROW_CAP = 300                       # JarvisFlow returns at most this many rows per call
MAX_ROWS_PER_PANEL = 14                    # stocks drawn per side of the landscape card; the rest fold into a "+N more" line


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


def _num(it: dict, *names: str) -> float | None:
    for n in names:
        v = it.get(n)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def _item_premium(it: dict) -> float:
    return _num(it, "total_Option_Premium_For_Trade", "totalOptionPremiumForTrade") or 0.0


def _contract_day_total(it: dict) -> float:
    return _num(it, "total_Option_Premium_for_Same_Contract_Today") or 0.0


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


# ETFs allowed on the card; every other ETF / fund / trust is dropped.
ETF_ALLOWLIST = {"SPY", "QQQ", "IWM", "VIX", "TLT", "HYG", "XLK", "XLF", "XLE", "XLV", "XLU", "SMH", "TQQQ", "SOXL"}
# Fund-like names that DO appear in the SEC's company list (grantor trusts), so the list alone would let them through.
_TRUST_ETFS = {"GLD", "SLV", "IAU", "GLDM", "SLVP", "USO", "UNG", "UUP", "BITO", "IBIT", "FBTC", "GBTC", "ETHE", "ETHA", "SPY", "DIA", "VXX", "UVXY"}
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_stock_tickers: set[str] | None = None


def load_stock_tickers() -> set[str] | None:
    """Tickers of operating companies (stocks and ADRs) from the SEC's public list; ETFs are not in it. Fetched once
    per process; None if the lookup fails (then only the known-trust set is enforced)."""
    global _stock_tickers
    if _stock_tickers is None:
        try:
            r = httpx.get(SEC_TICKERS_URL, timeout=30.0, headers={"User-Agent": os.getenv("SEC_USER_AGENT", "options-discord-engine admin@example.com")})
            r.raise_for_status()
            _stock_tickers = {str(v["ticker"]).upper() for v in r.json().values()}
        except Exception as e:
            logger.warning("big_flow stock list unavailable, ETF filter limited to known trusts: %s", e)
    return _stock_tickers


def is_blocked_etf(ticker: str, stocks: set[str] | None) -> bool:
    """True for an ETF outside ETF_ALLOWLIST: a known trust, or (when the stock list loaded) anything that is not an
    operating company."""
    t = (ticker or "").upper()
    if t in ETF_ALLOWLIST:
        return False
    return t in _TRUST_ETFS or (stocks is not None and t not in stocks)


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
    stocks = await asyncio.to_thread(load_stock_tickers)
    items = {_key(r): r for r in rows if qualifies(r, day, cutoff_et) and not is_blocked_etf(r.get("ticker"), stocks)}
    return sorted(items.values(), key=_item_premium, reverse=True)


# ---------------------------------------------------------------------------
# The stock's current price (Alpaca)
# ---------------------------------------------------------------------------

def fetch_stock_prices(tickers: list[str]) -> dict[str, float]:
    """Latest trade price for each stock / ETF, from Alpaca's snapshot endpoint (IEX feed: real time, though IEX
    prints can lag a thin name by a few cents). One request per 100 symbols. Symbols Alpaca does not know are
    simply missing from the result; any failure returns what was fetched so far, so the card shows '—' for a
    price rather than failing."""
    out: dict[str, float] = {}
    syms = sorted({t for t in tickers if t})
    for i in range(0, len(syms), 100):
        try:
            resp = httpx.get(f"{w.ALPACA_DATA}/v2/stocks/snapshots", headers=w._alpaca_headers(), timeout=20.0,
                             params={"symbols": ",".join(syms[i:i + 100]), "feed": "iex"})
            resp.raise_for_status()
            snaps = resp.json()
        except Exception as e:
            logger.warning("big_flow stock_prices_failed %s", e)
            continue
        for sym, snap in (snaps or {}).items():
            px = ((snap.get("latestTrade") or {}).get("p") or (snap.get("minuteBar") or {}).get("c")
                  or (snap.get("dailyBar") or {}).get("c"))
            if px:
                out[sym] = float(px)
    return out


# ---------------------------------------------------------------------------
# One print -> options data
# ---------------------------------------------------------------------------

def make_print(it: dict) -> dict[str, Any]:
    """One qualifying print with its options-specific figures. Anything the payload lacks is None."""
    t = trade_time_et(it)
    pc = str(it.get("put_Or_Call") or "").upper()
    strike = _num(it, "strike_Price", "strikePrice")
    spot = _num(it, "spot_Price", "spotPrice")
    paid = _num(it, "price_Of_Contract", "priceOfContract")
    paid = paid if paid and paid > 0 else None
    expiry = None
    try:
        expiry = date.fromisoformat(str(it.get("expriation_Date") or it.get("expriationDate"))[:10])
    except ValueError:
        pass
    vol, oi = _num(it, "volume_When_Traded", "volumeWhenTraded"), _num(it, "open_Interest_When_Traded", "openInterestWhenTraded")
    size = _num(it, "size_Of_Trade", "sizeOfTrade")
    premium = int(_item_premium(it))

    otm_pct = be_price = be_move_pct = None
    if strike and spot and pc in ("CALL", "PUT"):
        otm_pct = (strike / spot - 1.0) * 100.0 if pc == "CALL" else (1.0 - strike / spot) * 100.0
        if paid:
            be_price = strike + paid if pc == "CALL" else strike - paid
            be_move_pct = (be_price / spot - 1.0) * 100.0
    vol_oi = None                                       # "new" when open interest was zero
    if vol is not None and oi is not None:
        vol_oi = "new" if oi <= 0 else vol / oi
    return {
        "ticker": str(it.get("ticker") or "").upper(), "put_call": pc, "strike": strike, "expiry": expiry,
        "premium": premium, "alerted_at": t, "spot": spot, "paid": paid,
        "dte": (expiry - t.date()).days if expiry and t else None,
        "otm_pct": otm_pct, "be_price": be_price, "be_move_pct": be_move_pct,
        "vol_oi": vol_oi, "size": int(size) if size is not None else None,
        "kind": str(it.get("sweep_Or_Block") or "").upper() or None,
        "running_total": _contract_day_total(it),         # the contract's premium so far today, as of THIS print
        "day_total": None,                                # set in build_big_flow_report: the contract's latest total
    }


def group_by_ticker(prints: list[dict[str, Any]], prices: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """One summary per stock, biggest total premium first. A stock's row describes its LARGEST print (`top`);
    `n_flows` and `total_premium` cover all of its prints, so the header always adds up."""
    prices = prices or {}
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for p in prints:
        by_ticker.setdefault(p["ticker"], []).append(p)
    groups = []
    for ticker, ps in by_ticker.items():
        calls = sum(p["premium"] for p in ps if p["put_call"] == "CALL")
        puts = sum(p["premium"] for p in ps if p["put_call"] == "PUT")
        side = "both" if calls and puts else "calls" if calls else "puts"
        groups.append({"ticker": ticker, "stock_px": prices.get(ticker), "n_flows": len(ps), "side": side,
                       "total_premium": calls + puts, "call_premium": calls, "put_premium": puts,
                       "top": max(ps, key=lambda p: p["premium"]), "prints": sorted(ps, key=lambda p: p["alerted_at"])})
    groups.sort(key=lambda g: g["total_premium"], reverse=True)
    return groups


def build_big_flow_report(items: list[dict], day: date, now_et: datetime, price_fn=None) -> dict[str, Any]:
    """Turn the qualifying prints into the card's data, and look up each stock's current price (price_fn, default
    fetch_stock_prices). One Alpaca call at most; no DB, no Discord."""
    prints = [make_print(it) for it in items]
    # JarvisFlow's same-contract figure is a RUNNING total, so a contract's total today is the largest one seen
    # among its prints. Shown only when it is meaningfully more than the print itself.
    latest: dict[tuple, float] = {}
    for p in prints:
        k = (p["ticker"], p["put_call"], p["strike"], p["expiry"])
        latest[k] = max(latest.get(k, 0.0), p["running_total"])
    for p in prints:
        total = latest[(p["ticker"], p["put_call"], p["strike"], p["expiry"])]
        p["day_total"] = total if total > p["premium"] * 1.05 else None
    tickers = sorted({p["ticker"] for p in prints})
    try:
        prices = (price_fn or fetch_stock_prices)(tickers) if tickers else {}
    except Exception as e:
        logger.warning("big_flow stock price lookup failed: %s", e)
        prices = {}
    calls = [p for p in prints if p["put_call"] == "CALL"]
    puts = [p for p in prints if p["put_call"] == "PUT"]
    prem = lambda xs: sum(p["premium"] for p in xs)
    stats = {
        "count": len(prints), "unique_tickers": len(tickers), "total_premium": prem(prints),
        "calls": len(calls), "puts": len(puts), "call_premium": prem(calls), "put_premium": prem(puts),
        "sweeps": sum(1 for p in prints if p["kind"] == "SWEEP"), "blocks": sum(1 for p in prints if p["kind"] == "BLOCK"),
    }
    return {"day": day, "as_of": now_et, "prints": prints, "groups": group_by_ticker(prints, prices), "stats": stats,
            "caption": format_caption(day, now_et, stats)}


def format_caption(day: date, now_et: datetime, stats: dict[str, Any]) -> str:
    clock = now_et.strftime("%I:%M %p").lstrip("0")
    return (f"**$1M+ Flow Card — {day.strftime('%a %b %d')}**   {stats['count']} flows · {stats['unique_tickers']} stocks · "
            f"{w._fmt_prem(stats['total_premium'])} premium · {stats['calls']} calls / {stats['puts']} puts\n"
            f"*Out-of-the-money, bought (at/above the ask per JarvisFlow), single prints of $1M+, market-wide, "
            f"through {clock} ET. Not trade recommendations.*")


# ---------------------------------------------------------------------------
# Rendering: one wide landscape image, PUTS and CALLS as two tables side by side
# ---------------------------------------------------------------------------

TITLE = "$1M+ FLOW CARD"
W = 3600
BG, PANEL_BG, ROW_SHADES = "#0B1220", "#141D31", ("#16203A", "#121A2E")
TXT, TXT2, DIM, HAIR = "#F1F5F9", "#9AA7BC", "#5E6B84", "#223052"
ACCENT = {"puts": ("#F472B6", "#3B1F33"), "calls": ("#60A5FA", "#1B2B4A")}
PILL = {"SWEEP": ("#1E3A8A", "#93C5FD"), "BLOCK": ("#26303F", "#9CA3AF")}
MARGIN, GUTTER = 50, 36
ROW_H, ROW_GAP = 116, 8
EXPLAINER = "One row per name · sorted by total premium · details = largest print."
FOOTER = ("OTM = strike vs stock price at the print · BE = move needed to break even at expiry",
          "V/OI = volume vs open interest · PRICE = latest trade (Alpaca) · not trade recommendations")

_FONT_DIRS = [os.getenv("FLOW_CARD_FONT_DIR", ""), os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")]


def _font_path(bold: bool) -> str:
    """Inter when it is installed or dropped in ./fonts; otherwise the DejaVu Sans that ships with matplotlib."""
    for d in _FONT_DIRS:
        for name in (("Inter-Bold.ttf", "Inter_28pt-Bold.ttf") if bold else ("Inter-Regular.ttf", "Inter_28pt-Regular.ttf")):
            p = os.path.join(d, name) if d else ""
            if p and os.path.exists(p):
                return p
    import matplotlib
    return os.path.join(matplotlib.get_data_path(), "fonts", "ttf", "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")


_FONTS: dict[tuple, ImageFont.FreeTypeFont] = {}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    k = (size, bold)
    if k not in _FONTS:
        _FONTS[k] = ImageFont.truetype(_font_path(bold), size)
    return _FONTS[k]


def _tw(text: str, size: int, bold: bool = False) -> int:
    return int(round(_font(size, bold).getlength(text)))


def _fit(text: str, size: int, bold: bool, max_w: int, min_size: int) -> int:
    """Largest font size <= size (never below min_size) at which text fits max_w; raises if even min_size does not."""
    s = size
    while s > min_size and _tw(text, s, bold) > max_w:
        s -= 1
    if _tw(text, s, bold) > max_w:
        raise ValueError(f"flow card: {text!r} does not fit {max_w}px even at {s}px")
    return s


def _money(v: float, dec: int = 2) -> str:
    if v >= 1e9:
        return f"${v / 1e9:.{dec}f}B"
    if v >= 1e6:
        return f"${v / 1e6:.{dec}f}M"
    return f"${v / 1e3:.0f}K"


def _fmt_vol_oi(v) -> str:
    if v is None:
        return "—"
    return "new" if v == "new" else (f"{v:.1f}x" if v < 100 else f"{v:.0f}x")


def _row_side(g: dict) -> str:
    return "puts" if g["put_premium"] > g["call_premium"] else "calls"


def build_card_payload(report: dict[str, Any]) -> dict[str, Any]:
    """The landscape card's input contract (field names fixed by the design spec), built from the report.
    Keys starting with '_' carry raw numbers for the consistency asserts; they are never drawn."""
    st = report["stats"]
    tot = max(st["call_premium"] + st["put_premium"], 1)
    rows = []
    for g in report["groups"]:
        t = g["top"]
        voi = t["vol_oi"]
        strike = w._fmt_strike(t["strike"]) if t.get("strike") is not None else "?"
        exp = t["expiry"].strftime("%b %d, %Y") if t.get("expiry") else "?"
        rows.append({
            "ticker": g["ticker"], "price": w._fmt_px(g["stock_px"]) if g.get("stock_px") else None, "side": _row_side(g),
            "total_premium": _money(g["total_premium"]), "_premium": g["total_premium"], "n_prints": g["n_flows"],
            "largest_print": f"{(t['put_call'] or '?')[:1]} {strike} · {exp}",
            "dte": None if t["dte"] is None else f"DTE {t['dte']}",
            "otm": None if t["otm_pct"] is None else f"OTM {t['otm_pct']:.1f}%",
            "paid": f"paid {w._fmt_px(t['paid'])}" if t["paid"] else None,
            "be": None if t["be_move_pct"] is None else f"BE {t['be_move_pct']:+.1f}%",
            "voi": None if voi is None else f"V/OI {_fmt_vol_oi(voi)}",
            "cts": None if t["size"] is None else f"{t['size']:,} cts",
            "kind": t["kind"], "today": f"today {_money(t['day_total'])}" if t["day_total"] else None,
        })
    return {
        "date_label": report["day"].strftime("%A, %B %d, %Y").replace(" 0", " "),
        "cutoff_label": f"through {report['as_of'].strftime('%I:%M %p').lstrip('0')} ET",
        "stats": {"flows": st["count"], "stocks": st["unique_tickers"], "total_premium": _money(st["total_premium"], 1),
                  "calls_puts_pct": f"{st['call_premium'] / tot * 100:.0f}% / {st['put_premium'] / tot * 100:.0f}%",
                  "sweeps_blocks": f"{st['sweeps']} / {st['blocks']}", "_total": st["total_premium"]},
        "rows": rows,
    }


def _split_panels(payload: dict[str, Any]) -> list[tuple[str, list[dict], float]]:
    """[(side, rows, subtotal)], larger-premium side first. Fails loudly rather than drawing a wrong card."""
    rows, st = payload["rows"], payload["stats"]
    for r in rows:
        if not (r["ticker"] and r["total_premium"] and r["largest_print"] and r["kind"] in ("SWEEP", "BLOCK", None)):
            raise ValueError(f"flow card: incomplete row {r.get('ticker')!r}")
    by = {s: [r for r in rows if r["side"] == s] for s in ("puts", "calls")}
    sub = {s: sum(r["_premium"] for r in by[s]) for s in by}
    for s in by:
        prem = [r["_premium"] for r in by[s]]
        if prem != sorted(prem, reverse=True):
            raise ValueError(f"flow card: {s} rows are not in descending premium order")
    if sub["puts"] + sub["calls"] != st["_total"] or _money(st["_total"], 1) != st["total_premium"]:
        raise ValueError("flow card: panel subtotals do not add up to the total premium")
    first = "puts" if sub["puts"] > sub["calls"] else "calls"
    second = "calls" if first == "puts" else "puts"
    return [(first, by[first], sub[first]), (second, by[second], sub[second])]


def _pill(d, right: int, cy: int, text: str, bg: str, fg: str, size: int = 24, pad: int = 18) -> int:
    """Fully rounded pill with its right edge at `right`; returns its left edge."""
    h = size + 18
    left = right - _tw(text, size, True) - 2 * pad
    d.rounded_rectangle((left, cy - h // 2, right, cy + h // 2), radius=h // 2, fill=bg)
    d.text((left + pad, cy), text, font=_font(size, True), fill=fg, anchor="lm")
    return left


def _draw_row(d, x: int, y: int, pw: int, rank: int, r: dict, shade: str) -> None:
    d.rounded_rectangle((x, y, x + pw, y + ROW_H), radius=18, fill=shade)
    fg, bg = ACCENT[r["side"]]
    cy1, cy2, pad = y + 36, y + 84, 24
    # right side first (kind pill, then the total) so line 1 knows how much room the left side has
    kind_left = _pill(d, x + pw - pad, cy1, r["kind"], *PILL[r["kind"]]) if r["kind"] else x + pw - pad + 22   # no pill if the print had no sweep/block tag
    d.text((kind_left - 22, cy1), r["total_premium"], font=_font(40, True), fill=TXT, anchor="rm")
    limit = kind_left - 22 - _tw(r["total_premium"], 40, True) - 30
    d.text((x + pad, cy1 - 6), f"#{rank}", font=_font(25), fill=DIM, anchor="lm")
    tx = x + pad + _tw(f"#{rank}", 25) + 22
    d.text((tx, cy1), r["ticker"], font=_font(42, True), fill=TXT, anchor="lm")
    px = tx + _tw(r["ticker"], 42, True) + 20
    label = r["side"].upper() + (f" ×{r['n_prints']}" if r["n_prints"] > 1 else "")
    pill_w = _tw(label, 24, True) + 36
    _pill(d, px + pill_w, cy1, label, bg, fg)
    px += pill_w + 20
    if r["price"]:
        if px + _tw(r["price"], 29) > limit:
            raise ValueError(f"flow card: {r['ticker']} line 1 would touch its total")
        d.text((px, cy1 + 2), r["price"], font=_font(29), fill=TXT2, anchor="lm")
    # line 2: largest print, then today, then the dim detail tail; all on one line
    lx = x + pad
    d.text((lx, cy2), r["largest_print"], font=_font(29, True), fill=TXT, anchor="lm")
    lx += _tw(r["largest_print"], 29, True) + 24
    if r["today"]:
        d.text((lx, cy2 + 1), r["today"], font=_font(27), fill=TXT2, anchor="lm")
        lx += _tw(r["today"], 27) + 24
    tail = "  ·  ".join(v for v in (r["dte"], r["otm"], r["paid"], r["be"], r["voi"], r["cts"]) if v)
    if tail:
        size = _fit(tail, 27, False, x + pw - pad - lx, 22)
        d.text((lx, cy2 + 1), tail, font=_font(size), fill=DIM, anchor="lm")


def _stat_card(d, x0: int, y0: int, cw: int, ch: int, num: str, lab: str) -> None:
    d.rounded_rectangle((x0, y0, x0 + cw, y0 + ch), radius=22, fill=PANEL_BG, outline=HAIR, width=2)
    inner, gap = cw - 80, 28
    lab_w = _tw(lab, 30, True)
    size = 60
    while size > 42 and _tw(num, size, True) + gap + lab_w > inner:
        size -= 1
    if _tw(num, size, True) + gap + lab_w <= inner:       # number left, label beside it
        d.text((x0 + 40, y0 + ch // 2), num, font=_font(size, True), fill=TXT, anchor="lm")
        d.text((x0 + 40 + _tw(num, size, True) + gap, y0 + ch // 2 + 8), lab, font=_font(30, True), fill=DIM, anchor="lm")
    else:                                                  # label drops below the number only when they cannot fit side by side
        size = _fit(num, 60, True, inner, 42)
        d.text((x0 + 40, y0 + 48), num, font=_font(size, True), fill=TXT, anchor="lm")
        d.text((x0 + 40, y0 + 112), lab, font=_font(30, True), fill=DIM, anchor="lm")


def render_report_cards(report: dict[str, Any], out_dir: str) -> list[str]:
    """The whole day as ONE landscape PNG, 3600px wide: a stat strip, then PUTS and CALLS tables side by side (the
    larger-premium side on the left). Returns a one-element list so callers can post it like any set of cards."""
    os.makedirs(out_dir, exist_ok=True)
    payload = build_card_payload(report)
    panels = _split_panels(payload)
    n_rows = max(1, min(MAX_ROWS_PER_PANEL, max(len(p[1]) for p in panels)))
    pw = (W - 2 * MARGIN - GUTTER) // 2
    stat_y, panel_y = 280, 470
    rows_y = panel_y + 100
    H = rows_y + n_rows * (ROW_H + ROW_GAP) + 30 + 140
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    cx = W // 2
    d.text((cx, 86), TITLE, font=_font(84, True), fill=TXT, anchor="mm")
    d.text((cx, 176), f"{payload['date_label']}  ·  {payload['cutoff_label']}", font=_font(40), fill=TXT2, anchor="mm")
    d.text((cx, 236), EXPLAINER, font=_font(30), fill=DIM, anchor="mm")
    st = payload["stats"]
    cells = [(str(st["flows"]), f"FLOWS · {st['stocks']} STOCKS"), (st["total_premium"], "TOTAL PREMIUM"),
             (st["calls_puts_pct"], "CALLS / PUTS (PREMIUM)"), (st["sweeps_blocks"], "SWEEPS / BLOCKS")]
    gap = 28
    cw = (W - 2 * MARGIN - 3 * gap) // 4
    for i, (num, lab) in enumerate(cells):
        _stat_card(d, MARGIN + i * (cw + gap), stat_y, cw, 150, num, lab)
    total = sum(p[2] for p in panels) or 1
    for pi, (side, rows, sub) in enumerate(panels):
        x = MARGIN + pi * (pw + GUTTER)
        fg, bg = ACCENT[side]
        d.rounded_rectangle((x, panel_y, x + pw, panel_y + 84), radius=22, fill=bg)
        d.rounded_rectangle((x, panel_y, x + 14, panel_y + 84), radius=7, fill=fg)
        d.text((x + 44, panel_y + 42), side.upper(), font=_font(52, True), fill=fg, anchor="lm")
        d.text((x + pw - 28, panel_y + 44), f"{len(rows)} names · {_money(sub, 1)} premium · {sub / total * 100:.0f}%",
               font=_font(33), fill=TXT2, anchor="rm")
        shown = rows[:n_rows]
        for i in range(n_rows):                              # both panels draw the same number of slots, so they stay aligned
            ry = rows_y + i * (ROW_H + ROW_GAP)
            if i < len(shown):
                _draw_row(d, x, ry, pw, i + 1, shown[i], ROW_SHADES[i % 2])
            else:
                d.rounded_rectangle((x, ry, x + pw, ry + ROW_H), radius=18, fill=ROW_SHADES[i % 2])
        if len(rows) > n_rows:
            more = rows[n_rows:]
            d.text((x + pw - 28, rows_y + n_rows * (ROW_H + ROW_GAP) + 16),
                   f"+ {len(more)} more · {_money(sum(r['_premium'] for r in more), 1)}", font=_font(27), fill=DIM, anchor="rm")
    fy = H - 120
    d.line((MARGIN, fy, W - MARGIN, fy), fill=HAIR, width=2)
    for i, line in enumerate(FOOTER):
        d.text((cx, fy + 42 + i * 48), line, font=_font(_fit(line, 29, False, W - 2 * MARGIN, 22)), fill=DIM, anchor="mm")
    pth = os.path.join(out_dir, f"bigflow_{report['day'].isoformat()}.png")
    img.save(pth)
    return [pth]
