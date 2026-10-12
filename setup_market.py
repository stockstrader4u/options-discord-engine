"""
setup_market.py -- market data for the nightly trade-idea charts (NEW_CHARTS=1).

Ported from bmt-live-dashboard/charts/market.py + options_map.py (read-only reference), adapted for this job:
  * PURE functions (no network, unit-tested): session-aligned hourly candles, the "still forming" drop, the sanity
    check, and the call wall / put wall / max pain / expected-move rules (zone = spot +- 2 x expected move; do not widen);
  * thin NETWORK functions (Alpaca 1-minute IEX bars, Alpaca option contracts + snapshots) using `requests`.

Differences from the reference, on purpose:
  * the options map is built for the idea's OWN expiry (c["expiry_iso"]), not "the next weekly";
  * the sanity check is keyed to the last COMPLETED SESSION (this job runs at 6pm ET, or on a holiday evening),
    not to "today";
  * no trading-calendar dependency: the caller passes the session day it already computed.

Secrets only ever go in headers. Every network function raises on failure; the caller decides what degrades
(a failed options map removes that part of the chart, failed bars skip the chart, never the post).
"""
from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ET = ZoneInfo("America/New_York")
DATA_URL = "https://data.alpaca.markets"
TRADING_URL = "https://paper-api.alpaca.markets"     # option contracts (open interest) live on the trading API; paper keys work

HISTORY_DAYS = 45            # calendar days of 1-minute bars (about 31 sessions, about 215 hourly candles)
MIN_BARS = 110               # 100 plotted + warm-up for the EMA 50
MIN_SESSIONS = 15

ZONE_MULT = 2.0
ZONE_FALLBACK_PCT = 0.05


def alpaca_headers() -> dict | None:
    key, sec = os.environ.get("ALPACA_API_KEY_ID", ""), os.environ.get("ALPACA_API_SECRET_KEY", "")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec} if key and sec else None


# ------------------------------------------------------------------------------------------------ bars (pure)

def hourly_from_minutes(minute: pd.DataFrame) -> pd.DataFrame:
    """Session-aligned hourly candles (9:30-10:30, 10:30-11:30, ...) from regular-session 1-minute bars.
    `minute` has a tz-aware (ET) DatetimeIndex and Open/High/Low/Close/Volume columns."""
    mins = minute.index.hour * 60 + minute.index.minute
    reg = minute[(mins >= 570) & (mins < 960)]
    h = reg.resample("60min", offset="30min").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
    return h[h.Volume > 0]


def candle_end(start: pd.Timestamp) -> pd.Timestamp:
    """When a candle that starts at `start` is complete: an hour later, never after the 4:00 PM close
    (the last candle of the day, 15:30-16:00, is a half-hour candle)."""
    return min(start + pd.Timedelta(minutes=60), start.normalize() + pd.Timedelta(hours=16))


def complete_only(hourly: pd.DataFrame, now: dt.datetime) -> pd.DataFrame:
    """Drop the hourly candle that is still forming (its end has not been reached yet)."""
    keep = [candle_end(t) <= pd.Timestamp(now) for t in hourly.index]
    return hourly[keep]


def sanity(hourly: pd.DataFrame, session_day: dt.date) -> str | None:
    """None when the series is fit to chart, else the reason it is not. `session_day` is the last completed session."""
    if hourly is None or len(hourly) < MIN_BARS:
        return f"only {0 if hourly is None else len(hourly)} hourly bars"
    last = hourly.index[-1]
    if last.date() != session_day:
        return f"last complete bar is {last.date()}, not {session_day}"
    if last.hour < 15:
        return f"last bar starts {last:%H:%M}, the session is incomplete"
    if len({t.date() for t in hourly.index}) < MIN_SESSIONS:
        return "fewer than 15 sessions of history"
    if (hourly[["Open", "High", "Low", "Close"]] <= 0).any().any() or (hourly.High < hourly.Low).any():
        return "non-positive or inverted prices"
    if hourly[["Open", "High", "Low", "Close", "Volume"]].isna().any().any():
        return "missing values"
    recent = hourly.iloc[-100:]
    if (float(recent.High.max()) - float(recent.Low.min())) / float(recent.Close.iloc[-1]) < 0.002:
        return "no meaningful price range"
    return None


# ------------------------------------------------------------------------------------------------ options rules (pure)

def zone(spot: float, expected_move: float | None) -> tuple[float, float]:
    half = ZONE_MULT * expected_move if expected_move else spot * ZONE_FALLBACK_PCT
    return spot - half, spot + half


def _pick(candidates: dict[float, int], spot: float):
    best = [(k, v) for k, v in candidates.items() if v > 0]
    if not best:
        return None
    return max(best, key=lambda t: (t[1], -abs(t[0] - spot)))


def call_wall(calls: dict[float, int], spot: float, hi: float):
    """(strike, open_interest) of the largest call open interest in [spot, hi], or None."""
    return _pick({k: v for k, v in calls.items() if spot <= k <= hi}, spot)


def put_wall(puts: dict[float, int], spot: float, lo: float):
    """(strike, open_interest) of the largest put open interest in [lo, spot], or None."""
    return _pick({k: v for k, v in puts.items() if lo <= k <= spot}, spot)


def max_pain(calls: dict[float, int], puts: dict[float, int], lo: float, hi: float):
    """Settlement price (a listed strike in [lo, hi]) minimising total option-holder payout, or None."""
    strikes = sorted(set(calls) | set(puts))
    cand = [k for k in strikes if lo <= k <= hi]
    if not cand:
        return None
    payout = {k: sum(calls.get(s, 0) * max(k - s, 0) + puts.get(s, 0) * max(s - k, 0) for s in strikes) for k in cand}
    return min(cand, key=lambda k: (payout[k], k))


def expected_move(call_mid: float, put_mid: float) -> float:
    """Options-implied move to expiry: the at-the-money straddle price."""
    return call_mid + put_mid


def build_option_map(calls: dict[float, int], puts: dict[float, int], mids: dict[float, dict], spot: float,
                     expiry: dt.date, oi_date: str | None) -> dict:
    """Pure assembly of the options map from open interest ({strike: oi}) and quote mids ({strike: {'call':, 'put':}})."""
    both = {s: v for s, v in mids.items() if "call" in v and "put" in v}
    em = None
    if both:
        k = min(both, key=lambda s: abs(s - spot))
        em = {"strike": k, "straddle": expected_move(both[k]["call"], both[k]["put"])}
    lo, hi = zone(spot, em["straddle"] if em else None)
    return {"expiry": expiry.isoformat(), "call_wall": call_wall(calls, spot, hi), "put_wall": put_wall(puts, spot, lo),
            "max_pain": max_pain(calls, puts, spot * .85, spot * 1.15), "expected_move": em, "oi_date": oi_date}


# ------------------------------------------------------------------------------------------------ network

def fetch_minute_bars(sess: requests.Session, headers: dict, symbol: str, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    rows, token = [], None
    for _ in range(10):
        p = {"symbols": symbol, "timeframe": "1Min", "start": start.astimezone(dt.timezone.utc).isoformat(),
             "end": end.astimezone(dt.timezone.utc).isoformat(), "feed": "iex", "limit": 10000, "adjustment": "raw"}
        if token:
            p["page_token"] = token
        r = sess.get(f"{DATA_URL}/v2/stocks/bars", headers=headers, params=p, timeout=60)
        r.raise_for_status()
        j = r.json()
        rows += (j.get("bars") or {}).get(symbol, [])
        token = j.get("next_page_token")
        if not token:
            break
    if not rows:
        raise ValueError("no minute bars")
    d = pd.DataFrame(rows)
    d.index = pd.to_datetime(d["t"], utc=True).dt.tz_convert(ET)
    return d.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})[["Open", "High", "Low", "Close", "Volume"]]


def get_hourly(sess, headers, symbol: str, now: dt.datetime) -> pd.DataFrame:
    minute = fetch_minute_bars(sess, headers, symbol, now - dt.timedelta(days=HISTORY_DAYS), now)
    return complete_only(hourly_from_minutes(minute), now)


def _contracts(sess, headers, symbol: str, expiry: dt.date) -> list[dict]:
    out, token = [], None
    for _ in range(10):
        p = {"underlying_symbols": symbol, "expiration_date": expiry.isoformat(), "limit": 1000}
        if token:
            p["page_token"] = token
        r = sess.get(f"{TRADING_URL}/v2/options/contracts", headers=headers, params=p, timeout=60)
        r.raise_for_status()
        j = r.json()
        out += j.get("option_contracts") or []
        token = j.get("next_page_token")
        if not token:
            break
    return out


def option_map(sess, headers, symbol: str, spot: float, expiry: dt.date, today: dt.date) -> dict | None:
    """Call wall, put wall, max pain and expected move for `expiry`, or None when there is no chain."""
    chain = _contracts(sess, headers, symbol, expiry)
    if not chain:
        return None
    calls = {float(c["strike_price"]): int(c.get("open_interest") or 0) for c in chain if c["type"] == "call"}
    puts = {float(c["strike_price"]): int(c.get("open_interest") or 0) for c in chain if c["type"] == "put"}
    r = sess.get(f"{DATA_URL}/v1beta1/options/snapshots/{symbol}", headers=headers, timeout=60,
                 params={"feed": "indicative", "limit": 1000, "expiration_date": expiry.isoformat(),
                         "strike_price_gte": spot * .97, "strike_price_lte": spot * 1.03})
    r.raise_for_status()
    mids: dict[float, dict] = {}
    for k, v in (r.json().get("snapshots") or {}).items():
        q = v.get("latestQuote") or {}
        if not q.get("bp") or not q.get("ap") or q["bp"] <= 0:
            continue
        mids.setdefault(int(k[-8:]) / 1000, {})["call" if k[len(symbol) + 6] == "C" else "put"] = (q["bp"] + q["ap"]) / 2
    oi_date = next((c.get("open_interest_date") for c in chain if c.get("open_interest_date")), None)
    return build_option_map(calls, puts, mids, spot, expiry, oi_date or today.isoformat())
