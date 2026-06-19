"""
mcp_server.py — Phase 1 MCP server for the options-discord-engine.

Exposes the existing engine functionality as MCP tools that Claude (or any
MCP client) can call directly. This file is purely additive — it imports
from main.py and scoring.py without modifying them.

Run alongside main.py (they share the same DB and .env):
    python mcp_server.py          # stdio transport (Claude Desktop / Claude Code)
    python mcp_server.py --sse    # SSE transport (web clients, port 8001)

Connect to Claude Desktop by adding to claude_desktop_config.json:
    {
      "mcpServers": {
        "options-engine": {
          "command": "python",
          "args": ["/path/to/mcp_server.py"],
          "cwd": "/path/to/project"
        }
      }
    }
"""

import asyncio
import hashlib
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

from models import FlowAlert
from scoring import auto_score_alert
from flow_filters import filter_flow_items, is_high_conviction
from market_hours import is_market_open, market_closed_reason
from enrichment import enrich_alert, enrichment_summary
from classifier import classify_alert, classification_tag_line
from formatter import format_alert, format_plain_text
from db import (
    init_all_tables,
    is_postgres,
    alert_hash_exists_in_window,
    save_published_alert,
    get_recent_published_alerts,
    get_published_alert_count,
    save_flow_event_row,
    save_classification_row,
    create_outcome_row,
    get_outcome_row,
    update_outcome_horizons,
    get_unresolved_alerts,
    get_outcome_stats,
)
from outcomes import record_horizon_outcome, HORIZONS

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [mcp] %(message)s",
)
logger = logging.getLogger("options-mcp")

# ---------------------------------------------------------------------------
# Config (mirrors main.py — single source of truth is .env)
# ---------------------------------------------------------------------------

DISCORD_WEBHOOK_URL: str | None = os.getenv("DISCORD_WEBHOOK_URL")
MIN_ALERT_SCORE: int = int(os.getenv("MIN_ALERT_SCORE", "70"))
JARVIS_API_KEY: str | None = os.getenv("JARVIS_API_KEY")
JARVIS_MCP_URL: str = "https://api.jarvisflow.io/.well-known/mcp"
DB_PATH: str = os.getenv("DB_PATH", "alerts.db")
DEDUPE_WINDOW_MINUTES: int = int(os.getenv("DEDUPE_WINDOW_MINUTES", "30"))
AUTO_POLL_TICKERS: list[str] = [
    t.strip().upper()
    for t in os.getenv("AUTO_POLL_TICKERS", "SPY").split(",")
    if t.strip()
]

# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="options-discord-engine",
    instructions=(
        "Options flow intelligence engine. Use these tools to score, inspect, "
        "and publish high-conviction options alerts to Discord. "
        "Always call get_engine_status first to confirm the engine is configured."
    ),
)

# ---------------------------------------------------------------------------
# Internal helpers (self-contained so mcp_server.py runs standalone)
# ---------------------------------------------------------------------------


def _alert_hash(alert: FlowAlert):
    raw = f"{alert.ticker}|{alert.contract}|{alert.source}|{alert.flow_type}|{alert.sentiment}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _is_duplicate(alert: FlowAlert, window_minutes: int = DEDUPE_WINDOW_MINUTES) -> bool:
    return alert_hash_exists_in_window(_alert_hash(alert), window_minutes)


def _save_alert(alert: FlowAlert, score: int):
    """
    Save a published alert via db.py's adapter — respects Postgres/SQLite
    automatically. (Previously this opened its own raw sqlite3 connection
    directly to DB_PATH, which bypassed the Postgres adapter entirely and
    would silently write to the wrong database. That helper has been removed.)
    """
    save_published_alert(
        alert_hash=_alert_hash(alert),
        ticker=alert.ticker,
        contract=alert.contract,
        source=alert.source,
        score=score,
    )


def _build_discord_message(alert: FlowAlert, score: int, reasons):
    emoji = {"bullish": "🟢", "bearish": "🔴"}.get(alert.sentiment.lower(), "🟡")
    premium_str = f"${alert.premium:,}"
    lines = [
        f"{emoji} **{alert.ticker} {alert.source.title()} Alert**",
        "",
        f"**Time:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
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
    if reasons:
        lines += ["", "**Why it passed:**"] + [f"• {r}" for r in reasons]
    if alert.note:
        lines += ["", f"**Note:** {alert.note}"]
    return "\n".join(lines)


async def _post_discord(message: str):
    if not DISCORD_WEBHOOK_URL:
        return False
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(DISCORD_WEBHOOK_URL, json={"content": message})
    return r.status_code in (200, 204)


async def _fetch_jarvis(ticker: str):
    """Pull raw flow items from JarvisFlow MCP endpoint."""
    if not JARVIS_API_KEY:
        raise ValueError("JARVIS_API_KEY not set in .env")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "stock_ticker_unusual_options_data",
            "arguments": {"filter_by_Ticker": ticker},
        },
    }
    headers = {
        "Authorization": f"Bearer {JARVIS_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(JARVIS_MCP_URL, json=payload, headers=headers)
    resp.raise_for_status()

    for line in resp.text.splitlines():
        if line.startswith("data:"):
            data = json.loads(line[5:].strip())
            content = data.get("result", {}).get("content", [])
            if content and content[0].get("type") == "text":
                inner = json.loads(content[0]["text"])
                tool_result = inner.get("toolResult", inner)
                if isinstance(tool_result, dict):
                    return tool_result.get("optionsFlow", [])
                if isinstance(tool_result, list):
                    return tool_result
    return []


def _jarvis_to_alert(item: dict):
    ticker = item.get("ticker", "").upper()
    strike = item.get("strike_Price", item.get("strikePrice", ""))
    expiry = item.get("expriation_Date", item.get("expriationDate", ""))
    put_call = item.get("put_Or_Call", item.get("putOrCall", "")).upper()
    sweep_block = item.get("sweep_Or_Block", item.get("sweepOrBlock", "")).upper()
    bought_sold = item.get("implied_Bought_Or_Sold", item.get("impliedBoughtOrSold", "")).upper()
    premium = int(float(item.get("total_Option_Premium_For_Trade",
                                  item.get("totalOptionPremiumForTrade", 0)) or 0))
    spot_price_raw = item.get("spot_Price", item.get("spotPrice"))
    spot_price = float(spot_price_raw) if spot_price_raw is not None else None
    volume_raw = item.get("volume_When_Traded", item.get("volumeWhenTraded"))
    volume = int(volume_raw) if volume_raw is not None else None
    oi_raw = item.get("open_Interest_When_Traded", item.get("openInterestWhenTraded"))
    open_interest = int(oi_raw) if oi_raw is not None else None
    contract_price_raw = item.get("price_Of_Contract", item.get("priceOfContract"))
    contract_price = float(contract_price_raw) if contract_price_raw is not None else None

    contract = (
        f"{ticker} {expiry[:10]} {strike}{put_call[:1]}"
        if expiry and strike and put_call
        else ticker
    )

    if put_call == "CALL" and bought_sold == "BOUGHT":
        sentiment = "bullish"
    elif put_call == "PUT" and bought_sold == "SOLD":
        sentiment = "bullish"
    elif put_call == "PUT" and bought_sold == "BOUGHT":
        sentiment = "bearish"
    elif put_call == "CALL" and bought_sold == "SOLD":
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    conviction = item.get("interpreted_Conviction", item.get("interpretedConviction", ""))
    moneyness = item.get("moneyNess", "")
    note = f"JarvisFlow | {bought_sold} {put_call} | {moneyness} | Conviction: {conviction}"

    return FlowAlert(
        ticker=ticker,
        contract=contract,
        premium=premium,
        sentiment=sentiment,
        source="flow",
        dte_bucket="weeklies",
        flow_type=sweep_block.lower() if sweep_block else None,
        note=note,
        spot_price=spot_price,
        volume=volume,
        open_interest=open_interest,
        contract_price=contract_price,
    )


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_engine_status() -> dict[str, Any]:
    """
    Return the current configuration and health of the options-discord-engine.

    Includes DB stats, score threshold, dedup window, configured tickers,
    and whether Discord and JarvisFlow are properly configured.
    Call this first to confirm the engine is ready before running other tools.
    """
    db_ok = False
    alert_count = 0
    recent_alerts = []
    try:
        alert_count = get_published_alert_count()
        recent_alerts = get_recent_published_alerts(limit=5)
        db_ok = True
    except Exception as e:
        logger.warning("DB check failed: %s", e)

    return {
        "status": "ok",
        "engine": "options-discord-engine",
        "phase": "3 — subscriber formatter + outcome tracking active",
        "config": {
            "min_alert_score": MIN_ALERT_SCORE,
            "dedupe_window_minutes": DEDUPE_WINDOW_MINUTES,
            "auto_poll_tickers": AUTO_POLL_TICKERS,
            "db_path": DB_PATH,
        },
        "integrations": {
            "discord_configured": bool(DISCORD_WEBHOOK_URL),
            "jarvis_configured": bool(JARVIS_API_KEY),
        },
        "database": {
            "ok": db_ok,
            "total_published_alerts": alert_count,
            "recent_alerts": recent_alerts,
        },
    }


@mcp.tool()
def score_alert(
    ticker: str,
    contract: str,
    premium: int,
    sentiment: str,
    source: str = "flow",
    dte_bucket: str = "weeklies",
    flow_type: Optional[str] = None,
    catalyst: Optional[str] = None,
    levels: Optional[str] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """
    Score a single options flow alert without posting it anywhere.

    Use this to test whether a flow event would pass the publish threshold,
    understand which scoring components fired, and tune the engine's sensitivity.

    Args:
        ticker: Underlying symbol, e.g. "SPY", "AAPL"
        contract: Full contract string, e.g. "SPY 2025-06-20 600C"
        premium: Total premium in dollars, e.g. 250000
        sentiment: "bullish", "bearish", or "neutral"
        source: "flow", "news", "earnings", "scanner", or "macro"
        dte_bucket: "weeklies", "next_week", "monthly", or "unknown"
        flow_type: "sweep", "block", or None
        catalyst: Optional catalyst description
        levels: Optional key technical levels string
        note: Optional freeform note
    """
    valid_sentiments = {"bullish", "bearish", "neutral"}
    valid_sources = {"flow", "news", "earnings", "scanner", "macro"}
    valid_dte = {"weeklies", "next_week", "monthly", "unknown"}

    if sentiment not in valid_sentiments:
        return {"ok": False, "error": f"sentiment must be one of {valid_sentiments}"}
    if source not in valid_sources:
        return {"ok": False, "error": f"source must be one of {valid_sources}"}
    if dte_bucket not in valid_dte:
        return {"ok": False, "error": f"dte_bucket must be one of {valid_dte}"}

    alert = FlowAlert(
        ticker=ticker.upper(),
        contract=contract,
        premium=premium,
        sentiment=sentiment,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        dte_bucket=dte_bucket,  # type: ignore[arg-type]
        flow_type=flow_type,
        catalyst=catalyst,
        levels=levels,
        note=note,
    )

    final_score, reasons = auto_score_alert(alert)

    return {
        "ok": True,
        "ticker": ticker.upper(),
        "contract": contract,
        "score": final_score,
        "passes_threshold": final_score >= MIN_ALERT_SCORE,
        "min_alert_score": MIN_ALERT_SCORE,
        "score_reasons": reasons,
        "score_breakdown": {
            "base": 35,
            "components_fired": len(reasons),
            "final": final_score,
        },
        "alert_payload": alert.model_dump(),
    }


@mcp.tool()
def list_recent_alerts(
    limit: int = 20,
    ticker: Optional[str] = None,
    min_score: Optional[int] = None,
) -> dict[str, Any]:
    """
    List recently published alerts from the database.

    Args:
        limit: Max number of alerts to return (default 20, max 100)
        ticker: Optional filter by ticker symbol
        min_score: Optional filter by minimum score
    """
    limit = min(limit, 100)

    try:
        alerts = get_recent_published_alerts(limit=limit, ticker=ticker, min_score=min_score)
        total = get_published_alert_count()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "returned": len(alerts),
        "total_in_db": total,
        "filters": {"ticker": ticker, "min_score": min_score},
        "alerts": alerts,
    }


@mcp.tool()
async def pull_and_score_ticker(
    ticker: str = "SPY",
    limit: int = 10,
    dry_run: bool = False,
    format_mode: str = "subscriber",
) -> dict[str, Any]:
    """
    Pull live options flow for a ticker from JarvisFlow, apply the basic
    flow filters (DTE 0-14, ATM/OTM only, BOUGHT only), then run each
    surviving event through the full Phase 3 pipeline — enrich → classify →
    score → gate → format → post → save — same as ingest_and_enrich_v2.

    Args:
        ticker: Underlying symbol to pull flow for, e.g. "SPY", "QQQ", "NVDA"
        limit: Max number of filtered flow items to process (default 10, max 50)
        dry_run: If True, score/format everything but do NOT post to Discord.
        format_mode: "subscriber" or "internal"
    """
    limit = min(limit, 50)
    ticker = ticker.upper()
    if format_mode not in {"subscriber", "internal"}:
        return {"ok": False, "error": "format_mode must be 'subscriber' or 'internal'"}

    if not dry_run:
        closed_reason = market_closed_reason()
        if closed_reason:
            return {
                "ok": True, "ticker": ticker, "posted": False,
                "skipped_market_closed": True, "reason": closed_reason,
            }

    try:
        flow_items = await _fetch_jarvis(ticker)
    except Exception as e:
        return {"ok": False, "ticker": ticker, "error": str(e)}

    if not flow_items:
        return {"ok": False, "ticker": ticker, "error": "No flow items returned from JarvisFlow"}

    filtered_items, skipped_filter = filter_flow_items(flow_items)

    results = []
    summary = {
        "checked": 0, "posted": 0, "skipped_score": 0, "skipped_classifier": 0,
        "skipped_dedupe": 0, "skipped_discord_error": 0, "skipped_dry_run": 0,
    }

    for item in filtered_items[:limit]:
        alert = _jarvis_to_alert(item)
        enrichment = enrich_alert(alert)
        classification = classify_alert(alert, enrichment)
        score, reasons = auto_score_alert(alert)
        high_conviction = is_high_conviction(item)
        summary["checked"] += 1
        alert_hash = _alert_hash(alert)

        result: dict[str, Any] = {
            "ticker": alert.ticker, "contract": alert.contract,
            "premium": alert.premium, "sentiment": alert.sentiment,
            "score": score, "passes_threshold": score >= MIN_ALERT_SCORE,
            "score_reasons": reasons, "high_conviction": high_conviction,
            "enrichment_summary": enrichment_summary(enrichment),
            "classification_summary": classification.summary,
            "action": None,
        }

        if score < MIN_ALERT_SCORE and not (high_conviction and score >= 65):
            result["action"] = f"skipped — score {score} < threshold {MIN_ALERT_SCORE}"
            summary["skipped_score"] += 1
            results.append(result)
            continue

        if not classification.publish_recommended and not (high_conviction and score >= 65):
            result["action"] = f"skipped — classifier: {classification.suppress_reason}"
            summary["skipped_classifier"] += 1
            results.append(result)
            continue

        if _is_duplicate(alert):
            result["action"] = f"skipped — duplicate within {DEDUPE_WINDOW_MINUTES}min window"
            summary["skipped_dedupe"] += 1
            results.append(result)
            continue

        message = format_plain_text(format_alert(
            alert, enrichment, classification, score, reasons,
            mode=format_mode,  # type: ignore[arg-type]
            payload_type="plain_text",
        ))

        if dry_run:
            result["action"] = "dry_run — would post to Discord"
            result["discord_preview"] = message
            summary["skipped_dry_run"] += 1
            results.append(result)
            continue

        posted = await _post_discord(message)

        if posted:
            _save_alert(alert, score)
            event_id = save_flow_event_row(
                alert_hash=alert_hash, ticker=alert.ticker, contract=alert.contract,
                premium=alert.premium, sentiment=alert.sentiment, source=alert.source,
                dte_bucket=alert.dte_bucket, flow_type=alert.flow_type,
                catalyst=alert.catalyst, levels=alert.levels, note=alert.note,
                enrichment=enrichment, score=score, score_reasons=reasons,
                passed_threshold=True, passed_dedup=True, was_published=True,
            )
            save_classification_row(event_id, alert_hash, classification)
            try:
                create_outcome_row(
                    alert_hash=alert_hash, ticker=alert.ticker, contract=alert.contract,
                    sentiment=alert.sentiment, score=score, premium=alert.premium,
                    trade_style=classification.trade_style.label,
                    intent=classification.intent.label,
                    setup_quality=classification.setup_quality.label,
                    moneyness=enrichment.moneyness_tier, flow_type=alert.flow_type,
                )
            except Exception as e:
                logger.warning("outcome record failed for %s %s: %s", alert.ticker, alert.contract, e)
            result["action"] = "posted to Discord ✅"
            summary["posted"] += 1
        else:
            result["action"] = "Discord post failed ❌"
            summary["skipped_discord_error"] += 1

        results.append(result)

    return {
        "ok": True, "ticker": ticker, "dry_run": dry_run, "format_mode": format_mode,
        "flow_items_available": len(flow_items),
        "flow_items_after_filter": len(filtered_items),
        "skipped_filter": skipped_filter,
        "summary": summary, "results": results,
    }


@mcp.tool()
async def ingest_flow_alert(
    ticker: str,
    contract: str,
    premium: int,
    sentiment: str,
    source: str = "flow",
    dte_bucket: str = "weeklies",
    flow_type: Optional[str] = None,
    catalyst: Optional[str] = None,
    levels: Optional[str] = None,
    note: Optional[str] = None,
    force_post: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Manually push a single flow alert through the basic pipeline:
    score → dedup check → Discord post → DB save.

    For the full Phase 3 pipeline with subscriber formatting, use ingest_and_enrich_v2.
    """
    valid_sentiments = {"bullish", "bearish", "neutral"}
    valid_sources = {"flow", "news", "earnings", "scanner", "macro"}
    valid_dte = {"weeklies", "next_week", "monthly", "unknown"}

    if sentiment not in valid_sentiments:
        return {"ok": False, "error": f"sentiment must be one of {valid_sentiments}"}
    if source not in valid_sources:
        return {"ok": False, "error": f"source must be one of {valid_sources}"}
    if dte_bucket not in valid_dte:
        return {"ok": False, "error": f"dte_bucket must be one of {valid_dte}"}

    alert = FlowAlert(
        ticker=ticker.upper(), contract=contract, premium=premium,
        sentiment=sentiment,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        dte_bucket=dte_bucket,  # type: ignore[arg-type]
        flow_type=flow_type, catalyst=catalyst, levels=levels, note=note,
    )

    score, reasons = auto_score_alert(alert)
    pipeline_log = [f"scored {score}/100 ({len(reasons)} components fired)"]

    if not force_post and score < MIN_ALERT_SCORE:
        pipeline_log.append(f"blocked — score {score} < threshold {MIN_ALERT_SCORE}")
        return {
            "ok": True, "posted": False, "score": score,
            "score_reasons": reasons, "pipeline_log": pipeline_log,
            "reason": f"Score {score} below threshold {MIN_ALERT_SCORE}. Use force_post=True to override.",
        }

    if force_post:
        pipeline_log.append("threshold bypassed (force_post=True)")

    if _is_duplicate(alert):
        pipeline_log.append(f"blocked — duplicate within {DEDUPE_WINDOW_MINUTES}min window")
        return {
            "ok": True, "posted": False, "score": score,
            "score_reasons": reasons, "pipeline_log": pipeline_log,
            "reason": f"Duplicate alert seen within {DEDUPE_WINDOW_MINUTES} minutes.",
        }

    pipeline_log.append("dedup check passed")

    if not dry_run:
        closed_reason = market_closed_reason()
        if closed_reason:
            return {
                "ok": True, "posted": False, "score": score,
                "score_reasons": reasons, "pipeline_log": pipeline_log,
                "reason": f"market closed — {closed_reason}",
            }

    message = _build_discord_message(alert, score, reasons)

    if dry_run:
        pipeline_log.append("dry_run=True — skipping Discord post and DB save")
        return {
            "ok": True, "posted": False, "dry_run": True,
            "score": score, "score_reasons": reasons,
            "pipeline_log": pipeline_log, "discord_preview": message,
        }

    if not DISCORD_WEBHOOK_URL:
        return {"ok": False, "posted": False, "score": score,
                "pipeline_log": pipeline_log, "error": "DISCORD_WEBHOOK_URL not set in .env"}

    posted = await _post_discord(message)

    if posted:
        _save_alert(alert, score)
        pipeline_log.append("posted to Discord ✅")
        pipeline_log.append("saved to DB ✅")
    else:
        pipeline_log.append("Discord post failed ❌ — not saved to DB")

    return {
        "ok": posted, "posted": posted, "score": score,
        "score_reasons": reasons, "pipeline_log": pipeline_log,
        "discord_preview": message, "alert_payload": alert.model_dump(),
    }


@mcp.tool()
def explain_score(
    ticker: str,
    contract: str,
    premium: int,
    sentiment: str,
    source: str = "flow",
    dte_bucket: str = "weeklies",
    flow_type: Optional[str] = None,
    catalyst: Optional[str] = None,
    levels: Optional[str] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """
    Produce a detailed human-readable explanation of how an alert would be scored.
    Returns score breakdown, conviction label, and plain-English summary.
    """
    valid_sentiments = {"bullish", "bearish", "neutral"}
    valid_sources = {"flow", "news", "earnings", "scanner", "macro"}
    valid_dte = {"weeklies", "next_week", "monthly", "unknown"}

    if sentiment not in valid_sentiments:
        return {"ok": False, "error": f"sentiment must be one of {valid_sentiments}"}
    if source not in valid_sources:
        return {"ok": False, "error": f"source must be one of {valid_sources}"}
    if dte_bucket not in valid_dte:
        return {"ok": False, "error": f"dte_bucket must be one of {valid_dte}"}

    alert = FlowAlert(
        ticker=ticker.upper(), contract=contract, premium=premium,
        sentiment=sentiment,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        dte_bucket=dte_bucket,  # type: ignore[arg-type]
        flow_type=flow_type, catalyst=catalyst, levels=levels, note=note,
    )

    score, reasons = auto_score_alert(alert)

    if score >= 80:
        conviction = "high"
        conviction_note = "Strong multi-factor conviction. Good candidate for Discord."
    elif score >= 65:
        conviction = "medium"
        conviction_note = "Decent setup but missing 1-2 confirming factors."
    elif score >= 50:
        conviction = "low"
        conviction_note = "Weak signal. Would not pass default threshold."
    else:
        conviction = "very low"
        conviction_note = "Multiple factors missing. Likely noise or hedge."

    breakdown_lines = ["Base score:    35 pts  (starting floor)"]
    p = alert.premium
    if p >= 500_000:
        breakdown_lines.append("Premium:       +18 pts (very large ≥$500k)")
    elif p >= 250_000:
        breakdown_lines.append("Premium:       +14 pts (large ≥$250k)")
    elif p >= 100_000:
        breakdown_lines.append("Premium:       +10 pts (solid ≥$100k)")
    elif p >= 50_000:
        breakdown_lines.append("Premium:        +5 pts (decent ≥$50k)")
    else:
        breakdown_lines.append("Premium:        +0 pts (below $50k)")

    sent = alert.sentiment.lower()
    if sent in ("bullish", "bearish"):
        breakdown_lines.append(f"Sentiment:      +5 pts ({sent} directional)")
    else:
        breakdown_lines.append("Sentiment:      +0 pts (neutral)")

    src_pts = {"flow": 6, "scanner": 5, "news": 3, "earnings": 4, "macro": 2}.get(source, 0)
    breakdown_lines.append(f"Source ({source}): +{src_pts} pts")
    dte_pts = {"weeklies": 6, "next_week": 4, "monthly": 1, "unknown": 0}.get(dte_bucket, 0)
    breakdown_lines.append(f"DTE ({dte_bucket}): +{dte_pts} pts")

    if catalyst:
        breakdown_lines.append("Catalyst:       +5 pts")
    if levels:
        breakdown_lines.append("Levels:         +5 pts")
    if flow_type:
        ft = flow_type.lower()
        if "sweep" in ft:
            breakdown_lines.append("Flow type:      +6 pts (sweep)")
        elif "block" in ft:
            breakdown_lines.append("Flow type:      +4 pts (block)")
    if note:
        n = note.lower()
        note_pts = 0
        if "heavy" in n:
            note_pts += 2
        if "late-day" in n or "into the close" in n:
            note_pts += 3
        if note_pts:
            breakdown_lines.append(f"Note signals:   +{note_pts} pts")

    breakdown_lines += ["─────────────────────────", f"TOTAL:          {score}/100"]

    return {
        "ok": True, "score": score, "conviction": conviction,
        "conviction_note": conviction_note,
        "passes_threshold": score >= MIN_ALERT_SCORE,
        "min_alert_score": MIN_ALERT_SCORE,
        "score_reasons": reasons,
        "breakdown": "\n".join(breakdown_lines),
        "summary": (
            f"{ticker.upper()} {contract} scores {score}/100 — conviction is {conviction}. "
            f"{conviction_note} "
            f"{'Passes' if score >= MIN_ALERT_SCORE else 'Does NOT pass'} the "
            f"current publish threshold of {MIN_ALERT_SCORE}."
        ),
    }


# ---------------------------------------------------------------------------
# MCP Resources
# ---------------------------------------------------------------------------


@mcp.resource("resource://scoring-rubric")
def scoring_rubric() -> str:
    """The full scoring rubric used by the conviction engine."""
    return """
# Options Flow Conviction Scoring Rubric

## Base Score
All alerts start at 35 points.

## Premium Size (max +18)
- ≥$500k:  +18 pts — very large premium, institutional scale
- ≥$250k:  +14 pts — large premium
- ≥$100k:  +10 pts — solid premium
- ≥$50k:   + 5 pts — decent premium
- <$50k:   + 0 pts — below threshold

## Sentiment (+5)
- Bullish or Bearish: +5 pts — directional conviction
- Neutral:            +0 pts

## Source (+2 to +6)
- flow:     +6 pts — raw options flow (highest signal)
- scanner:  +5 pts — scanner-confirmed
- earnings: +4 pts — earnings-driven
- news:     +3 pts — news-backed
- macro:    +2 pts — macro-backed

## DTE Bucket (+0 to +6)
- weeklies:   +6 pts — short-dated, high gamma
- next_week:  +4 pts — near-term
- monthly:    +1 pt  — longer dated
- unknown:    +0 pts

## Catalyst (+5), Levels (+5), Flow Type (+0 to +6)
- sweep: +6 pts, block: +4 pts

## Note Signals (+0 to +5)
- "heavy": +2 pts, "late-day" / "into the close": +3 pts

## Conviction Labels
- 80–100: High, 65–79: Medium, 50–64: Low, 0–49: Very Low
"""


@mcp.resource("resource://alert-style-guide")
def alert_style_guide() -> str:
    """Style guide for Discord alert formatting and tone."""
    return """
# Discord Alert Style Guide

## Subscriber Format (use for paying members)
- Clean, trader-focused, no internal engine language
- Header: emoji + ticker + flow type + catalyst if present
- Contract short form: $130C · Jul 18 · Monthly
- Premium in K/M format: $380K not $380,000
- Conviction label + score on one line
- One-line setup summary (swing / continuation / actionable)
- RVOL only shown if elevated (high or extreme)
- No "Why it passed" bullets — subscribers don't need engine internals

## Internal Format (use for ops/debug channel)
- Full score component breakdown
- All classification dimensions with confidence and reason
- Full enrichment block
- Note field verbatim

## Emojis: 🟢 Bullish · 🔴 Bearish · 🟡 Neutral
## Tone: Direct, present tense, confident — no "potential" or "maybe"
"""


@mcp.resource("resource://phase-guide")
def phase_guide() -> str:
    """Current phase status and tool reference."""
    return """
# options-discord-engine — Phase Guide

## Phase 1 ✅ — MCP layer
Tools: get_engine_status, score_alert, list_recent_alerts,
       pull_and_score_ticker, ingest_flow_alert, explain_score

## Phase 2 ✅ — Enrichment + Classification
Tools: enrich_alert_tool, classify_alert_tool, ingest_and_enrich

## Phase 3 ✅ — Subscriber Formatter + Outcome Tracking
Tools: ingest_and_enrich_v2 (PRIMARY — use this for live posts)
       preview_alert_format, record_outcome,
       list_alerts_needing_outcomes, get_signal_quality_report

## Primary tool for live posting: ingest_and_enrich_v2
## Primary tool for previewing: preview_alert_format
## End of day workflow: list_alerts_needing_outcomes → record_outcome
"""


# ---------------------------------------------------------------------------
# Phase 2 Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def enrich_alert_tool(
    ticker: str,
    contract: str,
    premium: int,
    sentiment: str,
    source: str = "flow",
    dte_bucket: str = "weeklies",
    flow_type: Optional[str] = None,
    catalyst: Optional[str] = None,
    levels: Optional[str] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """
    Enrich a flow alert with computed context: DTE, moneyness, RVOL, premium tier,
    and trade structure signals. Currently uses mock spot prices for common tickers.
    Phase 4 will replace with live market data.
    """
    valid_sentiments = {"bullish", "bearish", "neutral"}
    valid_sources = {"flow", "news", "earnings", "scanner", "macro"}
    valid_dte = {"weeklies", "next_week", "monthly", "unknown"}

    if sentiment not in valid_sentiments:
        return {"ok": False, "error": f"sentiment must be one of {valid_sentiments}"}
    if source not in valid_sources:
        return {"ok": False, "error": f"source must be one of {valid_sources}"}
    if dte_bucket not in valid_dte:
        return {"ok": False, "error": f"dte_bucket must be one of {valid_dte}"}

    alert = FlowAlert(
        ticker=ticker.upper(), contract=contract, premium=premium,
        sentiment=sentiment,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        dte_bucket=dte_bucket,  # type: ignore[arg-type]
        flow_type=flow_type, catalyst=catalyst, levels=levels, note=note,
    )

    enrichment = enrich_alert(alert)
    return {"ok": True, "summary": enrichment_summary(enrichment), "enrichment": enrichment.model_dump()}


@mcp.tool()
def classify_alert_tool(
    ticker: str,
    contract: str,
    premium: int,
    sentiment: str,
    source: str = "flow",
    dte_bucket: str = "weeklies",
    flow_type: Optional[str] = None,
    catalyst: Optional[str] = None,
    levels: Optional[str] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """
    Run the full classification pipeline: trade_style, intent, direction,
    momentum, setup_quality. Each dimension includes confidence and reason.
    """
    valid_sentiments = {"bullish", "bearish", "neutral"}
    valid_sources = {"flow", "news", "earnings", "scanner", "macro"}
    valid_dte = {"weeklies", "next_week", "monthly", "unknown"}

    if sentiment not in valid_sentiments:
        return {"ok": False, "error": f"sentiment must be one of {valid_sentiments}"}
    if source not in valid_sources:
        return {"ok": False, "error": f"source must be one of {valid_sources}"}
    if dte_bucket not in valid_dte:
        return {"ok": False, "error": f"dte_bucket must be one of {valid_dte}"}

    alert = FlowAlert(
        ticker=ticker.upper(), contract=contract, premium=premium,
        sentiment=sentiment,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        dte_bucket=dte_bucket,  # type: ignore[arg-type]
        flow_type=flow_type, catalyst=catalyst, levels=levels, note=note,
    )

    enrichment = enrich_alert(alert)
    classification = classify_alert(alert, enrichment)

    return {
        "ok": True, "ticker": ticker.upper(), "contract": contract,
        "enrichment_summary": enrichment_summary(enrichment),
        "classification": {
            "summary": classification.summary,
            "tags": classification.tags,
            "tag_line": classification_tag_line(classification),
            "publish_recommended": classification.publish_recommended,
            "suppress_reason": classification.suppress_reason,
            "dimensions": {
                "trade_style": classification.trade_style.model_dump(),
                "intent": classification.intent.model_dump(),
                "direction": classification.direction.model_dump(),
                "momentum": classification.momentum.model_dump(),
                "setup_quality": classification.setup_quality.model_dump(),
            },
        },
    }


@mcp.tool()
async def ingest_and_enrich(
    ticker: str,
    contract: str,
    premium: int,
    sentiment: str,
    source: str = "flow",
    dte_bucket: str = "weeklies",
    flow_type: Optional[str] = None,
    catalyst: Optional[str] = None,
    levels: Optional[str] = None,
    note: Optional[str] = None,
    dry_run: bool = False,
    force_post: bool = False,
    override_classifier: bool = False,
) -> dict[str, Any]:
    """
    Phase 2 pipeline: enrich → classify → score → gate → post → save.
    Uses the internal/debug Discord format. For subscriber-formatted posts use ingest_and_enrich_v2.
    """
    valid_sentiments = {"bullish", "bearish", "neutral"}
    valid_sources = {"flow", "news", "earnings", "scanner", "macro"}
    valid_dte = {"weeklies", "next_week", "monthly", "unknown"}

    if sentiment not in valid_sentiments:
        return {"ok": False, "error": f"sentiment must be one of {valid_sentiments}"}
    if source not in valid_sources:
        return {"ok": False, "error": f"source must be one of {valid_sources}"}
    if dte_bucket not in valid_dte:
        return {"ok": False, "error": f"dte_bucket must be one of {valid_dte}"}

    alert = FlowAlert(
        ticker=ticker.upper(), contract=contract, premium=premium,
        sentiment=sentiment,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        dte_bucket=dte_bucket,  # type: ignore[arg-type]
        flow_type=flow_type, catalyst=catalyst, levels=levels, note=note,
    )

    pipeline_log = []
    enrichment = enrich_alert(alert)
    pipeline_log.append(f"enriched: {enrichment_summary(enrichment)}")
    classification = classify_alert(alert, enrichment)
    pipeline_log.append(f"classified: {classification.summary}")
    score, score_reasons = auto_score_alert(alert)
    pipeline_log.append(f"scored {score}/100 ({len(score_reasons)} components)")

    alert_hash = _alert_hash(alert)

    if not force_post and score < MIN_ALERT_SCORE:
        suppress_reason = f"score {score} < threshold {MIN_ALERT_SCORE}"
        pipeline_log.append(f"blocked by score gate: {suppress_reason}")
        return {
            "ok": True, "posted": False, "score": score,
            "pipeline_log": pipeline_log, "suppress_reason": suppress_reason,
        }
    pipeline_log.append("score gate passed ✅")

    if not override_classifier and not classification.publish_recommended:
        suppress_reason = classification.suppress_reason
        pipeline_log.append(f"blocked by classifier: {suppress_reason}")
        return {
            "ok": True, "posted": False, "score": score,
            "pipeline_log": pipeline_log, "suppress_reason": suppress_reason,
        }
    pipeline_log.append("classifier gate passed ✅")

    if _is_duplicate(alert):
        suppress_reason = f"duplicate within {DEDUPE_WINDOW_MINUTES}min window"
        pipeline_log.append(f"blocked by dedup: {suppress_reason}")
        return {
            "ok": True, "posted": False, "score": score,
            "pipeline_log": pipeline_log, "suppress_reason": suppress_reason,
        }
    pipeline_log.append("dedup gate passed ✅")

    if not dry_run:
        closed_reason = market_closed_reason()
        if closed_reason:
            pipeline_log.append(f"blocked — market closed ({closed_reason})")
            return {
                "ok": True, "posted": False, "score": score,
                "pipeline_log": pipeline_log, "suppress_reason": f"market closed — {closed_reason}",
            }

    enrich_line = enrichment_summary(enrichment)
    tag_line = classification_tag_line(classification)
    message = _build_discord_message(alert, score, score_reasons)
    message += (
        f"\n**Context:** {enrich_line}"
        f"\n**Classification:** {tag_line}"
    )
    if enrichment.structure_notes:
        message += f"\n**Structure:** {' · '.join(enrichment.structure_notes)}"

    if dry_run:
        pipeline_log.append("dry_run=True — skipping Discord post and DB save")
        return {
            "ok": True, "posted": False, "dry_run": True,
            "score": score, "score_reasons": score_reasons,
            "pipeline_log": pipeline_log,
            "classification": classification.model_dump(),
            "enrichment": enrichment.model_dump(),
            "discord_preview": message,
        }

    if not DISCORD_WEBHOOK_URL:
        return {"ok": False, "posted": False, "error": "DISCORD_WEBHOOK_URL not configured"}

    posted = await _post_discord(message)

    if posted:
        _save_alert(alert, score)
        pipeline_log.append("posted to Discord ✅")
        event_id = save_flow_event_row( alert_hash=alert_hash, ticker=alert.ticker,
                contract=alert.contract, premium=alert.premium,
                sentiment=alert.sentiment, source=alert.source,
                dte_bucket=alert.dte_bucket, flow_type=alert.flow_type,
                catalyst=alert.catalyst, levels=alert.levels, note=alert.note,
                enrichment=enrichment, score=score, score_reasons=score_reasons,
                passed_threshold=True, passed_dedup=True, was_published=True,
            )
        save_classification_row(event_id, alert_hash, classification)
        pipeline_log.append(f"saved to flow_events (id={event_id}) ✅")
    else:
        pipeline_log.append("Discord post failed ❌")

    return {
        "ok": posted, "posted": posted, "score": score,
        "score_reasons": score_reasons, "pipeline_log": pipeline_log,
        "classification": classification.model_dump(),
        "enrichment": enrichment.model_dump(),
        "discord_preview": message,
    }


# ---------------------------------------------------------------------------
# Phase 3 Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def ingest_and_enrich_v2(
    ticker: str,
    contract: str,
    premium: int,
    sentiment: str,
    source: str = "flow",
    dte_bucket: str = "weeklies",
    flow_type: Optional[str] = None,
    catalyst: Optional[str] = None,
    levels: Optional[str] = None,
    note: Optional[str] = None,
    dry_run: bool = False,
    force_post: bool = False,
    override_classifier: bool = False,
    format_mode: str = "subscriber",
) -> dict[str, Any]:
    """
    PRIMARY TOOL for live posting. Full pipeline with Phase 3 formatting:
    enrich → classify → score → gate → format → post → save outcomes record.

    format_mode "subscriber" = clean trader-friendly format for paying members.
    format_mode "internal" = full debug output for your ops channel.

    Args:
        ticker: Underlying symbol
        contract: Full contract string, e.g. "NVDA 2026-07-18 130C"
        premium: Total premium in dollars
        sentiment: "bullish", "bearish", or "neutral"
        source: "flow", "news", "earnings", "scanner", or "macro"
        dte_bucket: "weeklies", "next_week", "monthly", or "unknown"
        flow_type: "sweep", "block", or None
        catalyst: Optional catalyst description
        levels: Optional key levels string
        note: Optional freeform note
        dry_run: Run everything but skip Discord post and DB save
        force_post: Bypass score threshold
        override_classifier: Bypass classifier suppress vote
        format_mode: "subscriber" or "internal"
    """
    valid_sentiments = {"bullish", "bearish", "neutral"}
    valid_sources = {"flow", "news", "earnings", "scanner", "macro"}
    valid_dte = {"weeklies", "next_week", "monthly", "unknown"}
    valid_modes = {"subscriber", "internal"}

    if sentiment not in valid_sentiments:
        return {"ok": False, "error": f"sentiment must be one of {valid_sentiments}"}
    if source not in valid_sources:
        return {"ok": False, "error": f"source must be one of {valid_sources}"}
    if dte_bucket not in valid_dte:
        return {"ok": False, "error": f"dte_bucket must be one of {valid_dte}"}
    if format_mode not in valid_modes:
        return {"ok": False, "error": f"format_mode must be one of {valid_modes}"}

    alert = FlowAlert(
        ticker=ticker.upper(), contract=contract, premium=premium,
        sentiment=sentiment,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        dte_bucket=dte_bucket,  # type: ignore[arg-type]
        flow_type=flow_type, catalyst=catalyst, levels=levels, note=note,
    )

    pipeline_log = []
    enrichment = enrich_alert(alert)
    pipeline_log.append(f"enriched: {enrichment_summary(enrichment)}")
    classification = classify_alert(alert, enrichment)
    pipeline_log.append(f"classified: {classification.summary}")
    score, score_reasons = auto_score_alert(alert)
    pipeline_log.append(f"scored {score}/100")
    alert_hash = _alert_hash(alert)

    if not force_post and score < MIN_ALERT_SCORE:
        suppress = f"score {score} < threshold {MIN_ALERT_SCORE}"
        pipeline_log.append(f"blocked — {suppress}")
        return {"ok": True, "posted": False, "score": score,
                "pipeline_log": pipeline_log, "suppress_reason": suppress}
    pipeline_log.append("score gate ✅")

    if not override_classifier and not classification.publish_recommended:
        suppress = classification.suppress_reason
        pipeline_log.append(f"blocked — {suppress}")
        return {"ok": True, "posted": False, "score": score,
                "pipeline_log": pipeline_log, "suppress_reason": suppress}
    pipeline_log.append("classifier gate ✅")

    if _is_duplicate(alert):
        suppress = f"duplicate within {DEDUPE_WINDOW_MINUTES}min"
        pipeline_log.append(f"blocked — {suppress}")
        return {"ok": True, "posted": False, "score": score,
                "pipeline_log": pipeline_log, "suppress_reason": suppress}
    pipeline_log.append("dedup gate ✅")

    if not dry_run:
        closed_reason = market_closed_reason()
        if closed_reason:
            pipeline_log.append(f"blocked — market closed ({closed_reason})")
            return {
                "ok": True, "posted": False, "score": score,
                "pipeline_log": pipeline_log, "suppress_reason": f"market closed — {closed_reason}",
            }

    message = format_plain_text(format_alert(
        alert, enrichment, classification, score, score_reasons,
        mode=format_mode,  # type: ignore[arg-type]
        payload_type="plain_text",
    ))
    pipeline_log.append(f"formatted as {format_mode}")

    if dry_run:
        pipeline_log.append("dry_run — skipping post and save")
        return {
            "ok": True, "posted": False, "dry_run": True,
            "score": score, "score_reasons": score_reasons,
            "pipeline_log": pipeline_log, "format_mode": format_mode,
            "discord_preview": message,
            "classification_summary": classification.summary,
            "enrichment_summary": enrichment_summary(enrichment),
        }

    if not DISCORD_WEBHOOK_URL:
        return {"ok": False, "posted": False, "error": "DISCORD_WEBHOOK_URL not configured"}

    posted = await _post_discord(message)

    if posted:
        _save_alert(alert, score)
        pipeline_log.append("posted to Discord ✅")
        event_id = save_flow_event_row( alert_hash=alert_hash, ticker=alert.ticker,
                contract=alert.contract, premium=alert.premium,
                sentiment=alert.sentiment, source=alert.source,
                dte_bucket=alert.dte_bucket, flow_type=alert.flow_type,
                catalyst=alert.catalyst, levels=alert.levels, note=alert.note,
                enrichment=enrichment, score=score, score_reasons=score_reasons,
                passed_threshold=True, passed_dedup=True, was_published=True,
            )
        save_classification_row(event_id, alert_hash, classification)
        pipeline_log.append(f"saved flow_events id={event_id} ✅")
        try:
            outcome_id = create_outcome_row(
                alert_hash=alert_hash, ticker=alert.ticker, contract=alert.contract,
                sentiment=alert.sentiment, score=score, premium=alert.premium,
                trade_style=classification.trade_style.label,
                intent=classification.intent.label,
                setup_quality=classification.setup_quality.label,
                moneyness=enrichment.moneyness_tier, flow_type=alert.flow_type,
            )
            pipeline_log.append(f"outcome record created id={outcome_id} ✅")
        except Exception as e:
            pipeline_log.append(f"outcome record failed (non-fatal): {e}")
    else:
        pipeline_log.append("Discord post failed ❌")

    return {
        "ok": posted, "posted": posted,
        "score": score, "score_reasons": score_reasons,
        "pipeline_log": pipeline_log, "format_mode": format_mode,
        "discord_preview": message,
    }


@mcp.tool()
def record_outcome(
    alert_hash: str,
    horizon: str,
    return_pct: float,
    hit: bool,
    mfe: Optional[float] = None,
    mae: Optional[float] = None,
    invalidated: bool = False,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """
    Record how a published alert played out at a given time horizon.

    Args:
        alert_hash: From list_recent_alerts or list_alerts_needing_outcomes.
        horizon: One of: 5m, 15m, 30m, 1h, eod, next_open, next_close, 3d, 5d
        return_pct: Underlying price return %. Positive = up, negative = down.
        hit: True if price moved in the alerted direction.
        mfe: Max favorable excursion % (best it got).
        mae: Max adverse excursion % (worst it got, use negative).
        invalidated: True if the setup was invalidated.
        note: Optional plain-text note about what happened.
    """
    if horizon not in set(HORIZONS):
        return {"ok": False, "error": f"horizon must be one of {HORIZONS}"}

    try:
        # Auto-create outcome record if it doesn't exist yet
        # (covers alerts posted before Phase 3 was deployed)
        existing = get_outcome_row(alert_hash)
        if not existing:
            alerts = get_recent_published_alerts(limit=1)
            # Find this specific hash
            from db import db_cursor, is_postgres
            p = "%s" if is_postgres() else "?"
            with db_cursor() as (conn, cur):
                cur.execute(
                    f"SELECT ticker, contract, score FROM published_alerts WHERE alert_hash = {p} LIMIT 1",
                    (alert_hash,)
                )
                row = cur.fetchone()
            if row:
                row = dict(row)
                create_outcome_row(
                    alert_hash=alert_hash,
                    ticker=row["ticker"],
                    contract=row["contract"],
                    sentiment="unknown",
                    score=row["score"],
                )
            else:
                return {
                    "ok": False,
                    "error": (
                        f"No alert found for hash {alert_hash[:16]}... "
                        "Check list_alerts_needing_outcomes for valid hashes."
                    ),
                }

        updated = record_horizon_outcome(
            alert_hash=alert_hash, horizon=horizon, return_pct=return_pct,
            hit=hit, mfe=mfe, mae=mae, invalidated=invalidated, note=note,
        )
        return {
            "ok": True, "alert_hash": alert_hash,
            "horizon_recorded": horizon, "return_pct": return_pct,
            "hit": hit, "record": updated,
        }
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"Unexpected error: {e}"}


@mcp.tool()
def get_signal_quality_report(
    days_back: int = 30,
    min_score: int = 0,
    ticker: Optional[str] = None,
) -> dict[str, Any]:
    """
    Signal quality report: win rates and returns by score bucket, trade style,
    sentiment. Use this to tune the engine — raise the threshold, suppress
    certain styles, or double down on what's working.

    Args:
        days_back: Days back to include (default 30).
        min_score: Only include alerts with score >= this.
        ticker: Optional filter to a single ticker.
    """
    rows = get_outcome_stats(days_back=days_back, min_score=min_score, ticker=ticker)
    if not rows:
        return {
            "ok": True,
            "message": f"No outcome records in the last {days_back} days.",
            "total_alerts": 0,
        }
    import json as _json
    records = []
    for r in rows:
        d = dict(r)
        raw = d.pop("outcomes_json", None) or d.pop("outcomes", "{}")
        d["outcomes"] = _json.loads(raw) if isinstance(raw, str) else raw
        records.append(d)

    def _win_rate(subset):
        hits = [r for r in subset if r.get("hit_eod") in (1, True)]
        return f"{len(hits)/len(subset)*100:.0f}% ({len(hits)}/{len(subset)})" if subset else "N/A"

    def _avg_ret(subset, h):
        rets = [r["outcomes"][h]["return_pct"] for r in subset if h in r.get("outcomes", {}) and r["outcomes"][h].get("return_pct") is not None]
        return f"{sum(rets)/len(rets):.2f}%" if rets else "N/A"

    with_eod = [r for r in records if "eod" in r.get("outcomes", {})]
    buckets = {
        "80-100": [r for r in records if (r.get("alert_score") or 0) >= 80],
        "70-79":  [r for r in records if 70 <= (r.get("alert_score") or 0) < 80],
        "60-69":  [r for r in records if 60 <= (r.get("alert_score") or 0) < 70],
    }
    return {
        "ok": True,
        "period_days": days_back,
        "total_tracked": len(records),
        "with_eod_outcome": len(with_eod),
        "overall": {
            "eod_win_rate": _win_rate(with_eod),
            "avg_eod_return": _avg_ret(records, "eod"),
        },
        "by_score_bucket": {
            b: {"count": len(s), "eod_win_rate": _win_rate([r for r in s if "eod" in r.get("outcomes", {})])}
            for b, s in buckets.items() if s
        },
        "note": "Record outcomes via record_outcome tool. Phase 4 will auto-fill from market data.",
    }


@mcp.tool()
def list_alerts_needing_outcomes(days_back: int = 7) -> dict[str, Any]:
    """
    List recently published alerts with no outcome recorded yet.
    Use this at end of day, then record_outcome for each one.

    Args:
        days_back: How many days back to check (default 7).
    """
    try:
        unresolved = get_unresolved_alerts(days_back=days_back)
        return {
            "ok": True, "days_back": days_back,
            "count": len(unresolved), "alerts": unresolved,
            "next_step": f"Use record_outcome with the alert_hash. Available horizons: {HORIZONS}",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def preview_alert_format(
    ticker: str,
    contract: str,
    premium: int,
    sentiment: str,
    source: str = "flow",
    dte_bucket: str = "weeklies",
    flow_type: Optional[str] = None,
    catalyst: Optional[str] = None,
    levels: Optional[str] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """
    Preview both subscriber and internal Discord formats without posting.
    Use this before going live to confirm exactly what subscribers will see.
    No DB writes, no Discord posts.
    """
    valid_sentiments = {"bullish", "bearish", "neutral"}
    valid_sources = {"flow", "news", "earnings", "scanner", "macro"}
    valid_dte = {"weeklies", "next_week", "monthly", "unknown"}

    if sentiment not in valid_sentiments:
        return {"ok": False, "error": f"sentiment must be one of {valid_sentiments}"}
    if source not in valid_sources:
        return {"ok": False, "error": f"source must be one of {valid_sources}"}
    if dte_bucket not in valid_dte:
        return {"ok": False, "error": f"dte_bucket must be one of {valid_dte}"}

    alert = FlowAlert(
        ticker=ticker.upper(), contract=contract, premium=premium,
        sentiment=sentiment,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        dte_bucket=dte_bucket,  # type: ignore[arg-type]
        flow_type=flow_type, catalyst=catalyst, levels=levels, note=note,
    )

    enrichment = enrich_alert(alert)
    classification = classify_alert(alert, enrichment)
    score, score_reasons = auto_score_alert(alert)

    return {
        "ok": True,
        "score": score,
        "passes_threshold": score >= MIN_ALERT_SCORE,
        "classification_tags": classification.tags,
        "publish_recommended": classification.publish_recommended,
        "suppress_reason": classification.suppress_reason,
        "subscriber_preview": format_plain_text(format_alert(
            alert, enrichment, classification, score, score_reasons,
            mode="subscriber", payload_type="plain_text",
        )),
        "internal_preview": format_plain_text(format_alert(
            alert, enrichment, classification, score, score_reasons,
            mode="internal", payload_type="plain_text",
        )),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        init_all_tables()
        backend = "PostgreSQL" if is_postgres() else "SQLite"
        logger.info("DB ready backend=%s", backend)
    except Exception as e:
        logger.warning("DB init failed (non-fatal): %s", e)

    transport = "sse" if "--sse" in sys.argv else "stdio"

    if transport == "sse":
        port = int(os.getenv("MCP_PORT", "8001"))
        logger.info("Starting MCP server on SSE transport — http://localhost:%d/sse", port)
        mcp.run(transport="sse", host="0.0.0.0", port=port)
    else:
        logger.info("Starting MCP server on stdio transport")
        mcp.run(transport="stdio")
