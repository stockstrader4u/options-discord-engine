"""
setup_roles.py -- roles and risk labels computed IN CODE for the nightly top-5 (NEW_READ=1). The model never writes either.

Inputs are the setup dicts main() already builds (ticker, direction, strike, current_price, dte, flow, iv_rv_ratio, rr).
Nothing here touches selection, ranking, strikes or levels; it only labels what was already chosen.

Roles are handed out in this order, each from the setups not yet assigned (ties go to the better existing rank):
  1. Best Overall                            = the existing top-ranked setup
  2. Lowest-Move Setup                       = smallest required % move to the strike
  3. Speculative / High-Risk Breakout Setup  = largest required move (tie-break: lowest DTE)
  4. Flow-Backed Momentum Setup              = largest aligned options-flow premium
  5. Best Balanced Setup                     = the one remaining
"""
from __future__ import annotations

import math

ROLE_ORDER = [
    "Best Overall",
    "Lowest-Move Setup",
    "Speculative / High-Risk Breakout Setup",
    "Flow-Backed Momentum Setup",
    "Best Balanced Setup",
]


def required_move_pct(c: dict) -> float | None:
    """Signed % from the current price to the strike (positive = strike above the price)."""
    price, strike = c.get("current_price"), c.get("strike")
    if not price or not strike:
        return None
    return (float(strike) - float(price)) / float(price) * 100


def aligned_flow_premium(c: dict) -> float:
    """Dollars of bought options flow on the idea's own side (calls for a CALL idea, puts for a PUT idea)."""
    flow = c.get("flow") or {}
    prem = float(flow.get("premium") or 0)
    call_pct = flow.get("call_pct")
    if call_pct is None:
        return 0.0
    share = call_pct if str(c.get("direction", "")).upper() == "CALL" else 100 - call_pct
    return prem * share / 100.0


def _abs_move(c: dict) -> float:
    m = required_move_pct(c)
    return abs(m) if m is not None else math.inf       # unknown move never wins "lowest", can win "largest" last resort


def assign_roles(selected: list) -> dict:
    """{ticker: role}, one role each, no duplicates. `selected` is in rank order (index 0 = top-ranked)."""
    rank = {c["ticker"]: i for i, c in enumerate(selected)}
    left = list(selected)
    out: dict = {}

    def take(role, key):
        if not left:
            return
        pick = min(left, key=key)
        out[pick["ticker"]] = role
        left.remove(pick)

    take(ROLE_ORDER[0], lambda c: rank[c["ticker"]])
    take(ROLE_ORDER[1], lambda c: (_abs_move(c), rank[c["ticker"]]))
    take(ROLE_ORDER[2], lambda c: (-(_abs_move(c) if math.isfinite(_abs_move(c)) else -1.0), c.get("dte", 99), rank[c["ticker"]]))
    take(ROLE_ORDER[3], lambda c: (-aligned_flow_premium(c), rank[c["ticker"]]))
    take(ROLE_ORDER[4], lambda c: rank[c["ticker"]])
    for c in left:                                       # more than five setups cannot happen (TOP_N=5); never leave one blank
        out[c["ticker"]] = ROLE_ORDER[4]
    return out


def risk_label(c: dict) -> str:
    """Low / Moderate / Elevated / High / Speculative from required move, DTE and IV vs realised vol.
    Required move is scaled by sqrt(DTE) (a 5% move in 3 days is a lot harder than in 15), then +1 notch when the options
    are richly priced against recent movement (IV/RV >= 1.5)."""
    m = required_move_pct(c)
    dte = max(float(c.get("dte") or 1), 1.0)
    if m is None:
        return "Moderate"
    pace = abs(m) / math.sqrt(dte)
    pts = 0 if pace < 0.8 else 1 if pace < 1.5 else 2 if pace < 2.5 else 3 if pace < 4 else 4
    ratio = c.get("iv_rv_ratio")
    if ratio is not None and ratio >= 1.5:
        pts += 1
    return ["Low", "Moderate", "Elevated", "High", "Speculative"][min(pts, 4)]


def reward_risk(c: dict) -> tuple:
    """(R:R to target 1, R:R to target 2) -- same arithmetic as compute_reward_risk() in bmt_nightly_setups.py."""
    try:
        entry_mid = (c["entry_low"] + c["entry_high"]) / 2
        is_call = c["direction"].upper() == "CALL"
        risk = (entry_mid - c["stop"]) if is_call else (c["stop"] - entry_mid)
        if risk <= 0:
            return None, None
        r1 = (c["target1"] - entry_mid) if is_call else (entry_mid - c["target1"])
        r2 = (c["target2"] - entry_mid) if is_call else (entry_mid - c["target2"])
        return round(r1 / risk, 1), round(r2 / risk, 1)
    except (KeyError, TypeError):
        return None, None
