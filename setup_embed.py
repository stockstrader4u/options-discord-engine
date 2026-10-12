"""
setup_embed.py -- the one-embed-per-setup card (NEW_READ=1): title (ticker, expiry, strike, C/P, star if top pick), the 2-sentence
read as the description, then a compact fields row computed in code (Role, Risk, Required move, DTE, R:R, Flow side).
The chart is attached by post_setup_with_chart() in bmt_nightly_setups.py exactly as before.

Discord limits respected: title 256, description 4096, field name 256 / value 1024, 25 fields, 6000 chars per embed
(this card is far below all of them; clamp() keeps it that way even for odd input).
"""
from __future__ import annotations

from setup_roles import aligned_flow_premium, required_move_pct, reward_risk

money = lambda a: f"${a / 1e6:.2f}M" if a >= 1e6 else f"${a / 1e3:.0f}K"


def clamp(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def card_fields(c: dict) -> list:
    m = required_move_pct(c)
    rr1, rr2 = c.get("rr") if isinstance(c.get("rr"), (tuple, list)) else reward_risk(c)
    flow = c.get("flow") or {}
    kind = "calls" if str(c.get("direction", "")).upper() == "CALL" else "puts"
    if flow.get("premium") and flow.get("call_pct") is not None:
        flow_txt = f"{flow.get('bias') or 'n/a'} · {money(aligned_flow_premium(c))} {kind}"
    else:
        flow_txt = "N/A"
    dte = (c.get("time_pressure") or {}).get("dte", c.get("dte"))
    return [
        {"name": "Role", "value": clamp(c.get("role", "Not provided"), 1024), "inline": True},
        {"name": "Risk level", "value": clamp(c.get("risk", "Moderate"), 1024), "inline": True},
        {"name": "Required move", "value": f"{m:+.1f}% to ${c['strike']:g}" if m is not None else "N/A", "inline": True},
        {"name": "DTE", "value": f"{dte} days" if dte is not None else "N/A", "inline": True},
        {"name": "R:R (T1 / T2)", "value": f"{rr1:.1f} / {rr2:.1f}" if rr1 is not None else "N/A", "inline": True},
        {"name": "Flow", "value": clamp(flow_txt, 1024), "inline": True},
    ]


def build_setup_embed_v2(c: dict, rank: int, is_top_pick: bool, color: int) -> dict:
    star = "⭐ " if is_top_pick else ""
    embed = {
        "title": clamp(f"{star}{rank}. ${c['ticker']} — {c['next_expiry']} ${c['strike']:g}{c['direction'][0]}", 256),
        "color": color,
        "fields": card_fields(c),
    }
    if c.get("read"):
        embed["description"] = clamp(c["read"], 4096)
    return embed


def card_fallback_read(c: dict) -> str | None:
    """Used only when there is no chart to read (bars failed the sanity check or Alpaca is unreachable): states the numbers the
    card already has, nothing about the chart."""
    m = required_move_pct(c)
    flow = c.get("flow") or {}
    if m is None or not flow.get("premium"):
        return None
    kind = "calls" if str(c.get("direction", "")).upper() == "CALL" else "puts"
    return (f"Big options buyers put {money(aligned_flow_premium(c))} into {kind} last session. "
            f"The ${c['strike']:g} strike is {abs(m):.1f}% {'above' if m > 0 else 'below'} the price, so the stock needs that move by {c['next_expiry']}.")
