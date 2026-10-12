import json
import os
import sys

import pytest

import setup_embed as se
import setup_fixtures as fx
import setup_pipeline as sp
import setup_roles as ro

SEL = fx.fixture_setups()[0]


def card(**over):
    c = dict(SEL[0], role="Best Overall", risk="Moderate", read="Buyers are in charge. Hold 27.83.", rr=(2.2, 4.5))
    c.update(over)
    return c


def test_embed_shape_and_limits():
    e = se.build_setup_embed_v2(card(), 1, True, 0x5865F2)
    assert e["title"].startswith("⭐ 1. $AXTI") and e["title"].endswith("C")
    assert e["description"] == "Buyers are in charge. Hold 27.83."
    assert [f["name"] for f in e["fields"]] == ["Role", "Risk level", "Required move", "DTE", "R:R (T1 / T2)", "Flow"]
    assert all(f["inline"] for f in e["fields"])
    assert dict((f["name"], f["value"]) for f in e["fields"])["R:R (T1 / T2)"] == "2.2 / 4.5"
    assert len(e["title"]) <= 256 and all(len(f["value"]) <= 1024 for f in e["fields"]) and len(e["fields"]) <= 25
    assert len(json.dumps(e)) < 6000


def test_no_star_for_other_setups_and_missing_read_has_no_description():
    e = se.build_setup_embed_v2(card(read=None), 2, False, 1)
    assert not e["title"].startswith("⭐") and "description" not in e


def test_clamp_keeps_hostile_input_inside_discord_limits():
    e = se.build_setup_embed_v2(card(read="x" * 9000, role="r" * 3000), 1, False, 1)
    assert len(e["description"]) <= 4096 and len(e["fields"][0]["value"]) <= 1024


def test_missing_data_shows_na_not_a_crash():
    c = card(flow={}, rr=(None, None), strike=None, time_pressure={})
    c["dte"] = None
    f = {x["name"]: x["value"] for x in se.card_fields(c)}
    assert f["Flow"] == "N/A" and f["R:R (T1 / T2)"] == "N/A" and f["Required move"] == "N/A" and f["DTE"] == "N/A"


def test_put_required_move_is_negative_and_flow_side_is_aligned():
    put = dict(SEL[1], role="x", risk="y", rr=(2.2, 4.5))
    f = {x["name"]: x["value"] for x in se.card_fields(put)}
    assert f["Required move"].startswith("-") and "puts" in f["Flow"]


def test_card_fallback_read_states_only_card_numbers():
    t = se.card_fallback_read(SEL[0])
    assert "calls" in t and "%" in t and "target" not in t.lower()
    assert se.card_fallback_read({"strike": 10, "current_price": 0, "flow": {}}) is None


# ------------------------------------------------------------------ pipeline (no network: fixture data source)

class FixtureData:
    def __init__(self, fail=()):
        _, self.bars, self._spy, self.opts, self.bets, self.earn, _ = fx.fixture_setups()
        self.fail = set(fail)

    def hourly(self, c):
        if c["ticker"] in self.fail:
            raise ValueError("bars failed the sanity check: only 12 hourly bars")
        return self.bars[c["ticker"]]

    def spy(self):
        return self._spy

    def options(self, c, spot):
        return self.opts.get(c["ticker"])

    def earnings(self, c):
        return self.earn.get(c["ticker"])

    def bet(self, c):
        return self.bets.get(c["ticker"])


CTX = {"SPY": {"price": 673.42, "pct": 0.41}, "QQQ": {"price": 611.08, "pct": 0.66}, "IWM": {"price": 247.9, "pct": -0.18}}


def test_prepare_sets_only_presentation_fields_and_never_touches_the_idea(tmp_path):
    sel, *_rest, sd = fx.fixture_setups()
    before = {c["ticker"]: {k: c[k] for k in ("direction", "strike", "entry_low", "entry_high", "stop", "target1", "target2", "expiry_iso", "flow", "ticker")} for c in sel}
    res = sp.prepare(sel, CTX, sd, data=FixtureData(), out_dir=str(tmp_path), api_key=None)
    for c in sel:
        assert {k: c[k] for k in before[c["ticker"]]} == before[c["ticker"]]
        assert c["role"] and c["risk"] and c["read"] and os.path.exists(c["_new_chart_path"])
    assert [c["role"] for c in sel][0] == "Best Overall" and len({c["role"] for c in sel}) == 5
    assert res["top_pick_ticker"] == sel[0]["ticker"] and res["top_pick_why"] == sel[0]["read"]
    assert res["market_backdrop"].startswith("Markets closed with SPY")


def test_one_ticker_failing_its_bars_only_loses_its_chart(tmp_path):
    sel, *_rest, sd = fx.fixture_setups()
    sp.prepare(sel, CTX, sd, data=FixtureData(fail={"NVDA"}), out_dir=str(tmp_path), api_key=None)
    by = {c["ticker"]: c for c in sel}
    assert "_new_chart_path" not in by["NVDA"]
    assert by["NVDA"]["read"] and "puts" in by["NVDA"]["read"]                 # the card still gets a (card-numbers) read
    assert by["NVDA"]["read_source"] == "card-fallback"
    assert all("_new_chart_path" in c for t, c in by.items() if t != "NVDA")


def test_want_read_false_leaves_roles_and_reads_alone(tmp_path):
    sel, *_rest, sd = fx.fixture_setups()
    sp.prepare(sel, CTX, sd, data=FixtureData(), out_dir=str(tmp_path), want_read=False)
    assert all("role" not in c and "read" not in c and "_new_chart_path" in c for c in sel)


def test_flags(monkeypatch):
    monkeypatch.delenv("NEW_CHARTS", raising=False)
    monkeypatch.delenv("NEW_READ", raising=False)
    assert sp.flags() == (False, False)
    monkeypatch.setenv("NEW_CHARTS", "1")
    assert sp.flags() == (True, False)
    monkeypatch.setenv("NEW_READ", "1")
    assert sp.flags() == (True, True)
    monkeypatch.setenv("NEW_READ", "0")
    assert sp.flags() == (True, False)


def test_live_data_without_alpaca_keys_raises_so_the_chart_is_skipped(monkeypatch):
    import datetime as dt
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    ld = sp.LiveData(dt.date(2026, 10, 9))
    with pytest.raises(RuntimeError):
        ld.hourly({"ticker": "AAPL"})
    assert ld.options({"ticker": "AAPL", "expiry_iso": "2026-10-23"}, 100.0) is None


# ------------------------------------------------------------------ main(): the only change to flow data is an extra key

@pytest.fixture
def main_mod(monkeypatch):
    for k in ("JARVIS_API_KEY", "OPENROUTER_API_KEY", "FINNHUB_API_KEY", "NIGHTLY_SETUPS_DISCORD_WEBHOOK"):
        monkeypatch.setenv(k, "x")
    sys.modules.pop("bmt_nightly_setups", None)
    import bmt_nightly_setups as m
    return m


def _flow(*prints):
    return {"optionsFlow": [dict(ticker="ABC", put_Or_Call=pc, implied_Bought_Or_Sold="BOUGHT", moneyNess="OTM", strike_Price=k,
                                 expriation_Date="2026-10-23T00:00:00", sweep_Or_Block="SWEEP", transaction_DateTime="2026-10-09T15:59:56",
                                 total_Option_Premium_For_Trade=p) for pc, k, p in prints]}


def test_flow_selection_numbers_are_unchanged_and_top_print_is_the_largest_aligned(main_mod, monkeypatch):
    monkeypatch.setattr(main_mod, "call_jarvis", lambda tool, args={}: _flow(("CALL", 50, 60_000), ("CALL", 55, 200_000), ("PUT", 45, 90_000)))
    f = main_mod.get_flow_for_ticker("ABC", "10/09/2026")
    assert f["bias"] == "Bullish" and f["premium"] == 350_000 and f["call_pct"] == 74           # exactly the old computation
    assert f["top_print"] == {"strike": 55.0, "kind": "calls", "side": "bought", "amount": 200000, "order": "sweep", "expiry": "2026-10-23", "date": "2026-10-09"}


def test_top_print_is_on_the_put_side_for_bearish_flow_and_absent_for_neutral(main_mod, monkeypatch):
    monkeypatch.setattr(main_mod, "call_jarvis", lambda tool, args={}: _flow(("PUT", 45, 300_000), ("CALL", 55, 100_000)))
    f = main_mod.get_flow_for_ticker("ABC", "10/09/2026")
    assert f["bias"] == "Bearish" and f["top_print"]["kind"] == "puts" and f["top_print"]["strike"] == 45.0
    monkeypatch.setattr(main_mod, "call_jarvis", lambda tool, args={}: _flow(("PUT", 45, 100_000), ("CALL", 55, 100_000)))
    f = main_mod.get_flow_for_ticker("ABC", "10/09/2026")
    assert f["bias"] == "Neutral" and f["top_print"] is None


def test_malformed_print_never_breaks_the_flow_scan(main_mod, monkeypatch):
    bad = _flow(("CALL", 55, 200_000))
    del bad["optionsFlow"][0]["strike_Price"]
    monkeypatch.setattr(main_mod, "call_jarvis", lambda tool, args={}: bad)
    f = main_mod.get_flow_for_ticker("ABC", "10/09/2026")
    assert f["bias"] == "Bullish" and f["premium"] == 200_000 and f["top_print"] is None


def test_no_flow_still_returns_the_old_shape(main_mod, monkeypatch):
    monkeypatch.setattr(main_mod, "call_jarvis", lambda tool, args={}: None)
    assert main_mod.get_flow_for_ticker("ABC", "10/09/2026") == {"bias": None, "premium": 0, "call_pct": None}


def test_live_data_spy_is_fetched_once_and_cached_across_tickers(monkeypatch):
    import datetime as dt
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    calls = []
    spy = fx.fixture_setups()[2]
    monkeypatch.setattr(sp.mk, "get_hourly", lambda sess, h, sym, now: calls.append(sym) or spy)
    ld = sp.LiveData(dt.date(2026, 10, 9))
    assert ld.spy() is spy and ld.spy() is spy and ld.spy() is spy      # a DataFrame must never be compared to a string
    assert calls == ["SPY"]
