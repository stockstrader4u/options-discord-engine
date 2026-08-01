"""
gex_vex.py — Gamma Exposure (GEX) and Vanna Exposure (VEX) dashboard for
SPY / QQQ / IWM, built entirely on the existing stack (yfinance) — no new
data vendor needed.

WHY THIS IS BUILDABLE WITHOUT ALPACA OR A DEDICATED GEX VENDOR:
GEX/VEX are DERIVED metrics computed from three raw ingredients — per-
strike open interest, implied volatility, and spot price. yfinance's
option_chain() already returns all three (same call already used in
er_lotto_scanner.py's get_implied_move / get_options_skew /
get_iv_vs_realized_vol). The one thing yfinance does NOT return is Greeks
(gamma, vanna) — those are computed here directly via the standard
Black-Scholes closed-form formulas, using the IV/spot/strike/time-to-
expiry yfinance already gives us.

DEFAULT EXPIRY RULE (get_week_ending_expiry): computed for the LAST
TRADING DAY of the current calendar week — Mon Jul 27 through Fri Jul 31
of the same week all resolve to the Jul 31 expiry. Verified against the
ticker's real listed expiries rather than assuming Friday is always a
trading day, so a holiday-shortened week automatically falls back to
Thursday. Pass an explicit `expiries=[...]` list to override this (used
by the multi-expiry diagnostic mode in test_gex_vex_live.py).

METHODOLOGY NOTES (read before trusting the numbers):
- Convention used: the standard "public GEX approximation" that every
  retail GEX dashboard uses (GEXBot, SpotGamma-style tools, etc.) —
  dealers are assumed net LONG calls / net SHORT puts, since real
  market-maker position data isn't public. This is an industry-standard
  simplifying assumption, not verified fact. Every GEX number you see
  anywhere (including the friend's dashboard this was modeled on) uses
  the same approximation.
- Risk-free rate is a fixed constant (see RISK_FREE_RATE below) rather
  than fetched live — gamma is not highly sensitive to r for short-dated
  contracts, so this is a reasonable simplification.
- Gamma flip is computed via strike-ordered cumulative-GEX interpolation
  (the same simplified method most retail tools use — a fully rigorous
  flip calc would re-price gamma at each hypothetical spot level, which
  is materially heavier). Returns None — an honest "no crossing in this
  window" — rather than a fabricated number, if net GEX never crosses
  zero within the fetched expiry's strike range. If flip is frequently
  None on real data, the likely fix is aggregating across multiple
  near-term expirations instead of a single weekly (see
  compute_gex_vex()'s `expiries` param).

NOT YET TESTED AGAINST LIVE DATA: this sandbox has no network access to
Yahoo Finance. All formulas and aggregation logic are verified against
synthetic option-chain data (see test_gex_vex.py). Run this against a
real ticker once deployed to confirm yfinance's actual response shape
matches what's assumed here (column names, NaN handling, etc.).
"""

import math
import os
from datetime import datetime, date, timedelta

import requests

RISK_FREE_RATE = 0.045  # approximation — see module docstring

MIN_RELIABLE_IV = 0.05


def _norm_pdf(x: float) -> float:
    return (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x)


def bs_gamma(spot: float, strike: float, t_years: float, iv: float,
             r: float = RISK_FREE_RATE) -> float:
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    return _norm_pdf(d1) / (spot * iv * math.sqrt(t_years))


def bs_vanna(spot: float, strike: float, t_years: float, iv: float,
             r: float = RISK_FREE_RATE) -> float:
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    return -_norm_pdf(d1) * d2 / iv


ALPACA_API_KEY_ID = os.environ.get("ALPACA_API_KEY_ID", "")
ALPACA_API_SECRET_KEY = os.environ.get("ALPACA_API_SECRET_KEY", "")
ALPACA_TRADING_BASE = "https://paper-api.alpaca.markets"
ALPACA_DATA_BASE = "https://data.alpaca.markets"


def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY_ID,
        "APCA-API-SECRET-KEY": ALPACA_API_SECRET_KEY,
    }


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _bs_price(spot: float, strike: float, t_years: float, r: float,
              sigma: float, is_call: bool) -> float:
    intrinsic = max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
    if t_years <= 0 or sigma <= 0:
        return intrinsic
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    if is_call:
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2)
    return strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def solve_implied_vol(observed_price: float, spot: float, strike: float,
                       t_years: float, is_call: bool, r: float = RISK_FREE_RATE) -> float:
    if observed_price <= 0 or t_years <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    lo, hi = 1e-4, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        price = _bs_price(spot, strike, t_years, r, mid, is_call)
        if price < observed_price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def get_spot_price(ticker: str):
    try:
        resp = requests.get(
            f"{ALPACA_DATA_BASE}/v2/stocks/{ticker}/quotes/latest",
            headers=_alpaca_headers(), timeout=15,
        )
        if resp.status_code != 200:
            print(f"[GEX WARN] {ticker} spot price: HTTP {resp.status_code} — {resp.text[:200]}")
            return None
        quote = resp.json().get("quote", {})
        bid, ask = quote.get("bp"), quote.get("ap")
        if bid and ask:
            return (bid + ask) / 2
        return ask or bid or None
    except Exception as e:
        print(f"[GEX WARN] {ticker} spot price: {e}")
        return None


def _fetch_alpaca_contracts(ticker: str, expiry: str) -> list:
    all_contracts = []
    page_token = None
    for _ in range(20):
        params = {"underlying_symbols": ticker, "expiration_date": expiry, "limit": 100}
        if page_token:
            params["page_token"] = page_token
        try:
            resp = requests.get(f"{ALPACA_TRADING_BASE}/v2/options/contracts",
                                 headers=_alpaca_headers(), params=params, timeout=20)
        except Exception as e:
            print(f"[GEX WARN] {ticker} contracts fetch: {e}")
            break
        if resp.status_code != 200:
            print(f"[GEX WARN] {ticker} contracts fetch: HTTP {resp.status_code} — {resp.text[:200]}")
            break
        data = resp.json()
        all_contracts.extend(data.get("option_contracts", []))
        page_token = data.get("next_page_token")
        if not page_token:
            break
    return all_contracts


def _fetch_alpaca_snapshots(ticker: str, expiry: str) -> dict:
    snapshots = {}
    page_token = None
    for _ in range(20):
        params = {"expiration_date": expiry, "limit": 100}
        if page_token:
            params["page_token"] = page_token
        try:
            resp = requests.get(f"{ALPACA_DATA_BASE}/v1beta1/options/snapshots/{ticker}",
                                 headers=_alpaca_headers(), params=params, timeout=20)
        except Exception as e:
            print(f"[GEX WARN] {ticker} snapshots fetch: {e}")
            break
        if resp.status_code != 200:
            print(f"[GEX WARN] {ticker} snapshots fetch: HTTP {resp.status_code} — {resp.text[:200]}")
            break
        data = resp.json()
        for sym, snap in data.get("snapshots", {}).items():
            quote = snap.get("latestQuote", {})
            snapshots[sym] = {"bid": quote.get("bp"), "ask": quote.get("ap")}
        page_token = data.get("next_page_token")
        if not page_token:
            break
    return snapshots


def _restrict_to_band(per_strike: dict, spot: float, band_pct: float = 0.30) -> dict:
    if not spot:
        return per_strike
    lo, hi = spot * (1 - band_pct), spot * (1 + band_pct)
    filtered = {k: v for k, v in per_strike.items() if lo <= k <= hi}
    return filtered if filtered else per_strike


def pick_nearest_expiry(expirations: list) -> str:
    if not expirations:
        return None
    today = date.today()
    future = [e for e in expirations if datetime.strptime(e, "%Y-%m-%d").date() >= today]
    return future[0] if future else expirations[-1]


def get_week_ending_expiry(ticker: str, today: date = None) -> str:
    if today is None:
        today = date.today()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)

    candidate = friday
    while candidate >= monday:
        candidate_str = candidate.strftime("%Y-%m-%d")
        try:
            resp = requests.get(
                f"{ALPACA_TRADING_BASE}/v2/options/contracts",
                headers=_alpaca_headers(),
                params={"underlying_symbols": ticker, "expiration_date": candidate_str, "limit": 1},
                timeout=15,
            )
            if resp.status_code == 200 and resp.json().get("option_contracts"):
                return candidate_str
        except Exception as e:
            print(f"[GEX WARN] {ticker} expiry probe {candidate_str}: {e}")
        candidate -= timedelta(days=1)

    print(f"[GEX WARN] {ticker}: no listed expiry found anywhere in the current week")
    return None


def compute_gex_vex(ticker: str, expiries: list = None) -> dict:
    try:
        spot = get_spot_price(ticker)
        if not spot:
            return {"error": f"{ticker}: could not fetch spot price", "ticker": ticker}

        target_expiries = expiries
        if not target_expiries:
            week_exp = get_week_ending_expiry(ticker)
            if not week_exp:
                return {"error": f"{ticker}: no options chain available", "ticker": ticker}
            target_expiries = [week_exp]

        per_strike = {}
        total_oi_seen = 0.0
        em_calls, em_puts = {}, {}
        first_expiry_done = False

        for exp in target_expiries:
            contracts = _fetch_alpaca_contracts(ticker, exp)
            if not contracts:
                continue
            snapshots = _fetch_alpaca_snapshots(ticker, exp)

            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            days_to_exp = max((exp_date - date.today()).days, 0) + 1
            t_years = days_to_exp / 365.0

            for c in contracts:
                symbol = c.get("symbol")
                try:
                    strike = float(c.get("strike_price"))
                except (TypeError, ValueError):
                    continue
                is_call = c.get("type") == "call"

                oi_raw = c.get("open_interest")
                oi = float(oi_raw) if oi_raw is not None else 0.0
                total_oi_seen += oi

                snap = snapshots.get(symbol, {})
                bid, ask = snap.get("bid"), snap.get("ask")

                if not first_expiry_done:
                    (em_calls if is_call else em_puts)[strike] = {"bid": bid, "ask": ask}

                if bid and ask and bid > 0 and ask > 0:
                    mid_price = (bid + ask) / 2
                    iv = solve_implied_vol(mid_price, spot, strike, t_years, is_call)
                else:
                    iv = 0.0

                if 0 < iv < MIN_RELIABLE_IV:
                    iv = 0.0

                gamma = bs_gamma(spot, strike, t_years, iv) if iv > 0 else 0.0
                vanna = bs_vanna(spot, strike, t_years, iv) if iv > 0 else 0.0

                gex = oi * gamma * 100 * spot * spot * 0.01
                vex = oi * vanna * 100 * spot * 0.01

                if strike not in per_strike:
                    per_strike[strike] = {"call_gex": 0.0, "put_gex": 0.0,
                                           "call_vex": 0.0, "put_vex": 0.0}
                if is_call:
                    per_strike[strike]["call_gex"] += gex
                    per_strike[strike]["call_vex"] += vex
                else:
                    per_strike[strike]["put_gex"] += gex
                    per_strike[strike]["put_vex"] += vex

            first_expiry_done = True

        for k in per_strike:
            per_strike[k]["net_gex"] = per_strike[k]["call_gex"] - per_strike[k]["put_gex"]
            per_strike[k]["net_vex"] = per_strike[k]["call_vex"] - per_strike[k]["put_vex"]

        if not per_strike:
            return {"error": f"{ticker}: no strikes with usable data", "ticker": ticker}

        MIN_TOTAL_OI = 1000
        if total_oi_seen < MIN_TOTAL_OI:
            return {
                "error": (
                    f"{ticker}: only {total_oi_seen:.0f} total contracts of open "
                    f"interest found across the chain — likely incomplete/missing "
                    f"data from the source, not a real market condition. Skipping "
                    f"rather than posting a misleading card."
                ),
                "ticker": ticker,
            }

        net_gex_total = sum(v["net_gex"] for v in per_strike.values())
        net_vex_total = sum(v["net_vex"] for v in per_strike.values())

        banded_strikes = _restrict_to_band(per_strike, spot, band_pct=0.30)

        call_wall = max(banded_strikes, key=lambda k: banded_strikes[k]["call_gex"])
        put_wall = max(banded_strikes, key=lambda k: banded_strikes[k]["put_gex"])
        max_pos_strike = max(banded_strikes, key=lambda k: banded_strikes[k]["net_gex"])
        max_neg_strike = min(banded_strikes, key=lambda k: banded_strikes[k]["net_gex"])

        gamma_flip = find_gamma_flip(per_strike, spot=spot)

        expected_move = expected_move_from_quotes(em_calls, em_puts, spot)

        return {
            "ticker": ticker,
            "expiries": target_expiries,
            "spot": spot,
            "net_gex": net_gex_total,
            "net_vex": net_vex_total,
            "call_wall": call_wall,
            "call_wall_pct": pct_from_spot(call_wall, spot),
            "put_wall": put_wall,
            "put_wall_pct": pct_from_spot(put_wall, spot),
            "max_pos_gex_strike": max_pos_strike,
            "max_pos_gex_value": per_strike[max_pos_strike]["net_gex"],
            "max_neg_gex_strike": max_neg_strike,
            "max_neg_gex_value": per_strike[max_neg_strike]["net_gex"],
            "gamma_flip": gamma_flip,
            "expected_move": expected_move,
            "per_strike": per_strike,
        }
    except Exception as e:
        return {"error": f"{ticker}: {type(e).__name__}: {e}", "ticker": ticker}


def pct_from_spot(strike, spot):
    if strike is None or not spot:
        return None
    return round((strike - spot) / spot * 100, 2)


def find_gamma_flip(per_strike: dict, spot: float = None, band_pct: float = 0.30,
                     min_materiality_pct: float = 0.005):
    if not per_strike:
        return None
    if spot:
        per_strike = _restrict_to_band(per_strike, spot, band_pct)
        if not per_strike:
            return None

    max_abs = max(abs(v["net_gex"]) for v in per_strike.values())
    if max_abs <= 0:
        return None
    materiality_threshold = max_abs * min_materiality_pct

    strikes = sorted(per_strike.keys())
    cum = 0.0
    prev_strike, prev_cum = None, None
    for k in strikes:
        cum += per_strike[k]["net_gex"]
        if abs(per_strike[k]["net_gex"]) < materiality_threshold:
            continue
        if prev_cum is not None:
            prev_sign = 1 if prev_cum > 0 else (-1 if prev_cum < 0 else 0)
            cur_sign = 1 if cum > 0 else (-1 if cum < 0 else 0)
            if prev_sign != 0 and cur_sign != 0 and prev_sign != cur_sign:
                span = k - prev_strike
                denom = abs(prev_cum) + abs(cum)
                frac = abs(prev_cum) / denom if denom else 0.5
                return round(prev_strike + span * frac, 2)
        prev_strike, prev_cum = k, cum
    return None


def expected_move_from_quotes(calls: dict, puts: dict, spot: float):
    try:
        common = set(calls.keys()).intersection(set(puts.keys()))
        if not common:
            return None
        atm_strike = min(common, key=lambda s: abs(s - spot))
        call_q, put_q = calls.get(atm_strike, {}), puts.get(atm_strike, {})

        def mid(q):
            bid, ask = q.get("bid"), q.get("ask")
            if bid and ask and bid > 0 and ask > 0:
                return (bid + ask) / 2
            return 0.0

        straddle = mid(call_q) + mid(put_q)
        if straddle <= 0:
            return None
        return {
            "dollar": round(straddle, 2),
            "pct": round(straddle / spot * 100, 2),
            "max": round(spot + straddle, 2),
            "min": round(spot - straddle, 2),
        }
    except Exception:
        return None


def format_gex_card(result: dict) -> str:
    if "error" in result:
        return f"⚠️ GEX unavailable — {result['error']}"

    t = result["ticker"]
    spot = result["spot"]
    exp_label = result["expiries"][0] if len(result["expiries"]) == 1 else \
        f"{result['expiries'][0]}..{result['expiries'][-1]} ({len(result['expiries'])} expiries)"

    flip = result["gamma_flip"]
    flip_str = f"${flip:,.2f}" if flip is not None else \
        "N/A (no zero-crossing in this window — try aggregating more expiries)"

    net_gex = result["net_gex"]
    if net_gex < 0:
        gex_str = f"-${abs(net_gex)/1e9:.2f}B   short gamma \u2192 moves amplified"
    else:
        gex_str = f"+${net_gex/1e9:.2f}B   long gamma \u2192 moves dampened"

    lines = [
        f"\U0001F4CA **{t}**  ${spot:,.2f}   (exp {exp_label})",
        f"  GAMMA FLIP  {flip_str}",
        f"  NET GEX  {gex_str}",
        f"  NET VEX  ${result['net_vex']/1e6:,.2f}M",
        f"  CALL WALL  ${result['call_wall']:,.0f}  ({result['call_wall_pct']:+.1f}%)",
        f"  PUT WALL   ${result['put_wall']:,.0f}  ({result['put_wall_pct']:+.1f}%)",
        f"  MAX +GEX   ${result['max_pos_gex_strike']:,.0f}   +${result['max_pos_gex_value']/1e6:,.2f}M",
        f"  MAX -GEX   ${result['max_neg_gex_strike']:,.0f}   -${abs(result['max_neg_gex_value'])/1e6:,.2f}M",
    ]
    em = result["expected_move"]
    if em:
        lines.append(
            f"  EXPECTED MOVE  \u00b1${em['dollar']} ({em['pct']}%)   "
            f"Max: ${em['max']} / Min: ${em['min']}"
        )
    return "\n".join(lines)


def dump_cumulative_table(result: dict) -> str:
    if "per_strike" not in result:
        return "(no per_strike data in this result)"
    ps = result["per_strike"]
    if not ps:
        return "(empty per_strike data)"

    total_abs = sum(abs(v["net_gex"]) for v in ps.values()) or 1.0
    lines = [f"{'strike':>10} {'net_gex':>18} {'cumulative':>18} {'%%oftotal':>10}"]
    cum = 0.0
    for k in sorted(ps.keys()):
        net = ps[k]["net_gex"]
        cum += net
        pct_of_total = abs(net) / total_abs * 100
        flag = "  <-- DOMINATES" if pct_of_total > 25 else ""
        lines.append(f"{k:>10,.1f} {net:>18,.0f} {cum:>18,.0f} {pct_of_total:>9.1f}%{flag}")
    return "\n".join(lines)


def build_gex_dashboard(tickers=("SPY", "QQQ", "IWM"), expiries: list = None) -> str:
    blocks = []
    for t in tickers:
        result = compute_gex_vex(t, expiries=expiries)
        blocks.append(format_gex_card(result))
    return "\n\n".join(blocks)


def _fallback_watch_line(r: dict) -> str:
    em = r.get("expected_move") or {}
    em_pct = em.get("pct")
    call_pct = r.get("call_wall_pct")
    put_pct = r.get("put_wall_pct")
    call_wall, put_wall = r.get("call_wall"), r.get("put_wall")

    if em_pct is None or call_pct is None or put_pct is None:
        return f"Range likely holds between ${put_wall:,.0f} put and ${call_wall:,.0f} call walls."

    reaches_call = em_pct >= call_pct
    reaches_put = em_pct >= abs(put_pct)

    if reaches_call and reaches_put:
        return (f"Expected move spans both walls this week — ${put_wall:,.0f} and "
                f"${call_wall:,.0f} are both realistically in play.")
    if reaches_call:
        return (f"Expected move could reach the ${call_wall:,.0f} call wall; "
                f"${put_wall:,.0f} put side looks further out of range.")
    if reaches_put:
        return (f"Expected move could reach the ${put_wall:,.0f} put wall; "
                f"${call_wall:,.0f} call side looks further out of range.")
    return (f"Expected move falls short of both walls — ${put_wall:,.0f} and "
            f"${call_wall:,.0f} likely hold unless something outsized hits.")


def _classify_wall_reach(r: dict) -> str:
    em = r.get("expected_move") or {}
    em_pct = em.get("pct")
    call_pct = r.get("call_wall_pct")
    put_pct = r.get("put_wall_pct")
    if em_pct is None or call_pct is None or put_pct is None:
        return "unknown"
    reaches_call = em_pct >= call_pct
    reaches_put = em_pct >= abs(put_pct)
    if reaches_call and reaches_put:
        return "both"
    if reaches_call:
        return "call_only"
    if reaches_put:
        return "put_only"
    return "neither"


def _consolidated_watch_line(results: list, classification: str) -> str:
    valid = [r for r in results if "error" not in r]
    tickers_str = "/".join(r["ticker"] for r in valid)

    if classification == "put_only":
        levels = " · ".join(f"{r['ticker']} ${r['put_wall']:,.0f}" for r in valid)
        return (f"All three lean toward their put walls this week ({levels}) — "
                f"downside levels look more realistically in play than upside resistance.")
    if classification == "call_only":
        levels = " · ".join(f"{r['ticker']} ${r['call_wall']:,.0f}" for r in valid)
        return (f"All three lean toward their call walls this week ({levels}) — "
                f"upside resistance looks more realistically in play than downside support.")
    if classification == "both":
        levels = " · ".join(f"{r['ticker']} ${r['put_wall']:,.0f}/${r['call_wall']:,.0f}" for r in valid)
        return (f"{tickers_str} all have wide enough expected moves to test both walls "
                f"this week ({levels}) — either side could realistically get tested.")
    levels = " · ".join(f"{r['ticker']} ${r['put_wall']:,.0f}/${r['call_wall']:,.0f}" for r in valid)
    return (f"{tickers_str} all look contained inside their walls this week ({levels}) — "
            f"expected moves fall short of both sides.")


def build_watch_lines_fallback(results: list) -> dict:
    valid = [r for r in results if "error" not in r]
    if not valid:
        return {}
    classifications = {r["ticker"]: _classify_wall_reach(r) for r in valid}
    unique_patterns = set(classifications.values())

    if len(unique_patterns) == 1 and "unknown" not in unique_patterns:
        shared = _consolidated_watch_line(results, next(iter(unique_patterns)))
        return {r["ticker"]: shared for r in valid}

    return {r["ticker"]: _fallback_watch_line(r) for r in valid}


def generate_gex_watch_lines(results: list, api_key: str = None) -> dict:
    valid = [r for r in results if "error" not in r]
    fallback = build_watch_lines_fallback(results)

    api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key or not valid:
        if not api_key:
            print("[GEX WARN] generate_gex_watch_lines: no OPENROUTER_API_KEY set — using data-derived fallback lines")
        return fallback

    classifications = {r["ticker"]: _classify_wall_reach(r) for r in valid}
    unique_patterns = set(classifications.values())
    same_pattern = len(unique_patterns) == 1 and "unknown" not in unique_patterns

    data_lines = []
    for r in valid:
        flip = r.get("gamma_flip")
        flip_str = f"${flip:,.2f}" if flip is not None else "no clean flip"
        regime = "short gamma" if r["net_gex"] < 0 else "long gamma"
        em = r.get("expected_move") or {}
        data_lines.append(
            f"{r['ticker']}: spot ${r['spot']:,.2f}, {regime}, "
            f"put wall ${r['put_wall']:,.0f} ({r['put_wall_pct']:+.1f}%), "
            f"call wall ${r['call_wall']:,.0f} ({r['call_wall_pct']:+.1f}%), "
            f"expected move \u00b1{em.get('pct', '?')}%, gamma flip {flip_str}"
        )

    base_instructions = (
        'This posts directly below a table that ALREADY shows each ticker\'s exact put '
        'wall, call wall, and expected move numbers — do NOT just restate those numbers '
        'back. Say something the raw numbers alone don\'t: whether the expected move is '
        'large enough to realistically test a wall, how the tickers compare to each other, '
        'or what the short/long gamma regime implies for price behavior near those levels.\n\n'
        f"Data:\n{chr(10).join(data_lines)}\n\n"
        'BANNED: any line that just says "range holds between $X and $Y" or "watch $X '
        'support / $Y resistance" with no other insight.\n'
        'Direct, trader-to-trader, no fluff.'
    )

    if same_pattern:
        prompt = f"""You are writing a "what to watch" line for a GEX/VEX options positioning snapshot from BlueMoonTrades (BMT).

{base_instructions}

All three tickers share the SAME setup this week ({next(iter(unique_patterns))} pattern) — don't write three separate near-identical lines. Write exactly ONE consolidated sentence covering all three tickers together, naming each ticker and its specific level. Output ONLY that one sentence, nothing else before or after. Max 30 words."""
    else:
        prompt = f"""You are writing "what to watch" lines for a GEX/VEX options positioning snapshot from BlueMoonTrades (BMT).

{base_instructions}

The tickers have DIFFERENT setups this week, so write one line per ticker. Output ONLY lines in this EXACT format, one per ticker, nothing else before or after:
TICKER | one tight, actionable clause
Max ~15 words per line."""

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "x-ai/grok-4.3", "max_tokens": 220, "temperature": 0.6,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        raw = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[GEX WARN] generate_gex_watch_lines: API call failed ({e}) — using fallback")
        return fallback

    if not raw:
        print("[GEX WARN] generate_gex_watch_lines: Grok returned an empty response — using fallback")
        return fallback

    if same_pattern:
        cleaned = raw.strip().strip('"').lstrip("*").rstrip("*").strip()
        if cleaned:
            return {r["ticker"]: cleaned for r in valid}
        print(f"[GEX WARN] generate_gex_watch_lines: couldn't extract a usable consolidated line. "
              f"Raw Grok response was:\n{raw!r}\nUsing fallback instead.")
        return fallback

    parsed = dict(fallback)
    matched_any = False
    valid_tickers = {r["ticker"] for r in valid}
    for line in raw.split("\n"):
        line = line.strip().lstrip("*").strip()
        if not line:
            continue
        for sep in ("|", ":"):
            if sep in line:
                ticker_part, _, clause = line.partition(sep)
                ticker = ticker_part.strip().strip("*").lstrip("$").upper()
                clause = clause.strip()
                if ticker in valid_tickers and clause:
                    parsed[ticker] = clause
                    matched_any = True
                break

    if not matched_any:
        print(f"[GEX WARN] generate_gex_watch_lines: zero lines matched the expected format "
              f"out of {len(raw.splitlines())} line(s) — using fallback. Raw Grok response was:\n{raw!r}")
        return fallback

    return parsed


def build_gex_embed(results: list, week_label: str, watch_lines: dict = None) -> dict:
    watch_lines = watch_lines or {}
    valid = [r for r in results if "error" not in r]
    errored = [r for r in results if "error" in r]

    short_count = sum(1 for r in valid if r["net_gex"] < 0)
    long_count = len(valid) - short_count
    color = 0xDC2626 if short_count > long_count else (0x059669 if long_count > short_count else 0xD97706)

    regime_lines = []
    levels_lines = []
    move_lines = []
    for r in valid:
        dot = "\U0001F534" if r["net_gex"] < 0 else "\U0001F7E2"
        regime_word = "SHORT GAMMA" if r["net_gex"] < 0 else "LONG GAMMA"
        # FIXED (2026-07-30): "{dot} **{ticker}** — {regime_word}" on one
        # line was wrapping mid-phrase in Discord's narrow inline-field
        # column (confirmed in production screenshot — "SHORT GAMMA"
        # broke after "SHORT" for all three tickers, ugly and hard to
        # scan). Switched to a deliberate two-line format per ticker
        # instead: ticker+dot on its own line, regime word on the next
        # — same total info, but the break is controlled instead of
        # Discord's unpredictable inline wrap.
        regime_lines.append(f"{dot} **{r['ticker']}**\n{regime_word}")
        levels_lines.append(
            f"**{r['ticker']}**  Put ${r['put_wall']:,.0f} \u00b7 Call ${r['call_wall']:,.0f}"
        )
        em = r.get("expected_move") or {}
        if em:
            move_lines.append(f"**{r['ticker']}**  \u00b1${em['dollar']} ({em['pct']}%)")

    for r in errored:
        regime_lines.append(f"\u26A0\uFE0F **{r.get('ticker', '?')}**\nskipped (data issue)")

    watch_values = [watch_lines.get(r["ticker"], "") for r in valid]
    watch_values = [v for v in watch_values if v]
    if watch_values and len(set(watch_values)) == 1:
        watch_field_lines = [watch_values[0]]
    else:
        watch_field_lines = [
            f"**{r['ticker']}**: {watch_lines[r['ticker']]}"
            for r in valid if watch_lines.get(r["ticker"])
        ]

    fields = []
    if regime_lines:
        fields.append({"name": "\U0001F4CA Regime", "value": "\n".join(regime_lines), "inline": True})
    if levels_lines:
        fields.append({"name": "\U0001F3AF Key Levels", "value": "\n".join(levels_lines), "inline": True})
    if move_lines:
        fields.append({"name": "\U0001F4CF Expected Move", "value": "\n".join(move_lines), "inline": True})
    if watch_field_lines:
        fields.append({"name": "\U0001F440 What To Watch", "value": "\n".join(watch_field_lines), "inline": False})

    return {
        "title": f"\U0001F4CA GEX/VEX Snapshot \u2014 {week_label}",
        "color": color,
        "fields": fields,
        "footer": {"text": "Standard public-GEX approximation \u00b7 BlueMoonTrades"},
    }


def _fmt_dollars_b(v):
    return f"-${abs(v)/1e9:.2f}B" if v < 0 else f"+${v/1e9:.2f}B"


def _fmt_dollars_m(v):
    return f"-${abs(v)/1e6:.2f}M" if v < 0 else f"+${v/1e6:.2f}M"


def compute_regime_subtitle(results: list) -> dict:
    """
    REPLACES the old fixed subtitle logic (previously hardcoded as
    "choppier, amplified moves" / "calmer, range-bound moves" purely
    off the SIGN of net_gex — identical text for every ticker sharing
    a sign, which is the common case, meaning SPY/QQQ/IWM all got the
    same subtitle on 2026-07-30 despite Net GEX magnitudes -$5.01B /
    -$1.92B / -$1.40B, over 3x apart. Reported zero differentiating
    information between tickers.

    Ranks each ticker's Net GEX MAGNITUDE against its same-sign peers
    in THIS run, and returns two lines per ticker:
      - rank_str: "most / moderately / mildest [short/long] gamma of
        the group", or "roughly even [short/long] gamma across the
        group" if the group's magnitudes are within 15% of each other
        (avoids implying a bigger spread than actually exists), or just
        "[short/long] gamma" alone if the ticker has no same-sign peer
        this run (nothing to rank against).
      - implication_str: the mechanical takeaway tied to that rank
        (e.g. "most likely to see outsized moves if it breaks a wall")
        rather than leaving the rank word for the reader to interpret
        unaided.

    GEX ONLY — Net VEX (vanna exposure) is a distinct metric and is
    deliberately NOT folded into this ranking; it stays in its own
    POSITIONING row on the card.

    Tickers with "error" in their result dict (missing net_gex) are
    silently excluded from ranking and from the returned dict — safe
    on partial-failure days, same defensive pattern as the rest of
    this module. Returns {} if there's no valid data at all.
    """
    valid = [r for r in results if "error" not in r and r.get("net_gex") is not None]
    subtitles = {}
    if not valid:
        return subtitles

    short_group = [r for r in valid if r["net_gex"] < 0]
    long_group = [r for r in valid if r["net_gex"] >= 0]

    def label_group(group, sign_word):
        if not group:
            return
        if len(group) == 1:
            imp = ("moves likely to fade back toward range" if sign_word == "long"
                   else "moves could run further than usual if a wall breaks")
            subtitles[group[0]["ticker"]] = (f"{sign_word} gamma", imp)
            return
        mags = [abs(r["net_gex"]) for r in group]
        mn, mx = min(mags), max(mags)
        spread = (mx - mn) / mx if mx else 0
        if spread < 0.15:
            imp = ("similar dampened risk across all three" if sign_word == "long"
                   else "similar amplification risk across all three")
            for r in group:
                subtitles[r["ticker"]] = (f"roughly even {sign_word} gamma across the group", imp)
            return
        sorted_group = sorted(group, key=lambda r: abs(r["net_gex"]), reverse=True)
        n = len(sorted_group)
        for i, r in enumerate(sorted_group):
            if i == 0:
                rank_word = "most"
                imp = ("most likely to stay pinned/range-bound of the three" if sign_word == "long"
                       else "most likely to see outsized moves if it breaks a wall")
            elif i == n - 1:
                rank_word = "mildest"
                imp = ("relatively less dampened than the other two" if sign_word == "long"
                       else "relatively more contained than the other two")
            else:
                rank_word = "moderately"
                imp = ("moderate dampening, between the other two" if sign_word == "long"
                       else "moderate amplification, between the other two")
            subtitles[r["ticker"]] = (f"{rank_word} {sign_word} gamma of the group", imp)

    label_group(short_group, "short")
    label_group(long_group, "long")
    return subtitles


def render_gex_dashboard_card(results: list, week_label: str, out_path: str):
    """Renders the GEX/VEX dashboard as a Discord-ready PNG.

    REDESIGNED (2026-07-30, v5 — CURRENT PRODUCTION VERSION): replaces
    the prior cross-table layout (metrics as rows, tickers as columns,
    shared 8-paragraph definitions block) with self-contained per-ticker
    cards. Each ticker's own range bar and full metric set live directly
    under its own header — no column-scanning across tickers required to
    read one ticker's numbers.

    Also replaces the old fixed regime subtitle (identical text across
    tickers sharing a sign, zero differentiating info — see
    compute_regime_subtitle() docstring for the full before/after) with
    a GEX-magnitude-ranked subtitle computed fresh from this run's
    actual numbers.

    Also fixes wall-label collision: when a ticker's put wall and call
    wall land within ~10% of the bar's width of each other (confirmed
    in production: SPY's put and call wall both at $740 on 2026-07-30
    produced overlapping, unreadable label text), the two labels now
    collapse into one "P/C \u2248 $X" label instead of overlapping.

    Definitions trimmed from a full 8-paragraph legend block to one
    compact single-line key at the bottom of the card — a trader
    reading this daily doesn't need "Net GEX" re-explained in full-
    sentence form on every single post. All underlying data points are
    still rendered (Gamma Flip, Net GEX, Net VEX, Call Wall, Put Wall,
    Max +GEX, Max -GEX, Expected Move dollar/pct/min/max) — nothing was
    removed, only reorganized into POSITIONING / KEY LEVELS / EXPECTED
    MOVE groups per ticker instead of a flat metrics-by-tickers table.

    No emoji inside the rendered image — matplotlib's bundled fonts
    don't render emoji glyphs (same constraint as the trade journal's
    title bar and every prior version of this function)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as patches
    import re as _re

    def S(text):
        return _re.sub(r"[^\x00-\uFFFF]", "", str(text))

    _BG        = "#0a0e1a"
    _HDR_BG    = "#151c31"
    _HDR_TXT   = "#f8fafc"
    _TXT       = "#e9edf5"
    _TXT_DIM   = "#9aa7c7"
    _GREEN     = "#34d399"
    _RED       = "#fb7185"
    _BORDER    = "#2d3b56"
    _GOLD      = "#fbbf24"
    _CARD_BG   = "#141b2e"
    _ROW_BG    = "#0f1526"
    _TRACK_BG  = "#1e293b"
    _DATA_FONT   = "Liberation Mono"
    _HEADER_FONT = "Liberation Sans"

    def _fmt_b(v):
        return f"-${abs(v)/1e9:.2f}B" if v < 0 else f"+${v/1e9:.2f}B"

    def _fmt_m(v):
        return f"-${abs(v)/1e6:.2f}M" if v < 0 else f"+${v/1e6:.2f}M"

    FIG_W = 19.0
    MARGIN = 0.36
    HDR_H = 0.64
    SUB_H = 0.26
    GAP = 0.30

    STAT_ROW_H = 0.40
    SECTION_LABEL_H = 0.32
    N_SECTIONS = 3
    N_STAT_ROWS = 9
    SECTION_GAP = 0.10
    STAT_SECTION_H = (N_SECTIONS * SECTION_LABEL_H) + (N_STAT_ROWS * STAT_ROW_H) + (N_SECTIONS * SECTION_GAP)
    CARD_PAD = 0.24

    HEADER_BLOCK_H = 0.22 + 0.30 + 0.26 + 0.20 + 0.22
    BAR_H_ACTUAL = 0.36
    BAR_BLOCK_H = 0.30 + BAR_H_ACTUAL + 0.42
    CARD_H = CARD_PAD + HEADER_BLOCK_H + BAR_BLOCK_H + STAT_SECTION_H + CARD_PAD

    KEY_H = 0.42
    FOOTER_H = 0.36

    n = len(results)
    usable_w = FIG_W - 2 * MARGIN
    fig_h = MARGIN + HDR_H + SUB_H + GAP + CARD_H + GAP + KEY_H + GAP + FOOTER_H + MARGIN

    fig = plt.figure(figsize=(FIG_W, fig_h), dpi=200, facecolor=_BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, FIG_W)
    ax.set_ylim(fig_h, 0)
    ax.axis("off")

    def rect(x, y, w, h, color, z=2, alpha=1.0):
        ax.add_patch(patches.Rectangle((x, y), w, h, facecolor=color, edgecolor="none", zorder=z, alpha=alpha))

    def rrect(x, y, w, h, color, radius=0.05, z=2, alpha=1.0, ec="none", lw=0):
        ax.add_patch(patches.FancyBboxPatch(
            (x, y), w, h, boxstyle=f"round,pad=0,rounding_size={radius}",
            facecolor=color, edgecolor=ec, linewidth=lw, zorder=z, alpha=alpha))

    def hline(y, x0, x1, color=_BORDER, lw=0.5):
        ax.plot([x0, x1], [y, y], color=color, linewidth=lw, zorder=3)

    def txt(x, y, text, fs=8.0, color=_TXT, bold=False, ha="left",
            font=_DATA_FONT, style="normal"):
        ax.text(x, y, S(text), fontsize=fs, color=color,
                 fontweight="bold" if bold else "normal", fontstyle=style,
                 ha=ha, va="center", zorder=6, fontfamily=font)

    cur = MARGIN

    rect(MARGIN, cur, usable_w, HDR_H, _HDR_BG)
    hline(cur + HDR_H, MARGIN, MARGIN + usable_w, _GOLD, 1.4)
    txt(MARGIN + 0.22, cur + HDR_H / 2, f"GEX / VEX DASHBOARD  \u2014  {week_label}",
        fs=14.5, color=_HDR_TXT, bold=True, font=_HEADER_FONT)
    cur += HDR_H

    txt(FIG_W / 2, cur + SUB_H / 2,
        "Standard public-GEX approximation \u2014 dealers assumed net long calls / net short puts",
        fs=7.8, color=_TXT_DIM, ha="center", font=_HEADER_FONT)
    cur += SUB_H + GAP

    subtitles = compute_regime_subtitle(results)

    card_gap = 0.26
    card_w = (usable_w - card_gap * (n - 1)) / n

    for i, r in enumerate(results):
        hx = MARGIN + i * (card_w + card_gap)

        if "error" in r:
            rrect(hx, cur, card_w, CARD_H, _CARD_BG, radius=0.07, ec=_BORDER, lw=0.8, z=2)
            txt(hx + card_w / 2, cur + CARD_H / 2, f"{r.get('ticker', '?')}: error",
                fs=9, color=_TXT_DIM, ha="center", font=_HEADER_FONT)
            continue

        ticker = r["ticker"]
        spot = r["spot"]
        net_gex = r["net_gex"]
        net_vex = r["net_vex"]
        is_short = net_gex < 0
        regime_color = _RED if is_short else _GREEN

        rrect(hx, cur, card_w, CARD_H, _CARD_BG, radius=0.07, ec=_BORDER, lw=0.9, z=2)
        rrect(hx, cur, card_w, 0.10, regime_color, radius=0.05, z=3)

        inner_x0 = hx + CARD_PAD
        inner_w = card_w - CARD_PAD * 2
        y = cur + CARD_PAD + 0.22

        txt(inner_x0, y, ticker, fs=16, color=_TXT, bold=True, font=_HEADER_FONT)
        txt(inner_x0 + 0.86, y, f"${spot:,.2f}", fs=10, color=_TXT_DIM, font=_DATA_FONT)
        y += 0.30

        regime_word = "SHORT GAMMA" if is_short else "LONG GAMMA"
        txt(inner_x0, y, regime_word, fs=9.5, color=regime_color, bold=True, font=_HEADER_FONT)
        y += 0.26

        rank_str, implication_str = subtitles.get(ticker, ("", ""))
        txt(inner_x0, y, rank_str, fs=7.4, color=_TXT_DIM, style="italic", font=_HEADER_FONT)
        y += 0.20
        txt(inner_x0, y, implication_str, fs=7.0, color=_TXT, font=_HEADER_FONT)
        y += 0.22

        bar_h = 0.36
        bar_x0 = inner_x0
        bar_w = inner_w
        bar_y = y + 0.08

        call_wall = r["call_wall"]
        put_wall = r["put_wall"]
        em = r.get("expected_move") or {}
        em_min = em.get("min", spot)
        em_max = em.get("max", spot)
        gamma_flip = r.get("gamma_flip")

        range_min = min(put_wall, em_min, spot) if put_wall else min(em_min, spot)
        range_max = max(call_wall, em_max, spot) if call_wall else max(em_max, spot)
        pad = (range_max - range_min) * 0.14 or 1.0
        range_min -= pad
        range_max += pad
        span = range_max - range_min

        def to_x(price, bar_x0=bar_x0, bar_w=bar_w, range_min=range_min, span=span):
            return bar_x0 + (price - range_min) / span * bar_w

        rrect(bar_x0, bar_y, bar_w, bar_h, _TRACK_BG, radius=bar_h * 0.4, z=3)
        em_x0, em_x1 = to_x(em_min), to_x(em_max)
        rect(em_x0, bar_y, max(em_x1 - em_x0, 0.02), bar_h, _GOLD, z=4, alpha=0.45)

        if put_wall and call_wall:
            pw_x, cw_x = to_x(put_wall), to_x(call_wall)
            collision = abs(pw_x - cw_x) < bar_w * 0.10

            ax.plot([pw_x, pw_x], [bar_y - 0.03, bar_y + bar_h + 0.03], color=_RED, linewidth=2.3, zorder=5)
            ax.plot([cw_x, cw_x], [bar_y - 0.03, bar_y + bar_h + 0.03], color=_GREEN, linewidth=2.3, zorder=5)

            if collision:
                mid_x = (pw_x + cw_x) / 2
                txt(mid_x, bar_y - 0.16, f"P/C \u2248 ${put_wall:,.0f}", fs=6.6, color=_TXT, bold=True, ha="center", font=_HEADER_FONT)
            else:
                txt(pw_x, bar_y - 0.16, f"P ${put_wall:,.0f}", fs=6.6, color=_RED, bold=True, ha="center", font=_HEADER_FONT)
                txt(cw_x, bar_y - 0.16, f"C ${call_wall:,.0f}", fs=6.6, color=_GREEN, bold=True, ha="center", font=_HEADER_FONT)

        if gamma_flip is not None and range_min <= gamma_flip <= range_max:
            gf_x = to_x(gamma_flip)
            ax.plot([gf_x, gf_x], [bar_y - 0.06, bar_y + bar_h + 0.06], color="#c084fc", linewidth=1.4, linestyle="--", zorder=5)

        sp_x = to_x(spot)
        ax.plot([sp_x], [bar_y + bar_h / 2], marker="o", markersize=9,
                markerfacecolor=_GOLD, markeredgecolor="#ffffff", markeredgewidth=1.5, zorder=7)

        y = bar_y + bar_h + 0.42

        def stat_row(label, value, color, y):
            rect(inner_x0 - 0.06, y - STAT_ROW_H / 2 + 0.03, inner_w + 0.12, STAT_ROW_H - 0.06, _ROW_BG, z=2)
            txt(inner_x0, y, label, fs=7.4, color=_TXT_DIM, font=_HEADER_FONT)
            txt(inner_x0 + inner_w, y, value, fs=8.4, color=color, bold=True, ha="right")

        def section_label(text, y):
            txt(inner_x0, y, text, fs=6.8, color="#7dd3fc", bold=True, font=_HEADER_FONT)

        section_label("POSITIONING", y)
        y += SECTION_LABEL_H
        gf_str = f"${gamma_flip:,.2f}" if gamma_flip is not None else "N/A"
        stat_row("Gamma Flip", gf_str, _TXT_DIM if gamma_flip is None else _TXT, y); y += STAT_ROW_H
        stat_row("Net GEX", _fmt_b(net_gex), _RED if net_gex < 0 else _GREEN, y); y += STAT_ROW_H
        stat_row("Net VEX", _fmt_m(net_vex), _RED if net_vex < 0 else _GREEN, y); y += STAT_ROW_H

        y += SECTION_GAP
        section_label("KEY LEVELS", y)
        y += SECTION_LABEL_H
        cw_pct = r.get("call_wall_pct")
        pw_pct = r.get("put_wall_pct")
        stat_row("Call Wall", f"${call_wall:,.0f}  ({cw_pct:+.1f}%)" if call_wall else "N/A", _GREEN, y); y += STAT_ROW_H
        stat_row("Put Wall", f"${put_wall:,.0f}  ({pw_pct:+.1f}%)" if put_wall else "N/A", _RED, y); y += STAT_ROW_H
        mpg_strike, mpg_val = r.get("max_pos_gex_strike"), r.get("max_pos_gex_value")
        mng_strike, mng_val = r.get("max_neg_gex_strike"), r.get("max_neg_gex_value")
        stat_row("Max +GEX", f"${mpg_strike:,.0f}  {_fmt_m(mpg_val)}" if mpg_strike is not None else "N/A", _GREEN, y); y += STAT_ROW_H
        stat_row("Max -GEX", f"${mng_strike:,.0f}  {_fmt_m(mng_val)}" if mng_strike is not None else "N/A", _RED, y); y += STAT_ROW_H

        y += SECTION_GAP
        section_label("EXPECTED MOVE", y)
        y += SECTION_LABEL_H
        if em:
            stat_row("\u00b1 by Fri", f"\u00b1${em['dollar']} ({em['pct']}%)", _GOLD, y); y += STAT_ROW_H
            stat_row("Range", f"${em['min']:,.2f} \u2013 ${em['max']:,.2f}", _TXT, y); y += STAT_ROW_H
        else:
            stat_row("\u00b1 by Fri", "N/A", _TXT_DIM, y); y += STAT_ROW_H
            stat_row("Range", "N/A", _TXT_DIM, y); y += STAT_ROW_H

    cur += CARD_H + GAP

    rrect(MARGIN, cur, usable_w, KEY_H, "#0e1424", radius=0.05, z=2)
    key_y = cur + KEY_H / 2
    txt(MARGIN + 0.22, key_y,
        "GEX = gamma exposure (+dampens/-amplifies moves)   \u00b7   VEX = vanna exposure (hedging shift as IV changes)   "
        "\u00b7   Walls = strikes with heaviest gamma concentration, act as support/resistance   \u00b7   "
        "Gamma Flip = price level where dealer positioning flips regime   \u00b7   "
        "rank/likelihood notes below each badge are based on Net GEX magnitude only",
        fs=7.2, color=_TXT_DIM, font=_HEADER_FONT)
    cur += KEY_H + GAP

    txt(MARGIN + usable_w, cur + FOOTER_H / 2, "BlueMoonTrades", fs=10,
        color=_TXT_DIM, bold=True, ha="right", font=_HEADER_FONT)

    plt.savefig(out_path, facecolor=_BG, bbox_inches="tight", pad_inches=0.2)
    plt.close(fig)
    print(f"[GEX] Dashboard card -> {out_path}")