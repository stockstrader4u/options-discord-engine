"""
conviction.py -- rule-based conviction tier shown on subscriber cards.

Replaces the score-derived word ("Very High" = score >= 80). Measured on 570 published flows (Jul 1-Oct 16
2026) that label was indistinguishable from "High" on every outcome: the score's variable part is almost
entirely premium size, and size does not predict direction. The numeric score is unchanged (it still gates
posting at MIN_ALERT_SCORE and is still printed on the card); only the WORD comes from here.

The owner's lens (an options day trader):
  baseline  B1 sweep or block   B2 OTM   B3 BOUGHT   B4 executed at/above the ask
            B5 premium >= $50k  B6 volume > open interest
  High      = every verifiable baseline criterion met
  Very High = baseline met AND >= 2 "above and beyond" signals (S1-S6)
  Standard  = baseline not met (the engine posts looser than his lens: 0-21 DTE, no vol>OI requirement)
Premium size alone never earns Very High.

Post-print tape follow-through is deliberately NOT an input: it is unknowable at publish time, so it is an
evaluation metric only (conviction_scoreboard.py).

Data gaps (JarvisFlow row fields verified against a live payload 2026-10-17: ticker, moneyNess, spot_Price,
strike_Price, expriation_Date, price_Of_Contract, volume_When_Traded, size_Of_Trade,
open_Interest_When_Traded, total_Option_Premium_For_Trade, put_Or_Call, sweep_Or_Block,
implied_Bought_Or_Sold, total_Option_Premium_for_Same_Contract_Today, transaction_DateTime,
is_Repeating_Options_Trade_Today, interpreted_Conviction):
  B4 -- no bid/ask or NBBO field. implied_Bought_Or_Sold is JarvisFlow's own aggressor inference ("hit the
        ask") so B3 is the nearest proxy, but it is not a verification. B4 is excluded from the baseline.
  S2 -- no exchange list or per-fill price levels, so multi-exchange / multi-level urgency is always False.
  S5 -- no spread-leg flag, so only the not-deep-OTM half of the check runs.
Each gap logs one warning per process.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("options-discord-engine")

EASTERN = ZoneInfo("America/New_York")

TIER_VERY_HIGH = "Very High"
TIER_HIGH = "High"
TIER_STANDARD = "Standard"   # posted, but below the owner's baseline. Wording is an open owner decision.

MIN_PREMIUM = 50_000                 # B5 noise floor
MIN_SIGNALS_FOR_VERY_HIGH = 2
FRESH_VOL_OI_MULT = 3.0              # S3
UNUSUAL_PREMIUM_MULT = 3.0           # S4
UNUSUAL_HISTORY_DAYS = 28            # ~20 trading days
UNUSUAL_MIN_HISTORY = 5              # fewer prior prints than this -> no verdict (False)
OPEN_WINDOW_END = time(10, 0)        # S6: 9:30-10:00 ET
CLOSE_WINDOW_START = time(15, 0)     # S6: 15:00-16:00 ET
SESSION_OPEN, SESSION_CLOSE = time(9, 30), time(16, 0)

_logged_gaps: set[str] = set()


def _log_gap_once(key: str, msg: str) -> None:
    if key not in _logged_gaps:
        _logged_gaps.add(key)
        logger.warning("conviction data gap: %s", msg)


def _field(item: dict, *names: str) -> Any:
    for n in names:
        if item.get(n) is not None:
            return item[n]
    return None


def _trade_time_et(item: dict) -> datetime:
    """Trade time as aware ET. JarvisFlow stamps naive ET wall-clock time; falls back to now."""
    raw = _field(item, "transaction_DateTime", "transactionDateTime")
    if raw:
        try:
            return datetime.strptime(str(raw)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=EASTERN)
        except ValueError:
            pass
    return datetime.now(EASTERN)


def _utc_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _baseline(alert, enrichment, item: dict) -> dict:
    sweep_block = str(_field(item, "sweep_Or_Block", "sweepOrBlock") or alert.flow_type or "").upper()
    money = str(_field(item, "moneyNess") or "").upper()
    if not money:
        money = "OTM" if getattr(enrichment, "moneyness_tier", "") in ("otm", "deep_otm") else ""
    side = str(_field(item, "implied_Bought_Or_Sold", "impliedBoughtOrSold") or "").upper()
    if not side and alert.note and "BOUGHT" in alert.note.upper():
        side = "BOUGHT"

    vol, oi = alert.volume, alert.open_interest
    vol_gt_oi = bool(vol is not None and oi is not None and vol > oi)

    _log_gap_once("b4", "no bid/ask or aggressor field in the JarvisFlow payload -- B4 (executed at/above ask) "
                        "cannot be verified; excluded from the baseline (BOUGHT is Jarvis's own inference)")
    return {
        "b1_sweep_or_block": sweep_block in ("SWEEP", "BLOCK"),
        "b2_otm": money == "OTM",
        "b3_bought": side == "BOUGHT",
        "b4_at_or_above_ask": None,
        "b5_premium_50k": alert.premium >= MIN_PREMIUM,
        "b6_vol_gt_oi": vol_gt_oi,
    }


def _s1_repeat_accumulation(alert, item: dict, db, trade_et: datetime) -> bool:
    # JarvisFlow's own flag sees every print; our DB only sees the ones we published (the same-day dedup
    # suppresses most repeats), so either one counts.
    if _field(item, "is_Repeating_Options_Trade_Today", "isRepeatingOptionsTradeToday") is True:
        return True
    try:
        day_start = trade_et.replace(hour=0, minute=0, second=0, microsecond=0)
        return db.count_same_day_published_prints(alert.contract, alert.sentiment, _utc_str(day_start)) >= 1
    except Exception as e:
        logger.warning("conviction S1 lookup failed for %s: %s", alert.contract, e)
        return False


def _s2_urgency() -> bool:
    _log_gap_once("s2", "no exchange list / per-fill price levels in the JarvisFlow payload -- S2 (multi-exchange "
                        "or multi-level sweep) is always False")
    return False


def _s3_fresh_positioning(alert) -> bool:
    vol, oi = alert.volume, alert.open_interest
    if vol is None or oi is None:
        return False
    if oi <= 0:
        return vol > 0
    return vol >= FRESH_VOL_OI_MULT * oi


def _s4_unusual_for_name(alert, db, trade_et: datetime) -> bool:
    try:
        since = trade_et - timedelta(days=UNUSUAL_HISTORY_DAYS)
        premiums = db.get_recent_published_premiums(alert.ticker, _utc_str(since))
    except Exception as e:
        logger.warning("conviction S4 lookup failed for %s: %s", alert.ticker, e)
        return False
    if len(premiums) < UNUSUAL_MIN_HISTORY:
        return False
    med = statistics.median(premiums)
    return med > 0 and alert.premium >= UNUSUAL_PREMIUM_MULT * med


def _s5_pure_directional(enrichment) -> bool:
    _log_gap_once("s5", "no spread-leg flag in the JarvisFlow payload -- S5 only checks near-money (not deep OTM)")
    return getattr(enrichment, "moneyness_tier", "unknown") != "deep_otm"


def _s6_timing(alert, trade_et: datetime) -> bool:
    if alert.catalyst:
        return True
    t = trade_et.timetz().replace(tzinfo=None)
    return (SESSION_OPEN <= t < OPEN_WINDOW_END) or (CLOSE_WINDOW_START <= t <= SESSION_CLOSE)


def evaluate_conviction(alert, enrichment, item: dict, db) -> dict:
    """Return {'baseline': {...B1-B6}, 'signals': {...S1-S6}, 'tier': ..., 'b4_verifiable': bool,
    'baseline_met': bool, 'signal_count': int}.  Never raises: a failed lookup makes that signal False."""
    item = item or {}
    trade_et = _trade_time_et(item)

    baseline = _baseline(alert, enrichment, item)
    verifiable = {k: v for k, v in baseline.items() if v is not None}
    baseline_met = all(verifiable.values())

    signals = {
        "s1_repeat_accumulation": _s1_repeat_accumulation(alert, item, db, trade_et),
        "s2_urgency": _s2_urgency(),
        "s3_fresh_positioning": _s3_fresh_positioning(alert),
        "s4_unusual_for_name": _s4_unusual_for_name(alert, db, trade_et),
        "s5_pure_directional": _s5_pure_directional(enrichment),
        "s6_timing": _s6_timing(alert, trade_et),
    }
    signal_count = sum(1 for v in signals.values() if v)

    if not baseline_met:
        tier = TIER_STANDARD
    elif signal_count >= MIN_SIGNALS_FOR_VERY_HIGH:
        tier = TIER_VERY_HIGH
    else:
        tier = TIER_HIGH

    return {
        "tier": tier,
        "baseline": baseline,
        "signals": signals,
        "b4_verifiable": False,
        "baseline_met": baseline_met,
        "signal_count": signal_count,
    }
