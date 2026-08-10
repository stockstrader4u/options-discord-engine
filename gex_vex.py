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

WATCH-LINE ACTIONABILITY FIX (2026-08-07): generate_gex_watch_lines()'s
prompt previously EXPLICITLY BANNED the one sentence structure that
would tell a subscriber what to actually do ("watch $X support / $Y
resistance" was a banned phrase), in favor of "insight" -- which in
practice produced dense, mechanism-describing prose ("dealer hedging
likely pins price without breakout") with no verb telling the reader
what action to take. Confirmed directly by the user: paying subscribers
found the published "What To Watch" section informative-sounding but
not actionable -- no clear "so what do I do tomorrow."

FIX: the prompt now REQUIRES each line to end with a concrete,
second-person action clause (stay defined-risk / fade a break /
trim near / size down / etc.), not just a market-structure
observation. This is a prompt-shape change only -- the underlying data
is still pulled and computed fresh every single run (spot, walls,
expected move, gamma flip, regime), nothing is hardcoded or templated;
only the INSTRUCTION given to the model changed. The full detailed
dashboard card (render_gex_dashboard_card) is completely untouched --
this only affects the shorter "What To Watch" text in the Discord
embed and its data-derived fallback.

STRIKE-DISPLAY PRECISION BUGFIX (2026-08-10): confirmed in a real
production run that NVDA's card showed "Put Wall $212 (-2.7%)" -- but
(212-218.49)/218.49 = -2.97%, not -2.7%. The dollar figure was being
displayed rounded to the nearest whole dollar (":,.0f") everywhere,
while the percentage next to it was computed from the ticker's REAL,
unrounded strike (e.g. $212.50, common on higher-priced underlyings'
weeklies) -- so the two numbers on the same line came from different
precision and didn't reconcile, for any strike that wasn't already a
whole dollar. AAPL/MSFT/GOOGL/AMZN/META/TSLA/SPY/QQQ/IWM all happened to
have whole-dollar walls this run, which is why only NVDA exposed it.
Fixed with format_strike() (new, see below) -- shows a strike with
whatever precision it actually has (whole dollars stay whole, a $212.50
strike now DISPLAYS as $212.50) instead of always truncating to zero
decimals, so the displayed dollar and the percentage next to it are
always computed from the exact same number. Applied everywhere a
call_wall/put_wall/max_pos_gex_strike/max_neg_gex_strike dollar value is
shown -- both card renderers, the embed builders, and the "What To
Watch" narrative text (format_gex_card, render_single_ticker_gex_card,
render_gex_dashboard_card, build_gex_embed, _fallback_watch_line,
_consolidated_watch_line, generate_gex_watch_lines' data lines).

SINGLE-TICKER CONSOLIDATED-LINE BUGFIX (2026-08-10): confirmed in the
same production run that GOOGL and AMZN's "What To Watch" text read
"GOOGL could ALL realistically drift lower toward THEIR nearby support
levels..." -- plural language on a card for one ticker, and
one-sided (only ever mentions the put wall, never the call wall, unlike
every other ticker's card). Root cause: the Mag 7 pipeline
(gex_vex_combined_daily.py's run_mag7_section) calls
generate_gex_watch_lines([r]) once PER TICKER, with a single-element
list. _consolidated_watch_line() -- designed for the 3-ticker SPY/QQQ/
IWM dashboard, where "could all... their... one of these" genuinely
describes several tickers sharing one pattern -- was being selected
even for n=1, because both generate_gex_watch_lines()'s same_pattern
check and build_watch_lines_fallback()'s equivalent check only tested
whether the (trivially single) classification was consistent, never
whether there was more than one ticker to begin with. This is also
exactly why the LLM-rejected line fell back to the SAME contradicting
text: build_watch_lines_fallback() was returning the consolidated
plural template as "the fallback" too. Fixed by requiring len(valid) > 1
before either function will use the consolidated path -- a single-
ticker call now always uses _fallback_watch_line()'s per-ticker,
singular-voice template (which already correctly mentions BOTH the
call wall and put wall when available) as its fallback, and the LLM
prompt path for n=1 goes through the per-ticker "TICKER | sentence"
branch instead of the shared-paragraph branch.
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
        raw = resp.json()
        quote = raw.get("quote", {})
        bid, ask = quote.get("bp"), quote.get("ap")
        # DIAGNOSTIC LOGGING (2026-08-08): added after a real production
        # run showed AAPL priced at $295.94 when Alpaca's own dashboard
        # confirmed the true price was $313.19 at the same time -- a
        # ~5.5% gap with no code-visible explanation on static review
        # (this function's bid/ask averaging logic checked out fine).
        # Logs the full raw response symbol/timestamp/bid/ask on EVERY
        # call so the next occurrence can be diagnosed from real
        # evidence -- e.g. confirming whether Alpaca returned AAPL's
        # own symbol with a wrong price (a provider-side data issue) or
        # whether the response was for a DIFFERENT symbol entirely
        # (which would point to a request/response mismatch bug
        # instead). Intentionally verbose -- this is a real, unsolved
        # discrepancy, not a routine log line.
        returned_symbol = raw.get("symbol", "?")
        quote_ts = quote.get("t", "?")
        print(f"  [SPOT DEBUG] requested={ticker} returned_symbol={returned_symbol} "
              f"bid={bid} ask={ask} quote_timestamp={quote_ts}")
        if returned_symbol != "?" and returned_symbol != ticker:
            print(f"  [SPOT WARN] {ticker}: Alpaca returned data for symbol "
                  f"'{returned_symbol}' instead of the requested '{ticker}' — "
                  f"this would fully explain a wrong price. Full raw response: {raw}")

        # ROOT-CAUSE FIX (2026-08-08): confirmed via the diagnostic
        # logging above that AAPL's real production discrepancy
        # ($295.94 shown vs. $313.19 actual) was caused by Alpaca
        # returning a real, live bid (295.94) alongside ask=0 -- the
        # OLD "if bid and ask" check treated 0 as falsy and silently
        # fell through to "return ask or bid or None", which returned
        # the lone bid as if it were a trustworthy mid-price. A bid
        # with NO matching ask is not a reliable spot price -- it's
        # half a quote, and publishing it as if it were the real price
        # is actively misleading, not just imprecise. Same underlying
        # failure mode independently confirmed on the OPTIONS quote
        # side too (see compute_gex_vex()'s has_valid_quote fix).
        #
        # FIX: an ask of exactly 0 (or missing) is now treated as an
        # UNRELIABLE quote, not silently accepted. If ask is missing/0
        # but bid looks real, this returns None (causing that ticker to
        # be skipped this run, same as any other data failure) rather
        # than publishing a number that LOOKS like a legitimate price
        # but is actually derived from half a quote.
        if bid and ask and bid > 0 and ask > 0:
            mid = (bid + ask) / 2
            spread_pct = abs(ask - bid) / mid * 100 if mid else 0
            if spread_pct > 5:
                print(f"  [SPOT WARN] {ticker}: unusually wide bid/ask spread "
                      f"({spread_pct:.1f}%) — bid={bid} ask={ask}, worth a second look")
            return mid

        print(f"  [SPOT WARN] {ticker}: incomplete quote (bid={bid}, ask={ask}) -- "
              f"at least one side is missing or exactly 0, which is NOT a reliable "
              f"spot price. Skipping this ticker this run rather than publishing a "
              f"number derived from an incomplete quote.")
        return None
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
        # Tracks how many contracts on each side had an invalid quote
        # (bid/ask missing or exactly 0) -- see the diagnosis note
        # above the has_valid_quote check below. Used after the loop to
        # tell a genuine "zero interest" side apart from a "we
        # couldn't get usable quote data" side.
        zero_quote_strikes = {}
        total_contracts_seen = {"call": 0, "put": 0}

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
                total_contracts_seen["call" if is_call else "put"] += 1

                snap = snapshots.get(symbol, {})
                bid, ask = snap.get("bid"), snap.get("ask")

                if not first_expiry_done:
                    (em_calls if is_call else em_puts)[strike] = {"bid": bid, "ask": ask}

                # DIAGNOSIS (2026-08-08): confirmed via real production
                # logs that entire sides of the chain (all put strikes
                # for AAPL/MSFT/AMZN/NVDA, all call strikes for
                # GOOGL/META/TSLA, in the SAME run) came back with
                # put_gex or call_gex summing to exactly 0 across every
                # strike -- not a threshold problem, a DATA problem.
                # Root cause: Alpaca's options snapshot quotes had
                # ask=0 (or bid=0) for every contract on the affected
                # side at this specific off-hours run time (8:00pm UTC
                # / 4:00pm ET, right at/after close) -- same falsy-zero
                # failure mode independently confirmed on the STOCK
                # quote side too (AAPL/MSFT/AMZN spot price bug, see
                # get_spot_price()'s diagnostic logging). "bid and ask"
                # is False when either is exactly 0, so iv silently
                # became 0.0 for every affected strike, and gamma/gex
                # followed to 0 -- indistinguishable, before this fix,
                # from a genuine absence of options interest.
                has_valid_quote = bid is not None and ask is not None and bid > 0 and ask > 0
                if not has_valid_quote:
                    zero_quote_strikes.setdefault("call" if is_call else "put", 0)
                    zero_quote_strikes["call" if is_call else "put"] += 1

                if has_valid_quote:
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

        # DATA-QUALITY CHECK (2026-08-08): reports, per side, what
        # fraction of that side's contracts had an unusable quote
        # (bid/ask missing or exactly 0) -- see the diagnosis note
        # above has_valid_quote earlier in this function. A side where
        # MOST or ALL contracts lacked a usable quote produced a
        # genuinely zero call_gex/put_gex sum for reasons that have
        # NOTHING to do with real market interest -- the old code had
        # no way to distinguish that from an honest "nobody's trading
        # this side" finding, and the wall-selection significance
        # filter downstream would reject a 0-total side either way,
        # silently. Logged here so a bad run is visible in the logs
        # BEFORE it reaches wall selection, rather than only showing up
        # as an unexplained missing wall several steps later.
        quote_quality = {}
        for side in ("call", "put"):
            total = total_contracts_seen.get(side, 0)
            zero_count = zero_quote_strikes.get(side, 0)
            if total > 0:
                zero_pct = zero_count / total
                quote_quality[side] = {"total": total, "zero_quote": zero_count, "zero_pct": zero_pct}
                if zero_pct > 0.5:
                    print(f"  [DATA QUALITY WARN] {ticker} {side} side: {zero_count}/{total} "
                          f"({zero_pct:.0%}) contracts had NO usable bid/ask this run -- "
                          f"any resulting wall/GEX finding on this side reflects MISSING DATA, "
                          f"not necessarily real market interest. This is a known off-hours "
                          f"data-availability issue (see get_spot_price()'s matching diagnostic), "
                          f"not a code bug.")

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

        # Computed here (moved earlier than before) specifically so wall
        # selection below can size its search radius against this
        # ticker's OWN expected move, instead of a flat percentage that
        # doesn't account for how volatile a given name actually is.
        expected_move = expected_move_from_quotes(em_calls, em_puts, spot)

        # WALL-SELECTION BUGFIX (2026-08-08): confirmed via a real
        # published run that "walls" were frequently just artifacts of
        # the flat +/-30% search band's own edge, not real market
        # structure. Example: NVDA's expected move for the week was
        # roughly +/-0.6%, yet its reported put wall sat at -27.8% away
        # from spot -- within a few DOLLARS of the exact 30% band floor
        # for every affected ticker (AAPL $2.84 from the floor, MSFT
        # $0.15, AMZN $3.62, NVDA $4.89 -- not a coincidence). The old
        # logic always forced an answer by picking whichever strike had
        # the most gamma among the tiny sliver of strikes still inside
        # the band, even when that strike had no real chance of being
        # reached and wasn't a genuine concentration of dealer exposure
        # -- just the least-thin option in an otherwise-thin tail.
        #
        # A wall that's 6x further away than the week's own expected
        # move isn't a usable risk level for a trader deciding what to
        # watch this week -- presenting it next to a genuinely-nearby
        # call wall as if the two were comparable is actively
        # misleading, not just imprecise.
        #
        # FIX, two parts:
        #   1. The search band is now sized off THIS ticker's actual
        #      expected move (not a flat 30% for every name regardless
        #      of real volatility) -- specifically, up to 3x the
        #      expected move, or a 10% floor if expected move is
        #      unavailable/tiny, whichever is larger. A wall up to 3x
        #      the expected move is still a meaningful "watch this
        #      level" data point; beyond that it's not realistically
        #      in play this week.
        #   2. Even within that tighter, more relevant band, a strike
        #      must clear a minimum share of that side's total gamma
        #      to count as a real wall (MIN_WALL_SHARE below) -- this
        #      stops a strike from being crowned "the wall" purely for
        #      being the biggest fish in an otherwise-empty pond.
        #   3. If nothing clears the bar, call_wall/put_wall is
        #      returned as None (already handled everywhere downstream
        #      -- see the 2026-08-07 side-of-spot fix's None-handling)
        #      rather than forcing a misleading answer.
        if expected_move and expected_move.get("pct"):
            band_pct = max(expected_move["pct"] * 3 / 100, 0.10)
        else:
            band_pct = 0.10
        banded_strikes = _restrict_to_band(per_strike, spot, band_pct=band_pct)

        above_spot = {k: v for k, v in banded_strikes.items() if k > spot}
        below_spot = {k: v for k, v in banded_strikes.items() if k < spot}

        # THRESHOLD LOWERED (2026-08-08): confirmed in production that
        # MIN_WALL_SHARE=0.15 was rejecting REAL put walls, not just
        # band-edge artifacts -- every one of 7 real Mag 7 tickers lost
        # its put wall in the same run, which is too systematic to be
        # coincidence and strongly suggests the original 0.15 threshold
        # (picked without empirical basis) was simply too strict for
        # real markets, where put-side open interest is often more
        # spread across strikes than call-side. Lowered to 0.10 as a
        # less aggressive first correction, PLUS added real diagnostic
        # logging below so future threshold tuning is based on actual
        # observed share percentages, not another blind guess.
        MIN_WALL_SHARE = 0.10  # a wall must hold at least 10% of its
                                # side's total gamma within the band to
                                # count as a real concentration, not
                                # just the least-thin strike available.

        def _select_wall(side_strikes, gex_key, side_label):
            if not side_strikes:
                print(f"  [WALL DEBUG] {ticker} {side_label}: no strikes on this side within the band")
                return None
            total = sum(v[gex_key] for v in side_strikes.values())
            if total <= 0:
                print(f"  [WALL DEBUG] {ticker} {side_label}: {len(side_strikes)} strike(s) in band, "
                      f"but total {gex_key}=0 — no real interest here")
                return None
            best_strike = max(side_strikes, key=lambda k: side_strikes[k][gex_key])
            best_share = side_strikes[best_strike][gex_key] / total
            print(f"  [WALL DEBUG] {ticker} {side_label}: best strike ${best_strike:,.0f} "
                  f"holds {best_share:.1%} of {len(side_strikes)}-strike band total "
                  f"(threshold={MIN_WALL_SHARE:.0%}) — {'ACCEPTED' if best_share >= MIN_WALL_SHARE else 'REJECTED'}")
            if best_share < MIN_WALL_SHARE:
                return None
            return best_strike

        call_wall = _select_wall(above_spot, "call_gex", "CALL side")
        put_wall = _select_wall(below_spot, "put_gex", "PUT side")
        max_pos_strike = max(banded_strikes, key=lambda k: banded_strikes[k]["net_gex"])
        max_neg_strike = min(banded_strikes, key=lambda k: banded_strikes[k]["net_gex"])

        gamma_flip = find_gamma_flip(per_strike, spot=spot)

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
            "quote_quality": quote_quality,
        }
    except Exception as e:
        return {"error": f"{ticker}: {type(e).__name__}: {e}", "ticker": ticker}


def pct_from_spot(strike, spot):
    if strike is None or not spot:
        return None
    return round((strike - spot) / spot * 100, 2)


def format_strike(v) -> str:
    """
    STRIKE-DISPLAY PRECISION BUGFIX (2026-08-10): see the module
    docstring for the full production-confirmed diagnosis (NVDA's
    "$212 (-2.7%)" not reconciling because the dollar figure was always
    truncated to zero decimals while the percentage used the real,
    unrounded strike). Shows a strike/wall dollar value at whatever
    precision it actually has -- a whole-dollar strike still displays
    with zero decimals ($212), but a half-dollar strike now correctly
    shows as such ($212.50) instead of silently rounding away the part
    that made the adjacent percentage true. Use this EVERYWHERE a
    call_wall / put_wall / max_pos_gex_strike / max_neg_gex_strike
    dollar value is rendered, in cards, embeds, or narrative text --
    never format one of these with a bare ":,.0f" again.
    """
    if v is None:
        return "N/A"
    v = float(v)
    if v == int(v):
        return f"${v:,.0f}"
    return f"${v:,.2f}"


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

    call_wall = result.get("call_wall")
    put_wall = result.get("put_wall")
    call_wall_pct = result.get("call_wall_pct")
    put_wall_pct = result.get("put_wall_pct")
    # BUGFIX (2026-08-07): call_wall/put_wall can now legitimately be
    # None (see compute_gex_vex()'s side-of-spot fix) if no strike
    # exists on the correct side of spot within the band -- format
    # defensively instead of assuming a numeric value is always present.
    # STRIKE-DISPLAY PRECISION FIX (2026-08-10): uses format_strike()
    # now, not a bare ":,.0f", so the dollar figure always matches the
    # percentage next to it. See module docstring.
    call_wall_str = f"{format_strike(call_wall)}  ({call_wall_pct:+.1f}%)" if call_wall is not None else "N/A (no strike above spot in band)"
    put_wall_str = f"{format_strike(put_wall)}  ({put_wall_pct:+.1f}%)" if put_wall is not None else "N/A (no strike below spot in band)"

    lines = [
        f"\U0001F4CA **{t}**  ${spot:,.2f}   (exp {exp_label})",
        f"  GAMMA FLIP  {flip_str}",
        f"  NET GEX  {gex_str}",
        f"  NET VEX  ${result['net_vex']/1e6:,.2f}M",
        f"  CALL WALL  {call_wall_str}",
        f"  PUT WALL   {put_wall_str}",
        f"  MAX +GEX   {format_strike(result['max_pos_gex_strike'])}   +${result['max_pos_gex_value']/1e6:,.2f}M",
        f"  MAX -GEX   {format_strike(result['max_neg_gex_strike'])}   -${abs(result['max_neg_gex_value'])/1e6:,.2f}M",
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
    """
    PLAIN-ENGLISH FIX (2026-08-08): the 2026-08-07 actionability pass
    made this end in a real instruction, but still used trading jargon
    ("wall," "fade," "size") -- same gap confirmed in the LLM-path
    output on a real published post. Rewritten in plain, beginner-
    legible English: "level" instead of "wall," "take profit" / "don't
    chase" instead of "fade" / "trim," and a brief plain-language
    reason attached to the instruction, not just the instruction alone.

    REGIME-AWARENESS BUGFIX (2026-08-08, later same day): this function
    is the FALLBACK -- the guaranteed-safe text swapped in whenever the
    LLM-generated line fails validation (see _line_matches_regime()).
    Confirmed via direct testing that this function ITSELF was
    regime-inconsistent: the "expected move falls short of both walls"
    branch always described "a calmer, more contained week" regardless
    of whether the ticker was actually long or short gamma -- but
    "contained/pulls back" is specifically a LONG-gamma property. A
    short-gamma ticker having a small expected move THIS WEEK doesn't
    mean it has the same self-correcting tendency; it just means the
    move hasn't been large so far. Fixed by threading is_long_gamma
    through every branch and writing genuinely different, mechanism-
    correct text for each case rather than one shared "calm" framing.

    This is the CORRECT per-ticker, singular-voice template -- see the
    2026-08-10 module docstring note for why this (not
    _consolidated_watch_line()) must be what a single-ticker call falls
    back to.
    """
    em = r.get("expected_move") or {}
    em_pct = em.get("pct")
    call_pct = r.get("call_wall_pct")
    put_pct = r.get("put_wall_pct")
    call_wall, put_wall = r.get("call_wall"), r.get("put_wall")
    ticker = r.get("ticker", "")
    is_long = r.get("net_gex", 0) >= 0

    # BUGFIX (2026-08-07): call_wall/put_wall can now legitimately be
    # None (see compute_gex_vex()'s side-of-spot fix). Handle that case
    # explicitly with its own message rather than crashing on an
    # f-string ":,.0f" format against None, or (worse) silently
    # formatting a wrong-side wall as if it were still meaningful.
    if call_wall is None and put_wall is None:
        return (f"{ticker} doesn't have a clear ceiling or floor level nearby this week — "
                f"there's no strong lean either way, so keep positions small and stay flexible.")
    if call_wall is None:
        return (f"{ticker} doesn't have a clear ceiling level above the current price this week, "
                f"so a move higher has room to run — but if it drops back toward {format_strike(put_wall)}, "
                f"that level has previously acted as a floor.")
    if put_wall is None:
        return (f"{ticker} doesn't have a clear floor level below the current price this week, "
                f"so a move lower has room to run — but if it rallies toward {format_strike(call_wall)}, "
                f"that level has previously acted as a ceiling.")

    if em_pct is None or call_pct is None or put_pct is None:
        if is_long:
            return (f"{ticker} will likely stay somewhere between {format_strike(put_wall)} and "
                    f"{format_strike(call_wall)} this week — expect back-and-forth trading that tends "
                    f"to get pulled back toward the middle rather than a big breakout.")
        return (f"{ticker} will likely stay somewhere between {format_strike(put_wall)} and "
                f"{format_strike(call_wall)} this week, but keep in mind there's less of a cushion here "
                f"than usual — if it does break past either level, the move could extend "
                f"further than a typical week.")

    reaches_call = em_pct >= call_pct
    reaches_put = em_pct >= abs(put_pct)

    if reaches_call and reaches_put:
        if is_long:
            return (f"{ticker} could realistically swing toward either {format_strike(put_wall)} "
                    f"or {format_strike(call_wall)} this week, but it tends to get pulled back toward "
                    f"the middle rather than break through cleanly. Keep position sizes modest "
                    f"and don't chase either move.")
        return (f"{ticker} could realistically swing all the way to either {format_strike(put_wall)} "
                f"or {format_strike(call_wall)} this week, and with less cushion than usual, a move past "
                f"either level could run further than expected. Keep position sizes modest and "
                f"be ready for it to go either direction.")
    if reaches_call:
        if is_long:
            return (f"{ticker} could realistically reach {format_strike(call_wall)} this week. If it "
                    f"gets there, expect it to get pulled back rather than break out — that's a "
                    f"reasonable spot to take some profit rather than assume it keeps climbing.")
        return (f"{ticker} could realistically reach {format_strike(call_wall)} this week. If it clears "
                f"that level, the move could extend further than usual since there's less "
                f"cushion above it — take some profit there rather than assume it keeps "
                f"climbing without a pause.")
    if reaches_put:
        if is_long:
            return (f"{ticker} could realistically drop to {format_strike(put_wall)} this week. If it "
                    f"gets there, expect it to get pulled back rather than break down further — "
                    f"that's a reasonable spot to take some profit on a bearish position.")
        return (f"{ticker} could realistically drop to {format_strike(put_wall)} this week. If it clears "
                f"that level, the move could extend further than usual since there's less "
                f"cushion below it — take some profit on a bearish position rather than assume "
                f"it keeps falling without a pause.")
    if is_long:
        return (f"{ticker}'s likely range this week probably won't stretch all the way to "
                f"{format_strike(put_wall)} or {format_strike(call_wall)} — expect a calmer, more contained week, "
                f"so it's better to trade the back-and-forth than to bet on a big breakout.")
    return (f"{ticker}'s likely range this week probably won't stretch all the way to "
            f"{format_strike(put_wall)} or {format_strike(call_wall)} on paper, but keep in mind there's less of "
            f"a cushion here than usual — if something does push it past either level, the move "
            f"could pick up speed quickly, so don't assume it stays this quiet all week.")


def _classify_wall_reach(r: dict) -> str:
    em = r.get("expected_move") or {}
    em_pct = em.get("pct")
    call_pct = r.get("call_wall_pct")
    put_pct = r.get("put_wall_pct")
    # BUGFIX (2026-08-07): call_wall_pct/put_wall_pct can now be None
    # if compute_gex_vex() found no strike on the correct side of spot
    # -- treat that the same as "unknown" here rather than crashing on
    # abs(None).
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
    """
    PLAIN-ENGLISH FIX (2026-08-08): same rewrite as _fallback_watch_line()
    applied to the consolidated (all-tickers-share-a-pattern) case --
    "level" instead of "wall," everyday phrasing instead of trader
    shorthand, and a brief plain-language reason attached.

    SAFE-BY-CONSTRUCTION NOTE (2026-08-08): this function formats
    put_wall/call_wall with no explicit None-guard of its own, unlike
    _fallback_watch_line() and the data-line builder in
    generate_gex_watch_lines() (both of which needed real fixes after
    a production crash -- see their bugfix notes). This one doesn't
    need the same fix: build_watch_lines_fallback() only ever calls
    this function when EVERY ticker in the batch has classification !=
    "unknown", and _classify_wall_reach() only returns a non-"unknown"
    classification when call_wall_pct/put_wall_pct are BOTH not None
    (which only happens when call_wall/put_wall themselves are not
    None) -- so by the time execution reaches here, every wall value
    used below is already guaranteed present. Documented explicitly so
    this isn't mistaken for a missed case during a future audit.

    MULTI-TICKER-ONLY NOTE (2026-08-10): this function's language
    ("could all... their... one of these") is deliberately written in
    PLURAL, shared voice -- it is only ever meant to describe several
    tickers that share the same pattern this week (the real use case:
    the 3-ticker SPY/QQQ/IWM dashboard). build_watch_lines_fallback()
    and generate_gex_watch_lines() now both gate the call to this
    function on len(valid) > 1, so it should never again be invoked for
    a single ticker -- see their matching 2026-08-10 bugfix notes for
    the production incident (GOOGL/AMZN's Mag 7 cards) this guards
    against.
    """
    valid = [r for r in results if "error" not in r]
    tickers_str = "/".join(r["ticker"] for r in valid)

    if classification == "put_only":
        levels = " · ".join(f"{r['ticker']} {format_strike(r['put_wall'])}" for r in valid)
        return (f"{tickers_str} could all realistically drift lower toward their nearby "
                f"support levels this week ({levels}). If you're holding a bearish position "
                f"and one of these gets there, that's a sensible spot to take profit rather "
                f"than assume the drop keeps going.")
    if classification == "call_only":
        levels = " · ".join(f"{r['ticker']} {format_strike(r['call_wall'])}" for r in valid)
        return (f"{tickers_str} could all realistically climb toward their nearby resistance "
                f"levels this week ({levels}). If one gets there, that's a sensible spot to "
                f"take profit rather than assume it keeps climbing.")
    if classification == "both":
        levels = " · ".join(f"{r['ticker']} {format_strike(r['put_wall'])}/{format_strike(r['call_wall'])}" for r in valid)
        return (f"{tickers_str} all have enough room to swing to either their upper or lower "
                f"levels this week ({levels}) — expect a wider, more volatile range, so keep "
                f"position sizes modest.")
    levels = " · ".join(f"{r['ticker']} {format_strike(r['put_wall'])}/{format_strike(r['call_wall'])}" for r in valid)
    return (f"{tickers_str} all look likely to stay contained within their usual range this "
            f"week ({levels}) — expect a calmer, more back-and-forth week rather than a big "
            f"move, so trading the range makes more sense than betting on a breakout.")


def build_watch_lines_fallback(results: list) -> dict:
    """
    SINGLE-TICKER BUGFIX (2026-08-10): previously used the consolidated
    (plural, shared-voice) template whenever every ticker in `results`
    happened to share the same classification -- which, for a
    single-element `results` list, is ALWAYS trivially true (there's
    only one classification to compare against itself). This meant
    every single-ticker call (the Mag 7 pipeline calls this with one
    ticker at a time) fell back to text like "GOOGL could all
    realistically drift lower toward their nearby support levels" --
    plural language on a one-ticker card, confirmed in a real
    production run. Now requires len(valid) > 1 before using the
    consolidated path at all; a single ticker always gets
    _fallback_watch_line()'s correct, singular-voice template, which
    also correctly mentions BOTH the call wall and put wall when
    available (the consolidated template only mentions whichever side
    the shared classification refers to, which is fine across several
    tickers with a genuinely shared pattern, but wrong for a single
    ticker's own card).
    """
    valid = [r for r in results if "error" not in r]
    if not valid:
        return {}
    classifications = {r["ticker"]: _classify_wall_reach(r) for r in valid}
    unique_patterns = set(classifications.values())

    if len(valid) > 1 and len(unique_patterns) == 1 and "unknown" not in unique_patterns:
        shared = _consolidated_watch_line(results, next(iter(unique_patterns)))
        return {r["ticker"]: shared for r in valid}

    return {r["ticker"]: _fallback_watch_line(r) for r in valid}


# VALIDATION-AND-FALLBACK FIX (2026-08-08): three escalating rounds of
# prompt engineering (banned phrases -> absolute rule + worked example
# -> deterministic pre-computed FACT handed to the model) reduced the
# regime-mismatch failure rate but never eliminated it -- confirmed via
# 8 total real test runs across all three rounds, with the LAST round
# (the model handed a pre-computed, correct-by-construction FACT to
# use) STILL producing a run where ALL THREE tickers had the wrong
# mechanism (short-gamma tickers described with long-gamma "pulls back"
# language, and the long-gamma ticker described with short-gamma "keep
# going further" language) -- confirming this is not a prompt-wording
# problem that further instruction-tuning can reliably solve; the model
# sometimes contradicts a fact it was explicitly handed.
#
# FIX: stop trying to prevent the error via instructions alone. Instead,
# VALIDATE each ticker's generated line against the same deterministic
# regime classification used elsewhere in this module, and if a line
# uses acceleration-style phrasing for a long-gamma ticker (or vice
# versa), swap in that ticker's line from build_watch_lines_fallback()
# -- which is code-only, has no LLM involved, and is therefore
# regime-correct by construction every time. This guarantees nothing
# posted to Discord is ever a demonstrably wrong statement about a
# ticker's mechanism, even though it means an occasional ticker gets
# the more template-y fallback wording instead of a fresh LLM sentence
# -- a mismatched swap in wording quality is a far smaller problem than
# publishing a factually backwards explanation to subscribers.
_ACCELERATION_PHRASES = [
    "run further than usual", "could continue further", "continue past",
    "could run away", "run away to the upside", "run away to the downside",
    "could extend", "little to slow", "nothing to slow", "less support",
    "less to slow", "continuation above", "continuation past",
    "keep going", "keep running", "little resistance", "little cushion",
    "leaves more room", "leaves little cushion", "less cushion",
    "accelerate", "run harder", "bigger move once", "extend further",
]
_PULLBACK_PHRASES = [
    "pull back", "pulled back", "pulls back", "gets pulled", "get pulled",
    "self-correct", "cushion that tends to pull", "come back toward",
    "snap back", "revert toward", "reverts toward", "back toward the middle",
    "back toward the center", "pulls price back", "pull price back",
    "keep it in check", "keeps it in check",
    "keep it pinned", "stays pinned",
]


def _line_matches_regime(ticker_line: str, is_long_gamma: bool) -> bool:
    """
    Returns False if the line contains phrasing that describes the
    WRONG mechanism for this ticker's actual regime -- i.e. a
    long-gamma ticker's line using acceleration/"could run further"
    language, or a short-gamma ticker's line using pull-back/
    self-correcting language. Either mismatch means the line makes a
    factually backwards claim about how this ticker tends to behave.

    This is intentionally a simple substring check, not a full semantic
    read -- it exists to catch the SPECIFIC, CONFIRMED failure pattern
    from real test runs, not to validate every possible way a line
    could be wrong. A line with neither set of phrases present (e.g. it
    only talks about price levels without characterizing the
    mechanism) passes -- there's nothing wrong to catch there.
    """
    line_lower = ticker_line.lower()
    has_acceleration = any(p in line_lower for p in _ACCELERATION_PHRASES)
    has_pullback = any(p in line_lower for p in _PULLBACK_PHRASES)

    if is_long_gamma and has_acceleration:
        return False
    if (not is_long_gamma) and has_pullback and not has_acceleration:
        # A short-gamma ticker described PURELY in pull-back terms with
        # no acceleration language anywhere is also backwards -- short
        # gamma should lean toward "could extend," not "gets pulled back."
        return False
    return True


def generate_gex_watch_lines(results: list, api_key: str = None) -> dict:
    """
    PLAIN-ENGLISH FIX (2026-08-08): yesterday's actionability fix (see
    the 2026-08-07 note below) successfully got every line ending in a
    real instruction (fade / trim / stay defined-risk / etc.), but
    confirmed in a REAL published post that it did NOT fix the actual
    complaint -- the lines were still full of unexplained trading
    jargon ("short gamma near flip," "call wall," "defined-risk," used
    as a verb: "fade moves toward $X"). A beginner reading "SPY short
    gamma near flip -- fade moves toward $769 or $777, stay
    defined-risk" has no idea what any of those four terms mean, even
    though the sentence technically satisfied the action-clause
    requirement. Actionable and plain-English are two separate bars;
    the previous fix only cleared the first one.

    This is, again, a PROMPT-SHAPE change only -- every run still
    pulls that day's real numbers into the prompt fresh. What changed:
      1. An explicit, enforced BANNED JARGON list (gamma, flip, wall,
         regime, defined-risk, fade [as a verb], dealer, hedging, etc.)
         -- the model must express the same underlying fact in
         everyday words instead (e.g. "expect back-and-forth chop,"
         "don't chase a big move," "a good spot to take some profit").
      2. Length constraints loosened substantially, per direct user
         instruction ("it doesn't have to be so short... extend the
         write up... a little more if that makes sense") -- a
         beginner-legible explanation of WHY a level matters usually
         needs more words than a jargon-dense one-liner, not fewer.
      3. Requires each line to briefly explain WHY (in plain terms,
         e.g. "because the market tends to swing harder once it
         breaks past this point") before or alongside the instruction,
         not just the instruction alone -- a beginner acting on advice
         they don't understand at all is a worse outcome than a
         slightly longer explanation.

    VALIDATION-AND-FALLBACK FIX (2026-08-08, later same day): see the
    module-level comment above _line_matches_regime() for the full
    story -- after prompt-only fixes plateaued at roughly a 1-in-3 to
    1-in-8 mismatch rate (and one run got ALL THREE tickers backwards
    despite being handed the correct fact directly), every per-ticker
    line generated here is now validated against the same deterministic
    regime check, and swapped for the guaranteed-correct fallback line
    if it fails. This applies whether the batch went through the
    same-pattern (consolidated paragraph) path or the per-ticker path.

    SINGLE-TICKER BUGFIX (2026-08-10): `same_pattern` now also requires
    len(valid) > 1 -- see build_watch_lines_fallback()'s matching
    2026-08-10 note for the full production incident this fixes (the
    Mag 7 pipeline calls this function once per ticker with a
    single-element list, which previously always satisfied the old
    "all classifications equal" check trivially and routed through the
    plural, multi-ticker consolidated-paragraph prompt/fallback even
    for one ticker).
    """
    valid = [r for r in results if "error" not in r]
    fallback = build_watch_lines_fallback(results)

    api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key or not valid:
        if not api_key:
            print("[GEX WARN] generate_gex_watch_lines: no OPENROUTER_API_KEY set — using data-derived fallback lines")
        return fallback

    classifications = {r["ticker"]: _classify_wall_reach(r) for r in valid}
    unique_patterns = set(classifications.values())
    same_pattern = len(valid) > 1 and len(unique_patterns) == 1 and "unknown" not in unique_patterns

    # DETERMINISTIC REGIME-BEHAVIOR FIX (2026-08-08): confirmed via 5
    # real test runs (2 in the original actionability-only version, 3
    # in the first regime-instruction attempt) that asking the LLM to
    # DERIVE the correct plain-English mechanism from "long gamma" vs.
    # "short gamma" is unreliable -- it kept independently reasoning
    # its way to short-gamma-style "could run further" language for
    # IWM (a long-gamma ticker) specifically when IWM's PUT wall
    # happened to be unusually far away, incorrectly generalizing "that
    # one side is unconstrained" into "so this ticker isn't
    # self-correcting at all." Tightening the prompt's wording reduced
    # the failure rate (2/2 -> 1/3) but did not eliminate it, and the
    # "passing" runs still contained the same underlying wrong
    # inference in different phrasing the banned-word check didn't
    # catch (e.g. "leaves more room for bigger swings" instead of the
    # exact banned phrase "run further than usual") -- confirming this
    # is a reasoning failure, not a wording failure, and no amount of
    # "don't say X" instruction reliably suppresses an INFERENCE, only
    # specific phrasings of it.
    #
    # FIX: the correct plain-English regime-behavior sentence is now
    # computed HERE, in Python, deterministically -- correct by
    # construction, the same way beat_strength is pre-classified in
    # er_lotto_scanner.py rather than left for the model to judge. This
    # fact is handed to the model as a GIVEN in the data line (not
    # something to derive), and the model's job is narrowed to: use
    # this fact to write the explanation in a natural, varied,
    # conversational way -- not to reason about which mechanism
    # applies in the first place. This removes the failure mode
    # entirely rather than reducing its frequency.
    def _regime_behavior_fact(net_gex: float) -> str:
        if net_gex >= 0:
            return ("FACT (use this, do not contradict it): this ticker tends to get pulled "
                     "back toward the middle of its range if it swings too far in EITHER "
                     "direction, regardless of how close or far either the put or call level "
                     "is. Explain the outcome using this self-correcting/pull-back idea.")
        return ("FACT (use this, do not contradict it): this ticker has LESS of a cushion "
                "against bigger moves -- once price clears a nearby level, the move can "
                "accelerate rather than get pulled back. Explain the outcome using this "
                "less-support/can-extend idea.")

    data_lines = []
    for r in valid:
        flip = r.get("gamma_flip")
        flip_str = f"${flip:,.2f}" if flip is not None else "no clean flip"
        regime = "short gamma" if r["net_gex"] < 0 else "long gamma"
        em = r.get("expected_move") or {}
        behavior_fact = _regime_behavior_fact(r["net_gex"])
        # BUGFIX (2026-08-08): call_wall/put_wall can legitimately be
        # None now that wall selection requires a minimum significance
        # threshold -- this was the THIRD unguarded format-string site
        # found after the same crash hit build_gex_embed() in
        # production (see that function's matching bugfix note). Same
        # fix pattern: format each side independently with an explicit
        # None check, rather than assuming both walls always exist.
        # STRIKE-DISPLAY PRECISION FIX (2026-08-10): uses
        # format_strike() so the number shown to the model (and thus
        # what it may echo back in its output) matches what's actually
        # displayed on the card. See module docstring.
        put_wall_str = (f"{format_strike(r['put_wall'])} ({r['put_wall_pct']:+.1f}%)"
                         if r.get("put_wall") is not None else "no clear level nearby")
        call_wall_str = (f"{format_strike(r['call_wall'])} ({r['call_wall_pct']:+.1f}%)"
                          if r.get("call_wall") is not None else "no clear level nearby")
        data_lines.append(
            f"{r['ticker']}: spot ${r['spot']:,.2f}, {regime}, "
            f"put wall {put_wall_str}, "
            f"call wall {call_wall_str}, "
            f"expected move \u00b1{em.get('pct', '?')}%, gamma flip {flip_str}\n"
            f"  {behavior_fact}"
        )

    base_instructions = (
        'This posts directly below a table that ALREADY shows each ticker\'s exact put '
        'wall, call wall, and expected move numbers -- you do not need to re-derive those '
        'numbers, and you do NOT need to name them using trading jargon. Your reader is a '
        'BEGINNER subscriber with no options-trading background. Your job is to explain, in '
        'plain everyday English, what kind of week this setup implies and what to actually '
        'do about it -- as if talking to a smart friend who has never traded options.\n\n'
        f"Data:\n{chr(10).join(data_lines)}\n\n"
        'REQUIRED, in this order, for every ticker:\n'
        '  1. A plain-English description of what kind of price action to expect (choppy/'
        'range-bound vs. sharper/bigger swings vs. steady grind) -- use the FACT given in the '
        'data for that ticker as the basis, translated into what it actually FEELS like to '
        'watch the stock trade. Do NOT reason about the mechanism yourself or override the '
        'given FACT with your own judgment, even if a wall looks unusually close or far -- the '
        'FACT already accounts for that ticker\'s real situation and is correct as given.\n'
        '  2. A concrete instruction using the real price level(s) from the data (e.g. "if it '
        'rallies toward $X, that\'s a good spot to take profit rather than chase higher" or '
        '"keep positions small this week" or "a move past $X could run further than usual, so '
        'don\'t fight it" -- only use "could run further" language if the ticker\'s FACT says '
        'less cushion/can extend; use "gets pulled back" language if the FACT says '
        'self-correcting/pull-back).\n'
        '  3. Briefly, in plain words, WHY -- restate the given FACT in your own natural '
        'phrasing, one clause is enough (e.g. "...because there\'s a cushion that tends to '
        'pull it back" or "...because there\'s less support once it clears that level").\n\n'
        'BANNED WORDS/PHRASES -- do not use ANY of these, express the same idea in plain '
        'English instead: "gamma," "flip," "wall" (say "level" or the actual dollar figure '
        'instead), "regime," "defined-risk," "fade" (as a verb -- say "don\'t chase" or "take '
        'profit" instead), "dealer," "dealer hedging," "positioning," "short gamma," "long '
        'gamma," "FACT" (that word is a label for you, never print it). If you catch yourself '
        'about to use one of these words, stop and rewrite that clause in plain language.\n\n'
        'Good examples of the STYLE required (write fresh content from the real data above, do '
        'not reuse these examples verbatim):\n'
        '  "SPY: expect a bumpy, back-and-forth week rather than a clean trend in either '
        'direction -- if it pushes up toward $777 or down toward $769, don\'t chase that move, '
        'since the options market is set up to pull price back toward the middle of that range. '
        'Keep position sizes small and take quick profits rather than holding for a big breakout."\n'
        '  "IWM: expect a steadier week than the other two, with a built-in cushion that tends '
        'to pull it back if it swings too far in either direction. If it pushes up toward $302, '
        'don\'t expect it to run away to the upside -- that cushion should keep it in check, so '
        'ride the move rather than betting on a breakout past it."\n\n'
        'Direct, warm, plain-spoken -- like explaining it to a friend, not writing for other '
        'traders. Complete sentences, not clipped trader shorthand.'
    )

    if same_pattern:
        prompt = f"""You are writing the "what to watch" section for a GEX/VEX options positioning snapshot from BlueMoonTrades (BMT), read by beginner subscribers.

{base_instructions}

All three tickers share the SAME setup this week ({next(iter(unique_patterns))} pattern). Write ONE combined paragraph (3-5 sentences) covering all three tickers together, naming each ticker and its real price level(s), following the REQUIRED structure and BANNED WORDS rules above. Output ONLY that paragraph, nothing else before or after."""
    else:
        prompt = f"""You are writing the "what to watch" section for a GEX/VEX options positioning snapshot from BlueMoonTrades (BMT), read by beginner subscribers.

{base_instructions}

The tickers have DIFFERENT setups this week, so write one entry per ticker, each 2-3 full sentences following the REQUIRED structure and BANNED WORDS rules above. Output ONLY lines in this EXACT format, one per ticker, nothing else before or after:
TICKER | 2-3 sentence plain-English explanation ending in a concrete instruction"""

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "x-ai/grok-4.3", "max_tokens": 600, "temperature": 0.6,
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
            # Consolidated path: one shared paragraph covers all tickers,
            # which only happens when they share the SAME classification
            # (_classify_wall_reach), but they can still have DIFFERENT
            # gamma signs feeding into this paragraph in principle -- so
            # validate per-ticker against the shared text and fall back
            # to the deterministic per-ticker fallback (not the shared
            # consolidated fallback) for any ticker whose regime the
            # shared paragraph appears to contradict.
            result = {}
            for r in valid:
                is_long = r["net_gex"] >= 0
                if _line_matches_regime(cleaned, is_long):
                    result[r["ticker"]] = cleaned
                else:
                    print(f"[GEX WARN] generate_gex_watch_lines: consolidated line contradicts "
                          f"{r['ticker']}'s actual regime — using per-ticker fallback for {r['ticker']} instead")
                    result[r["ticker"]] = fallback.get(r["ticker"], cleaned)
            return result
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

    # VALIDATION PASS: check every ticker's line (whether it came from
    # the LLM or was already the fallback from an unmatched line above)
    # against its real, deterministic regime. Any line that describes
    # the wrong mechanism gets swapped for that ticker's guaranteed-
    # correct fallback line instead. This is the actual fix -- see the
    # module-level comment above _line_matches_regime() for why prompt
    # instructions alone were not sufficient.
    validated = dict(parsed)
    for r in valid:
        ticker = r["ticker"]
        line = validated.get(ticker, "")
        is_long = r["net_gex"] >= 0
        if line and not _line_matches_regime(line, is_long):
            print(f"[GEX WARN] generate_gex_watch_lines: {ticker}'s generated line contradicts "
                  f"its actual regime ({'long' if is_long else 'short'} gamma) — swapping in the "
                  f"deterministic fallback line for {ticker} instead. Rejected line was: {line!r}")
            validated[ticker] = fallback.get(ticker, line)

    return validated


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
        # BUGFIX (2026-08-08): call_wall/put_wall can legitimately be
        # None now that wall selection requires a minimum significance
        # threshold (see compute_gex_vex()'s wall-selection fix) --
        # confirmed in production: this line crashed BOTH the Mag 7 and
        # SPY/QQQ/IWM sections with "unsupported format string passed
        # to NoneType.__format__" the very first run after that fix
        # shipped, since a ticker with no wall clearing the bar on
        # either side now legitimately returns None here, and this
        # f-string had no guard for that (unlike the card renderers,
        # which already handled it). Formats each side independently so
        # a ticker missing ONE wall still shows the other correctly,
        # rather than the whole line falling back to "N/A" over a
        # single missing side.
        # STRIKE-DISPLAY PRECISION FIX (2026-08-10): uses
        # format_strike() so this matches the card image exactly. See
        # module docstring.
        put_str = format_strike(r.get("put_wall"))
        call_str = format_strike(r.get("call_wall"))
        levels_lines.append(
            f"**{r['ticker']}**  Put {put_str} \u00b7 Call {call_str}"
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
        fields.append({"name": "\U0001F440 What To Watch \u2014 Action", "value": "\n".join(watch_field_lines), "inline": False})

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


def compute_single_ticker_subtitle(r: dict) -> tuple:
    """
    NEW (2026-08-08): standalone-ticker equivalent of
    compute_regime_subtitle(), for the Mag 7 daily pipeline where each
    ticker is rendered on its OWN card with no same-run peers to rank
    against (compute_regime_subtitle()'s "most/moderately/mildest of
    the group" ranking has nothing to compare against for a single
    ticker run alone). Returns the same (rank_str, implication_str)
    tuple shape so render_single_ticker_gex_card() can reuse the exact
    same rendering code path as the multi-ticker card's per-panel loop.
    """
    net_gex = r.get("net_gex")
    if net_gex is None:
        return ("", "")
    sign_word = "short" if net_gex < 0 else "long"
    imp = ("moves likely to fade back toward range" if sign_word == "long"
           else "moves could run further than usual if a wall breaks")
    return (f"{sign_word} gamma", imp)


def render_single_ticker_gex_card(r: dict, week_label: str, out_path: str):
    """
    NEW (2026-08-08): renders ONE ticker's GEX/VEX panel as its own
    standalone Discord-ready PNG -- built for the Mag 7 daily pipeline,
    where each of the 7 tickers gets posted as its own separate card
    rather than several tickers side-by-side in one wide image (per
    direct user request: cramming 7 tickers into one card was
    confirmed to make it "too clumsy and unreadable").

    This is NOT a new design -- it's the exact same per-ticker panel
    already proven in render_gex_dashboard_card() (colors, layout,
    POSITIONING/KEY LEVELS/EXPECTED MOVE sections, range bar with
    wall/flip/spot markers, wall-collision handling), just rendered as
    a single full-width panel instead of one of several narrow columns.
    render_gex_dashboard_card() itself is UNCHANGED and continues to
    power the existing 3-across SPY/QQQ/IWM card exactly as before --
    this is a new, separate function, not a modification of that one.
    """
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
    # FONT BUGFIX (2026-08-08): the original "Liberation Mono"/"Liberation
    # Sans" font names are NOT installed in the Railway container --
    # confirmed via a real production run that logged hundreds of
    # "findfont: Font family ... not found" warnings per card (one per
    # text element drawn), which also flooded/truncated the Railway log
    # viewer before the actual run-summary/posting-confirmation lines
    # could be seen. Cards still rendered (matplotlib was silently
    # falling back to DejaVu Sans regardless), so this was a log-noise
    # and diagnostics problem, not a rendering failure -- but explicitly
    # requesting DejaVu Sans / DejaVu Sans Mono (matplotlib's own bundled
    # default fonts, guaranteed present with zero extra system
    # dependencies) means the SAME font actually gets used with no
    # fallback warning at all, keeping production logs clean and
    # actually readable.
    _DATA_FONT   = "DejaVu Sans Mono"
    _HEADER_FONT = "DejaVu Sans"

    def _fmt_b(v):
        return f"-${abs(v)/1e9:.2f}B" if v < 0 else f"+${v/1e9:.2f}B"

    def _fmt_m(v):
        return f"-${abs(v)/1e6:.2f}M" if v < 0 else f"+${v/1e6:.2f}M"

    FIG_W = 8.4  # single-panel width -- roughly the per-ticker column
                 # width from the 3-across dashboard, standalone.
    MARGIN = 0.36
    HDR_H = 0.64
    SUB_H = 0.26
    GAP = 0.30

    STAT_ROW_H = 0.42
    SECTION_LABEL_H = 0.34
    N_SECTIONS = 3
    N_STAT_ROWS = 9
    SECTION_GAP = 0.11
    STAT_SECTION_H = (N_SECTIONS * SECTION_LABEL_H) + (N_STAT_ROWS * STAT_ROW_H) + (N_SECTIONS * SECTION_GAP)
    CARD_PAD = 0.26

    HEADER_BLOCK_H = 0.22 + 0.32 + 0.28 + 0.22 + 0.24
    BAR_H_ACTUAL = 0.40
    BAR_BLOCK_H = 0.32 + BAR_H_ACTUAL + 0.46
    CARD_H = CARD_PAD + HEADER_BLOCK_H + BAR_BLOCK_H + STAT_SECTION_H

    KEY_H = 0.58
    FOOTER_H = 0.36

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

    ticker = r.get("ticker", "?")

    if "error" in r:
        rect(MARGIN, cur, usable_w, HDR_H, _HDR_BG)
        hline(cur + HDR_H, MARGIN, MARGIN + usable_w, _GOLD, 1.4)
        txt(MARGIN + 0.22, cur + HDR_H / 2, f"{ticker} GEX / VEX  \u2014  {week_label}",
            fs=13, color=_HDR_TXT, bold=True, font=_HEADER_FONT)
        cur += HDR_H + GAP
        rrect(MARGIN, cur, usable_w, 1.0, _CARD_BG, radius=0.07, ec=_BORDER, lw=0.8, z=2)
        txt(MARGIN + usable_w / 2, cur + 0.5, f"{ticker}: data unavailable this run",
            fs=9, color=_TXT_DIM, ha="center", font=_HEADER_FONT)
        cur += 1.0 + MARGIN
        fig.set_size_inches(FIG_W, cur)
        plt.savefig(out_path, facecolor=_BG, bbox_inches="tight", pad_inches=0.2)
        plt.close(fig)
        print(f"[GEX] Single-ticker error card ({ticker}) -> {out_path}")
        return

    rect(MARGIN, cur, usable_w, HDR_H, _HDR_BG)
    hline(cur + HDR_H, MARGIN, MARGIN + usable_w, _GOLD, 1.4)
    txt(MARGIN + 0.22, cur + HDR_H / 2, f"{ticker} GEX / VEX  \u2014  {week_label}",
        fs=13, color=_HDR_TXT, bold=True, font=_HEADER_FONT)
    cur += HDR_H

    txt(FIG_W / 2, cur + SUB_H / 2,
        "Standard public-GEX approximation \u2014 dealers assumed net long calls / net short puts",
        fs=7.4, color=_TXT_DIM, ha="center", font=_HEADER_FONT)
    cur += SUB_H + GAP

    spot = r["spot"]
    net_gex = r["net_gex"]
    net_vex = r["net_vex"]
    is_short = net_gex < 0
    regime_color = _RED if is_short else _GREEN

    rrect(MARGIN, cur, usable_w, CARD_H, _CARD_BG, radius=0.07, ec=_BORDER, lw=0.9, z=2)
    rrect(MARGIN, cur, usable_w, 0.10, regime_color, radius=0.05, z=3)

    inner_x0 = MARGIN + CARD_PAD
    inner_w = usable_w - CARD_PAD * 2
    y = cur + CARD_PAD + 0.24

    txt(inner_x0, y, ticker, fs=19, color=_TXT, bold=True, font=_HEADER_FONT)
    txt(inner_x0 + 1.05, y, f"${spot:,.2f}", fs=11.5, color=_TXT_DIM, font=_DATA_FONT)
    y += 0.32

    regime_word = "SHORT GAMMA" if is_short else "LONG GAMMA"
    txt(inner_x0, y, regime_word, fs=10.5, color=regime_color, bold=True, font=_HEADER_FONT)
    y += 0.28

    rank_str, implication_str = compute_single_ticker_subtitle(r)
    txt(inner_x0, y, rank_str, fs=8.0, color=_TXT_DIM, style="italic", font=_HEADER_FONT)
    y += 0.22
    txt(inner_x0, y, implication_str, fs=7.6, color=_TXT, font=_HEADER_FONT)
    y += 0.24

    bar_h = BAR_H_ACTUAL
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

        ax.plot([pw_x, pw_x], [bar_y - 0.04, bar_y + bar_h + 0.04], color=_RED, linewidth=2.6, zorder=5)
        ax.plot([cw_x, cw_x], [bar_y - 0.04, bar_y + bar_h + 0.04], color=_GREEN, linewidth=2.6, zorder=5)

        if collision:
            mid_x = (pw_x + cw_x) / 2
            txt(mid_x, bar_y - 0.18, f"P/C \u2248 {format_strike(put_wall)}", fs=7.2, color=_TXT, bold=True, ha="center", font=_HEADER_FONT)
        else:
            txt(pw_x, bar_y - 0.18, f"P {format_strike(put_wall)}", fs=7.2, color=_RED, bold=True, ha="center", font=_HEADER_FONT)
            txt(cw_x, bar_y - 0.18, f"C {format_strike(call_wall)}", fs=7.2, color=_GREEN, bold=True, ha="center", font=_HEADER_FONT)

    if gamma_flip is not None and range_min <= gamma_flip <= range_max:
        gf_x = to_x(gamma_flip)
        ax.plot([gf_x, gf_x], [bar_y - 0.07, bar_y + bar_h + 0.07], color="#c084fc", linewidth=1.6, linestyle="--", zorder=5)

    sp_x = to_x(spot)
    ax.plot([sp_x], [bar_y + bar_h / 2], marker="o", markersize=10,
            markerfacecolor=_GOLD, markeredgecolor="#ffffff", markeredgewidth=1.7, zorder=7)

    y = bar_y + bar_h + 0.46

    def stat_row(label, value, color, y):
        rect(inner_x0 - 0.07, y - STAT_ROW_H / 2 + 0.03, inner_w + 0.14, STAT_ROW_H - 0.07, _ROW_BG, z=2)
        txt(inner_x0, y, label, fs=8.0, color=_TXT_DIM, font=_HEADER_FONT)
        txt(inner_x0 + inner_w, y, value, fs=9.0, color=color, bold=True, ha="right")

    def section_label(text, y):
        txt(inner_x0, y, text, fs=7.3, color="#7dd3fc", bold=True, font=_HEADER_FONT)

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
    stat_row("Call Wall", f"{format_strike(call_wall)}  ({cw_pct:+.1f}%)" if call_wall else "N/A", _GREEN, y); y += STAT_ROW_H
    stat_row("Put Wall", f"{format_strike(put_wall)}  ({pw_pct:+.1f}%)" if put_wall else "N/A", _RED, y); y += STAT_ROW_H
    mpg_strike, mpg_val = r.get("max_pos_gex_strike"), r.get("max_pos_gex_value")
    mng_strike, mng_val = r.get("max_neg_gex_strike"), r.get("max_neg_gex_value")
    stat_row("Max +GEX", f"{format_strike(mpg_strike)}  {_fmt_m(mpg_val)}" if mpg_strike is not None else "N/A", _GREEN, y); y += STAT_ROW_H
    stat_row("Max -GEX", f"{format_strike(mng_strike)}  {_fmt_m(mng_val)}" if mng_strike is not None else "N/A", _RED, y); y += STAT_ROW_H

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
    txt(MARGIN + usable_w / 2, cur + KEY_H / 2,
        "GEX = gamma exposure (+dampens/-amplifies moves) \u00b7 VEX = vanna exposure \u00b7 "
        "Walls = support/resistance \u00b7 Gamma Flip = regime-change level",
        fs=6.6, color=_TXT_DIM, ha="center", font=_HEADER_FONT)
    cur += KEY_H + GAP

    txt(MARGIN + usable_w, cur + FOOTER_H / 2, "BlueMoonTrades", fs=9.5,
        color=_TXT_DIM, bold=True, ha="right", font=_HEADER_FONT)
    cur += FOOTER_H

    fig.set_size_inches(FIG_W, cur + MARGIN)
    plt.savefig(out_path, facecolor=_BG, bbox_inches="tight", pad_inches=0.2)
    plt.close(fig)
    print(f"[GEX] Single-ticker card ({ticker}) -> {out_path}")


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
    title bar and every prior version of this function).

    NOTE (2026-08-07): this rendered card is UNCHANGED by the
    actionability fix -- only generate_gex_watch_lines()'s prompt (used
    for the Discord EMBED's "What To Watch" field, not this image) was
    modified. This card remains the full-detail reference view; the
    embed text above it is what got the actionability rewrite."""
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
    # FONT BUGFIX (2026-08-08): the original "Liberation Mono"/"Liberation
    # Sans" font names are NOT installed in the Railway container --
    # confirmed via a real production run that logged hundreds of
    # "findfont: Font family ... not found" warnings per card (one per
    # text element drawn), which also flooded/truncated the Railway log
    # viewer before the actual run-summary/posting-confirmation lines
    # could be seen. Cards still rendered (matplotlib was silently
    # falling back to DejaVu Sans regardless), so this was a log-noise
    # and diagnostics problem, not a rendering failure -- but explicitly
    # requesting DejaVu Sans / DejaVu Sans Mono (matplotlib's own bundled
    # default fonts, guaranteed present with zero extra system
    # dependencies) means the SAME font actually gets used with no
    # fallback warning at all, keeping production logs clean and
    # actually readable.
    _DATA_FONT   = "DejaVu Sans Mono"
    _HEADER_FONT = "DejaVu Sans"

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
                txt(mid_x, bar_y - 0.16, f"P/C \u2248 {format_strike(put_wall)}", fs=6.6, color=_TXT, bold=True, ha="center", font=_HEADER_FONT)
            else:
                txt(pw_x, bar_y - 0.16, f"P {format_strike(put_wall)}", fs=6.6, color=_RED, bold=True, ha="center", font=_HEADER_FONT)
                txt(cw_x, bar_y - 0.16, f"C {format_strike(call_wall)}", fs=6.6, color=_GREEN, bold=True, ha="center", font=_HEADER_FONT)

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
        stat_row("Call Wall", f"{format_strike(call_wall)}  ({cw_pct:+.1f}%)" if call_wall else "N/A", _GREEN, y); y += STAT_ROW_H
        stat_row("Put Wall", f"{format_strike(put_wall)}  ({pw_pct:+.1f}%)" if put_wall else "N/A", _RED, y); y += STAT_ROW_H
        mpg_strike, mpg_val = r.get("max_pos_gex_strike"), r.get("max_pos_gex_value")
        mng_strike, mng_val = r.get("max_neg_gex_strike"), r.get("max_neg_gex_value")
        stat_row("Max +GEX", f"{format_strike(mpg_strike)}  {_fmt_m(mpg_val)}" if mpg_strike is not None else "N/A", _GREEN, y); y += STAT_ROW_H
        stat_row("Max -GEX", f"{format_strike(mng_strike)}  {_fmt_m(mng_val)}" if mng_strike is not None else "N/A", _RED, y); y += STAT_ROW_H

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