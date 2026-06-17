"""
db.py — Unified database adapter.

Supports both SQLite (local dev) and PostgreSQL (Railway/production).
Both main.py and mcp_server.py import from here — one place, no duplication.

Selection logic:
  - If DATABASE_URL env var is set → use PostgreSQL
  - Otherwise → use SQLite (DB_PATH, default alerts.db)

PostgreSQL notes:
  - Uses psycopg (v3)
  - All queries use %s placeholders (not ?)
  - datetime() SQL functions replaced with NOW()
  - Parameterized queries work identically

Usage:
    from db import get_connection, is_postgres, placeholder, init_all_tables

    with get_connection() as conn:
        conn.execute("SELECT 1")
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("options-db")

DATABASE_URL: str | None = os.getenv("DATABASE_URL")
DB_PATH: str = os.getenv("DB_PATH", "alerts.db")
_POSTGRES = bool(DATABASE_URL)


# ---------------------------------------------------------------------------
# Placeholder helper
# ---------------------------------------------------------------------------

def placeholder(n: int = 1) -> str:
    """
    Return the right parameter placeholder for the active DB.
    SQLite uses ?, PostgreSQL uses %s.

    For multi-param queries, pass n:
        placeholder(3) → "?, ?, ?" or "%s, %s, %s"
    """
    p = "%s" if _POSTGRES else "?"
    return ", ".join([p] * n)


def ph(n: int = 1) -> str:
    """Alias for placeholder()."""
    return placeholder(n)


def is_postgres() -> bool:
    return _POSTGRES


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _pg_conn():
    """Return a psycopg3 connection with dict row factory."""
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return conn


def get_connection():
    """
    Return a DB connection for the active backend.
    Use as a context manager:

        with get_connection() as conn:
            conn.execute(...)
    """
    if _POSTGRES:
        return _pg_conn()
    return _sqlite_conn()


@contextmanager
def db_cursor():
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SQL compatibility helpers
# ---------------------------------------------------------------------------

def now_sql() -> str:
    """SQL expression for current UTC timestamp."""
    return "NOW()" if _POSTGRES else "datetime('now')"


def interval_ago_sql(minutes: int) -> str:
    """SQL expression for N minutes ago."""
    if _POSTGRES:
        return f"NOW() - INTERVAL '{minutes} minutes'"
    return f"datetime('now', '-{minutes} minutes')"


def bool_val(v: bool) -> Any:
    """Convert Python bool to DB-appropriate value."""
    return v if _POSTGRES else int(v)


def utc_now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Schema — all tables
# ---------------------------------------------------------------------------

def _published_alerts_sql() -> str:
    if _POSTGRES:
        return """
        CREATE TABLE IF NOT EXISTS published_alerts (
            id          SERIAL PRIMARY KEY,
            alert_hash  TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            contract    TEXT NOT NULL,
            source      TEXT NOT NULL,
            score       INTEGER NOT NULL,
            published_at TIMESTAMP NOT NULL DEFAULT NOW()
        )"""
    return """
        CREATE TABLE IF NOT EXISTS published_alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_hash  TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            contract    TEXT NOT NULL,
            source      TEXT NOT NULL,
            score       INTEGER NOT NULL,
            published_at TEXT NOT NULL
        )"""


def _flow_events_sql() -> str:
    if _POSTGRES:
        return """
        CREATE TABLE IF NOT EXISTS flow_events (
            id              SERIAL PRIMARY KEY,
            alert_hash      TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            contract        TEXT NOT NULL,
            premium         INTEGER NOT NULL,
            sentiment       TEXT NOT NULL,
            source          TEXT NOT NULL,
            dte_bucket      TEXT NOT NULL,
            flow_type       TEXT,
            catalyst        TEXT,
            levels          TEXT,
            note            TEXT,
            dte             INTEGER,
            expiry_date     TEXT,
            strike          REAL,
            put_call        TEXT,
            spot_price      REAL,
            moneyness_pct   REAL,
            moneyness_tier  TEXT,
            premium_tier    TEXT,
            rvol            REAL,
            rvol_label      TEXT,
            is_lotto        BOOLEAN DEFAULT FALSE,
            is_near_expiry  BOOLEAN DEFAULT FALSE,
            structure_notes TEXT,
            score           INTEGER,
            score_reasons   TEXT,
            passed_threshold    BOOLEAN DEFAULT FALSE,
            passed_dedup        BOOLEAN DEFAULT FALSE,
            was_published       BOOLEAN DEFAULT FALSE,
            suppress_reason     TEXT,
            ingested_at     TIMESTAMP NOT NULL DEFAULT NOW()
        )"""
    return """
        CREATE TABLE IF NOT EXISTS flow_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_hash      TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            contract        TEXT NOT NULL,
            premium         INTEGER NOT NULL,
            sentiment       TEXT NOT NULL,
            source          TEXT NOT NULL,
            dte_bucket      TEXT NOT NULL,
            flow_type       TEXT,
            catalyst        TEXT,
            levels          TEXT,
            note            TEXT,
            dte             INTEGER,
            expiry_date     TEXT,
            strike          REAL,
            put_call        TEXT,
            spot_price      REAL,
            moneyness_pct   REAL,
            moneyness_tier  TEXT,
            premium_tier    TEXT,
            rvol            REAL,
            rvol_label      TEXT,
            is_lotto        INTEGER DEFAULT 0,
            is_near_expiry  INTEGER DEFAULT 0,
            structure_notes TEXT,
            score           INTEGER,
            score_reasons   TEXT,
            passed_threshold    INTEGER DEFAULT 0,
            passed_dedup        INTEGER DEFAULT 0,
            was_published       INTEGER DEFAULT 0,
            suppress_reason     TEXT,
            ingested_at     TEXT NOT NULL
        )"""


def _flow_classifications_sql() -> str:
    if _POSTGRES:
        return """
        CREATE TABLE IF NOT EXISTS flow_classifications (
            id                  SERIAL PRIMARY KEY,
            flow_event_id       INTEGER NOT NULL,
            alert_hash          TEXT NOT NULL,
            trade_style_label   TEXT,
            trade_style_conf    TEXT,
            trade_style_reason  TEXT,
            intent_label        TEXT,
            intent_conf         TEXT,
            intent_reason       TEXT,
            direction_label     TEXT,
            direction_conf      TEXT,
            direction_reason    TEXT,
            momentum_label      TEXT,
            momentum_conf       TEXT,
            momentum_reason     TEXT,
            setup_quality_label TEXT,
            setup_quality_conf  TEXT,
            setup_quality_reason TEXT,
            tags                TEXT,
            summary             TEXT,
            publish_recommended BOOLEAN DEFAULT TRUE,
            suppress_reason     TEXT,
            classified_at       TIMESTAMP NOT NULL DEFAULT NOW()
        )"""
    return """
        CREATE TABLE IF NOT EXISTS flow_classifications (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            flow_event_id       INTEGER NOT NULL,
            alert_hash          TEXT NOT NULL,
            trade_style_label   TEXT,
            trade_style_conf    TEXT,
            trade_style_reason  TEXT,
            intent_label        TEXT,
            intent_conf         TEXT,
            intent_reason       TEXT,
            direction_label     TEXT,
            direction_conf      TEXT,
            direction_reason    TEXT,
            momentum_label      TEXT,
            momentum_conf       TEXT,
            momentum_reason     TEXT,
            setup_quality_label TEXT,
            setup_quality_conf  TEXT,
            setup_quality_reason TEXT,
            tags                TEXT,
            summary             TEXT,
            publish_recommended INTEGER DEFAULT 1,
            suppress_reason     TEXT,
            classified_at       TEXT NOT NULL,
            FOREIGN KEY (flow_event_id) REFERENCES flow_events(id)
        )"""


def _alert_outcomes_sql() -> str:
    if _POSTGRES:
        return """
        CREATE TABLE IF NOT EXISTS alert_outcomes (
            id                  SERIAL PRIMARY KEY,
            alert_hash          TEXT NOT NULL,
            ticker              TEXT NOT NULL,
            contract            TEXT NOT NULL,
            sentiment           TEXT NOT NULL,
            score               INTEGER,
            alert_published_at  TIMESTAMP,
            alert_score         INTEGER,
            alert_premium       INTEGER,
            alert_trade_style   TEXT,
            alert_intent        TEXT,
            alert_setup_quality TEXT,
            alert_moneyness     TEXT,
            alert_flow_type     TEXT,
            outcomes_json       TEXT DEFAULT '{}',
            best_return_pct     REAL,
            worst_return_pct    REAL,
            hit_eod             BOOLEAN,
            hit_next_close      BOOLEAN,
            fully_recorded      BOOLEAN DEFAULT FALSE,
            created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMP NOT NULL DEFAULT NOW()
        )"""
    return """
        CREATE TABLE IF NOT EXISTS alert_outcomes (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_hash          TEXT NOT NULL,
            ticker              TEXT NOT NULL,
            contract            TEXT NOT NULL,
            sentiment           TEXT NOT NULL,
            score               INTEGER,
            alert_published_at  TEXT,
            alert_score         INTEGER,
            alert_premium       INTEGER,
            alert_trade_style   TEXT,
            alert_intent        TEXT,
            alert_setup_quality TEXT,
            alert_moneyness     TEXT,
            alert_flow_type     TEXT,
            outcomes_json       TEXT DEFAULT '{}',
            best_return_pct     REAL,
            worst_return_pct    REAL,
            hit_eod             INTEGER,
            hit_next_close      INTEGER,
            fully_recorded      INTEGER DEFAULT 0,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL
        )"""


def _indexes_sql() -> list[str]:
    """Return all index CREATE statements."""
    return [
        "CREATE INDEX IF NOT EXISTS idx_published_alerts_hash_time ON published_alerts (alert_hash, published_at)",
        "CREATE INDEX IF NOT EXISTS idx_published_alerts_published_at ON published_alerts (published_at)",
        "CREATE INDEX IF NOT EXISTS idx_flow_events_hash ON flow_events (alert_hash)",
        "CREATE INDEX IF NOT EXISTS idx_flow_events_ticker ON flow_events (ticker, ingested_at)",
        "CREATE INDEX IF NOT EXISTS idx_flow_events_score ON flow_events (score, ingested_at)",
        "CREATE INDEX IF NOT EXISTS idx_flow_classifications_event ON flow_classifications (flow_event_id)",
        "CREATE INDEX IF NOT EXISTS idx_flow_classifications_hash ON flow_classifications (alert_hash)",
        "CREATE INDEX IF NOT EXISTS idx_alert_outcomes_hash ON alert_outcomes (alert_hash)",
        "CREATE INDEX IF NOT EXISTS idx_alert_outcomes_ticker ON alert_outcomes (ticker, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_alert_outcomes_score ON alert_outcomes (alert_score, created_at)",
    ]


# ---------------------------------------------------------------------------
# Public init
# ---------------------------------------------------------------------------

def init_all_tables() -> None:
    """
    Create all tables and indexes. Safe to call multiple times.
    Called on startup by both main.py and mcp_server.py.
    """
    backend = "PostgreSQL" if _POSTGRES else f"SQLite ({DB_PATH})"
    logger.info("db_init backend=%s", backend)

    with db_cursor() as (conn, cur):
        for sql in [
            _published_alerts_sql(),
            _flow_events_sql(),
            _flow_classifications_sql(),
            _alert_outcomes_sql(),
        ]:
            cur.execute(sql)

        for idx_sql in _indexes_sql():
            try:
                cur.execute(idx_sql)
            except Exception as e:
                # Index may already exist under a different name — non-fatal
                logger.debug("index skipped: %s", e)

    logger.info("db_init complete all tables ready")


# ---------------------------------------------------------------------------
# Core query helpers used by both main.py and mcp_server.py
# ---------------------------------------------------------------------------

def alert_hash_exists_in_window(alert_hash: str, window_minutes: int) -> bool:
    """Dedup check — True if this hash was published within window_minutes."""
    if _POSTGRES:
        sql = """
            SELECT 1 FROM published_alerts
            WHERE alert_hash = %s
              AND published_at >= NOW() - make_interval(mins => %s)
            LIMIT 1
        """
        params = (alert_hash, window_minutes)
    else:
        sql = """
            SELECT 1 FROM published_alerts
            WHERE alert_hash = ?
              AND datetime(published_at) >= datetime('now', ?)
            LIMIT 1
        """
        params = (alert_hash, f"-{window_minutes} minutes")

    with db_cursor() as (conn, cur):
        cur.execute(sql, params)
        return cur.fetchone() is not None


def save_published_alert(
    alert_hash: str,
    ticker: str,
    contract: str,
    source: str,
    score: int,
) -> None:
    """Insert a row into published_alerts."""
    now = utc_now_str()
    if _POSTGRES:
        sql = """
            INSERT INTO published_alerts (alert_hash, ticker, contract, source, score, published_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
        """
        params = (alert_hash, ticker, contract, source, score)
    else:
        sql = """
            INSERT INTO published_alerts (alert_hash, ticker, contract, source, score, published_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        params = (alert_hash, ticker, contract, source, score, now)

    with db_cursor() as (conn, cur):
        cur.execute(sql, params)


def get_recent_published_alerts(limit: int = 20, ticker: str | None = None, min_score: int | None = None) -> list[dict]:
    """Fetch recent published alerts with optional filters."""
    p = "%s" if _POSTGRES else "?"
    conditions = []
    params: list[Any] = []

    if ticker:
        conditions.append(f"ticker = {p}")
        params.append(ticker.upper())
    if min_score is not None:
        conditions.append(f"score >= {p}")
        params.append(min_score)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    sql = f"""
        SELECT id, ticker, contract, source, score, published_at, alert_hash
        FROM published_alerts
        {where}
        ORDER BY published_at DESC
        LIMIT {p}
    """

    with db_cursor() as (conn, cur):
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [dict(r) for r in rows]


def get_published_alert_count() -> int:
    with db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) FROM published_alerts")
        row = cur.fetchone()
        return list(row.values())[0] if _POSTGRES else row[0]


def save_flow_event_row(
    alert_hash: str,
    ticker: str,
    contract: str,
    premium: int,
    sentiment: str,
    source: str,
    dte_bucket: str,
    flow_type: str | None,
    catalyst: str | None,
    levels: str | None,
    note: str | None,
    enrichment: Any | None = None,
    score: int | None = None,
    score_reasons: list | None = None,
    passed_threshold: bool = False,
    passed_dedup: bool = False,
    was_published: bool = False,
    suppress_reason: str | None = None,
) -> int:
    """Insert a flow_events row. Returns new row id."""
    structure_notes: list = []
    enrich: dict = {}

    if enrichment:
        structure_notes = enrichment.structure_notes
        enrich = {
            "dte": enrichment.dte,
            "expiry_date": enrichment.expiry_date,
            "strike": enrichment.strike,
            "put_call": enrichment.put_call,
            "spot_price": enrichment.spot_price,
            "moneyness_pct": enrichment.moneyness_pct,
            "moneyness_tier": enrichment.moneyness_tier,
            "premium_tier": enrichment.premium_tier,
            "rvol": enrichment.rvol,
            "rvol_label": enrichment.rvol_label,
            "is_lotto": enrichment.is_lotto,
            "is_near_expiry": enrichment.is_near_expiry,
        }

    p = "%s" if _POSTGRES else "?"
    plist = ", ".join([p] * 31)
    now = utc_now_str()

    sql = f"""
        INSERT INTO flow_events (
            alert_hash, ticker, contract, premium, sentiment, source,
            dte_bucket, flow_type, catalyst, levels, note,
            dte, expiry_date, strike, put_call, spot_price,
            moneyness_pct, moneyness_tier, premium_tier,
            rvol, rvol_label, is_lotto, is_near_expiry, structure_notes,
            score, score_reasons,
            passed_threshold, passed_dedup, was_published, suppress_reason,
            ingested_at
        ) VALUES ({plist})
        {'RETURNING id' if _POSTGRES else ''}
    """

    params = (
        alert_hash, ticker, contract, premium, sentiment, source,
        dte_bucket, flow_type, catalyst, levels, note,
        enrich.get("dte"), enrich.get("expiry_date"), enrich.get("strike"),
        enrich.get("put_call"), enrich.get("spot_price"),
        enrich.get("moneyness_pct"), enrich.get("moneyness_tier"),
        enrich.get("premium_tier"), enrich.get("rvol"), enrich.get("rvol_label"),
        bool_val(enrich.get("is_lotto", False)),
        bool_val(enrich.get("is_near_expiry", False)),
        json.dumps(structure_notes),
        score, json.dumps(score_reasons or []),
        bool_val(passed_threshold), bool_val(passed_dedup), bool_val(was_published),
        suppress_reason, now,
    )

    with db_cursor() as (conn, cur):
        cur.execute(sql, params)
        if _POSTGRES:
            return cur.fetchone()["id"]
        else:
            return cur.lastrowid


def save_classification_row(
    flow_event_id: int,
    alert_hash: str,
    classification: Any,
) -> int:
    """Insert a flow_classifications row. Returns new row id."""
    p = "%s" if _POSTGRES else "?"
    plist = ", ".join([p] * 22)
    now = utc_now_str()
    c = classification

    sql = f"""
        INSERT INTO flow_classifications (
            flow_event_id, alert_hash,
            trade_style_label, trade_style_conf, trade_style_reason,
            intent_label, intent_conf, intent_reason,
            direction_label, direction_conf, direction_reason,
            momentum_label, momentum_conf, momentum_reason,
            setup_quality_label, setup_quality_conf, setup_quality_reason,
            tags, summary, publish_recommended, suppress_reason,
            classified_at
        ) VALUES ({plist})
        {'RETURNING id' if _POSTGRES else ''}
    """

    params = (
        flow_event_id, alert_hash,
        c.trade_style.label, c.trade_style.confidence, c.trade_style.reason,
        c.intent.label, c.intent.confidence, c.intent.reason,
        c.direction.label, c.direction.confidence, c.direction.reason,
        c.momentum.label, c.momentum.confidence, c.momentum.reason,
        c.setup_quality.label, c.setup_quality.confidence, c.setup_quality.reason,
        json.dumps(c.tags), c.summary,
        bool_val(c.publish_recommended), c.suppress_reason,
        now,
    )

    with db_cursor() as (conn, cur):
        cur.execute(sql, params)
        if _POSTGRES:
            return cur.fetchone()["id"]
        else:
            return cur.lastrowid


def create_outcome_row(
    alert_hash: str,
    ticker: str,
    contract: str,
    sentiment: str,
    score: int,
    premium: int | None = None,
    published_at: str | None = None,
    trade_style: str | None = None,
    intent: str | None = None,
    setup_quality: str | None = None,
    moneyness: str | None = None,
    flow_type: str | None = None,
) -> int:
    """Create a blank outcome record. Returns new row id."""
    p = "%s" if _POSTGRES else "?"
    plist = ", ".join([p] * 13)
    now = utc_now_str()

    if _POSTGRES:
        sql = f"""
            INSERT INTO alert_outcomes (
                alert_hash, ticker, contract, sentiment, score,
                alert_published_at, alert_score, alert_premium,
                alert_trade_style, alert_intent, alert_setup_quality,
                alert_moneyness, alert_flow_type,
                outcomes_json, created_at, updated_at
            ) VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},'{{}}',NOW(),NOW())
            RETURNING id
        """
    else:
        sql = f"""
            INSERT INTO alert_outcomes (
                alert_hash, ticker, contract, sentiment, score,
                alert_published_at, alert_score, alert_premium,
                alert_trade_style, alert_intent, alert_setup_quality,
                alert_moneyness, alert_flow_type,
                outcomes_json, created_at, updated_at
            ) VALUES ({plist}, '{{}}', ?, ?)
        """

    params = (
        alert_hash, ticker, contract, sentiment, score,
        published_at or now, score, premium,
        trade_style, intent, setup_quality,
        moneyness, flow_type,
    )
    if not _POSTGRES:
        params = params + (now, now)

    with db_cursor() as (conn, cur):
        cur.execute(sql, params)
        if _POSTGRES:
            return cur.fetchone()["id"]
        else:
            return cur.lastrowid


def get_outcome_row(alert_hash: str) -> dict | None:
    """Fetch outcome record by alert_hash."""
    p = "%s" if _POSTGRES else "?"
    with db_cursor() as (conn, cur):
        cur.execute(
            f"SELECT * FROM alert_outcomes WHERE alert_hash = {p} LIMIT 1",
            (alert_hash,),
        )
        row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("outcomes_json"), str):
        d["outcomes"] = json.loads(d.pop("outcomes_json") or "{}")
    return d


def update_outcome_horizons(alert_hash: str, outcomes: dict, rolled_up: dict) -> None:
    """Update outcomes_json and rolled-up stats for an alert."""
    p = "%s" if _POSTGRES else "?"
    now = utc_now_str()

    if _POSTGRES:
        sql = """
            UPDATE alert_outcomes SET
                outcomes_json = %s,
                best_return_pct = %s,
                worst_return_pct = %s,
                hit_eod = %s,
                hit_next_close = %s,
                fully_recorded = %s,
                updated_at = NOW()
            WHERE alert_hash = %s
        """
    else:
        sql = """
            UPDATE alert_outcomes SET
                outcomes_json = ?,
                best_return_pct = ?,
                worst_return_pct = ?,
                hit_eod = ?,
                hit_next_close = ?,
                fully_recorded = ?,
                updated_at = ?
            WHERE alert_hash = ?
        """

    hit_eod = rolled_up.get("hit_eod")
    hit_nc = rolled_up.get("hit_next_close")

    params = [
        json.dumps(outcomes),
        rolled_up.get("best"),
        rolled_up.get("worst"),
        bool_val(hit_eod) if hit_eod is not None else None,
        bool_val(hit_nc) if hit_nc is not None else None,
        bool_val(rolled_up.get("fully_recorded", False)),
    ]
    if not _POSTGRES:
        params.append(now)
    params.append(alert_hash)

    with db_cursor() as (conn, cur):
        cur.execute(sql, params)


def get_unresolved_alerts(days_back: int = 7) -> list[dict]:
    """Alerts in published_alerts with no outcome row yet."""
    if _POSTGRES:
        sql = f"""
            SELECT pa.alert_hash, pa.ticker, pa.contract, pa.score, pa.published_at
            FROM published_alerts pa
            LEFT JOIN alert_outcomes ao ON pa.alert_hash = ao.alert_hash
            WHERE ao.id IS NULL
              AND pa.published_at >= NOW() - INTERVAL '{days_back} days'
            ORDER BY pa.published_at DESC
        """
    else:
        sql = f"""
            SELECT pa.alert_hash, pa.ticker, pa.contract, pa.score, pa.published_at
            FROM published_alerts pa
            LEFT JOIN alert_outcomes ao ON pa.alert_hash = ao.alert_hash
            WHERE ao.id IS NULL
              AND datetime(pa.published_at) >= datetime('now', '-{days_back} days')
            ORDER BY pa.published_at DESC
        """
    with db_cursor() as (conn, cur):
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def get_outcome_stats(days_back: int = 30, min_score: int = 0, ticker: str | None = None) -> list[dict]:
    """Fetch outcome rows for signal quality report."""
    p = "%s" if _POSTGRES else "?"
    conditions = [
        f"created_at >= NOW() - INTERVAL '{days_back} days'" if _POSTGRES
        else f"datetime(created_at) >= datetime('now', '-{days_back} days')",
        f"alert_score >= {p}",
    ]
    params: list[Any] = [min_score]

    if ticker:
        conditions.append(f"ticker = {p}")
        params.append(ticker.upper())

    where = " AND ".join(conditions)
    with db_cursor() as (conn, cur):
        cur.execute(f"SELECT * FROM alert_outcomes WHERE {where} ORDER BY created_at DESC", params)
        return [dict(r) for r in cur.fetchall()]