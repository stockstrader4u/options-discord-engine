"""
bmt_swing_grader.py -- grades every Trend Swing idea by what the SUGGESTED CONTRACT was worth on the sell-by date.

Not "did the stock go up": the exact option contract from the card is looked up on Alpaca's daily option bars.
  daily    every run, each open idea's contract prices (open/high/low/close/vwap/volume) from the post date on are stored in swing_idea_marks
  final    on/after the sell-by date, one row in swing_idea_results:
             exit        that contract's VWAP on the sell-by session (last trading day on/before the sell-by date); `exit_stale` if it
                         did not trade that day and the last earlier bar was used
             ret_vs_ask  exit / the ASK on the card - 1   (a realistic fill when the card was posted; the headline number)
             ret_vs_mid  exit / the MID on the card - 1
             ret_vs_next_vwap  exit / next session's VWAP - 1  (if you bought the following day instead)
             ret_net     headline return after selling 3% under the VWAP (selling nearer the bid)
             plan_*      what following the card's own exit rule would have done (sell the first session the stock closed under
                         its 50d [over, for puts], otherwise at sell-by)
             max_close_gain / max_close_drawdown  best and worst daily close vs the ask while holding
Shadow (bearish) ideas are graded the same way, so the put question gets answered with forward data.
READ-ONLY on market data; writes only swing_idea_marks and swing_idea_results. Never posts anywhere.

    python bmt_swing_grader.py            # mark + grade everything open
    python bmt_swing_grader.py --report   # win rate / median / average by side, setup, status
Env: DATABASE_URL, ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY, RUN_MODE=once (Railway cron "30 21,22 * * 1-5", proceeds only at 17:xx New York, before the 18:00 scan).
"""
from __future__ import annotations

import argparse
import os
import time
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import swing_db
from market_hours import is_trading_day, most_recent_trading_day

ET = ZoneInfo("America/New_York")
H = {"APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY_ID", ""), "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET_KEY", "")}
SELL_HAIRCUT = 0.03


def option_daily(sym: str, start: date, end: date) -> pd.DataFrame:
    rows, p = [], {"symbols": sym, "timeframe": "1Day", "limit": 1000, "start": str(start), "end": str(end + timedelta(days=1))}
    while True:
        r = None
        for a in range(4):
            r = requests.get("https://data.alpaca.markets/v1beta1/options/bars", headers=H, params=p, timeout=40)
            time.sleep(0.3)
            if r.status_code == 429:
                time.sleep(4 * (a + 1))
                continue
            break
        if r is None or r.status_code != 200:
            break
        j = r.json()
        rows += j.get("bars", {}).get(sym, [])
        if not j.get("next_page_token"):
            break
        p["page_token"] = j["next_page_token"]
    if not rows:
        return pd.DataFrame()
    d = pd.DataFrame(rows)
    d.index = pd.to_datetime(d["t"]).dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()
    return d


def stock_closes(ticker: str, start: date, end: date) -> pd.Series:
    import yfinance as yf
    try:
        df = yf.download(ticker, start=str(start - timedelta(days=3)), end=str(end + timedelta(days=2)), interval="1d", auto_adjust=False, progress=False)
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        return df["Close"].astype(float)
    except Exception:
        return pd.Series(dtype=float)


def _f(x):
    return None if x is None or (isinstance(x, float) and pd.isna(x)) else float(x)


def grade_one(conn, row: dict, today: date) -> str:
    iid, sym, side, run_date, sell_by = row["id"], row["contract_symbol"], row["side"], row["run_date"], row["sell_by"]
    last_needed = min(today, sell_by + timedelta(days=1))
    ob = option_daily(sym, run_date, last_needed)
    sc = stock_closes(row["ticker"], run_date, last_needed)
    sma50 = float(row["sma50"])
    for d_, b in ob.iterrows():
        sclose = _f(sc.get(d_)) if len(sc) else None
        conn.run("""INSERT INTO swing_idea_marks (idea_id, mark_date, opt_open, opt_high, opt_low, opt_close, opt_vwap, opt_volume, opt_trades, stock_close, stock_vs_50d)
                    VALUES (:i, :d, :o, :h, :l, :c, :vw, :v, :n, :sc, :sv)
                    ON CONFLICT (idea_id, mark_date) DO UPDATE SET opt_open=EXCLUDED.opt_open, opt_high=EXCLUDED.opt_high, opt_low=EXCLUDED.opt_low,
                    opt_close=EXCLUDED.opt_close, opt_vwap=EXCLUDED.opt_vwap, opt_volume=EXCLUDED.opt_volume, opt_trades=EXCLUDED.opt_trades,
                    stock_close=EXCLUDED.stock_close, stock_vs_50d=EXCLUDED.stock_vs_50d""",
                 i=iid, d=d_.date(), o=_f(b["o"]), h=_f(b["h"]), l=_f(b["l"]), c=_f(b["c"]), vw=_f(b.get("vw")), v=int(b["v"]), n=int(b.get("n", 0)),
                 sc=sclose, sv=(sclose / sma50 - 1) if sclose else None)

    sell_day = most_recent_trading_day(sell_by)
    if not (today > sell_day or (today == sell_day and datetime.now(ET).hour >= 17)):
        return "marked"      # not due: grade only after the sell-by session has closed
    upto = ob[ob.index <= pd.Timestamp(sell_day)] if len(ob) else ob
    if upto.empty:
        if today > sell_by + timedelta(days=5):
            conn.run("""INSERT INTO swing_idea_results (idea_id, status, sell_by, note) VALUES (:i, 'no_option_data', :s, 'no daily bars for the contract')
                        ON CONFLICT (idea_id) DO NOTHING""", i=iid, s=sell_by)
            return "no_option_data"
        return "marked"
    px = lambda b: _f(b["vw"]) if _f(b.get("vw")) and b["v"] > 0 else _f(b["c"])
    ex = upto.iloc[-1]
    exit_vwap, exit_close = px(ex), _f(ex["c"])
    stale = upto.index[-1].date() < sell_day
    ask, mid = float(row["ask"]), float(row["mid"])
    nxt = ob[ob.index > pd.Timestamp(run_date)]
    nxt_vwap = px(nxt.iloc[0]) if len(nxt) else None
    closes = upto["c"].astype(float)
    # the card's own exit rule: first close beyond the 50d reference after the post date
    plan_date = plan_px = None
    reason = "sell_by"
    if len(sc):
        after = sc[(sc.index > pd.Timestamp(run_date)) & (sc.index <= pd.Timestamp(sell_day))]
        hit = after[after < sma50] if side == "call" else after[after > sma50]
        if len(hit):
            pdte = hit.index[0]
            pb = ob[ob.index <= pdte]
            if len(pb):
                plan_date, plan_px, reason = pdte.date(), px(pb.iloc[-1]), "closed_under_50d" if side == "call" else "closed_over_50d"
    if plan_px is None:
        plan_date, plan_px = upto.index[-1].date(), exit_vwap
    s_entry = _f(sc.get(pd.Timestamp(run_date))) if len(sc) else None
    s_exit = _f(sc.get(pd.Timestamp(sell_day))) if len(sc) else None
    conn.run("""INSERT INTO swing_idea_results (idea_id, status, sell_by, exit_date, exit_stale, entry_ask, entry_mid, entry_next_vwap, exit_vwap, exit_close,
                ret_vs_ask, ret_vs_mid, ret_vs_next_vwap, ret_net, win, max_close_gain, max_close_drawdown, stock_entry, stock_exit, stock_ret,
                plan_exit_date, plan_exit_reason, plan_exit_vwap, plan_ret_vs_ask, note)
                VALUES (:i, 'final', :sb, :xd, :st, :ask, :mid, :nv, :xv, :xc, :ra, :rm, :rn, :rnet, :win, :mg, :md, :se, :sx, :sr, :pd, :pr, :pp, :pra, :note)
                ON CONFLICT (idea_id) DO NOTHING""",
             i=iid, sb=sell_by, xd=upto.index[-1].date(), st=stale, ask=ask, mid=mid, nv=nxt_vwap, xv=exit_vwap, xc=exit_close,
             ra=exit_vwap / ask - 1, rm=exit_vwap / mid - 1, rn=(exit_vwap / nxt_vwap - 1) if nxt_vwap else None,
             rnet=exit_vwap * (1 - SELL_HAIRCUT) / ask - 1, win=exit_vwap / ask - 1 > 0,
             mg=float(closes.max() / ask - 1), md=float(closes.min() / ask - 1), se=s_entry, sx=s_exit,
             sr=(s_exit / s_entry - 1) if (s_entry and s_exit) else None, pd=plan_date, pr=reason, pp=plan_px, pra=plan_px / ask - 1,
             note="exit bar older than the sell-by session (contract did not trade that day)" if stale else None)
    return "final"


def run() -> None:
    conn = swing_db.connect()
    if not conn or not swing_db.ensure_tables(conn):
        print("no database - nothing graded.")
        return
    today = datetime.now(ET).date()
    rows = conn.run("""SELECT i.id, i.ticker, i.side, i.run_date, i.sell_by, i.contract_symbol, i.ask, i.mid, i.sma50
                       FROM swing_ideas i LEFT JOIN swing_idea_results r ON r.idea_id = i.id
                       WHERE r.idea_id IS NULL AND i.status IN ('posted', 'shadow') ORDER BY i.run_date""")
    cols = ["id", "ticker", "side", "run_date", "sell_by", "contract_symbol", "ask", "mid", "sma50"]
    tally: dict = {}
    for r in rows:
        d = dict(zip(cols, r))
        try:
            out = grade_one(conn, d, today)
        except Exception:
            traceback.print_exc()
            out = "error"
        tally[out] = tally.get(out, 0) + 1
        print(f"  {d['ticker']:6} {d['side']:4} {d['contract_symbol']}  sell-by {d['sell_by']}  -> {out}")
    print("graded:", tally or "nothing open")
    conn.close()


def report() -> None:
    conn = swing_db.connect()
    rows = conn.run("""SELECT i.side, i.setup, i.status, count(*), avg(r.ret_vs_ask), percentile_cont(0.5) WITHIN GROUP (ORDER BY r.ret_vs_ask),
                       avg((r.ret_vs_ask > 0)::int), avg(r.ret_net), avg(r.plan_ret_vs_ask), avg(r.stock_ret)
                       FROM swing_idea_results r JOIN swing_ideas i ON i.id = r.idea_id WHERE r.status = 'final'
                       GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""")
    print("side setup status n  mean_vs_ask median win%  mean_net  mean_plan  mean_stock_ret")
    for r in rows:
        print(r[0], r[1], r[2], r[3], *[None if x is None else round(float(x), 3) for x in r[4:]])
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        return report()
    if os.environ.get("RUN_MODE") == "once" and not os.environ.get("FORCE_RUN"):
        now = datetime.now(ET)
        if now.hour != 17 or not is_trading_day(now.date()):
            print(f"ET {now:%H:%M} - not 17:xx on a trading day, exiting.")
            return
    try:
        run()
    except Exception:
        traceback.print_exc()      # exit 0 so a restart policy never re-runs the job


if __name__ == "__main__":
    main()
