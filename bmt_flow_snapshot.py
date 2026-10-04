"""
bmt_flow_snapshot.py -- nightly LOG of the unusual-options-flow the nightly digest scans.

WHY: JarvisFlow only keeps trades on contracts that haven't expired, so short-dated (7-14 DTE)
flow history can't be recovered later. The nightly job also throws away every ticker that
doesn't qualify, so its signal can never be tested against a baseline. This job saves one row
per (session date, ticker) for the nightly's own universe -- including tickers with NO flow --
plus the top raw trades, so the nightly's signal (and any variant) can be tested honestly once
2-3 months have accumulated.

READ-ONLY toward Jarvis and subscribers: it never posts anywhere. It writes one new table,
flow_snapshot. It runs the same query the nightly uses (bought-side rows, one session).

    python bmt_flow_snapshot.py                  # snapshot the last completed session
    python bmt_flow_snapshot.py --date 10/02/2026 --limit 5
Env: DATABASE_URL, JARVIS_API_KEY, RUN_MODE=once (Railway cron)
Railway cron (UTC-only, no DST): "30 22,23 * * 1-5"; proceeds only when America/New_York
hour == 18 (18:30 ET, after the 18:00 nightly post), so exactly one firing runs all year.
Exceptions are logged and swallowed (exit 0) so a restart policy never re-runs a partial job.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
URL = "https://api.jarvisflow.io/.well-known/mcp"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
JARVIS_API_KEY = os.environ.get("JARVIS_API_KEY", "")
MIN_ROWS_JSON = 60


def _connect():
    import pg8000.native as pg
    p = urlparse(DATABASE_URL)
    return pg.Connection(host=p.hostname, port=p.port or 5432, database=p.path.lstrip("/"),
                         user=p.username, password=p.password)


def ensure_table(conn):
    conn.run("""
        CREATE TABLE IF NOT EXISTS flow_snapshot (
            snap_date DATE NOT NULL,
            ticker TEXT NOT NULL,
            n_rows INTEGER NOT NULL,
            call_bought NUMERIC, put_bought NUMERIC,
            tot NUMERIC, call_pct NUMERIC, bias TEXT,
            short_call NUMERIC, short_put NUMERIC,
            mid_call NUMERIC, mid_put NUMERIC,
            long_call NUMERIC, long_put NUMERIC,
            sweep_prem NUMERIC, max_trade NUMERIC,
            repeat_share NUMERIC, high_conv_share NUMERIC,
            spot NUMERIC,
            rows_json TEXT,
            logged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (snap_date, ticker)
        )""")


def universe():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bmt_nightly_setups.py"), encoding="utf-8").read()
    block = re.search(r"FULL_WATCHLIST = \[(.*?)\n\]", src, re.S).group(1)
    excl = re.search(r"EXCLUDE_FROM_CANDIDATES = \{(.*?)\}", src, re.S).group(1)
    bad = set(re.findall(r'"([^"]+)"', excl))
    return [t for t in re.findall(r'"([A-Z]+)"', block) if t not in bad]


def last_session(now=None):
    d = (now or datetime.now(ET))
    if d.hour < 16:                       # session not finished yet -> previous day
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.date()


def fetch(ticker, session):
    mdy = session.strftime("%m/%d/%Y")
    args = {"filter_by_Ticker": ticker, "filter_by_transaction_date_range_from": mdy,
            "filter_by_transaction_date_range_to": mdy}
    for a in range(4):
        try:
            r = requests.post(URL, headers={"Authorization": f"Bearer {JARVIS_API_KEY}", "Content-Type": "application/json"},
                              timeout=90, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                "params": {"name": "stock_ticker_unusual_options_data", "arguments": args}})
            if r.status_code == 429:
                time.sleep(4 * (a + 1)); continue
            if r.status_code != 200:
                time.sleep(2); continue
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    j = json.loads(line[5:].strip())
                    o = json.loads(j["result"]["content"][0]["text"])["toolResult"]
                    rows = o.get("optionsFlow", []) if isinstance(o, dict) else o
                    return [x for x in rows if str(x.get("ticker", "")).upper() == ticker.upper()]
            return []
        except Exception:
            time.sleep(2)
    return None            # None = fetch failed (do not record as "no flow")


def summarize(ticker, session, rows):
    def prem(x):
        try:
            return float(x.get("total_Option_Premium_For_Trade") or 0)
        except Exception:
            return 0.0

    def dte(x):
        try:
            return (datetime.fromisoformat(str(x["expriation_Date"])[:10]).date() - session).days
        except Exception:
            return None

    bought = [x for x in rows if x.get("implied_Bought_Or_Sold") == "BOUGHT"]
    nightly = [x for x in bought if str(x.get("moneyNess", "")).upper() in ("OTM", "ATM")]
    cb = sum(prem(x) for x in nightly if x.get("put_Or_Call") == "CALL")
    pb = sum(prem(x) for x in nightly if x.get("put_Or_Call") == "PUT")
    tot = cb + pb
    cpct = round(cb / tot * 100) if tot else None
    bias = None if not tot else ("Bullish" if cpct > 55 else "Bearish" if cpct < 45 else "Neutral")

    def bucket(lo, hi, cp):
        return sum(prem(x) for x in nightly if x.get("put_Or_Call") == cp and dte(x) is not None and lo <= dte(x) <= hi)

    top = sorted(rows, key=prem, reverse=True)[:MIN_ROWS_JSON]
    spot = next((x.get("spot_Price") for x in rows if x.get("spot_Price")), None)
    return dict(
        n_rows=len(rows), call_bought=cb, put_bought=pb, tot=tot, call_pct=cpct, bias=bias,
        short_call=bucket(0, 14, "CALL"), short_put=bucket(0, 14, "PUT"),
        mid_call=bucket(15, 45, "CALL"), mid_put=bucket(15, 45, "PUT"),
        long_call=bucket(46, 99999, "CALL"), long_put=bucket(46, 99999, "PUT"),
        sweep_prem=sum(prem(x) for x in nightly if x.get("sweep_Or_Block") == "SWEEP"),
        max_trade=max((prem(x) for x in rows), default=0.0),
        repeat_share=(sum(1 for x in rows if x.get("is_Repeating_Options_Trade_Today")) / len(rows)) if rows else 0.0,
        high_conv_share=(sum(1 for x in rows if x.get("interpreted_Conviction") in ("HIGH", "HIGHER", "HIGHEST")) / len(rows)) if rows else 0.0,
        spot=spot, rows_json=json.dumps(top, separators=(",", ":")))


def run(session, limit=None, threads=4):
    tickers = universe()
    if limit:
        tickers = tickers[:limit]
    print(f"snapshot {session} for {len(tickers)} tickers")
    with ThreadPoolExecutor(threads) as ex:
        res = list(ex.map(lambda t: (t, fetch(t, session)), tickers))
    ok = [(t, r) for t, r in res if r is not None]
    failed = len(res) - len(ok)
    if not any(r for _, r in ok):
        print(f"no flow for ANY ticker on {session} (holiday or outage) -- nothing saved"); return
    if failed > len(res) * 0.2:
        print(f"{failed}/{len(res)} fetches failed -- aborting to avoid saving a biased partial day"); return
    conn = _connect()
    ensure_table(conn)
    saved = 0
    for t, rows in ok:
        s = summarize(t, session, rows)
        conn.run("""INSERT INTO flow_snapshot (snap_date, ticker, n_rows, call_bought, put_bought, tot, call_pct, bias,
                        short_call, short_put, mid_call, mid_put, long_call, long_put, sweep_prem, max_trade,
                        repeat_share, high_conv_share, spot, rows_json)
                    VALUES (:d,:t,:n_rows,:call_bought,:put_bought,:tot,:call_pct,:bias,:short_call,:short_put,
                            :mid_call,:mid_put,:long_call,:long_put,:sweep_prem,:max_trade,:repeat_share,
                            :high_conv_share,:spot,:rows_json)
                    ON CONFLICT (snap_date, ticker) DO NOTHING""", d=session, t=t, **s)
        saved += 1
    conn.close()
    print(f"saved {saved} ticker rows ({failed} fetch failures skipped)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="MM/DD/YYYY session (default: last completed)")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    if not (DATABASE_URL and JARVIS_API_KEY):
        print("DATABASE_URL / JARVIS_API_KEY missing -- nothing to do."); return
    if os.environ.get("RUN_MODE") == "once" and not os.environ.get("FORCE_RUN"):
        now = datetime.now(ET)
        if now.hour != 18:
            print(f"ET hour is {now.hour}, not 18 -- exiting."); return
    try:
        session = datetime.strptime(a.date, "%m/%d/%Y").date() if a.date else last_session()
        run(session, a.limit)
    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    main()
