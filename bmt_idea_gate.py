"""
bmt_idea_gate.py -- evidence-based suppression of consistently weak nightly idea types,
plus the idea_log / idea_grades tables that make every future decision measurable.

WHY: the nightly digest has no memory of what worked. This module (a) logs EVERY candidate
the nightly considers (published or suppressed) with its features, (b) lets
bmt_idea_grader.py grade them honestly (stop-first replay + real option P&L), and
(c) suppresses an idea BUCKET only when the graded history says it is consistently weak.

SUPPRESSION RULE (fixed in advance, not tuned to any bucket):
  A bucket = direction, or direction + chart pattern.  Over the trailing LOOKBACK_DAYS of
  graded ideas (published + shadow), suppress when ALL hold:
    * n >= MIN_N graded ideas                     (enough trades)
    * spans >= MIN_WEEKS distinct publish weeks   ("long period", not one bad fortnight)
    * mean R < 0 and the one-sided 80% UPPER bound of mean R < 0
      (we are >= ~80% confident the bucket loses; noisy buckets stay published)
  R = honest underlying R: (exit - entry) / (entry - stop), stop wins ties, time-exit at expiry.
  Suppressed ideas are NOT posted, but they are still logged (published=false) and graded as
  SHADOW ideas so the bucket keeps being measured and is released automatically if it recovers.

MANUAL OVERRIDE: env IDEA_GATE_FORCE="PUT" or "PUT:lower highs,CALL:V-recovery" suppresses
those buckets regardless of stats. IDEA_GATE_DISABLE=1 turns the whole gate off.

FAIL-OPEN: any error here (no DB, bad data) means NOTHING is suppressed and tonight's post
proceeds exactly as before. This module can never block or alter a post by crashing.

CLI:
  python bmt_idea_gate.py --report      bucket stats + what would be suppressed now
  python bmt_idea_gate.py --backfill    copy historical nightly_setup_ideas into idea_log
"""
from __future__ import annotations

import math
import os
import sys
from datetime import datetime, timedelta
from urllib.parse import urlparse

DATABASE_URL = os.environ.get("DATABASE_URL", "")
LOOKBACK_DAYS = 120
MIN_N = 30
MIN_WEEKS = 8
CONF_Z = 0.84          # one-sided 80%


def _connect():
    import pg8000.native as pg
    p = urlparse(DATABASE_URL)
    return pg.Connection(host=p.hostname, port=p.port or 5432, database=p.path.lstrip("/"),
                         user=p.username, password=p.password)


def ensure_tables(conn=None):
    own = conn is None
    conn = conn or _connect()
    try:
        conn.run("""
            CREATE TABLE IF NOT EXISTS idea_log (
                id SERIAL PRIMARY KEY,
                logged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                publish_date DATE NOT NULL,
                ticker TEXT NOT NULL,
                direction TEXT NOT NULL,
                pattern TEXT,
                dte INTEGER,
                expiry_date DATE,
                strike NUMERIC,
                premium NUMERIC,
                spot NUMERIC,
                entry_low NUMERIC, entry_high NUMERIC, stop NUMERIC, target1 NUMERIC, target2 NUMERIC,
                iv_rv_ratio NUMERIC,
                flow_premium NUMERIC,
                call_pct NUMERIC,
                flow_bias TEXT,
                ranking_score NUMERIC,
                tier_percentile NUMERIC,
                is_mega BOOLEAN,
                role TEXT,
                risk TEXT,
                published BOOLEAN NOT NULL DEFAULT TRUE,
                suppress_reason TEXT,
                source TEXT NOT NULL DEFAULT 'live',
                UNIQUE (publish_date, ticker, direction)
            )""")
        conn.run("""
            CREATE TABLE IF NOT EXISTS idea_grades (
                idea_id INTEGER PRIMARY KEY REFERENCES idea_log(id),
                graded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                filled BOOLEAN NOT NULL,
                entry_ts TIMESTAMPTZ, entry_px NUMERIC, exit_ts TIMESTAMPTZ, exit_px NUMERIC,
                why TEXT, r NUMERIC, mae_r NUMERIC,
                opt_symbol TEXT, opt_buy NUMERIC, opt_sell NUMERIC, opt_ret NUMERIC
            )""")
    finally:
        if own:
            conn.close()


# ------------------------------------------------------------------ stats / rule
def _bucket_rows(conn):
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).date()
    return conn.run("""
        SELECT l.direction, l.pattern, l.publish_date, g.r
        FROM idea_log l JOIN idea_grades g ON g.idea_id = l.id
        WHERE g.filled AND g.r IS NOT NULL AND l.publish_date >= :since
    """, since=since)


def bucket_stats(conn=None):
    """{bucket_key: dict(n, weeks, mean, se, upper, suppress)} for direction and direction|pattern."""
    own = conn is None
    conn = conn or _connect()
    try:
        rows = _bucket_rows(conn)
    finally:
        if own:
            conn.close()
    buckets: dict = {}
    for direction, pattern, pdate, r in rows:
        for key in (direction.upper(), f"{direction.upper()}|{pattern}" if pattern else None):
            if key:
                buckets.setdefault(key, []).append((pdate, float(r)))
    out = {}
    for key, v in buckets.items():
        n = len(v)
        rs = [x[1] for x in v]
        weeks = len({(d.isocalendar()[0], d.isocalendar()[1]) for d, _ in v})
        mean = sum(rs) / n
        se = (math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) / math.sqrt(n)) if n > 1 else float("inf")
        upper = mean + CONF_Z * se
        out[key] = {"n": n, "weeks": weeks, "mean": mean, "se": se, "upper": upper,
                    "suppress": (n >= MIN_N and weeks >= MIN_WEEKS and mean < 0 and upper < 0)}
    return out


def _forced():
    raw = os.environ.get("IDEA_GATE_FORCE", "").strip()
    # accept "PUT" or "PUT:lower highs" (or "PUT|lower highs"); bucket keys are "DIRECTION|pattern"
    return {x.strip().replace(":", "|").upper() for x in raw.split(",") if x.strip()} if raw else set()


def _keys(c):
    d = str(c.get("direction", "")).upper()
    p = c.get("pattern")
    return [d] + ([f"{d}|{p}"] if p else [])


def apply_gate(eligible: list):
    """Returns (kept, suppressed). Suppressed candidates get c['suppress_reason'] set.
    Fail-open: on ANY problem returns (eligible, [])."""
    try:
        if os.environ.get("IDEA_GATE_DISABLE") == "1" or not DATABASE_URL:
            return eligible, []
        forced = {f.upper() for f in _forced()}
        stats = {}
        try:
            stats = bucket_stats()
        except Exception as e:
            print(f"  [GATE WARN] stats unavailable ({e}); only forced buckets apply")
        kept, supp = [], []
        for c in eligible:
            reason = None
            for k in _keys(c):
                if k.upper() in forced:
                    reason = f"forced:{k}"
                    break
                s = stats.get(k)
                if s and s["suppress"]:
                    reason = (f"weak:{k} n={s['n']} weeks={s['weeks']} meanR={s['mean']:+.2f} "
                              f"upper80={s['upper']:+.2f}")
                    break
            if reason:
                c["suppress_reason"] = reason
                supp.append(c)
                print(f"  [GATE] suppressing {c['ticker']} {c.get('direction')} -- {reason}")
            else:
                kept.append(c)
        return kept, supp
    except Exception as e:
        print(f"  [GATE WARN] gate failed open: {e}")
        return eligible, []


# ------------------------------------------------------------------ logging
def log_ideas(published: list, suppressed: list, target_date):
    """Best-effort: record every candidate (published or suppressed) with its features."""
    if not DATABASE_URL:
        return
    try:
        conn = _connect()
        ensure_tables(conn)
        n = 0
        for rows, pub in ((published, True), (suppressed, False)):
            for c in rows:
                if "strike" not in c:
                    continue
                exp = None
                if c.get("expiry_iso"):
                    exp = datetime.strptime(c["expiry_iso"], "%Y-%m-%d").date()
                flow = c.get("flow") or {}
                conn.run("""
                    INSERT INTO idea_log (publish_date, ticker, direction, pattern, dte, expiry_date, strike, premium,
                        spot, entry_low, entry_high, stop, target1, target2, iv_rv_ratio, flow_premium, call_pct,
                        flow_bias, ranking_score, tier_percentile, is_mega, role, risk, published, suppress_reason)
                    VALUES (:pd,:t,:d,:pat,:dte,:exp,:k,:prem,:spot,:el,:eh,:st,:t1,:t2,:ivrv,:fp,:cp,:fb,:rs,:tp,:mega,
                            :role,:risk,:pub,:why)
                    ON CONFLICT (publish_date, ticker, direction) DO NOTHING""",
                         pd=target_date.date(), t=c["ticker"], d=str(c["direction"]).upper(), pat=c.get("pattern"),
                         dte=c.get("dte"), exp=exp, k=c.get("strike"), prem=c.get("premium"),
                         spot=c.get("current_price"), el=c.get("entry_low"), eh=c.get("entry_high"),
                         st=c.get("stop"), t1=c.get("target1"), t2=c.get("target2"), ivrv=c.get("iv_rv_ratio"),
                         fp=flow.get("premium"), cp=flow.get("call_pct"), fb=flow.get("bias"),
                         rs=c.get("ranking_score"), tp=c.get("tier_percentile"), mega=c.get("is_mega"),
                         role=c.get("role"), risk=c.get("risk"), pub=pub, why=c.get("suppress_reason"))
                n += 1
        conn.close()
        print(f"  [IDEA LOG] logged {n} candidate(s) ({len(published)} published, {len(suppressed)} suppressed/shadow).")
    except Exception as e:
        print(f"  [IDEA LOG WARN] {e}")


def backfill_from_nightly_ideas():
    conn = _connect()
    ensure_tables(conn)
    rows = conn.run("""
        SELECT publish_date, ticker, direction, expiry_date, strike, entry_low, entry_high, stop, target1, target2,
               role, risk FROM nightly_setup_ideas""")
    n = 0
    for (pd_, t, d, exp, k, el, eh, st, t1, t2, role, risk) in rows:
        dte = (exp - pd_).days if exp and pd_ else None
        conn.run("""
            INSERT INTO idea_log (publish_date, ticker, direction, dte, expiry_date, strike, entry_low, entry_high,
                stop, target1, target2, role, risk, published, source)
            VALUES (:pd,:t,:d,:dte,:exp,:k,:el,:eh,:st,:t1,:t2,:role,:risk,TRUE,'backfill')
            ON CONFLICT (publish_date, ticker, direction) DO NOTHING""",
                 pd=pd_, t=t, d=str(d).upper(), dte=dte, exp=exp, k=k, el=el, eh=eh, st=st, t1=t1, t2=t2,
                 role=role, risk=risk)
        n += 1
    conn.close()
    print(f"backfilled {n} historical ideas into idea_log")


def report():
    s = bucket_stats()
    print(f"lookback {LOOKBACK_DAYS}d | rule: n>={MIN_N}, weeks>={MIN_WEEKS}, mean R<0 and upper80<0")
    print(f"{'bucket':<28}{'n':>5}{'weeks':>7}{'meanR':>8}{'se':>7}{'upper80':>9}  decision")
    for k, v in sorted(s.items()):
        print(f"{k:<28}{v['n']:>5}{v['weeks']:>7}{v['mean']:>+8.2f}{v['se']:>7.2f}{v['upper']:>+9.2f}  "
              f"{'SUPPRESS' if v['suppress'] else 'publish'}")
    f = _forced()
    if f:
        print("forced:", sorted(f))


if __name__ == "__main__":
    if "--backfill" in sys.argv:
        backfill_from_nightly_ideas()
    if "--report" in sys.argv:
        report()
