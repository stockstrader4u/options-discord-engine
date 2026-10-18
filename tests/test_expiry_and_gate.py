"""
Tests for: Standard-tier flows are not published, the setup tag ("Lotto") is gone from cards, and flow results are
posted only after the contract has expired (selection, marking, pricing, rendering). No network, no Discord, and
SQLite only -- the DB tests force db._POSTGRES off so a DATABASE_URL in .env can never be touched.
"""
import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Card text: no "Lotto" tag
# ---------------------------------------------------------------------------

def _card(trade_style_label):
    from models import FlowAlert
    from enrichment import enrich_alert
    from classifier import classify_alert
    from formatter import format_alert, format_plain_text
    a = FlowAlert(ticker="NVDA", contract="NVDA 2026-10-23 220C", premium=150000, sentiment="bullish", flow_type="sweep",
                  dte_bucket="weeklies", spot_price=214.72, volume=3000, open_interest=1000, contract_price=0.53)
    e = enrich_alert(a)
    c = classify_alert(a, e)
    c.trade_style.label = trade_style_label
    return format_plain_text(format_alert(a, e, c, 80, [], conviction_label="High"))


def test_lotto_and_unknown_cards_have_no_setup_tag_line(monkeypatch):
    import enrichment
    monkeypatch.setattr(enrichment, "_real_rvol", lambda t: (1.0, "normal"))
    for label in ("lotto", "unknown"):
        card = _card(label)
        assert "Lotto" not in card and "Standard" not in card and "🏷️" not in card
        assert "Conviction: High" in card          # the rest of the card is intact


def test_swing_cards_still_show_the_tag(monkeypatch):
    import enrichment
    monkeypatch.setattr(enrichment, "_real_rvol", lambda t: (1.0, "normal"))
    assert "🏷️ **Swing**" in _card("swing")


# ---------------------------------------------------------------------------
# Standard tier is not published
# ---------------------------------------------------------------------------

def _item(ticker, vol, oi, strike):
    exp = (datetime.now(timezone.utc).date() + timedelta(days=7)).strftime("%Y-%m-%dT00:00:00")
    return {"ticker": ticker, "moneyNess": "OTM", "spot_Price": 100.0, "strike_Price": strike, "expriation_Date": exp,
            "price_Of_Contract": 2.0, "volume_When_Traded": vol, "size_Of_Trade": 500, "open_Interest_When_Traded": oi,
            "total_Option_Premium_For_Trade": 300000, "put_Or_Call": "CALL", "sweep_Or_Block": "SWEEP",
            "implied_Bought_Or_Sold": "BOUGHT", "interpreted_Conviction": "HIGH",
            "transaction_DateTime": "2026-10-16T12:30:00", "is_Repeating_Options_Trade_Today": False}


@pytest.fixture
def pipeline(monkeypatch):
    import enrichment
    import main as m
    monkeypatch.setattr(enrichment, "_real_rvol", lambda t: (1.0, "normal"))
    monkeypatch.setattr(m, "compute_levels", lambda *a, **k: None)
    monkeypatch.setattr(m, "DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setattr(m, "market_closed_reason", lambda *a, **k: None)
    monkeypatch.setattr(m, "alert_published_within_window", lambda alert: False)
    monkeypatch.setattr(m, "jarvis_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(m.db_module, "count_same_day_published_prints", lambda *a: 0)
    monkeypatch.setattr(m.db_module, "get_recent_published_premiums", lambda *a: [])
    log = {"posts": [], "saved": [], "conviction": []}

    async def fake_post(msg):
        log["posts"].append(msg)
        return True
    monkeypatch.setattr(m, "post_to_discord", fake_post)
    monkeypatch.setattr(m, "save_alert", lambda alert, score: None)
    monkeypatch.setattr(m, "save_flow_event_row", lambda **kw: log["saved"].append(kw["ticker"]) or 1)
    monkeypatch.setattr(m, "save_classification_row", lambda *a: None)
    monkeypatch.setattr(m, "create_outcome_row", lambda **kw: None)
    monkeypatch.setattr(m.db_module, "update_flow_event_conviction",
                        lambda eid, jc, label, detail: log["conviction"].append((jc, label)))
    return m, log


def _run(m, items, monkeypatch):
    async def fake_fetch(ticker):
        return items
    monkeypatch.setattr(m, "fetch_jarvis_flow", fake_fetch)
    return asyncio.run(m.process_jarvis_ticker("NVDA"))


def test_flow_below_baseline_is_skipped_and_baseline_flow_is_posted(pipeline, monkeypatch):
    m, log = pipeline
    good = _item("NVDA", vol=3000, oi=1000, strike=105)      # vol > OI  -> baseline met (High or Very High)
    weak = _item("NVDA", vol=500, oi=1000, strike=110)       # vol < OI  -> Standard
    result = _run(m, [good, weak], monkeypatch)
    assert result["posted"] == 1 and result["skipped_standard"] == 1
    assert log["saved"] == ["NVDA"] and len(log["posts"]) == 1
    assert "$105C" in log["posts"][0] and "$110C" not in log["posts"][0]
    assert "Standard" not in log["posts"][0] and "Lotto" not in log["posts"][0]
    assert log["conviction"][0][1] in ("High", "Very High")


def test_a_skipped_standard_flow_is_not_logged_so_a_later_stronger_print_can_still_post(pipeline, monkeypatch):
    m, log = pipeline
    result = _run(m, [_item("NVDA", vol=500, oi=1000, strike=110)], monkeypatch)
    assert result["posted"] == 0 and log["saved"] == [] and log["posts"] == []


# ---------------------------------------------------------------------------
# Expiry-based results
# ---------------------------------------------------------------------------

@pytest.fixture
def sqlite_db(monkeypatch, tmp_path):
    import db
    monkeypatch.setattr(db, "_POSTGRES", False)                     # never touch a DATABASE_URL from .env
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(db, "_conviction_schema_ready", False)
    db.init_all_tables()
    db.add_premium_column_migration()
    db.add_vol_oi_columns_migration()
    db.ensure_conviction_schema()
    assert not db.is_postgres()
    return db


def _flow(db, tag, ticker, expiry, rule_version, ingested="2026-10-20 14:00:00", strike=100.0, label="High"):
    fid = db.save_flow_event_row(
        alert_hash=tag, ticker=ticker, contract=f"{ticker} {expiry} {strike:g}C", premium=200000, sentiment="bullish",
        source="flow", dte_bucket="weeklies", flow_type="sweep", catalyst=None, levels=None, note="x", score=80,
        was_published=True, enrichment=SimpleNamespace(
            dte=3, expiry_date=expiry, strike=strike, put_call="call", spot_price=99.0, moneyness_pct=1.0,
            moneyness_tier="otm", premium_tier="large", rvol=1.0, rvol_label="normal", is_lotto=False,
            is_near_expiry=False, structure_notes=[]))
    with db.db_cursor() as (conn, cur):
        cur.execute("UPDATE flow_events SET ingested_at = ?, conviction_label = ?, conviction_rule_version = ? WHERE id = ?",
                    (ingested, label, rule_version, fid))
    return fid


def test_only_expired_unposted_rule_based_flows_are_due(sqlite_db):
    db = sqlite_db
    due = _flow(db, "a", "AMD", "2026-10-23", 1)
    _flow(db, "b", "NVDA", "2026-10-30", 1)                 # not expired yet
    _flow(db, "c", "TSLA", "2026-10-23", None)              # published before the rule-based tiers: never posted
    rows = db.get_unposted_expired_flows("2026-10-23", "2026-10-01 00:00:00")
    assert [r["id"] for r in rows] == [due]
    assert db.get_unposted_expired_flows("2026-10-22", "2026-10-01 00:00:00") == []     # the day before: nothing due
    assert db.get_unposted_expired_flows("2026-10-23", "2026-10-21 00:00:00") == []     # outside the catch-up window


def _bars(day_et, base):
    """5-minute option bars for one session: (ts, open, high, low, close)."""
    out, px = [], base
    for i in range(78):
        ts = datetime(day_et.year, day_et.month, day_et.day, 9, 30, tzinfo=ET) + timedelta(minutes=5 * i)
        px = max(0.05, px * (1.012 if i < 30 else 0.985))
        out.append((ts, px, px * 1.03, px * 0.97, px))
    return out


def test_expiry_results_are_priced_stored_rendered_posted_once(sqlite_db, monkeypatch, tmp_path):
    import weekly_recap as w
    db = sqlite_db
    f1 = _flow(db, "a", "AMD", "2026-10-23", 1)
    f2 = _flow(db, "b", "NVDA", "2026-10-23", 1, ingested="2026-10-21 15:00:00", strike=105.0, label="Very High")
    monkeypatch.setattr(w, "alpaca_configured", lambda: True)
    bars = {w.occ_symbol("AMD", date(2026, 10, 23), "call", 100.0): _bars(date(2026, 10, 20), 2.0) + _bars(date(2026, 10, 23), 2.5),
            w.occ_symbol("NVDA", date(2026, 10, 23), "call", 105.0): _bars(date(2026, 10, 21), 1.0) + _bars(date(2026, 10, 23), 1.2)}
    monkeypatch.setattr(w, "fetch_option_bars", lambda syms, start, end: bars)
    monkeypatch.setattr(w, "fetch_stock_daily_closes",
                        lambda tickers, s, e: {t: {date(2026, 10, 23): 101.0} for t in tickers})
    now = datetime(2026, 10, 23, 16, 45, tzinfo=ET)

    groups = w.build_expiry_results("", now_et=now)
    assert len(groups) == 1 and groups[0]["expiry"] == date(2026, 10, 23)
    g = groups[0]
    assert {f["flow_event_id"] for f in g["flows"]} == {f1, f2} and all(f["expired"] and f["ok"] for f in g["flows"])
    assert "Expiry Results" in g["caption"] and "2 flows" in g["caption"]

    with db.db_cursor() as (conn, cur):                      # outcomes are stored BEFORE anything is posted
        cur.execute("SELECT flow_event_id, bars_ok, result_posted_at FROM flow_option_outcomes ORDER BY 1")
        assert [tuple(r) for r in cur.fetchall()] == [(f1, 1, None), (f2, 1, None)]

    sc = g["scorecard"]                                         # High vs Very High: this expiry, and all settled flows
    assert sc["Very High"]["day"]["n"] == 1 and sc["High"]["day"]["n"] == 1
    assert sc["Very High"]["since_live"]["n"] == 1 and sc["High"]["since_live"]["n"] == 1
    for tier in ("Very High", "High"):
        assert sc[tier]["day"]["pct_up_last"] in (0.0, 100.0)    # one flow each: win rate is all-or-nothing

    paths = w.render_expiry_cards(g, str(tmp_path / "cards"))
    assert len(paths) == 2 and paths[0].endswith("_00_scorecard.png")      # scorecard first, then the flow cards
    assert all(os.path.getsize(p) > 10_000 for p in paths)

    db.mark_results_posted([f1, f2])
    assert w.build_expiry_results("", now_et=now) == []      # posted exactly once
    with db.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) FROM flow_option_outcomes WHERE result_posted_at IS NOT NULL")
        assert cur.fetchone()[0] == 2


def test_nothing_is_due_before_the_close_on_expiry_day(sqlite_db, monkeypatch):
    import weekly_recap as w
    _flow(sqlite_db, "a", "AMD", "2026-10-23", 1)
    monkeypatch.setattr(w, "alpaca_configured", lambda: True)
    monkeypatch.setattr(w, "fetch_option_bars", lambda *a: {})
    monkeypatch.setattr(w, "fetch_stock_daily_closes", lambda *a: {})
    assert w.build_expiry_results("", now_et=datetime(2026, 10, 23, 11, 0, tzinfo=ET)) == []


def test_expiry_job_posts_each_group_and_marks_only_after_a_successful_post(sqlite_db, monkeypatch):
    import main as m
    import weekly_recap as w
    f1 = _flow(sqlite_db, "a", "AMD", "2026-10-23", 1)
    monkeypatch.setattr(m, "DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setattr(m, "alpaca_configured", lambda: True)
    flow = {"flow_event_id": f1, "expired": True, "ok": True, "premium": 1, "expiry": date(2026, 10, 23)}
    group = {"expiry": date(2026, 10, 23), "flows": [flow], "caption": "c", "title": "EXPIRY RESULTS", "subtitle": "s"}
    monkeypatch.setattr(m, "build_expiry_results", lambda key: [group])
    monkeypatch.setattr(m, "render_expiry_cards", lambda g, d: ["x.png"])
    marked = []
    monkeypatch.setattr(m, "mark_results_posted", lambda ids: marked.append(list(ids)))

    async def fail(*a): return False
    async def ok(*a): return True
    monkeypatch.setattr(m, "post_weekly_recap_images", fail)
    asyncio.run(m.expiry_results_job())
    assert marked == []                                        # failed post: retried at the next run
    monkeypatch.setattr(m, "post_weekly_recap_images", ok)
    asyncio.run(m.expiry_results_job())
    assert marked == [[f1]]


def test_weekly_recap_job_is_not_scheduled_by_default(monkeypatch):
    import main as m
    assert m.WEEKLY_RECAP_ENABLED is False


def test_scorecard_cumulative_counts_only_settled_contracts(sqlite_db):
    import weekly_recap as w
    db = sqlite_db
    f1 = _flow(db, "a", "AMD", "2026-10-23", 1, label="Very High")
    f2 = _flow(db, "b", "NVDA", "2026-10-30", 1, label="Very High")
    base = dict(alert_hash="x", ticker="T", contract="c", put_call="CALL", strike=1.0, expiry_date="2026-10-23",
                alerted_at=datetime(2026, 10, 20, 14, tzinfo=timezone.utc), alert_px=1., high_px=2., low_px=.5, last_px=1.2,
                high_pct=100., low_pct=-50., last_pct=20., hit_50_before_minus_50="yes", time_to_peak_min=5., bars_ok=True)
    db.upsert_flow_option_outcomes([dict(base, flow_event_id=f1, expired=True), dict(base, flow_event_id=f2, expired=False)])
    card = w.build_expiry_scorecard([])
    assert card["Very High"]["since_live"]["n"] == 1          # the still-open contract is not counted
    assert card["High"]["since_live"]["n"] == 0
