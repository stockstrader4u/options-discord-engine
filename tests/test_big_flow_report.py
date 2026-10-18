"""
Tests for the 3:30 PM ET $1M+ Flow Report Card: which prints qualify, the 300-row completeness guard, pricing and
rendering, and the scheduled job. No network, no Discord, no DB.
"""
import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import big_flow_report as b

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 16)
NOW = datetime(2026, 10, 16, 15, 30, tzinfo=ET)


def item(ticker="SPY", premium=1_500_000, pc="PUT", strike=700, expiry="2026-11-20T00:00:00", t="2026-10-16T13:09:00",
         money="OTM", side="BOUGHT", day_total=None):
    return {"ticker": ticker, "put_Or_Call": pc, "strike_Price": strike, "expriation_Date": expiry, "moneyNess": money,
            "implied_Bought_Or_Sold": side, "total_Option_Premium_For_Trade": premium, "transaction_DateTime": t,
            "total_Option_Premium_for_Same_Contract_Today": day_total if day_total is not None else premium,
            "sweep_Or_Block": "SWEEP", "spot_Price": 690.0, "price_Of_Contract": 3.0}


# ---------------------------------------------------------------------------
# What qualifies
# ---------------------------------------------------------------------------

def test_qualifies_requires_otm_bought_1m_same_day_before_cutoff():
    assert b.qualifies(item(), DAY, NOW)
    assert b.qualifies(item(premium=1_000_000), DAY, NOW)                    # exactly $1M counts
    assert not b.qualifies(item(premium=999_999), DAY, NOW)
    assert not b.qualifies(item(money="ITM"), DAY, NOW)
    assert not b.qualifies(item(money="ATM"), DAY, NOW)
    assert not b.qualifies(item(side="SOLD"), DAY, NOW)
    assert not b.qualifies(item(t="2026-10-15T13:09:00"), DAY, NOW)          # yesterday
    assert not b.qualifies(item(t="2026-10-16T15:45:00"), DAY, NOW)          # after the 3:30 cutoff
    assert not b.qualifies(item(t="garbage"), DAY, NOW)


def test_no_watchlist_and_no_expiry_filter():
    for tk, exp in (("IWM", "2026-10-16T00:00:00"), ("EWY", "2027-04-16T00:00:00"), ("SOXX", "2028-01-21T00:00:00"), ("ZZZZ", "2026-11-20T00:00:00")):
        assert b.qualifies(item(ticker=tk, expiry=exp), DAY, NOW)


def test_fetch_filters_sorts_and_dedupes(monkeypatch):
    rows = [item("A", 1_200_000), item("B", 9_000_000), item("C", 500_000), item("D", 2_000_000, money="ITM"),
            item("B", 9_000_000)]                                            # duplicate of the second
    calls = []

    async def fake(args, key):
        calls.append(args)
        return rows
    monkeypatch.setattr(b, "_fetch", fake)
    out = asyncio.run(b.fetch_big_flows(DAY, NOW, "key"))
    assert [i["ticker"] for i in out] == ["B", "A"]                          # biggest first, deduped, filtered
    assert len(calls) == 1
    a = calls[0]
    assert a["filter_by_moneyNess"] == "OTM" and a["filter_by_impliedAction"] == "BOUGHT" and a["sort_by"] == "Premium"
    assert a["filter_by_transaction_date_range_from"] == a["filter_by_transaction_date_range_to"] == "10/16/2026"
    assert "filter_by_Ticker" not in a                                      # market-wide


def test_cap_guard_splits_only_when_the_result_could_be_cut_off_above_1m(monkeypatch):
    calls = []

    def run(last_total):
        rows = [item(f"T{i}", 1_100_000 + i, day_total=5_000_000) for i in range(299)] + [item("LAST", 1_050_000, day_total=last_total)]
        calls.clear()

        async def fake(args, key):
            calls.append(args.get("filter_by_sweepOrBlock"))
            return rows if "filter_by_sweepOrBlock" not in args else [item(f"X{args['filter_by_sweepOrBlock']}", 3_000_000)]
        monkeypatch.setattr(b, "_fetch", fake)
        return asyncio.run(b.fetch_big_flows(DAY, NOW, "key"))

    out = run(last_total=400_000)                 # 300 rows but the last contract total is under $1M: provably complete
    assert calls == [None] and len(out) == 300
    out = run(last_total=2_000_000)               # 300 rows and still above $1M: could be truncated, so split
    assert calls == [None, "SWEEP", "BLOCK"] and {"XSWEEP", "XBLOCK"} <= {i["ticker"] for i in out}


def test_missing_api_key_raises():
    with pytest.raises(ValueError):
        asyncio.run(b.fetch_big_flows(DAY, NOW, ""))


# ---------------------------------------------------------------------------
# Pricing + rendering
# ---------------------------------------------------------------------------

def _bars(base, hours=(10, 11, 12, 13, 14)):
    out, px = [], base
    for h in hours:
        for m in range(0, 60, 5):
            ts = datetime(2026, 10, 16, h, m, tzinfo=ET)
            px = max(0.05, px * (1.01 if h < 12 else 0.995))
            out.append((ts, px, px * 1.02, px * 0.98, px))
    return out


def _priced(monkeypatch, n):
    import weekly_recap as w
    monkeypatch.setattr(w, "alpaca_configured", lambda: True)
    items = [item(f"T{i}", 3_000_000 - i * 50_000, pc="PUT" if i % 2 else "CALL", strike=100 + i,
                  t=f"2026-10-16T{9 + (i % 5):02d}:{10 + i % 40:02d}:00") for i in range(n)]
    bars = {w.occ_symbol(f"T{i}", date(2026, 11, 20), "put" if i % 2 else "call", float(100 + i)): _bars(2.0)
            for i in range(n) if i != 3}                                     # flow 3 has no contract trades
    monkeypatch.setattr(w, "fetch_option_bars", lambda syms, s, e: bars)
    monkeypatch.setattr(w, "fetch_stock_daily_closes", lambda tickers, s, e: {})
    return items


def test_report_prices_every_print_and_summarises(monkeypatch):
    items = _priced(monkeypatch, 8)
    report = b.build_big_flow_report(items, DAY, NOW)
    st = report["stats"]
    assert st["count"] == 8 and st["with_data"] == 7 and st["calls"] == 4 and st["puts"] == 4
    assert st["total_premium"] == sum(i["total_Option_Premium_For_Trade"] for i in items)
    assert [f["premium"] for f in report["flows"]] == sorted((f["premium"] for f in report["flows"]), reverse=True)
    unpriced = [f for f in report["flows"] if not f["ok"]]
    assert len(unpriced) == 1 and unpriced[0]["ticker"] == "T3"
    assert "$1M+ Flow Report Card" in report["caption"] and "Fri Oct 16" in report["caption"] and "3:30 PM ET" in report["caption"]


def test_report_pages_hold_every_ticker_with_the_summary_on_page_one(monkeypatch, tmp_path):
    report = b.build_big_flow_report(_priced(monkeypatch, 40), DAY, NOW)
    paths = b.render_report_cards(report, str(tmp_path))
    assert len(paths) >= 3 and all(os.path.getsize(p) > 10_000 for p in paths)
    pages = b.paginate(report["groups"])
    shown = [blk[1]["ticker"] for pg in pages for blk in pg if blk[0] == "band"]
    assert sorted(shown) == sorted(g["ticker"] for g in report["groups"])        # every ticker appears
    assert len(b.render_report_cards(b.build_big_flow_report(_priced(monkeypatch, 3), DAY, NOW), str(tmp_path / "small"))) == 1


def _flow(ticker, pc, strike, premium, minute, expiry=date(2026, 11, 20), high=10.0, ok=True):
    return {"ticker": ticker, "put_call": pc, "strike": strike, "expiry": expiry, "premium": premium, "ok": ok,
            "alerted_at": datetime(2026, 10, 16, 12, minute, tzinfo=ET), "high_pct": high, "low_pct": -5.0, "last_pct": 1.0}


def test_flows_group_by_ticker_biggest_total_first_with_a_call_put_split():
    flows = [_flow("SPY", "PUT", 700, 1_500_000, 1), _flow("IWM", "PUT", 258, 9_000_000, 2),
             _flow("SPY", "CALL", 720, 1_200_000, 3), _flow("SPY", "PUT", 690, 1_100_000, 4)]
    groups = b.group_by_ticker(flows)
    assert [g["ticker"] for g in groups] == ["IWM", "SPY"]                      # 9.0M vs 3.8M total
    spy = groups[1]
    assert spy["n_flows"] == 3 and spy["total_premium"] == 3_800_000
    assert spy["call_premium"] == 1_200_000 and spy["put_premium"] == 2_600_000
    assert [r["strike"] for r in spy["rows"]] == [700, 720, 690]                 # contracts by premium within the ticker


def test_every_print_keeps_its_own_row_and_the_rows_add_up_to_the_ticker_total():
    first = _flow("SOXX", "CALL", 555, 1_600_000, 32, high=12.0)
    later = _flow("SOXX", "CALL", 555, 1_200_000, 37, high=3.0)
    other = _flow("SOXX", "CALL", 600, 1_000_000, 40)
    g = b.group_by_ticker([later, other, first])[0]
    assert g["n_flows"] == 3 and len(g["rows"]) == 3                       # header count == number of rows
    assert sum(r["premium"] for r in g["rows"]) == g["total_premium"] == 3_800_000
    assert [(r["strike"], r["alerted_at"].minute) for r in g["rows"]] == [(555, 32), (555, 37), (600, 40)]
    assert g["rows"][0]["high_pct"] == 12.0 and g["rows"][1]["high_pct"] == 3.0   # each print keeps its own numbers


def test_on_a_real_style_report_every_group_adds_up(monkeypatch):
    report = b.build_big_flow_report(_priced(monkeypatch, 30), DAY, NOW)
    for g in report["groups"]:
        assert g["n_flows"] == len(g["rows"])
        assert sum(r["premium"] for r in g["rows"]) == g["total_premium"]
    assert sum(g["total_premium"] for g in report["groups"]) == report["stats"]["total_premium"]


def test_pagination_keeps_a_ticker_header_with_its_rows_and_continues_long_tickers():
    one = lambda t, n: {"ticker": t, "n_flows": n, "total_premium": n, "call_premium": n, "put_premium": 0,
                        "rows": [{"premium": 1_000_000 + i} for i in range(n)]}
    groups = [one(f"T{i}", 1) for i in range(5)] + [one("BIG", 30)] + [one(f"U{i}", 2) for i in range(6)]
    pages = b.paginate(groups)
    for pg in pages:
        for i, blk in enumerate(pg):
            if blk[0] == "band":
                assert i + 1 < len(pg) and pg[i + 1][0] == "row"                   # never a header alone at a page end
    big_bands = [blk for pg in pages for blk in pg if blk[0] == "band" and blk[1]["ticker"] == "BIG"]
    assert len(big_bands) >= 2 and big_bands[0][2] is False and all(bb[2] for bb in big_bands[1:])   # "(cont.)" on later pages
    rows = [blk for pg in pages for blk in pg if blk[0] == "row"]
    assert len(rows) == 5 + 30 + 12                                                  # nothing dropped


# ---------------------------------------------------------------------------
# The scheduled job
# ---------------------------------------------------------------------------

@pytest.fixture
def job(monkeypatch):
    import main as m
    monkeypatch.setattr(m, "DISCORD_WEBHOOK_URL", "https://example.invalid/main")
    monkeypatch.setattr(m, "BIG_FLOW_WEBHOOK_URL", None)
    monkeypatch.setattr(m, "alpaca_configured", lambda: True)
    monkeypatch.setattr(m, "is_trading_day", lambda d: True)
    log = {"fetched": 0, "posted": []}

    async def fetch(day, cutoff, key):
        log["fetched"] += 1
        return [item()]
    monkeypatch.setattr(m, "fetch_big_flows", fetch)
    monkeypatch.setattr(m, "build_big_flow_report", lambda items, day, now, key: {
        "stats": {"count": 1, "with_data": 1, "total_premium": 1}, "caption": "cap", "flows": []})
    monkeypatch.setattr(m, "render_report_cards", lambda r, d: ["x.png"])

    async def post(webhook, paths, caption):
        log["posted"].append((webhook, paths, caption))
        return True
    monkeypatch.setattr(m, "post_weekly_recap_images", post)
    return m, log


def test_job_posts_to_the_main_webhook_by_default(job):
    m, log = job
    asyncio.run(m.big_flow_report_job())
    assert log["posted"] == [("https://example.invalid/main", ["x.png"], "cap")]


def test_job_uses_its_own_webhook_when_configured(job, monkeypatch):
    m, log = job
    monkeypatch.setattr(m, "BIG_FLOW_WEBHOOK_URL", "https://example.invalid/big")
    asyncio.run(m.big_flow_report_job())
    assert log["posted"][0][0] == "https://example.invalid/big"


def test_job_skips_non_trading_days_and_empty_days_and_missing_keys(job, monkeypatch):
    m, log = job
    monkeypatch.setattr(m, "is_trading_day", lambda d: False)
    asyncio.run(m.big_flow_report_job())
    assert log["fetched"] == 0 and log["posted"] == []                       # holiday: no Jarvis call at all
    monkeypatch.setattr(m, "is_trading_day", lambda d: True)

    async def none(day, cutoff, key):
        return []
    monkeypatch.setattr(m, "fetch_big_flows", none)
    asyncio.run(m.big_flow_report_job())
    assert log["posted"] == []                                              # nothing qualified: nothing posted
    monkeypatch.setattr(m, "alpaca_configured", lambda: False)
    asyncio.run(m.big_flow_report_job())
    assert log["posted"] == []


def test_job_survives_a_jarvis_failure(job, monkeypatch):
    m, log = job

    async def boom(day, cutoff, key):
        raise RuntimeError("JarvisFlow error")
    monkeypatch.setattr(m, "fetch_big_flows", boom)
    asyncio.run(m.big_flow_report_job())                                     # logged, never raises
    assert log["posted"] == []


# ---------------------------------------------------------------------------
# Entry price: the card's percentages are measured from the price the flow printed at
# ---------------------------------------------------------------------------

def test_percentages_are_measured_from_the_print_price_and_the_entry_is_kept():
    f = {"ok": True, "alert_px": 2.5, "high_px": 4.0, "low_px": 1.5, "last_px": 3.0,
         "high_pct": 60.0, "low_pct": -40.0, "last_pct": 20.0}
    b.rebase_to_print(f, 2.0)                                   # the flow actually printed at $2.00, not the bar close $2.50
    assert f["entry_px"] == 2.0 and f["alert_px"] == 2.0
    assert round(f["high_pct"], 6) == 100.0 and round(f["low_pct"], 6) == -25.0 and round(f["last_pct"], 6) == 50.0
    g = {"ok": True, "alert_px": 1.0, "high_px": 1.2, "low_px": 0.9, "last_px": 1.1, "high_pct": 20.0, "low_pct": -10.0, "last_pct": 10.0}
    b.rebase_to_print(g, 1.5)                                   # nothing traded above the print: high floors at the entry
    assert g["high_px"] == 1.5 and g["high_pct"] == 0.0 and round(g["low_pct"], 6) == -40.0
    h = {"ok": False, "alert_px": None}
    b.rebase_to_print(h, 3.25)                                  # no later trades, but the entry price is still shown
    assert h["entry_px"] == 3.25
    k = {"ok": True, "alert_px": 2.5, "high_px": 4.0, "low_px": 1.5, "last_px": 3.0, "high_pct": 60.0, "low_pct": -40.0, "last_pct": 20.0}
    b.rebase_to_print(k, None)                                  # no print price in the payload: keep the bar-based entry
    assert k["entry_px"] == 2.5 and round(k["high_pct"], 6) == 60.0


def test_report_rows_carry_the_print_price(monkeypatch):
    report = b.build_big_flow_report(_priced(monkeypatch, 4), DAY, NOW)
    assert all(f["entry_px"] == 3.0 for f in report["flows"])                   # item() prints at $3.00
    priced = [f for f in report["flows"] if f["ok"]]
    assert all(abs(f["last_pct"] - (f["last_px"] / 3.0 - 1) * 100) < 1e-6 for f in priced)
