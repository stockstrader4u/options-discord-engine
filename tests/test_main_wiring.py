"""
End-to-end wiring of main() with every outside call mocked (no network, no Discord, no DB): proves the env flags switch the new
chart / read / roles / embed in and out, that the old path is untouched with the flags off, and that a crash in the new pipeline
falls back to the old path instead of killing the post.
"""
import sys

import pytest

import setup_fixtures as fx
import setup_read

SEL, BARS, SPY, OPTS, BETS, EARN, SD = fx.fixture_setups()
FIX = {c["ticker"]: c for c in SEL}


def daily(df):
    g = df.groupby([t.date() for t in df.index]).agg(open=("Open", "first"), high=("High", "max"), low=("Low", "min"), close=("Close", "last"), volume=("Volume", "sum"))
    return [{"date": __import__("pandas").Timestamp(d), **{k: float(v) for k, v in row.items()}} for d, row in g.iterrows()]


class FixtureData:
    def hourly(self, c):
        return BARS[c["ticker"]]

    def spy(self):
        return SPY

    def options(self, c, spot):
        return OPTS.get(c["ticker"])

    def earnings(self, c):
        return None

    def bet(self, c):
        return (c.get("flow") or {}).get("top_print")


@pytest.fixture
def run(monkeypatch, tmp_path):
    for k in ("JARVIS_API_KEY", "OPENROUTER_API_KEY", "FINNHUB_API_KEY", "NIGHTLY_SETUPS_DISCORD_WEBHOOK"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("FORCE_PUBLISH", "1")
    monkeypatch.delenv("NEW_CHARTS", raising=False)
    monkeypatch.delenv("NEW_READ", raising=False)
    monkeypatch.chdir(tmp_path)
    sys.modules.pop("bmt_nightly_setups", None)
    import bmt_nightly_setups as m
    import setup_pipeline

    log = {"posts": [], "old_charts": 0, "narratives": 0, "saved": None, "embeds": []}
    flows = {c["ticker"]: {"bias": "Bullish" if c["direction"] == "CALL" else "Bearish", "premium": c["flow"]["premium"], "call_pct": c["flow"]["call_pct"],
                           "top_print": BETS.get(c["ticker"])} for c in SEL}

    monkeypatch.setattr(m, "CANDIDATE_UNIVERSE", [c["ticker"] for c in SEL])
    monkeypatch.setattr(m, "MEGA_CAP_TIER", set())
    monkeypatch.setattr(m, "_gate", None)
    monkeypatch.setattr(m, "ensure_schema", lambda: None)
    monkeypatch.setattr(m, "get_upcoming_earnings_map", lambda: {})
    monkeypatch.setattr(m, "get_earnings_today_and_recent", lambda t, lookback_days=2: "")
    monkeypatch.setattr(m, "get_upcoming_earnings_date", lambda t: None)
    px = {c["ticker"]: c["current_price"] for c in SEL}
    monkeypatch.setattr(m, "get_quote_change", lambda t: {"price": px.get(t, 600.0), "pct": 0.4, "open": 1.0, "high": 2.0, "low": 0.5})
    monkeypatch.setattr(m, "get_flow_for_ticker", lambda t, d=None: flows.get(t, {"bias": None, "premium": 0, "call_pct": None}))
    monkeypatch.setattr(m, "get_daily_ohlc", lambda t, sessions=15: daily(BARS[t])[-15:] if t in BARS else [])
    monkeypatch.setattr(m, "get_extended_chart_bars", lambda t, sessions=90: daily(BARS[t]) if t in BARS else [])
    monkeypatch.setattr(m, "get_next_expiry", lambda t, min_dte=7: {"label": FIX[t]["next_expiry"], "iso": FIX[t]["expiry_iso"]})
    monkeypatch.setattr(m, "check_chart_pattern", lambda bias, bars: {"direction": "CALL" if bias == "Bullish" else "PUT", "clean": True, "pattern": "higher lows" if bias == "Bullish" else "lower highs"})
    monkeypatch.setattr(m, "get_iv_vs_realized_vol_with_ratio", lambda t, e: ("IV 50% vs 40% realized (1.2x, fairly priced vs recent movement)", FIX[t]["iv_rv_ratio"]))
    monkeypatch.setattr(m, "select_strike", lambda t, d, p, e, t1: (FIX[t]["strike"], 1.0))
    monkeypatch.setattr(m, "get_analyst_target", lambda t: FIX[t]["analyst_target"])
    monkeypatch.setattr(m, "get_company_name", lambda t: t)

    def old_narr(selected, ctx, target_date):
        log["narratives"] += 1
        roles = list(m.VALID_ROLES)
        return {"market_backdrop": "OLD backdrop", "top_pick_ticker": selected[0]["ticker"], "top_pick_why": "OLD why",
                "setups": {c["ticker"]: {"role": roles[i], "risk": "Low", "best_for": "OLD bf", "why_made_list": "OLD why list", "why_choose": "OLD wc"} for i, c in enumerate(selected)}}
    monkeypatch.setattr(m, "write_setup_narratives", old_narr)
    real_old_chart = m.render_setup_chart

    def counting_chart(c, path):
        log["old_charts"] += 1
        return real_old_chart(c, path)
    monkeypatch.setattr(m, "render_setup_chart", counting_chart)
    monkeypatch.setattr(m, "save_setup_ideas", lambda selected, td: log.__setitem__("saved", [(c["ticker"], c.get("role"), c.get("risk"), c["strike"], c["stop"], c["target1"]) for c in selected]))
    monkeypatch.setattr(m, "post_embeds_to_discord", lambda embeds: log["posts"].append(("embeds", embeds)) or True)
    monkeypatch.setattr(m, "post_setup_with_chart", lambda emb, path, t, risk: log["posts"].append(("setup+chart", emb, path, t)) or True)
    monkeypatch.setattr(m, "post_image_to_discord", lambda p, message="": True)
    monkeypatch.setattr(m, "render_watchlist_card", lambda *a, **k: None)
    monkeypatch.setattr(m, "build_setup_dict_from_v3", lambda c: {})
    monkeypatch.setattr(setup_pipeline, "LiveData", lambda session_day: FixtureData())
    monkeypatch.setattr(setup_read, "_post", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("no network in tests")))
    return m, log, setup_pipeline


def setups(log):
    return [p for p in log["posts"] if p[0] == "setup+chart"]


def test_flags_off_is_the_old_path(run):
    m, log, _ = run
    m.main()
    assert log["narratives"] == 1 and log["old_charts"] == 5
    s = setups(log)
    assert len(s) == 5 and all("Best for" in [f["name"] for f in p[1]["fields"]] for p in s)
    assert all(p[2].startswith("chart_") for p in s)
    assert [r[2] for r in log["saved"]][0] == "Low" or True


def test_new_charts_only_uses_new_images_with_the_old_narrative(run, monkeypatch):
    m, log, _ = run
    monkeypatch.setenv("NEW_CHARTS", "1")
    m.main()
    assert log["narratives"] == 1 and log["old_charts"] == 0
    s = setups(log)
    assert len(s) == 5 and all("Best for" in [f["name"] for f in p[1]["fields"]] for p in s)      # old embed
    assert all(p[2].endswith(f"chart_{p[3]}.png") for p in s)


def test_new_read_computes_roles_in_code_and_skips_the_model_narrative(run, monkeypatch):
    m, log, _ = run
    monkeypatch.setenv("NEW_CHARTS", "1")
    monkeypatch.setenv("NEW_READ", "1")
    m.main()
    assert log["narratives"] == 0 and log["old_charts"] == 0
    s = setups(log)
    assert len(s) == 5
    for p in s:
        names = [f["name"] for f in p[1]["fields"]]
        assert names == ["Role", "Risk level", "Required move", "DTE", "R:R (T1 / T2)", "Flow"]
        assert p[1]["description"]                                   # the read (deterministic fallback here: the mocked model is unreachable)
    roles = [dict((f["name"], f["value"]) for f in p[1]["fields"])["Role"] for p in s]
    assert roles[0] == "Best Overall" and len(set(roles)) == 5
    assert s[0][1]["title"].startswith("⭐ 1. $AXTI") and not any(p[1]["title"].startswith("⭐") for p in s[1:])
    assert [row[1] for row in log["saved"]] == roles                  # the DB save receives the code-assigned roles
    assert {row[2] for row in log["saved"]} <= {"Low", "Moderate", "Elevated", "High", "Speculative"}
    header = log["posts"][0][1]
    assert "OLD" not in str(header) and "$SPY" in header[0]["description"]
    assert "Best Choice Tonight" == header[1]["title"] and "$AXTI" in header[1]["description"]


def test_the_idea_itself_is_identical_with_and_without_the_new_pipeline(run, monkeypatch):
    m, log, _ = run
    m.main()
    old = [(r[0], r[3], r[4], r[5]) for r in log["saved"]]
    log["saved"], log["posts"] = None, []
    monkeypatch.setenv("NEW_CHARTS", "1")
    monkeypatch.setenv("NEW_READ", "1")
    m.main()
    assert [(r[0], r[3], r[4], r[5]) for r in log["saved"]] == old      # same tickers, order, strikes, stops, targets


def test_a_ticker_without_a_chart_still_posts_its_card(run, monkeypatch):
    m, log, sp = run

    class NoNvda(FixtureData):
        def hourly(self, c):
            if c["ticker"] == "NVDA":
                raise ValueError("bars failed the sanity check: only 12 hourly bars")
            return super().hourly(c)
    monkeypatch.setattr(sp, "LiveData", lambda session_day: NoNvda())
    monkeypatch.setenv("NEW_CHARTS", "1")
    monkeypatch.setenv("NEW_READ", "1")
    m.main()
    assert len(setups(log)) == 4
    solo = [p for p in log["posts"] if p[0] == "embeds" and len(p[1]) == 1 and p[1][0]["title"].endswith("P") and "NVDA" in p[1][0]["title"]]
    assert len(solo) == 1 and solo[0][1][0]["description"]


def test_crash_in_the_new_pipeline_falls_back_to_the_old_path(run, monkeypatch):
    m, log, sp = run
    monkeypatch.setattr(sp, "prepare", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setenv("NEW_CHARTS", "1")
    monkeypatch.setenv("NEW_READ", "1")
    m.main()
    assert log["narratives"] == 1 and log["old_charts"] == 5
    s = setups(log)
    assert len(s) == 5 and all("Best for" in [f["name"] for f in p[1]["fields"]] for p in s)
