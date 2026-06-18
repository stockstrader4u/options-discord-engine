"""
market_hours.py — US stock market (NYSE) regular session gate.

is_market_open() checks all three of:
  - Weekday (Mon-Fri)
  - Within regular session hours: 9:30 AM - 4:00 PM US Eastern time
  - Not a NYSE market holiday

NYSE holidays are computed by RULE, not a hardcoded date list, so this
doesn't go stale year to year:
  - Fixed-date holidays (New Year's, Juneteenth, Independence Day, Christmas)
    shift per NYSE convention: Saturday -> observed Friday, Sunday -> observed
    Monday.
  - Floating holidays (MLK, Presidents, Memorial, Labor, Thanksgiving) are
    computed as "nth weekday of month".
  - Good Friday is computed from the standard Gregorian Easter algorithm.

Requires the IANA timezone database to be available for zoneinfo. On Linux
(Railway) this is normally present system-wide. On Windows, Python's
zoneinfo needs the `tzdata` package installed (pip install tzdata) — this is
listed in requirements.txt for that reason. If tzdata isn't available, this
raises clearly rather than silently falling back to the wrong timezone.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    EASTERN = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError as e:  # pragma: no cover
    raise RuntimeError(
        "Timezone data for 'America/New_York' not found. "
        "On Windows, run: pip install tzdata --break-system-packages "
        "(or just `pip install tzdata` inside your venv)."
    ) from e

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


# ---------------------------------------------------------------------------
# Date rule helpers
# ---------------------------------------------------------------------------

def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """weekday: Monday=0 ... Sunday=6. n: 1-indexed occurrence (1st, 2nd, ...)."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    d = d + timedelta(days=offset)
    return d + timedelta(weeks=n - 1)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    d = next_month - timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher) for Easter Sunday."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    """NYSE convention: Saturday holiday observed Friday, Sunday observed Monday."""
    if d.weekday() == 5:  # Saturday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> set[date]:
    """All NYSE full-market-closure holidays for a given year, with weekend observance."""
    return {
        _observed(date(year, 1, 1)),                       # New Year's Day
        _nth_weekday_of_month(year, 1, 0, 3),               # MLK Day (3rd Mon Jan)
        _nth_weekday_of_month(year, 2, 0, 3),               # Presidents Day (3rd Mon Feb)
        _easter_sunday(year) - timedelta(days=2),           # Good Friday
        _last_weekday_of_month(year, 5, 0),                 # Memorial Day (last Mon May)
        _observed(date(year, 6, 19)),                       # Juneteenth
        _observed(date(year, 7, 4)),                        # Independence Day
        _nth_weekday_of_month(year, 9, 0, 1),                # Labor Day (1st Mon Sep)
        _nth_weekday_of_month(year, 11, 3, 4),               # Thanksgiving (4th Thu Nov)
        _observed(date(year, 12, 25)),                      # Christmas
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _to_eastern(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(EASTERN)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # assume naive datetimes are UTC
    return dt.astimezone(EASTERN)


def is_market_open(dt: datetime | None = None) -> bool:
    """
    True if dt (defaults to now) falls within NYSE regular trading hours:
    Mon-Fri, 9:30 AM - 4:00 PM US Eastern, and not a NYSE holiday.
    """
    dt_et = _to_eastern(dt)

    if dt_et.weekday() >= 5:  # Sat/Sun
        return False
    if dt_et.date() in nyse_holidays(dt_et.year):
        return False
    return MARKET_OPEN <= dt_et.time() < MARKET_CLOSE


def market_closed_reason(dt: datetime | None = None) -> str | None:
    """Human-readable reason the market is closed at dt, or None if it's open."""
    dt_et = _to_eastern(dt)

    if dt_et.weekday() >= 5:
        return f"weekend ({dt_et.strftime('%A')})"
    if dt_et.date() in nyse_holidays(dt_et.year):
        return f"market holiday ({dt_et.date().isoformat()})"
    if not (MARKET_OPEN <= dt_et.time() < MARKET_CLOSE):
        return f"outside regular session hours ({dt_et.strftime('%H:%M')} ET)"
    return None
