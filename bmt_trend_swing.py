"""
BMT TREND SWING - long-hold options ideas for traders who can't watch the screen.

  Trend Breakout  (validated in research)  -> full idea card + contract + exit plan
  Reversal Watch  (NOT validated)          -> watch-list only, no contracts, graded forward

Rules come from research_swing2/3/4.py (5y replay, 1,558 names, same-date baseline,
in-sample vs out-of-sample halves). Tight stops were tested and destroyed returns, so the
plan is: buy a deep-ITM call with 6-9 months left, risk = premium only, exit on a weekly
close under the 50-day average or when ~90 days remain.

Usage (webhook is read from the environment, never from a file):
    python bmt_trend_swing.py --dry-run
    $env:TREND_WEBHOOK="https://discord.com/api/webhooks/..."; python bmt_trend_swing.py --post
"""
from __future__ import annotations

import os, sys, json, math, time, argparse, logging
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

import swing_chart
import swing_db

log = logging.getLogger("trend")
OUT = os.path.join(os.environ.get("SWING_OUT", "./swing_out"), "trend")
BG, FG, MUTED = "#0B0F14", "#E8EEF4", "#7C8B9A"
UP, DOWN, GREEN, RED, AMBER = "#22B8CF", "#E64980", "#2FBF71", "#FF5C7A", "#E8B339"
CHANNEL = os.environ.get("TREND_CHANNEL", "test")      # label stored with every idea: 'test' until a real webhook is wired
MAX_SHADOW = 3                                          # bearish setups paper-logged (never posted) per day


def download(tickers: list[str], period="2y") -> dict[str, pd.DataFrame]:
    """Batched OHLCV download. Returns {ticker: df}. Tolerates partial failure."""
    import yfinance as yf
    out, batch = {}, 100
    for i in range(0, len(tickers), batch):
        chunk = tickers[i:i + batch]
        try:
            raw = yf.download(chunk, period=period, interval="1d", auto_adjust=False, progress=False, group_by="ticker", threads=True)
        except Exception as exc:
            log.warning("batch %d failed: %s", i, exc)
            continue
        for t in chunk:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.dropna()
                if len(df) >= 120:
                    out[t] = df
            except Exception:
                pass
    return out

MAX_IDEAS = 5
FRESH_BARS = 1          # signal must be from the latest session (3-day-old signals were already extended)
MAX_EXT = 0.03          # skip if already >3% above the breakout level
MAX_IV_HV = 2.0         # sanity cap only (study found no IV/HV edge below this)
MIN_DVOL = 5e7          # stock-level pre-filter ($ volume); universe.csv is already options-liquid, the real gate is the option chain
MIN_OPT_OI = 300        # open interest at the chosen contract
MAX_OPT_SPREAD = 0.10   # bid/ask spread as a fraction of mid
DTE_MIN, DTE_MAX = 85, 125   # ~100 DTE, held ~4 weeks (research_swing12)
DELTA_LO, DELTA_HI = 0.42, 0.62
HOLD_DAYS = 28          # planned sale ~20 trading days after entry
REPEAT_DAYS = 21        # don't re-post the same ticker within this many days
# Research evidence (model options, 0.70-delta, 9mo, held 6mo; see research_swing3.py)
EVID = {"n": 120, "win": 51, "median": 3, "mean": 28, "wipe": 7, "base_win": 35, "base_median": -32, "base_mean": -4}   # research_swing12, 20-day hold, sector-confirmed


# --------------------------------------------------------------------------- features
def feats(df: pd.DataFrame, spy_c: pd.Series) -> pd.DataFrame:
    c = df["Adj Close"].astype(float)
    adj = c / df["Close"]
    h, l, o, v = df["High"] * adj, df["Low"] * adj, df["Open"] * adj, df["Volume"].astype(float)
    f = pd.DataFrame(index=df.index)
    f["c"], f["o"], f["h"], f["l"], f["v"] = c, o, h, l, v
    f["sma50"], f["sma150"], f["sma200"] = (c.rolling(n).mean() for n in (50, 150, 200))
    f["hi252"] = c.rolling(252, min_periods=200).max()
    f["dd"] = c / f["hi252"] - 1
    f["mindd80"] = f["dd"].rolling(80).min()
    f["low20"] = l.rolling(20).min()
    f["low_prev60"] = l.shift(20).rolling(60).min()
    rs = c / spy_c.reindex(c.index).ffill()
    f["rs"] = rs
    f["rs30"], f["rs63"] = rs / rs.shift(30) - 1, rs / rs.shift(63) - 1
    vavg = v.rolling(50).mean()
    f["volx"] = v / vavg
    f["slope50"] = f["sma50"] / f["sma50"].shift(10) - 1
    f["hi20_prev"] = h.shift(1).rolling(20).max()
    f["lo20_prev"] = l.shift(1).rolling(20).min()
    f["lo252"] = c.rolling(252, min_periods=200).min()
    f["up_from_low"] = c / f["lo252"] - 1
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    f["atr"] = tr.rolling(14).mean()
    f["dvol"] = (c * v).rolling(50).mean()
    f["ret63"] = c / c.shift(63) - 1
    f["hv60"] = np.log(c).diff().rolling(60).std() * math.sqrt(252)
    f["below200_80"] = (c < f["sma200"]).rolling(80).sum()
    return f


def trend_breakout_mask(f: pd.DataFrame) -> pd.Series:      # == "B3" in research
    c = f["c"]
    liq = (f["dvol"] > 2e7) & (c > 8)
    return (liq & (c > f["sma50"]) & (f["sma50"] > f["sma150"]) & (f["sma150"] > f["sma200"]) & (f["slope50"] > 0)
            & (c > f["hi20_prev"]) & (f["volx"] >= 1.3) & (f["rs63"] > 0.05) & (f["dd"] > -0.15)).fillna(False)


def trend_breakdown_mask(f: pd.DataFrame) -> pd.Series:    # mirror of B3 for put ideas (NOT separately backtested)
    c = f["c"]
    liq = (f["dvol"] > 2e7) & (c > 8)
    return (liq & (c < f["sma50"]) & (f["sma50"] < f["sma150"]) & (f["sma150"] < f["sma200"]) & (f["slope50"] < 0)
            & (c < f["lo20_prev"]) & (f["volx"] >= 1.3) & (f["rs63"] < -0.05) & (f["up_from_low"] < 0.15)).fillna(False)


def reversal_mask(f: pd.DataFrame) -> pd.Series:            # == "R5" in research
    c = f["c"]
    liq = (f["dvol"] > 2e7) & (c > 8)
    return (liq & (f["mindd80"] < -0.35) & (c > f["sma50"]) & (f["slope50"] > 0) & (c > f["hi20_prev"])
            & (f["volx"] >= 1.5) & (f["low20"] > f["low_prev60"]) & (f["rs63"] > 0)).fillna(False)


# --------------------------------------------------------------------------- market regime
def market_state(spy: pd.DataFrame) -> dict:
    c = spy["Adj Close"].astype(float)
    s50, s200 = c.rolling(50).mean(), c.rolling(200).mean()
    dd = float(c.iloc[-1] / c.rolling(252).max().iloc[-1] - 1)
    ok = bool(c.iloc[-1] > s200.iloc[-1])
    up50 = bool(c.iloc[-1] > s50.iloc[-1] and s50.iloc[-1] > s50.iloc[-11])
    return {"price": float(c.iloc[-1]), "s50": float(s50.iloc[-1]), "s200": float(s200.iloc[-1]),
            "above200": ok, "uptrend50": up50, "dd": dd,
            "pct_vs_200": float(c.iloc[-1] / s200.iloc[-1] - 1)}


# --------------------------------------------------------------------------- contract
def _num(x, d=0.0):
    try:
        v = float(x)
        return d if math.isnan(v) else v
    except (TypeError, ValueError):
        return d


ALPACA_KEY = lambda: os.environ.get("ALPACA_API_KEY_ID", "")
ALPACA_SEC = lambda: os.environ.get("ALPACA_API_SECRET_KEY", "")


def _alpaca_get(url: str, params: dict) -> dict | None:
    h = {"APCA-API-KEY-ID": ALPACA_KEY(), "APCA-API-SECRET-KEY": ALPACA_SEC()}
    for a in range(4):
        try:
            r = requests.get(url, headers=h, params=params, timeout=30)
        except requests.RequestException:
            time.sleep(2)
            continue
        if r.status_code == 429:
            time.sleep(3 * (a + 1))
            continue
        return r.json() if r.status_code == 200 else None
    return None


def pick_long_call(ticker: str, spot: float, side: str = "call") -> tuple[dict | None, str]:
    """~100-DTE call, delta ~0.5 (at the money to ~3% out), from Alpaca's live chain: real bid/ask, IV, delta and open interest.
    The 5-year-style study on real option prices (research_swing11/12) found this structure held for ~4 weeks held up best;
    45-DTE options had 40-50% wipe-outs and 170-DTE did no better."""
    if not ALPACA_KEY():
        return None, "no_alpaca_keys"
    today = date.today()
    lo, hi = today + timedelta(days=DTE_MIN), today + timedelta(days=DTE_MAX)
    k_lo, k_hi = (spot * 0.98, spot * 1.06) if side == "call" else (spot * 0.94, spot * 1.02)
    flt = {"type": side, "expiration_date_gte": str(lo), "expiration_date_lte": str(hi),
           "strike_price_gte": f"{k_lo:.2f}", "strike_price_lte": f"{k_hi:.2f}"}
    con = _alpaca_get("https://paper-api.alpaca.markets/v2/options/contracts",
                      dict(flt, underlying_symbols=ticker, status="active", limit=1000))
    oi = {c["symbol"]: int(c["open_interest"]) for c in (con or {}).get("option_contracts", []) if c.get("open_interest")}
    snap = _alpaca_get(f"https://data.alpaca.markets/v1beta1/options/snapshots/{ticker}",
                       dict(flt, feed="indicative", limit=1000))
    if snap is None:
        return None, "rate_limited"
    snaps = snap.get("snapshots", {})
    if not snaps:
        return None, "no_long_dated_expiry"
    rows = []
    for sym, s in snaps.items():
        q, g = s.get("latestQuote") or {}, s.get("greeks") or {}
        bid, ask, iv, dl = _num(q.get("bp")), _num(q.get("ap")), _num(s.get("impliedVolatility")), abs(_num(g.get("delta"), np.nan))
        if bid <= 0 or ask <= 0 or ask < bid or not (iv > 0.05) or np.isnan(dl):
            continue
        tail = sym[len(ticker):]                       # YYMMDD C/P strike*1000
        exp_d = datetime.strptime(tail[:6], "%y%m%d").date()
        K = int(tail[7:]) / 1000
        mid = (bid + ask) / 2
        rows.append(dict(sym=sym, strike=K, expiry=exp_d.isoformat(), dte=(exp_d - today).days, delta=dl, mid=round(mid, 2),
                         bid=bid, ask=ask, spread=(ask - bid) / mid, iv=iv, oi=oi.get(sym, 0), stale=False))
    if not rows:
        return None, "no_priced_strikes"
    near_atm = (lambda k: k >= spot * 0.99) if side == "call" else (lambda k: k <= spot * 1.01)
    ok = [x for x in rows if DELTA_LO <= x["delta"] <= DELTA_HI and near_atm(x["strike"])
          and x["oi"] >= MIN_OPT_OI and x["spread"] <= MAX_OPT_SPREAD]
    if not ok:
        return None, "too_illiquid"
    best = min(ok, key=lambda x: (abs(x["dte"] - 100) / 30 + abs(x["delta"] - 0.52) * 4 - min(x["oi"], 3000) / 6000))
    best["intrinsic"] = max(spot - best["strike"], 0) if side == "call" else max(best["strike"] - spot, 0)
    best["extrinsic"] = best["mid"] - best["intrinsic"]
    best["breakeven"] = best["strike"] + best["mid"] if side == "call" else best["strike"] - best["mid"]
    return best, "ok"


def earnings_note(ticker: str) -> str | None:
    import yfinance as yf
    try:
        cal = yf.Ticker(ticker).calendar
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if ed:
            d = ed[0] if isinstance(ed, (list, tuple)) else ed
            d = pd.Timestamp(d).date()
            if d >= date.today():
                return d.strftime("%b %d")
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- chart
def chart(idea: dict, path: str, reversal: bool = False) -> str:
    f = idea["f"].tail(190)
    n = len(f)
    x = np.arange(n)
    fig = plt.figure(figsize=(12, 8.2), dpi=110)
    fig.patch.set_facecolor(BG)
    axp = fig.add_axes([0.055, 0.30, 0.82, 0.58])
    axv = fig.add_axes([0.055, 0.10, 0.82, 0.16], sharex=axp)
    for ax in (axp, axv):
        ax.set_facecolor(BG)
        for s in ax.spines.values():
            s.set_color("#1E2933")
        ax.tick_params(colors=MUTED, labelsize=9)
        ax.grid(color="#161E27", lw=0.7)
        ax.yaxis.tick_right()
    adj = f["c"] / f["c"]  # prices are already split/div adjusted in f
    for i in range(n):
        o, h, l, c = f["o"].iat[i], f["h"].iat[i], f["l"].iat[i], f["c"].iat[i]
        col = UP if c >= o else DOWN
        axp.plot([i, i], [l, h], color=col, lw=0.8, zorder=2)
        axp.add_patch(Rectangle((i - 0.32, min(o, c)), 0.64, max(abs(c - o), 1e-6), facecolor=col, edgecolor=col, lw=0.5, zorder=3))
    for col_name, col, lab in [("sma50", "#E8EEF4", "50d"), ("sma150", AMBER, "150d"), ("sma200", "#E64980", "200d")]:
        axp.plot(x, f[col_name].values, color=col, lw=1.3, alpha=0.9, zorder=4)
        off = {"sma150": 1, "sma200": -1}.get(col_name, 0) * float(f["h"].max() - f["l"].min()) * 0.012
        axp.text(n + 1, f[col_name].iat[-1] + off, lab, color=col, fontsize=9, va="center", parse_math=False)
    b = idea["bo_idx"]                       # breakout bar index within tail window
    lvl = idea["breakout_level"]
    put = idea.get("side") == "put"
    axp.plot([max(0, b - 20), n + 3], [lvl, lvl], color=RED if put else GREEN, lw=1.1, ls="--", zorder=5)
    axp.text(n + 3, lvl, f"20d low {lvl:,.2f}" if put else f"20d high {lvl:,.2f}", color=RED if put else GREEN, fontsize=9, fontweight="bold", va="top" if put else "bottom", parse_math=False)
    txt_bg = dict(facecolor=BG, edgecolor="none", alpha=0.85, pad=2.5)
    span = float(f["h"].max() - f["l"].min())
    axp.annotate(idea.get("label", "breakout on volume"), xy=(b, f["c"].iat[b]), xytext=(max(3, b - 38), f["c"].iat[b] + span * (-0.14 if put else 0.14)),
                 color=FG, fontsize=10, parse_math=False, bbox=txt_bg, arrowprops=dict(arrowstyle="->", color=FG, lw=0.9), zorder=7)
    if reversal:
        pk = int(np.argmax(f["c"].values[:b])) if b > 0 else 0
        axp.annotate(f"washout {idea['mindd']*100:.0f}% off the high", xy=(int(np.argmin(f["c"].values[:b])), float(f["c"].values[:b].min())),
                     xytext=(2, float(f["c"].values[:b].min()) - span * 0.1), color=MUTED, fontsize=10, parse_math=False, bbox=txt_bg,
                     arrowprops=dict(arrowstyle="->", color=MUTED, lw=0.9), zorder=7)
    axp.set_xlim(-1, n + 22)
    axp.set_ylim(float(f["l"].min()) * 0.97, float(f["h"].max()) * 1.04)
    vols = f["v"].values
    axv.bar(x, vols, color=[UP if f["c"].iat[i] >= f["o"].iat[i] else DOWN for i in range(n)], width=0.68, alpha=0.85)
    axv.plot(x, pd.Series(vols).rolling(50, min_periods=20).mean().values, color="#8FA3B5", lw=1.0)
    axv.annotate(f"{idea['volx']:.1f}x average volume", xy=(b, vols[b]), xytext=(max(3, b - 45), float(np.nanmax(vols)) * 0.8),
                 color=FG, fontsize=10, parse_math=False, bbox=txt_bg, arrowprops=dict(arrowstyle="->", color=FG, lw=0.9))
    axv.set_yticks([])
    dates = pd.to_datetime(f.index)
    ticks = list(range(0, n, max(1, n // 9)))
    axv.set_xticks(ticks)
    axv.set_xticklabels([dates[i].strftime("%b %d") for i in ticks], fontsize=9)
    axp.tick_params(labelbottom=False)
    kind = "REVERSAL WATCH" if reversal else ("TREND BREAKDOWN" if put else "TREND BREAKOUT")
    fig.text(0.055, 0.952, idea["ticker"], color=FG, fontsize=26, fontweight="bold", parse_math=False)
    fig.text(0.055, 0.922, f"{kind}  ·  {idea['industry'].lower()}", color=MUTED, fontsize=12, parse_math=False)
    for i, (k, v) in enumerate([("vs SPY 63d", f"{idea['rs63']*100:+.0f}%"), (("from 52w low", f"+{idea['up_low']*100:.0f}%") if put else ("from 52w high", f"{idea['dd']*100:.0f}%")),
                                ("ATR", f"{idea['atr_pct']:.1f}%")]):
        fig.text(0.80, 0.955 - i * 0.024, k, color=MUTED, fontsize=10, parse_math=False)
        fig.text(0.945, 0.955 - i * 0.024, v, color=FG, fontsize=10, ha="right", parse_math=False)
    foot = ("watch only — no contract suggested" if reversal else
            f"exit idea: weekly close {'over' if put else 'under'} 50d ({idea['f']['sma50'].iat[-1]:,.2f})  ·  risk = premium only")
    fig.text(0.055, 0.045, foot, color=FG, fontsize=11, parse_math=False)
    fig.text(0.055, 0.018, "blue moon trades  ·  not financial advice", color=MUTED, fontsize=10, parse_math=False)
    fig.savefig(path, facecolor=BG)
    plt.close(fig)
    return path


# --------------------------------------------------------------------------- scan
def last_fresh(mask: pd.Series) -> int | None:
    idx = np.flatnonzero(mask.values[-FRESH_BARS:])
    return None if len(idx) == 0 else len(mask) - FRESH_BARS + int(idx[-1])


def load_universe() -> pd.DataFrame:
    """Curated options-liquid list (universe.csv). Only these names are ever scanned. Index products (SPX/XSP) have no stock bars."""
    u = pd.read_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "universe.csv"))
    u = u[u["tier"] != "IDX"]
    return pd.DataFrame({"ticker": u["ticker"].str.strip().str.upper(), "industry": u["category"].fillna("n/a").str.replace("_", " "),
                         "tier": u["tier"]})


# stock category -> sector ETF used as CONTEXT only (ETFs are never suggested as ideas)
SECTOR_ETF = {"semis": "SMH", "semis adr": "SMH", "ai infra": "SMH", "software": "IGV", "ai software": "IGV", "cybersecurity": "IGV",
              "banks": "KRE", "fintech": "XLF", "energy": "XLE", "energy adr": "XLE", "biotech": "XBI", "pharma": "XBI",
              "miners": "GDX", "mining": "GDX", "mining adr": "GDX", "consumer": "XLP", "industrials": "XLI", "defense tech": "XLI",
              "utility": "XLU", "chemicals": "XLB", "tech": "XLK", "mega tech": "XLK", "consumer tech": "XLK", "crypto beta": "IBIT"}
INDEX_ETFS = ("SPY", "QQQ", "IWM", "DIA")


def etf_state(df: pd.DataFrame, spy_c: pd.Series) -> dict | None:
    try:
        c = df["Adj Close"].astype(float)
        s50, s200 = c.rolling(50).mean(), c.rolling(200).mean()
        rs = c / spy_c.reindex(c.index).ffill()
        return {"above50": bool(c.iat[-1] > s50.iat[-1]), "above200": bool(c.iat[-1] > s200.iat[-1]) if not np.isnan(s200.iat[-1]) else None,
                "rising50": bool(s50.iat[-1] > s50.iat[-11]), "rs63": float(rs.iat[-1] / rs.iat[-64] - 1), "ret20": float(c.iat[-1] / c.iat[-21] - 1)}
    except Exception:
        return None


def recent_posts() -> set:
    """Local fallback when the database is unavailable (Railway's disk is ephemeral, so the DB is the real source)."""
    path = os.path.join(OUT, "forward_log.csv")
    if not os.path.exists(path):
        return set()
    try:
        d = pd.read_csv(path, parse_dates=["date"])
        d = d[d["kind"].astype(str).str.startswith("trend")]
        return set(d[d["date"] >= pd.Timestamp(date.today() - timedelta(days=REPEAT_DAYS))]["ticker"])
    except Exception:
        return set()


def continuation_mask(f: pd.DataFrame) -> pd.Series:        # == "L4" in research_swing12: strong run, still near highs, volume calm
    c = f["c"]
    liq = (f["dvol"] > MIN_DVOL) & (c > 5)
    stack = (c > f["sma50"]) & (f["sma50"] > f["sma150"]) & (f["sma150"] > f["sma200"]) & (f["slope50"] > 0)
    return (liq & stack & (f["ret63"] > 0.25) & (c >= f["h"].rolling(20).max() * 0.97) & (f["volx"] < 1.0) & (f["rs63"] > 0.10)).fillna(False)


def scan(fast: bool, limit: int = 0, seen: set | None = None):
    uni = load_universe()
    if limit:
        uni = uni.head(limit)
    ind = dict(zip(uni["ticker"], uni["industry"]))
    is_etf = {t for t, tr in zip(uni["ticker"], uni["tier"]) if str(tr).startswith("ETF")}
    bars = download(list(ind) + ["SPY"], period="2y")
    spy = bars.pop("SPY")
    spy_c = spy["Adj Close"].astype(float)
    mk = market_state(spy)
    mk["etf"] = {t: st for t in sorted(is_etf) if t in bars and (st := etf_state(bars[t], spy_c))}
    mk["etf"]["SPY"] = etf_state(spy, spy_c)
    mk["etf_px"] = {t: bars[t]["Adj Close"].astype(float) for t in is_etf if t in bars}      # for the chart's sector line
    mk["spy_px"] = spy_c
    seen = recent_posts() if seen is None else seen
    longs, revs, shadows, cands, cut = [], [], [], [], {"repeat": 0, "sector": 0}
    for t, df in bars.items():
        if t in is_etf:         # ETFs inform the backdrop only; never an idea
            continue
        try:
            f = feats(df, spy_c)
            if len(f) < 260 or f["c"].isna().iloc[-1]:
                continue
        except Exception:
            continue
        for kind, mask in (("long", trend_breakout_mask(f)), ("cont", continuation_mask(f)), ("short", trend_breakdown_mask(f)), ("rev", reversal_mask(f))):
            i = last_fresh(mask)
            if i is None:
                continue
            if kind == "cont" and i != len(f) - 1:
                continue
            row = f.iloc[i]
            lvl = float(f["h"].iloc[-20:].max()) if kind == "cont" else (float(row["lo20_prev"]) if kind == "short" else float(row["hi20_prev"]))
            ext = (1 - f["c"].iat[-1] / lvl) if kind == "short" else f["c"].iat[-1] / lvl - 1
            if kind not in ("cont",) and ext > MAX_EXT:
                continue
            if kind != "rev" and f["dvol"].iat[-1] < MIN_DVOL:
                continue
            sec_etf = SECTOR_ETF.get(str(ind.get(t, "")).lower())
            st = mk["etf"].get(sec_etf) if sec_etf else None
            if kind != "rev":
                if t in seen:
                    cut["repeat"] += 1
                    cands.append(dict(ticker=t, side="put" if kind == "short" else "call", setup=kind, outcome="skipped_repeat", spot=float(f["c"].iat[-1])))
                    continue
                if kind != "short" and st and not (st["above50"] and st["rs63"] > 0):      # group must confirm (research_swing12: 51% vs 29% profitable)
                    cut["sector"] += 1
                    cands.append(dict(ticker=t, side="call", setup=kind, outcome="skipped_sector",
                                      detail=f"{sec_etf} above50={st['above50']} rs63={st['rs63']:.3f}", spot=float(f["c"].iat[-1])))
                    continue
            bo_idx = (len(f) - 1 - (len(f) - min(len(f), 190))) if kind == "cont" else i - (len(f) - min(len(f), 190))
            rec = dict(ticker=t, kind=kind, industry=ind.get(t, "n/a"), f=f, bo_idx=bo_idx, breakout_level=lvl, side="put" if kind == "short" else "call",
                       label={"cont": "tight, near highs", "short": "breakdown on volume"}.get(kind, "breakout on volume"), sector_etf=sec_etf,
                       sector_rs=(st["rs63"] if st else None), volx=float(f["volx"].iat[-1] if kind == "cont" else row["volx"]),
                       rs63=float(f["rs63"].iat[-1] if kind == "cont" else row["rs63"]), rs30=float(row["rs30"]), dd=float(f["dd"].iat[-1]),
                       mindd=float(row["mindd80"]), ret63=float(f["ret63"].iat[-1]), spot=float(f["c"].iat[-1]),
                       atr_pct=float(f["atr"].iat[-1] / f["c"].iat[-1] * 100), days_ago=len(f) - 1 - i,
                       ext=float(ext), up_low=float(f["up_from_low"].iat[-1]), hv=float(f["hv60"].iat[-1]), dvol=float(f["dvol"].iat[-1]))
            (revs if kind == "rev" else shadows if kind == "short" else longs).append(rec)
    for r in longs:     # rank: leadership, thrust, closeness to highs, and the group leading too
        r["score"] = (min(abs(r["rs63"]), 0.25) * 100 + min(r["volx"], 4) * 4 - max(r["ext"], 0) * 50 + (r["dd"] + 0.15) * 40
                      + (min(max(r["sector_rs"], 0), 0.15) * 60 if r["sector_rs"] is not None else -3))
    for r in shadows:
        r["score"] = min(abs(r["rs63"]), 0.25) * 100 + min(r["volx"], 4) * 4 - max(r["ext"], 0) * 50
    longs.sort(key=lambda r: -r["score"])
    shadows.sort(key=lambda r: -r["score"])
    revs.sort(key=lambda r: -r["rs63"])
    log.info("filtered out: %s", cut)
    return mk, longs, revs, len([t for t in bars if t not in is_etf]), shadows, cands, cut


# --------------------------------------------------------------------------- discord
def fmt_money(x): return f"${x:,.2f}"


def reasons(r: dict, mk: dict) -> list[str]:
    f = r["f"]
    if r["side"] == "put":
        return [f"**Downtrend is fully established** — price {fmt_money(r['spot'])} below the 50d ({fmt_money(f['sma50'].iat[-1])}), "
                f"150d ({fmt_money(f['sma150'].iat[-1])}) and 200d ({fmt_money(f['sma200'].iat[-1])}), stacked in order and falling",
                f"**Fresh breakdown** — closed below the 20-day low {fmt_money(r['breakout_level'])} "
                f"{'today' if r['days_ago'] == 0 else str(r['days_ago']) + ' session(s) ago'} on **{r['volx']:.1f}x** normal volume "
                f"({r['ext']*100:.1f}% past the level, not chased)",
                f"**Lagging the market** — {r['rs63']*100:+.0f}% relative strength vs SPY over 63 days; "
                f"only {r['up_low']*100:.0f}% above its 52-week low",
                f"**Market backdrop** — SPY is {mk['pct_vs_200']*100:+.1f}% vs its 200d average"
                + (" (a rising market works against puts, so size smaller)" if mk["above200"] else "")]
    out = [f"**Trend is fully established** — price {fmt_money(r['spot'])} above the 50d ({fmt_money(f['sma50'].iat[-1])}), "
           f"150d ({fmt_money(f['sma150'].iat[-1])}) and 200d ({fmt_money(f['sma200'].iat[-1])}), stacked in order and rising"]
    if r.get("kind") == "cont":
        out.append(f"**Strong run, still holding it** — {r['ret63']*100:+.0f}% in 63 days, within {abs(r['ext'])*100:.1f}% of its 20-day high "
                   f"({fmt_money(r['breakout_level'])}) on **quiet volume ({r['volx']:.1f}x)** — no heavy selling into the highs")
    else:
        out.append(f"**Fresh breakout** — closed above the 20-day high {fmt_money(r['breakout_level'])} "
                   f"{'today' if r['days_ago'] == 0 else str(r['days_ago']) + ' session(s) ago'} on **{r['volx']:.1f}x** normal volume "
                   f"({r['ext']*100:+.1f}% past the level, not chased)")
    out.append(f"**Leading the market** — {r['rs63']*100:+.0f}% relative strength vs SPY over 63 days; "
               f"only {abs(r['dd'])*100:.0f}% off its 52-week high")
    out.append(f"**Market backdrop {'supports it' if mk['uptrend50'] else 'is mixed'}** — SPY is {mk['pct_vs_200']*100:+.1f}% vs its 200d average"
               + (" and its 50d is rising" if mk["uptrend50"] else " but it is below/under its 50d trend — less tailwind"))
    return out


def tape_line(mk: dict) -> str:
    e = mk.get("etf", {})
    def mark(t):
        st = e.get(t)
        return f"{t} {'▲' if st and st['above50'] and st['rising50'] else ('▼' if st and not st['above50'] else '▬')}" if st else None
    idx = " · ".join(x for x in (mark(t) for t in INDEX_ETFS) if x)
    secs = [st for t, st in e.items() if t in set(SECTOR_ETF.values()) and st]
    br = f"{sum(1 for st in secs if st['above50'])}/{len(secs)} sector ETFs above their 50d" if secs else ""
    return idx + (f" · {br}" if br else "")


def sector_note(r: dict, mk: dict) -> str | None:
    t = SECTOR_ETF.get(str(r["industry"]).lower())
    st = mk.get("etf", {}).get(t) if t else None
    if not st:
        return None
    lead = "leading" if st["rs63"] > 0.02 else ("lagging" if st["rs63"] < -0.02 else "in line with")
    return (f"{t} is {'above' if st['above50'] else 'below'} its 50d ({'rising' if st['rising50'] else 'flat/falling'}), "
            f"{lead} SPY over 63d ({st['rs63']*100:+.0f}%)")


def earnings_date(ticker: str) -> date | None:
    import yfinance as yf
    try:
        cal = yf.Ticker(ticker).calendar
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if ed:
            d = pd.Timestamp(ed[0] if isinstance(ed, (list, tuple)) else ed).date()
            return d if d >= date.today() else None
    except Exception:
        pass
    return None


def idea_embed(r: dict, c: dict, mk: dict, earn: date | None) -> dict:
    cont = r["kind"] == "cont"
    exit_on = date.today() + timedelta(days=HOLD_DAYS)
    cost = c["mid"] * 100
    fields = [{"name": "Contract (call, ~100 days, near the money)",
               "value": (f"**{r['ticker']} {c['expiry']} ${c['strike']:g} Call** · {c['dte']} DTE · Δ≈{c['delta']:.2f}\n"
                         f"~**${c['mid']:.2f}** (${cost:,.0f} per contract) · {c['iv']*100:.0f}% IV · bid {c['bid']:.2f} / ask {c['ask']:.2f}\n"
                         f"Strike {(c['strike']/r['spot']-1)*100:+.1f}% vs the current price · break-even at expiry **{fmt_money(c['breakeven'])}** "
                         f"({(c['breakeven']/r['spot']-1)*100:+.1f}%)\n"
                         f"OI {c['oi']:,} · spread {c['spread']*100:.1f}%"),
               "inline": False},
              {"name": "Plan (about a 4-week swing)",
               "value": ("• **Max loss = what you pay.** No stop to babysit\n"
                         f"• **Sell by ~{exit_on:%b %d}** (about 4 weeks). The research held 4 weeks; the edge faded by 8 weeks\n"
                         f"• **Exit sooner** if the stock closes under its 50d ({fmt_money(r['f']['sma50'].iat[-1])})\n"
                         "• Size small: ≤1% of the account per idea; it can go to zero"),
               "inline": False}]
    risk = []
    if earn:
        inside = earn <= exit_on
        risk.append(f"Earnings **{earn:%b %d}**" + (" falls inside the hold — expect a gap either way; consider waiting or sizing down" if inside else ""))
    if r["atr_pct"] > 5:
        risk.append(f"volatile name (ATR {r['atr_pct']:.1f}%/day)")
    if not mk["uptrend50"]:
        risk.append("market 50d not rising yet")
    if r["sector_etf"] is None:
        risk.append("no sector ETF to confirm the group")
    sn = sector_note(r, mk)
    fields.append({"name": "Market & sector backdrop", "value": tape_line(mk) + (f"\nSector: {sn}" if sn else ""), "inline": False})
    fields.append({"name": "Watch-outs", "value": "; ".join(risk) if risk else "None flagged beyond normal market risk.", "inline": False})
    fields.append({"name": "How this setup has done (real option prices)",
                   "value": (f"{EVID['n']} similar past setups with the sector confirming, ~100-day near-the-money calls held 4 weeks (Alpaca option prices, "
                             f"Mar 2024–2026, 4% cost each way): **{EVID['win']}% profitable**, median **{EVID['median']:+d}%**, average **{EVID['mean']:+d}%**, "
                             f"**{EVID['wipe']}%** lost over 80%. A random stock on the same dates: {EVID['base_win']}% / {EVID['base_median']:+d}% / {EVID['base_mean']:+d}%. "
                             "The average is carried by a minority of big winners; sample is small (~2.5 years). Not a promise."),
                   "inline": False})
    return {"title": f"{r['ticker']} — " + ("Trend Continuation" if cont else "Trend Breakout") + " (calls)", "description": "\n".join("• " + x for x in reasons(r, mk)),
            "color": 0x22B8CF, "fields": fields, "footer": {"text": f"{r['industry']} · research model, not a promise · not financial advice"}}


def header_embed(mk: dict, n_cards: int, scanned: int) -> dict:
    d = (f"🟢 SPY is {mk['pct_vs_200']*100:+.1f}% vs its 200-day average ({mk['dd']*100:.1f}% from its 52-week high). "
         f"Scanned **{scanned:,}** options-liquid names; **{n_cards}** passed every check today.\n\n"
         "**How to use this:** ~4-week swing ideas on trends that are still working. Each card lists the exact exit. "
         "Calls can only lose what you pay, and near-the-money calls can still lose most of it.")
    return {"title": "BMT Trend Swing", "description": d, "color": 0x2FBF71, "footer": {"text": f"{date.today():%A, %b %d %Y}"}}


def reversal_embed(revs: list[dict]) -> dict:
    rows = [f"`{r['ticker']:<6}` {r['mindd']*100:>4.0f}% low → now {r['dd']*100:>4.0f}% · vol {r['volx']:.1f}x · "
            f"RS {r['rs63']*100:+.0f}% · {r['industry'][:24]}" for r in revs[:10]] or ["No stocks are breaking out of a deep washout right now."]
    d = ("Stocks that fell 35%+ and have just broken out of the bottom on volume above a rising 50-day.\n\n" + "\n".join(rows) + "\n\n"
         "**Experimental — no contracts suggested.** Every name here is logged and graded forward.")
    return {"title": "Reversal Watch (experimental)", "description": d, "color": 0xE8B339, "footer": {"text": "watch-list only · not a recommendation"}}


def send(webhook: str, embed: dict, image: str | None = None):
    e = dict(embed)
    if image:
        e["image"] = {"url": f"attachment://{os.path.basename(image)}"}
    payload = {"embeds": [e]}
    if image:
        with open(image, "rb") as fh:
            r = requests.post(webhook, data={"payload_json": json.dumps(payload)},
                              files={"file": (os.path.basename(image), fh, "image/png")}, timeout=30)
    else:
        r = requests.post(webhook, json=payload, timeout=30)
    r.raise_for_status()
    time.sleep(1.2)


def idea_row(r: dict, c: dict, mk: dict, earn, run_id, run_date, status: str, embed: dict | None) -> dict:
    """Everything we know about one idea at the moment it is published/paper-logged."""
    f = r["f"]
    sell_by = date.today() + timedelta(days=HOLD_DAYS)
    st = mk.get("etf", {}).get(r.get("sector_etf") or "") or {}
    return dict(
        run_id=run_id, run_date=run_date, signal_date=pd.Timestamp(f.index[-1]).date(), ticker=r["ticker"], side=r["side"],
        setup={"long": "breakout", "cont": "continuation", "short": "breakdown"}.get(r["kind"], r["kind"]), status=status, channel=CHANNEL,
        industry=r["industry"], sector_etf=r.get("sector_etf"), sector_rs63=r.get("sector_rs"), sector_above50=st.get("above50"),
        spot=r["spot"], days_ago=r["days_ago"], breakout_level=r["breakout_level"], ext=r["ext"], volx=r["volx"], rs63=r["rs63"], rs30=r["rs30"],
        ret63=r["ret63"], dd_from_high=r["dd"], atr_pct=r["atr_pct"], hv60=r["hv"], dollar_vol=r["dvol"], score=r.get("score"),
        sma50=float(f["sma50"].iat[-1]), sma150=float(f["sma150"].iat[-1]), sma200=float(f["sma200"].iat[-1]), hi252=float(f["hi252"].iat[-1]),
        contract_symbol=c["sym"], expiry=date.fromisoformat(c["expiry"]), strike=c["strike"], dte=c["dte"], delta=c["delta"], bid=c["bid"], ask=c["ask"],
        mid=c["mid"], iv=c["iv"], iv_hv_ratio=c["iv"] / max(r["hv"], 0.05), open_interest=c["oi"], spread_pct=c["spread"] * 100, breakeven=c["breakeven"],
        earnings_date=earn, earnings_in_hold=bool(earn and earn <= sell_by), sell_by=sell_by, hold_days=HOLD_DAYS,
        exit_rule="stock closes under its 50d" if r["side"] == "call" else "stock closes over its 50d",
        market={k: mk[k] for k in ("price", "s50", "s200", "above200", "uptrend50", "dd", "pct_vs_200")} | {"etf": mk.get("etf")},
        evidence=EVID, embed=embed,
        params=dict(FRESH_BARS=FRESH_BARS, MAX_EXT=MAX_EXT, MAX_IV_HV=MAX_IV_HV, MIN_DVOL=MIN_DVOL, MIN_OPT_OI=MIN_OPT_OI, MAX_OPT_SPREAD=MAX_OPT_SPREAD,
                    DTE=[DTE_MIN, DTE_MAX], DELTA=[DELTA_LO, DELTA_HI], HOLD_DAYS=HOLD_DAYS, REPEAT_DAYS=REPEAT_DAYS, code="bmt_trend_swing v2"),
    )


def log_forward(picked):
    """Local CSV fallback log (used only when there is no database)."""
    path = os.path.join(OUT, "forward_log.csv")
    rows = [dict(date=date.today(), kind="trend_" + r["kind"], ticker=r["ticker"], spot=round(r["spot"], 2), expiry=c["expiry"],
                 strike=c["strike"], mid=c["mid"], delta=round(c["delta"], 2), iv=round(c["iv"], 3), exit_ref_50d=round(float(r["f"]["sma50"].iat[-1]), 2))
            for r, c in picked]
    if rows:
        pd.DataFrame(rows).to_csv(path, mode="a", header=not os.path.exists(path), index=False)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="show ideas even if SPY < 200d (preview only)")
    ap.add_argument("--watchlists", action="store_true", help="also post the experimental reversal watch-list")
    ap.add_argument("--no-db", action="store_true", help="do not read or write the database")
    args = ap.parse_args()
    if not (args.dry_run or args.post):
        ap.error("pass --dry-run or --post")
    webhook = os.environ.get("TREND_WEBHOOK", "")
    if args.post and not webhook:
        ap.error("set TREND_WEBHOOK in the environment")
    if os.environ.get("RUN_MODE") == "once" and not os.environ.get("FORCE_RUN"):
        # Railway cron is UTC-only: it fires at 22:00 and 23:00 UTC; only the run that lands at 18:xx New York time proceeds (DST-proof)
        from zoneinfo import ZoneInfo
        from market_hours import is_trading_day
        now = datetime.now(ZoneInfo("America/New_York"))
        if now.hour != 18 or not is_trading_day(now.date()):
            print(f"ET {now:%H:%M} on {now.date()} - not 18:xx on a trading day, exiting.")
            return
    os.makedirs(OUT, exist_ok=True)

    conn = None if args.no_db else swing_db.connect()
    if conn and not swing_db.ensure_tables(conn):
        conn = None
    run_date = date.today()
    run_id = swing_db.start_run(conn, run_date, CHANNEL, dict(post=args.post)) if (conn and args.post) else None
    seen = (swing_db.recent_tickers(conn, REPEAT_DAYS) if conn else None)

    mk, longs, revs, scanned, shadows, cands, cut = scan(False, args.limit, seen)
    log.info("market above200=%s uptrend50=%s | call signals %d | bearish (shadow) %d | reversal %d", mk["above200"], mk["uptrend50"], len(longs), len(shadows), len(revs))
    gated = (not mk["above200"]) and not args.force
    cards, picked = [], []
    for r in ([] if gated else longs[:MAX_IDEAS * 3]):
        if len(cards) >= MAX_IDEAS:
            break
        cd = dict(ticker=r["ticker"], side="call", setup=r["kind"], score=r.get("score"), spot=r["spot"])
        c, why = pick_long_call(r["ticker"], r["spot"])
        if not c:                      # options traders: no usable contract, no card
            log.info("%s skipped (%s)", r["ticker"], why)
            cands.append(dict(cd, outcome=f"skipped_{why}"))
            continue
        if c["iv"] / max(r["hv"], 0.05) > MAX_IV_HV:
            log.info("%s skipped (IV %.0f%% vs realized %.0f%%)", r["ticker"], c["iv"] * 100, r["hv"] * 100)
            cands.append(dict(cd, outcome="skipped_iv", detail=f"iv {c['iv']:.2f} hv {r['hv']:.2f}"))
            continue
        earn = earnings_date(r["ticker"])
        if earn and (earn - date.today()).days <= 7:
            log.info("%s skipped (earnings %s within a week)", r["ticker"], earn)
            cands.append(dict(cd, outcome="skipped_earnings", detail=str(earn)))
            continue
        img_path = os.path.join(OUT, f"{r['ticker']}_{date.today()}.png")
        try:
            img = swing_chart.render(r, c, mk, earn, img_path)
        except Exception:
            log.exception("%s: swing chart failed, using the simple chart", r["ticker"])
            img = chart(r, img_path)
        cards.append((idea_embed(r, c, mk, earn), img))
        picked.append((r, c, earn))
        cands.append(dict(cd, outcome="posted" if args.post else "dry_run_card"))
        log.info("%s ready (%s)", r["ticker"], r["kind"])

    # bearish setups: researched as NOT working, so they are never posted -- paper-logged so the forward data can settle it
    shadow_rows = []
    if True:
        for r in shadows[:MAX_SHADOW * 3]:
            if len(shadow_rows) >= MAX_SHADOW:
                break
            c, why = pick_long_call(r["ticker"], r["spot"], "put")
            cd = dict(ticker=r["ticker"], side="put", setup="short", score=r.get("score"), spot=r["spot"])
            if not c:
                cands.append(dict(cd, outcome=f"skipped_{why}"))
                continue
            shadow_rows.append((r, c, earnings_date(r["ticker"])))
            cands.append(dict(cd, outcome="shadow"))

    head = header_embed(mk, len(cards), scanned)
    with open(os.path.join(OUT, f"preview_{date.today()}.json"), "w") as fh:
        json.dump({"header": head, "cards": [c for c, _ in cards]}, fh, indent=1, default=str)

    posted = 0
    ids = {}
    if conn and args.post:
        for (r, c, earn), (emb, _) in zip(picked, cards):
            ids[r["ticker"]] = swing_db.log_idea(conn, idea_row(r, c, mk, earn, run_id, run_date, "posted", emb))
        for r, c, earn in shadow_rows:
            swing_db.log_idea(conn, idea_row(r, c, mk, earn, run_id, run_date, "shadow", None))
    if not cards:
        log.info("nothing fits today - posting nothing%s", " (SPY below 200d: calls gated)" if gated else "")
    elif args.post:
        if not conn:
            log_forward([(r, c) for r, c, _ in picked])
        send(webhook, head)
        for (r, c, _), (e, img) in zip(picked, cards):
            ok = False
            try:
                send(webhook, e, img)
                ok, posted = True, posted + 1
            except Exception:
                log.exception("%s: discord post failed", r["ticker"])
            if conn and ids.get(r["ticker"]):
                swing_db.mark_posted(conn, ids[r["ticker"]], ok)
        log.info("posted header + %d idea cards", posted)
    else:
        log.info("dry run: %d cards, charts in %s", len(cards), OUT)
    if args.watchlists and args.post and revs:
        send(webhook, reversal_embed(revs), chart(revs[0], os.path.join(OUT, f"REV_{revs[0]['ticker']}_{date.today()}.png"), reversal=True))

    if conn:
        if args.post:
            for cd in cands:
                swing_db.log_candidate(conn, run_id, run_date, cd["ticker"], cd.get("side"), cd.get("setup"), cd["outcome"], cd.get("detail"), cd.get("score"), cd.get("spot"))
        if run_id:
            swing_db.finish_run(conn, run_id, scanned=scanned, calls=len(longs), shadow=len(shadows), posted=posted, gated=gated, filtered=cut,
                                market={k: mk[k] for k in ("price", "above200", "uptrend50", "dd", "pct_vs_200")} | {"etf": mk.get("etf")},
                                note=None if cards else "nothing fit")
        conn.close()


if __name__ == "__main__":
    main()
