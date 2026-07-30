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

# Contracts with IV reported below this are almost always a stale or
# corrupted quote, not a real market price — common on thin/illiquid
# far-from-spot strikes. Gamma = 1/(spot * iv * sqrt(T)) blows up toward
# infinity as iv -> 0, so a single bad low-IV print can produce one
# wildly oversized gamma value that dominates the entire aggregate and
# creates a spurious gamma-flip crossing OR a fake wall/max-GEX pick at
# a strike with no real market significance. Contracts below this
# threshold are treated as unusable (contribute 0 gamma/vanna) — same
# treatment as zero open interest.
#
# RAISED (2026-07-29): the original 0.02 (2%) floor was too permissive.
# Confirmed in production and reproduced in testing: a strike with
# IV=0.021 (2.1%, just above the old floor) against a realistic ~17%
# ATM IV elsewhere in the same chain still inflated gamma enough to get
# selected as a fake put wall 48% away from spot. This threshold zeroes
# the contribution at the SOURCE (before any summing), so it protects
# net_gex_total/net_vex_total directly, not just wall/flip selection —
# the moneyness-band restriction (_restrict_to_band) is a second,
# independent layer of defense for the selection step specifically.
# 0.05 is a heuristic, not a precisely derived number — revisit if real
# runs still show implausible walls/totals.
MIN_RELIABLE_IV = 0.05


# ── Black-Scholes Greeks (gamma + vanna) ────────────────────────────────────
def _norm_pdf(x: float) -> float:
    return (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x)


def bs_gamma(spot: float, strike: float, t_years: float, iv: float,
             r: float = RISK_FREE_RATE) -> float:
    """Black-Scholes gamma — identical formula for calls and puts
    (put-call gamma parity for European options with the same
    strike/expiry/vol)."""
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    return _norm_pdf(d1) / (spot * iv * math.sqrt(t_years))


def bs_vanna(spot: float, strike: float, t_years: float, iv: float,
             r: float = RISK_FREE_RATE) -> float:
    """Black-Scholes vanna: d(delta)/d(IV), closed form.
    vanna = -phi(d1) * d2 / iv"""
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    return -_norm_pdf(d1) * d2 / iv


# ── Alpaca connection (replaces yfinance — 2026-07-29) ──────────────────────
# CONFIRMED LIVE (2026-07-29) against a real paper account:
#   - /v2/options/contracts (bulk listing) DOES include open_interest per
#     contract directly — no separate per-contract call needed, contrary
#     to earlier (incorrect) research that suggested otherwise.
#   - /v1beta1/options/snapshots/{ticker} returns ONLY bid/ask/last/OHLC —
#     NO Greeks, NO implied volatility, despite earlier assumptions.
#     IV is solved here via Black-Scholes inversion from the bid/ask mid
#     price (see solve_implied_vol below) rather than trusted from a
#     vendor-provided field.
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
    """Solves for the IV that reproduces observed_price under Black-
    Scholes, via bisection. Bisection is used deliberately over Newton-
    Raphson: option price is monotonically increasing in sigma, so
    bisection always converges reliably without needing a derivative,
    and can't diverge the way Newton-Raphson occasionally can on thin
    quotes.

    BUGFIX (2026-07-29): an earlier version short-circuited to 0.0 if
    observed_price was at/below the naive intrinsic value
    (max(S-K,0) or max(K-S,0)). That's WRONG for puts specifically —
    the correct theoretical floor for a European put is the discounted
    K*e^(-rT) - S, which is LOWER than the naive K-S when r>0. Confirmed
    in testing: a real, valid low-IV put price (0.10 vol) legitimately
    priced below the naive intrinsic but above the correct discounted
    floor was getting rejected and returned as 0.0 instead of the
    correct ~0.10. Fixed by removing the shortcut entirely — bisection
    naturally converges toward the lower search bound (near-zero IV,
    correctly reflecting minimal time value) for prices near the true
    floor, without needing a hand-rolled approximation that can be
    wrong for one side of the chain."""
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
    """Alpaca latest-quote spot price — mid of bid/ask."""
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
    """Paginated fetch of every option contract for ticker+expiry.
    Confirmed live: open_interest is included directly per contract in
    this bulk response, sometimes null (no OI ever recorded for that
    contract — treated as 0 downstream, same handling as before)."""
    all_contracts = []
    page_token = None
    for _ in range(20):  # hard cap — a single expiry's chain never needs this many pages
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
    """Paginated fetch of bid/ask for every contract at ticker+expiry.
    Returns {contract_symbol: {"bid": x, "ask": y}}. Confirmed live:
    NO Greeks, NO implied volatility in this response — IV is solved
    separately via solve_implied_vol() from the bid/ask mid price."""
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
    """Restricts strikes to within band_pct of spot — used to keep wall/
    max-GEX/gamma-flip selection from picking a deep, thin, effectively-
    noise strike just because it happens to be the mathematical argmax/
    argmin. Falls back to the unrestricted dict if spot is missing or
    the band would empty everything out (shouldn't happen on a real
    chain, but never silently return nothing usable)."""
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
    """Returns the expiry (YYYY-MM-DD) for the LAST TRADING DAY of the
    current calendar week (Mon-Fri) — regardless of which day this runs
    on. Run any day Mon-Jul-27 through Fri-Jul-31 of the same week and
    this always resolves to Jul 31 (assuming it's a real listed expiry).

    CHANGED (2026-07-29, Alpaca migration): previously took a pre-fetched
    list of available expirations (from yfinance's stock.options). Alpaca
    has no equivalent single "list all expirations" call that's cheap to
    make, so this now walks backward from the calendar Friday and makes
    a live contracts-list probe at each candidate date, stopping at the
    first one that actually has contracts — same "verify against real
    data, don't assume Friday is always a trading day" principle as
    before, just checked live instead of against a pre-fetched list.
    Each probe uses limit=1 to keep it cheap."""
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



# ── Core GEX/VEX computation ────────────────────────────────────────────────
def compute_gex_vex(ticker: str, expiries: list = None) -> dict:
    """Computes GEX/VEX for a ticker across one or more expirations,
    sourced from Alpaca (Trading API for contracts/OI, Market Data API
    for bid/ask, IV solved locally — see solve_implied_vol).

    expiries: list of "YYYY-MM-DD" strings to include. If None, uses
    the week-ending expiry (see get_week_ending_expiry). Pass multiple
    expirations for a more robust gamma-flip read if a single expiry
    keeps returning None.
    """
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

        per_strike = {}  # strike -> accumulated dict across all target expiries
        total_oi_seen = 0.0  # sanity check — see the guard right after this loop
        em_calls, em_puts = {}, {}  # strike -> {bid, ask}, collected from the FIRST expiry only
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

                # Guard against a barely-above-floor bad quote still
                # inflating gamma via the 1/(spot*iv*sqrt(T)) term.
                if 0 < iv < MIN_RELIABLE_IV:
                    iv = 0.0

                gamma = bs_gamma(spot, strike, t_years, iv) if iv > 0 else 0.0
                vanna = bs_vanna(spot, strike, t_years, iv) if iv > 0 else 0.0

                # Dollar gamma exposure per 1% underlying move, 100x
                # contract multiplier, spot^2 scaling — standard
                # convention (see module docstring re: dealer sign
                # assumption).
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

        # Data-sufficiency guard — confirmed necessary with yfinance and
        # kept here as the same defense-in-depth: a run returning
        # ~$0.00B net GEX across every ticker simultaneously is the
        # fingerprint of missing/incomplete open-interest data, not a
        # real market state. SPY/QQQ/IWM should always have tens of
        # thousands of contracts of combined OI at minimum.
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

        # Restrict wall/max-GEX/flip SELECTION to a moneyness band around
        # spot — the total above still sums the full chain correctly.
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
    """Walks strikes ascending, tracks cumulative net GEX, and linearly
    interpolates the level where the running total crosses zero. Returns
    None (not a guess) if it never crosses within the given strikes —
    an honest 'no flip in this window' rather than a fabricated number.

    BUGFIX (2026-07-29, round 1): the original version initialized
    cum=0 before the loop and used non-strict comparisons
    (prev_cum >= 0 > cum), which treated that initial zero — or any
    strike that legitimately contributes exactly zero (e.g. a strike
    filtered out by MIN_RELIABLE_IV) — as if it were a valid positive
    reference point. Fixed by using strict sign comparison
    (prev_sign != 0 and cur_sign != 0 and prev_sign != cur_sign).

    BUGFIX (2026-07-29, round 2): confirmed in production — QQQ returned
    a "gamma flip" of $389.13 against a $661.73 spot (41% away), caused
    by tiny near-zero net GEX values crossing zero by pure noise far
    from the real signal. Fixed (at the time) by restricting the search
    to strikes within band_pct of spot.

    BUGFIX (2026-07-29, round 3 — the real fix): the distance-based band
    from round 2 was the wrong tool. Confirmed in production on BOTH
    QQQ ($516.55 vs $660.91 spot, 21.8% away) and IWM ($254.41 vs $287.52
    spot, 11.5% away) — both comfortably INSIDE the 30% band, both still
    noise crossings. In each case the crossing strikes contributed tens
    of thousands of dollars while the real signal 500+ points further up
    the chain moved hundreds of millions per strike and never crossed
    back. Distance from spot was never the actual problem — MAGNITUDE
    was. Fixed by only treating a strike as a valid crossing reference
    point if its own |net_gex| is at least min_materiality_pct (default
    0.5%) of the largest single-strike magnitude in the set. Immaterial
    strikes still have their value folded into the running cumulative
    total (it's real money, just not large enough on its own to anchor a
    flip decision) — they're just skipped as comparison points, so the
    algorithm effectively looks through noise to compare only
    consecutive economically-significant strikes. Verified against both
    real datasets above: both now correctly return None. The distance
    band is kept as a secondary, redundant guard — doesn't hurt, and a
    genuine near-term flip should be close to spot anyway."""
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
            continue  # real money, but too small to anchor a crossing decision
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
    """Alpaca version — takes plain {strike: {'bid':, 'ask':}} dicts
    (as collected inline during compute_gex_vex's main fetch loop)
    instead of yfinance DataFrames."""
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


# ── Discord card formatting (matches the screenshot's data points) ─────────
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
    """Diagnostic utility — prints every strike's net GEX and the running
    cumulative total, so an implausible gamma-flip result (or a
    persistent None) can be visually traced back to the actual
    strike-level data driving it, rather than trusted or distrusted
    blindly. Flags any single strike whose |net_gex| is disproportionate
    relative to the total — the most likely fingerprint of a bad IV
    quote dominating the aggregate."""
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
    """Convenience entry point for nightly setups — computes and formats
    GEX/VEX cards for each ticker, ready to drop into a Discord post."""
    blocks = []
    for t in tickers:
        result = compute_gex_vex(t, expiries=expiries)
        blocks.append(format_gex_card(result))
    return "\n\n".join(blocks)


# ── Visual dashboard card (matches the trade journal / weekly insights'
# navy/gold "Light Bold" theme for a consistent look across all BMT
# Discord content) ──────────────────────────────────────────────────────
_METRIC_DEFS = [
    # (key, label, plain-English definition)
    ("gamma_flip", "GAMMA FLIP",
     "Price level where dealers flip from short to long gamma. Above it, moves "
     "tend to calm down; below it, moves tend to accelerate."),
    ("net_gex", "NET GEX",
     "Total dollar gamma exposure across all strikes. Negative = dealers are "
     "short gamma and must chase price, which amplifies moves. Positive = "
     "dealers are long gamma and dampen moves."),
    ("net_vex", "NET VEX",
     "Total vanna exposure — how much dealer hedging shifts as implied "
     "volatility itself changes."),
    ("call_wall", "CALL WALL",
     "Strike with the largest call-side gamma concentration — often acts as "
     "resistance or a magnet for price."),
    ("put_wall", "PUT WALL",
     "Strike with the largest put-side gamma concentration — often acts as "
     "support or a magnet for price."),
    ("max_pos_gex", "MAX +GEX",
     "The single strike with the largest positive net gamma exposure."),
    ("max_neg_gex", "MAX -GEX",
     "The single strike with the largest negative net gamma exposure — often "
     "where downside selling pressure is most amplified."),
    ("expected_move", "EXPECTED MOVE",
     "Market-implied +/- move by expiration, derived from the at-the-money "
     "straddle price."),
]

_GEX_BG        = "#0a0e1a"   # deep near-black navy
_GEX_HDR_BG    = "#151c31"   # header/card navy, slightly lighter than bg for depth
_GEX_HDR_TXT   = "#f8fafc"
_GEX_SEC_BG    = "#151c31"
_GEX_SEC_TXT   = "#7dd3fc"   # bright sky blue
_GEX_ROW_E     = "#151c31"
_GEX_ROW_O     = "#0e1424"
_GEX_TXT       = "#e2e8f0"
_GEX_TXT_DIM   = "#8b98b8"
_GEX_GREEN     = "#34d399"   # rich emerald — reads well on dark
_GEX_RED       = "#fb7185"   # rich rose — softer than pure red on dark, still unambiguous
_GEX_BORDER    = "#2d3b56"
_GEX_GOLD      = "#fbbf24"   # vibrant amber
_GEX_DEF_BG    = "#0e1424"
_GEX_CARD_BG   = "#141b2e"
_GEX_TRACK_BG  = "#1e293b"   # range-bar track
_GEX_DATA_FONT   = "Liberation Mono"
_GEX_HEADER_FONT = "Liberation Sans"


def _fallback_watch_line(r: dict) -> str:
    """Data-derived watch line — used when Grok isn't available, and as
    the base Grok is told to build on. REDESIGNED (2026-07-30): the old
    fallback just restated the raw wall levels verbatim ("range likely
    holds between $X put and $Y call walls"), which is identical
    information to what's already sitting in the Key Levels field right
    above it — confirmed feedback that this read as redundant, not
    additive. Fixed by computing something the raw fields don't show on
    their own: whether this week's expected move is large enough to
    plausibly REACH a wall, by comparing expected-move % against each
    wall's % distance from spot — two numbers that are shown separately
    elsewhere but never compared against each other."""
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
    """Returns one of 'both', 'call_only', 'put_only', 'neither',
    or 'unknown' — the same comparison _fallback_watch_line uses,
    factored out so the SAME classification can decide (a) whether to
    write one consolidated line or one per ticker, in both the
    deterministic fallback and the Grok prompt."""
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
    """ONE shared line covering every ticker, used when they all land
    in the same _classify_wall_reach bucket this week — avoids saying
    the same underlying pattern three separate times when it's
    genuinely the same pattern each time. Confirmed necessary in
    production: SPY/QQQ/IWM all landed in 'put_only' on 2026-07-30, and
    three near-identical per-ticker lines read as repetitive rather than
    thorough."""
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
    """Adaptive deterministic fallback: if every ticker shares the same
    _classify_wall_reach pattern this week, returns ONE shared
    consolidated line (same value for every ticker key — build_gex_embed
    detects this and renders it once, not three times). If patterns
    DIVERGE across tickers, returns one distinct line per ticker via
    _fallback_watch_line instead, since each then genuinely needs its
    own callout. This is the actual fallback used by
    generate_gex_watch_lines, and also drives which prompt shape gets
    sent to Grok (see below) — same classification decides both paths."""
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
    """REPLACED (2026-07-29): the old generate_gex_summary() asked Grok
    for a 3-4 sentence flowing paragraph, posted as a single wall of
    text. Confirmed feedback: too much text, no clear scannable action.

    REDESIGNED (2026-07-30, round 1): confirmed feedback that the output
    just restated wall levels already visible in the Key Levels field,
    adding nothing. Fixed the fallback (see _fallback_watch_line) to
    compute a genuinely new comparison instead of echoing raw numbers.

    REDESIGNED (2026-07-30, round 2): confirmed in production — output
    was STILL three near-identical lines, and it turned out this was
    the fallback text verbatim even though OPENROUTER_API_KEY was
    confirmed set. Root cause: the old parser silently found zero lines
    matching its strict "$TICKER | clause" format and returned pure
    fallback with NO warning printed — the exact same failure mode as
    the no-API-key case, but invisible. Fixed two ways: (1) added a
    diagnostic print of Grok's raw response whenever zero lines parse,
    so a format mismatch is visible instead of silently masked by the
    fallback; (2) the parser is now more lenient — accepts "TICKER |",
    "TICKER:", or "**TICKER**:" prefixes instead of requiring a literal
    leading '$'. Also added build_watch_lines_fallback's adaptive
    single-line-vs-per-ticker logic here: the SAME pattern
    classification that decides the fallback's shape now tells Grok
    which shape to write too, rather than leaving that judgment to the
    model (which is exactly the kind of instruction that was silently
    going unfollowed)."""
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
        # Expect one plain sentence — take the response as-is (stripped
        # of any stray quotes/markdown Grok might add), applied to every
        # ticker so build_gex_embed renders it once, not three times.
        cleaned = raw.strip().strip('"').lstrip("*").rstrip("*").strip()
        if cleaned:
            return {r["ticker"]: cleaned for r in valid}
        print(f"[GEX WARN] generate_gex_watch_lines: couldn't extract a usable consolidated line. "
              f"Raw Grok response was:\n{raw!r}\nUsing fallback instead.")
        return fallback

    # Per-ticker mode — lenient parsing: accepts "TICKER | clause",
    # "TICKER: clause", or "**TICKER**: clause", with or without a
    # leading '$'.
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
    """Builds a single Discord embed — compact fields, colored side bar —
    matching the visual convention already used in brief_8am.py/
    brief_eod.py (add_field-style short lines) instead of a prose
    paragraph. Meant to post alongside the rendered dashboard card, not
    replace it: this is the scannable summary, the image is the detail.

    UPDATED (2026-07-30): watch_lines may now be a CONSOLIDATED dict
    (every ticker mapped to the SAME line — see
    build_watch_lines_fallback / generate_gex_watch_lines) when every
    ticker shares the same wall-reach pattern this week. Detected here
    by checking whether all values are identical, and rendered as one
    unprefixed line instead of the same sentence repeated three times
    with a different ticker bolded in front of it each time."""
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
        regime_lines.append(f"{dot} **{r['ticker']}** \u2014 {regime_word}")
        levels_lines.append(
            f"**{r['ticker']}**  Put ${r['put_wall']:,.0f} \u00b7 Call ${r['call_wall']:,.0f}"
        )
        em = r.get("expected_move") or {}
        if em:
            move_lines.append(f"**{r['ticker']}**  \u00b1${em['dollar']} ({em['pct']}%)")

    for r in errored:
        regime_lines.append(f"\u26A0\uFE0F **{r.get('ticker', '?')}** \u2014 skipped (data issue)")

    watch_values = [watch_lines.get(r["ticker"], "") for r in valid]
    watch_values = [v for v in watch_values if v]
    if watch_values and len(set(watch_values)) == 1:
        # Every ticker shares the same line -- show it once, unprefixed.
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


def _metric_display(result: dict, key: str):
    """Returns (value_str, color) for a given metric key, or (None, None)
    if the underlying data is missing — callers skip rendering that row
    entirely rather than showing a blank."""
    if "error" in result:
        return None, None

    if key == "gamma_flip":
        v = result.get("gamma_flip")
        if v is None:
            return "N/A", _GEX_TXT_DIM
        return f"${v:,.2f}", _GEX_TXT

    if key == "net_gex":
        v = result.get("net_gex")
        return _fmt_dollars_b(v), (_GEX_RED if v < 0 else _GEX_GREEN)

    if key == "net_vex":
        v = result.get("net_vex")
        return _fmt_dollars_m(v), (_GEX_RED if v < 0 else _GEX_GREEN)

    if key == "call_wall":
        v, pct = result.get("call_wall"), result.get("call_wall_pct")
        if v is None:
            return "N/A", _GEX_TXT_DIM
        return f"${v:,.0f}  ({pct:+.1f}%)", _GEX_GREEN

    if key == "put_wall":
        v, pct = result.get("put_wall"), result.get("put_wall_pct")
        if v is None:
            return "N/A", _GEX_TXT_DIM
        return f"${v:,.0f}  ({pct:+.1f}%)", _GEX_RED

    if key == "max_pos_gex":
        v, val = result.get("max_pos_gex_strike"), result.get("max_pos_gex_value")
        if v is None:
            return "N/A", _GEX_TXT_DIM
        return f"${v:,.0f}   {_fmt_dollars_m(val)}", _GEX_GREEN

    if key == "max_neg_gex":
        v, val = result.get("max_neg_gex_strike"), result.get("max_neg_gex_value")
        if v is None:
            return "N/A", _GEX_TXT_DIM
        return f"${v:,.0f}   {_fmt_dollars_m(val)}", _GEX_RED

    if key == "expected_move":
        em = result.get("expected_move")
        if not em:
            return "N/A", _GEX_TXT_DIM
        return (f"\u00b1${em['dollar']} ({em['pct']}%)  "
                f"Max ${em['max']} / Min ${em['min']}", _GEX_TXT)

    return None, None


def render_gex_dashboard_card(results: list, week_label: str, out_path: str):
    """Renders the GEX/VEX dashboard as a Discord-ready PNG.

    REDESIGNED AGAIN (2026-07-29, v3): the compact table (v2) fixed the
    repeated-definitions problem but was still just a spreadsheet — no
    visual hierarchy, nothing to look at beyond numbers in a grid. This
    version adds a HERO ROW above the table: one card per ticker with a
    bold gamma-regime badge (SHORT/LONG GAMMA) and an actual visual range
    bar showing where spot sits between the put wall and call wall, with
    the expected-move band shaded on top — an at-a-glance read of "where
    is this ticker relative to its walls" that a table of numbers can't
    give you. Hero cards reuse the drop-shadow card style from the
    weekly insights KPI dashboard for visual consistency. The detailed
    metric x ticker table and shared legend from v2 are kept below for
    full data completeness. No emoji inside the rendered image —
    matplotlib's bundled fonts don't render emoji glyphs (same issue
    already fixed in the trade journal's title bar); bold typography,
    color, and the range bar carry the visual interest instead."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as patches
    import re as _re

    def S(text):
        return _re.sub(r"[^\x00-\uFFFF]", "", str(text))

    FIG_W = 19.0
    MARGIN = 0.34
    HDR_H = 0.60
    SUB_H = 0.28
    GAP = 0.28

    HERO_H = 2.38
    BAR_H = 0.34
    HERO_SHADOW = 0.035

    TICK_HDR_H = 0.56
    ROW_H = 0.44
    LEGEND_HDR_H = 0.36
    LEGEND_ROW_H = 0.58
    FOOTER_H = 0.36
    LEGEND_COLS = 2

    n_tickers = len(results)
    usable_w = FIG_W - 2 * MARGIN

    def rect(x, y, w, h, color, z=2, alpha=1.0):
        ax.add_patch(patches.Rectangle((x, y), w, h, facecolor=color, edgecolor="none", zorder=z, alpha=alpha))

    def rrect(x, y, w, h, color, radius=0.06, z=2, alpha=1.0, ec="none", lw=0):
        ax.add_patch(patches.FancyBboxPatch(
            (x, y), w, h, boxstyle=f"round,pad=0,rounding_size={radius}",
            facecolor=color, edgecolor=ec, linewidth=lw, zorder=z, alpha=alpha))

    def hline(y, x0, x1, color=_GEX_BORDER, lw=0.5):
        ax.plot([x0, x1], [y, y], color=color, linewidth=lw, zorder=3)

    def vline(x, y0, y1, color=_GEX_BORDER, lw=0.4):
        ax.plot([x, x], [y0, y1], color=color, linewidth=lw, zorder=3)

    def txt(x, y, text, fs=8.0, color=_GEX_TXT, bold=False, ha="left",
            font=_GEX_DATA_FONT, style="normal"):
        ax.text(x, y, S(text), fontsize=fs, color=color,
                 fontweight="bold" if bold else "normal", fontstyle=style,
                 ha=ha, va="center", zorder=6, fontfamily=font)

    # ── Figure height pre-calc ───────────────────────────────────────────
    label_col_w = usable_w * 0.22
    ticker_col_w = (usable_w - label_col_w) / n_tickers if n_tickers else usable_w
    n_metrics = len(_METRIC_DEFS)
    table_h = TICK_HDR_H + n_metrics * ROW_H
    legend_rows = -(-len(_METRIC_DEFS) // LEGEND_COLS)
    legend_h = LEGEND_HDR_H + legend_rows * LEGEND_ROW_H
    BAR_LEGEND_H = 0.30

    fig_h = (MARGIN + HDR_H + SUB_H + BAR_LEGEND_H + GAP + HERO_H + GAP
             + table_h + GAP + legend_h + GAP + FOOTER_H + MARGIN)

    fig = plt.figure(figsize=(FIG_W, fig_h), dpi=200, facecolor=_GEX_BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, FIG_W)
    ax.set_ylim(fig_h, 0)
    ax.axis("off")

    cur = MARGIN

    # ── Title bar ────────────────────────────────────────────────────────
    rect(MARGIN, cur, usable_w, HDR_H, _GEX_HDR_BG)
    hline(cur + HDR_H, MARGIN, MARGIN + usable_w, _GEX_GOLD, 1.4)
    txt(MARGIN + 0.22, cur + HDR_H / 2, f"GEX / VEX DASHBOARD  \u2014  {week_label}",
        fs=13.5, color=_GEX_HDR_TXT, bold=True, font=_GEX_HEADER_FONT)
    cur += HDR_H

    txt(FIG_W / 2, cur + SUB_H / 2,
        "Standard public-GEX approximation \u2014 dealers assumed net long calls / net short puts",
        fs=7.5, color=_GEX_TXT_DIM, ha="center", font=_GEX_HEADER_FONT)
    cur += SUB_H

    # ── "How to read this" bar legend — explains the range-bar visual
    # ONCE, shared, before the hero cards that use it three times ────────
    LEGEND_BAR_H = BAR_LEGEND_H
    rrect(MARGIN, cur, usable_w, LEGEND_BAR_H, _GEX_DEF_BG, radius=0.05, z=2)
    lg_y = cur + LEGEND_BAR_H / 2
    lg_x = MARGIN + 0.25
    ax.plot([lg_x], [lg_y], marker="o", markersize=7, markerfacecolor=_GEX_GOLD,
            markeredgecolor="#ffffff", markeredgewidth=1.0, zorder=6)
    txt(lg_x + 0.16, lg_y, "= current price", fs=6.8, color=_GEX_TXT_DIM, font=_GEX_HEADER_FONT)
    lg_x += 2.0
    rect(lg_x, lg_y - 0.06, 0.22, 0.12, _GEX_GOLD, z=4, alpha=0.5)
    txt(lg_x + 0.30, lg_y, "= expected move range", fs=6.8, color=_GEX_TXT_DIM, font=_GEX_HEADER_FONT)
    lg_x += 2.55
    ax.plot([lg_x, lg_x], [lg_y - 0.08, lg_y + 0.08], color=_GEX_RED, linewidth=2.2, zorder=6)
    txt(lg_x + 0.10, lg_y, "= put wall (support)", fs=6.8, color=_GEX_TXT_DIM, font=_GEX_HEADER_FONT)
    lg_x += 2.2
    ax.plot([lg_x, lg_x], [lg_y - 0.08, lg_y + 0.08], color=_GEX_GREEN, linewidth=2.2, zorder=6)
    txt(lg_x + 0.10, lg_y, "= call wall (resistance)", fs=6.8, color=_GEX_TXT_DIM, font=_GEX_HEADER_FONT)
    cur += LEGEND_BAR_H + GAP

    # ── Hero row: one range-bar card per ticker ─────────────────────────
    hero_gap = 0.26
    hero_w = (usable_w - hero_gap * (n_tickers - 1)) / n_tickers if n_tickers else usable_w

    for i, result in enumerate(results):
        hx = MARGIN + i * (hero_w + hero_gap)

        if "error" in result:
            rrect(hx, cur, hero_w, HERO_H, _GEX_CARD_BG, radius=0.06, ec=_GEX_BORDER, lw=0.8)
            txt(hx + hero_w / 2, cur + HERO_H / 2, f"{result.get('ticker','?')}: error",
                fs=9, color=_GEX_TXT_DIM, ha="center", font=_GEX_HEADER_FONT)
            continue

        ticker = result["ticker"]
        spot = result["spot"]
        net_gex = result["net_gex"]
        is_short = net_gex < 0
        regime_color = _GEX_RED if is_short else _GEX_GREEN
        regime_label = "SHORT GAMMA" if is_short else "LONG GAMMA"
        regime_sub = "choppier, amplified moves" if is_short else "calmer, range-bound moves"

        # Glow ring (dark-theme equivalent of a drop shadow) + card
        rrect(hx - 0.02, cur - 0.02, hero_w + 0.04, HERO_H + 0.04, regime_color,
              radius=0.07, z=1, alpha=0.14)
        rrect(hx, cur, hero_w, HERO_H, _GEX_CARD_BG, radius=0.06, ec=_GEX_BORDER, lw=0.9, z=2)
        # Top accent strip in the regime color
        rrect(hx, cur, hero_w, 0.09, regime_color, radius=0.04, z=3)

        inner_y = cur + 0.30
        txt(hx + hero_w / 2, inner_y, f"{ticker}", fs=15, color=_GEX_TXT, bold=True,
            ha="center", font=_GEX_HEADER_FONT)
        txt(hx + hero_w / 2, inner_y + 0.26, f"${spot:,.2f}", fs=10, color=_GEX_TXT_DIM,
            ha="center", font=_GEX_DATA_FONT)

        # Regime badge (pill)
        badge_y = inner_y + 0.58
        badge_w = hero_w * 0.72
        badge_h = 0.30
        rrect(hx + (hero_w - badge_w) / 2, badge_y, badge_w, badge_h, regime_color,
              radius=badge_h * 0.45, z=3)
        txt(hx + hero_w / 2, badge_y + badge_h / 2, regime_label, fs=8.5, color=_GEX_BG,
            bold=True, ha="center", font=_GEX_HEADER_FONT)
        txt(hx + hero_w / 2, badge_y + badge_h + 0.15, regime_sub, fs=6.6,
            color=_GEX_TXT_DIM, ha="center", style="italic", font=_GEX_HEADER_FONT)

        # ── Range bar: put wall <--- spot ---> call wall, expected-move
        # band shaded, gamma flip marked if present ──────────────────────
        bar_y = badge_y + badge_h + 0.42
        bar_x0 = hx + 0.30
        bar_w = hero_w - 0.60

        call_wall = result["call_wall"]
        put_wall = result["put_wall"]
        em = result.get("expected_move") or {}
        em_min = em.get("min", spot)
        em_max = em.get("max", spot)
        gamma_flip = result.get("gamma_flip")

        range_min = min(put_wall, em_min, spot) if put_wall else min(em_min, spot)
        range_max = max(call_wall, em_max, spot) if call_wall else max(em_max, spot)
        pad = (range_max - range_min) * 0.12 or 1.0
        range_min -= pad
        range_max += pad
        span = range_max - range_min

        def to_x(price):
            return bar_x0 + (price - range_min) / span * bar_w

        # base track
        rrect(bar_x0, bar_y, bar_w, BAR_H, _GEX_TRACK_BG, radius=BAR_H * 0.4, z=3)
        # expected-move band (translucent gold — brighter alpha reads
        # better against the dark track than the light-theme version did)
        em_x0, em_x1 = to_x(em_min), to_x(em_max)
        rect(em_x0, bar_y, max(em_x1 - em_x0, 0.02), BAR_H, _GEX_GOLD, z=4, alpha=0.45)

        # put wall marker
        if put_wall:
            pw_x = to_x(put_wall)
            ax.plot([pw_x, pw_x], [bar_y - 0.03, bar_y + BAR_H + 0.03],
                    color=_GEX_RED, linewidth=2.4, zorder=5)
            txt(pw_x, bar_y + BAR_H + 0.20, f"${put_wall:,.0f}", fs=6.8,
                color=_GEX_RED, bold=True, ha="center")
            txt(pw_x, bar_y - 0.16, "PUT", fs=5.8, color=_GEX_RED, ha="center", font=_GEX_HEADER_FONT)

        # call wall marker
        if call_wall:
            cw_x = to_x(call_wall)
            ax.plot([cw_x, cw_x], [bar_y - 0.03, bar_y + BAR_H + 0.03],
                    color=_GEX_GREEN, linewidth=2.4, zorder=5)
            txt(cw_x, bar_y + BAR_H + 0.20, f"${call_wall:,.0f}", fs=6.8,
                color=_GEX_GREEN, bold=True, ha="center")
            txt(cw_x, bar_y - 0.16, "CALL", fs=5.8, color=_GEX_GREEN, ha="center", font=_GEX_HEADER_FONT)

        # gamma flip marker (dashed, only if within range)
        if gamma_flip is not None and range_min <= gamma_flip <= range_max:
            gf_x = to_x(gamma_flip)
            ax.plot([gf_x, gf_x], [bar_y - 0.08, bar_y + BAR_H + 0.08],
                    color="#c084fc", linewidth=1.5, linestyle="--", zorder=5)

        # spot marker — the star of the bar, drawn last so it's on top.
        # Gold fill with a bright white ring pops clearly against the
        # dark track, unlike a navy-on-navy marker would.
        sp_x = to_x(spot)
        ax.plot([sp_x], [bar_y + BAR_H / 2], marker="o", markersize=10,
                markerfacecolor=_GEX_GOLD, markeredgecolor="#ffffff",
                markeredgewidth=1.6, zorder=7)

    cur += HERO_H + GAP

    # ── Detail table: rows = metrics, columns = tickers ──────────────────
    table_x0 = MARGIN
    col_edges = [table_x0, table_x0 + label_col_w]
    for _ in range(n_tickers):
        col_edges.append(col_edges[-1] + ticker_col_w)

    rect(table_x0, cur, usable_w, TICK_HDR_H, _GEX_SEC_BG)
    hline(cur + TICK_HDR_H, table_x0, table_x0 + usable_w, _GEX_GOLD, 0.8)
    txt(table_x0 + 0.12, cur + TICK_HDR_H / 2, "METRIC", fs=8.2,
        color=_GEX_SEC_TXT, bold=True, font=_GEX_HEADER_FONT)
    for i, result in enumerate(results):
        cx = col_edges[i + 1]
        ticker = result.get("ticker", "?")
        spot = result.get("spot")
        exp_list = result.get("expiries", [])
        exp_str = exp_list[0] if exp_list else "N/A"
        header_label = f"{ticker}  ${spot:,.2f}" if spot else f"{ticker} (error)"
        txt(cx + ticker_col_w / 2, cur + TICK_HDR_H / 2 - 0.10, header_label,
            fs=9.5, color=_GEX_HDR_TXT, bold=True, ha="center", font=_GEX_HEADER_FONT)
        txt(cx + ticker_col_w / 2, cur + TICK_HDR_H / 2 + 0.13, f"exp {exp_str}",
            fs=6.6, color=_GEX_SEC_TXT, ha="center", font=_GEX_HEADER_FONT)
        vline(cx, cur, cur + TICK_HDR_H, _GEX_BORDER, 0.4)
    cur += TICK_HDR_H

    for ri, (key, label, _definition) in enumerate(_METRIC_DEFS):
        bg = _GEX_ROW_E if ri % 2 == 0 else _GEX_ROW_O
        rect(table_x0, cur, usable_w, ROW_H, bg)
        hline(cur + ROW_H, table_x0, table_x0 + usable_w, _GEX_BORDER, 0.3)
        txt(table_x0 + 0.12, cur + ROW_H / 2, label, fs=7.8, color=_GEX_TXT_DIM,
            bold=True, font=_GEX_HEADER_FONT)
        for i, result in enumerate(results):
            cx = col_edges[i + 1]
            value_str, color = _metric_display(result, key)
            if value_str is None:
                value_str, color = "N/A", _GEX_TXT_DIM
            txt(cx + ticker_col_w - 0.10, cur + ROW_H / 2, value_str, fs=7.6,
                color=color, bold=True, ha="right")
            vline(cx, cur, cur + ROW_H, _GEX_BORDER, 0.25)
        cur += ROW_H

    ax.plot([table_x0, table_x0 + usable_w], [cur, cur], color=_GEX_BORDER, linewidth=0.6, zorder=4)
    cur += GAP

    # ── Legend — shown ONCE, shared across every ticker ─────────────────
    rect(table_x0, cur, usable_w, LEGEND_HDR_H, _GEX_SEC_BG)
    hline(cur + LEGEND_HDR_H, table_x0, table_x0 + usable_w, _GEX_GOLD, 0.7)
    txt(table_x0 + 0.12, cur + LEGEND_HDR_H / 2, "WHAT THESE MEAN", fs=8.0,
        color=_GEX_SEC_TXT, bold=True, font=_GEX_HEADER_FONT)
    cur += LEGEND_HDR_H

    legend_col_w = usable_w / LEGEND_COLS
    for idx, (key, label, definition) in enumerate(_METRIC_DEFS):
        col = idx % LEGEND_COLS
        row = idx // LEGEND_COLS
        lx = table_x0 + col * legend_col_w
        ly = cur + row * LEGEND_ROW_H
        bg = _GEX_DEF_BG if row % 2 == 0 else _GEX_BG
        rect(lx, ly, legend_col_w, LEGEND_ROW_H, bg)
        txt(lx + 0.12, ly + 0.16, label, fs=7.4, color=_GEX_TXT, bold=True, font=_GEX_HEADER_FONT)
        wrapped = _wrap_text(definition, max_chars=int(legend_col_w * 15))
        wy = ly + 0.34
        for line in wrapped[:2]:
            txt(lx + 0.12, wy, line, fs=6.4, color=_GEX_TXT_DIM, style="italic", font=_GEX_HEADER_FONT)
            wy += 0.16
    cur += legend_rows * LEGEND_ROW_H
    ax.plot([table_x0, table_x0 + usable_w], [cur, cur], color=_GEX_BORDER, linewidth=0.6, zorder=4)
    cur += GAP

    # ── Footer / signature ────────────────────────────────────────────
    txt(table_x0 + usable_w, cur + FOOTER_H / 2, "BlueMoonTrades", fs=9.5,
        color=_GEX_TXT_DIM, bold=True, ha="right", font=_GEX_HEADER_FONT)

    plt.savefig(out_path, facecolor=_GEX_BG, bbox_inches="tight", pad_inches=0.2)
    plt.close(fig)
    print(f"[GEX] Dashboard card -> {out_path}")


def _wrap_text(text: str, max_chars: int) -> list:
    """Simple word-wrap — matplotlib has no built-in wrapping for plain
    ax.text, so definitions are pre-chunked into lines here."""
    words = text.split()
    lines, current = [], ""
    for w in words:
        candidate = (current + " " + w).strip()
        if len(candidate) > max_chars and current:
            lines.append(current)
            current = w
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines