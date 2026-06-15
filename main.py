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

    lines = [
        f"{emoji} **{alert.ticker} flow alert**",
        f"**Contract:** {alert.contract}",
        f"**Premium:** {alert.premium}",
        f"**Sentiment:** {alert.sentiment}",
        f"**Score:** {final_score}/100"
    ]

    if alert.flow_type:
        lines.append(f"**Flow Type:** {alert.flow_type}")

    if alert.levels:
        lines.append(f"**Levels:** {alert.levels}")

    if alert.catalyst:
        lines.append(f"**Catalyst:** {alert.catalyst}")

    if score_reasons:
        lines.append(f"**Why it passed:** {', '.join(score_reasons)}")

    if alert.note:
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