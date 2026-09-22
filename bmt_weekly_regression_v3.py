"""
bmt_weekly_regression_v3.py — ITEM 7: weekly learning loop for the v3
selection pipeline. FULLY ISOLATED FROM PRODUCTION:
  - Reads only from nightly_setup_ideas_v3 (never nightly_setup_ideas).
  - Writes only to scoring_weights_v3 (a brand-new table -- never any
    production scoring/weights table).

WHAT THIS DOES
Once a week (scheduled Sunday 6pm ET, per spec), reads every RESOLVED
row (status IN ('win','loss')) from nightly_setup_ideas_v3, fits a
logistic regression predicting win/loss from these features:
  - req_exp_ratio          (item 1's gate ratio; backfilled where computable)
  - flow_premium_over_advol (flow premium / avg_dollar_volume, item 3)
  - call_pct_deviation     (|call_pct - 50|, conviction strength)
  - iv_rv_ratio            (pricing richness/cheapness)
  - rvol                   (relative volume, nullable)
  - dte                    (days to expiry)
  - pattern                (categorical -- one-hot encoded)
  - is_mega_cap            (boolean flag)

Writes fitted coefficients to scoring_weights_v3(feature, weight,
fitted_at). bmt_nightly_setups_v3_test.py's main() does NOT currently
load these weights automatically (see NOT WIRED UP note below) -- this
script only fits and persists them for inspection and for a future,
explicit decision to wire them into compute_composite_score(). Per
direct user decision, this script is built fully now even though there
is little/no resolved history yet; run after a v3 setup has had time to
resolve (~1-2 weeks in), before the first run there is genuinely nothing
to fit and it exits cleanly, logging why.

SKLEARN AVAILABILITY: tries `from sklearn.linear_model import
LogisticRegression` first. If sklearn is unavailable on Railway (per
spec's explicit allowance), falls back to a small pure-Python logistic
regression fit via batch gradient descent on standardized features --
see PureLogisticRegression below. Both paths produce the same output
shape (a per-feature weight, z-scored, plus an intercept), so
downstream consumers don't need to know which path ran.

NOT WIRED UP INTO NIGHTLY SCORING (yet): the spec says "Nightly main()
loads weights if present and scores candidates as sum(w*z)... if the
table is empty, fall back to the current ranking_score formula." That
wiring is a change to bmt_nightly_setups_v3_test.py's
compute_composite_score(), not to this file. It is intentionally NOT
done in this first pass, alongside item 10's backtest script, because:
  1. There are zero resolved rows at first deploy -- nothing to fit yet.
  2. Per direct user instruction, item 10's backtest must show a
     projected win-rate lift on real history BEFORE any weight-driven
     scoring change ships -- doing the wiring now would be scoring
     changes deployed ahead of that validation gate.
This script's job for now is purely to fit and persist weights weekly
so that, once backtest_v3_gate.py can be run against enough real
resolved data, the decision to wire scoring_weights_v3 into
compute_composite_score() is a small, well-informed follow-up change --
not a rewrite.

RUN MODES (mirrors the other v3 scripts' *_MODE pattern):
  run       -> runs the fit immediately and exits
  (unset)   -> starts the persistent APScheduler service (Sunday 6pm ET)

Run locally:
  $env:REGRESSION_MODE = "run"
  C:\\Python314\\python.exe bmt_weekly_regression_v3.py
"""

import os
import sys
import time
import math
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

from apscheduler.schedulers.background import BackgroundScheduler
import pg8000.native as _pg8000

DATABASE_URL = os.environ.get("DATABASE_URL", "")
ET = ZoneInfo("America/New_York")

MIN_RESOLVED_ROWS = 20  # below this, a fit is too noisy to be worth writing


def log(msg: str):
    print(f"[REGRESSION-V3] {msg}", flush=True)


# ── DB ────────────────────────────────────────────────────────────────────
def _connect():
    p = urlparse(DATABASE_URL)
    return _pg8000.Connection(
        host=p.hostname, port=p.port or 5432,
        database=p.path.lstrip("/"),
        user=p.username, password=p.password,
    )


def ensure_schema():
    if not DATABASE_URL:
        log("DATABASE_URL not set -- weekly regression is fully disabled.")
        return
    conn = _connect()
    try:
        conn.run("""
            CREATE TABLE IF NOT EXISTS scoring_weights_v3 (
                feature TEXT PRIMARY KEY,
                weight DOUBLE PRECISION NOT NULL,
                fitted_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
    except Exception as e:
        log(f"[DB WARN] ensure_schema failed: {e}")
    finally:
        conn.close()


def fetch_resolved_rows() -> list:
    """Only rows with status IN ('win','loss') -- never_triggered rows
    have no meaningful win/loss label and are excluded from the fit,
    same as they're excluded from the results report."""
    if not DATABASE_URL:
        return []
    conn = _connect()
    try:
        rows = conn.run("""
            SELECT status, req_exp_ratio, flow_premium_over_advol, call_pct_deviation,
                   iv_rv_ratio, rvol, dte, pattern, is_mega_cap
            FROM nightly_setup_ideas_v3
            WHERE status IN ('win', 'loss')
        """)
        return [
            {
                "status": r[0], "req_exp_ratio": r[1], "flow_premium_over_advol": r[2],
                "call_pct_deviation": r[3], "iv_rv_ratio": r[4], "rvol": r[5],
                "dte": r[6], "pattern": r[7], "is_mega_cap": r[8],
            }
            for r in rows
        ]
    except Exception as e:
        log(f"[DB WARN] fetch_resolved_rows failed: {e}")
        return []
    finally:
        conn.close()


def write_scoring_weights(weights: dict):
    """weights: {feature_name: weight}. Overwrites any existing row per
    feature (fresh fit each week) -- ON CONFLICT upsert, one row per
    feature, fitted_at bumped to now() on every write."""
    if not DATABASE_URL:
        log("DATABASE_URL not set -- cannot persist weights.")
        return
    conn = _connect()
    try:
        for feature, weight in weights.items():
            conn.run("""
                INSERT INTO scoring_weights_v3 (feature, weight, fitted_at)
                VALUES (:feature, :weight, now())
                ON CONFLICT (feature) DO UPDATE SET weight = :weight, fitted_at = now()
            """, feature=feature, weight=float(weight))
        log(f"Wrote {len(weights)} feature weight(s) to scoring_weights_v3.")
    except Exception as e:
        log(f"[DB WARN] write_scoring_weights failed: {e}")
    finally:
        conn.close()


# ── Feature engineering ───────────────────────────────────────────────────

PATTERN_CATEGORIES = ["higher lows", "lower highs", "V-recovery", "breakdown"]


def build_feature_matrix(rows: list):
    """
    Builds (X, y, feature_names). Rows with entirely missing numeric
    features are dropped; individual missing numeric values are imputed
    with that feature's own column mean (a simple, defensible default
    for a first-pass weekly fit -- not claimed to be optimal). pattern
    is one-hot encoded across PATTERN_CATEGORIES (unknown/missing
    pattern -> all-zero row, i.e. an implicit baseline category).
    is_mega_cap is 0/1.

    y = 1 for 'win', 0 for 'loss'.
    """
    numeric_features = ["req_exp_ratio", "flow_premium_over_advol", "call_pct_deviation",
                         "iv_rv_ratio", "rvol", "dte"]

    # First pass: compute column means over available (non-None) values,
    # for imputation.
    col_values = {f: [] for f in numeric_features}
    for row in rows:
        for f in numeric_features:
            v = row.get(f)
            if v is not None:
                col_values[f].append(float(v))
    col_means = {f: (sum(vals) / len(vals) if vals else 0.0) for f, vals in col_values.items()}

    feature_names = numeric_features + [f"pattern_{p.replace(' ', '_')}" for p in PATTERN_CATEGORIES] + ["is_mega_cap"]

    X, y = [], []
    for row in rows:
        if row["status"] not in ("win", "loss"):
            continue
        feat_row = []
        for f in numeric_features:
            v = row.get(f)
            feat_row.append(float(v) if v is not None else col_means[f])
        pattern = row.get("pattern")
        for p in PATTERN_CATEGORIES:
            feat_row.append(1.0 if pattern == p else 0.0)
        feat_row.append(1.0 if row.get("is_mega_cap") else 0.0)
        X.append(feat_row)
        y.append(1 if row["status"] == "win" else 0)

    return X, y, feature_names


def zscore_columns(X: list):
    """Standardizes each column to mean 0, std 1 (guarding against a
    zero-variance column, which would otherwise divide by zero -- such
    a column just stays at 0 for every row, since it carries no
    discriminative information anyway). Returns (X_scaled, means, stds)
    so the same transform could be reapplied to new data later if
    needed."""
    if not X:
        return [], [], []
    n_features = len(X[0])
    means = [sum(row[j] for row in X) / len(X) for j in range(n_features)]
    stds = []
    for j in range(n_features):
        variance = sum((row[j] - means[j]) ** 2 for row in X) / len(X)
        stds.append(math.sqrt(variance) if variance > 1e-12 else 1.0)
    X_scaled = [[(row[j] - means[j]) / stds[j] for j in range(n_features)] for row in X]
    return X_scaled, means, stds


# ── Pure-Python logistic regression fallback ─────────────────────────────

class PureLogisticRegression:
    """Minimal batch-gradient-descent logistic regression, used only if
    sklearn is unavailable. Not a general-purpose implementation --
    scoped exactly to this script's needs (small feature count, small
    row count expected for the foreseeable future of this test track)."""

    def __init__(self, lr=0.1, n_iter=2000, l2=0.01):
        self.lr = lr
        self.n_iter = n_iter
        self.l2 = l2
        self.coef_ = None
        self.intercept_ = 0.0

    def fit(self, X: list, y: list):
        n_samples = len(X)
        n_features = len(X[0]) if X else 0
        w = [0.0] * n_features
        b = 0.0
        for _ in range(self.n_iter):
            grad_w = [0.0] * n_features
            grad_b = 0.0
            for i in range(n_samples):
                z = sum(w[j] * X[i][j] for j in range(n_features)) + b
                pred = 1.0 / (1.0 + math.exp(-z)) if abs(z) < 700 else (1.0 if z > 0 else 0.0)
                error = pred - y[i]
                for j in range(n_features):
                    grad_w[j] += error * X[i][j]
                grad_b += error
            for j in range(n_features):
                w[j] = w[j] - self.lr * (grad_w[j] / n_samples + self.l2 * w[j])
            b = b - self.lr * (grad_b / n_samples)
        self.coef_ = [w]
        self.intercept_ = b
        return self


def fit_logistic_regression(X_scaled: list, y: list):
    """Returns (coefficients_list, intercept, backend_name). Tries
    sklearn first, falls back to PureLogisticRegression if unavailable,
    per spec's explicit allowance ('sklearn, or pure-Python if sklearn
    is unavailable on Railway')."""
    try:
        from sklearn.linear_model import LogisticRegression
        model = LogisticRegression(max_iter=1000, C=1.0)
        model.fit(X_scaled, y)
        return list(model.coef_[0]), float(model.intercept_[0]), "sklearn"
    except ImportError:
        log("sklearn not available -- falling back to pure-Python logistic regression.")
        model = PureLogisticRegression()
        model.fit(X_scaled, y)
        return list(model.coef_[0]), float(model.intercept_), "pure-python"


# ── Main job ──────────────────────────────────────────────────────────────
def run_weekly_regression():
    now = datetime.now(ET)
    log(f"[{now.isoformat()}] Running weekly v3 scoring regression...")

    if not DATABASE_URL:
        log("DATABASE_URL not set -- nothing to fit.")
        return

    ensure_schema()
    rows = fetch_resolved_rows()
    log(f"Fetched {len(rows)} resolved v3 row(s) (win/loss only, never_triggered excluded).")

    if len(rows) < MIN_RESOLVED_ROWS:
        log(f"Only {len(rows)} resolved row(s) -- below the minimum of {MIN_RESOLVED_ROWS} needed for a "
            f"non-noisy fit. Skipping this week; nightly scoring continues on the current ranking_score "
            f"formula (scoring_weights_v3 stays empty or unchanged) until enough history accumulates.")
        return

    wins = sum(1 for r in rows if r["status"] == "win")
    losses = len(rows) - wins
    if wins == 0 or losses == 0:
        log(f"All {len(rows)} resolved row(s) are the same class ({wins} win, {losses} loss) -- "
            f"logistic regression needs both classes present. Skipping this week.")
        return

    X, y, feature_names = build_feature_matrix(rows)
    X_scaled, means, stds = zscore_columns(X)

    coefs, intercept, backend = fit_logistic_regression(X_scaled, y)
    log(f"Fit complete via {backend} on {len(X)} row(s), {len(feature_names)} feature(s).")

    weights = dict(zip(feature_names, coefs))
    weights["__intercept__"] = intercept
    for name, w in weights.items():
        log(f"  {name}: {w:+.4f}")

    write_scoring_weights(weights)


run_weekly_regression_job = run_weekly_regression


# ── Scheduler ─────────────────────────────────────────────────────────────
def start_scheduler():
    scheduler = BackgroundScheduler(timezone="America/New_York")
    scheduler.add_job(run_weekly_regression_job, "cron", day_of_week="sun", hour=18, minute=0,
                       id="weekly_regression_v3", replace_existing=True, max_instances=1)
    scheduler.start()
    log("Scheduler started: weekly v3 scoring regression fires Sundays at 6:00pm ET.")

    def heartbeat():
        while True:
            time.sleep(900)
            log(f"[HEARTBEAT] scheduler running={scheduler.running}")

    threading.Thread(target=heartbeat, daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    mode = os.environ.get("REGRESSION_MODE", "scheduler").lower()
    log(f"BMT Weekly Regression V3 starting (mode={mode})...")
    if mode == "run":
        run_weekly_regression_job()
    else:
        start_scheduler()