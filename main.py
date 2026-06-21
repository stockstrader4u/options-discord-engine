from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import os
import httpx
import json
import hashlib
import logging
import asyncio

from models import FlowAlert
from scoring import auto_score_alert
from flow_filters import filter_flow_items, is_high_conviction
from market_hours import is_market_open, market_closed_reason
from weekly_recap import build_weekly_recap, post_weekly_recap
from flow_heatmap import heatmap_job
from enrichment import enrich_alert, enrichment_summary, compute_levels
from classifier import classify_alert
from formatter import format_alert, format_plain_text
from db import (
    init_all_tables,
    alert_hash_exists_in_window,
    save_published_alert,
    get_recent_published_alerts,
    get_published_alert_count,
    save_flow_event_row,
    save_classification_row,
    create_outcome_row,
    is_postgres,
)

load_dotenv()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
MIN_ALERT_SCORE = int(os.getenv("MIN_ALERT_SCORE", "70"))
JARVIS_API_KEY = os.getenv("JARVIS_API_KEY")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
HEATMAP_WEBHOOK_URL = os.getenv("HEATMAP_WEBHOOK_URL") or os.getenv("CHART_WEBHOOK_URL")
JARVIS_MCP_URL = "https://api.jarvisflow.io/.well-known/mcp"
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
AUTO_POLL_ENABLED = os.getenv("AUTO_POLL_ENABLED", "true").lower() == "true"
DEDUPE_WINDOW_MINUTES = int(os.getenv("DEDUPE_WINDOW_MINUTES", "30"))
ALERT_FORMAT_MODE = os.getenv("ALERT_FORMAT_MODE", "subscriber")
WATCHLIST = {
    "DDOG","MDB","ANET","TWLO","CRM","UBER","NFLX","NVDA","AAPL","TSLA",
    "AMZN","ZS","NOW","CRWD","BABA","QCOM","AMD","BA","CELH","DKNG",
    "PLTR","LULU","COIN","MRNA","SNOW","AFRM","MSFT","ABNB","MRVL","QQQ",
    "RBLX","SOFI","META","TSM","MU","GOOGL","RIVN","JNJ","SPY","INTC",
    "MARA","CVNA","ENPH","FDX","SMCI","ARM","LRCX","PANW","BIDU","PDD",
    "FUTU","MSTR","ORCL","HOOD","DELL","RDDT","HIMS","AVGO","GTLB","CLSK",
    "IBM","LLY","RGTI","QUBT","TEM","OKLO","NNE","RKLB","NBIS","CEG",
    "IONQ","QBTS","APP","CRWV","GME","UNH","CRCL","FSLR","SMR","OSCR",
    "ACHR","ASTS","BMNR","FIG","GLXY","IREN","UUUU","POET","CIFR","BE",
    "EOSE","ONDS","CRML","MP","SNDK","PATH","JPM","ZM","AXTI","USO","AAOI","SPCX",
}

# Semaphore limits concurrent JarvisFlow requests so we don't get rate-limited
JARVIS_CONCURRENCY = int(os.getenv("JARVIS_CONCURRENCY", "10"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("options-discord-engine")

scheduler = AsyncIOScheduler()
jarvis_semaphore: asyncio.Semaphore | None = None


def make_alert_hash(alert: FlowAlert) -> str:
    raw = f"{alert.ticker}|{alert.contract}|{alert.source}|{alert.flow_type}|{alert.sentiment}"
    return hashlib.sha256(raw.encode()).hexdigest()


def alert_published_within_window(alert: FlowAlert) -> bool:
    return alert_hash_exists_in_window(make_alert_hash(alert), DEDUPE_WINDOW_MINUTES)


def save_alert(alert: FlowAlert, score: int) -> None:
    save_published_alert(
        alert_hash=make_alert_hash(alert),
        ticker=alert.ticker,
        contract=alert.contract,
        source=alert.source,
        score=score,
    )


async def fetch_jarvis_flow(ticker: str):
    if not JARVIS_API_KEY:
        raise ValueError("JARVIS_API_KEY is missing")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "stock_ticker_unusual_options_data",
            "arguments": {"filter_by_Ticker": ticker}
        }
    }
    headers = {
        "Authorization": f"Bearer {JARVIS_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(JARVIS_MCP_URL, json=payload, headers=headers)

    response.raise_for_status()

    for line in response.text.splitlines():
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


def jarvis_item_to_flow_alert(item: dict) -> FlowAlert:
    ticker = item.get("ticker", "").upper()
    strike = item.get("strike_Price", item.get("strikePrice", ""))
    expiry_raw = item.get("expriation_Date", item.get("expriationDate", ""))
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
        f"{ticker} {expiry_raw[:10]} {strike}{put_call[:1]}"
        if expiry_raw and strike and put_call else ticker
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
    money_ness = item.get("moneyNess", "")
    note = f"JarvisFlow | {bought_sold} {put_call} | {money_ness} | Conviction: {conviction}"

    return FlowAlert(
        ticker=ticker, contract=contract, premium=premium,
        sentiment=sentiment, source="flow", dte_bucket="weeklies",
        flow_type=sweep_block.lower() if sweep_block else None, note=note,
        spot_price=spot_price, volume=volume, open_interest=open_interest,
        contract_price=contract_price,
    )


def build_discord_message(alert: FlowAlert, final_score: int, score_reasons: list) -> str:
    emoji = {"bullish": "🟢", "bearish": "🔴"}.get(alert.sentiment.lower(), "🟡")
    lines = [
        f"{emoji} **{alert.ticker} {alert.source.replace('_',' ').title()} Alert**",
        "",
        f"**Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Contract:** {alert.contract}",
        f"**Premium:** ${alert.premium:,}",
        f"**Sentiment:** {alert.sentiment.title()}",
        f"**DTE Bucket:** {alert.dte_bucket.replace('_',' ').title()}",
        f"**Score:** {final_score}/100",
    ]
    if alert.flow_type:
        lines.append(f"**Flow Type:** {alert.flow_type}")
    if getattr(alert, "levels", None):
        lines.append(f"**Levels:** {alert.levels}")
    if getattr(alert, "catalyst", None):
        lines.append(f"**Catalyst:** {alert.catalyst}")
    if score_reasons:
        lines += ["", "**Why it passed:**"] + [f"• {r}" for r in score_reasons]
    if alert.note:
        lines += ["", f"**Note:** {alert.note}"]
    return "\n".join(lines)


async def post_to_discord(message: str) -> bool:
    async with httpx.AsyncClient() as client:
        response = await client.post(DISCORD_WEBHOOK_URL, json={"content": message})
    return response.status_code in [200, 204]


async def process_jarvis_ticker(ticker: str, limit: int = 25):
    if not DISCORD_WEBHOOK_URL:
        return {"ok": False, "error": "DISCORD_WEBHOOK_URL is missing"}

    closed_reason = market_closed_reason()
    if closed_reason:
        return {
            "ok": True, "ticker": ticker, "posted": 0,
            "skipped_market_closed": True, "reason": closed_reason,
        }

    async with jarvis_semaphore:
        flow_items = await fetch_jarvis_flow(ticker)
        if not flow_items:
            return {"ok": False, "error": "No flow items returned"}

        filtered_items, skipped_filter = filter_flow_items(flow_items)

        posted = skipped = skipped_score = skipped_classifier = 0
        skipped_dedupe = skipped_post_error = 0
        previews = []

        for item in filtered_items[:limit]:
            alert = jarvis_item_to_flow_alert(item)
            enrichment = enrich_alert(alert)
            # Direction-aware target/stop levels from Bollinger/Keltner
            # bands, computed here (not inside enrich_alert, which
            # deliberately never mutates FlowAlert) so the mutation is
            # explicit at the call site. Set before classification/
            # scoring/formatting so all three see the real levels.
            alert.levels = compute_levels(alert.ticker, alert.sentiment)
            classification = classify_alert(alert, enrichment)
            final_score, score_reasons = auto_score_alert(alert)
            high_conviction = is_high_conviction(item)

            if final_score < MIN_ALERT_SCORE:
                skipped += 1; skipped_score += 1
                continue

            if not classification.publish_recommended:
                skipped += 1; skipped_classifier += 1
                continue

            if alert_published_within_window(alert):
                skipped += 1; skipped_dedupe += 1
                continue

            message = format_plain_text(format_alert(
                alert, enrichment, classification, final_score, score_reasons,
                mode=ALERT_FORMAT_MODE,  # type: ignore[arg-type]
                payload_type="plain_text",
            ))
            posted_ok = await post_to_discord(message)

            if posted_ok:
                alert_hash = make_alert_hash(alert)
                save_alert(alert, final_score)
                event_id = save_flow_event_row(
                    alert_hash=alert_hash, ticker=alert.ticker, contract=alert.contract,
                    premium=alert.premium, sentiment=alert.sentiment, source=alert.source,
                    dte_bucket=alert.dte_bucket, flow_type=alert.flow_type,
                    catalyst=alert.catalyst, levels=alert.levels, note=alert.note,
                    enrichment=enrichment, score=final_score, score_reasons=score_reasons,
                    passed_threshold=True, passed_dedup=True, was_published=True,
                )
                save_classification_row(event_id, alert_hash, classification)
                try:
                    create_outcome_row(
                        alert_hash=alert_hash, ticker=alert.ticker, contract=alert.contract,
                        sentiment=alert.sentiment, score=final_score, premium=alert.premium,
                        trade_style=classification.trade_style.label,
                        intent=classification.intent.label,
                        setup_quality=classification.setup_quality.label,
                        moneyness=enrichment.moneyness_tier, flow_type=alert.flow_type,
                    )
                except Exception as e:
                    logger.warning("outcome record failed for %s %s: %s", alert.ticker, alert.contract, e)
                posted += 1
                previews.append({
                    "ticker": alert.ticker, "contract": alert.contract,
                    "score": final_score, "high_conviction_override": high_conviction,
                })
            else:
                skipped += 1; skipped_post_error += 1
                logger.warning("Discord post failed for %s %s", alert.ticker, alert.contract)

        return {
            "ok": True, "ticker": ticker,
            "flow_items_total": len(flow_items),
            "flow_items_after_filter": len(filtered_items),
            "skipped_filter": skipped_filter,
            "checked": min(limit, len(filtered_items)),
            "posted": posted, "skipped": skipped,
            "skipped_score": skipped_score, "skipped_classifier": skipped_classifier,
            "skipped_dedupe": skipped_dedupe,
            "skipped_post_error": skipped_post_error, "previews": previews
        }


async def poll_single_ticker(ticker: str) -> dict:
    """Poll one ticker and return its result. Errors are caught and logged."""
    try:
        result = await process_jarvis_ticker(ticker=ticker, limit=25)
        if result.get("posted", 0) > 0:
            logger.info("poll_posted ticker=%s result=%s", ticker, result)
        return result
    except Exception as e:
        logger.exception("poll_failed ticker=%s error=%s", ticker, str(e))
        return {"ok": False, "ticker": ticker, "error": str(e)}


async def scheduled_poll_job():
    """Poll every ticker in WATCHLIST concurrently, respecting the semaphore."""
    closed_reason = market_closed_reason()
    if closed_reason:
        logger.info("scheduled_poll_skipped reason=%s", closed_reason)
        return

    logger.info("scheduled_poll_start tickers=%d", len(WATCHLIST))
    tasks = [poll_single_ticker(ticker) for ticker in WATCHLIST]
    results = await asyncio.gather(*tasks)

    total_posted = sum(r.get("posted", 0) for r in results if isinstance(r, dict))
    total_checked = sum(r.get("checked", 0) for r in results if isinstance(r, dict))
    logger.info(
        "scheduled_poll_complete tickers=%d checked=%d posted=%d",
        len(WATCHLIST), total_checked, total_posted,
    )


async def weekly_recap_job():
    """
    Friday end-of-week recap. Runs on the same scheduler as the daily
    poll, gated separately so it only actually builds/posts on Fridays
    (or, if Friday is a market holiday, this still won't fire on a non-
    Friday day — the schedule itself only triggers Fridays; the holiday
    handling lives inside resolve_recap_window(), which rolls the "as of"
    date back to the last real trading day for the close reference).
    """
    if not DISCORD_WEBHOOK_URL:
        logger.warning("weekly_recap_skipped reason=DISCORD_WEBHOOK_URL_missing")
        return
    if not FINNHUB_API_KEY:
        logger.warning("weekly_recap_skipped reason=FINNHUB_API_KEY_missing")
        return

    try:
        recap = build_weekly_recap(api_key=FINNHUB_API_KEY)
    except Exception as e:
        logger.exception("weekly_recap_build_failed error=%s", str(e))
        return

    logger.info(
        "weekly_recap_built window=%s_to_%s total=%d resolved=%d failed=%d",
        recap["start_date"], recap["end_date"],
        recap["total_alerts"], recap["resolved_count"], recap["failed_count"],
    )

    if recap["total_alerts"] == 0:
        logger.info("weekly_recap_skipped reason=no_alerts_in_window")
        return

    try:
        posted = await post_weekly_recap(DISCORD_WEBHOOK_URL, recap["message"])
    except Exception as e:
        logger.exception("weekly_recap_post_failed error=%s", str(e))
        return

    if posted:
        logger.info("weekly_recap_posted ok")
    else:
        logger.warning("weekly_recap_post_failed reason=non_2xx_response")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global jarvis_semaphore
    jarvis_semaphore = asyncio.Semaphore(JARVIS_CONCURRENCY)

    init_all_tables()
    backend = "PostgreSQL" if is_postgres() else "SQLite"
    logger.info("db_backend=%s", backend)

    if AUTO_POLL_ENABLED:
        scheduler.add_job(
            scheduled_poll_job, "interval",
            seconds=POLL_INTERVAL_SECONDS,
            id="jarvis_auto_poll", replace_existing=True, max_instances=1
        )
        scheduler.add_job(
            weekly_recap_job, "cron",
            day_of_week="fri", hour=16, minute=30, timezone="America/New_York",
            id="weekly_recap", replace_existing=True, max_instances=1
        )
        scheduler.add_job(
            heatmap_job, "cron",
            day_of_week="mon-fri", hour=16, minute=30, timezone="America/New_York",
            id="flow_heatmap", replace_existing=True, max_instances=1
        )
        scheduler.start()
        logger.info(
            "scheduler_started watchlist=%d interval=%s dedupe_window=%s concurrency=%s",
            len(WATCHLIST), POLL_INTERVAL_SECONDS, DEDUPE_WINDOW_MINUTES, JARVIS_CONCURRENCY,
        )
        logger.info("weekly_recap_job_registered fri_16:30_ET finnhub_configured=%s", bool(FINNHUB_API_KEY))
        logger.info("heatmap_job_registered mon-fri_16:30_ET webhook_configured=%s", bool(HEATMAP_WEBHOOK_URL))
    else:
        logger.info("scheduler_disabled")

    yield

    if scheduler.running:
        scheduler.shutdown()
        logger.info("scheduler_stopped")


app = FastAPI(lifespan=lifespan)


@app.get("/")
def root():
    return {
        "status": "ok",
        "message": "options-discord-engine is running",
        "db_backend": "postgresql" if is_postgres() else "sqlite",
        "min_alert_score": MIN_ALERT_SCORE,
        "dedupe_window_minutes": DEDUPE_WINDOW_MINUTES,
        "auto_poll_enabled": AUTO_POLL_ENABLED,
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "watchlist_size": len(WATCHLIST),
        "jarvis_concurrency": JARVIS_CONCURRENCY,
    }


@app.get("/pull-jarvis-flow")
async def pull_jarvis_flow(ticker: str = "SPY"):
    try:
        flow_items = await fetch_jarvis_flow(ticker)
        return {"ok": True, "ticker": ticker, "count": len(flow_items), "sample": flow_items[:3]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/jarvis-preview")
async def jarvis_preview(ticker: str = "SPY"):
    try:
        flow_items = await fetch_jarvis_flow(ticker)
        if not flow_items:
            return {"ok": False, "error": "No flow items returned"}
        alert = jarvis_item_to_flow_alert(flow_items[0])
        final_score, score_reasons = auto_score_alert(alert)
        # TEMPORARY: enrichment + levels added here only to verify the
        # real RVOL and Bollinger/Keltner levels fixes against live
        # JarvisFlow data without posting to Discord. Safe to remove
        # once confirmed working — read-only, no side effects.
        enrichment = enrich_alert(alert)
        alert.levels = compute_levels(alert.ticker, alert.sentiment)
        return {
            "ok": True, "ticker": ticker,
            "raw_item": flow_items[0], "mapped_alert": alert.model_dump(),
            "enrichment": enrichment.model_dump(),
            "score": final_score, "score_reasons": score_reasons,
            "passes_threshold": final_score >= MIN_ALERT_SCORE
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/pull-and-post-jarvis")
async def pull_and_post_jarvis(ticker: str = "SPY", limit: int = 10):
    try:
        return await process_jarvis_ticker(ticker=ticker, limit=limit)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/send-test")
async def send_test():
    if not DISCORD_WEBHOOK_URL:
        return {"ok": False, "error": "DISCORD_WEBHOOK_URL is missing"}
    async with httpx.AsyncClient() as client:
        response = await client.post(DISCORD_WEBHOOK_URL, json={"content": "Test alert from options-discord-engine"})
    return {"ok": response.status_code in [200, 204], "status_code": response.status_code}


@app.post("/score-only")
async def score_only(alert: FlowAlert):
    final_score, score_reasons = auto_score_alert(alert)
    return {
        "ok": True, "posted": False, "ticker": alert.ticker,
        "source": alert.source, "score": final_score,
        "score_reasons": score_reasons,
        "passes_threshold": final_score >= MIN_ALERT_SCORE,
        "min_alert_score": MIN_ALERT_SCORE
    }


@app.post("/flow-alert")
async def flow_alert(alert: FlowAlert):
    if not DISCORD_WEBHOOK_URL:
        return {"ok": False, "error": "DISCORD_WEBHOOK_URL is missing"}

    final_score, score_reasons = auto_score_alert(alert)

    if final_score < MIN_ALERT_SCORE:
        return {
            "ok": True, "posted": False, "score": final_score,
            "score_reasons": score_reasons,
            "reason": f"score below threshold ({final_score} < {MIN_ALERT_SCORE})"
        }

    if alert_published_within_window(alert):
        return {
            "ok": True, "posted": False, "score": final_score,
            "score_reasons": score_reasons,
            "reason": f"duplicate alert within cooldown window ({DEDUPE_WINDOW_MINUTES} min)"
        }

    closed_reason = market_closed_reason()
    if closed_reason:
        return {
            "ok": True, "posted": False, "score": final_score,
            "score_reasons": score_reasons, "reason": f"market closed — {closed_reason}",
        }

    message = build_discord_message(alert, final_score, score_reasons)
    posted_ok = await post_to_discord(message)

    if posted_ok:
        save_alert(alert, final_score)

    return {
        "ok": posted_ok, "posted": posted_ok,
        "score": final_score, "score_reasons": score_reasons,
        "message_preview": message
    }
