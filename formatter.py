"""
formatter.py — Phase 3 Discord alert formatter.

Two output modes:
  - subscriber: clean, trader-friendly, no internal engine language
  - internal:   full debug output with score components, classification, enrichment

Two payload types:
  - plain_text: simple Discord webhook content string
  - embed:      structured Discord embed payload with color, fields, timestamp

Usage:
    from formatter import format_alert, FormatMode, PayloadType

    # Subscriber plain text
    msg = format_alert(alert, enrichment, classification, score, reasons)

    # Internal embed for ops channel
    payload = format_alert(
        alert, enrichment, classification, score, reasons,
        mode="internal", payload_type="embed"
    )
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from models import FlowAlert
from enrichment import FlowEnrichment
from classifier import FlowClassification

FormatMode = Literal["subscriber", "internal"]
PayloadType = Literal["plain_text", "embed"]

# Discord embed colors
_COLOR_BULLISH = 0x00C851   # green
_COLOR_BEARISH = 0xFF4444   # red
_COLOR_NEUTRAL = 0xFFBB33   # amber


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sentiment_emoji(sentiment: str) -> str:
    return {"bullish": "🟢", "bearish": "🔴"}.get(sentiment.lower(), "🟡")


def _conviction_label(score: int) -> str:
    if score >= 80:
        return "Very High"
    if score >= 70:
        return "High"
    if score >= 60:
        return "Medium"
    return "Low"


def _premium_display(premium: int) -> str:
    if premium >= 1_000_000:
        return f"${premium / 1_000_000:.1f}M"
    if premium >= 1_000:
        return f"${premium / 1_000:.0f}K"
    return f"${premium:,}"


def _contract_short(contract: str) -> str:
    """Extract just the expiry + strike + type, e.g. 'Jul 18 · $130C'"""
    parts = contract.split()
    if len(parts) >= 3:
        ticker = parts[0]
        expiry = parts[1]
        strike_type = parts[2]

        # Parse expiry date
        try:
            dt = datetime.strptime(expiry, "%Y-%m-%d")
            expiry_fmt = dt.strftime("%b %d")
        except ValueError:
            expiry_fmt = expiry

        # Format strike
        strike_num = strike_type[:-1]
        pc = "C" if strike_type.endswith("C") else "P"
        try:
            strike_fmt = f"${float(strike_num):.0f}{pc}"
        except ValueError:
            strike_fmt = strike_type

        return f"{strike_fmt} · {expiry_fmt}"
    return contract


def _dte_display(enrichment: FlowEnrichment) -> str:
    if enrichment.dte is not None and enrichment.dte > 0:
        return f"{enrichment.dte}d"
    bucket = enrichment.dte_bucket.replace("_", " ").title()
    return bucket


def _embed_color(sentiment: str) -> int:
    return {
        "bullish": _COLOR_BULLISH,
        "bearish": _COLOR_BEARISH,
    }.get(sentiment.lower(), _COLOR_NEUTRAL)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Subscriber format — plain text
# ---------------------------------------------------------------------------

def _subscriber_plain(
    alert: FlowAlert,
    enrichment: FlowEnrichment,
    classification: FlowClassification,
    score: int,
    reasons: list[str],
) -> str:
    emoji = _sentiment_emoji(alert.sentiment)
    direction = alert.sentiment.title()
    flow = (alert.flow_type or "flow").title()
    contract_short = _contract_short(alert.contract)
    premium_str = _premium_display(alert.premium)
    dte_str = _dte_display(enrichment)
    conviction = _conviction_label(score)

    # Header — punchy, trader-focused
    catalyst_suffix = ""
    if alert.catalyst:
        catalyst_suffix = f" into {alert.catalyst}"
    header = f"{emoji} **{alert.ticker} — {direction} {flow}{catalyst_suffix}**"

    lines = [
        header,
        "",
        f"📋 `{contract_short}` · {dte_str}",
        f"💰 {premium_str} premium · {flow}",
    ]

    # Moneyness if useful
    if enrichment.moneyness_tier not in ("unknown",):
        tier = enrichment.moneyness_tier.replace("_", " ").upper()
        if enrichment.spot_price:
            lines.append(f"📍 {tier} · ${enrichment.spot_price:,.0f} spot")
        else:
            lines.append(f"📍 {tier}")

    # Levels
    if alert.levels:
        lines.append(f"🎯 {alert.levels}")

    # Catalyst (only if not already in header)
    if alert.catalyst and len(catalyst_suffix) == 0:
        lines.append(f"📅 Catalyst: {alert.catalyst}")

    lines.append("")

    # Conviction + score
    lines.append(f"▸ **Conviction: {conviction}** · Score {score}/100")

    # One-line setup summary based on classification
    setup_parts = []
    if classification.trade_style.label != "unknown":
        setup_parts.append(classification.trade_style.label)
    if classification.intent.label not in ("unknown", "speculative"):
        setup_parts.append(classification.intent.label)
    if classification.momentum.label not in ("unknown",):
        setup_parts.append(f"{classification.momentum.label} momentum")

    if setup_parts:
        lines.append(f"▸ {' · '.join(setup_parts).title()}")

    # RVOL if elevated
    if enrichment.rvol_label in ("extreme", "high") and enrichment.rvol:
        lines.append(f"▸ RVOL {enrichment.rvol}x — elevated volume")

    # Note — only if it adds real context (not JarvisFlow boilerplate)
    if alert.note and "JarvisFlow" not in alert.note:
        lines.append(f"\n_{alert.note}_")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Subscriber format — Discord embed
# ---------------------------------------------------------------------------

def _subscriber_embed(
    alert: FlowAlert,
    enrichment: FlowEnrichment,
    classification: FlowClassification,
    score: int,
    reasons: list[str],
) -> dict[str, Any]:
    emoji = _sentiment_emoji(alert.sentiment)
    direction = alert.sentiment.title()
    flow = (alert.flow_type or "flow").title()
    premium_str = _premium_display(alert.premium)
    dte_str = _dte_display(enrichment)
    conviction = _conviction_label(score)
    contract_short = _contract_short(alert.contract)

    catalyst_suffix = f" into {alert.catalyst}" if alert.catalyst else ""
    title = f"{emoji} {alert.ticker} — {direction} {flow}{catalyst_suffix}"

    fields: list[dict] = [
        {"name": "Contract", "value": f"`{contract_short}` · {dte_str}", "inline": True},
        {"name": "Premium", "value": premium_str, "inline": True},
        {"name": "Flow Type", "value": flow, "inline": True},
    ]

    if enrichment.moneyness_tier not in ("unknown",):
        tier = enrichment.moneyness_tier.replace("_", " ").upper()
        spot_str = f" · ${enrichment.spot_price:,.0f} spot" if enrichment.spot_price else ""
        fields.append({"name": "Moneyness", "value": f"{tier}{spot_str}", "inline": True})

    if enrichment.rvol and enrichment.rvol_label in ("extreme", "high"):
        fields.append({"name": "RVOL", "value": f"{enrichment.rvol}x ({enrichment.rvol_label})", "inline": True})

    if alert.levels:
        fields.append({"name": "🎯 Key Levels", "value": alert.levels, "inline": False})

    if alert.catalyst:
        fields.append({"name": "📅 Catalyst", "value": alert.catalyst, "inline": False})

    # Setup summary
    setup_parts = []
    if classification.trade_style.label != "unknown":
        setup_parts.append(classification.trade_style.label.title())
    if classification.momentum.label != "unknown":
        setup_parts.append(f"{classification.momentum.label} momentum".title())
    if classification.setup_quality.label == "actionable":
        setup_parts.append("✅ Actionable")

    if setup_parts:
        fields.append({"name": "Setup", "value": " · ".join(setup_parts), "inline": False})

    fields.append({
        "name": "Conviction",
        "value": f"**{conviction}** · {score}/100",
        "inline": True,
    })

    embed: dict[str, Any] = {
        "title": title,
        "color": _embed_color(alert.sentiment),
        "fields": fields,
        "footer": {"text": f"options-engine · {alert.source.title()}"},
        "timestamp": _utc_now_iso(),
    }

    return {"embeds": [embed]}


# ---------------------------------------------------------------------------
# Internal format — plain text (current format + all debug info)
# ---------------------------------------------------------------------------

def _internal_plain(
    alert: FlowAlert,
    enrichment: FlowEnrichment,
    classification: FlowClassification,
    score: int,
    reasons: list[str],
) -> str:
    emoji = _sentiment_emoji(alert.sentiment)
    premium_str = f"${alert.premium:,}"
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"{emoji} **[INTERNAL] {alert.ticker} {alert.source.title()} Alert**",
        "",
        f"**Time:** {ts}",
        f"**Contract:** {alert.contract}",
        f"**Premium:** {premium_str}",
        f"**Sentiment:** {alert.sentiment.title()}",
        f"**DTE Bucket:** {alert.dte_bucket.replace('_', ' ').title()}",
        f"**Score:** {score}/100",
    ]

    if alert.flow_type:
        lines.append(f"**Flow Type:** {alert.flow_type.title()}")
    if alert.levels:
        lines.append(f"**Levels:** {alert.levels}")
    if alert.catalyst:
        lines.append(f"**Catalyst:** {alert.catalyst}")

    # Score breakdown
    if reasons:
        lines += ["", "**Score components:**"]
        for r in reasons:
            lines.append(f"• {r}")

    # Enrichment
    lines += [
        "",
        f"**Enrichment:** {enrichment.moneyness_tier.replace('_',' ').upper()} | "
        f"{enrichment.dte if enrichment.dte is not None else '?'}d DTE | "
        f"${enrichment.spot_price:,.0f} spot | "
        f"RVOL {enrichment.rvol}x ({enrichment.rvol_label}) | "
        f"{enrichment.premium_tier} premium",
    ]

    if enrichment.structure_notes:
        lines.append(f"**Structure:** {' · '.join(enrichment.structure_notes)}")

    # Classification
    lines += [
        "",
        "**Classification:**",
        f"• Trade style: {classification.trade_style.label} ({classification.trade_style.confidence}) — {classification.trade_style.reason}",
        f"• Intent: {classification.intent.label} ({classification.intent.confidence}) — {classification.intent.reason}",
        f"• Direction: {classification.direction.label} ({classification.direction.confidence}) — {classification.direction.reason}",
        f"• Momentum: {classification.momentum.label} ({classification.momentum.confidence}) — {classification.momentum.reason}",
        f"• Setup: {classification.setup_quality.label} ({classification.setup_quality.confidence}) — {classification.setup_quality.reason}",
        f"• Publish recommended: {classification.publish_recommended}",
    ]

    if classification.suppress_reason:
        lines.append(f"• Suppress reason: {classification.suppress_reason}")

    if alert.note:
        lines += ["", f"**Note:** {alert.note}"]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal format — embed
# ---------------------------------------------------------------------------

def _internal_embed(
    alert: FlowAlert,
    enrichment: FlowEnrichment,
    classification: FlowClassification,
    score: int,
    reasons: list[str],
) -> dict[str, Any]:
    emoji = _sentiment_emoji(alert.sentiment)
    premium_str = f"${alert.premium:,}"

    fields: list[dict] = [
        {"name": "Contract", "value": alert.contract, "inline": True},
        {"name": "Premium", "value": premium_str, "inline": True},
        {"name": "Score", "value": f"{score}/100", "inline": True},
        {"name": "Sentiment", "value": alert.sentiment.title(), "inline": True},
        {"name": "Flow Type", "value": (alert.flow_type or "—").title(), "inline": True},
        {"name": "DTE Bucket", "value": alert.dte_bucket.replace("_", " ").title(), "inline": True},
    ]

    if alert.levels:
        fields.append({"name": "Levels", "value": alert.levels, "inline": False})
    if alert.catalyst:
        fields.append({"name": "Catalyst", "value": alert.catalyst, "inline": False})

    # Enrichment block
    enrich_val = (
        f"{enrichment.moneyness_tier.replace('_',' ').upper()} | "
        f"{enrichment.dte if enrichment.dte is not None else '?'}d | "
        f"${enrichment.spot_price:,.0f} spot | "
        f"RVOL {enrichment.rvol}x ({enrichment.rvol_label})"
    )
    fields.append({"name": "Enrichment", "value": enrich_val, "inline": False})

    if enrichment.structure_notes:
        fields.append({
            "name": "Structure flags",
            "value": " · ".join(enrichment.structure_notes),
            "inline": False,
        })

    # Score reasons
    if reasons:
        fields.append({
            "name": "Score components",
            "value": "\n".join(f"• {r}" for r in reasons),
            "inline": False,
        })

    # Classification summary
    class_val = (
        f"Style: {classification.trade_style.label} ({classification.trade_style.confidence})\n"
        f"Intent: {classification.intent.label} ({classification.intent.confidence})\n"
        f"Direction: {classification.direction.label} ({classification.direction.confidence})\n"
        f"Momentum: {classification.momentum.label} ({classification.momentum.confidence})\n"
        f"Setup: {classification.setup_quality.label} ({classification.setup_quality.confidence})\n"
        f"Publish: {'✅' if classification.publish_recommended else '❌'}"
    )
    fields.append({"name": "Classification", "value": class_val, "inline": False})

    if alert.note:
        fields.append({"name": "Note", "value": alert.note, "inline": False})

    embed: dict[str, Any] = {
        "title": f"{emoji} [INTERNAL] {alert.ticker} · {score}/100",
        "color": _embed_color(alert.sentiment),
        "fields": fields,
        "footer": {"text": "options-engine internal"},
        "timestamp": _utc_now_iso(),
    }

    return {"embeds": [embed]}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def format_alert(
    alert: FlowAlert,
    enrichment: FlowEnrichment,
    classification: FlowClassification,
    score: int,
    reasons: list[str],
    mode: FormatMode = "subscriber",
    payload_type: PayloadType = "plain_text",
) -> str | dict[str, Any]:
    """
    Format an alert for Discord.

    Args:
        alert: The raw FlowAlert.
        enrichment: FlowEnrichment from enrich_alert().
        classification: FlowClassification from classify_alert().
        score: Final conviction score (0-100).
        reasons: Score component reasons list.
        mode: "subscriber" (clean) or "internal" (full debug).
        payload_type: "plain_text" returns a string.
                      "embed" returns a Discord embed dict payload.

    Returns:
        str for plain_text, dict for embed.
    """
    if mode == "subscriber":
        if payload_type == "embed":
            return _subscriber_embed(alert, enrichment, classification, score, reasons)
        return _subscriber_plain(alert, enrichment, classification, score, reasons)
    else:  # internal
        if payload_type == "embed":
            return _internal_embed(alert, enrichment, classification, score, reasons)
        return _internal_plain(alert, enrichment, classification, score, reasons)


def format_plain_text(payload: str | dict) -> str:
    """
    If payload is already a string, return it.
    If it's an embed dict, extract the title + field values as plain text fallback.
    """
    if isinstance(payload, str):
        return payload
    # Flatten embed to text
    embeds = payload.get("embeds", [])
    if not embeds:
        return str(payload)
    embed = embeds[0]
    lines = [embed.get("title", "")]
    for field in embed.get("fields", []):
        lines.append(f"**{field['name']}:** {field['value']}")
    return "\n".join(lines)
