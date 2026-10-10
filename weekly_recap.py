"""
weekly_recap.py — Friday end-of-week "Week in Flow" recap.

A neutral record of what we PUBLISHED this week and what each contract did
afterward. It is deliberately NOT a scorecard:

  - no WIN / LOSS labels, no win rate, no "winners" or "laggards"
  - the flows are observations of options activity, not trade calls, so the
    card never implies a position was recommended
  - every number is the OPTION CONTRACT's own traded price (Alpaca option
    bars), not the stock price:
        ALERT  last traded contract price at the moment the flow was published
        HIGH   highest traded price between publish and the end of the window
        LOW    lowest traded price in the same span
        LAST   latest traded price, or the settlement value if it expired
    plus a small path sparkline so you can see HOW it got there.
  - the stock's move since the alert is shown as context only.

Window and DB source are unchanged (flow_events rows for the last
TRADING_DAYS_IN_WINDOW trading days). build_weekly_recap() stays pure
computation (network reads only, no matplotlib); render_weekly_recap_card()
draws the PNG; post_weekly_recap_image() posts it.

Env: ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY (option + stock bars).
     FINNHUB_API_KEY is now only a fallback for the stock's latest price.
"""

from __future__ import annotations

import logging
import os
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyBboxPatch

from market_hours import most_recent_trading_day, trading_days_before
from db import get_flow_events_in_range

logger = logging.getLogger("options-weekly-recap")

ET = ZoneInfo("America/New_York")
FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
ALPACA_DATA = "https://data.alpaca.markets"
TRADING_DAYS_IN_WINDOW = 5
BAR_TIMEFRAME = "5Min"
SESSION_MINUTES = 390          # 9:30-16:00
MAX_ROWS = 36                  # rows drawn in full; the rest fold into one summary line
DATA_DELAY = timedelta(minutes=16)   # the data plan refuses requests whose end is within ~15 min of now
SYMBOL_BATCH = 25              # option symbols per Alpaca request


class QuoteUnavailable(Exception):
    """Raised when a ticker's current quote can't be fetched."""
    def __init__(self, ticker: str, reason: str):
        self.ticker = ticker
        self.reason = reason
        super().__init__(f"{ticker}: {reason}")


class MarketDataError(Exception):
    """Alpaca request failed (after retries)."""


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def get_quote(ticker: str, api_key: str, timeout: float = 10.0) -> dict[str, float]:
    """Finnhub quote (fallback for the stock's latest price only)."""
    if not api_key:
        raise QuoteUnavailable(ticker, "FINNHUB_API_KEY not configured")
    try:
        resp = httpx.get(FINNHUB_QUOTE_URL, params={"symbol": ticker, "token": api_key}, timeout=timeout)
    except httpx.RequestError as e:
        raise QuoteUnavailable(ticker, f"network error: {e}")
    if resp.status_code != 200:
        raise QuoteUnavailable(ticker, f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise QuoteUnavailable(ticker, "invalid JSON response")
    if not data.get("c"):
        raise QuoteUnavailable(ticker, "no data returned (symbol may be invalid)")
    return {"current": data["c"], "high": data.get("h"), "low": data.get("l")}


def _alpaca_headers() -> dict[str, str]:
    return {"APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY_ID", ""),
            "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET_KEY", "")}


def alpaca_configured() -> bool:
    return bool(os.environ.get("ALPACA_API_KEY_ID") and os.environ.get("ALPACA_API_SECRET_KEY"))


def _paged_bars(path: str, params: dict) -> dict[str, list[dict]]:
    """GET an Alpaca bars endpoint, following next_page_token. Returns {symbol: [bar,...]}."""
    out: dict[str, list[dict]] = {}
    p = dict(params)
    for _ in range(60):
        r = None
        for attempt in range(4):
            r = httpx.get(ALPACA_DATA + path, headers=_alpaca_headers(), params=p, timeout=30.0)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            break
        if r is None or r.status_code != 200:
            raise MarketDataError(f"{path} HTTP {getattr(r, 'status_code', '?')}: {getattr(r, 'text', '')[:160]}")
        j = r.json()
        for sym, rows in (j.get("bars") or {}).items():
            out.setdefault(sym, []).extend(rows)
        tok = j.get("next_page_token")
        if not tok:
            break
        p["page_token"] = tok
    return out


def _utc_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_bar(b: dict) -> tuple[datetime, float, float, float, float]:
    ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
    return ts, float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])


def occ_symbol(ticker: str, expiry: date, put_call: str, strike: float) -> str:
    cp = "C" if put_call.upper().startswith("C") else "P"
    return f"{ticker.upper()}{expiry:%y%m%d}{cp}{int(round(float(strike) * 1000)):08d}"


def fetch_option_bars(symbols: list[str], start: datetime, end: datetime) -> dict[str, list[tuple]]:
    """{occ_symbol: [(ts_et, o, h, l, c), ...]} for 5-minute traded-price bars."""
    result: dict[str, list[tuple]] = {}
    for i in range(0, len(symbols), SYMBOL_BATCH):
        chunk = symbols[i:i + SYMBOL_BATCH]
        raw = _paged_bars("/v1beta1/options/bars", {
            "symbols": ",".join(chunk), "timeframe": BAR_TIMEFRAME, "limit": 10000,
            "start": _utc_str(start), "end": _utc_str(end), "sort": "asc"})
        for sym, rows in raw.items():
            result[sym] = [_parse_bar(b) for b in rows]
    return result


def fetch_stock_daily_closes(tickers: list[str], start: date, end: date) -> dict[str, dict[date, float]]:
    """{ticker: {date: close}} daily closes (IEX feed; close can differ a few cents from consolidated)."""
    out: dict[str, dict[date, float]] = {}
    for i in range(0, len(tickers), SYMBOL_BATCH):
        chunk = tickers[i:i + SYMBOL_BATCH]
        raw = _paged_bars("/v2/stocks/bars", {
            "symbols": ",".join(chunk), "timeframe": "1Day", "limit": 10000, "feed": "iex",
            "adjustment": "split", "sort": "asc",
            "start": start.isoformat(), "end": (end + timedelta(days=1)).isoformat()})
        for sym, rows in raw.items():
            out[sym] = {_parse_bar(b)[0].date(): float(b["c"]) for b in rows}
    return out


# ---------------------------------------------------------------------------
# Window resolution
# ---------------------------------------------------------------------------

def resolve_recap_window(as_of: date | None = None) -> tuple[date, date]:
    """(start_date, end_date); end_date rolls back to the last real trading day."""
    if as_of is None:
        as_of = datetime.now(timezone.utc).date()
    end_date = most_recent_trading_day(as_of)
    # n-1 days back, so the window is exactly N trading days (end day included) and the
    # previous Friday's flows belong to the previous recap
    start_date = trading_days_before(end_date, TRADING_DAYS_IN_WINDOW - 1)
    return start_date, end_date


# ---------------------------------------------------------------------------
# Per-flow computation
# ---------------------------------------------------------------------------

def _as_utc(dt: datetime) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _session_minute(ts: datetime) -> float:
    m = (ts.hour * 60 + ts.minute + ts.second / 60) - (9 * 60 + 30)
    return min(max(m, 0.0), float(SESSION_MINUTES))


def trading_x(ts: datetime, origin: datetime) -> float:
    """Position in 'trading minutes' since origin: overnight/weekend gaps collapse to nothing."""
    days = int(np.busday_count(origin.date(), ts.date()))
    return days * SESSION_MINUTES + _session_minute(ts) - _session_minute(origin)


def _is_expired(expiry: date, now_et: datetime) -> bool:
    return expiry < now_et.date() or (expiry == now_et.date() and now_et.hour >= 16)


def compute_flow_result(row: dict[str, Any], bars: list[tuple] | None,
                        daily_closes: dict[date, float] | None,
                        spot_last: float | None, now_et: datetime) -> dict[str, Any]:
    """One published flow -> neutral facts about what its contract traded at afterward."""
    alerted_at = _as_utc(row["ingested_at"]).astimezone(ET)
    put_call = (row.get("put_call") or "").upper()
    strike = row.get("strike")
    expiry = date.fromisoformat(str(row["expiry_date"])[:10]) if row.get("expiry_date") else None
    spot_alert = row.get("spot_price")

    res: dict[str, Any] = {
        "ticker": row["ticker"], "contract": row.get("contract"), "put_call": put_call,
        "strike": strike, "expiry": expiry, "premium": row.get("premium") or 0,
        "alerted_at": alerted_at, "alert_hash": row.get("alert_hash"),
        "spot_alert": spot_alert, "spot_last": spot_last,
        "spot_chg_pct": ((spot_last - spot_alert) / spot_alert * 100) if (spot_last and spot_alert) else None,
        "ok": False, "reason": None, "expired": bool(expiry and _is_expired(expiry, now_et)),
    }
    if expiry is None or strike is None or not put_call:
        res["reason"] = "contract details missing"
        return res
    if not bars:
        res["reason"] = "no option trades found"
        return res

    # ALERT price: last traded bar at or before the publish time (same session), else the first trade after it.
    cutoff = alerted_at.replace(second=0, microsecond=0)
    before = [b for b in bars if b[0] <= alerted_at and b[0].date() == alerted_at.date()]
    after = [b for b in bars if b[0] > alerted_at]
    if before:
        alert_px, alert_note = before[-1][4], None
    elif after and (after[0][0] - alerted_at) <= timedelta(hours=2):
        alert_px, alert_note = after[0][1], "first trade after publish"
    else:
        res["reason"] = "no trade near publish time"
        return res

    window = [b for b in bars if b[0] >= cutoff - timedelta(minutes=5)]
    if not window:
        res["alert_px"] = alert_px
        res["reason"] = "published after the close — no trades since"
        return res

    hi_bar = max(window, key=lambda b: b[2])
    lo_bar = min(window, key=lambda b: b[3])
    high_px, low_px = max(hi_bar[2], alert_px), min(lo_bar[3], alert_px)

    # path (for the sparkline): alert point, then each bar's close, in trading-minute space
    path = [(0.0, alert_px)]
    for b in window:
        if b[0] > alerted_at:
            path.append((trading_x(b[0], alerted_at), b[4]))

    if res["expired"]:
        close = (daily_closes or {}).get(expiry)
        if close is not None:
            last_px = max(0.0, close - strike) if put_call == "CALL" else max(0.0, strike - close)
            last_note = "settled"
        else:
            last_px, last_note = window[-1][4], "last trade"
        end_x = trading_x(datetime(expiry.year, expiry.month, expiry.day, 16, 0, tzinfo=ET), alerted_at)
    else:
        last_px, last_note = window[-1][4], None
        end_x = trading_x(now_et if now_et.hour < 16 else now_et.replace(hour=16, minute=0), alerted_at)
    end_x = max(end_x, path[-1][0])
    path.append((end_x, last_px))

    pct = lambda px: (px / alert_px - 1.0) * 100.0
    res.update(
        ok=True, alert_px=alert_px, alert_note=alert_note,
        high_px=high_px, low_px=low_px, last_px=last_px, last_note=last_note,
        high_pct=pct(high_px), low_pct=pct(low_px), last_pct=pct(last_px),
        path=path, path_pct=[(x, pct(y)) for x, y in path], end_x=end_x,
    )
    return res


# ---------------------------------------------------------------------------
# Recap assembly
# ---------------------------------------------------------------------------

def build_weekly_recap(api_key: str = "", as_of: date | None = None) -> dict[str, Any]:
    """
    Pull the window's published flows, fetch each contract's traded prices from
    Alpaca, and return a neutral recap dict. `api_key` is the optional Finnhub key
    (used only if Alpaca has no daily bar for a stock).
    """
    start_date, end_date = resolve_recap_window(as_of)
    rows = get_flow_events_in_range(start_date.isoformat(), end_date.isoformat())
    now_et = datetime.now(ET)

    flows: list[dict[str, Any]] = []
    if rows and alpaca_configured():
        syms: dict[int, str] = {}
        for i, r in enumerate(rows):
            if r.get("expiry_date") and r.get("strike") is not None and r.get("put_call"):
                syms[i] = occ_symbol(r["ticker"], date.fromisoformat(str(r["expiry_date"])[:10]),
                                     r["put_call"], r["strike"])
        first_alert = min(_as_utc(r["ingested_at"]) for r in rows)
        bars_by_sym: dict[str, list[tuple]] = {}
        try:
            bars_by_sym = fetch_option_bars(sorted(set(syms.values())), first_alert - timedelta(hours=1),
                                            datetime.now(timezone.utc) - DATA_DELAY)
        except MarketDataError as e:
            logger.warning("weekly_recap option_bars_failed %s", e)
        tickers = sorted({r["ticker"] for r in rows})
        closes: dict[str, dict[date, float]] = {}
        try:
            closes = fetch_stock_daily_closes(tickers, first_alert.astimezone(ET).date(),
                                              max(end_date, now_et.date()))
        except MarketDataError as e:
            logger.warning("weekly_recap stock_daily_failed %s", e)

        for i, r in enumerate(rows):
            tk = r["ticker"]
            tick_closes = closes.get(tk) or {}
            spot_last = tick_closes[max(tick_closes)] if tick_closes else None
            if spot_last is None and api_key:
                try:
                    spot_last = get_quote(tk, api_key)["current"]
                except QuoteUnavailable:
                    pass
            flows.append(compute_flow_result(r, bars_by_sym.get(syms.get(i, "")), tick_closes, spot_last, now_et))
    else:
        for r in rows:
            f = compute_flow_result(r, None, None, None, now_et)
            f["reason"] = "Alpaca keys not configured" if not alpaca_configured() else f["reason"]
            flows.append(f)

    stats = _summarize(flows)
    return {
        "start_date": start_date, "end_date": end_date,
        "total_alerts": len(flows), "flows": flows, "stats": stats,
        "caption": format_weekly_recap_caption(start_date, end_date, stats),
    }


def _summarize(flows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [f for f in flows if f["ok"]]
    calls = [f for f in flows if f["put_call"] == "CALL"]
    puts = [f for f in flows if f["put_call"] == "PUT"]
    prem = lambda xs: sum(f["premium"] for f in xs)
    tickers: dict[str, int] = {}
    for f in flows:
        tickers[f["ticker"]] = tickers.get(f["ticker"], 0) + 1
    return {
        "count": len(flows), "with_data": len(ok), "unique_tickers": len(tickers),
        "calls": len(calls), "puts": len(puts),
        "call_premium": prem(calls), "put_premium": prem(puts), "total_premium": prem(flows),
        "median_high_pct": statistics.median(f["high_pct"] for f in ok) if ok else None,
        "median_low_pct": statistics.median(f["low_pct"] for f in ok) if ok else None,
        "repeat_tickers": sorted(((t, n) for t, n in tickers.items() if n > 1), key=lambda x: -x[1]),
    }


# ---------------------------------------------------------------------------
# Caption
# ---------------------------------------------------------------------------

def _fmt_prem(v: float) -> str:
    if v >= 1e6:
        return f"${v / 1e6:.2f}M" if v < 1e7 else f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def format_weekly_recap_caption(start_date: date, end_date: date, stats: dict[str, Any]) -> str:
    date_range = f"{start_date.strftime('%b %d')}–{end_date.strftime('%b %d')}"
    return (f"**Week in Flow — {date_range}**   {stats['count']} flows published · "
            f"{_fmt_prem(stats['total_premium'])} premium · {stats['calls']} calls / {stats['puts']} puts\n"
            f"*A recap of published flow and how each contract traded afterward — not trade recommendations.*")


# ---------------------------------------------------------------------------
# Card rendering
#
# Discord shows an attached image ~500px wide in the chat, so the cards are drawn on a
# NARROW canvas (8in) with large type, and a big week is split across pages: one summary
# page followed by detail pages of ROWS_PER_PAGE flows each. Readability does not degrade
# as the number of published flows grows -- only the page count does.
# ---------------------------------------------------------------------------

DARK_BG      = "#0a0e1c"
PANEL_BG     = "#12172a"
PANEL_BORDER = "#252c47"
TXT_LIGHT    = "#f2f4f8"
TXT_DIM      = "#9aa3b8"
GRAY         = "#6b7488"
# Deliberately NOT green/red: those read as "good/bad". Blue = contract price above its price
# at publish, amber = below it. Calls/puts get quiet hues of their own.
UP           = "#6ea8ff"
DOWN         = "#f0a04b"
CALL_HUE     = "#9fb3d9"
PUT_HUE      = "#c4a7e7"
HEADER_FONT  = "DejaVu Sans"
DATA_FONT    = "DejaVu Sans Mono"

W = 8.0
LEFT, RIGHT = 0.40, W - 0.40
CENTER = W / 2
ROWS_PER_PAGE = 6
ROW_H = 1.52
DPI = 200
DISCLAIMER = ("Recap of options flow we published · contract traded prices (Alpaca) · "
              "not trade recommendations · not financial advice")


def _esc(text: str) -> str:
    """Matplotlib treats a pair of unescaped '$' as math-mode delimiters."""
    return text.replace("$", r"\$")


def _fmt_px(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "N/A"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:+.0f}%" if abs(v) >= 10 else f"{v:+.1f}%"


def _fmt_strike(k: float) -> str:
    return f"{k:g}"


def _clock(ts: datetime) -> str:
    h = ts.hour % 12 or 12
    return f"{ts.strftime('%a')} {h}:{ts.minute:02d}{'a' if ts.hour < 12 else 'p'}"


def _canvas(height: float):
    fig = plt.figure(figsize=(W, height), facecolor=DARK_BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(height, 0)
    ax.axis("off")

    def T(x, y, s, **kw):
        kw.setdefault("va", "top")
        return ax.text(x, y, _esc(s), **kw)
    return fig, ax, T


def _footer(T, ax, H, page_label: str):
    ax.plot([LEFT, RIGHT], [H - 0.62, H - 0.62], color=PANEL_BORDER, linewidth=1)
    T(CENTER, H - 0.50, DISCLAIMER, ha="center", fontsize=7.8, color=GRAY, fontfamily=HEADER_FONT)
    T(CENTER, H - 0.28, page_label, ha="center", fontsize=8.5, color=TXT_DIM, fontfamily=HEADER_FONT)


def _range(recap) -> str:
    return f"{recap['start_date'].strftime('%b %d')}–{recap['end_date'].strftime('%b %d, %Y')}"


def _outcome_buckets(flows: list[dict]) -> list[tuple[str, int, str]]:
    """Neutral distribution of where each contract stood at the end of the window."""
    ok = [f for f in flows if f["ok"]]
    defs = [
        ("Expired worthless", lambda f: f["expired"] and f["last_px"] <= 0.005, DOWN),
        ("Down 50% or more", lambda f: f["last_pct"] <= -50, DOWN),
        ("Down 10–50%", lambda f: -50 < f["last_pct"] <= -10, DOWN),
        ("Within 10% of publish price", lambda f: -10 < f["last_pct"] < 10, GRAY),
        ("Up 10–50%", lambda f: 10 <= f["last_pct"] < 50, UP),
        ("Up 50% or more", lambda f: f["last_pct"] >= 50, UP),
    ]
    out, taken = [], set()
    for label, test, color in defs:
        hit = [id(f) for f in ok if id(f) not in taken and test(f)]
        taken.update(hit)
        out.append((label, len(hit), color))
    return out


def render_summary_card(recap: dict[str, Any], out_path: str, pages: int = 1) -> None:
    stats, flows = recap["stats"], recap["flows"]
    by_day: dict[date, list] = {}
    for f in flows:
        by_day.setdefault(f["alerted_at"].date(), []).append(f)
    buckets = _outcome_buckets(flows)
    nodata = [f for f in flows if not f["ok"]]

    H = 1.9 + 2.75 + (0.62 + len(by_day) * 0.52) + (0.62 + len(buckets) * 0.52 + (0.40 if nodata else 0)) + 0.95
    fig, ax, T = _canvas(H)

    T(CENTER, 0.40, "WEEK IN FLOW", ha="center", fontsize=26, color=TXT_LIGHT, fontweight="bold", fontfamily=HEADER_FONT)
    T(CENTER, 1.00, _range(recap), ha="center", fontsize=15, color=TXT_DIM, fontfamily=HEADER_FONT)
    T(CENTER, 1.45, "The options flow we published this week,\nand how each contract traded afterward", ha="center",
      fontsize=11.5, color=GRAY, fontfamily=HEADER_FONT, linespacing=1.4)

    tw, th, tg = (RIGHT - LEFT - 0.2) / 2, 1.25, 0.2
    ty = 1.95

    def tile(col, row):
        x0, y0 = LEFT + col * (tw + tg), ty + row * (th + tg)
        ax.add_patch(FancyBboxPatch((x0, y0), tw, th, boxstyle="round,pad=0,rounding_size=0.09",
                                    linewidth=1, edgecolor=PANEL_BORDER, facecolor=PANEL_BG))
        return x0 + tw / 2, y0

    cx, y0 = tile(0, 0)
    T(cx, y0 + 0.15, str(stats["count"]), ha="center", fontsize=30, color=TXT_LIGHT, fontweight="bold", fontfamily=HEADER_FONT)
    T(cx, y0 + 0.82, f"FLOWS PUBLISHED · {stats['unique_tickers']} TICKERS", ha="center", fontsize=9.5, color=TXT_DIM, fontweight="bold", fontfamily=HEADER_FONT)
    cx, y0 = tile(1, 0)
    T(cx, y0 + 0.15, _fmt_prem(stats["total_premium"]), ha="center", fontsize=30, color=TXT_LIGHT, fontweight="bold", fontfamily=HEADER_FONT)
    T(cx, y0 + 0.82, "TOTAL PREMIUM IN THOSE FLOWS", ha="center", fontsize=9.5, color=TXT_DIM, fontweight="bold", fontfamily=HEADER_FONT)

    cx, y0 = tile(0, 1)
    tot = max(stats["call_premium"] + stats["put_premium"], 1)
    bx0, bx1 = cx - tw / 2 + 0.3, cx + tw / 2 - 0.3
    cw = (bx1 - bx0) * stats["call_premium"] / tot
    T(cx, y0 + 0.13, f"{stats['calls']} calls  ·  {stats['puts']} puts", ha="center", fontsize=17, color=TXT_LIGHT, fontweight="bold", fontfamily=HEADER_FONT)
    ax.add_patch(plt.Rectangle((bx0, y0 + 0.66), cw, 0.14, facecolor=CALL_HUE, edgecolor="none"))
    ax.add_patch(plt.Rectangle((bx0 + cw, y0 + 0.66), (bx1 - bx0) - cw, 0.14, facecolor=PUT_HUE, edgecolor="none"))
    T(cx, y0 + 0.90, f"BY PREMIUM  {stats['call_premium'] / tot * 100:.0f}% CALLS / {stats['put_premium'] / tot * 100:.0f}% PUTS",
      ha="center", fontsize=9, color=TXT_DIM, fontweight="bold", fontfamily=HEADER_FONT)

    cx, y0 = tile(1, 1)
    mh, ml = stats["median_high_pct"], stats["median_low_pct"]
    if mh is not None:
        T(cx - 0.12, y0 + 0.17, _fmt_pct(mh), ha="right", fontsize=24, color=UP, fontweight="bold", fontfamily=HEADER_FONT)
        T(cx, y0 + 0.17, "/", ha="center", fontsize=24, color=GRAY, fontfamily=HEADER_FONT)
        T(cx + 0.12, y0 + 0.17, _fmt_pct(ml), ha="left", fontsize=24, color=DOWN, fontweight="bold", fontfamily=HEADER_FONT)
    T(cx, y0 + 0.82, "TYPICAL HIGH / LOW AFTER PUBLISH", ha="center", fontsize=9.5, color=TXT_DIM, fontweight="bold", fontfamily=HEADER_FONT)

    y = ty + 2 * th + tg + 0.40
    # flows by day --------------------------------------------------------------
    T(LEFT, y, "FLOWS PUBLISHED BY DAY", fontsize=12, color=TXT_LIGHT, fontweight="bold", fontfamily=HEADER_FONT)
    y += 0.50
    max_n = max((len(v) for v in by_day.values()), default=1)
    bar_x0, bar_max = LEFT + 1.55, 2.9
    for d in sorted(by_day):
        fs = by_day[d]
        T(LEFT, y, d.strftime("%a %b %d"), fontsize=12, color=TXT_LIGHT, fontfamily=DATA_FONT)
        ax.add_patch(plt.Rectangle((bar_x0, y + 0.03), max(len(fs) / max_n * bar_max, 0.05), 0.24, facecolor=CALL_HUE, edgecolor="none", alpha=0.85))
        T(bar_x0 + len(fs) / max_n * bar_max + 0.12, y, f"{len(fs)} · {_fmt_prem(sum(f['premium'] for f in fs))}",
          fontsize=11.5, color=TXT_DIM, fontfamily=DATA_FONT)
        y += 0.52
    y += 0.30
    # where contracts stood ------------------------------------------------------
    T(LEFT, y, "WHERE THE CONTRACTS STOOD AT WEEK’S END", fontsize=12, color=TXT_LIGHT, fontweight="bold", fontfamily=HEADER_FONT)
    y += 0.50
    max_c = max((c for _, c, _ in buckets), default=1) or 1
    bar_x0, bar_max = LEFT + 3.05, 2.6
    for label, count, color in buckets:
        T(LEFT, y, label, fontsize=11.5, color=TXT_LIGHT, fontfamily=HEADER_FONT)
        if count:
            ax.add_patch(plt.Rectangle((bar_x0, y + 0.03), count / max_c * bar_max, 0.24, facecolor=color, edgecolor="none", alpha=0.9))
        T(bar_x0 + (count / max_c * bar_max if count else 0) + 0.12, y, str(count), fontsize=12, color=TXT_DIM, fontweight="bold", fontfamily=DATA_FONT)
        y += 0.52
    if nodata:
        T(LEFT, y + 0.02, f"+ {len(nodata)} flow(s) with no trades since being published", fontsize=10.5, color=GRAY, fontfamily=HEADER_FONT)

    _footer(T, ax, H, f"Summary · flow-by-flow detail on the next {pages - 1} page{'s' if pages != 2 else ''}" if pages > 1 else "Summary")
    fig.savefig(out_path, facecolor=DARK_BG, dpi=DPI)
    plt.close(fig)


def render_detail_page(recap: dict[str, Any], flows: list[dict], page_no: int, pages: int, out_path: str) -> None:
    head = 1.55
    H = head + len(flows) * ROW_H + 0.85
    fig, ax, T = _canvas(H)
    T(LEFT, 0.28, "WEEK IN FLOW", fontsize=15, color=TXT_LIGHT, fontweight="bold", fontfamily=HEADER_FONT)
    T(LEFT, 0.62, _range(recap), fontsize=10.5, color=TXT_DIM, fontfamily=HEADER_FONT)
    T(RIGHT, 0.28, f"Page {page_no} of {pages}", ha="right", fontsize=11, color=TXT_DIM, fontfamily=HEADER_FONT)

    SP_X0, SP_X1 = LEFT + 0.22, LEFT + 3.25
    COL = [LEFT + 4.05, LEFT + 5.25, LEFT + 6.45]
    ly = 1.00
    ax.plot([SP_X0, SP_X0 + 0.28], [ly + 0.07, ly + 0.07], color=GRAY, linewidth=1, linestyle=(0, (3, 2)))
    T(SP_X0 + 0.34, ly - 0.02, "price when published", fontsize=8.8, color=TXT_DIM, fontfamily=HEADER_FONT)
    T(SP_X0, ly + 0.24, "PRICE PATH SINCE FLOW PUBLISHED", fontsize=8.5, color=GRAY, fontweight="bold", fontfamily=HEADER_FONT)
    for cx, lab in zip(COL, ("HIGH", "LOW", "LAST")):
        T(cx, ly + 0.24, lab, ha="center", fontsize=9, color=GRAY, fontweight="bold", fontfamily=HEADER_FONT)
    ax.plot([LEFT, RIGHT], [head - 0.12, head - 0.12], color=PANEL_BORDER, linewidth=1)

    y = head
    for f in flows:
        _draw_row(ax, T, f, y, SP_X0, SP_X1, COL)
        y += ROW_H
    _footer(T, ax, H, f"Page {page_no} of {pages}")
    fig.savefig(out_path, facecolor=DARK_BG, dpi=DPI)
    plt.close(fig)


def _draw_row(ax, T, f, y, SP_X0, SP_X1, COL):
    ax.add_patch(FancyBboxPatch((LEFT, y), RIGHT - LEFT, ROW_H - 0.14, boxstyle="round,pad=0,rounding_size=0.07",
                                linewidth=0, facecolor=PANEL_BG, zorder=0))
    cp = f["put_call"] or "?"
    hue = CALL_HUE if cp == "CALL" else PUT_HUE if cp == "PUT" else GRAY
    ax.plot([LEFT + 0.02, LEFT + 0.02], [y + 0.08, y + ROW_H - 0.22], color=hue, linewidth=3.5, solid_capstyle="butt")
    k = _fmt_strike(f["strike"]) if f.get("strike") is not None else "?"
    exp = f["expiry"].strftime("%b %d") if f.get("expiry") else "?"
    T(LEFT + 0.22, y + 0.10, f"${f['ticker']}", fontsize=16, color=TXT_LIGHT, fontweight="bold", fontfamily=DATA_FONT)
    T(LEFT + 1.55, y + 0.14, f"{cp} {k} · {exp}", fontsize=12.5, color=hue, fontweight="bold", fontfamily=DATA_FONT)
    T(RIGHT - 0.18, y + 0.14, f"{_fmt_prem(f['premium'])} flow", ha="right", fontsize=12.5, color=TXT_LIGHT, fontweight="bold", fontfamily=DATA_FONT)
    sub = f"{_clock(f['alerted_at'])}"
    if f.get("alert_px") is not None:
        sub += f" · {_fmt_px(f['alert_px'])} when published"
    T(LEFT + 0.22, y + 0.47, sub, fontsize=10.5, color=TXT_DIM, fontfamily=DATA_FONT)
    if f.get("spot_chg_pct") is not None:
        T(RIGHT - 0.18, y + 0.47, f"stock {f['spot_chg_pct']:+.1f}%", ha="right", fontsize=10.5, color=GRAY, fontfamily=DATA_FONT)

    if not f["ok"]:
        T((LEFT + RIGHT) / 2, y + 0.90, f.get("reason") or "no contract data", ha="center", fontsize=10.5, color=GRAY, fontfamily=DATA_FONT)
        return

    pts = f["path_pct"]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    lo_v, hi_v = min(min(ys), -12.0), max(max(ys), 12.0)
    pad = (hi_v - lo_v) * 0.08
    lo_v, hi_v = lo_v - pad, hi_v + pad
    top, bot = y + 0.82, y + ROW_H - 0.28
    xmax = max(xs[-1], 1.0)
    mx = lambda x: SP_X0 + (x / xmax) * (SP_X1 - SP_X0)
    my = lambda v: bot - (v - lo_v) / (hi_v - lo_v) * (bot - top)
    base_y = my(0.0)
    ax.plot([SP_X0, SP_X1], [base_y, base_y], color=GRAY, linewidth=0.9, linestyle=(0, (3, 2)), zorder=2)
    sx, sy = [mx(xs[0])], [my(ys[0])]
    for (x0_, y0_), (x1_, y1_) in zip(pts, pts[1:]):
        sx += [mx(x1_), mx(x1_)]
        sy += [my(y0_), my(y1_)]
    color = UP if ys[-1] >= 0 else DOWN
    ax.fill_between(sx, sy, base_y, color=color, alpha=0.17, linewidth=0, zorder=2)
    ax.plot(sx, sy, color=color, linewidth=2.0, zorder=3, solid_joinstyle="miter")
    hi_i, lo_i = int(np.argmax(ys)), int(np.argmin(ys))
    ax.scatter([mx(xs[hi_i])], [my(ys[hi_i])], s=26, color=UP, zorder=4, edgecolors=DARK_BG, linewidths=0.8)
    ax.scatter([mx(xs[lo_i])], [my(ys[lo_i])], s=26, color=DOWN, zorder=4, edgecolors=DARK_BG, linewidths=0.8)

    vals = [(f["high_pct"], f["high_px"]), (f["low_pct"], f["low_px"]), (f["last_pct"], f["last_px"])]
    for i, (cx, (pc, px)) in enumerate(zip(COL, vals)):
        c = UP if pc > 0.05 else DOWN if pc < -0.05 else TXT_DIM
        T(cx, y + 0.84, _fmt_pct(pc), ha="center", fontsize=15, color=c, fontweight="bold", fontfamily=DATA_FONT)
        sub = _fmt_px(px)
        if i == 2 and f["expired"]:
            sub = "expired " + sub
        T(cx, y + 1.12, sub, ha="center", fontsize=9.5, color=TXT_DIM, fontfamily=DATA_FONT)


def render_weekly_recap_cards(recap: dict[str, Any], out_dir: str) -> list[str]:
    """Render the summary page + detail pages (ROWS_PER_PAGE flows each, chronological).
    Returns the PNG paths in posting order."""
    os.makedirs(out_dir, exist_ok=True)
    flows = sorted(recap["flows"], key=lambda f: f["alerted_at"])
    chunks = [flows[i:i + ROWS_PER_PAGE] for i in range(0, len(flows), ROWS_PER_PAGE)]
    pages = 1 + len(chunks)
    paths = [os.path.join(out_dir, "recap_00_summary.png")]
    render_summary_card(recap, paths[0], pages)
    for n, chunk in enumerate(chunks, start=1):
        pth = os.path.join(out_dir, f"recap_{n:02d}_flows.png")
        render_detail_page(recap, chunk, n + 1, pages, pth)
        paths.append(pth)
    return paths


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

async def post_weekly_recap(webhook_url: str, message: str) -> bool:
    """DEPRECATED plain-text poster, kept only so an old import can't crash the service."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(webhook_url, json={"content": message})
    return resp.status_code in (200, 204)


async def post_weekly_recap_image(webhook_url: str, image_path: str, caption: str) -> bool:
    """Posts one rendered card + a short caption (kept for compatibility)."""
    return await post_weekly_recap_images(webhook_url, [image_path], caption)


async def post_weekly_recap_images(webhook_url: str, image_paths: list[str], caption: str) -> bool:
    """Posts the cards in order, up to 10 per Discord message (caption on the first message)."""
    ok = True
    async with httpx.AsyncClient(timeout=60.0) as client:
        for n in range(0, len(image_paths), 10):
            batch = image_paths[n:n + 10]
            handles = [open(p, "rb") for p in batch]
            try:
                files = {f"files[{i}]": (os.path.basename(p), h, "image/png") for i, (p, h) in enumerate(zip(batch, handles))}
                content = caption if n == 0 else "*Week in Flow — continued*"
                resp = await client.post(webhook_url, data={"content": content}, files=files)
            finally:
                for h in handles:
                    h.close()
            ok = ok and resp.status_code in (200, 204)
    return ok
