"""
swing_db.py -- Postgres logging for the Trend Swing ideas (bmt_trend_swing.py) and their contract-level results (bmt_swing_grader.py).

Tables (all new, created on demand, safe to re-run):
  swing_scan_runs      one row per scan, including days where nothing fit (what the market/filters looked like)
  swing_candidates     every stock that triggered a setup and what happened to it (posted / shadow / skipped_<why>)
  swing_ideas          every idea that was posted (status 'posted') or paper-logged (status 'shadow': bearish setups we do NOT post),
                       with the signal numbers, market context, the exact contract and its quote, and the card that was sent
  swing_idea_marks     the suggested contract's daily prices from the post date until the sell-by date
  swing_idea_results   the verdict: what the SUGGESTED CONTRACT was worth on the sell-by date (not the stock price)

Fail-open: every function returns None / False on error (no DATABASE_URL, no network) so the Discord post is never blocked by logging.
Env: DATABASE_URL.
"""
from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
    load_dotenv()          # local runs; on Railway the variables are already in the environment
except Exception:
    pass

log = logging.getLogger("swing_db")


def connect():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return None
    try:
        import pg8000.native as pg
        p = urlparse(url)
        return pg.Connection(host=p.hostname, port=p.port or 5432, database=p.path.lstrip("/"), user=p.username, password=p.password, timeout=30)
    except Exception as e:
        log.warning("swing_db: cannot connect (%s)", e)
        return None


def ensure_tables(conn) -> bool:
    try:
        conn.run("""CREATE TABLE IF NOT EXISTS swing_scan_runs (
            id SERIAL PRIMARY KEY, run_at TIMESTAMPTZ NOT NULL DEFAULT now(), run_date DATE NOT NULL, channel TEXT,
            scanned INTEGER, call_signals INTEGER, shadow_signals INTEGER, cards_posted INTEGER, gated BOOLEAN,
            filtered JSONB, market JSONB, params JSONB, note TEXT)""")
        conn.run("""CREATE TABLE IF NOT EXISTS swing_candidates (
            id SERIAL PRIMARY KEY, run_id INTEGER REFERENCES swing_scan_runs(id), run_date DATE NOT NULL, ticker TEXT NOT NULL,
            side TEXT, setup TEXT, outcome TEXT NOT NULL, detail TEXT, score NUMERIC, spot NUMERIC)""")
        conn.run("""CREATE TABLE IF NOT EXISTS swing_ideas (
            id SERIAL PRIMARY KEY, logged_at TIMESTAMPTZ NOT NULL DEFAULT now(), run_id INTEGER REFERENCES swing_scan_runs(id),
            run_date DATE NOT NULL, signal_date DATE, ticker TEXT NOT NULL, side TEXT NOT NULL, setup TEXT NOT NULL,
            status TEXT NOT NULL, channel TEXT, industry TEXT, sector_etf TEXT, sector_rs63 NUMERIC, sector_above50 BOOLEAN,
            spot NUMERIC, days_ago INTEGER, breakout_level NUMERIC, ext NUMERIC, volx NUMERIC, rs63 NUMERIC, rs30 NUMERIC,
            ret63 NUMERIC, dd_from_high NUMERIC, atr_pct NUMERIC, hv60 NUMERIC, dollar_vol NUMERIC, score NUMERIC,
            sma50 NUMERIC, sma150 NUMERIC, sma200 NUMERIC, hi252 NUMERIC,
            contract_symbol TEXT, expiry DATE, strike NUMERIC, dte INTEGER, delta NUMERIC, bid NUMERIC, ask NUMERIC, mid NUMERIC,
            iv NUMERIC, iv_hv_ratio NUMERIC, open_interest INTEGER, spread_pct NUMERIC, breakeven NUMERIC,
            earnings_date DATE, earnings_in_hold BOOLEAN, sell_by DATE, hold_days INTEGER, exit_rule TEXT,
            market JSONB, evidence JSONB, params JSONB, embed JSONB, posted_ok BOOLEAN,
            UNIQUE (run_date, ticker, side))""")
        conn.run("""CREATE TABLE IF NOT EXISTS swing_idea_marks (
            idea_id INTEGER NOT NULL REFERENCES swing_ideas(id), mark_date DATE NOT NULL,
            opt_open NUMERIC, opt_high NUMERIC, opt_low NUMERIC, opt_close NUMERIC, opt_vwap NUMERIC, opt_volume INTEGER, opt_trades INTEGER,
            stock_close NUMERIC, stock_vs_50d NUMERIC, PRIMARY KEY (idea_id, mark_date))""")
        conn.run("""CREATE TABLE IF NOT EXISTS swing_idea_results (
            idea_id INTEGER PRIMARY KEY REFERENCES swing_ideas(id), graded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            status TEXT NOT NULL, sell_by DATE, exit_date DATE, exit_stale BOOLEAN,
            entry_ask NUMERIC, entry_mid NUMERIC, entry_next_vwap NUMERIC, exit_vwap NUMERIC, exit_close NUMERIC,
            ret_vs_ask NUMERIC, ret_vs_mid NUMERIC, ret_vs_next_vwap NUMERIC, ret_net NUMERIC, win BOOLEAN,
            max_close_gain NUMERIC, max_close_drawdown NUMERIC, stock_entry NUMERIC, stock_exit NUMERIC, stock_ret NUMERIC,
            plan_exit_date DATE, plan_exit_reason TEXT, plan_exit_vwap NUMERIC, plan_ret_vs_ask NUMERIC, note TEXT)""")
        conn.run("CREATE INDEX IF NOT EXISTS swing_ideas_ticker_idx ON swing_ideas (ticker, run_date)")
        return True
    except Exception as e:
        log.warning("swing_db: ensure_tables failed (%s)", e)
        return False


def _j(x):
    return json.dumps(x, default=str) if x is not None else None


def recent_tickers(conn, days: int, statuses=("posted", "shadow")) -> set:
    try:
        rows = conn.run("SELECT DISTINCT ticker FROM swing_ideas WHERE run_date >= CURRENT_DATE - CAST(:d AS INTEGER) AND status = ANY(:s)",
                        d=days, s=list(statuses))
        return {r[0] for r in rows}
    except Exception as e:
        log.warning("swing_db: recent_tickers failed (%s)", e)
        return set()


def start_run(conn, run_date, channel, params) -> int | None:
    try:
        return conn.run("INSERT INTO swing_scan_runs (run_date, channel, params) VALUES (:d, :c, CAST(:p AS jsonb)) RETURNING id",
                        d=run_date, c=channel, p=_j(params))[0][0]
    except Exception as e:
        log.warning("swing_db: start_run failed (%s)", e)
        return None


def finish_run(conn, run_id, **kw) -> None:
    try:
        conn.run("""UPDATE swing_scan_runs SET scanned=:scanned, call_signals=:calls, shadow_signals=:shadow, cards_posted=:posted, gated=:gated,
                    filtered=CAST(:filtered AS jsonb), market=CAST(:market AS jsonb), note=:note WHERE id=:id""",
                 id=run_id, scanned=kw.get("scanned"), calls=kw.get("calls"), shadow=kw.get("shadow"), posted=kw.get("posted"),
                 gated=kw.get("gated"), filtered=_j(kw.get("filtered")), market=_j(kw.get("market")), note=kw.get("note"))
    except Exception as e:
        log.warning("swing_db: finish_run failed (%s)", e)


def log_candidate(conn, run_id, run_date, ticker, side, setup, outcome, detail=None, score=None, spot=None) -> None:
    try:
        conn.run("""INSERT INTO swing_candidates (run_id, run_date, ticker, side, setup, outcome, detail, score, spot)
                    VALUES (:r, :d, :t, :side, :setup, :o, :det, :sc, :sp)""",
                 r=run_id, d=run_date, t=ticker, side=side, setup=setup, o=outcome, det=detail, sc=score, sp=spot)
    except Exception as e:
        log.warning("swing_db: log_candidate failed (%s)", e)


def log_idea(conn, row: dict) -> int | None:
    """Insert (or refresh) one idea. `row` keys match swing_ideas columns; JSON-ish values are dumped to jsonb."""
    cols = [k for k in row if k not in ("market", "evidence", "params", "embed")]
    js = [k for k in ("market", "evidence", "params", "embed") if k in row]
    names = cols + js
    vals = ", ".join([f":{k}" for k in cols] + [f"CAST(:{k} AS jsonb)" for k in js])
    upd = ", ".join(f"{k}=EXCLUDED.{k}" for k in names if k not in ("run_date", "ticker", "side"))
    params = {k: row[k] for k in cols}
    params.update({k: _j(row[k]) for k in js})
    try:
        return conn.run(f"INSERT INTO swing_ideas ({', '.join(names)}) VALUES ({vals}) "
                        f"ON CONFLICT (run_date, ticker, side) DO UPDATE SET {upd} RETURNING id", **params)[0][0]
    except Exception as e:
        log.warning("swing_db: log_idea failed (%s)", e)
        return None


def mark_posted(conn, idea_id, ok: bool) -> None:
    try:
        conn.run("UPDATE swing_ideas SET posted_ok=:ok, status=CASE WHEN :ok THEN status ELSE 'send_failed' END WHERE id=:id", ok=ok, id=idea_id)
    except Exception as e:
        log.warning("swing_db: mark_posted failed (%s)", e)
