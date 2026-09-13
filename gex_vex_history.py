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
    actionable "look forward to this" signal -- always the headline
    when it happens, translated into plain English (more/less
    cushion against big moves) with a concrete instruction (size
    down, don't chase, etc.), not raw "LONG -> SHORT" labels.
  - Call/put level shifts: surfaced when the move is large enough to
    matter (see WALL_SHIFT_THRESHOLD_PCT below), reframed as "don't
    anchor on yesterday's number, use today's instead" rather than a
    bare dollar-to-dollar delta.
  - When neither of the above applies: an explicit plain-English
    reassurance that yesterday's setup still holds, rather than
    silence -- a missing section reads as ambiguous ("did this break?
    did nobody check?"), while an explicit "nothing changed, same
    levels still apply" is itself useful, actionable information for
    a reader deciding whether to keep watching the same numbers.

REWRITTEN 2026-08-13 to match the same plain-English, action-first
standard already enforced on gex_vex.py's "What To Watch" section
(see that module's WATCH-LINE ACTIONABILITY FIX / PLAIN-ENGLISH FIX
docstring notes) -- confirmed directly by the user that raw technical
deltas ("Call wall moved down: $510 -> $500") told a layman WHAT
changed but not what it meant or what to do about it. Every branch of
build_since_yesterday_line() now ends in a concrete takeaway, and no
raw jargon ("wall", "regime", "gamma flip") appears in the output --
same banned-word posture as the rest of the GEX pipeline's subscriber-
facing text.

TEMPLATE-VARIETY BUGFIX (2026-09-13): confirmed directly by the user,
comparing two real posted "Today's Focus" REGIME FLIP entries on the
same day, that MSFT's and GOOGL's paragraphs were near word-for-word
identical -- both flipped the SAME direction (long -> short) the same
day, and build_since_yesterday_line() has exactly ONE hardcoded string
per flip direction, so any two tickers sharing a direction on the same
day always produced the literal same paragraph (only the ticker name
and the opening spot-change clause differed, via spot_clause()). This
is NOT an LLM -- this whole function is deterministic Python string
building, so the "templated" complaint was about REAL template reuse,
not an LLM parroting itself.

FIX: each flip direction now has FIVE phrasing variants instead of one
(widened from an initial 3 -- see the 2026-09-13 same-day follow-up
note below). A ticker's variant is chosen deterministically from its
own symbol (see _pick_variant() below) -- the SAME ticker always gets
the same variant day to day (so its voice doesn't feel randomly
inconsistent), but DIFFERENT tickers very likely land on different
variants, so two tickers flipping the same direction on the same day
no longer produce identical text. Each variant also weaves in the
ticker's actual gamma_flip level when available (not just the ticker
name), which further differentiates two same-direction tickers whose
pivot levels genuinely differ -- concrete numbers, not just varied
wording, same principle already used in bmt_nightly_setups.py's own
narrative-variety fix (lean on the specific real numbers already
available, not just categorical language). All five variants for a
given direction say the exact same real thing (same mechanism, same
instruction) -- only sentence structure and phrasing differ.

SAME-DAY FOLLOW-UP (2026-09-13): a real mock render caught a residual
collision even after the above fix -- SPY and QQQ, both flipping the
same direction, both landed on the SAME variant (3 variants means
roughly 1-in-3 odds for any pair) AND both had gamma_flip=None (a
completely normal, common occurrence, not a data error), so
_flip_level_clause() returned an empty string for both -- meaning the
one piece of the sentence meant to carry ticker-specific numbers
carried nothing for either, and they ended up identical after the
variant text anyway. Two independent fixes: (1) variant count raised
from 3 to 5, lowering collision odds further; (2) _flip_level_clause()
now falls back to the nearer of put_wall/call_wall when gamma_flip is
unavailable, so the clause is non-empty far more often instead of
depending entirely on a field that's frequently None.
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


def _pick_variant(ticker: str, n: int) -> int:
    """
    TEMPLATE-VARIETY BUGFIX (2026-09-13): deterministic per-ticker
    variant selector -- see module docstring for the full production
    incident (MSFT/GOOGL posting word-for-word identical paragraphs on
    the same flip direction). The SAME ticker always gets the same
    variant index across runs (so its voice doesn't feel randomly
    inconsistent day to day), but different tickers very likely land
    on different variants, since this hashes the ticker symbol itself
    rather than the date or any shared input.
    """
    return sum(ord(c) for c in ticker) % n


def _flip_level_clause(today: dict) -> str:
    """
    Small optional clause naming a real concrete number, when available,
    so two tickers sharing both a flip direction AND a phrasing variant
    still read as concretely different -- their real numbers differ
    even when their sentence structure doesn't.

    FALLBACK FIX (2026-09-13): confirmed via a real mock render that
    SPY and QQQ -- both flipping the same direction the same day, both
    with gamma_flip=None (a completely normal, common occurrence, not
    an error) -- produced an EMPTY level_bit for both, which was the
    deeper reason they still collided even after the variant fix: two
    empty strings are identical no matter how many variants exist.
    Falls back to whichever of put_wall/call_wall sits closer to spot
    when gamma_flip isn't available, so this clause is far more often
    non-empty than before.
    """
    flip = today.get("gamma_flip")
    if flip is not None:
        return f" (around {flip:,.2f})"

    spot = today.get("spot")
    candidates = [w for w in (today.get("put_wall"), today.get("call_wall")) if w is not None]
    if not candidates or spot is None:
        return ""
    nearest = min(candidates, key=lambda w: abs(w - spot))
    return f" (near {nearest:,.2f})"


def build_since_yesterday_line(ticker: str, today: dict, yesterday: dict) -> str:
    """
    Returns a short, PLAIN-ENGLISH, action-oriented "Since Yesterday"
    paragraph for one ticker, or an empty string if there's truly
    nothing to report (no prior snapshot exists at all).

    REWRITTEN (2026-08-13): the original version printed raw technical
    deltas ("Call wall moved down: $510 -> $500", "Regime flipped:
    LONG -> SHORT gamma") with an arrow and a number -- accurate, but
    it left a layman reader with no idea what any of it means or what
    to actually do about it, which breaks the same bar already
    enforced on the "What To Watch" section in gex_vex.py (see that
    module's WATCH-LINE ACTIONABILITY FIX and PLAIN-ENGLISH FIX
    docstring notes -- no trading jargon, every line explains the
    real-world implication and ends in a concrete instruction). This
    rewrite applies the identical standard here: same banned-jargon
    posture ("wall"->"level", "regime"->plain description, "gamma
    flip"->"pivot point"), same self-correcting/less-cushion framing
    already used elsewhere in the GEX pipeline for regime behavior,
    and every branch ends in something the reader can actually act on.

    TEMPLATE-VARIETY BUGFIX (2026-09-13): see module docstring. The
    regime-flip branch (Priority 1) now picks from THREE phrasing
    variants per direction via _pick_variant(), instead of the single
    hardcoded string each direction previously had.

    Priority order (a ticker only gets ONE of these, not a bullet list
    of all that apply -- a single clear paragraph beats a stacked
    checklist for a layman reader):
      1. Regime flip -- always the headline when it happens, since it
         changes how the ticker tends to BEHAVE, not just where a
         level sits.
      2. A meaningfully shifted call/put level -- reframed as "the
         level to watch moved, don't anchor on yesterday's number."
      3. Nothing structural changed -- an explicit reassurance that
         yesterday's levels still apply, so the reader isn't left
         wondering whether the silence means something broke.
    """
    if not yesterday:
        return ""

    spot_today = today.get("spot")
    spot_yday = yesterday.get("spot")
    spot_change_pct = None
    if spot_today is not None and spot_yday:
        spot_change_pct = (spot_today - spot_yday) / spot_yday * 100

    def spot_clause() -> str:
        if spot_change_pct is None:
            return ""
        direction = "up" if spot_change_pct >= 0 else "down"
        magnitude = "sharply" if abs(spot_change_pct) >= 2 else ("a bit" if abs(spot_change_pct) >= 0.5 else "barely")
        return f"{ticker} is {direction} {magnitude} ({abs(spot_change_pct):.1f}%) from yesterday's close"

    # --- Priority 1: regime flip -- always the headline -----------------
    net_gex_today = today.get("net_gex")
    net_gex_yday = yesterday.get("net_gex")
    if net_gex_today is not None and net_gex_yday is not None:
        was_long = net_gex_yday >= 0
        is_long = net_gex_today >= 0
        if was_long != is_long:
            spot_bit = spot_clause()
            spot_prefix = f"{spot_bit}, and t" if spot_bit else "T"
            level_bit = _flip_level_clause(today)
            variant = _pick_variant(ticker, 5)

            if is_long:
                # short -> long: MORE contained now
                variants = [
                    (f"{spot_prefix}his one has calmed down since yesterday \u2014 it now has more of a "
                     f"built-in cushion that tends to pull price back toward the middle if it swings too "
                     f"far in either direction. That means big breakout moves are less likely to stick "
                     f"today than they were yesterday, so it's a better setup for trading the range than "
                     f"chasing a big move."),
                    (f"{spot_prefix}his setup shifted calmer overnight{level_bit} \u2014 there's real "
                     f"resistance now to a runaway move in either direction, so a push toward either edge "
                     f"of today's range is more likely to get bought/sold back than to keep going. Fits "
                     f"better with fading extremes than swinging for a breakout today."),
                    (f"{spot_prefix}his positioning turned more contained since yesterday{level_bit} \u2014 "
                     f"a move past either edge now has something pulling it back toward the middle rather "
                     f"than running away. Today favors trading the range over chasing a big move, since a "
                     f"spike past either level is more likely to snap back than extend."),
                    (f"{spot_prefix}his one flipped into a steadier setup overnight{level_bit} \u2014 price "
                     f"now has more of a natural brake on it, so a fast move in either direction is more "
                     f"likely to fade than follow through. Good day to lean on the range rather than chase "
                     f"a breakout."),
                    (f"{spot_prefix}his one is behaving differently than it did yesterday{level_bit} \u2014 "
                     f"the options market is now leaning toward pulling price back toward the middle rather "
                     f"than letting a move run, so today's breakouts are less trustworthy than they were. "
                     f"Trade the bounce, not the breakout."),
                ]
            else:
                # long -> short: LESS contained now
                variants = [
                    (f"{spot_prefix}his one has less of a cushion against bigger moves than it did "
                     f"yesterday \u2014 if it breaks past a key level today, the move could travel "
                     f"further and faster than you'd expect from a normal day. Keep position sizes "
                     f"smaller than usual and don't assume a dip gets bought right away."),
                    (f"{spot_prefix}his setup shifted more exposed overnight{level_bit} \u2014 the "
                     f"cushion that was keeping moves contained yesterday isn't there today, so a break "
                     f"past a key level has real room to run further than usual. Size down and don't "
                     f"assume a pullback shows up on cue."),
                    (f"{spot_prefix}his positioning turned less stable since yesterday{level_bit} \u2014 "
                     f"there's less to slow a move down once it clears a nearby level, so today's swings "
                     f"could extend further and faster than a typical session. Trade smaller and stay "
                     f"ready for a move that doesn't get bought back quickly."),
                    (f"{spot_prefix}his one lost some of its natural brake overnight{level_bit} \u2014 "
                     f"once price clears a nearby level today, there's less standing in the way of it "
                     f"continuing, so a breakout has a better chance of actually sticking than it did "
                     f"yesterday. Keep size in check."),
                    (f"{spot_prefix}his one is more exposed to a real move today than it was "
                     f"yesterday{level_bit} \u2014 the options market isn't leaning toward pulling price "
                     f"back the way it was, so a break past a nearby level could travel further than "
                     f"usual. Don't fade this one as confidently as you might have yesterday."),
                ]
            return variants[variant]

    # --- Priority 2: a level shifted meaningfully ------------------------
    for label_up, label_down, key in (
        ("ceiling", "ceiling", "call_wall"),
        ("floor", "floor", "put_wall"),
    ):
        today_val = today.get(key)
        yday_val = yesterday.get(key)
        spot = spot_today or spot_yday
        if today_val is not None and yday_val is not None and spot:
            shift_pct = abs(today_val - yday_val) / spot * 100
            if shift_pct >= WALL_SHIFT_THRESHOLD_PCT:
                import gex_vex
                direction_word = "higher" if today_val > yday_val else "lower"
                spot_bit = spot_clause()
                spot_prefix = f"{spot_bit}. T" if spot_bit else "T"
                return (f"{spot_prefix}he {label_up} level to watch moved {direction_word} today, from "
                        f"{gex_vex.format_strike(yday_val)} yesterday to {gex_vex.format_strike(today_val)} "
                        f"now \u2014 if you were anchored on yesterday's number, use today's instead, since "
                        f"that's the level actually in play right now.")
        elif today_val is not None and yday_val is None:
            import gex_vex
            spot_bit = spot_clause()
            spot_prefix = f"{spot_bit}. A" if spot_bit else "A"
            return (f"{spot_prefix} clear {label_up} level has shown up today at "
                    f"{gex_vex.format_strike(today_val)} that wasn't there yesterday \u2014 worth keeping "
                    f"an eye on if price approaches it.")
        elif today_val is None and yday_val is not None:
            import gex_vex
            spot_bit = spot_clause()
            spot_prefix = f"{spot_bit}. T" if spot_bit else "T"
            return (f"{spot_prefix}he {label_up} level from yesterday ({gex_vex.format_strike(yday_val)}) "
                    f"isn't showing up as clearly today \u2014 there's less of a defined level overhead/below "
                    f"right now, so don't rely on that old number.")

    # --- Priority 3: nothing structural changed --------------------------
    spot_bit = spot_clause()
    if spot_bit:
        return f"{spot_bit}, but the levels to watch haven't meaningfully changed \u2014 yesterday's setup still applies."
    return "Nothing meaningfully changed since yesterday \u2014 the same levels and setup still apply."


def get_comparison_summary(r: dict, today_date: date) -> dict:
    """
    NEW (2026-08-13), purely additive -- does not modify save_and_compare(),
    build_since_yesterday_line(), save_snapshot(), or get_previous_snapshot()
    in any way, just calls them.

    WHY THIS EXISTS: the unified single-card GEX/VEX dashboard needs BOTH
    a compact, glanceable delta (a small "vs yesterday" % and a flip icon
    drawn directly on the card) AND the full plain-English paragraph (for
    the "Today's Focus" panel, only when something is actually notable)
    -- without duplicating the flip-detection/threshold logic that
    already lives in build_since_yesterday_line(). This is READ-ONLY (it
    does not call save_snapshot() itself -- the caller is expected to
    save the snapshot separately, same as before).

    Returns:
      {
        "has_comparison": bool,               # False if no prior snapshot exists yet
        "spot_change_pct": float | None,
        "regime_flipped": bool,
        "flip_direction": "to_long" | "to_short" | None,
        "plain_text": str,                    # same paragraph build_since_yesterday_line() returns
      }
    """
    ticker = r["ticker"]
    yesterday = get_previous_snapshot(ticker, today_date)
    if not yesterday:
        return {"has_comparison": False, "spot_change_pct": None, "regime_flipped": False,
                "flip_direction": None, "plain_text": ""}

    spot_today = r.get("spot")
    spot_yday = yesterday.get("spot")
    spot_change_pct = None
    if spot_today is not None and spot_yday:
        spot_change_pct = (spot_today - spot_yday) / spot_yday * 100

    regime_flipped = False
    flip_direction = None
    net_gex_today = r.get("net_gex")
    net_gex_yday = yesterday.get("net_gex")
    if net_gex_today is not None and net_gex_yday is not None:
        was_long = net_gex_yday >= 0
        is_long = net_gex_today >= 0
        if was_long != is_long:
            regime_flipped = True
            flip_direction = "to_long" if is_long else "to_short"

    return {
        "has_comparison": True,
        "spot_change_pct": spot_change_pct,
        "regime_flipped": regime_flipped,
        "flip_direction": flip_direction,
        "plain_text": build_since_yesterday_line(ticker, r, yesterday),
    }


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