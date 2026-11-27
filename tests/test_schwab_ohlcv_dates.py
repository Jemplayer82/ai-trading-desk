import logging

import pandas as pd
import pytest

from tradingagents.dataflows import schwab_mcp
from tradingagents.dataflows.stockstats_utils import _clean_dataframe, _schwab_ohlcv


EPOCH_MS = (1635742800000, 1635829200000)
ISO_UTC = ("2021-11-01T05:00:00.000Z", "2021-11-02T05:00:00.000Z")
EXPECTED_DATES = pd.Series(
    pd.to_datetime(["2021-11-01 05:00:00", "2021-11-02 05:00:00"]), name="Date"
)

pytestmark = pytest.mark.unit


@pytest.fixture
def fetch_candles(monkeypatch):
    monkeypatch.setattr(schwab_mcp, "market_data_enabled", lambda: True)

    def fetch(*timestamps):
        payload = {
            "candles": [
                {"datetime": date, "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100}
                for date in timestamps
            ]
        }
        monkeypatch.setattr(schwab_mcp, "get_price_history", lambda *args, **kwargs: payload)
        return _schwab_ohlcv("AAPL", "2021-11-01", "2021-11-03")

    return fetch


def test_epoch_milliseconds_preserve_dates_and_columns(fetch_candles):
    frame = fetch_candles(*EPOCH_MS)
    assert list(frame.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    pd.testing.assert_series_equal(frame["Date"], EXPECTED_DATES)


def test_iso_z_matches_epoch_milliseconds(fetch_candles):
    frame = fetch_candles(*ISO_UTC)
    epoch_frame = fetch_candles(*EPOCH_MS)
    assert frame["Date"].dt.tz is None
    pd.testing.assert_series_equal(frame["Date"], epoch_frame["Date"])


@pytest.mark.parametrize(
    "timestamp",
    ["2021-11-01T00:00:00-05:00", "2021-11-01T05:00:00", "1635742800000", 1635742800000.0],
    ids=["offset", "no-zone", "epoch-string", "epoch-float"],
)
def test_alternate_timestamp_forms(fetch_candles, timestamp):
    frame = fetch_candles(timestamp)
    pd.testing.assert_series_equal(frame["Date"], EXPECTED_DATES.iloc[:1])


def test_mixed_timestamp_forms_survive_cleaning(fetch_candles):
    frame = fetch_candles(EPOCH_MS[0], ISO_UTC[1])
    pd.testing.assert_series_equal(frame["Date"], EXPECTED_DATES)
    pd.testing.assert_frame_equal(_clean_dataframe(frame.copy()), frame)


@pytest.mark.parametrize("bad_date", [None, "", "garbage", True, float("nan")])
def test_bad_timestamp_falls_back_for_whole_symbol(fetch_candles, caplog, bad_date):
    with caplog.at_level(logging.WARNING, logger="tradingagents.dataflows.stockstats_utils"):
        assert fetch_candles(EPOCH_MS[0], bad_date) is None
    assert "Schwab price history failed for AAPL; using yfinance" in caplog.messages
