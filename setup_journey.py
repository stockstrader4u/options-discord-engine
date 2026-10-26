"""
setup_journey.py -- the option contract's journey for a nightly trade idea, from entry to expiry.

ENTRY is the moment the stock first trades inside the idea's entry zone (first regular-hours 5-minute
stock bar on or after the publish date that overlaps the zone). From there we follow the suggested
CONTRACT's own traded price (Alpaca 5-minute option bars):

    price at entry = last traded contract price at or before the entry moment
    HIGH / LOW     = highest / lowest traded price after entry
    LAST           = settlement value at expiry

VERDICT:
    WIN  = the contract traded at least +WIN_GAIN above its entry price at some point after entry
    LOSS = it never got there

Same result shape as weekly_recap.compute_flow_result(), so weekly_recap.render_detail_page() draws it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from weekly_recap import (DATA_DELAY, ET, _clock, _paged_bars, _parse_bar, _utc_str,
                          fetch_option_bars, fetch_stock_daily_closes, occ_symbol, trading_x)

WIN_GAIN = 0.20
STOCK_FEED = "sip"
MAX_ENTRY_GAP = timedelta(minutes=30)   # first contract trade must come this soon after entry to price it
BARS_LOOKBACK = timedelta(days=7)    # enough to find the last trade before a weekend/holiday entry
WIN_COLOR, LOSS_COLOR = "#6ea8ff", "#f0a04b"     # weekly_recap's UP / DOWN


def _when(ts: datetime) -> str:
    h12 = ts.hour % 12 or 12
    return f"{ts:%a %b} {ts.day}  {h12}:{ts.minute:02d} {'AM' if ts.hour < 12 else 'PM'} ET"


def _in_session(ts: datetime) -> bool:
    return 9 * 60 + 30 <= ts.hour * 60 + ts.minute < 16 * 60


def fetch_stock_5m(tickers: list[str], start: date, end: date) -> dict[str, list[tuple]]:
    """{ticker: [(ts_et, o, h, l, c), ...]} regular-hours 5-minute bars. SIP (full market) is free once the
    data is 15+ minutes old; IEX alone is thin and misses zone touches."""
    out: dict[str, list[tuple]] = {}
    for i in range(0, len(tickers), 25):
        raw = _paged_bars("/v2/stocks/bars", {
            "symbols": ",".join(tickers[i:i + 25]), "timeframe": "5Min", "limit": 10000, "feed": STOCK_FEED,
            "adjustment": "split", "sort": "asc",
            "start": _utc_str(datetime(start.year, start.month, start.day, tzinfo=ET)),
            "end": _utc_str(min(datetime(end.year, end.month, end.day, tzinfo=ET) + timedelta(days=1),
                                datetime.now(timezone.utc) - DATA_DELAY))})
        for sym, rows in raw.items():
            out[sym] = [b for b in map(_parse_bar, rows) if _in_session(b[0])]
    return out


def find_entry(row: dict[str, Any], stock_bars: list[tuple]) -> datetime:
    """First stock bar on/after the publish date overlapping the entry zone; if the IEX feed shows no
    touch (it is thin), the open of the day the daily-bar grading found."""
    for b in stock_bars:
        if b[0].date() >= row["publish_date"] and b[3] <= row["entry_high"] and b[2] >= row["entry_low"]:
            return b[0]
    d = (row.get("grade") or {}).get("entry_date") or row["publish_date"]
    return datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)


def compute_setup_result(row: dict[str, Any], stock_bars: list[tuple], opt_bars: list[tuple] | None,
                         closes: dict[date, float] | None) -> dict[str, Any]:
    """row: ticker, direction, strike, expiry_date, publish_date, entry_low/high, stop, target1/2, id,
    optional grade {entry_date}."""
    put_call = row["direction"].upper()
    strike, expiry = float(row["strike"]), row["expiry_date"]
    entry_ts = find_entry(row, stock_bars)
    sk = lambda v: f"{float(v):g}"
    res: dict[str, Any] = {
        "ticker": row["ticker"], "put_call": put_call, "strike": strike, "expiry": expiry,
        "alerted_at": entry_ts, "ok": False, "reason": None, "expired": True, "premium": 0,
        "spot_chg_pct": None, "setup_id": row["id"], "status": "no_option_data", "px_label": "at entry",
        "detail": (f"zone {sk(row['entry_low'])}–{sk(row['entry_high'])}  ·  stop {sk(row['stop'])}"
                   f"  ·  T1 {sk(row['target1'])}  ·  T2 {sk(row['target2'])}"),
    }
    closes = closes or {}
    spot_entry = next((b[1] for b in stock_bars if b[0] >= entry_ts), None)   # stock price at the entry bar's start
    res["spot_px"] = spot_entry
    if spot_entry and closes.get(expiry):
        res["spot_chg_pct"] = None     # card shows the contract, not the stock move

    if not opt_bars:
        res["reason"] = "no option trades found"
        return res
    # contract price at the entry moment, from the entry day only (an overnight gap must not be priced off
    # yesterday's close): the open of the bar starting at entry, else the last trade before it today,
    # else the first trade after it today
    today_bars = [b for b in opt_bars if b[0].date() == entry_ts.date()]
    at = next((b for b in today_bars if b[0] == entry_ts), None)
    prior = [b for b in today_bars if b[0] < entry_ts]
    after = [b for b in opt_bars if b[0] >= entry_ts]
    if at:
        entry_px = at[1]
    elif prior:
        entry_px = prior[-1][4]
    elif after and after[0][0].date() == entry_ts.date() and after[0][0] - entry_ts <= MAX_ENTRY_GAP:
        entry_px = after[0][1]
    elif after and after[0][0].date() == entry_ts.date():
        # the contract had not traded by the time the stock triggered, and there is no quote history to price
        # it from: show the idea, but do not score it
        spot = f"${spot_entry:,.2f}" if spot_entry else "n/a"
        res.update(status="not_scored", right_label="NOT SCORED", right_color="#8a93a8",
                   reason="No contract trade within 30 min of the trigger, so not counted",
                   sub_left=f"Triggered {_when(entry_ts)}  ·  stock {spot}  ·  contract had not traded")
        return res
    else:
        res["reason"] = "contract had not traded at entry"
        return res
    if not after:
        res["alert_px"] = entry_px
        res["reason"] = "no trades after entry"
        return res

    hi_bar = max(after, key=lambda b: b[2])
    lo_bar = min(after, key=lambda b: b[3])
    high_px, low_px = max(hi_bar[2], entry_px), min(lo_bar[3], entry_px)
    path = [(0.0, entry_px)] + [(max(trading_x(b[0], entry_ts), 0.0), b[4]) for b in after]
    close = closes.get(expiry)
    if close is not None:
        last_px = max(0.0, close - strike) if put_call == "CALL" else max(0.0, strike - close)
        last_note = "settled"
    else:
        last_px, last_note = after[-1][4], "last trade"
    end_x = max(trading_x(datetime(expiry.year, expiry.month, expiry.day, 16, 0, tzinfo=ET), entry_ts), path[-1][0])
    path.append((end_x, last_px))

    # verdict: WIN if the contract traded at least +WIN_GAIN above its entry price at any point after entry
    won = high_px >= entry_px * (1 + WIN_GAIN)
    status, label, color = ("win", "WIN", WIN_COLOR) if won else ("loss", "LOSS", LOSS_COLOR)

    pct = lambda px: (px / entry_px - 1.0) * 100.0
    when = _when(entry_ts)
    spot = f"${spot_entry:,.2f}" if spot_entry else "n/a"
    res["sub_left"] = f"Triggered {when}  ·  stock {spot}  ·  contract ${entry_px:,.2f}"
    res.update(
        ok=True, status=status, right_label=label, right_color=color, alert_px=entry_px,
        high_px=high_px, low_px=low_px, last_px=last_px, last_note=last_note,
        high_pct=pct(high_px), low_pct=pct(low_px), last_pct=pct(last_px),
        path=path, path_pct=[(x, pct(y)) for x, y in path], end_x=end_x,
    )
    return res


def compute_setup_results(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Price every setup in one batch of Alpaca calls. Raises MarketDataError if Alpaca is unreachable,
    so the caller can retry instead of reporting empty journeys."""
    tickers = sorted({r["ticker"] for r in rows})
    first = min(r["publish_date"] for r in rows)
    last = max(r["expiry_date"] for r in rows)
    stock = fetch_stock_5m(tickers, first, last)
    syms = [occ_symbol(r["ticker"], r["expiry_date"], r["direction"], float(r["strike"])) for r in rows]
    start = datetime(first.year, first.month, first.day, tzinfo=ET) - BARS_LOOKBACK
    opt = fetch_option_bars(sorted(set(syms)), start, datetime.now(timezone.utc) - DATA_DELAY)
    closes = fetch_stock_daily_closes(tickers, first, last)
    return [compute_setup_result(r, stock.get(r["ticker"], []), opt.get(s), closes.get(r["ticker"]))
            for r, s in zip(rows, syms)]
