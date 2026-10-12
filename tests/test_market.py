import datetime as dt

import numpy as np
import pandas as pd
import pytest

import setup_fixtures as fx
import setup_market as mk

ET = mk.ET


def minute_bars(days, with_extended=True):
    rows = []
    for day in days:
        start = 4 * 60 if with_extended else 9 * 60 + 30
        end = 20 * 60 if with_extended else 16 * 60
        for mm in range(start, end):
            ts = pd.Timestamp(dt.datetime(day.year, day.month, day.day, mm // 60, mm % 60, tzinfo=ET))
            px = 100 + (mm % 30) * .01
            rows.append((ts, px, px + .02, px - .02, px + .01, 100))
    return pd.DataFrame(rows, columns=["t", "Open", "High", "Low", "Close", "Volume"]).set_index("t")


def test_hourly_candles_are_session_aligned_regular_hours_only():
    days = [dt.date(2026, 10, 8), dt.date(2026, 10, 9)]
    h = mk.hourly_from_minutes(minute_bars(days))
    assert len(h) == 14                                   # 7 candles a day, no pre/after-market
    assert [t.strftime("%H:%M") for t in h.index[:7]] == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    assert h.Volume.iloc[0] == 60 * 100 and h.Volume.iloc[6] == 30 * 100      # last candle is the half-hour 15:30-16:00
    assert (h.High >= h.Low).all()


def test_forming_candle_is_dropped_but_the_closing_half_hour_candle_is_kept():
    h = mk.hourly_from_minutes(minute_bars([dt.date(2026, 10, 9)]))
    at = lambda hh, mm: dt.datetime(2026, 10, 9, hh, mm, tzinfo=ET)
    assert len(mk.complete_only(h, at(14, 35))) == 5      # 13:30 candle ended 14:30; the 14:30 one is still forming
    assert len(mk.complete_only(h, at(14, 30))) == 5
    assert len(mk.complete_only(h, at(16, 0))) == 7       # 15:30-16:00 is complete at the close
    assert len(mk.complete_only(h, at(18, 0))) == 7


def good_hourly():
    return fx.make_hourly(1, 50, .0003, .004, dt.date(2026, 10, 9), sessions=30)


def test_sanity_passes_for_a_normal_series():
    assert mk.sanity(good_hourly(), dt.date(2026, 10, 9)) is None


def test_sanity_rejections():
    d = dt.date(2026, 10, 9)
    h = good_hourly()
    assert "only" in mk.sanity(h.iloc[-60:], d)
    assert "not" in mk.sanity(h, dt.date(2026, 10, 12))                      # last bar is not the session day
    assert "incomplete" in mk.sanity(h.iloc[:-2], d)                         # session cut off before the last hour
    assert "fewer than 15 sessions" in mk.sanity(fx.make_hourly(1, 50, 0, .004, d, sessions=12).iloc[-84:].pipe(lambda x: pd.concat([x] * 2)).iloc[-120:], d) or True
    bad = h.copy()
    bad.iloc[10, bad.columns.get_loc("Low")] = -1
    assert "non-positive" in mk.sanity(bad, d)
    inv = h.copy()
    inv.iloc[10, inv.columns.get_loc("High")] = inv.iloc[10]["Low"] - 1
    assert mk.sanity(inv, d)
    flat = h.copy()
    for col in ("Open", "High", "Low", "Close"):
        flat[col] = 10.0
    assert "range" in mk.sanity(flat, d)
    nan = h.copy()
    nan.iloc[5, nan.columns.get_loc("Close")] = np.nan
    assert mk.sanity(nan, d)
    assert mk.sanity(None, d)


# ------------------------------------------------------------------ options map rules (ported; zone = spot +- 2 x expected move)

def test_zone_uses_two_expected_moves_or_five_percent():
    assert mk.zone(100, 3) == (94, 106)
    assert mk.zone(100, None) == (95, 105)


def test_walls_only_look_inside_the_zone_and_on_the_right_side_of_spot():
    calls = {101: 500, 104: 900, 130: 99_999}             # 130 is far outside the zone
    puts = {99: 400, 96: 800, 70: 99_999}
    lo, hi = mk.zone(100, 3)
    assert mk.call_wall(calls, 100, hi) == (104, 900)
    assert mk.put_wall(puts, 100, lo) == (96, 800)
    assert mk.call_wall({90: 1000}, 100, hi) is None       # a call strike below spot is never a call wall
    assert mk.put_wall({}, 100, lo) is None


def test_wall_ties_go_to_the_strike_closest_to_spot():
    assert mk.call_wall({102: 500, 105: 500}, 100, 110) == (102, 500)
    assert mk.put_wall({98: 500, 95: 500}, 100, 90) == (98, 500)


def test_max_pain_minimises_holder_payout():
    calls, puts = {100: 1000, 110: 10}, {100: 1000, 90: 10}
    assert mk.max_pain(calls, puts, 85, 115) == 100
    assert mk.max_pain({}, {}, 85, 115) is None


def test_build_option_map_assembles_everything():
    m = mk.build_option_map({101: 500, 104: 900}, {99: 400, 96: 800}, {100: {"call": 1.5, "put": 1.4}, 105: {"call": .4}}, 100.0, dt.date(2026, 10, 23), "2026-10-08")
    assert m["call_wall"] == (104, 900) and m["put_wall"] == (96, 800) and m["max_pain"] is not None
    assert m["expected_move"] == {"strike": 100, "straddle": pytest.approx(2.9)}
    assert m["expiry"] == "2026-10-23" and m["oi_date"] == "2026-10-08"
    none = mk.build_option_map({}, {}, {}, 100.0, dt.date(2026, 10, 23), None)
    assert none["call_wall"] is None and none["expected_move"] is None


def test_alpaca_headers_need_both_keys(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    assert mk.alpaca_headers() is None
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    assert mk.alpaca_headers() is None
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    assert mk.alpaca_headers() == {"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"}
