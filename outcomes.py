"""
outcomes.py — Phase 3 alert outcome tracking.

Records forward performance of published alerts at multiple time horizons.
Used to measure whether alerts are actually valuable and tune the engine.

Horizons tracked: 5m, 15m, 30m, 1h, eod, next_open, next_close, 3d, 5d

Each outcome stores:
- underlying return (%)
- directional hit (did price move the right way?)
- max favorable excursion (best it got)
- max adverse excursion (worst it got)
- invalidated flag

Phase 4 will auto-fill these from a market data provider.
Phase 3 provides the table, the MCP tool to record them manually,
and the query layer for the signal quality report.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any, Literal

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("options-outcomes")
DB_PATH = os.getenv("DB_PATH", "alerts.db")

Horizon = Literal["5m", "15m", "30m", "1h", "eod", "next_open", "next_close", "3d", "5d"]
HORIZONS: list[str] = ["5m", "15m", "30m", "1h", "eod", "next_open", "next_close", "3d", "5d"]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def ensure_outcomes_table() -> None:
    """Create alert_outcomes table if it doesn't exist. Safe to call multiple times."""
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_outcomes (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,

                -- Link back to published alert
                alert_hash          TEXT NOT NULL,
                ticker              TEXT NOT NULL,
                contract            TEXT NOT NULL,
                sentiment           TEXT NOT NULL,
                score               INTEGER,

                -- Alert metadata snapshot
                alert_published_at  TEXT,
                alert_score         INTEGER,
                alert_premium       INTEGER,
                alert_trade_style   TEXT,    -- lotto / swing
                alert_intent        TEXT,    -- speculative / hedge
                alert_setup_quality TEXT,    -- actionable / chase
                alert_moneyness     TEXT,    -- atm / otm / etc
                alert_flow_type     TEXT,    -- sweep / block

                -- Outcome per horizon (stored as JSON for flexibility)
                -- Each horizon key maps to:
                --   { return_pct, hit, mfe, mae, invalidated, recorded_at, note }
                outcomes_json       TEXT DEFAULT '{}',

                -- Rolled-up summary (filled once enough horizons are recorded)
                best_return_pct     REAL,
                worst_return_pct    REAL,
                hit_eod             INTEGER,   -- 1=hit, 0=miss, NULL=not recorded
                hit_next_close      INTEGER,
                fully_recorded      INTEGER DEFAULT 0,

                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_alert_outcomes_hash
            ON alert_outcomes (alert_hash)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_alert_outcomes_ticker
            ON alert_outcomes (ticker, created_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_alert_outcomes_score
            ON alert_outcomes (alert_score, created_at)
        """)
        conn.commit()


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def create_outcome_record(
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
    """
    Create a blank outcome record for a published alert.
    Returns the new row id.
    Call this right after posting to Discord.
    """
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        cur = conn.execute(
            """
            INSERT INTO alert_outcomes (
                alert_hash, ticker, contract, sentiment, score,
                alert_published_at, alert_score, alert_premium,
                alert_trade_style, alert_intent, alert_setup_quality,
                alert_moneyness, alert_flow_type,
                outcomes_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
            """,
            (
                alert_hash, ticker, contract, sentiment, score,
                published_at or now, score, premium,
                trade_style, intent, setup_quality,
                moneyness, flow_type,
                now, now,
            ),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]


def record_horizon_outcome(
    alert_hash: str,
    horizon: str,
    return_pct: float,
    hit: bool,
    mfe: float | None = None,
    mae: float | None = None,
    invalidated: bool = False,
    note: str | None = None,
) -> dict[str, Any]:
    """
    Record the outcome for a specific time horizon on an alert.

    Args:
        alert_hash: Hash of the alert (from published_alerts or flow_events).
        horizon: One of: 5m, 15m, 30m, 1h, eod, next_open, next_close, 3d, 5d
        return_pct: Underlying return % at this horizon. Positive = up.
        hit: True if price moved in the alerted direction.
        mfe: Max favorable excursion % (best the trade got).
        mae: Max adverse excursion % (worst the trade got).
        invalidated: True if the setup was invalidated (e.g. broke key level).
        note: Optional text note about the outcome.

    Returns:
        Updated outcome record dict.
    """
    if horizon not in HORIZONS:
        raise ValueError(f"horizon must be one of {HORIZONS}")

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    with _db() as conn:
        row = conn.execute(
            "SELECT id, outcomes_json, sentiment FROM alert_outcomes WHERE alert_hash = ? LIMIT 1",
            (alert_hash,),
        ).fetchone()

        if not row:
            raise ValueError(f"No outcome record found for alert_hash={alert_hash}. "
                             "Call create_outcome_record first.")

        outcomes = json.loads(row["outcomes_json"] or "{}")
        outcomes[horizon] = {
            "return_pct": return_pct,
            "hit": hit,
            "mfe": mfe,
            "mae": mae,
            "invalidated": invalidated,
            "recorded_at": now,
            "note": note,
        }

        # Rolled-up stats
        returns = [v["return_pct"] for v in outcomes.values() if v.get("return_pct") is not None]
        best = max(returns) if returns else None
        worst = min(returns) if returns else None
        hit_eod = outcomes.get("eod", {}).get("hit")
        hit_next_close = outcomes.get("next_close", {}).get("hit")
        fully_recorded = int(all(h in outcomes for h in HORIZONS))

        conn.execute(
            """
            UPDATE alert_outcomes SET
                outcomes_json = ?,
                best_return_pct = ?,
                worst_return_pct = ?,
                hit_eod = ?,
                hit_next_close = ?,
                fully_recorded = ?,
                updated_at = ?
            WHERE alert_hash = ?
            """,
            (
                json.dumps(outcomes),
                best, worst,
                int(hit_eod) if hit_eod is not None else None,
                int(hit_next_close) if hit_next_close is not None else None,
                fully_recorded,
                now, alert_hash,
            ),
        )
        conn.commit()

        updated = conn.execute(
            "SELECT * FROM alert_outcomes WHERE alert_hash = ?", (alert_hash,)
        ).fetchone()
        return dict(updated)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def get_outcome(alert_hash: str) -> dict | None:
    """Fetch the full outcome record for an alert."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM alert_outcomes WHERE alert_hash = ? LIMIT 1",
            (alert_hash,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["outcomes"] = json.loads(d.pop("outcomes_json") or "{}")
        return d


def get_signal_quality_stats(
    days_back: int = 30,
    min_score: int = 0,
    ticker: str | None = None,
) -> dict[str, Any]:
    """
    Compute signal quality statistics over a date range.

    Returns win rates, average returns, and breakdowns by:
    - score bucket
    - trade style
    - sentiment
    - flow type
    - setup quality

    Args:
        days_back: How many days back to look (default 30).
        min_score: Only include alerts with score >= this.
        ticker: Optional filter by ticker.
    """
    with _db() as conn:
        conditions = [
            f"datetime(created_at) >= datetime('now', '-{days_back} days')",
            f"alert_score >= {min_score}",
        ]
        if ticker:
            conditions.append(f"ticker = '{ticker.upper()}'")

        where = " AND ".join(conditions)

        rows = conn.execute(
            f"SELECT * FROM alert_outcomes WHERE {where} ORDER BY created_at DESC"
        ).fetchall()

    if not rows:
        return {
            "ok": True,
            "message": f"No outcome records found in the last {days_back} days.",
            "total_alerts": 0,
            "recorded_outcomes": 0,
        }

    records = []
    for row in rows:
        d = dict(row)
        d["outcomes"] = json.loads(d.pop("outcomes_json") or "{}")
        records.append(d)

    total = len(records)
    with_eod = [r for r in records if "eod" in r["outcomes"]]
    with_next_close = [r for r in records if "next_close" in r["outcomes"]]

    def _win_rate(subset: list) -> str:
        hits = [r for r in subset if r.get("hit_eod") == 1]
        if not subset:
            return "N/A"
        return f"{len(hits)/len(subset)*100:.0f}% ({len(hits)}/{len(subset)})"

    def _avg_return(subset: list, horizon: str) -> str:
        rets = [
            r["outcomes"][horizon]["return_pct"]
            for r in subset
            if horizon in r["outcomes"] and r["outcomes"][horizon].get("return_pct") is not None
        ]
        if not rets:
            return "N/A"
        return f"{sum(rets)/len(rets):.2f}%"

    # Breakdown by score bucket
    buckets = {
        "80-100": [r for r in records if (r["alert_score"] or 0) >= 80],
        "70-79":  [r for r in records if 70 <= (r["alert_score"] or 0) < 80],
        "60-69":  [r for r in records if 60 <= (r["alert_score"] or 0) < 70],
        "0-59":   [r for r in records if (r["alert_score"] or 0) < 60],
    }

    # Breakdown by trade style
    styles = {}
    for r in records:
        s = r.get("alert_trade_style") or "unknown"
        styles.setdefault(s, []).append(r)

    # Breakdown by sentiment
    sentiments = {}
    for r in records:
        s = r.get("sentiment") or "unknown"
        sentiments.setdefault(s, []).append(r)

    return {
        "ok": True,
        "period_days": days_back,
        "total_alerts_tracked": total,
        "with_eod_outcome": len(with_eod),
        "with_next_close_outcome": len(with_next_close),
        "overall": {
            "eod_win_rate": _win_rate(with_eod),
            "avg_eod_return": _avg_return(records, "eod"),
            "avg_next_close_return": _avg_return(records, "next_close"),
        },
        "by_score_bucket": {
            bucket: {
                "count": len(subset),
                "eod_win_rate": _win_rate([r for r in subset if "eod" in r["outcomes"]]),
                "avg_eod_return": _avg_return(subset, "eod"),
            }
            for bucket, subset in buckets.items()
            if subset
        },
        "by_trade_style": {
            style: {
                "count": len(subset),
                "eod_win_rate": _win_rate([r for r in subset if "eod" in r["outcomes"]]),
            }
            for style, subset in styles.items()
        },
        "by_sentiment": {
            sent: {
                "count": len(subset),
                "eod_win_rate": _win_rate([r for r in subset if "eod" in r["outcomes"]]),
                "avg_eod_return": _avg_return(subset, "eod"),
            }
            for sent, subset in sentiments.items()
        },
        "note": (
            "Outcomes are manually recorded via record_outcome MCP tool. "
            "Phase 4 will auto-fill from live market data."
        ),
    }


def list_unresolved_alerts(days_back: int = 7) -> list[dict]:
    """
    List published alerts that have no outcome recorded yet.
    Use this to know which alerts need follow-up.
    """
    with _db() as conn:
        rows = conn.execute(
            f"""
            SELECT pa.alert_hash, pa.ticker, pa.contract, pa.score, pa.published_at
            FROM published_alerts pa
            LEFT JOIN alert_outcomes ao ON pa.alert_hash = ao.alert_hash
            WHERE ao.id IS NULL
              AND datetime(pa.published_at) >= datetime('now', '-{days_back} days')
            ORDER BY pa.published_at DESC
            """,
        ).fetchall()
    return [dict(r) for r in rows]
