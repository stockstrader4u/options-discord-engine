"""
setup_read.py -- the short per-setup read (NEW_READ=1): one call PER setup to Grok (x-ai/grok-4.3 via OpenRouter, reasoning effort
low, chart image attached), validated in code, with a deterministic fallback. Also a one-line market backdrop for the header.

Pattern ported from bmt-live-dashboard/charts/verdict.py (read-only reference). The model only WORDS facts the code computed;
everything it writes is checked before it can be posted:
  * every number must appear in the facts (it cannot invent a level, percent or date, even if it misreads the image);
  * takes a stance (leans bullish / bearish / no clear lean) that agrees with the computed bias (also its `lean` field);
  * names at least one price level from the facts, and a level above the price is never one to "hold", below never one to "clear";
  * no buy/sell/target/stop/entry/advice words, no AI mention, no jargon or field names, at most 2 sentences;
  * a counter-trend idea (direction against the chart) must SAY so, and an idea that agrees with the chart must not claim otherwise.
Two attempts (the second drops the image if the first was refused), then the deterministic fallback built from the facts.
OpenRouter `usage.cost` and every rejection reason are logged; cost never changes what subscribers see. Never raises.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time

import requests

URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "x-ai/grok-4.3"
MAX_CHARS = 380
MAX_WORDS = 60
MAX_SENTENCES = 2
ATTEMPTS = 2
ATTEMPT_DEADLINE_S = 90

PROMPT = (
    "You are a sharp, friendly market explainer talking to a friend. Under a stock chart in a paid Discord for options traders (day "
    "traders and swing traders holding a few days up to a week, some new to options) you write YOUR OWN short read of what this stock is "
    "doing and how the trade idea fits, in very simple everyday words, like you are explaining it out loud. Tell the story, do not recite "
    "indicators: who is in control (buyers or sellers), is the move getting stronger or running out of steam, what the options market is "
    "hinting at. You get the chart image and DATA with the exact numbers.\n"
    "Take a clear stance about the CHART: it leans bullish, leans bearish, or there is no clear lean, and say why in plain words. Then point to "
    "the one price level that matters most and what it would mean if the stock holds it or loses it, in your own natural wording. Mention a "
    "caution only if it really matters (momentum fading, max pain far away, earnings before the expiry, a strike many days of normal movement away).\n"
    "Three examples of the TONE only (never reuse their wording, write something fresh each time, and vary how you start and end): "
    "'Buyers have been in charge all week and every dip got bought, but the push is losing steam up here. Hold LEVEL and the trend stays healthy; "
    "slip under it and this starts to look tired.' / "
    "'Sellers are pressing: the bounce failed under LEVEL and it keeps making lower lows, so rallies look like pauses in a down move until it "
    "gets back above that.' / "
    "'Nobody is winning here. It is stuck between LEVEL A and LEVEL B and options are pricing only a small move, so a clean break of either side "
    "will tell you the next direction.'\n"
    "A level below the price is something it has to HOLD; a level above the price is something it has to CLEAR (never say it must hold a level that is above the price, "
    "or clear one that is below it).\n"
    "Be straight about the idea. If DATA.counter_trend is true, use the word counter-trend and explain it in simple words a beginner gets at once, never leaving "
    "the term hanging: what the chart is doing (still falling or rising), what the big options buyers are betting on instead (a bounce or a drop), and the one thing that "
    "has to happen first (price turning through the level in DATA). Example of the clarity wanted, not the wording: 'This is a counter-trend idea: the stock is still "
    "sliding, but big money is betting on a bounce, so it only works once price turns back up through LEVEL.' Do not spin it or hide the risk. If DATA.analyst_view says analysts' price goal is below the price, or the flow and the chart disagree, you may say so briefly instead of "
    "cheerleading.\n"
    "Rules: 1 or 2 short sentences, at most 45 words in total. Copy every number exactly as written in DATA (digits, decimals, signs) and never "
    "write a number that is not in DATA, even if you can see it on the image. Use at least one price level from DATA. Your lean must agree with "
    "DATA.bias ('up' = bullish, 'down' = bearish, 'neutral' = no clear lean). No technical shorthand or field names (never write ema34, "
    "ema50, EMA, RSI, Fibonacci, POC, IV, OTM): say 'its average price', 'momentum', 'the recent high' instead. Never use the words buy, sell, "
    "target, stop, entry or advice, never tell anyone what to do, never give price targets, stop-losses or entries, never promise or predict a "
    "price, never mention being an AI.\n"
    'Reply with ONLY a JSON object: {"lean": "bullish" | "bearish" | "none", "read": "..."}\nDATA: '
)

BACKDROP_PROMPT = (
    "Write ONE plain-English sentence (at most 32 words) for the top of tonight's trade-ideas post describing how the market closed, from DATA only "
    "(SPY = S&P 500, QQQ = Nasdaq 100, IWM = small caps). Copy numbers exactly as written in DATA and never write a number that is not in DATA. Mention at "
    "least two of the three tickers by symbol. No advice, no predictions, never use the words buy, sell, target, stop or entry, never mention being an AI.\n"
    'Reply with ONLY a JSON object: {"read": "..."}\nDATA: '
)

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_LVL = re.compile(r"\d[\d,]*\.\d{2}")
_BANNED = re.compile(r"\b(buy|buys|buying|sell|sells|selling|targets?|stop[- ]?loss|stops?|entry|entries|exit|should|must|recommend\w*|advice|advise\w*|guarantee\w*)\b", re.I)
_AI = re.compile(r"\bAI\b|language model|artificial intelligence|\bI\b|\bmy\b|\bas an\b")
_STANCE = re.compile(r"\b(bullish|bearish|neutral|constructive|cautious|leans?|leaning|no clear|sideways|balanced|stuck|in control|in charge|buyers|sellers|bulls|bears|pressing|pressure|upside|downside|healthy|weak(?:ening|er)?|strong(?:er)?|tired|losing steam|running out of steam|uptrend|downtrend|tilt\w*|edge|favou?r\w*)\b", re.I)
_JARGON = re.compile(r"\bema\d*\b|\bfibonacci\b|\bpoc\b|\brsi\b|\bbias\b|\biv\b|\botm\b|\batm\b|\brvol\b|\bvwap\b|\w_\w", re.I)
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")
_LEAN_BULL = re.compile(r"\b(?:leans?|leaning|looks?|tilts?|stays?|remains?|is|are)\s+(?:\w+\s+)?bullish\b|\bbullish\s+(?:lean|bias|stance|tilt|picture|read|setup|chart)\b", re.I)
_LEAN_BEAR = re.compile(r"\b(?:leans?|leaning|looks?|tilts?|stays?|remains?|is|are)\s+(?:\w+\s+)?bearish\b|\bbearish\s+(?:lean|bias|stance|tilt|picture|read|setup|chart)\b", re.I)
_NO_LEAN = re.compile(r"\bno clear (?:lean|direction|edge|winner)\b|\bneutral\b|\bsideways\b|\bnobody (?:is )?(?:winning|in control)\b|\bstuck\b|\bbalanced\b|\bno lean\b", re.I)
_COUNTER = re.compile(r"counter-?\s?trend|\bagainst the (?:trend|chart|current)|fighting the (?:trend|chart)|swimming against|goes against|bucks? the trend", re.I)
_HOLD = re.compile(r"\b(hold|holds|holding|defend\w*|stays? above|keeps? above|support)\b", re.I)
_CLEAR = re.compile(r"\b(clear|clears|clearing|reclaim\w*|breaks? (?:above|through|out)|gets? back above|push\w* (?:through|above)|climbs? (?:above|over)|resistance)\b", re.I)
_LOSE = re.compile(r"\b(slips?|slipping|(?:falls?|drops?|sinks?|slides?) (?:under|below)|breaks? (?:below|under))\b", re.I)
_LEVEL_KEYS = ("next_level_above", "second_level_above", "first_level_below", "call_wall", "put_wall", "max_pain", "expected_move", "ema34", "ema50",
               "busiest_price", "window_high", "window_low", "price")


def _f(n: str) -> float:
    return float(n.replace(",", ""))


def numbers(text: str) -> set[float]:
    return {_f(n) for n in _NUM.findall(text)}


def level_numbers(facts: dict) -> set[float]:
    out = {_f(m) for k in _LEVEL_KEYS for m in _LVL.findall(str(facts.get(k, "")))}
    if facts.get("strike_price"):
        out |= numbers(str(facts["strike_price"]))
    return out


_CLAUSE_SPLIT = re.compile(r"[.!?](?=\s|$)|[;:]|,(?=\s)|\band\b|\bbut\b|\bwhile\b|\bthen\b|\bwhereas\b|\bso\b", re.I)   # decimals and 1,234.50 stay whole


def _level_direction_issue(text: str, facts: dict) -> str | None:
    """A level above the price is one to clear (never to hold or lose); a level below is one to hold (never to clear).
    Checked clause by clause ("hold 96.50, then clear 104.20" is two clauses), so each verb is judged against its own level."""
    try:
        price = _f(str(facts["price"]))
    except (KeyError, ValueError):
        return None
    levels = level_numbers(facts) - {price}
    for clause in _CLAUSE_SPLIT.split(text):
        vals = [(mm.group(0), _f(mm.group(0))) for mm in _NUM.finditer(clause) if _f(mm.group(0)) in levels]
        if not vals:
            continue
        for raw, v in vals:
            if v > price * 1.0005 and (_HOLD.search(clause) or _LOSE.search(clause)):
                return f"treats {raw}, a level above the price, as one to hold or lose"
            if v < price * 0.9995 and _CLEAR.search(clause):
                return f"treats {raw}, a level below the price, as one to clear"
    return None


def check_read(text: str | None, facts: dict, lean: str | None = None) -> str | None:
    """None when the read is fit to post, otherwise the reason it is not."""
    if not text or len(text.strip()) < 20:
        return "empty or too short"
    text = text.strip()
    if len(text) > MAX_CHARS or len(text.split()) > MAX_WORDS:
        return "too long"
    if len(_SENTENCE_END.findall(text)) > MAX_SENTENCES:
        return "too many sentences"
    j = _JARGON.search(text)
    if j:
        return f"jargon or field name: {j.group(0)}"
    m = _BANNED.search(text)
    if m:
        return f"advice-style word: {m.group(0)}"
    if _AI.search(text):
        return "mentions being an AI"
    stray = numbers(text) - numbers(json.dumps(facts, ensure_ascii=False))
    if stray:
        return f"number not in the data: {sorted(stray)[0]:g}"
    if not _STANCE.search(text):
        return "no clear stance"
    if not (numbers(text) & level_numbers(facts)):
        return "names no price level from the data"
    bias = facts.get("bias")
    if bias == "up" and _LEAN_BEAR.search(text):
        return "bearish wording on an up chart"
    if bias == "down" and _LEAN_BULL.search(text):
        return "bullish wording on a down chart"
    if bias == "neutral":
        if _LEAN_BULL.search(text) or _LEAN_BEAR.search(text):
            return "takes a side on a chart with no clear lean"
        if not _NO_LEAN.search(text):
            return "does not say there is no clear lean"
    if lean is not None and {"up": "bullish", "down": "bearish", "neutral": "none"}.get(bias) != str(lean).lower():
        return f"lean field '{lean}' disagrees with the chart ({bias})"
    if facts.get("counter_trend") and not _COUNTER.search(text):
        return "does not say the idea is counter-trend"
    if not facts.get("counter_trend") and re.search(r"counter-?\s?trend", text, re.I):
        return "calls an idea counter-trend that agrees with the chart"
    issue = _level_direction_issue(text, facts)
    if issue:
        return issue
    return None


def clean(text: str) -> str:
    """One line, no Discord markdown or mentions."""
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"[*_`@~|>]", "", text).strip()


# ------------------------------------------------------------------------------------------------ deterministic fallback

def _lvl(s: str | None) -> str | None:
    """'29.53 (+2.5%)' -> '29.53'"""
    if not s:
        return None
    mm = _LVL.search(s)
    return mm.group(0) if mm else None


_MONEY = re.compile(r"\$\d[\d.,]*[MK]")


def _counter_fallback(facts: dict, sym: str, bias: str, above: str | None, below: str | None) -> str:
    """Plain two-sentence explanation of a counter-trend idea: what the chart is doing, what the options buyers are betting on,
    and the one thing that has to happen first."""
    is_call = "call" in str(facts.get("idea", ""))
    kind = facts.get("counter_kind", "against")
    if kind == "stretched":
        calm = " and the chart has no clear lean" if bias == "neutral" else ""
        if is_call:
            s1 = f"This is a counter-trend idea{calm}: buyers have already pushed {sym} up a long way, so a call now is chasing the move."
            s2 = f"A pullback can come first, and {below} is the level to hold." if below else "A pullback can come first."
        else:
            s1 = f"This is a counter-trend idea{calm}: sellers have already pushed {sym} down a long way, so a put now is chasing the move."
            s2 = f"A bounce can come first, and {above} is the level to clear for that." if above else "A bounce can come first."
        return f"{s1} {s2}"
    mm = _MONEY.search(str(facts.get("options_flow", "")))
    kind_word = "calls" if is_call else "puts"
    bet = f"big traders bought {mm.group(0)} of {kind_word}" if mm else f"big traders are betting on {kind_word}"
    if is_call:
        s1 = f"This is a counter-trend idea: {sym} is still falling, but {bet}, betting on a bounce."
        s2 = f"It only works if price turns up first, and a move back above {above} would be the first sign buyers are back." if above else "It only works if price turns up first."
    else:
        s1 = f"This is a counter-trend idea: {sym} is still rising, but {bet}, betting on a drop."
        s2 = f"It only works if price turns down first, and a slip under {below} would be the first sign sellers are taking over." if below else "It only works if price turns down first."
    return f"{s1} {s2}"


def fallback_read(facts: dict) -> str:
    """Two plain sentences built only from the facts (always passes check_read when levels exist)."""
    sym = facts.get("symbol", "This stock")
    state, bias = facts.get("trend", ""), facts.get("bias", "neutral")
    above, below = _lvl(facts.get("next_level_above")), _lvl(facts.get("first_level_below"))
    cloud = facts.get("price_vs_cloud")
    mag = cloud.lstrip("+-") if cloud else None
    if facts.get("counter_trend"):
        return clean(_counter_fallback(facts, sym, bias, above, below))
    elif state == "uptrend":
        s1 = f"Buyers are in control: {sym} sits {mag} above its average price and the uptrend is {facts.get('trend_momentum', 'holding')}."
    elif state == "downtrend":
        s1 = f"Sellers are in control: {sym} sits {mag} below its average price and the downtrend is {facts.get('trend_momentum', 'holding')}."
    elif state == "pullback":
        s1 = f"{sym} has slipped {mag} below its average price, but the longer uptrend has not turned yet, and sellers have the upper hand for now."
    elif state == "bounce":
        s1 = f"{sym} is bouncing {mag} above its average price, but the longer downtrend has not turned yet, and buyers have the upper hand for now."
    else:
        s1 = f"There is no clear lean: {sym} is stuck around its average price."
    if bias == "up":
        s2 = (f"The level to hold is {below} and the one to clear is {above}." if above and below else
              f"The level to hold is {below}." if below else f"The level to clear is {above}." if above else "")
    elif bias == "down":
        s2 = (f"A move back above {above} would be the first sign sellers are losing their grip, and {below} is the next level below." if above and below else
              f"A move back above {above} would be the first sign sellers are losing their grip." if above else f"The level to hold is {below}." if below else "")
    else:
        s2 = (f"A clean move past {above} or under {below} will show the next direction." if above and below else
              f"A clean move past {above} would show the next direction." if above else f"A clean move under {below} would show the next direction." if below else "")
    return clean(f"{s1} {s2}")


def fallback_backdrop(ctx: dict) -> str:
    parts = []
    for t in ("SPY", "QQQ", "IWM"):
        m = ctx.get(t) or {}
        if m.get("price") is not None and m.get("pct") is not None:
            parts.append(f"{t} at {m['price']:,.2f} ({m['pct']:+.2f}%)")
    return ("Markets closed with " + ", ".join(parts) + ".") if parts else "Markets closed."


# ------------------------------------------------------------------------------------------------ the model call

def _message(prompt: str, facts: dict, png: bytes | None) -> list[dict]:
    text = prompt + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
    if not png:
        return [{"role": "user", "content": text}]
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    return [{"role": "user", "content": [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": url}}]}]


def _post(api_key: str, body: dict, deadline_s: float):
    """requests.post with a TRUE wall-clock limit (the same daemon-thread guard the narrative call uses)."""
    out = {}

    def run():
        try:
            out["r"] = requests.post(URL, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=body, timeout=(10, deadline_s))
        except Exception as e:                      # surfaced below
            out["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(deadline_s)
    if t.is_alive():
        raise requests.exceptions.Timeout(f"no response within {deadline_s}s")
    if "e" in out:
        raise out["e"]
    return out["r"]


def _parse(content: str):
    a, b = content.find("{"), content.rfind("}")
    if a >= 0 and b > a:
        try:
            obj = json.loads(content[a:b + 1])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    return {}


def ask(api_key: str, model: str, prompt: str, facts: dict, checker, png: bytes | None = None, deadline_s: float = ATTEMPT_DEADLINE_S) -> dict:
    """{'read': str | None, 'cost': float, 'reason': str | None, 'attempts': int}. Never raises. `checker(read, lean) -> reason | None`."""
    cost, reason, use_image, attempts, rejections = 0.0, "no attempt", bool(png), 0, []
    for _ in range(ATTEMPTS):
        attempts += 1
        body = {"model": model, "messages": _message(prompt, facts, png if use_image else None), "max_tokens": 1500,
                "reasoning": {"effort": "low"}, "response_format": {"type": "json_object"}}
        status = None
        try:
            r = _post(api_key, body, deadline_s)
            status = r.status_code
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            if use_image and status in (400, 413, 415, 422):
                use_image = False                    # the image was refused: retry once as text only
                reason = f"image refused (http {status})"
                rejections.append(reason)
                continue
            return {"read": None, "cost": cost, "reason": f"call failed: {type(e).__name__}", "attempts": attempts, "rejections": rejections}
        cost += float((data.get("usage") or {}).get("cost") or 0.0)
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        obj = _parse(content)
        read = obj.get("read")
        read = clean(read) if isinstance(read, str) else None
        reason = checker(read, obj.get("lean"))
        if reason is None:
            return {"read": read, "cost": cost, "reason": None, "attempts": attempts, "rejections": rejections}
        rejections.append(reason)
    return {"read": None, "cost": cost, "reason": reason, "attempts": attempts, "rejections": rejections}


def read_for_setup(api_key: str | None, model: str, facts: dict, png: bytes | None) -> dict:
    """{'read', 'source': 'llm'|'fallback', 'cost', 'reason'}. The fallback is deterministic and always returns text."""
    res = {"read": None, "cost": 0.0, "reason": "no API key" if not api_key else None, "rejections": []}
    if api_key and facts.get("bias"):
        res = ask(api_key, model, PROMPT, facts, lambda r, lean: check_read(r, facts, lean), png)
    elif api_key:
        res["reason"] = "no chart facts"
    if res["read"]:
        return {"read": res["read"], "source": "llm", "cost": res["cost"], "reason": None, "rejections": res["rejections"]}
    return {"read": fallback_read(facts) if facts.get("bias") else None, "source": "fallback", "cost": res["cost"], "reason": res["reason"],
            "rejections": res["rejections"]}


# ------------------------------------------------------------------------------------------------ market backdrop

def backdrop_facts(ctx: dict, tone_fn=None) -> dict:
    out = {}
    for t in ("SPY", "QQQ", "IWM"):
        m = ctx.get(t) or {}
        if m.get("price") is None or m.get("pct") is None:
            continue
        out[t] = {"price": f"{m['price']:,.2f}", "change": f"{m['pct']:+.2f}%"}
        tone = tone_fn(m) if tone_fn else None
        if tone and tone != "N/A":
            out[t]["session"] = tone
    return out


def check_backdrop(text: str | None, facts: dict) -> str | None:
    if not text or len(text.strip()) < 20:
        return "empty or too short"
    if len(text.split()) > 45 or len(_SENTENCE_END.findall(text.strip())) > 1:
        return "too long or more than one sentence"
    if _BANNED.search(text) or _AI.search(text) or _JARGON.search(text):
        return "banned wording"
    if numbers(text) - numbers(json.dumps(facts)):
        return "number not in the data"
    if sum(1 for t in ("SPY", "QQQ", "IWM") if t in text) < 2:
        return "names fewer than two indexes"
    return None


def write_backdrop(api_key: str | None, model: str, ctx: dict, tone_fn=None) -> dict:
    facts = backdrop_facts(ctx, tone_fn)
    if api_key and len(facts) >= 2:
        res = ask(api_key, model, BACKDROP_PROMPT, facts, lambda r, lean: check_backdrop(r, facts))
        if res["read"]:
            return {"text": res["read"], "source": "llm", "cost": res["cost"], "reason": None}
        return {"text": fallback_backdrop(ctx), "source": "fallback", "cost": res["cost"], "reason": res["reason"]}
    return {"text": fallback_backdrop(ctx), "source": "fallback", "cost": 0.0, "reason": "no API key" if not api_key else "no market data"}


# ------------------------------------------------------------------------------------------------ all setups, in parallel

def run_reads(jobs: list[dict], api_key: str | None, model: str = DEFAULT_MODEL, deadline_s: float = 240.0) -> dict:
    """jobs: [{'ticker', 'facts', 'png' (bytes|None)}]. One model call per setup, all in parallel; one bad reply costs one card.
    Returns {ticker: result}. A job that has not finished by `deadline_s` gets the fallback (its thread is a daemon and cannot hold the process)."""
    results: dict = {}
    threads = []

    def work(job):
        try:
            results[job["ticker"]] = read_for_setup(api_key, model, job["facts"], job.get("png"))
        except Exception as e:                       # belt and braces: never let one setup break the others
            results[job["ticker"]] = {"read": fallback_read(job["facts"]) if job["facts"].get("bias") else None, "source": "fallback", "cost": 0.0,
                                      "reason": f"error: {type(e).__name__}"}

    for job in jobs:
        t = threading.Thread(target=work, args=(job,), daemon=True)
        t.start()
        threads.append(t)
    end = time.time() + deadline_s
    for t in threads:
        t.join(max(0.0, end - time.time()))
    for job in jobs:
        if job["ticker"] not in results:
            f = job["facts"]
            results[job["ticker"]] = {"read": fallback_read(f) if f.get("bias") else None, "source": "fallback", "cost": 0.0, "reason": "deadline"}
    return results
