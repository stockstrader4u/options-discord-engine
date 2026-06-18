"""
flow_filters.py — Shared pre-filter for raw JarvisFlow items.

Applied BEFORE scoring/enrichment, on the raw dict JarvisFlow returns
(not the mapped FlowAlert). Both main.py's scheduled poller and
mcp_server.py's manual MCP tools should call passes_basic_filters()
on each raw item right after fetching, before doing anything else with it.

Filter criteria:
  - DTE (days to expiration) between 0 and MAX_DTE_DAYS inclusive
  - moneyNess is ATM or OTM (never ITM)
  - implied_Bought_Or_Sold == "BOUGHT" — proxy for "trade executed at/above
    the ask" (JarvisFlow doesn't send raw bid/ask quotes, so BOUGHT vs SOLD
    is the closest available signal: BOUGHT trades are inferred as hitting
    the ask, SOLD trades are inferred as hitting the bid).

Note: this intentionally drops all SOLD-side trades (e.g. "SOLD PUT"),
even though main.py's sentiment mapping treats some SOLD trades as
bullish signals. That's a deliberate behavior change requested explicitly —
the goal is to only ever consider ask-side (BOUGHT) flow at all.
"""

from __future__ import annotations

from datetime import datetime, timezone

MAX_DTE_DAYS = 14
ALLOWED_MONEYNESS = {"ATM", "OTM"}
REQUIRED_SIDE = "BOUGHT"


def _parse_expiry(expiry_raw: str) -> datetime | None:
    """Parse JarvisFlow's expiration_Date string into a tz-aware datetime."""
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
    """
    Days until expiration, counting from as_of (defaults to now, UTC).
    Returns None if expiry_raw can't be parsed.
    """
    expiry_dt = _parse_expiry(expiry_raw)
    if expiry_dt is None:
        return None
    now = as_of or datetime.now(timezone.utc)
    return (expiry_dt.date() - now.date()).days


def passes_basic_filters(item: dict) -> bool:
    """
    True if this raw JarvisFlow item meets all basic criteria:
    DTE 0-14, ATM/OTM only, BOUGHT only.
    """
    money_ness = (item.get("moneyNess") or "").strip().upper()
    if money_ness not in ALLOWED_MONEYNESS:
        return False

    bought_sold = (
        item.get("implied_Bought_Or_Sold")
        or item.get("impliedBoughtOrSold")
        or ""
    ).strip().upper()
    if bought_sold != REQUIRED_SIDE:
        return False

    expiry_raw = item.get("expriation_Date") or item.get("expriationDate")
    dte_days = compute_dte_days(expiry_raw)
    if dte_days is None:
        return False
    if dte_days < 0 or dte_days > MAX_DTE_DAYS:
        return False

    return True


def filter_flow_items(items: list[dict]) -> tuple[list[dict], int]:
    """
    Apply passes_basic_filters to a list of raw items.
    Returns (filtered_items, skipped_count).
    """
    filtered = [item for item in items if passes_basic_filters(item)]
    skipped = len(items) - len(filtered)
    return filtered, skipped
