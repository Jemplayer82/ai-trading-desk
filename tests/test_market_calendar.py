"""Unit tests for web/market_calendar.py — trading days, NYSE holidays, the
MARKET_HOLIDAYS_EXTRA override, the ET clock patch point, and the session window."""

from datetime import date, datetime

import pytest

from web import market_calendar

pytestmark = pytest.mark.unit

HOLIDAYS_2026 = [
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
]

TUESDAY = date(2026, 9, 29)
SATURDAY = date(2026, 9, 26)


@pytest.fixture(autouse=True)
def _no_extra_holidays(monkeypatch):
    monkeypatch.delenv("MARKET_HOLIDAYS_EXTRA", raising=False)


@pytest.fixture(autouse=True)
def _reset_uncovered_year_warnings(monkeypatch):
    monkeypatch.setattr(market_calendar, "_warned_uncovered_years", set())


def _et(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=market_calendar._ET)


def test_weekend_is_not_trading_day():
    assert market_calendar.is_trading_day(SATURDAY) is False
    assert market_calendar.is_trading_day(date(2026, 9, 27)) is False


@pytest.mark.parametrize("holiday", HOLIDAYS_2026, ids=lambda d: d.isoformat())
def test_2026_holidays_are_not_trading_days(holiday):
    assert market_calendar.is_trading_day(holiday) is False


def test_regular_weekday_is_trading_day():
    assert market_calendar.is_trading_day(TUESDAY) is True


def test_datetime_argument_uses_its_date():
    assert market_calendar.is_trading_day(_et(2026, 9, 29, 23, 59)) is True
    assert market_calendar.is_trading_day(_et(2026, 9, 26, 10, 0)) is False


def test_extra_holidays_env_adds_dates_and_skips_garbage(monkeypatch):
    monkeypatch.setenv("MARKET_HOLIDAYS_EXTRA", "2026-09-29, garbage,")
    assert market_calendar.extra_holidays() == frozenset({TUESDAY})
    assert market_calendar.is_trading_day(TUESDAY) is False


def test_clock_types():
    today = market_calendar.today_et()
    assert isinstance(today, date)
    assert not isinstance(today, datetime)
    assert market_calendar.now_et().tzinfo is not None


def test_market_open_constant():
    assert market_calendar.MARKET_OPEN_ET == (9, 35)


def test_patching_now_et_moves_today_and_is_trading_day(monkeypatch):
    monkeypatch.setattr(market_calendar, "now_et", lambda: _et(2026, 9, 26, 10, 0))
    assert market_calendar.today_et() == SATURDAY
    assert market_calendar.is_trading_day() is False

    monkeypatch.setattr(market_calendar, "now_et", lambda: _et(2026, 9, 29, 10, 0))
    assert market_calendar.today_et() == TUESDAY
    assert market_calendar.is_trading_day() is True


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (_et(2026, 9, 29, 9, 34), False),
        (_et(2026, 9, 29, 9, 35), True),
        (_et(2026, 9, 29, 16, 0), False),
        (_et(2026, 9, 26, 10, 0), False),
    ],
    ids=["tue-0934", "tue-0935", "tue-1600", "sat-1000"],
)
def test_is_market_open_now(when, expected):
    assert market_calendar.is_market_open_now(when) is expected


def test_options_data_today_follows_patched_calendar_clock(monkeypatch):
    # options_data is stripped below tier 4; this file ships at every tier.
    options_data = pytest.importorskip("web.options_data")
    monkeypatch.setattr(market_calendar, "now_et", lambda: _et(2026, 9, 29, 10, 0))
    assert options_data.today_et() == TUESDAY
    assert options_data.now_et() == _et(2026, 9, 29, 10, 0)


_WARN_LOGGER = "web.market_calendar"


def _uncovered_warnings(caplog):
    return [r for r in caplog.records if r.name == _WARN_LOGGER and "NYSE_HOLIDAYS" in r.getMessage()]


def test_uncovered_year_warns_once_per_year(caplog):
    caplog.set_level("WARNING", logger=_WARN_LOGGER)
    mlk_2028 = date(2028, 1, 17)
    assert mlk_2028.year > market_calendar.LAST_COVERED_YEAR
    market_calendar.is_trading_day(mlk_2028)
    market_calendar.is_trading_day(date(2028, 2, 21))
    warnings = _uncovered_warnings(caplog)
    assert len(warnings) == 1
    assert "2028" in warnings[0].getMessage()


def test_covered_year_does_not_warn(caplog):
    caplog.set_level("WARNING", logger=_WARN_LOGGER)
    market_calendar.is_trading_day(TUESDAY)
    market_calendar.is_trading_day(date(2027, 1, 18))
    assert _uncovered_warnings(caplog) == []


def test_holiday_calendar_covers_current_year():
    """Maintenance guard: fails once the real ET year outruns NYSE_HOLIDAYS.
    Add the next year's closures from nyse.com/markets/hours-calendars."""
    assert market_calendar.today_et().year <= market_calendar.LAST_COVERED_YEAR
