"""
classifier.py — Phase 2 classification engine.

Takes a FlowAlert + FlowEnrichment and returns a FlowClassification with
probabilistic labels for trade structure, intent, and setup quality.

All classifications are rule-based with confidence levels.
Phase 4 can layer ML on top of these same outputs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from models import FlowAlert
from enrichment import FlowEnrichment


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Confidence = Literal["high", "medium", "low"]
TradeStyle = Literal["lotto", "swing", "unknown"]
Intent = Literal["speculative", "hedge", "unknown"]
Direction = Literal["opening", "closing", "unknown"]
Momentum = Literal["continuation", "reversal", "unknown"]
SetupQuality = Literal["actionable", "chase", "unknown"]


# ---------------------------------------------------------------------------
# Per-dimension result
# ---------------------------------------------------------------------------

class ClassificationResult(BaseModel):
    label: str
    confidence: Confidence
    reason: str


class FlowClassification(BaseModel):
    """Full classification output for a single alert."""

    ticker: str
    contract: str

    trade_style: ClassificationResult          # lotto vs swing
    intent: ClassificationResult               # speculative vs hedge
    direction: ClassificationResult            # opening vs closing
    momentum: ClassificationResult             # continuation vs reversal
    setup_quality: ClassificationResult        # actionable vs chase

    # Rolled-up summary
    tags: list[str]                            # e.g. ["swing", "speculative", "continuation"]
    summary: str                               # one-line plain English
    publish_recommended: bool                  # classifier vote on whether to post
    suppress_reason: str | None                # why classifier votes to suppress


# ---------------------------------------------------------------------------
# Individual classifiers
# ---------------------------------------------------------------------------

def _classify_trade_style(
    alert: FlowAlert,
    enrichment: FlowEnrichment,
) -> ClassificationResult:
    """
    Lotto: deep OTM + short DTE (high gamma, binary outcome).
    Swing: closer to money + longer DTE (directional position).
    """
    dte = enrichment.dte
    moneyness = enrichment.moneyness_tier

    # Strong lotto signal
    if enrichment.is_lotto:
        return ClassificationResult(
            label="lotto",
            confidence="high",
            reason=f"deep OTM ({moneyness}) with {dte}d DTE — binary gamma play",
        )

    # Near-expiry but not deep OTM
    if dte is not None and dte <= 7 and moneyness in ("otm", "atm"):
        return ClassificationResult(
            label="lotto",
            confidence="medium",
            reason=f"short DTE ({dte}d) with {moneyness} strike — elevated risk",
        )

    # Clean swing structure
    if dte is not None and dte >= 14 and moneyness in ("atm", "itm", "otm"):
        return ClassificationResult(
            label="swing",
            confidence="high",
            reason=f"{moneyness.upper()} strike with {dte}d DTE — clean swing structure",
        )

    # Moderate swing
    if dte is not None and dte >= 7 and moneyness in ("atm", "itm"):
        return ClassificationResult(
            label="swing",
            confidence="medium",
            reason=f"{moneyness.upper()} strike with {dte}d — reasonable swing window",
        )

    # Fallback from dte_bucket when DTE not parsed
    if alert.dte_bucket == "weeklies" and moneyness in ("deep_otm",):
        return ClassificationResult(
            label="lotto",
            confidence="low",
            reason="weekly contract + far OTM — likely lotto (DTE not computed)",
        )

    return ClassificationResult(
        label="swing",
        confidence="low",
        reason="insufficient data to classify confidently — defaulting to swing",
    )


def _classify_intent(
    alert: FlowAlert,
    enrichment: FlowEnrichment,
) -> ClassificationResult:
    """
    Speculative: directional bet expecting a move.
    Hedge: protection or risk management — typically puts on long stock.

    Hedges are characterised by:
    - Large OTM put blocks (not sweeps)
    - bearish puts on any underlying
    - very large premium relative to typical
    - note signals: "hedge", "protection", "collar"
    """
    note_lower = (alert.note or "").lower()
    ticker = alert.ticker.upper()
    pc = enrichment.put_call
    flow_type = (alert.flow_type or "").lower()
    moneyness = enrichment.moneyness_tier
    sentiment = alert.sentiment.lower()

    # Explicit hedge signal in note
    if any(w in note_lower for w in ("hedge", "protection", "collar", "insur")):
        return ClassificationResult(
            label="hedge",
            confidence="high",
            reason="note contains explicit hedge language",
        )

    # Classic hedge pattern: large OTM put block on any underlying
    if (
        pc == "put"
        and moneyness in ("otm", "deep_otm")
        and "block" in flow_type
        and enrichment.premium_tier in ("whale", "large")
    ):
        return ClassificationResult(
            label="hedge",
            confidence="high",
            reason=f"large OTM put block on {ticker} — institutional hedge pattern",
        )

    # Probable hedge: big bearish put sweep but could be directional
    if (
        pc == "put"
        and moneyness in ("otm", "deep_otm")
        and enrichment.premium_tier in ("whale", "large", "solid")
    ):
        return ClassificationResult(
            label="hedge",
            confidence="medium",
            reason=f"OTM put on {ticker} — possibly hedge, watch for stock offset",
        )

    # Clean speculative signal
    if sentiment in ("bullish", "bearish") and "sweep" in flow_type:
        return ClassificationResult(
            label="speculative",
            confidence="high",
            reason=f"aggressive {flow_type} with {sentiment} directional bias",
        )

    if sentiment in ("bullish", "bearish"):
        return ClassificationResult(
            label="speculative",
            confidence="medium",
            reason=f"{sentiment} directional flow — likely speculative",
        )

    return ClassificationResult(
        label="speculative",
        confidence="low",
        reason="no hedge signals detected — defaulting to speculative",
    )


def _classify_direction(
    alert: FlowAlert,
    enrichment: FlowEnrichment,
) -> ClassificationResult:
    """
    Opening: new position being established.
    Closing: existing position being unwound.

    Without live OI data, use heuristics:
    - Sweeps are almost always opening
    - Blocks can be either — size relative to OI matters (Phase 4)
    - Bought-side flow = opening
    - Sold-side flow = closing (or writing)
    - Note signals: "opening", "closing", "buy to open", "sell to close"
    """
    note_lower = (alert.note or "").lower()
    flow_type = (alert.flow_type or "").lower()

    # Explicit note signals
    if "buy to open" in note_lower or "opening" in note_lower:
        return ClassificationResult(
            label="opening",
            confidence="high",
            reason="note explicitly indicates opening transaction",
        )
    if "sell to close" in note_lower or "closing" in note_lower:
        return ClassificationResult(
            label="closing",
            confidence="high",
            reason="note explicitly indicates closing transaction",
        )

    # Sweep = almost always opening
    if "sweep" in flow_type:
        return ClassificationResult(
            label="opening",
            confidence="high",
            reason="sweeps are almost always opening transactions",
        )

    # Bought sentiment on calls or puts = opening
    if "bought" in note_lower:
        return ClassificationResult(
            label="opening",
            confidence="medium",
            reason="implied bought flow — likely opening",
        )

    # Sold sentiment = could be closing or writing
    if "sold" in note_lower:
        return ClassificationResult(
            label="closing",
            confidence="low",
            reason="implied sold flow — possibly closing or writing, unclear without OI data",
        )

    # Block with bullish/bearish signal = likely opening
    if "block" in flow_type and alert.sentiment in ("bullish", "bearish"):
        return ClassificationResult(
            label="opening",
            confidence="medium",
            reason="directional block — probable opening",
        )

    return ClassificationResult(
        label="unknown",
        confidence="low",
        reason="insufficient data to determine opening vs closing without OI comparison",
    )


def _classify_momentum(
    alert: FlowAlert,
    enrichment: FlowEnrichment,
) -> ClassificationResult:
    """
    Continuation: flow aligns with existing trend.
    Reversal: flow goes against recent trend (contrarian bet or exhaustion).

    Without live price data, use structural signals:
    - RVOL + sweep + directional = continuation
    - Deep OTM puts on extended bull run = possible reversal hedge
    - Note signals: "reversal", "fade", "top", "bottom"
    """
    note_lower = (alert.note or "").lower()
    flow_type = (alert.flow_type or "").lower()
    rvol_label = enrichment.rvol_label
    sentiment = alert.sentiment.lower()
    moneyness = enrichment.moneyness_tier

    # Explicit reversal language
    if any(w in note_lower for w in ("reversal", "fade", "top", "bottom", "contra")):
        return ClassificationResult(
            label="reversal",
            confidence="high",
            reason="note contains reversal language",
        )

    # High RVOL sweep with directional bias = momentum continuation
    if (
        rvol_label in ("extreme", "high")
        and "sweep" in flow_type
        and sentiment in ("bullish", "bearish")
    ):
        return ClassificationResult(
            label="continuation",
            confidence="high",
            reason=f"high RVOL sweep with {sentiment} bias — strong momentum signal",
        )

    # Directional sweep without RVOL confirmation
    if "sweep" in flow_type and sentiment in ("bullish", "bearish"):
        return ClassificationResult(
            label="continuation",
            confidence="medium",
            reason=f"{sentiment} sweep — likely momentum play",
        )

    # Deep OTM puts = possible reversal or tail hedge
    if moneyness == "deep_otm" and enrichment.put_call == "put":
        return ClassificationResult(
            label="reversal",
            confidence="medium",
            reason="deep OTM puts suggest reversal bet or tail-risk hedge",
        )

    return ClassificationResult(
        label="continuation",
        confidence="low",
        reason="no strong reversal signals — defaulting to continuation",
    )


def _classify_setup_quality(
    alert: FlowAlert,
    enrichment: FlowEnrichment,
    trade_style: ClassificationResult,
    intent: ClassificationResult,
) -> ClassificationResult:
    """
    Actionable: setup has structure worth trading around.
    Chase: flow is reactive, late, or lacks confirmation.

    Signals:
    - Early-day sweep with high RVOL + ATM = actionable
    - Late-day sweep into close = actionable (timing confirmation)
    - Low premium + deep OTM = chase/lotto
    - Already-moved ticker + OTM call = chase
    - Note: "late-day" or "into the close" = actionable timing
    """
    note_lower = (alert.note or "").lower()
    flow_type = (alert.flow_type or "").lower()
    rvol_label = enrichment.rvol_label
    moneyness = enrichment.moneyness_tier
    premium_tier = enrichment.premium_tier

    # Strong actionable signals
    if "into the close" in note_lower or "late-day" in note_lower:
        return ClassificationResult(
            label="actionable",
            confidence="high",
            reason="late-day flow with timing confirmation — informed money pattern",
        )

    if (
        "sweep" in flow_type
        and rvol_label in ("extreme", "high")
        and moneyness in ("atm", "itm")
        and premium_tier in ("whale", "large", "solid")
    ):
        return ClassificationResult(
            label="actionable",
            confidence="high",
            reason="high-RVOL sweep on ATM/ITM strike — clean actionable structure",
        )

    # Lotto = almost never actionable for swing traders
    if trade_style.label == "lotto" and trade_style.confidence == "high":
        return ClassificationResult(
            label="chase",
            confidence="high",
            reason="lotto structure (deep OTM short-DTE) — high risk, low edge for most traders",
        )

    # Deep OTM with small premium = chase
    if moneyness == "deep_otm" and premium_tier == "small":
        return ClassificationResult(
            label="chase",
            confidence="high",
            reason="deep OTM + small premium — low-quality speculative flow",
        )

    # Reasonable structure
    if moneyness in ("atm", "itm", "otm") and premium_tier in ("whale", "large", "solid"):
        return ClassificationResult(
            label="actionable",
            confidence="medium",
            reason=f"{moneyness.upper()} strike with {premium_tier} premium — reasonable setup",
        )

    # Hedge is actionable as informational signal
    if intent.label == "hedge" and intent.confidence in ("high", "medium"):
        return ClassificationResult(
            label="actionable",
            confidence="medium",
            reason="hedge flow is actionable as a directional signal for the market",
        )

    return ClassificationResult(
        label="chase",
        confidence="low",
        reason="setup lacks strong confirming factors",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_alert(
    alert: FlowAlert,
    enrichment: FlowEnrichment,
) -> FlowClassification:
    """
    Run all classifiers and return a full FlowClassification.

    Args:
        alert: The raw FlowAlert.
        enrichment: The FlowEnrichment produced by enrich_alert().

    Returns:
        FlowClassification with all dimensions populated.
    """
    trade_style = _classify_trade_style(alert, enrichment)
    intent = _classify_intent(alert, enrichment)
    direction = _classify_direction(alert, enrichment)
    momentum = _classify_momentum(alert, enrichment)
    setup_quality = _classify_setup_quality(alert, enrichment, trade_style, intent)

    # Build tags
    tags: list[str] = []
    for result in (trade_style, intent, direction, momentum, setup_quality):
        if result.label not in ("unknown",):
            tags.append(result.label)

    # Suppress logic — classifier vote (scoring gate is separate)
    suppress_reason: str | None = None
    publish_recommended = True

    if trade_style.label == "lotto" and trade_style.confidence == "high":
        if enrichment.premium_tier not in ("whale", "large"):
            suppress_reason = "lotto structure with insufficient premium to justify alert"
            publish_recommended = False

    if setup_quality.label == "chase" and setup_quality.confidence == "high":
        if intent.label != "hedge":
            suppress_reason = "chase setup with no hedge context — suppressing to reduce noise"
            publish_recommended = False

    # One-line summary
    style_str = trade_style.label
    intent_str = intent.label
    momentum_str = momentum.label
    quality_str = setup_quality.label

    summary = (
        f"{alert.ticker} | {style_str} {intent_str} | "
        f"{momentum_str} momentum | {quality_str} setup"
    )
    if enrichment.moneyness_tier != "unknown":
        summary += f" | {enrichment.moneyness_tier.replace('_', ' ').upper()}"
    if enrichment.dte is not None:
        summary += f" | {enrichment.dte}d DTE"

    return FlowClassification(
        ticker=alert.ticker,
        contract=alert.contract,
        trade_style=trade_style,
        intent=intent,
        direction=direction,
        momentum=momentum,
        setup_quality=setup_quality,
        tags=tags,
        summary=summary,
        publish_recommended=publish_recommended,
        suppress_reason=suppress_reason,
    )


def classification_tag_line(c: FlowClassification) -> str:
    """
    Return a compact tag string for Discord embeds.
    Example: "[swing] [speculative] [opening] [continuation] [actionable]"
    """
    return " ".join(f"[{t}]" for t in c.tags)
