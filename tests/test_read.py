import json

import pytest

import setup_chart as sc
import setup_fixtures as fx
import setup_read as rd
import setup_roles as ro

FACTS = {"symbol": "ABC", "price": "100.00", "data_as_of": "Oct 09", "trend": "uptrend", "bias": "up", "ema34": "98.10", "ema50": "97.40",
         "price_vs_cloud": "+1.9%", "trend_momentum": "strengthening", "rsi": "62", "rsi_zone": "normal (30-70)",
         "next_level_above": "104.20 (+4.2%)", "first_level_below": "96.50 (-3.5%)", "call_wall": "105.00 (+5.0%)", "put_wall": "95.00 (-5.0%)",
         "max_pain": "101.00 (+1.0%), inside the expected range", "expected_move": "±4.50 (±4.5%) by Oct 23, i.e. 95.50 to 104.50",
         "typical_daily_move": "2.1%", "idea": "call at the 106 strike, expires Oct 23", "strike_price": "106", "days_to_expiry": "14",
         "strike_distance": "+6.0% from the price, about 2.9 days of normal movement", "idea_vs_chart": "agrees with the chart",
         "counter_trend": False, "options_flow": "$1.97M of calls bought in the last session (82% of the bought flow, $2.40M total)",
         "analyst_view": "analysts' average price goal is 90.00 (-10.0% from here, below the price), consensus hold"}
GOOD = "Buyers are in charge and the uptrend is strengthening, with calls bought heavily. The level to hold is 96.50 and the one to clear is 104.20."


def chk(text, facts=FACTS, lean="bullish"):
    return rd.check_read(text, facts, lean)


def test_good_read_passes():
    assert chk(GOOD) is None


@pytest.mark.parametrize("text,why", [
    ("", "empty"),
    ("Buyers are in charge and 100.00 is steady. " * 6, "too long"),
    ("Buyers lead. 96.50 matters. Hold it. Clear 104.20. Done.", "sentences"),
    ("Buyers are in charge near 104.25 and the uptrend is strong.", "number not in the data"),
    ("Buyers are in charge and the RSI is normal, so hold 96.50 and clear 104.20.", "jargon"),
    ("Buyers are in charge; you should hold 96.50 and clear 104.20 for the move.", "advice"),
    ("Buyers are in charge; the first target is 104.20 and 96.50 is the floor.", "advice"),
    ("Buyers are in charge, and I think 96.50 holds and 104.20 clears.", "AI"),
    ("Buyers are in charge and the uptrend is strengthening, with calls bought heavily on the day.", "level"),
    ("The chart leans bearish here with sellers in charge, so 96.50 is the level to hold.", "bearish on an up chart"),
])
def test_rejections(text, why):
    assert chk(text) is not None, why


def test_number_check_is_numeric_not_textual():
    assert chk("Buyers are in charge; hold 96.5 and clear 104.2 and the uptrend is strengthening.") is None   # 96.5 == 96.50


def test_field_names_and_underscores_rejected():
    assert "jargon" in chk("Buyers are in charge and next_level_above is 104.20, with 96.50 as the floor.")


def test_hold_a_level_above_price_and_clear_one_below_are_rejected():
    assert "above the price" in chk("Buyers are in charge and the uptrend is strengthening, so 104.20 is the level to hold.")
    assert "below the price" in chk("Buyers are in charge and the uptrend is strengthening, but it has to clear 96.50 first.")
    assert chk("Buyers are in charge and the uptrend is strengthening: hold 96.50, then clear 104.20.") is None
    assert "above the price" in chk("Buyers are in charge and the uptrend is strengthening; if it slips under 104.20 that is trouble.")


def test_lean_field_must_match_computed_bias():
    assert "lean field" in chk(GOOD, lean="bearish")
    assert "lean field" in chk(GOOD, lean="none")
    down = dict(FACTS, bias="down", trend="downtrend")
    assert rd.check_read("Sellers are in control and the downtrend is strengthening; the level to hold is 96.50 and to clear is 104.20.", down, "bearish") is None
    assert "bullish wording" in rd.check_read("The chart leans bullish even in a downtrend, with 96.50 the level to hold and 104.20 to clear.", down, "bearish")


def test_neutral_requires_no_lean_wording():
    neu = dict(FACTS, bias="neutral", trend="inside the cloud")
    assert rd.check_read("Nobody is winning: it is stuck between 96.50 and 104.20, so a clean break of either decides it.", neu, "none") is None
    assert "side" in rd.check_read("It leans bullish between 96.50 and 104.20, a clear edge for buyers here.", neu, "none")
    assert "no clear lean" in rd.check_read("Buyers have the edge between 96.50 and 104.20 and momentum is fine.", neu, "none")


def test_counter_trend_idea_must_say_so_and_agreeing_idea_must_not_claim_it():
    ct = dict(FACTS, bias="down", trend="downtrend", counter_trend=True, idea_vs_chart="counter-trend: a call while the chart shows a downtrend")
    spin = "Sellers are in control and the downtrend is strengthening; the level to hold is 96.50 and to clear is 104.20."
    honest = "Sellers are in control, so this call is a counter-trend idea; the level to hold is 96.50 and to clear is 104.20."
    assert "counter-trend" in rd.check_read(spin, ct, "bearish")
    assert rd.check_read(honest, ct, "bearish") is None
    assert "agrees" in chk("This is a counter-trend idea but buyers are in charge; hold 96.50 and clear 104.20 and the uptrend is strong.")


def test_analyst_view_may_be_mentioned_without_the_word_target():
    ok = "Buyers lead and the uptrend is strengthening, though analysts' price goal of 90.00 is below the price; the level to hold is 96.50."
    assert chk(ok) is None


def test_fallback_passes_the_same_checker_for_every_chart_state():
    for state, bias in [("uptrend", "up"), ("downtrend", "down"), ("pullback", "down"), ("bounce", "up"), ("inside the cloud", "neutral")]:
        for ct in (False, True):
            f = dict(FACTS, trend=state, bias=bias, counter_trend=ct, counter_kind="stretched" if bias == "neutral" else "against")
            if state == "inside the cloud":
                f.pop("price_vs_cloud")
            lean = {"up": "bullish", "down": "bearish", "neutral": "none"}[bias]
            text = rd.fallback_read(f)
            assert rd.check_read(text, f, lean) is None, (state, ct, text)


def test_fallback_without_levels_does_not_crash():
    f = {"symbol": "ABC", "trend": "uptrend", "bias": "up", "price": "100.00", "price_vs_cloud": "+1.0%"}
    assert "ABC" in rd.fallback_read(f)


def test_fixture_facts_and_fallbacks_validate_end_to_end():
    sel, bars, spy, opts, bets, earn, sd = fx.fixture_setups()
    seen_counter = False
    for c in sel:
        c["rr"] = ro.reward_risk(c)
        t = c["ticker"]
        m = sc.compute_metrics(c, bars[t], spy, opts[t], earn[t], bets[t], sd)
        f = sc.build_facts(m)
        seen_counter |= f["counter_trend"]
        lean = {"up": "bullish", "down": "bearish", "neutral": "none"}[f["bias"]]
        assert rd.check_read(rd.fallback_read(f), f, lean) is None
    assert seen_counter                      # UPST (a call in a downtrend) is in the fixture


# ------------------------------------------------------------------ model call (mocked, no network)

class Resp:
    def __init__(self, status=200, content=None, cost=0.0123):
        self.status_code, self._c, self._cost = status, content, cost

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return {"choices": [{"message": {"content": self._c}}], "usage": {"cost": self._cost}}


def script(monkeypatch, responses):
    calls = []

    def fake_post(api_key, body, deadline_s):
        calls.append(body)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(rd, "_post", fake_post)
    return calls


def jr(read, lean="bullish"):
    return json.dumps({"lean": lean, "read": read})


def test_ask_accepts_a_valid_reply_and_sums_cost(monkeypatch):
    calls = script(monkeypatch, [Resp(content=jr(GOOD))])
    r = rd.read_for_setup("k", "m", FACTS, b"png")
    assert r["source"] == "llm" and r["read"] == GOOD and r["cost"] == pytest.approx(0.0123)
    assert len(calls) == 1 and calls[0]["reasoning"] == {"effort": "low"} and calls[0]["model"] == "m"
    assert isinstance(calls[0]["messages"][0]["content"], list)            # image attached


def test_rejected_reply_is_retried_once_then_falls_back(monkeypatch):
    calls = script(monkeypatch, [Resp(content=jr("You should buy 104.25 now, bullish.")), Resp(content=jr("Still nonsense with 77.77."))])
    r = rd.read_for_setup("k", "m", FACTS, b"png")
    assert len(calls) == 2 and r["source"] == "fallback" and r["reason"] and r["cost"] == pytest.approx(0.0246)
    assert rd.check_read(r["read"], FACTS, "bullish") is None


def test_second_attempt_can_succeed(monkeypatch):
    script(monkeypatch, [Resp(content=jr("Buy now at 104.20.")), Resp(content=jr(GOOD))])
    assert rd.read_for_setup("k", "m", FACTS, None)["source"] == "llm"


def test_image_refused_second_attempt_drops_the_image(monkeypatch):
    calls = script(monkeypatch, [Resp(status=422), Resp(content=jr(GOOD))])
    r = rd.read_for_setup("k", "m", FACTS, b"png")
    assert r["source"] == "llm" and isinstance(calls[0]["messages"][0]["content"], list) and isinstance(calls[1]["messages"][0]["content"], str)


def test_network_failure_falls_back_and_never_raises(monkeypatch):
    script(monkeypatch, [ConnectionError("boom")])
    r = rd.read_for_setup("k", "m", FACTS, b"png")
    assert r["source"] == "fallback" and "call failed" in r["reason"] and r["read"]


def test_garbage_json_and_wrong_lean_field_are_rejected(monkeypatch):
    script(monkeypatch, [Resp(content="not json at all"), Resp(content=jr(GOOD, lean="bearish"))])
    r = rd.read_for_setup("k", "m", FACTS, None)
    assert r["source"] == "fallback"


def test_no_key_or_no_chart_facts_means_fallback_without_a_call(monkeypatch):
    calls = script(monkeypatch, [])
    assert rd.read_for_setup(None, "m", FACTS, None)["source"] == "fallback"
    r = rd.read_for_setup("k", "m", {}, None)
    assert r["read"] is None and calls == []


def test_run_reads_is_per_setup_and_one_failure_costs_one_card(monkeypatch):
    def fake(api_key, model, facts, png):
        if facts["symbol"] == "BAD":
            raise RuntimeError("x")
        return {"read": "ok " + facts["symbol"], "source": "llm", "cost": 0.01, "reason": None}
    monkeypatch.setattr(rd, "read_for_setup", fake)
    jobs = [{"ticker": t, "facts": dict(FACTS, symbol=t)} for t in ("AAA", "BAD", "CCC")]
    out = rd.run_reads(jobs, "k")
    assert out["AAA"]["read"] == "ok AAA" and out["CCC"]["read"] == "ok CCC"
    assert out["BAD"]["source"] == "fallback" and out["BAD"]["read"]


def test_run_reads_deadline_gives_fallback_for_a_hung_setup(monkeypatch):
    import time

    def slow(api_key, model, facts, png):
        time.sleep(5)
    monkeypatch.setattr(rd, "read_for_setup", slow)
    out = rd.run_reads([{"ticker": "AAA", "facts": FACTS}], "k", deadline_s=0.2)
    assert out["AAA"]["reason"] == "deadline" and out["AAA"]["read"]


def test_backdrop_validation_and_fallback():
    ctx = {"SPY": {"price": 673.42, "pct": 0.41}, "QQQ": {"price": 611.08, "pct": 0.66}, "IWM": {"price": 247.9, "pct": -0.18}}
    facts = rd.backdrop_facts(ctx)
    assert rd.check_backdrop("SPY gained 0.41% to 673.42 and QQQ rose 0.66% to 611.08 while small caps in IWM slipped 0.18%.", facts) is None
    assert rd.check_backdrop("SPY gained 0.99% and QQQ rose 0.66% to 611.08.", facts)
    assert rd.check_backdrop("SPY rose 0.41% and QQQ rose 0.66%, so buy the dip.", facts)
    assert rd.check_backdrop("SPY rose 0.41%.", facts)
    assert "SPY at 673.42 (+0.41%)" in rd.fallback_backdrop(ctx)
    assert rd.fallback_backdrop({}) == "Markets closed."


def test_clean_strips_markdown_and_mentions():
    assert rd.clean("**bold** @everyone `x`\nnext") == "bold everyone x next"


def test_counter_trend_fallbacks_explain_the_idea_in_plain_words():
    base = dict(FACTS, counter_trend=True, options_flow="$1.97M of calls bought in the last session (82% of the bought flow, $2.40M total)")
    call_down = dict(base, bias="down", trend="downtrend", counter_kind="against", counter_trend_story="x")
    t = rd.fallback_read(call_down)
    assert "counter-trend" in t and "still falling" in t and "$1.97M of calls" in t and "betting on a bounce" in t and "turns up first" in t
    assert rd.check_read(t, call_down, "bearish") is None
    put_up = dict(base, idea="put at the 90 strike", bias="up", trend="uptrend", counter_kind="against", options_flow="$3.52M of puts bought in the last session (69% of the bought flow, $5.10M total)")
    t = rd.fallback_read(put_up)
    assert "still rising" in t and "betting on a drop" in t and "slip under 96.50" in t
    assert rd.check_read(t, put_up, "bullish") is None
    st_put = dict(base, idea="put at the 90 strike", bias="down", trend="downtrend", counter_kind="stretched")
    t = rd.fallback_read(st_put)
    assert "chasing the move" in t and rd.check_read(t, st_put, "bearish") is None
    st_call = dict(base, bias="up", trend="uptrend", counter_kind="stretched")
    t = rd.fallback_read(st_call)
    assert "chasing the move" in t and rd.check_read(t, st_call, "bullish") is None
    no_flow = dict(call_down); no_flow.pop("options_flow")
    assert rd.check_read(rd.fallback_read(no_flow), no_flow, "bearish") is None
