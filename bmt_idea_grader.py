"""
bmt_idea_grader.py -- honest nightly grader for idea_log (published AND shadow/suppressed ideas).

Replaces "did the stock touch the strike" with what a trader following the card would have got:
  ENTRY  first 5-min bar from the publish date whose range overlaps [entry_low, entry_high]
         (fill = open if inside the zone, else nearest zone edge); no fill -> graded filled=false
  EXIT   first of stop / target1 / expiry close, in time order (stop wins ties inside a bar)
  R      direction-adjusted (exit - entry) / |entry - stop|  (underlying)
  OPTION buy the exact contract at the entry timestamp, sell at the exit timestamp (Alpaca 5-min
         option bars, bar close, HAIRCUT each way). Missing option bars -> opt_ret NULL.
Only ideas whose expiry date has passed are graded. Results go to idea_grades (new table).
READ-ONLY on market data; writes only idea_grades. Never posts anywhere.

    python bmt_idea_grader.py                 # grade everything resolved and not yet graded
    python bmt_idea_grader.py --limit 20
Env: DATABASE_URL, ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY, RUN_MODE=once (Railway cron).
Railway cron (UTC-only): "30 21,22 * * 1-5"; proceeds only when America/New_York hour == 17.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import bmt_idea_gate as G

ET = ZoneInfo("America/New_York")
KEY, SEC = os.environ.get("ALPACA_API_KEY_ID", ""), os.environ.get("ALPACA_API_SECRET_KEY", "")
HAIRCUT = 0.03
H = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}


def _bars(url, params):
    rows, p = [], dict(params)
    while True:
        for a in range(4):
            r = requests.get(url, headers=H, params=p, timeout=40)
            time.sleep(0.3)
            if r.status_code == 429:
                time.sleep(4 * (a + 1)); continue
            break
        if r.status_code != 200:
            break
        j = r.json()
        for v in j.get("bars", {}).values():
            rows += v
        if not j.get("next_page_token"):
            break
        p["page_token"] = j["next_page_token"]
    if not rows:
        return pd.DataFrame()
    d = pd.DataFrame(rows)
    d.index = pd.to_datetime(d["t"]).dt.tz_convert("America/New_York")
    return d[(d.index.time >= datetime(2000, 1, 1, 9, 30).time()) & (d.index.time <= datetime(2000, 1, 1, 16, 0).time())]


def stock_5m(t, start, end):
    s = datetime(start.year, start.month, start.day, tzinfo=ET).astimezone(timezone.utc)
    e = (datetime(end.year, end.month, end.day, tzinfo=ET) + timedelta(days=1)).astimezone(timezone.utc)
    return _bars("https://data.alpaca.markets/v2/stocks/bars",
                 {"symbols": t, "timeframe": "5Min", "limit": 10000, "feed": "iex", "adjustment": "split",
                  "start": s.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": e.strftime("%Y-%m-%dT%H:%M:%SZ")})


def option_5m(sym, start_ts, end_ts):
    return _bars("https://data.alpaca.markets/v1beta1/options/bars",
                 {"symbols": sym, "timeframe": "5Min", "limit": 10000,
                  "start": start_ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "end": (end_ts + pd.Timedelta(days=1)).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")})


def occ(t, exp, call, k):
    return f"{t.upper()}{exp:%y%m%d}{'C' if call else 'P'}{int(round(float(k) * 1000)):08d}"


def grade(row):
    (iid, pdate, t, direction, exp, k, el, eh, stop, t1) = row
    call = str(direction).upper().startswith("C")
    el, eh, stop, t1, k = float(el), float(eh), float(stop), float(t1), float(k)
    b = stock_5m(t, pdate, exp)
    if b.empty:
        return None
    ei = ep = None
    for i, (ts, x) in enumerate(b.iterrows()):
        if x["l"] <= eh and x["h"] >= el:
            ei, ep = i, (x["o"] if el <= x["o"] <= eh else (eh if x["o"] > eh else el))
            break
    if ei is None:
        return {"filled": False}
    risk = abs(ep - stop)
    if risk <= 0:
        return {"filled": False}
    sgn = 1 if call else -1
    xts, xpx, why, mae = b.index[-1], float(b["c"].iloc[-1]), "time", 0.0
    for ts, x in b.iloc[ei:].iterrows():
        hs = x["l"] <= stop if call else x["h"] >= stop
        ht = x["h"] >= t1 if call else x["l"] <= t1
        mae = max(mae, ((ep - x["l"]) if call else (x["h"] - ep)) / risk)
        if hs:
            xts, xpx, why = ts, stop, "stop"; break
        if ht:
            xts, xpx, why = ts, t1, "t1"; break
    res = {"filled": True, "entry_ts": b.index[ei], "entry_px": float(ep), "exit_ts": xts, "exit_px": float(xpx),
           "why": why, "r": sgn * (xpx - ep) / risk, "mae_r": mae}
    try:
        sym = occ(t, exp, call, k)
        ob = option_5m(sym, res["entry_ts"], res["exit_ts"])
        res["opt_symbol"] = sym
        if not ob.empty:
            e = ob[ob.index >= res["entry_ts"]]
            x = ob[ob.index <= res["exit_ts"]]
            if not e.empty and not x.empty:
                buy, sell = float(e["c"].iloc[0]) * (1 + HAIRCUT), float(x["c"].iloc[-1]) * (1 - HAIRCUT)
                res.update(opt_buy=buy, opt_sell=sell, opt_ret=sell / buy - 1)
    except Exception as e:
        print(f"  [GRADER] option leg failed for {t}: {e}")
    return res


def run(limit=None):
    if not (KEY and SEC):
        print("ALPACA keys missing -- nothing graded."); return
    conn = G._connect()
    G.ensure_tables(conn)
    today = datetime.now(ET).date()
    q = """SELECT l.id, l.publish_date, l.ticker, l.direction, l.expiry_date, l.strike, l.entry_low, l.entry_high,
                  l.stop, l.target1
           FROM idea_log l LEFT JOIN idea_grades g ON g.idea_id = l.id
           WHERE g.idea_id IS NULL AND l.expiry_date < :today AND l.strike IS NOT NULL AND l.stop IS NOT NULL
           ORDER BY l.publish_date"""
    rows = conn.run(q, today=today)
    if limit:
        rows = rows[:limit]
    print(f"{len(rows)} idea(s) to grade")
    done = 0
    for row in rows:
        try:
            r = grade(row)
        except Exception as e:
            print(f"  [GRADER WARN] {row[2]} {row[1]}: {e}")
            continue
        if r is None:
            continue
        conn.run("""INSERT INTO idea_grades (idea_id, filled, entry_ts, entry_px, exit_ts, exit_px, why, r, mae_r,
                        opt_symbol, opt_buy, opt_sell, opt_ret)
                    VALUES (:id,:f,:ets,:epx,:xts,:xpx,:why,:r,:mae,:sym,:ob,:os,:oret)
                    ON CONFLICT (idea_id) DO NOTHING""",
                 id=row[0], f=r["filled"], ets=r.get("entry_ts"), epx=r.get("entry_px"), xts=r.get("exit_ts"),
                 xpx=r.get("exit_px"), why=r.get("why"), r=r.get("r"), mae=r.get("mae_r"), sym=r.get("opt_symbol"),
                 ob=r.get("opt_buy"), os=r.get("opt_sell"), oret=r.get("opt_ret"))
        done += 1
    conn.close()
    print(f"graded {done}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    if os.environ.get("RUN_MODE") == "once" and not os.environ.get("FORCE_RUN"):
        now = datetime.now(ET)
        if now.hour != 17:
            print(f"ET hour is {now.hour}, not 17 -- exiting."); return
    try:
        run(a.limit)
    except Exception:
        traceback.print_exc()      # swallowed: exit 0 so a restart policy never re-runs the job


if __name__ == "__main__":
    main()
