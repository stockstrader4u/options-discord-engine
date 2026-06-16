"""
enrichment.py — Phase 2 enrichment service.

Takes a FlowAlert and returns a FlowEnrichment with computed context:
- DTE (days to expiry) calculated from contract string
- DTE bucket (derived or confirmed)
- Moneyness classification
- Premium size tier label
- Mock spot price + RVOL (real provider slot ready for Phase 4)

All enrichment is additive — FlowAlert is never mutated.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

from models import FlowAlert


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

MoneynessTier = Literal["deep_itm", "itm", "atm", "otm", "deep_otm", "unknown"]
PremiumTier = Literal["whale", "large", "solid", "decent", "small"]
DTEBucket = Literal["weeklies", "next_week", "monthly", "unknown"]
ConvictionLabel = Literal["high", "medium", "low", "very_low"]


class FlowEnrichment(BaseModel):
    """Enriched context derived from a FlowAlert."""

    ticker: str
    contract: str

    # DTE
    dte: int | None = None                          # days to expiry
    dte_bucket: DTEBucket = "unknown"               # derived bucket
    expiry_date: str | None = None                  # YYYY-MM-DD parsed from contract

    # Moneyness
    strike: float | None = None
    put_call: Literal["call", "put", "unknown"] = "unknown"
    spot_price: float | None = None                 # mock until Phase 4
    moneyness_pct: float | None = None              # (strike - spot) / spot * 100
    moneyness_tier: MoneynessTier = "unknown"

    # Premium
    premium: int
    premium_tier: PremiumTier

    # Volume context (mock until Phase 4)
    rvol: float | None = None                       # relative volume vs 20-day avg
    rvol_label: Literal["extreme", "high", "normal", "low", "unknown"] = "unknown"

    # Trade structure signals derived from enrichment
    is_lotto: bool = False                          # far OTM + short DTE
    is_near_expiry: bool = False                    # DTE <= 3
    structure_notes: list[str] = []

    # Source alert hash for linking back
    alert_hash: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Matches contract strings like:
#   "SPY 2025-06-20 600C"
#   "AAPL 2025-07-18 200C"
#   "NVDA 2025-06-20 130P"
_CONTRACT_RE = re.compile(
    r"(?P<ticker>[A-Z]+)\s+"
    r"(?P<expiry>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<strike>[\d.]+)"
    r"(?P<pc>[CP])",
    re.IGNORECASE,
)


def _parse_contract(contract: str) -> tuple[str | None, float | None, str | None]:
    """
    Parse a contract string into (expiry_date, strike, put_call).
    Returns (None, None, None) if the format is unrecognised.
    """
    m = _CONTRACT_RE.search(contract)
    if not m:
        return None, None, None
    expiry = m.group("expiry")
    strike = float(m.group("strike"))
    pc = "call" if m.group("pc").upper() == "C" else "put"
    return expiry, strike, pc


def _compute_dte(expiry_str: str) -> int | None:
    """Days from today to expiry_str (YYYY-MM-DD). Returns None on parse error."""
    try:
        expiry = date.fromisoformat(expiry_str)
        delta = (expiry - date.today()).days
        return max(delta, 0)
    except ValueError:
        return None


def _dte_to_bucket(dte: int) -> DTEBucket:
    if dte <= 7:
        return "weeklies"
    if dte <= 14:
        return "next_week"
    if dte <= 45:
        return "monthly"
    return "unknown"


def _premium_tier(premium: int) -> PremiumTier:
    if premium >= 500_000:
        return "whale"
    if premium >= 250_000:
        return "large"
    if premium >= 100_000:
        return "solid"
    if premium >= 50_000:
        return "decent"
    return "small"


def _moneyness(strike: float, spot: float, put_call: str) -> tuple[float, MoneynessTier]:
    """
    Returns (moneyness_pct, tier).
    moneyness_pct = (strike - spot) / spot * 100
    For puts, flip the sign so positive = OTM for both sides.
    """
    pct = (strike - spot) / spot * 100.0
    if put_call == "put":
        pct = -pct  # puts are OTM when strike < spot

    if pct <= -10:
        return pct, "deep_itm"
    if pct <= -2:
        return pct, "itm"
    if pct <= 2:
        return pct, "atm"
    if pct <= 10:
        return pct, "otm"
    return pct, "deep_otm"


def _mock_spot(ticker: str) -> float | None:
    """
    Mock spot prices for common tickers.
    Phase 4 replaces this with a real market data provider.
    """
    mock_prices: dict[str, float] = {
        "SPY": 545.0,
        "QQQ": 465.0,
        "IWM": 205.0,
        "NVDA": 130.0,
        "AAPL": 210.0,
        "TSLA": 175.0,
        "MSFT": 420.0,
        "AMZN": 195.0,
        "META": 510.0,
        "GOOGL": 175.0,
        "AMD": 155.0,
        "COIN": 230.0,
        "MSTR": 390.0,
    }
    return mock_prices.get(ticker.upper())


def _mock_rvol(ticker: str) -> tuple[float, str]:
    """
    Mock relative volume. Phase 4 replaces with real provider.
    Returns (rvol_float, label).
    """
    import random
    random.seed(hash(ticker) % 1000)
    rvol = round(random.uniform(0.8, 3.5), 2)
    if rvol >= 3.0:
        label = "extreme"
    elif rvol >= 2.0:
        label = "high"
    elif rvol >= 0.8:
        label = "normal"
    else:
        label = "low"
    return rvol, label


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich_alert(alert: FlowAlert, alert_hash: str | None = None) -> FlowEnrichment:
    """
    Enrich a FlowAlert with computed context.

    Args:
        alert: The raw FlowAlert to enrich.
        alert_hash: Optional hash to link back to the published_alerts table.

    Returns:
        FlowEnrichment with all derived fields populated where possible.
    """
    expiry_str, strike, put_call = _parse_contract(alert.contract)

    # DTE
    dte: int | None = None
    dte_bucket: DTEBucket = alert.dte_bucket  # type: ignore[assignment]
    if expiry_str:
        dte = _compute_dte(expiry_str)
        if dte is not None:
            dte_bucket = _dte_to_bucket(dte)

    # Spot + moneyness
    spot = _mock_spot(alert.ticker)
    moneyness_pct: float | None = None
    moneyness_tier: MoneynessTier = "unknown"
    if spot and strike and put_call:
        moneyness_pct, moneyness_tier = _moneyness(strike, spot, put_call)

    # RVOL
    rvol, rvol_label = _mock_rvol(alert.ticker)

    # Premium tier
    premium_tier = _premium_tier(alert.premium)

    # Structure notes
    structure_notes: list[str] = []
    is_lotto = False
    is_near_expiry = False

    if dte is not None and dte <= 3:
        is_near_expiry = True
        structure_notes.append(f"near-expiry ({dte}d)")

    if moneyness_tier == "deep_otm":
        structure_notes.append("deep OTM")
        if dte is not None and dte <= 7:
            is_lotto = True
            structure_notes.append("⚠️ lotto structure (deep OTM + short DTE)")

    if moneyness_tier == "atm":
        structure_notes.append("ATM — high gamma")

    if rvol_label in ("extreme", "high"):
        structure_notes.append(f"elevated RVOL ({rvol}x)")

    if premium_tier == "whale":
        structure_notes.append("whale-size premium")

    return FlowEnrichment(
        ticker=alert.ticker,
        contract=alert.contract,
        dte=dte,
        dte_bucket=dte_bucket,
        expiry_date=expiry_str,
        strike=strike,
        put_call=put_call or "unknown",  # type: ignore[arg-type]
        spot_price=spot,
        moneyness_pct=round(moneyness_pct, 2) if moneyness_pct is not None else None,
        moneyness_tier=moneyness_tier,
        premium=alert.premium,
        premium_tier=premium_tier,
        rvol=rvol,
        rvol_label=rvol_label,  # type: ignore[arg-type]
        is_lotto=is_lotto,
        is_near_expiry=is_near_expiry,
        structure_notes=structure_notes,
        alert_hash=alert_hash,
    )


def enrichment_summary(e: FlowEnrichment) -> str:
    """
    Return a one-line human-readable enrichment summary for Discord or logs.
    Example: "SPY | ATM | 3 DTE | $545 spot | RVOL 2.1x (high) | whale premium"
    """
    parts: list[str] = [e.ticker]

    if e.moneyness_tier != "unknown":
        parts.append(e.moneyness_tier.replace("_", " ").upper())

    if e.dte is not None:
        parts.append(f"{e.dte} DTE")

    if e.spot_price:
        parts.append(f"${e.spot_price:,.0f} spot")

    if e.rvol:
        parts.append(f"RVOL {e.rvol}x ({e.rvol_label})")

    parts.append(f"{e.premium_tier} premium")

    return " | ".join(parts)
