"""
bmt_edge_report.py -- weekly PRIVATE edge report: are the nightly ideas / flow signals actually working?

Section 1  IDEA GRADES (idea_log + idea_grades): honest stop-first win rate, mean R and real
           option return by direction and by pattern, published vs shadow (suppressed) ideas,
           plus the gate's current decision per bucket.
Section 2  PRE-REGISTERED FLOW TESTS on flow_snapshot (fixed here, BEFORE the data exists, so
           nothing can be cherry-picked later). Outcome = enter next session open, exit close
           H sessions later, minus SPY over the same window, direction-adjusted by the signal.
           CIs are bootstrapped over DATES (the market moves every stock together) and widened
           with a Bonferroni correction for the number of tests below.
             T1 nightly-style: tot >= $50k and bias Bullish/Bearish (call_pct >55 / <45)
             T2 bearish-only version of T1 (hypothesis: bearish flow is ANTI-predictive)
             T3 short-dated (<=14 DTE) flow: short_call (or short_put) >= $50k and >= 2x the other side
             T4 sweep-heavy: sweep_prem/tot >= .5 and tot >= $100k, same direction as bias
           Baseline: every ticker-day with n_rows = 0 (what a random stock did).
           Needs >= MIN_DAYS distinct snapshot dates, otherwise says "insufficient data".
A test is only called a finding if its Bonferroni CI excludes zero in BOTH halves of the dates.

Posts to CALIBRATION_WEBHOOK_URL (private admin channel) if set; never to subscribers.
    python bmt_edge_report.py --no-post
Env: DATABASE_URL, ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY, CALIBRATION_WEBHOOK_URL (optional)
RUN_MODE=once: Railway cron "0 13,14 * * 6" (Sat), proceeds only when America/New_York hour == 9.
"""
from __future__ import annotations

import argparse
import math
import os
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

import bmt_idea_gate as G

ET = ZoneInfo("America/New_York")
MIN_DAYS = 40
HS = (5, 10)
N_TESTS = 4
H = {"APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY_ID", ""), "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET_KEY", "")}


def daily_bars(t, start, end):
    rows, p = [], {"symbols": t, "timeframe": "1Day", "start": start, "end": end, "limit": 10000,
                   "adjustment": "split", "feed": "iex"}
    while True:
        r = requests.get("https://data.alpaca.markets/v2/stocks/bars", headers=H, params=p, timeout=40)
        time.sleep(0.25)
        if r.status_code != 200:
            break
        j = r.json()
        rows += j.get("bars", {}).get(t, [])
        if not j.get("next_page_token"):
            break
        p["page_token"] = j["next_page_token"]
    if not rows:
        return None
    d = pd.DataFrame(rows)
    d.index = pd.to_datetime(d["t"]).dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()
    return d


def grades_section(conn):
    rows = conn.run("""
        SELECT l.direction, COALESCE(l.pattern,'(n/a)'), l.published, g.filled, g.why, g.r, g.opt_ret
        FROM idea_log l JOIN idea_grades g ON g.idea_id = l.id""")
    if not rows:
        return ["No graded ideas yet."]
    d = pd.DataFrame(rows, columns=["dir", "pattern", "published", "filled", "why", "r", "opt_ret"])
    d = d[d.filled]
    for c in ("r", "opt_ret"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    out = [f"IDEA GRADES (filled ideas: {len(d)}; shadow/suppressed: {int((~d.published).sum())})"]

    def line(name, g):
        o = g.opt_ret.dropna()
        return (f"  {name:<28} n={len(g):<4} t1-first {100*(g.why=='t1').mean():4.0f}%  meanR {g.r.mean():+.2f}  "
                f"opt mean {100*o.mean() if len(o) else float('nan'):+5.0f}%  opt median {100*o.median() if len(o) else float('nan'):+5.0f}% (n={len(o)})")
    out.append(line("ALL", d))
    for k, g in d.groupby("dir"):
        out.append(line(k, g))
    for (k, p), g in d.groupby(["dir", "pattern"]):
        if len(g) >= 5:
            out.append(line(f"{k} | {p}", g))
    sh = d[~d.published]
    if len(sh):
        out.append(line("SHADOW (suppressed)", sh))
    out.append("")
    out.append("GATE DECISIONS")
    try:
        for k, v in sorted(G.bucket_stats(conn).items()):
            out.append(f"  {k:<26} n={v['n']:<4} weeks={v['weeks']:<3} meanR {v['mean']:+.2f} upper80 {v['upper']:+.2f} "
                       f"-> {'SUPPRESS' if v['suppress'] else 'publish'}")
    except Exception as e:
        out.append(f"  (unavailable: {e})")
    return out


def boot_ci(g, col, alpha):
    byd = g.groupby("date")[col].mean().values
    if len(byd) < 8:
        return None
    rng = np.random.default_rng(0)
    m = [rng.choice(byd, len(byd)).mean() for _ in range(3000)]
    return np.percentile(m, [100 * alpha / 2, 100 * (1 - alpha / 2)])


def flow_section(conn):
    rows = conn.run("""SELECT snap_date, ticker, n_rows, tot, call_pct, bias, short_call, short_put, sweep_prem
                       FROM flow_snapshot""")
    if not rows:
        return ["FLOW TESTS: no snapshots yet."]
    f = pd.DataFrame(rows, columns=["date", "ticker", "n_rows", "tot", "call_pct", "bias", "short_call", "short_put", "sweep"])
    for c in ("tot", "call_pct", "short_call", "short_put", "sweep"):
        f[c] = pd.to_numeric(f[c], errors="coerce")
    f["date"] = pd.to_datetime(f["date"])
    ndays = f.date.nunique()
    if ndays < MIN_DAYS:
        return [f"FLOW TESTS: insufficient data ({ndays}/{MIN_DAYS} snapshot days). Logging continues."]
    start = (f.date.min() - timedelta(days=5)).strftime("%Y-%m-%d")
    end = (f.date.max() + timedelta(days=25)).strftime("%Y-%m-%d")
    spy = daily_bars("SPY", start, end)
    bars = {t: daily_bars(t, start, end) for t in f.ticker.unique()}
    recs = []
    for r in f.itertuples():
        b = bars.get(r.ticker)
        if b is None or spy is None or r.date not in b.index or r.date not in spy.index:
            continue
        i, j = b.index.get_loc(r.date), spy.index.get_loc(r.date)
        rec = r._asdict()
        for h in HS:
            if i + 1 + h < len(b) and j + 1 + h < len(spy):
                rec[f"x{h}"] = (b["c"].iloc[i + 1 + h] / b["o"].iloc[i + 1] - 1) - (spy["c"].iloc[j + 1 + h] / spy["o"].iloc[j + 1] - 1)
            else:
                rec[f"x{h}"] = np.nan
        recs.append(rec)
    d = pd.DataFrame(recs)
    d["sgn"] = np.where(d.bias == "Bullish", 1, np.where(d.bias == "Bearish", -1, 0))
    sig = {
        "T1 nightly-style": (d.tot >= 5e4) & (d.sgn != 0),
        "T2 bearish-only": (d.tot >= 5e4) & (d.sgn < 0),
        "T3 short-dated (<=14DTE)": ((d.short_call >= 5e4) & (d.short_call >= 2 * d.short_put)) | ((d.short_put >= 5e4) & (d.short_put >= 2 * d.short_call)),
        "T4 sweep-heavy": (d.tot >= 1e5) & (d.sweep / d.tot.replace(0, np.nan) >= .5) & (d.sgn != 0),
    }
    alpha = 0.05 / N_TESTS
    half = d.date.sort_values().iloc[len(d) // 2]
    out = [f"FLOW TESTS ({ndays} snapshot days, {len(d)} ticker-days; CI = {100*(1-alpha):.2f}% date-bootstrapped, Bonferroni x{N_TESTS})"]
    base = d[d.n_rows == 0]
    for h in HS:
        out.append(f"  baseline no-flow ticker-days H{h}: n={base[f'x{h}'].notna().sum()} mean excess {100*base[f'x{h}'].mean():+.2f}%")
    for name, m in sig.items():
        for h in HS:
            g = d[m].copy()
            if name.startswith("T3"):
                g["sgn"] = np.where(g.short_call >= g.short_put, 1, -1)
            g["dx"] = g.sgn * g[f"x{h}"]
            g = g.dropna(subset=["dx"])
            if len(g) < 30:
                out.append(f"  {name:<26} H{h:<2} n={len(g)} (too few)"); continue
            ci_all = boot_ci(g, "dx", alpha)
            halves = []
            for hh in (g[g.date < half], g[g.date >= half]):
                c = boot_ci(hh, "dx", alpha) if len(hh) >= 30 else None
                halves.append(c)
            consistent = all(c is not None and (c[0] > 0 or c[1] < 0) and np.sign(c[0] + c[1]) == np.sign(ci_all[0] + ci_all[1]) for c in halves) \
                if ci_all is not None else False
            finding = "FINDING" if (ci_all is not None and (ci_all[0] > 0 or ci_all[1] < 0) and consistent) else "no finding"
            out.append(f"  {name:<26} H{h:<2} n={len(g):<5} mean dir-excess {100*g.dx.mean():+.2f}%  hit {100*(g.dx>0).mean():.0f}%  "
                       f"CI [{100*ci_all[0]:+.2f},{100*ci_all[1]:+.2f}]  {finding}" if ci_all is not None else f"  {name} H{h}: not enough dates")
    return out


def build():
    conn = G._connect()
    G.ensure_tables(conn)
    lines = [f"BMT EDGE REPORT {datetime.now(ET):%Y-%m-%d}", ""]
    lines += grades_section(conn)
    lines.append("")
    try:
        lines += flow_section(conn)
    except Exception as e:
        lines.append(f"FLOW TESTS failed: {e}")
    conn.close()
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-post", action="store_true")
    a = ap.parse_args()
    if os.environ.get("RUN_MODE") == "once" and not os.environ.get("FORCE_RUN"):
        if datetime.now(ET).hour != 9:
            print("not 09 ET -- exiting."); return
    try:
        text = build()
        print(text)
        hook = os.environ.get("CALIBRATION_WEBHOOK_URL")
        if hook and not a.no_post:
            for i in range(0, len(text), 1800):
                requests.post(hook, json={"content": "```\n" + text[i:i + 1800] + "\n```", "username": "BMT Edge"}, timeout=20)
    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    main()
