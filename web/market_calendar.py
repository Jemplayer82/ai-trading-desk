"""US equity market calendar and the Eastern-time clock, in one place.

Tier-agnostic and pure stdlib: this module ships at every tier and must not
import anything from ``web.*``.

The NYSE holiday list below is hand-maintained from
https://www.nyse.com/markets/hours-calendars and should be revisited
annually. ``is_trading_day`` logs a warning (once per year per process) when
asked about a year outside ``HOLIDAY_YEARS_COVERED``, and a unit test fails
once the real ET year passes the last covered year. The ``MARKET_HOLIDAYS_EXTRA`` env var (comma-separated ISO dates,
read at call time) adds closure dates, e.g. an unscheduled closure; it can
only add dates, never remove them.

CONVENTION: callers use these functions THROUGH THE MODULE
(``market_calendar.now_et()``), never by from-import. That gives tests
exactly one patch point: ``monkeypatch.setattr(market_calendar, "now_et", ...)``.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone, tzinfo

log = logging.getLogger(__name__)

try:  # tzdata may be absent on a bare Windows dev host — date math only needs ~ET
    from zoneinfo import ZoneInfo

    _ET: tzinfo = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - environment dependent
    _ET = timezone(timedelta(hours=-5))

# Regular-session window used by the desk, (hour, minute) in ET. Opens at
# 09:35 rather than 09:30 so opening-auction quotes have settled.
MARKET_OPEN_ET: tuple[int, int] = (9, 35)
MARKET_CLOSE_ET: tuple[int, int] = (16, 0)

NYSE_HOLIDAYS: frozenset[date] = frozenset({
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
})

# Years NYSE_HOLIDAYS is known to be complete for. Outside this range every
# weekday holiday reads as a trading day, so is_trading_day warns.
HOLIDAY_YEARS_COVERED: frozenset[int] = frozenset(d.year for d in NYSE_HOLIDAYS)
LAST_COVERED_YEAR: int = max(HOLIDAY_YEARS_COVERED)

_warned_uncovered_years: set[int] = set()


def _warn_if_year_uncovered(year: int) -> None:
    if year in HOLIDAY_YEARS_COVERED or year in _warned_uncovered_years:
        return
    _warned_uncovered_years.add(year)
    log.warning(
        "NYSE_HOLIDAYS has no entries for %d (covers %d-%d): weekday holidays will "
        "be treated as trading days. Update web/market_calendar.py or set "
        "MARKET_HOLIDAYS_EXTRA.",
        year, min(HOLIDAY_YEARS_COVERED), LAST_COVERED_YEAR,
    )


def now_et() -> datetime:
    return datetime.now(_ET)


def today_et() -> date:
    return now_et().date()


def extra_holidays() -> frozenset[date]:
    """Dates from ``MARKET_HOLIDAYS_EXTRA``; malformed entries are logged and skipped."""
    raw = os.environ.get("MARKET_HOLIDAYS_EXTRA", "")
    out: set[date] = set()
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        try:
            out.add(date.fromisoformat(entry))
        except ValueError:
            log.warning("MARKET_HOLIDAYS_EXTRA: ignoring malformed date %r", entry)
    return frozenset(out)


def is_trading_day(d: date | datetime | None = None) -> bool:
    if d is None:
        d = today_et()
    elif isinstance(d, datetime):  # before the date check: datetime subclasses date
        d = d.date()
    _warn_if_year_uncovered(d.year)
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS and d not in extra_holidays()


def is_market_open_now(now: datetime | None = None) -> bool:
    now = now or now_et()
    return is_trading_day(now.date()) and MARKET_OPEN_ET <= (now.hour, now.minute) < MARKET_CLOSE_ET
