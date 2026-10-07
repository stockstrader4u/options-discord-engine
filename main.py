from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
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
from flow_filters import filter_flow_items, is_high_conviction, MAX_DTE_DAYS
from market_hours import is_market_open, market_closed_reason, is_trading_day, MARKET_OPEN, MARKET_CLOSE, EASTERN
from weekly_recap import build_weekly_recap, render_weekly_recap_card, post_weekly_recap_image
from flow_heatmap import heatmap_job
from enrichment import enrich_alert, enrichment_summary, compute_levels
from classifier import classify_alert
from formatter import format_alert, format_plain_text
from daily_flow_summary import build_daily_flow_summary, post_daily_flow_summary
from db import (
    init_all_tables,
    add_premium_column_migration,
    add_vol_oi_columns_migration,
    alert_hash_exists_in_window,
    get_same_day_published_premium,
    save_published_alert,
    save_published_alert_with_premium,
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
# ALERT_POLL_MODE (2026-10-06): "batch" (default) queries JarvisFlow once per 15-minute window and posts
# what qualified in that window (26 runs/day ~ 2,600 calls) instead of polling all tickers every minute
# (~39,000 calls/day, which tripped the vendor's usage limits). "interval" restores the old
# POLL_INTERVAL_SECONDS behavior -- set it on Railway to roll back without a code change.
ALERT_POLL_MODE = os.getenv("ALERT_POLL_MODE", "batch").lower()
BATCH_WINDOW_MINUTES = 15
# Trades from up to this long BEFORE the window start are still considered, so a trade Jarvis reports a
# little late (or a run delayed by a restart) isn't lost. The same-day dedupe prevents any repost.
BATCH_GRACE_MINUTES = int(os.getenv("BATCH_GRACE_MINUTES", "3"))
DEDUPE_WINDOW_MINUTES = int(os.getenv("DEDUPE_WINDOW_MINUTES", "30"))
# How much bigger same-day premium must be on a repeat contract+sentiment
# for it to be treated as materially new information rather than a
# duplicate. 2.0 = must be at least double the prior same-day premium.
DEDUPE_PREMIUM_MULTIPLIER = float(os.getenv("DEDUPE_PREMIUM_MULTIPLIER", "2.0"))
ALERT_FORMAT_MODE = os.getenv("ALERT_FORMAT_MODE", "subscriber")
WATCHLIST = {
    "DDOG","MDB","ANET","TWLO","CRM","UBER","NFLX","NVDA","AAPL","TSLA",
    "AMZN","ZS","NOW","CRWD","BABA","QCOM","AMD","BA","CELH","DKNG",
    "PLTR","LULU","COIN","MRNA","SNOW","AFRM","MSFT","ABNB","MRVL","QQQ",
    "RBLX","SOFI","META","TSM","GOOGL","RIVN","JNJ","SPY","INTC",
    "MARA","CVNA","ENPH","FDX","SMCI","ARM","LRCX","PANW","BIDU","PDD",
    "FUTU","MSTR","ORCL","HOOD","DELL","RDDT","HIMS","AVGO","GTLB","CLSK",
    "IBM","LLY","RGTI","QUBT","TEM","OKLO","NNE","RKLB","NBIS","CEG",
    "IONQ","QBTS","APP","CRWV","GME","UNH","CRCL","FSLR","SMR","OSCR",
    "ACHR","ASTS","BMNR","FIG","GLXY","IREN","UUUU","POET","CIFR","BE",
    "EOSE","ONDS","CRML","MP","PATH","JPM","ZM","AXTI","USO","AAOI","SPCX",
}

# Semaphore limits concurrent JarvisFlow requests so we don't get rate-limited
JARVIS_CONCURRENCY = int(os.getenv("JARVIS_CONCURRENCY", "10"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("options-discord-engine")

scheduler = AsyncIOScheduler()
jarvis_semaphore: asyncio.Semaphore | None = None


def batch_window(now_et: datetime):
    """The 15-minute window a batch run at now_et should cover, as (start, end) in ET -- or None when
    there is nothing to cover (weekend/holiday, or outside the session). Windows are anchored to the
    open: 9:30-9:45 ... 15:45-16:00. A run at 9:46/10:01/10:16/... (the minute after a window closes)
    covers the window that just ended; the 16:01 run covers the last one."""
    if not is_trading_day(now_et.date()):
        return None
    end = now_et.replace(minute=(now_et.minute // BATCH_WINDOW_MINUTES) * BATCH_WINDOW_MINUTES,
                         second=0, microsecond=0)
    start = end - timedelta(minutes=BATCH_WINDOW_MINUTES)
    open_dt = datetime.combine(now_et.date(), MARKET_OPEN, tzinfo=EASTERN)
    close_dt = datetime.combine(now_et.date(), MARKET_CLOSE, tzinfo=EASTERN)
    if end <= open_dt or end > close_dt:
        return None
    return start, end


def item_trade_time_et(item: dict):
    """Trade time of a raw Jarvis item as an aware ET datetime, or None. Jarvis stamps trades with
    naive ET wall-clock time (verified: 09:30-16:00 across 20k rows), e.g. '2026-10-05T10:37:57.88'."""
    raw = item.get("transaction_DateTime") or item.get("transactionDateTime")
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=EASTERN)
    except ValueError:
        return None


def filter_items_since(items: list, since: datetime) -> list:
    """Keep items whose trade time is at or after `since` (items with no readable time are dropped)."""
    out = []
    for it in items:
        t = item_trade_time_et(it)
        if t is not None and t >= since:
            out.append(it)
    return out


def make_alert_hash(alert: FlowAlert) -> str:
    raw = f"{alert.ticker}|{alert.contract}|{alert.source}|{alert.flow_type}|{alert.sentiment}"
    return hashlib.sha256(raw.encode()).hexdigest()


def alert_published_within_window(alert: FlowAlert) -> bool:
    """
    Same-day dedup check with a material-change override — replaces the
    old rolling-30-minute-window check.

    PROBLEM THIS FIXES: the old check only blocked a repost within
    DEDUPE_WINDOW_MINUTES (30 min) of the first post. JarvisFlow's feed
    can resurface the exact same sweep/block hours later — confirmed in
    production on 2026-06-22, where APP, QCOM, TSM, and TSLA all posted
    twice, roughly 4 hours apart, with near-identical premium each time.
    Since 4 hours is far outside any 30-minute window, the old check let
    every one of these duplicates straight through.

    NEW BEHAVIOR: blocks any repost of the same contract+sentiment on
    the SAME CALENDAR DAY, unless the new premium is at least
    DEDUPE_PREMIUM_MULTIPLIER times (default 2x) the most recent
    same-day premium for that exact contract+sentiment — in which case
    it's treated as materially new information (a genuinely bigger new
    sweep/block on the same contract) and allowed through.
    """
    alert_hash = make_alert_hash(alert)
    prior_premium = get_same_day_published_premium(alert_hash)

    if prior_premium is None:
        return False  # hasn't posted today at all -> not a duplicate

    if alert.premium >= prior_premium * DEDUPE_PREMIUM_MULTIPLIER:
        logger.info(
            "dedupe_override ticker=%s contract=%s prior_premium=%s new_premium=%s multiplier=%s",
            alert.ticker, alert.contract, prior_premium, alert.premium, DEDUPE_PREMIUM_MULTIPLIER,
        )
        return False  # material change -> allow the repost

    return True  # same-day duplicate, premium didn't materially change -> block


def save_alert(alert: FlowAlert, score: int) -> None:
    save_published_alert_with_premium(
        alert_hash=make_alert_hash(alert),
        ticker=alert.ticker,
        contract=alert.contract,
        source=alert.source,
        score=score,
        premium=alert.premium,
    )


def jarvis_flow_arguments(ticker: str) -> dict:
    """Arguments for stock_ticker_unusual_options_data.

    Server-side filters (JARVIS_SERVER_FILTERS, default on) ask Jarvis only for the rows our
    pre-filter could ever keep -- OTM, BOUGHT, expiring within MAX_DTE_DAYS -- instead of the 300
    most recent rows of every kind, ~97% fewer bytes (usage spike 2026-10-05). The local
    passes_basic_filters() still runs on whatever comes back (premium, conviction, exact DTE),
    so the result is the same; the expiry range is one day wider than MAX_DTE_DAYS as a buffer.
    Set JARVIS_SERVER_FILTERS=false on Railway to revert to the old ticker-only request."""
    args = {"filter_by_Ticker": ticker}
    if os.getenv("JARVIS_SERVER_FILTERS", "true").lower() == "true":
        today = datetime.now(timezone.utc).date()   # same date basis as flow_filters.compute_dte_days
        args.update({
            "filter_by_moneyNess": "OTM",
            "filter_by_impliedAction": "BOUGHT",
            "filter_by_expiration_date_range_from": today.strftime("%m/%d/%Y"),
            "filter_by_expiration_date_range_to": (today + timedelta(days=MAX_DTE_DAYS + 1)).strftime("%m/%d/%Y"),
        })
    return args


async def fetch_jarvis_flow(ticker: str):
    if not JARVIS_API_KEY:
        raise ValueError("JARVIS_API_KEY is missing")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "stock_ticker_unusual_options_data",
            "arguments": jarvis_flow_arguments(ticker)
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


_discord_post_lock: asyncio.Lock | None = None


async def post_to_discord(message: str) -> bool:
    """Post one alert. Posts are serialized and spaced ~0.6s apart (a batch run can have several alerts to
    send at once; Discord allows only ~5 webhook posts per 2s), and a 429 is retried after the
    Retry-After Discord asks for (max 2 retries). Any other failure logs the status and body."""
    global _discord_post_lock
    if _discord_post_lock is None:
        _discord_post_lock = asyncio.Lock()
    async with _discord_post_lock:
        for attempt in range(3):
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(DISCORD_WEBHOOK_URL, json={"content": message})
            if response.status_code in (200, 204):
                await asyncio.sleep(0.6)
                return True
            if response.status_code == 429 and attempt < 2:
                try:
                    wait = float(response.json().get("retry_after", 2.0))
                except Exception:
                    wait = float(response.headers.get("Retry-After", 2.0) or 2.0)
                wait = min(max(wait, 0.5), 10.0)
                logger.warning("discord_rate_limited retry_in=%.1fs attempt=%d", wait, attempt + 1)
                await asyncio.sleep(wait)
                continue
            logger.warning("discord_post_failed status=%s body=%s", response.status_code, response.text[:200])
            return False
        return False


async def process_jarvis_ticker(ticker: str, limit: int = 25, since: datetime | None = None):
    """since=None is the legacy every-minute behavior (market-hours gate applied here). With `since`
    (batch mode) only trades at/after that ET time are considered, and the batch job has already
    gated the session itself (the 16:01 run for the last window is after the 16:00 close)."""
    if not DISCORD_WEBHOOK_URL:
        return {"ok": False, "error": "DISCORD_WEBHOOK_URL is missing"}

    closed_reason = market_closed_reason() if since is None else None
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
        if since is not None:
            filtered_items = filter_items_since(filtered_items, since)

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
            #
            # BUGFIX (2026-06-24): now passes alert.spot_price through so
            # compute_levels() can filter candidate band levels against
            # the real spot price before assigning them to target slots.
            # Previously this only sorted the raw band values by
            # magnitude, which could place a band on the wrong side of
            # spot into a target slot (confirmed in production on CRWV:
            # a bearish alert published a "target" above spot). See
            # enrichment.py for the full fix writeup.
            alert.levels = compute_levels(alert.ticker, alert.sentiment, alert.spot_price)
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
                    volume=alert.volume, open_interest=alert.open_interest,
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


async def poll_single_ticker(ticker: str, since: datetime | None = None) -> dict:
    """Poll one ticker and return its result. Errors are caught and logged."""
    try:
        result = await process_jarvis_ticker(ticker=ticker, limit=25, since=since)
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


async def batch_poll_job():
    """Once per 15-minute window: query every WATCHLIST ticker one time, and post whatever qualified.
    Runs at 9:46, 10:01, 10:16 ... 15:46, 16:01 ET (see lifespan). If nothing qualified, nothing posts."""
    now_et = datetime.now(EASTERN)
    window = batch_window(now_et)
    if window is None:
        logger.info("batch_poll_skipped now_et=%s (weekend/holiday or outside the session)", now_et.strftime("%a %H:%M"))
        return
    start, end = window
    since = start - timedelta(minutes=BATCH_GRACE_MINUTES)
    logger.info("batch_poll_start window=%s-%s ET since=%s tickers=%d",
                start.strftime("%H:%M"), end.strftime("%H:%M"), since.strftime("%H:%M"), len(WATCHLIST))
    results = await asyncio.gather(*[poll_single_ticker(t, since=since) for t in WATCHLIST])
    total_posted = sum(r.get("posted", 0) for r in results if isinstance(r, dict))
    total_checked = sum(r.get("checked", 0) for r in results if isinstance(r, dict))
    logger.info("batch_poll_complete window=%s-%s ET tickers=%d checked=%d posted=%d",
                start.strftime("%H:%M"), end.strftime("%H:%M"), len(WATCHLIST), total_checked, total_posted)


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
        import os, tempfile
        fd, card_path = tempfile.mkstemp(suffix=".png", prefix="weekly_recap_")
        os.close(fd)
        try:
            render_weekly_recap_card(recap, card_path)
            posted = await post_weekly_recap_image(DISCORD_WEBHOOK_URL, card_path, recap["caption"])
        finally:
            try:
                os.remove(card_path)
            except OSError:
                pass
    except Exception as e:
        logger.exception("weekly_recap_post_failed error=%s", str(e))
        return

    if posted:
        logger.info("weekly_recap_posted ok")
    else:
        logger.warning("weekly_recap_post_failed reason=non_2xx_response")


async def daily_flow_summary_job():
    """
    "Today's Flow at a Glance" — standalone end-of-day recap, posted at
    4:35pm ET (right after the existing 4:30pm heatmap job). Synthesizes
    the day's published alerts into same-ticker clustering, genuine
    Vol/OI outliers, and the day's highest-conviction calls. See
    daily_flow_summary.py for the full design rationale.

    Skips posting entirely on a zero-alert day (e.g. market holiday) —
    build_daily_flow_summary() returns message=None in that case, and
    there's nothing useful to tell subscribers.
    """
    if not DISCORD_WEBHOOK_URL:
        logger.warning("daily_flow_summary_skipped reason=DISCORD_WEBHOOK_URL_missing")
        return

    try:
        result = build_daily_flow_summary()
    except Exception as e:
        logger.exception("daily_flow_summary_build_failed error=%s", str(e))
        return

    logger.info(
        "daily_flow_summary_built date=%s total=%d bullish=%d bearish=%d",
        result["date_str"], result["total_alerts"], result["bullish_count"], result["bearish_count"],
    )

    if result["message"] is None:
        logger.info("daily_flow_summary_skipped reason=no_alerts_today")
        return

    try:
        posted = await post_daily_flow_summary(DISCORD_WEBHOOK_URL, result["message"])
    except Exception as e:
        logger.exception("daily_flow_summary_post_failed error=%s", str(e))
        return

    if posted:
        logger.info("daily_flow_summary_posted ok")
    else:
        logger.warning("daily_flow_summary_post_failed reason=non_2xx_response")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global jarvis_semaphore
    jarvis_semaphore = asyncio.Semaphore(JARVIS_CONCURRENCY)

    init_all_tables()
    add_premium_column_migration()
    add_vol_oi_columns_migration()
    backend = "PostgreSQL" if is_postgres() else "SQLite"
    logger.info("db_backend=%s", backend)

    if AUTO_POLL_ENABLED:
        if ALERT_POLL_MODE == "interval":
            scheduler.add_job(
                scheduled_poll_job, "interval",
                seconds=POLL_INTERVAL_SECONDS,
                id="jarvis_auto_poll", replace_existing=True, max_instances=1
            )
        else:
            # Batch mode: 9:46 (covers 9:30-9:45), then :01/:16/:31/:46 from 10:01 to 15:46, then 16:01
            # (covers 15:45-16:00) -- 26 runs per trading day. batch_poll_job() re-checks holidays.
            for hour, minute, job_id in (("10-15", "1,16,31,46", "jarvis_batch_poll_midday"),
                                         ("9", "46", "jarvis_batch_poll_open"),
                                         ("16", "1", "jarvis_batch_poll_close")):
                scheduler.add_job(
                    batch_poll_job, "cron",
                    day_of_week="mon-fri", hour=hour, minute=minute, timezone="America/New_York",
                    id=job_id, replace_existing=True, max_instances=1,
                    misfire_grace_time=300, coalesce=True,
                )
        scheduler.add_job(
            weekly_recap_job, "cron",
            day_of_week="fri", hour=16, minute=32, timezone="America/New_York",
            id="weekly_recap", replace_existing=True, max_instances=1,
            misfire_grace_time=300,
        )
        scheduler.add_job(
            heatmap_job, "cron",
            day_of_week="mon-fri", hour=16, minute=30, timezone="America/New_York",
            id="flow_heatmap", replace_existing=True, max_instances=1,
            misfire_grace_time=300,
        )
        scheduler.add_job(
            daily_flow_summary_job, "cron",
            day_of_week="mon-fri", hour=16, minute=35, timezone="America/New_York",
            id="daily_flow_summary", replace_existing=True, max_instances=1,
            misfire_grace_time=300,
        )
        scheduler.start()
        logger.info(
            "scheduler_started watchlist=%d mode=%s interval=%s dedupe_window=%s concurrency=%s dedupe_premium_multiplier=%s",
            len(WATCHLIST), ALERT_POLL_MODE, POLL_INTERVAL_SECONDS, DEDUPE_WINDOW_MINUTES, JARVIS_CONCURRENCY, DEDUPE_PREMIUM_MULTIPLIER,
        )
        logger.info("weekly_recap_job_registered fri_16:32_ET finnhub_configured=%s", bool(FINNHUB_API_KEY))
        logger.info("heatmap_job_registered mon-fri_16:30_ET webhook_configured=%s", bool(HEATMAP_WEBHOOK_URL))
        logger.info("daily_flow_summary_job_registered mon-fri_16:35_ET")
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
        "dedupe_premium_multiplier": DEDUPE_PREMIUM_MULTIPLIER,
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
        #
        # BUGFIX (2026-06-24): passes alert.spot_price through, same as
        # the live posting path in process_jarvis_ticker — see that
        # function's comment and enrichment.py for the full writeup.
        enrichment = enrich_alert(alert)
        alert.levels = compute_levels(alert.ticker, alert.sentiment, alert.spot_price)
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
            "reason": "duplicate alert already published today (no material premium increase)"
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
