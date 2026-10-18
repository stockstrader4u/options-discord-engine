"""
Tests for the post-close $1M+ Flow Card (a flow card, not a performance card): which prints qualify, the 300-row
completeness guard, the options-specific figures (DTE, OTM, breakeven, volume vs open interest, running totals),
grouping by stock, the one-image render, the stock-price lookup, and the scheduled job.
No network, no Discord, no DB.
"""
import asyncio
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import big_flow_report as b

_REAL_FETCH_STOCK_PRICES = b.fetch_stock_prices      # saved before the autouse stub replaces it

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 16)
NOW = datetime(2026, 10, 16, 15, 30, tzinfo=ET)


def item(ticker="SPY", premium=1_500_000, pc="PUT", strike=700, expiry="2026-11-20T00:00:00", t="2026-10-16T13:09:00",
         money="OTM", side="BOUGHT", day_total=None, spot=720.0, paid=3.0, size=5000, vol=9000, oi=3000, kind="SWEEP"):
    return {"ticker": ticker, "put_Or_Call": pc, "strike_Price": strike, "expriation_Date": expiry, "moneyNess": money,
            "implied_Bought_Or_Sold": side, "total_Option_Premium_For_Trade": premium, "transaction_DateTime": t,
            "total_Option_Premium_for_Same_Contract_Today": day_total if day_total is not None else premium,
            "sweep_Or_Block": kind, "spot_Price": spot, "price_Of_Contract": paid, "size_Of_Trade": size,
            "volume_When_Traded": vol, "open_Interest_When_Traded": oi}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every report build goes through this stub, so no test can reach Alpaca."""
    monkeypatch.setattr(b, "fetch_stock_prices", lambda tickers: {t: 100.0 + i for i, t in enumerate(sorted(tickers))})
    monkeypatch.setattr(b, "load_stock_tickers", lambda: None)      # no SEC lookup; tests that need the list set it themselves


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


def test_etfs_outside_the_allowlist_are_dropped(monkeypatch):
    stocks = {"NVDA", "WMT", "GLD"}                                          # SEC list: operating companies (and the GLD trust)
    monkeypatch.setattr(b, "load_stock_tickers", lambda: stocks)
    rows = [item(t, 2_000_000) for t in ("NVDA", "SPY", "IWM", "EWY", "SOXX", "GLD", "WMT", "SOXL", "VIX", "XLK")]

    async def fake(args, key):
        return rows
    monkeypatch.setattr(b, "_fetch", fake)
    out = asyncio.run(b.fetch_big_flows(DAY, NOW, "key"))
    assert sorted(i["ticker"] for i in out) == ["IWM", "NVDA", "SOXL", "SPY", "VIX", "WMT", "XLK"]   # EWY, SOXX, GLD dropped


def test_when_the_stock_list_is_unavailable_only_known_trusts_are_dropped():
    assert b.is_blocked_etf("GLD", None) and not b.is_blocked_etf("EWY", None) and not b.is_blocked_etf("SPY", None)


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
# The options-specific figures for one print
# ---------------------------------------------------------------------------

def test_a_put_print_dte_otm_breakeven_and_volume_vs_open_interest():
    p = b.make_print(item(pc="PUT", strike=700, spot=720.0, paid=5.0, vol=9000, oi=3000, size=4478, t="2026-10-16T13:09:00",
                          expiry="2026-11-20T00:00:00"))
    assert p["dte"] == 35 and p["strike"] == 700 and p["put_call"] == "PUT"
    assert round(p["otm_pct"], 4) == round((1 - 700 / 720) * 100, 4)                # 2.78% below the stock
    assert p["be_price"] == 695.0 and round(p["be_move_pct"], 4) == round((695 / 720 - 1) * 100, 4)   # needs a fall of ~3.47%
    assert p["be_move_pct"] < 0 and p["vol_oi"] == 3.0 and p["size"] == 4478 and p["kind"] == "SWEEP"


def test_a_call_print_breakeven_is_strike_plus_what_was_paid():
    p = b.make_print(item(ticker="SMH", pc="CALL", strike=620, spot=550.0, paid=15.25))
    assert p["be_price"] == 635.25 and p["be_move_pct"] > 0
    assert round(p["otm_pct"], 4) == round((620 / 550 - 1) * 100, 4)                # calls are OTM above the stock


def test_volume_vs_open_interest_says_new_when_open_interest_was_zero():
    assert b.make_print(item(vol=500, oi=0))["vol_oi"] == "new"
    assert b.make_print(item(vol=None, oi=100))["vol_oi"] is None


def test_missing_payload_fields_give_none_not_a_crash():
    bare = {"ticker": "ZZZ", "put_Or_Call": "CALL", "strike_Price": 10, "expriation_Date": "2026-11-20T00:00:00",
            "total_Option_Premium_For_Trade": 1_200_000, "transaction_DateTime": "2026-10-16T10:00:00"}
    p = b.make_print(bare)
    assert p["otm_pct"] is None and p["be_move_pct"] is None and p["paid"] is None and p["vol_oi"] is None
    assert p["size"] is None and p["kind"] is None and p["dte"] == 35 and p["premium"] == 1_200_000


# ---------------------------------------------------------------------------
# JarvisFlow's same-contract figure is a RUNNING total
# ---------------------------------------------------------------------------

def test_contract_total_today_is_the_latest_running_total_and_only_shown_when_larger_than_the_print(monkeypatch):
    soxx = [item("SOXX", 1_600_000, pc="CALL", strike=555, expiry="2028-01-21T00:00:00", t="2026-10-16T12:32:04", day_total=2_200_000),
            item("SOXX", 1_195_500, pc="CALL", strike=555, expiry="2028-01-21T00:00:00", t="2026-10-16T12:32:52", day_total=3_395_500),
            item("SOXX", 1_394_750, pc="CALL", strike=555, expiry="2028-01-21T00:00:00", t="2026-10-16T12:37:01", day_total=6_384_250)]
    single = [item("IWM", 8_970_454, strike=258, day_total=8_970_454)]                    # nothing beyond the print itself
    report = b.build_big_flow_report(soxx + single, DAY, NOW)
    by = {g["ticker"]: g for g in report["groups"]}
    assert by["SOXX"]["top"]["premium"] == 1_600_000                                      # largest print...
    assert by["SOXX"]["top"]["day_total"] == 6_384_250                                    # ...but the contract's LATEST total
    assert by["IWM"]["top"]["day_total"] is None


# ---------------------------------------------------------------------------
# Grouping by stock and the report's numbers
# ---------------------------------------------------------------------------

def _prints(*specs):
    return [b.make_print(item(**s)) for s in specs]


def test_stocks_group_biggest_total_first_and_each_row_describes_its_largest_print():
    prints = _prints(dict(ticker="SPY", premium=1_500_000, pc="PUT", strike=700, t="2026-10-16T13:46:00"),
                     dict(ticker="IWM", premium=9_000_000, pc="PUT", strike=258),
                     dict(ticker="SPY", premium=2_600_000, pc="PUT", strike=728, t="2026-10-16T13:09:00"),
                     dict(ticker="SPY", premium=1_200_000, pc="CALL", strike=800, t="2026-10-16T14:00:00"))
    groups = b.group_by_ticker(prints, {"SPY": 762.85})
    assert [g["ticker"] for g in groups] == ["IWM", "SPY"]                              # 9.0M vs 5.3M total
    spy = groups[1]
    assert spy["n_flows"] == 3 and spy["total_premium"] == 5_300_000 and spy["side"] == "both"
    assert spy["call_premium"] == 1_200_000 and spy["put_premium"] == 4_100_000
    assert spy["top"]["strike"] == 728 and spy["top"]["premium"] == 2_600_000           # the row describes the biggest print
    assert spy["stock_px"] == 762.85 and groups[0]["stock_px"] is None                  # price only where Alpaca gave one
    assert [p["alerted_at"].hour for p in spy["prints"]] == [13, 13, 14]                # all prints kept, in time order


def test_report_stats_count_flows_stocks_sides_sweeps_and_blocks():
    items = [item("A", 3_000_000, pc="CALL", kind="SWEEP"), item("A", 1_000_000, pc="PUT", kind="BLOCK"),
             item("B", 2_000_000, pc="PUT", kind="BLOCK"), item("C", 1_500_000, pc="CALL", kind="SWEEP")]
    r = b.build_big_flow_report(items, DAY, NOW)
    st = r["stats"]
    assert st["count"] == 4 and st["unique_tickers"] == 3 and st["total_premium"] == 7_500_000
    assert st["calls"] == 2 and st["puts"] == 2 and st["call_premium"] == 4_500_000 and st["put_premium"] == 3_000_000
    assert st["sweeps"] == 2 and st["blocks"] == 2
    assert sum(g["total_premium"] for g in r["groups"]) == st["total_premium"]
    assert "$1M+ Flow Card" in r["caption"] and "Fri Oct 16" in r["caption"] and "3:30 PM ET" in r["caption"]
    assert "performance" not in r["caption"].lower() and "traded since" not in r["caption"].lower()


def test_a_failed_price_lookup_never_fails_the_card(tmp_path):
    items = [item("A", 3_000_000), item("B", 1_200_000, pc="CALL")]
    r = b.build_big_flow_report(items, DAY, NOW, price_fn=lambda tickers: 1 / 0)
    assert all(g["stock_px"] is None for g in r["groups"])
    assert os.path.getsize(b.render_report_cards(r, str(tmp_path))[0]) > 10_000


def test_the_report_carries_no_performance_data():
    r = b.build_big_flow_report([item("A", 3_000_000)], DAY, NOW)
    forbidden = {"high_pct", "low_pct", "last_pct", "entry_px", "alert_px", "hit_50_before_minus_50"}
    assert not forbidden & set(r["groups"][0]["top"]) and not forbidden & set(r["stats"])


# ---------------------------------------------------------------------------
# One image
# ---------------------------------------------------------------------------

def test_the_card_is_exactly_one_image_however_many_stocks(tmp_path):
    for n in (1, 5, 40):
        items = [item(f"T{i}", 3_000_000 - i * 10_000, strike=100 + i) for i in range(n)]
        paths = b.render_report_cards(b.build_big_flow_report(items, DAY, NOW), str(tmp_path / f"n{n}"))
        assert len(paths) == 1 and os.path.getsize(paths[0]) > 10_000


def test_card_is_landscape_and_rows_past_the_panel_cap_fold(tmp_path):
    from PIL import Image
    n = 2 * b.MAX_ROWS_PER_PANEL + 6
    items = [item(f"T{i}", 3_000_000 - i * 10_000, strike=100 + i) for i in range(n)]
    path = b.render_report_cards(b.build_big_flow_report(items, DAY, NOW), str(tmp_path))[0]
    width, height = Image.open(path).size
    assert width == 3600 and width > height


def test_a_print_with_missing_fields_still_renders(tmp_path):
    bare = {"ticker": "ZZZ", "put_Or_Call": "CALL", "strike_Price": 10, "expriation_Date": "2026-11-20T00:00:00",
            "total_Option_Premium_For_Trade": 1_200_000, "transaction_DateTime": "2026-10-16T10:00:00", "moneyNess": "OTM",
            "implied_Bought_Or_Sold": "BOUGHT"}
    assert os.path.getsize(b.render_report_cards(b.build_big_flow_report([bare], DAY, NOW), str(tmp_path))[0]) > 10_000


# ---------------------------------------------------------------------------
# The stock's current price
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._d


def test_stock_prices_come_from_the_latest_trade_with_fallbacks_and_skip_unknown_symbols(monkeypatch):
    fetch = _REAL_FETCH_STOCK_PRICES
    snaps = {"IWM": {"latestTrade": {"p": 270.41}, "minuteBar": {"c": 1.0}},
             "SPY": {"minuteBar": {"c": 763.31}},                                    # no latest trade: minute bar
             "Q": {"dailyBar": {"c": 115.18}},                                       # only a daily bar
             "EMPTY": {}}                                                            # nothing usable: omitted
    seen = []
    monkeypatch.setattr(b.httpx, "get", lambda url, **kw: seen.append(kw["params"]) or _Resp(snaps))
    monkeypatch.setattr(b.w, "_alpaca_headers", lambda: {})
    out = fetch(["SPY", "IWM", "Q", "EMPTY", "SPY", "NOPE"])
    assert out == {"IWM": 270.41, "SPY": 763.31, "Q": 115.18}
    assert len(seen) == 1 and seen[0]["feed"] == "iex" and seen[0]["symbols"] == "EMPTY,IWM,NOPE,Q,SPY"   # deduped, one request


def test_stock_price_lookup_batches_100_symbols_and_survives_a_failed_batch(monkeypatch):
    fetch = _REAL_FETCH_STOCK_PRICES
    calls = []

    def fake(url, **kw):
        calls.append(kw["params"]["symbols"].count(",") + 1)
        if len(calls) == 1:
            return _Resp({}, status=500)
        return _Resp({"S120": {"latestTrade": {"p": 5.0}}})
    monkeypatch.setattr(b.httpx, "get", fake)
    monkeypatch.setattr(b.w, "_alpaca_headers", lambda: {})
    out = fetch([f"S{i:03d}" for i in range(130)])
    assert calls == [100, 30] and out == {"S120": 5.0}                               # first batch failed, second still used


# ---------------------------------------------------------------------------
# The scheduled job
# ---------------------------------------------------------------------------

@pytest.fixture
def job(monkeypatch):
    import main as m
    monkeypatch.setattr(m, "DISCORD_WEBHOOK_URL", "https://example.invalid/main")
    monkeypatch.setattr(m, "BIG_FLOW_WEBHOOK_URL", None)
    monkeypatch.setattr(m, "is_trading_day", lambda d: True)
    log = {"fetched": 0, "posted": []}

    async def fetch(day, cutoff, key):
        log["fetched"] += 1
        return [item()]
    monkeypatch.setattr(m, "fetch_big_flows", fetch)
    monkeypatch.setattr(m, "build_big_flow_report", lambda items, day, now: {
        "stats": {"count": 1, "unique_tickers": 1, "total_premium": 1}, "caption": "cap", "groups": []})
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


def test_job_no_longer_needs_the_alpaca_keys_to_post(job, monkeypatch):
    m, log = job
    monkeypatch.setattr(m, "alpaca_configured", lambda: False)                   # a flow card; only the stock price needs Alpaca
    asyncio.run(m.big_flow_report_job())
    assert len(log["posted"]) == 1


def test_job_skips_non_trading_days_and_empty_days(job, monkeypatch):
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


def test_job_survives_a_jarvis_failure(job, monkeypatch):
    m, log = job

    async def boom(day, cutoff, key):
        raise RuntimeError("JarvisFlow error")
    monkeypatch.setattr(m, "fetch_big_flows", boom)
    asyncio.run(m.big_flow_report_job())                                     # logged, never raises
    assert log["posted"] == []
