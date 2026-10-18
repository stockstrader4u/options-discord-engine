-- v1_conviction.sql -- rule-based conviction tier + per-flow option outcomes (PostgreSQL).
-- Idempotent: safe to run any number of times. The app applies the same steps at startup via
-- db.ensure_conviction_schema(); this file is the by-hand equivalent.
--
-- conviction_rule_version: NULL = legacy (label derived from the score thresholds 80/70/60),
--                          1    = rule-based tier from conviction.py (Very High / High / Standard).
-- Evaluation windows must split on this column: the two labels mean different things.

ALTER TABLE flow_events ADD COLUMN IF NOT EXISTS jarvis_conviction        TEXT;
ALTER TABLE flow_events ADD COLUMN IF NOT EXISTS conviction_label         TEXT;
ALTER TABLE flow_events ADD COLUMN IF NOT EXISTS conviction_rule_version  INTEGER;
ALTER TABLE flow_events ADD COLUMN IF NOT EXISTS conviction_detail        JSONB;   -- baseline B1-B6, signals S1-S6, tier

-- One row per published flow. Keyed by flow_event_id, NOT alert_hash: alert_hash is a hash of
-- ticker|contract|source|flow_type|sentiment, so the same contract reposted later (or on another day)
-- shares a hash and would silently overwrite its earlier row.
CREATE TABLE IF NOT EXISTS flow_option_outcomes (
    flow_event_id          INTEGER PRIMARY KEY,
    alert_hash             TEXT NOT NULL,
    ticker                 TEXT NOT NULL,
    contract               TEXT,
    put_call               TEXT,
    strike                 REAL,
    expiry_date            TEXT,
    alerted_at             TIMESTAMPTZ,
    alert_px               REAL,
    high_px                REAL,
    low_px                 REAL,
    last_px                REAL,
    high_pct               REAL,
    low_pct                REAL,
    last_pct               REAL,
    hit_50_before_minus_50 TEXT CHECK (hit_50_before_minus_50 IN ('yes','no','neither','both')),
    time_to_peak_min       REAL,
    expired                BOOLEAN,
    bars_ok                BOOLEAN,
    computed_at            TIMESTAMPTZ DEFAULT NOW(),
    result_posted_at       TIMESTAMPTZ   -- set when the flow's expiry-result card was posted to subscribers
);
ALTER TABLE flow_option_outcomes ADD COLUMN IF NOT EXISTS result_posted_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_fo_outcomes_ticker_time ON flow_option_outcomes (ticker, alerted_at);
CREATE INDEX IF NOT EXISTS idx_fo_outcomes_computed    ON flow_option_outcomes (computed_at);

-- Backfill Jarvis's own label from the note ("JarvisFlow | BOUGHT CALL | OTM | Conviction: HIGH").
UPDATE flow_events
   SET jarvis_conviction = upper((regexp_match(note, 'Conviction:\s*([A-Za-z]+)'))[1])
 WHERE jarvis_conviction IS NULL
   AND note ~ 'Conviction:\s*[A-Za-z]+';

-- Backfill the legacy label (OLD score thresholds) so history stays queryable. Rule version stays NULL.
UPDATE flow_events
   SET conviction_label = CASE WHEN score >= 80 THEN 'Very High'
                               WHEN score >= 70 THEN 'High'
                               WHEN score >= 60 THEN 'Medium'
                               ELSE 'Low' END
 WHERE conviction_label IS NULL AND score IS NOT NULL;
