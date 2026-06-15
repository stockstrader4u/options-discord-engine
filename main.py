from datetime import datetime
from fastapi import FastAPI
from dotenv import load_dotenv
import os
import httpx

from models import FlowAlert
from scoring import auto_score_alert

load_dotenv()

app = FastAPI()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
MIN_ALERT_SCORE = int(os.getenv("MIN_ALERT_SCORE", "70"))


@app.get("/")
def root():
    return {
        "status": "ok",
        "message": "options-discord-engine is running",
        "min_alert_score": MIN_ALERT_SCORE
    }


@app.post("/send-test")
async def send_test():
    if not DISCORD_WEBHOOK_URL:
        return {"ok": False, "error": "DISCORD_WEBHOOK_URL is missing"}

    payload = {
        "content": "Test alert from options-discord-engine"
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(DISCORD_WEBHOOK_URL, json=payload)

    return {
        "ok": response.status_code in [200, 204],
        "status_code": response.status_code
    }

@app.post("/score-only")
async def score_only(alert: FlowAlert):
    final_score = alert.score
    score_reasons = []

    if final_score is None:
        final_score, score_reasons = auto_score_alert(alert)

    return {
        "ok": True,
        "posted": False,
        "ticker": alert.ticker,
        "source": alert.source,
        "score": final_score,
        "score_reasons": score_reasons,
        "passes_threshold": final_score >= MIN_ALERT_SCORE,
        "min_alert_score": MIN_ALERT_SCORE
    }

@app.post("/flow-alert")
async def flow_alert(alert: FlowAlert):
    if not DISCORD_WEBHOOK_URL:
        return {"ok": False, "error": "DISCORD_WEBHOOK_URL is missing"}

    final_score = alert.score
    score_reasons = []

    if final_score is None:
        final_score, score_reasons = auto_score_alert(alert)

    if final_score < MIN_ALERT_SCORE:
        return {
            "ok": True,
            "posted": False,
            "score": final_score,
            "score_reasons": score_reasons,
            "reason": f"score below threshold ({final_score} < {MIN_ALERT_SCORE})"
        }

    emoji = "🟢"
    if alert.sentiment.lower() == "bearish":
        emoji = "🔴"
    elif alert.sentiment.lower() == "neutral":
        emoji = "🟡"

    dte_display = alert.dte_bucket.replace("_", " ").title()
    source_display = alert.source.replace("_", " ").title()
    alert_label = f"{source_display} Alert"
    try:
        premium_display = f"${alert.premium:,}"
    except Exception:
        premium_display = alert.premium
    sentiment_display = alert.sentiment.title()
    timestamp_display = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"{emoji} **{alert.ticker} {alert_label}**",
        "",
        f"**Time:** {timestamp_display}",
        f"**Contract:** {alert.contract}",
        f"**Premium:** {premium_display}",
        f"**Sentiment:** {sentiment_display}",
        f"**Source:** {source_display}",
        f"**DTE Bucket:** {dte_display}",
        f"**Score:** {final_score}/100"
    ]

    if alert.flow_type:
        lines.append(f"**Flow Type:** {alert.flow_type}")

    if alert.levels:
        lines.append(f"**Levels:** {alert.levels}")

    if alert.catalyst:
        lines.append(f"**Catalyst:** {alert.catalyst}")
    
    if score_reasons:
        lines.append("")
        lines.append("**Why it passed:**")
        for reason in score_reasons:
            lines.append(f"• {reason}")

    if alert.note:
        lines.append("")
        lines.append(f"**Note:** {alert.note}")

    message = "\n".join(lines)

    payload = {"content": message}

    async with httpx.AsyncClient() as client:
        response = await client.post(DISCORD_WEBHOOK_URL, json=payload)

    return {
        "ok": response.status_code in [200, 204],
        "posted": response.status_code in [200, 204],
        "status_code": response.status_code,
        "score": final_score,
        "score_reasons": score_reasons,
        "message_preview": message
    }