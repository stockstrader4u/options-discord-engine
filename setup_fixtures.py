"""
setup_fixtures.py -- deterministic fake data for tests and the dry run (no network, no secrets).
Five setups shaped like main()'s `selected` (one clear uptrend call, one downtrend put, one counter-trend call, ...),
plus synthetic session-aligned hourly bars and an options map.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")


def weekdays_back(end: dt.date, n: int) -> list[dt.date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return out[::-1]


def make_hourly(seed: int, start_px: float, drift: float, vol: float, session_day: dt.date, sessions: int = 30,
                base_volume: float = 40_000) -> pd.DataFrame:
    """Hourly candles 9:30..15:30 (the last is a half-hour one), `drift` per bar, `vol` per-bar stdev, deterministic."""
    rng = np.random.default_rng(seed)
    rows, px = [], start_px
    for day in weekdays_back(session_day, sessions):
        for hh, mm in ((9, 30), (10, 30), (11, 30), (12, 30), (13, 30), (14, 30), (15, 30)):
            ts = pd.Timestamp(dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET))
            o = px * (1 + rng.normal(0, vol * .4)) if mm == 30 and hh == 9 else px
            c = o * (1 + drift + rng.normal(0, vol))
            h = max(o, c) * (1 + abs(rng.normal(0, vol * .5)))
            l = min(o, c) * (1 - abs(rng.normal(0, vol * .5)))
            u = base_volume * (1.8 if hh in (9, 15) else 1.0) * (0.5 if (hh, mm) == (15, 30) else 1) * (1 + rng.normal(0, .25))
            rows.append((ts, o, h, l, c, max(u, 1000)))
            px = c
    d = pd.DataFrame(rows, columns=["t", "Open", "High", "Low", "Close", "Volume"]).set_index("t")
    return d


def option_map_fixture(spot: float, expiry: dt.date, step: float) -> dict:
    return {"expiry": expiry.isoformat(), "call_wall": (round(spot * 1.035 / step) * step, 18400), "put_wall": (round(spot * .965 / step) * step, 15200),
            "max_pain": round(spot * 1.008 / step) * step, "expected_move": {"strike": round(spot / step) * step, "straddle": spot * .045},
            "oi_date": (expiry - dt.timedelta(days=14)).isoformat()}


def _setup(ticker, direction, pattern, px, strike, expiry, dte, flow, iv_rv, analyst, atr_pct, rank_i):
    is_call = direction == "CALL"
    atr = px * atr_pct
    move = atr * max(1.0, (max(dte, 1) / 5) ** .5)
    sgn = 1 if is_call else -1
    return {"ticker": ticker, "direction": direction, "pattern": pattern, "current_price": px, "strike": strike,
            "next_expiry": expiry.strftime("%b %d"), "expiry_iso": expiry.isoformat(), "dte": dte, "flow": flow, "iv_rv_ratio": iv_rv,
            "analyst_target": analyst, "entry_low": round(px * .995, 2), "entry_high": round(px * 1.005, 2),
            "stop": round(px - sgn * atr * .75, 2), "target1": round(px + sgn * move, 2), "target2": round(px + sgn * move * 2, 2),
            "time_pressure": {"dte": dte, "move_needed_pct": round((strike - px) / px * 100, 1)}, "company_name": ticker, "_rank": rank_i}


def fixture_setups(session_day: dt.date | None = None):
    """-> (selected, bars {ticker: DataFrame}, spy DataFrame, opts {ticker: map}, bets {ticker: dict|None}, earn {ticker: iso|None}, session_day)"""
    session_day = session_day or weekdays_back(dt.date(2026, 10, 9), 1)[0]
    exp = session_day + dt.timedelta(days=14)
    spec = [  # ticker, dir, pattern, start, drift, vol, strike, flow, iv_rv, analyst, atr_pct
        ("AXTI", "CALL", "higher lows", 21.0, .0016, .007, 24.0, {"bias": "Bullish", "premium": 2_400_000, "call_pct": 82}, .9, "Analyst target $26.50 (+11.0% from current), consensus: Buy", .04),
        ("NVDA", "PUT", "lower highs", 188.0, -.0009, .004, 172.5, {"bias": "Bearish", "premium": 5_100_000, "call_pct": 31}, 1.1, "Analyst target $225.00 (+18.4% from current), consensus: Strong Buy", .028),
        ("UPST", "CALL", "V-recovery", 52.0, -.0012, .009, 58.0, {"bias": "Bullish", "premium": 900_000, "call_pct": 71}, 1.3, "Analyst target $44.00 (-15.2% from current), consensus: Hold", .05),
        ("SMCI", "CALL", "higher lows", 41.0, .0004, .008, 43.0, {"bias": "Bullish", "premium": 1_700_000, "call_pct": 64}, .7, "Not provided", .045),
        ("ORCL", "PUT", "breakdown", 276.0, -.0003, .005, 262.5, {"bias": "Bearish", "premium": 3_300_000, "call_pct": 38}, 1.7, "Analyst target $330.00 (+19.6% from current), consensus: Buy", .03),
    ]
    selected, bars, opts, bets, earn = [], {}, {}, {}, {}
    for i, (t, d, pat, px0, drift, vol, strike, flow, ivrv, analyst, atrp) in enumerate(spec):
        df = make_hourly(100 + i, px0, drift, vol, session_day)
        px = round(float(df.Close.iloc[-1]), 2)
        strike_adj = round(px * (1 + (strike / px0 - 1)), 1)
        c = _setup(t, d, pat, px, strike_adj, exp, 14, flow, ivrv, analyst, atrp, i)
        selected.append(c)
        bars[t] = df
        step = 0.5 if px < 100 else 2.5 if px < 300 else 5
        opts[t] = option_map_fixture(px, exp, step) if i != 3 else None           # one ticker without an options map (degrades)
        kind = "calls" if d == "CALL" else "puts"
        bets[t] = ({"strike": strike_adj, "kind": kind, "side": "bought", "amount": int(flow["premium"] * .28), "order": "sweep",
                    "expiry": exp.isoformat(), "date": session_day.isoformat()} if i != 2 else None)
        earn[t] = (exp + dt.timedelta(days=9 + i)).isoformat() if i % 2 == 0 else None
    spy = make_hourly(7, 660.0, .0002, .0025, session_day, base_volume=900_000)
    return selected, bars, spy, opts, bets, earn, session_day
