from models import FlowAlert


def auto_score_alert(alert: FlowAlert) -> tuple[int, list[str]]:
    score = 35
    reasons = []

    premium_value = alert.premium

    if premium_value >= 500_000:
        score += 18
        reasons.append("very large premium")
    elif premium_value >= 250_000:
        score += 14
        reasons.append("large premium")
    elif premium_value >= 100_000:
        score += 10
        reasons.append("solid premium")
    elif premium_value >= 50_000:
        score += 5
        reasons.append("decent premium")

    sentiment = alert.sentiment.lower()
    if sentiment in ["bullish", "bearish"]:
        score += 5
        reasons.append(f"{sentiment} sentiment")

    if alert.source:
        source = alert.source.lower()

        if source == "flow":
            score += 6
            reasons.append("flow-driven setup")
        elif source == "scanner":
            score += 5
            reasons.append("scanner-confirmed setup")
        elif source == "news":
            score += 3
            reasons.append("news-backed setup")
        elif source == "earnings":
            score += 4
            reasons.append("earnings-driven setup")
        elif source == "macro":
            score += 2
            reasons.append("macro-backed setup")

    if alert.dte_bucket == "weeklies":
        score += 6
        reasons.append("weeklies contract")
    elif alert.dte_bucket == "next_week":
        score += 4
        reasons.append("next-week contract")
    elif alert.dte_bucket == "monthly":
        score += 1
        reasons.append("monthly contract")

    if alert.catalyst:
        score += 5
        reasons.append("catalyst present")

    if alert.levels:
        score += 5
        reasons.append("levels defined")

    if alert.flow_type:
        flow_type = alert.flow_type.lower()
        if "sweep" in flow_type:
            score += 6
            reasons.append("sweep flow")
        elif "block" in flow_type:
            score += 4
            reasons.append("block flow")

    if alert.note:
        note_lower = alert.note.lower()
        if "heavy" in note_lower:
            score += 2
            reasons.append("heavy flow note")
        if "late-day" in note_lower or "into the close" in note_lower:
            score += 3
            reasons.append("timing confirmation")

    score = max(0, min(score, 100))
    return score, reasons