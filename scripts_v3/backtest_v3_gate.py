"""
scripts/backtest_v3_gate.py — ITEM 10: offline validation, run before
promoting v3's selection to production.

READS FROM nightly_setup_ideas_v3 ONLY (the v3 test track's own table).
This script does not touch, and has no awareness of, production's
nightly_setup_ideas table -- there is nothing to backtest there, since
production doesn't compute req_exp_ratio/rvol/etc. at all.

WHAT THIS DOES
Loads every RESOLVED row (win/loss/never_triggered) that the v3 test
track has actually posted and graded, and reports:
  (i)  Projected win rate had the 0.85 expected-move gate PLUS dedup
       PLUS cooldown been applied to this exact historical set -- i.e.
       "if we'd been running with the SAME thresholds as today, but a
       stricter/looser gate value, what would win rate have looked
       like?" This lets the threshold itself be sanity-checked against
       real outcomes, not just trusted a priori.
  (ii) How many published setups/week that stricter application would
       have cut, so the win-rate-vs-volume tradeoff is visible together,
       not just the win-rate number in isolation.

IMPORTANT SCOPE NOTE: because nightly_setup_ideas_v3 only contains rows
that the LIVE v3 pipeline already decided to publish (it already applied
the 0.85 gate, dedup, and cooldown at publish time), this script's
"projected win rate had the gate been applied" can only ever RE-APPLY
the SAME gate value (or a stricter one) to already-gated data -- it
cannot show what would have happened to candidates the live pipeline
excluded before they were ever persisted (there is no row for an
excluded candidate to backtest against). This is fundamentally a
sensitivity-to-threshold check on published setups, not a full
counterfactual replay of the entire candidate universe. That distinction
is stated plainly in this script's output every run, so it's never
mistaken for something it isn't.

Per direct instruction: v3's selection logic must NOT be promoted to
production until this script reports a projected win-rate LIFT on real
history. This script itself does not gate deployment mechanically (there
is no CI hook) -- it is a decision-support report for a human to read
before making that call by hand.

RESULTS_MODE / dry-run: this script always runs "dry" in the sense that
it never writes anything back to the database or posts to Discord -- it
only reads and prints a report. FORCE_PUBLISH-style env-var gating
doesn't apply here since there is no publish step; it's included in the
docstring only because the spec explicitly asked for "Keep FORCE_PUBLISH
dry-run printing gate decisions" as an acceptance criterion — that
behavior lives in bmt_nightly_setups_v3_test.py's own FORCE_PUBLISH_V3
env var (see that file), which already prints every [EXP-MOVE EXCLUDE]/
[DEDUP EXCLUDE]/[COOLDOWN EXCLUDE] decision with its numbers on every
run, dry or not -- there is nothing additional for this offline script
to gate, since it doesn't publish anything at all.

Run locally / on Railway as a one-off job:
  python scripts/backtest_v3_gate.py
  python scripts/backtest_v3_gate.py --gate-ratios 0.7,0.85,1.0
"""

import os
import argparse
from urllib.parse import urlparse

import pg8000.native as _pg8000

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def log(msg: str):
    print(msg, flush=True)


def _connect():
    p = urlparse(DATABASE_URL)
    return _pg8000.Connection(
        host=p.hostname, port=p.port or 5432,
        database=p.path.lstrip("/"),
        user=p.username, password=p.password,
    )


def fetch_all_resolved_rows() -> list:
    """Every row from nightly_setup_ideas_v3 with a real resolution
    (win/loss/never_triggered) — never_triggered included here (unlike
    the results report and the regression fit) because item 10 needs to
    account for it explicitly when computing "how many published setups
    would this have cut", not silently drop it."""
    if not DATABASE_URL:
        log("DATABASE_URL not set -- cannot backtest, nothing to read.")
        return []
    conn = _connect()
    try:
        rows = conn.run("""
            SELECT ticker, direction, status, publish_date, expiry_date,
                   req_exp_ratio, iv_rv_ratio, rvol, dte, is_mega_cap
            FROM nightly_setup_ideas_v3
            WHERE status IN ('win', 'loss', 'never_triggered')
            ORDER BY publish_date
        """)
        return [
            {
                "ticker": r[0], "direction": r[1], "status": r[2],
                "publish_date": r[3], "expiry_date": r[4],
                "req_exp_ratio": r[5], "iv_rv_ratio": r[6], "rvol": r[7],
                "dte": r[8], "is_mega_cap": r[9],
            }
            for r in rows
        ]
    except Exception as e:
        log(f"[DB WARN] fetch_all_resolved_rows failed: {e}")
        return []
    finally:
        conn.close()


def approximate_missing_ratio(row: dict) -> float:
    """
    Per spec: "recomputes req_exp_ratio where chains are unavailable via
    archived iv_rv approximations or flags unknown." A row that was
    persisted before the expected-move gate's own req_exp_ratio column
    was ever populated (or where the live yfinance chain fetch failed at
    publish time and left it NULL) can still get a rough approximation
    from iv_rv_ratio, which is a related-but-different measure of how
    rich/cheap the option was priced relative to how much the stock
    actually moves -- NOT a substitute for the real gate math, just a
    stand-in so such rows aren't simply dropped from every threshold
    sensitivity in this report. If iv_rv_ratio is also unavailable, the
    row is explicitly flagged unknown (returns None) and excluded from
    ratio-based tables, but still counted in the "excluded from backtest
    entirely" tally so that count itself is honest about coverage.

    Approximation logic: a rich (>1.0) IV/RV ratio roughly tracks with a
    LARGER required move being priced in relative to what the stock
    actually does -- so a simple monotonic proxy, capped to a sane
    range, is used: approx_ratio = min(iv_rv_ratio / 2.0, 1.5). This is
    a coarse stand-in, clearly labeled as such in every place it's used.
    """
    if row.get("req_exp_ratio") is not None:
        return row["req_exp_ratio"], "real"
    if row.get("iv_rv_ratio"):
        approx = min(row["iv_rv_ratio"] / 2.0, 1.5)
        return approx, "approximated_from_iv_rv"
    return None, "unknown"


def weeks_spanned(rows: list) -> float:
    if not rows:
        return 0.0
    dates = [r["publish_date"] for r in rows if r.get("publish_date")]
    if not dates:
        return 0.0
    span_days = (max(dates) - min(dates)).days
    return max(span_days / 7.0, 1.0)


def apply_gate_ratio(rows: list, gate_ratio: float) -> dict:
    """
    Applies a candidate gate threshold to the already-published,
    already-resolved rows and reports what win rate WOULD have resulted
    had only the rows within this threshold been published (rows where
    |ratio| <= gate_ratio), plus how many rows would have been cut.

    Rows with status 'never_triggered' are excluded from the win-rate
    calculation (same convention as the live results report -- a trade
    that was never entered isn't a win or a loss) but ARE counted in the
    "published" and "cut" tallies, since the gate operates on whether a
    setup gets published at all, before anyone knows if it will ever
    trigger.
    """
    kept = []
    cut = []
    unknown = []
    for row in rows:
        ratio, source = approximate_missing_ratio(row)
        if ratio is None:
            unknown.append(row)
            continue
        if abs(ratio) <= gate_ratio:
            kept.append(row)
        else:
            cut.append(row)

    kept_reportable = [r for r in kept if r["status"] in ("win", "loss")]
    wins = sum(1 for r in kept_reportable if r["status"] == "win")
    losses = len(kept_reportable) - wins
    win_rate = round(wins / len(kept_reportable) * 100, 1) if kept_reportable else None

    weeks = weeks_spanned(rows)
    kept_per_week = round(len(kept) / weeks, 2) if weeks > 0 else None
    cut_per_week = round(len(cut) / weeks, 2) if weeks > 0 else None

    return {
        "gate_ratio": gate_ratio,
        "kept_count": len(kept),
        "cut_count": len(cut),
        "unknown_count": len(unknown),
        "kept_reportable_count": len(kept_reportable),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "kept_per_week": kept_per_week,
        "cut_per_week": cut_per_week,
    }


def compute_baseline(rows: list) -> dict:
    """Win rate with NO additional gate applied beyond what the live
    pipeline already did at publish time (i.e. all resolved rows as
    they actually are) -- the point of comparison every gate_ratio
    scenario is measured against."""
    reportable = [r for r in rows if r["status"] in ("win", "loss")]
    wins = sum(1 for r in reportable if r["status"] == "win")
    losses = len(reportable) - wins
    win_rate = round(wins / len(reportable) * 100, 1) if reportable else None
    weeks = weeks_spanned(rows)
    per_week = round(len(rows) / weeks, 2) if weeks > 0 else None
    never_triggered = sum(1 for r in rows if r["status"] == "never_triggered")
    return {
        "total_rows": len(rows), "reportable_count": len(reportable),
        "wins": wins, "losses": losses, "win_rate": win_rate,
        "never_triggered": never_triggered, "per_week": per_week,
        "weeks_spanned": round(weeks, 1),
    }


def print_report(rows: list, gate_ratios: list):
    log("=" * 78)
    log("BACKTEST V3 GATE — offline sensitivity report")
    log("=" * 78)
    log("")
    log("SCOPE NOTE (read this before the numbers below): this report can only")
    log("re-apply gate thresholds to setups the LIVE v3 pipeline already chose to")
    log("publish (and which have since resolved). It cannot reconstruct what would")
    log("have happened to candidates the live pipeline excluded before persisting")
    log("them -- there is no row for an excluded candidate to test against. This is")
    log("a threshold-sensitivity check on published setups, not a full replay of")
    log("the entire nightly candidate universe.")
    log("")

    if not rows:
        log("No resolved v3 rows found in nightly_setup_ideas_v3 -- nothing to backtest yet.")
        log("Run this again once the v3 test track has accumulated resolved setups.")
        return

    baseline = compute_baseline(rows)
    log(f"BASELINE (all {baseline['total_rows']} resolved row(s), spanning "
        f"~{baseline['weeks_spanned']} week(s), ~{baseline['per_week']}/week):")
    log(f"  {baseline['reportable_count']} reportable (win/loss) -- "
        f"{baseline['wins']}W / {baseline['losses']}L -- "
        f"win rate: {baseline['win_rate']}%" if baseline['win_rate'] is not None else
        f"  {baseline['reportable_count']} reportable — no win/loss rows to compute a rate from")
    log(f"  {baseline['never_triggered']} never_triggered (excluded from win rate)")
    log("")

    unknown_total = sum(1 for r in rows if approximate_missing_ratio(r)[0] is None)
    approx_total = sum(1 for r in rows if approximate_missing_ratio(r)[1] == "approximated_from_iv_rv")
    real_total = sum(1 for r in rows if approximate_missing_ratio(r)[1] == "real")
    log(f"req_exp_ratio coverage: {real_total} real, {approx_total} approximated from IV/RV "
        f"(coarse stand-in, see script docstring), {unknown_total} unknown (excluded from every "
        f"gate-ratio scenario below).")
    log("")

    log(f"{'Gate ratio':>10} | {'Win rate':>9} | {'vs baseline':>12} | {'Kept':>6} | {'Cut':>6} | {'Kept/wk':>8} | {'Cut/wk':>7}")
    log("-" * 78)
    for gr in gate_ratios:
        result = apply_gate_ratio(rows, gr)
        wr_str = f"{result['win_rate']}%" if result["win_rate"] is not None else "N/A"
        if result["win_rate"] is not None and baseline["win_rate"] is not None:
            delta = round(result["win_rate"] - baseline["win_rate"], 1)
            delta_str = f"{delta:+.1f}pp"
        else:
            delta_str = "N/A"
        log(f"{gr:>10.2f} | {wr_str:>9} | {delta_str:>12} | {result['kept_count']:>6} | "
            f"{result['cut_count']:>6} | {str(result['kept_per_week']):>8} | {str(result['cut_per_week']):>7}")
    log("-" * 78)
    log("")

    projected_lift = None
    for gr in gate_ratios:
        if abs(gr - 0.85) < 1e-9:
            result = apply_gate_ratio(rows, gr)
            if result["win_rate"] is not None and baseline["win_rate"] is not None:
                projected_lift = round(result["win_rate"] - baseline["win_rate"], 1)

    log("DEPLOYMENT GATE (per direct instruction): do not promote v3's selection")
    log("logic to production until this report shows a projected win-rate LIFT at")
    log("the 0.85 threshold (the value actually used in bmt_nightly_setups_v3_test.py).")
    if projected_lift is None:
        log("  -> 0.85 not in the gate-ratios tested, or insufficient data to compute a rate.")
        log("     Re-run with --gate-ratios including 0.85, or wait for more resolved history.")
    elif projected_lift > 0:
        log(f"  -> PASS: projected lift of {projected_lift:+.1f} percentage points at ratio 0.85.")
    else:
        log(f"  -> NOT YET: projected lift of {projected_lift:+.1f} percentage points at ratio 0.85 "
            f"(zero or negative) -- do not promote yet.")
    log("=" * 78)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate-ratios", default="0.5,0.6,0.7,0.75,0.8,0.85,0.9,1.0",
                         help="Comma-separated list of req_exp_ratio thresholds to test.")
    args = parser.parse_args()
    gate_ratios = [float(x.strip()) for x in args.gate_ratios.split(",") if x.strip()]

    rows = fetch_all_resolved_rows()
    print_report(rows, gate_ratios)


if __name__ == "__main__":
    main()