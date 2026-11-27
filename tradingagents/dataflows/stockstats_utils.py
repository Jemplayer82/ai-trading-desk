import time
import logging

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFRateLimitError
from stockstats import wrap
from typing import Annotated
import os
from .config import get_config
from .utils import safe_ticker_component

logger = logging.getLogger(__name__)


def yf_retry(func, max_retries=3, base_delay=2.0):
    """Execute a yfinance call with exponential backoff on rate limits.

    yfinance raises YFRateLimitError on HTTP 429 responses but does not
    retry them internally. This wrapper adds retry logic specifically
    for rate limits. Other exceptions propagate immediately.
    """
    for attempt in range(max_retries + 1):
        try:
            return func()
        except YFRateLimitError:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Yahoo Finance rate limited, retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                raise


def _schwab_candle_dates(series: pd.Series) -> pd.Series:
    """Parse candle timestamps as timezone-naive UTC dates."""
    def parse(value):
        if isinstance(value, str) and value.isdecimal():
            value = int(value)

        if pd.api.types.is_integer(value) or pd.api.types.is_float(value):
            date = pd.to_datetime(value, unit="ms", utc=True)
        elif isinstance(value, str):
            date = pd.to_datetime(value, format="ISO8601", utc=True)
        else:
            raise ValueError("Invalid Schwab candle datetime")

        if pd.isna(date):
            raise ValueError("Missing Schwab candle datetime")
        return date

    return series.map(parse).dt.tz_localize(None)


def _schwab_ohlcv(symbol: str, start_str: str, end_str: str) -> pd.DataFrame | None:
    """Fetch ~5y daily OHLCV from the Schwab MCP server, or None to fall back.

    Returns a DataFrame with Date/Open/High/Low/Close/Volume columns so it
    drops straight into the existing cache + stockstats pipeline.
    """
    try:
        from . import schwab_mcp
        if not schwab_mcp.market_data_enabled():
            return None
        payload = schwab_mcp.get_price_history(
            symbol, period_type="year", period=5, frequency_type="daily", frequency=1
        )
        candles = (payload or {}).get("candles") or []
        if not candles:
            return None
        df = pd.DataFrame(candles)
        df["Date"] = _schwab_candle_dates(df["datetime"])
        df = df.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        })
        cols = [c for c in ["Date", "Open", "High", "Low", "Close", "Volume"] if c in df.columns]
        logger.info("Loaded %d Schwab candles for %s", len(df), symbol)
        return df[cols]
    except Exception:
        logger.warning("Schwab price history failed for %s; using yfinance", symbol, exc_info=True)
        return None


def _clean_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize a stock DataFrame for stockstats: parse dates, drop invalid rows, fill price gaps."""
    # Older cached CSVs and some yfinance outputs name the date column
    # differently. Normalise to "Date" before any downstream code touches it
    # — this avoids a KeyError("Date") when reading legacy caches where
    # df.reset_index() produced an "index" column from an unnamed
    # DatetimeIndex.
    if "Date" not in data.columns:
        for cand in ("Datetime", "date", "index"):
            if cand in data.columns:
                data = data.rename(columns={cand: "Date"})
                break
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])

    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    data = data.dropna(subset=["Close"])
    data[price_cols] = data[price_cols].ffill().bfill()

    return data


def load_ohlcv(symbol: str, curr_date: str) -> pd.DataFrame:
    """Fetch OHLCV data with caching, filtered to prevent look-ahead bias.

    Downloads 15 years of data up to today and caches per symbol. On
    subsequent calls the cache is reused. Rows after curr_date are
    filtered out so backtests never see future prices.
    """
    # Reject ticker values that would escape the cache directory when
    # interpolated into the cache filename (e.g. ``../../tmp/x``).
    safe_symbol = safe_ticker_component(symbol)

    config = get_config()
    curr_date_dt = pd.to_datetime(curr_date)

    # Cache uses a fixed window (15y to today) so one file per symbol
    today_date = pd.Timestamp.today()
    start_date = today_date - pd.DateOffset(years=5)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = today_date.strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    data_file = os.path.join(
        config["data_cache_dir"],
        f"{safe_symbol}-YFin-data-{start_str}-{end_str}.csv",
    )

    if os.path.exists(data_file):
        data = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
    else:
        # Prefer Schwab price history; fall back to yfinance on empty/error.
        data = _schwab_ohlcv(symbol, start_str, end_str)
        if data is None or data.empty:
            data = yf_retry(lambda: yf.download(
                symbol,
                start=start_str,
                end=end_str,
                multi_level_index=False,
                progress=False,
                auto_adjust=True,
            ))
            data = data.reset_index()
        data.to_csv(data_file, index=False, encoding="utf-8")

    data = _clean_dataframe(data)

    # Filter to curr_date to prevent look-ahead bias in backtesting
    data = data[data["Date"] <= curr_date_dt]

    return data


def filter_financials_by_date(data: pd.DataFrame, curr_date: str) -> pd.DataFrame:
    """Drop financial statement columns (fiscal period timestamps) after curr_date.

    yfinance financial statements use fiscal period end dates as columns.
    Columns after curr_date represent future data and are removed to
    prevent look-ahead bias.
    """
    if not curr_date or data.empty:
        return data
    cutoff = pd.Timestamp(curr_date)
    mask = pd.to_datetime(data.columns, errors="coerce") <= cutoff
    return data.loc[:, mask]


class StockstatsUtils:
    @staticmethod
    def get_stock_stats(
        symbol: Annotated[str, "ticker symbol for the company"],
        indicator: Annotated[
            str, "quantitative indicators based off of the stock data for the company"
        ],
        curr_date: Annotated[
            str, "curr date for retrieving stock price data, YYYY-mm-dd"
        ],
    ):
        data = load_ohlcv(symbol, curr_date)
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
        curr_date_str = pd.to_datetime(curr_date).strftime("%Y-%m-%d")

        df[indicator]  # trigger stockstats to calculate the indicator
        matching_rows = df[df["Date"].str.startswith(curr_date_str)]

        if not matching_rows.empty:
            indicator_value = matching_rows[indicator].values[0]
            return indicator_value
        else:
            return "N/A: Not a trading day (weekend or holiday)"
