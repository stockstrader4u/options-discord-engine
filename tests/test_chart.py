import json
import os

import pytest

import setup_chart as sc
import setup_fixtures as fx
import setup_roles as ro

SEL, BARS, SPY, OPTS, BETS, EARN, SD = fx.fixture_setups()
BY = {c["ticker"]: c for c in SEL}
for _c in SEL:
    _c["rr"] = ro.reward_risk(_c)


def metrics(t, **over):
    args = dict(df=BARS[t], spy=SPY, opts=OPTS[t], earn=EARN[t], bet=BETS[t])
    args.update(over)
    return sc.compute_metrics(BY[t], args["df"], args["spy"], args["opts"], args["earn"], args["bet"], SD)


def test_bias_and_state_for_fixture_charts():
    assert metrics("AXTI").bias == "up" and metrics("AXTI").state == "uptrend"
    assert metrics("NVDA").bias == "down"
    assert metrics("SMCI").bias in ("neutral", "up", "down")


def test_counter_trend_flag_follows_direction_vs_chart():
    up_call = metrics("AXTI")
    assert up_call.is_call and up_call.bias == "up" and up_call.counter_why is None
    down_call = metrics("UPST")                      # a CALL idea on a downtrend chart
    assert down_call.is_call and down_call.bias == "down" and "call while the chart shows" in down_call.counter_why
    down_put = metrics("NVDA")                       # a PUT on a downtrend agrees
    assert (not down_put.is_call) and down_put.counter_why is None
    f = sc.build_facts(down_call)
    assert f["counter_trend"] is True and f["idea_vs_chart"].startswith("counter-trend")
    assert sc.build_facts(up_call)["idea_vs_chart"] == "agrees with the chart"


def test_stretched_against_the_idea_is_flagged_counter_trend():
    # a PUT idea after a relentless slide (RSI <= 30): the chart agrees with the put, but the move is already stretched
    df = fx.make_hourly(5, 80.0, -.004, .002, SD)
    c = dict(BY["NVDA"])
    m = sc.compute_metrics(c, df, SPY, None, None, None, SD)
    assert m.bias == "down" and m.rv <= 30 and "put after price has already stretched down" in m.counter_why
    assert sc.build_facts(m)["counter_trend"] is True
    # and the mirror image: a CALL after a relentless rally (RSI >= 70)
    up = fx.make_hourly(6, 40.0, .004, .002, SD)
    m2 = sc.compute_metrics(dict(BY["AXTI"]), up, SPY, None, None, None, SD)
    assert m2.bias == "up" and m2.rv >= 70 and "call after price has already stretched up" in m2.counter_why


def test_trade_levels_widen_the_y_range_so_everything_is_visible():
    for t in BY:
        m = metrics(t)
        c = BY[t]
        for k in ("entry_low", "entry_high", "stop", "target1", "target2"):
            assert m.ymin <= c[k] <= m.ymax, (t, k)


def test_facts_contain_what_the_read_needs_and_nothing_empty():
    f = sc.build_facts(metrics("AXTI"))
    for k in ("symbol", "price", "trend", "bias", "price_vs_cloud", "rsi", "rsi_zone", "vs_SPY", "next_level_above", "first_level_below",
              "typical_daily_move", "typical_weekly_move", "strike_distance", "idea", "strike_price", "options_flow", "call_wall", "put_wall",
              "max_pain", "expected_move", "earnings", "analyst_view", "idea_vs_chart", "counter_trend"):
        assert k in f and f[k] not in (None, ""), k
    assert f["bias"] in ("up", "down", "neutral")
    assert "days of normal movement" in f["strike_distance"]
    assert f["earnings"].endswith("expiry") and "after" in f["earnings"]          # fixture earnings fall after the expiry
    assert "$1.97M of calls bought" in f["options_flow"] and "largest print" in f["options_flow"]


def test_facts_never_use_the_word_target_and_analyst_is_reworded():
    f = sc.build_facts(metrics("UPST"))
    assert "target" not in json.dumps(f).lower()
    c = {"analyst_target": "Analyst target $44.00 (-15.2% from current), consensus: Hold"}
    below = sc._analyst_fact(c, 50.0)
    assert "price goal is 44.00" in below and "below the price" in below and "-12.0%" in below and "consensus hold" in below
    assert "above the price" in sc._analyst_fact(c, 40.0)
    assert sc._analyst_fact({"analyst_target": "Not provided"}, 50.0) is None
    assert "consensus" not in sc._analyst_fact({"analyst_target": "Analyst target $44.00 (+1.0% from current), consensus: Not provided"}, 50.0)


def test_optional_parts_disappear_from_facts_when_missing():
    f = sc.build_facts(metrics("AXTI", opts=None, bet=None, earn=None, spy=None))
    for k in ("call_wall", "put_wall", "max_pain", "expected_move", "earnings", "vs_SPY"):
        assert k not in f
    assert "largest print" not in f["options_flow"]
    assert f["bias"] == "up"


def test_facts_levels_are_consistent_with_the_chart_levels():
    m = metrics("AXTI")
    f = sc.build_facts(m)
    above = min(p for p in m.prices if p > m.last)
    assert f["next_level_above"].startswith(f"{above:,.2f}")
    assert f["price"] == f"{m.last:,.2f}"


def test_notes_cover_the_idea_flow_and_counter_trend():
    names = [n for n, _ in sc.build_notes(metrics("UPST"))]
    assert {"Trend", "Momentum", "Key levels", "The idea", "Why this goes against the chart", "Options flow behind the pick", "Options map", "Max pain"} <= set(names)
    assert "Why this goes against the chart" not in [n for n, _ in sc.build_notes(metrics("AXTI"))]
    body = dict(sc.build_notes(metrics("UPST")))["Why this goes against the chart"]
    assert "counter-trend" in body and "still falling" in body and "betting on a bounce" in body and "$639K of calls" in body
    assert "turn up first" in body and "above" in body


def test_spread_keeps_labels_apart_and_inside_the_axes():
    tags = [[p, p, "x", "c", "k"] for p in (10.0, 10.01, 10.02, 10.03, 10.9, 11.0)]
    sc._spread(tags, 0.1, 9.9, 11.05)
    ys = sorted(t[1] for t in tags)
    assert all(b - a >= 0.0999 for a, b in zip(ys, ys[1:]))
    assert ys[0] >= 9.9 - 1e-9 and ys[-1] <= 11.05 + 1e-9


@pytest.mark.parametrize("ticker", list(BY))
def test_render_produces_a_png_under_the_discord_limit(tmp_path, ticker):
    out = sc.build_chart(BY[ticker], BARS[ticker], SPY, OPTS[ticker], EARN[ticker], BETS[ticker], SD, str(tmp_path / "c.png"))
    data = (tmp_path / "c.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) < 7 * 1024 * 1024
    assert out["facts"]["bias"] in ("up", "down", "neutral") and out["notes"]


def test_render_degrades_part_by_part(tmp_path):
    for kw in ({"spy": None}, {"opts": None}, {"bet": None}, {"earn": None}, {"spy": None, "opts": None, "bet": None, "earn": None}):
        args = dict(spy=SPY, opts=OPTS["NVDA"], bet=BETS["NVDA"], earn=EARN["NVDA"])
        args.update(kw)
        p = tmp_path / f"c{len(kw)}{'_'.join(kw)}.png"
        sc.build_chart(BY["NVDA"], BARS["NVDA"], args["spy"], args["opts"], args["earn"], args["bet"], SD, str(p))
        assert p.stat().st_size > 10_000


def test_render_without_rr_or_flow_still_works(tmp_path):
    c = dict(BY["AXTI"])
    c.pop("rr")
    c["flow"] = {}
    sc.build_chart(c, BARS["AXTI"], SPY, OPTS["AXTI"], None, None, SD, str(tmp_path / "x.png"))
    assert (tmp_path / "x.png").exists()


def test_flow_numbers_use_the_ideas_own_side():
    c = {"direction": "PUT", "flow": {"premium": 1_000_000, "call_pct": 30, "bias": "Bearish"}}
    fn = sc.flow_numbers(c)
    assert fn["kind"] == "puts" and fn["share"] == 70 and fn["aligned"] == 700_000
    assert sc.flow_numbers({"direction": "CALL", "flow": {}}) is None


def test_counter_trend_explainers_are_plain_for_every_kind():
    put_up = dict(BY["NVDA"], direction="PUT")                   # a put while the chart is rising
    df = fx.make_hourly(6, 40.0, .0015, .004, SD)
    m = sc.compute_metrics(put_up, df, SPY, None, None, None, SD)
    assert m.bias == "up" and m.counter_kind == "against"
    txt = sc.counter_trend_explainer(m, [50.0], [38.0])
    assert "still rising" in txt and "betting on a drop" in txt and "slip under 38.00" in txt and "turn down first" in txt
    assert sc.build_facts(m)["counter_kind"] == "against" and "betting on a drop" in sc.build_facts(m)["counter_trend_story"]
    up = fx.make_hourly(6, 40.0, .004, .002, SD)                 # stretched: a call after a relentless rally
    m2 = sc.compute_metrics(dict(BY["AXTI"]), up, SPY, None, None, None, SD)
    t2 = sc.counter_trend_explainer(m2, [], [])
    assert m2.counter_kind == "stretched" and "chasing" in t2 and "counter-trend" in t2
    for jargon in ("RSI", "EMA", "Fibonacci", "IV", "OTM"):
        assert jargon not in txt and jargon not in t2


def test_right_axis_tags_are_only_stop_targets_and_the_two_walls(monkeypatch, tmp_path):
    import matplotlib.pyplot as plt
    seen = {}
    real_close = plt.close
    monkeypatch.setattr(plt, "close", lambda fig=None: seen.setdefault("fig", fig))
    sc.build_chart(BY["NVDA"], BARS["NVDA"], SPY, OPTS["NVDA"], EARN["NVDA"], BETS["NVDA"], SD, str(tmp_path / "t.png"))
    real_close(seen["fig"])
    ax = seen["fig"].axes[0]
    tags = [t.get_text() for t in ax.texts if abs(t.get_position()[0] - (sc.N + 20.4)) < 1e-6]
    last_tag = f"{float(BARS['NVDA'].Close.iloc[-1]):,.2f}"
    assert last_tag in tags                                      # the dark last-price tag
    tags = [t for t in tags if t != last_tag]
    assert len(tags) == 5, tags
    assert sorted(tg.split()[0] + (" " + tg.split()[1] if tg.startswith(("Call", "Put")) else "") for tg in tags) == ["Call wall", "Put wall", "STOP", "T1", "T2"]
