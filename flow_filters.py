"""
flow_filters.py — Shared pre-filter for raw JarvisFlow items.

Applied BEFORE scoring/enrichment, on the raw dict JarvisFlow returns.
Both main.py's scheduled poller and mcp_server.py's manual MCP tools
should call passes_basic_filters() on each raw item right after fetching.

Filter criteria (ALL must pass):
  - moneyNess is OTM only (never ATM, ITM, or DEEP OTM by JarvisFlow tag)
  - implied_Bought_Or_Sold == "BOUGHT"
  - total_Option_Premium_For_Trade >= $100,000
  - interpreted_Conviction is HIGH or above (never NORMAL)
  - DTE between 0 and 14 days inclusive

Applies identically to every ticker — no symbol-based restrictions.
"""

from __future__ import annotations

from datetime import datetime, timezone

MAX_DTE_DAYS = 14
ALLOWED_MONEYNESS = {"OTM"}
REQUIRED_SIDE = "BOUGHT"
MIN_PREMIUM = 100_000
ALLOWED_CONVICTION = {"HIGH", "VERY HIGH", "EXTREMELY HIGH"}
HIGH_CONVICTION_VALUE = "HIGH"


def _parse_expiry(expiry_raw: str) -> datetime | None:
    if not expiry_raw:
        return None
    try:
        dt = datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def compute_dte_days(expiry_raw: str, as_of: datetime | None = None) -> int | None:
    expiry_dt = _parse_expiry(expiry_raw)
    if expiry_dt is None:
        return None
    now = as_of or datetime.now(timezone.utc)
    return (expiry_dt.date() - now.date()).days


def passes_basic_filters(item: dict) -> bool:
    """
    True if this raw JarvisFlow item meets ALL basic criteria.
    Applies identically to every ticker.
    """
    # Moneyness: OTM only
    money_ness = (item.get("moneyNess") or "").strip().upper()
    if money_ness not in ALLOWED_MONEYNESS:
        return False

    # Side: BOUGHT only
    bought_sold = (
        item.get("implied_Bought_Or_Sold")
        or item.get("impliedBoughtOrSold")
        or ""
    ).strip().upper()
    if bought_sold != REQUIRED_SIDE:
        return False

    # Premium: $100K minimum
    premium_raw = (
        item.get("total_Option_Premium_For_Trade")
        or item.get("totalOptionPremiumForTrade")
        or 0
    )
    try:
        premium = float(premium_raw)
    except (TypeError, ValueError):
        premium = 0.0
    if premium < MIN_PREMIUM:
        return False

    # Conviction: HIGH or above only
    conviction = (
        item.get("interpreted_Conviction")
        or item.get("interpretedConviction")
        or ""
    ).strip().upper()
    if conviction not in ALLOWED_CONVICTION:
        return False

    # DTE: 0-14 days
    expiry_raw = item.get("expriation_Date") or item.get("expriationDate")
    dte_days = compute_dte_days(expiry_raw)
    if dte_days is None:
        return False
    if dte_days < 0 or dte_days > MAX_DTE_DAYS:
        return False

    return True


def is_high_conviction(item: dict) -> bool:
    conviction = (
        item.get("interpreted_Conviction")
        or item.get("interpretedConviction")
        or ""
    ).strip().upper()
    return conviction in ALLOWED_CONVICTION


def filter_flow_items(items: list[dict]) -> tuple[list[dict], int]:
    filtered = [item for item in items if passes_basic_filters(item)]
    skipped = len(items) - len(filtered)
    return filtered, skipped
