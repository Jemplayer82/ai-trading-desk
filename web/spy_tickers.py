"""S&P 500 ticker list, fetched from Wikipedia and cached for 24h.

The cache is a module global, i.e. per-process: every container that imports
this keeps its own copy. On a fetch failure a stale cache is served if one
exists; with no cache at all the error propagates and the caller
(research_engine.run_research) fails the scan with a friendly phase message.
Symbols are normalized to yfinance form (BRK.B → BRK-B).
"""
from __future__ import annotations

import io
import logging
import time

import pandas as pd
import requests

log = logging.getLogger(__name__)

_CACHE: list[str] | None = None
_CACHE_TS: float = 0.0
_TTL = 86400  # 24 hours

# Wikipedia 403s bare urllib user-agents.  A realistic browser UA is accepted.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; TradingAgents/1.0; "
        "+https://github.com/Jemplayer82/ai-trading-desk)"
    )
}
_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def _fetch_tickers() -> list[str]:
    """Fetch the S&P 500 constituents table from Wikipedia."""
    resp = requests.get(_WIKI_URL, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    dfs = pd.read_html(io.StringIO(resp.text))
    df = dfs[0]
    # yfinance uses hyphens; Wikipedia uses dots (BRK.B → BRK-B)
    return df["Symbol"].str.replace(".", "-", regex=False).tolist()


def get_sp500_tickers() -> list[str]:
    global _CACHE, _CACHE_TS
    if _CACHE and (time.time() - _CACHE_TS) < _TTL:
        return _CACHE
    try:
        tickers = _fetch_tickers()
        _CACHE = tickers
        _CACHE_TS = time.time()
        log.info("Fetched %d S&P 500 tickers from Wikipedia", len(tickers))
        return tickers
    except Exception as exc:
        log.exception("Failed to fetch S&P 500 tickers: %s", exc)
        if _CACHE:
            log.warning("Using stale ticker cache (%d tickers)", len(_CACHE))
            return _CACHE
        raise
