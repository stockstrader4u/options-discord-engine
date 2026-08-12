"""
gex_vex_history.py — day-over-day snapshot storage and comparison for
the GEX/VEX pipeline.

WHY THIS EXISTS: gex_vex_combined_daily.py runs once per day, computes
fresh GEX/VEX numbers, and posts them -- with nothing carried over from
the previous day's run. Subscribers had no way to tell whether today's
call wall is the same as yesterday's, whether a ticker flipped from
long to short gamma overnight, or whether a level that was announced
yesterday actually held. This module adds that memory.

STORAGE: Postgres, reusing the project's existing DB (per standing
convention: pg8000, `:name` parameter style -- via pg8000.native, whose
Connection.run() takes `:name`-style placeholders directly, matching
that convention exactly). Deliberately NOT a flat file on Railway's
filesystem -- Railway's filesystem is ephemeral and resets on every
redeploy, so anything not in Postgres or an env var can silently
vanish, which would make "yesterday" occasionally just disappear for
no visible reason.

REQUIRED ENV VAR: DATABASE_URL (Railway's standard Postgres connection
string, auto-provided when a Postgres plugin is attached to a service).
If this service doesn't already have a Postgres plugin reference, add
one via the Railway dashboard the same way the flow-alert engine's
service has one.

The table is self-bootstrapping (CREATE TABLE IF NOT EXISTS on first
use) -- no manual migration step required.

WHAT GETS COMPARED, AND WHY THOSE FIELDS SPECIFICALLY:
  - Regime flip (long gamma <-> short gamma): the single most
    actionable "look forward to this" signal -- always surfaced when
    it happens, regardless of how small the underlying magnitude
    change was, since the REGIME itself is what changes how a ticker
    tends to behave.
  - Call wall / put wall level shifts: only surfaced when the move is
    large enough to matter (see WALL_SHIFT_THRESHOLD_PCT below) -- a
    wall that's within noise of yesterday's isn't worth a line, and
    printing deltas every single day even when nothing changed would
    train subscribers to ignore the section entirely.
  - Gamma flip level shift: same threshold logic.
  - Spot price change: always shown when a comparison exists at all,
    since it's the one number every reader immediately understands
    without any GEX background.
"""

import os
from datetime import date, timedelta

WALL_SHIFT_THRESHOLD_PCT = 1.5   # a call/put wall must move at least this
                                  # much (as % of spot) to be worth a line
GAMMA_FLIP_SHIFT_THRESHOLD_PCT = 1.5

_TABLE_READY = False


def _get_connection():
    """
    Returns a pg8000.native.Connection built from DATABASE_URL. Raises
    if DATABASE_URL isn't set -- callers should catch and degrade
    gracefully (see save_snapshot/get_previous_snapshot below), since a
    missing/misconfigured DB should never take down the actual GEX/VEX
    posting pipeline, only the "Since Yesterday" comparison layer on
    top of it.
    """
    import pg8000.native
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set -- cannot connect to Postgres for GEX/VEX history.")

    # Parse postgres://user:pass@host:port/dbname (Railway's standard format)
    from urllib.parse import urlparse
    parsed = urlparse(database_url)
    return pg8000.native.Connection(
        user=parsed.username,
        password=parsed.password,
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=parsed.path.lstrip("/"),
    )


def ensure_table() -> bool:
    """
    Creates the snapshots table if it doesn't already exist. Call once
    per run, before any save/read. Returns False (and logs, doesn't
    raise) if Postgres is unreachable -- history/comparison simply
    won't be available this run, but the rest of the pipeline
    (computing and posting today's numbers) must still proceed.
    """
    global _TABLE_READY
    if _TABLE_READY:
        return True
    try:
        conn = _get_connection()
        conn.run("""
            CREATE TABLE IF NOT EXISTS gex_vex_snapshots (
                ticker TEXT NOT NULL,
                snapshot_date DATE NOT NULL,
                spot NUMERIC,
                call_wall NUMERIC,
                put_wall NUMERIC,
                max_pos_gex_strike NUMERIC,
                max_neg_gex_strike NUMERIC,
                net_gex NUMERIC,
                net_vex NUMERIC,
                gamma_flip NUMERIC,
                created_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (ticker, snapshot_date)
            )
        """)
        conn.close()
        _TABLE_READY = True
        print("[GEX HISTORY] snapshots table ready.")
        return True
    except Exception as e:
        print(f"[GEX HISTORY WARN] could not create/verify snapshots table: {type(e).__name__}: {e} "
              f"-- Since Yesterday comparisons will be unavailable this run.")
        return False


def save_snapshot(r: dict, snapshot_date: date) -> bool:
    """
    Upserts today's snapshot for one ticker. Safe to call multiple
    times for the same ticker/date (e.g. a retried run) -- ON CONFLICT
    just overwrites with the latest numbers rather than erroring or
    duplicating.
    """
    if "error" in r or not _TABLE_READY:
        return False
    try:
        conn = _get_connection()
        conn.run(
            """
            INSERT INTO gex_vex_snapshots
                (ticker, snapshot_date, spot, call_wall, put_wall,
                 max_pos_gex_strike, max_neg_gex_strike, net_gex, net_vex, gamma_flip)
            VALUES
                (:ticker, :snapshot_date, :spot, :call_wall, :put_wall,
                 :max_pos_gex_strike, :max_neg_gex_strike, :net_gex, :net_vex, :gamma_flip)
            ON CONFLICT (ticker, snapshot_date) DO UPDATE SET
                spot = EXCLUDED.spot,
                call_wall = EXCLUDED.call_wall,
                put_wall = EXCLUDED.put_wall,
                max_pos_gex_strike = EXCLUDED.max_pos_gex_strike,
                max_neg_gex_strike = EXCLUDED.max_neg_gex_strike,
                net_gex = EXCLUDED.net_gex,
                net_vex = EXCLUDED.net_vex,
                gamma_flip = EXCLUDED.gamma_flip
            """,
            ticker=r["ticker"], snapshot_date=snapshot_date,
            spot=r.get("spot"), call_wall=r.get("call_wall"), put_wall=r.get("put_wall"),
            max_pos_gex_strike=r.get("max_pos_gex_strike"), max_neg_gex_strike=r.get("max_neg_gex_strike"),
            net_gex=r.get("net_gex"), net_vex=r.get("net_vex"), gamma_flip=r.get("gamma_flip"),
        )
        conn.close()
        return True
    except Exception as e:
        print(f"[GEX HISTORY WARN] {r.get('ticker', '?')}: failed to save snapshot: {type(e).__name__}: {e}")
        return False


def get_previous_snapshot(ticker: str, before_date: date, lookback_days: int = 7) -> dict:
    """
    Returns the most recent snapshot for `ticker` strictly BEFORE
    `before_date`, searching back up to `lookback_days` (so a Monday
    run correctly finds the prior Friday, skipping the weekend gap).
    Returns None if nothing found or the DB is unavailable.
    """
    if not _TABLE_READY:
        return None
    try:
        conn = _get_connection()
        earliest = before_date - timedelta(days=lookback_days)
        rows = conn.run(
            """
            SELECT snapshot_date, spot, call_wall, put_wall,
                   max_pos_gex_strike, max_neg_gex_strike, net_gex, net_vex, gamma_flip
            FROM gex_vex_snapshots
            WHERE ticker = :ticker AND snapshot_date < :before_date AND snapshot_date >= :earliest
            ORDER BY snapshot_date DESC
            LIMIT 1
            """,
            ticker=ticker, before_date=before_date, earliest=earliest,
        )
        conn.close()
        if not rows:
            return None
        row = rows[0]
        cols = ["snapshot_date", "spot", "call_wall", "put_wall",
                "max_pos_gex_strike", "max_neg_gex_strike", "net_gex", "net_vex", "gamma_flip"]
        result = dict(zip(cols, row))
        # pg8000 returns NUMERIC columns as Decimal, not float -- mixing
        # Decimal and float in arithmetic (e.g. today_val - yday_val
        # where today_val came from compute_gex_vex() as a plain float)
        # raises TypeError. Cast every numeric field to float here, once,
        # so every downstream comparison in build_since_yesterday_line()
        # can safely assume plain floats regardless of which side
        # (today's fresh dict vs. yesterday's DB row) a value came from.
        for k in cols:
            if k != "snapshot_date" and result[k] is not None:
                result[k] = float(result[k])
        return result
    except Exception as e:
        print(f"[GEX HISTORY WARN] {ticker}: failed to fetch previous snapshot: {type(e).__name__}: {e}")
        return None


def build_since_yesterday_line(ticker: str, today: dict, yesterday: dict) -> str:
    """
    Returns a short "Since Yesterday" line for one ticker, or an empty
    string if nothing meaningful changed (deliberately -- printing a
    delta every single day even when nothing moved would train readers
    to skip the section entirely). A regime flip is ALWAYS surfaced
    regardless of magnitude, since the regime itself is the headline
    fact, not a threshold-gated detail.
    """
    if not yesterday:
        return ""

    parts = []

    spot_today = today.get("spot")
    spot_yday = yesterday.get("spot")
    if spot_today is not None and spot_yday:
        spot_change_pct = (spot_today - spot_yday) / spot_yday * 100
        arrow = "\u2191" if spot_change_pct >= 0 else "\u2193"
        parts.append(f"Spot {arrow} {abs(spot_change_pct):.1f}% (${spot_yday:,.2f} \u2192 ${spot_today:,.2f})")

    net_gex_today = today.get("net_gex")
    net_gex_yday = yesterday.get("net_gex")
    if net_gex_today is not None and net_gex_yday is not None:
        was_long = net_gex_yday >= 0
        is_long = net_gex_today >= 0
        if was_long != is_long:
            flip_desc = "LONG \u2192 SHORT gamma" if was_long else "SHORT \u2192 LONG gamma"
            parts.append(f"\u26A0\uFE0F Regime flipped: {flip_desc}")

    for label, key, pct_key in (("Call wall", "call_wall", None), ("Put wall", "put_wall", None)):
        today_val = today.get(key)
        yday_val = yesterday.get(key)
        spot = spot_today or spot_yday
        if today_val is not None and yday_val is not None and spot:
            shift_pct = abs(today_val - yday_val) / spot * 100
            if shift_pct >= WALL_SHIFT_THRESHOLD_PCT:
                direction = "up" if today_val > yday_val else "down"
                import gex_vex
                parts.append(f"{label} moved {direction}: {gex_vex.format_strike(yday_val)} \u2192 "
                              f"{gex_vex.format_strike(today_val)}")
        elif today_val is not None and yday_val is None:
            import gex_vex
            parts.append(f"{label} newly established at {gex_vex.format_strike(today_val)} (had no clear level yesterday)")
        elif today_val is None and yday_val is not None:
            import gex_vex
            parts.append(f"{label} no longer clear today (was {gex_vex.format_strike(yday_val)} yesterday)")

    flip_today = today.get("gamma_flip")
    flip_yday = yesterday.get("gamma_flip")
    spot_ref = spot_today or spot_yday
    if flip_today is not None and flip_yday is not None and spot_ref:
        shift_pct = abs(flip_today - flip_yday) / spot_ref * 100
        if shift_pct >= GAMMA_FLIP_SHIFT_THRESHOLD_PCT:
            parts.append(f"Gamma flip level shifted: ${flip_yday:,.2f} \u2192 ${flip_today:,.2f}")

    if not parts:
        return ""
    return "  \u00b7  ".join(parts)


def save_and_compare(r: dict, today_date: date) -> str:
    """
    Convenience wrapper: saves today's snapshot for this ticker, fetches
    yesterday's, and returns the "Since Yesterday" line (or "" if no
    prior snapshot exists yet, or nothing meaningful changed). This is
    the single function gex_vex_combined_daily.py needs to call per
    ticker -- callers don't need to know about the save/fetch split.
    """
    if "error" in r:
        return ""
    yesterday = get_previous_snapshot(r["ticker"], today_date)
    save_snapshot(r, today_date)
    if not yesterday:
        return ""
    return build_since_yesterday_line(r["ticker"], r, yesterday)