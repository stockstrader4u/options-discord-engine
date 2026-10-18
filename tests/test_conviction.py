"""Synthetic-alert tests for conviction.py.  Run: python tests/test_conviction.py  (or pytest)."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import FlowAlert
from conviction import evaluate_conviction, TIER_VERY_HIGH, TIER_HIGH, TIER_STANDARD


class FakeDB:
    def __init__(self, repeats=0, premiums=None, boom=False):
        self.repeats, self.premiums, self.boom = repeats, premiums or [], boom

    def count_same_day_published_prints(self, *a):
        if self.boom:
            raise RuntimeError("db down")
        return self.repeats

    def get_recent_published_premiums(self, *a):
        if self.boom:
            raise RuntimeError("db down")
        return self.premiums


def enr(tier="otm"):
    return SimpleNamespace(moneyness_tier=tier)


def item(**kw):
    base = {"sweep_Or_Block": "SWEEP", "moneyNess": "OTM", "implied_Bought_Or_Sold": "BOUGHT",
            "transaction_DateTime": "2026-10-16T12:30:00"}   # mid-session: no timing signal
    base.update(kw)
    return base


def alert(**kw):
    base = dict(ticker="NVDA", contract="NVDA 2026-10-19 220C", premium=150_000, sentiment="bullish",
                flow_type="sweep", volume=2000, open_interest=1000)   # vol/OI 2x: baseline yes, S3 no
    base.update(kw)
    return FlowAlert(**base)


def test_baseline_plus_two_signals_is_very_high():
    # repeat print (S1) + vol >= 3x OI (S3) + near-money (S5)
    r = evaluate_conviction(alert(volume=4000), enr(), item(is_Repeating_Options_Trade_Today=True), FakeDB())
    assert r["tier"] == TIER_VERY_HIGH
    assert r["signals"]["s1_repeat_accumulation"] and r["signals"]["s3_fresh_positioning"]


def test_baseline_plus_zero_signals_is_high():
    r = evaluate_conviction(alert(), enr("deep_otm"), item(), FakeDB())   # deep OTM switches S5 off
    assert r["signal_count"] == 0 and r["tier"] == TIER_HIGH


def test_one_signal_is_still_high():
    r = evaluate_conviction(alert(), enr(), item(), FakeDB())   # only S5 (near-money) fires
    assert r["signal_count"] == 1 and r["tier"] == TIER_HIGH


def test_baseline_failure_is_standard():
    # vol < OI fails B6 even though S1 fires
    r = evaluate_conviction(alert(volume=500, open_interest=1000), enr(),
                            item(is_Repeating_Options_Trade_Today=True), FakeDB())
    assert not r["baseline"]["b6_vol_gt_oi"] and r["tier"] == TIER_STANDARD


def test_premium_alone_never_makes_very_high():
    r = evaluate_conviction(alert(premium=5_000_000), enr("deep_otm"), item(), FakeDB())
    assert r["tier"] == TIER_HIGH and r["signal_count"] == 0


def test_below_premium_floor_is_standard():
    assert evaluate_conviction(alert(premium=40_000), enr(), item(), FakeDB())["tier"] == TIER_STANDARD


def test_unusual_for_name_needs_history_and_3x_median():
    hist = [100_000] * 6
    s4 = lambda prem, h: evaluate_conviction(alert(premium=prem), enr("deep_otm"), item(), FakeDB(premiums=h))["signals"]["s4_unusual_for_name"]
    assert s4(320_000, hist)
    assert not s4(250_000, hist)
    assert not s4(900_000, [100_000] * 3)     # too little history -> no verdict


def test_timing_open_close_and_catalyst():
    f = lambda t, **a: evaluate_conviction(alert(**a), enr(), item(transaction_DateTime=t), FakeDB())["signals"]["s6_timing"]
    assert f("2026-10-16T09:41:00") and f("2026-10-16T15:20:00")
    assert not f("2026-10-16T12:30:00")
    assert f("2026-10-16T12:30:00", catalyst="earnings")


def test_missing_optional_fields_do_not_crash():
    a = alert(volume=None, open_interest=None)
    r = evaluate_conviction(a, enr(), {}, FakeDB())
    assert r["tier"] == TIER_STANDARD            # B2/B3/B6 unverifiable -> baseline not met
    assert not r["signals"]["s3_fresh_positioning"]
    r = evaluate_conviction(a, SimpleNamespace(), {"transaction_DateTime": "garbage"}, FakeDB())
    assert r["tier"] in (TIER_STANDARD, TIER_HIGH, TIER_VERY_HIGH)


def test_db_failure_degrades_signals_not_the_tier():
    r = evaluate_conviction(alert(), enr("deep_otm"), item(is_Repeating_Options_Trade_Today=False), FakeDB(boom=True))
    assert r["tier"] == TIER_HIGH and not r["signals"]["s1_repeat_accumulation"]


def test_b4_is_reported_unverifiable_and_excluded():
    r = evaluate_conviction(alert(), enr(), item(), FakeDB())
    assert r["b4_verifiable"] is False and r["baseline"]["b4_at_or_above_ask"] is None and r["baseline_met"]


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:
                fails += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
