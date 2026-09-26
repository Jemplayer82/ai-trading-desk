"""Deep dives must contribute to System C (the memory log).

Regression cover for the learning-loop gap where run_deep_dives read past
context but never stored its own decisions — the highest-volume decision path
(options + S&P scans) contributed nothing to nightly outcome grading.
"""

import threading
import time
from unittest.mock import MagicMock

import pandas as pd
import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog
from web import db, spy_scanner

pytestmark = pytest.mark.unit

DECISION = "Rating: Buy\nStrong momentum thesis; enter on pullback."


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


def _fake_orchestrator_cls(memory_log, decision=DECISION, signal="BUY"):
    """Stand-in for SwitchboardOrchestrator matching what _dive_inner uses."""

    class FakeOrchestrator:
        def __init__(self, config=None, selected_analysts=None, **kw):
            self.memory_log = memory_log

        def run(self, ticker, trade_date, **kw):
            return {"final_trade_decision": decision}, signal

    return FakeOrchestrator


def _run(tmp_db, monkeypatch, tmp_path, *, config_extra=None, decision=DECISION,
         store_raises=False, tickers=("AAPL",)):
    scan_id = db.create_spy_scan("2026-07-29", kind="options")
    config = {"memory_log_path": str(tmp_path / "mem.md")}
    config.update(config_extra or {})
    memory_log = TradingMemoryLog(config)
    if store_raises:
        memory_log = MagicMock(wraps=memory_log)
        memory_log.store_decision.side_effect = RuntimeError("disk full")
    monkeypatch.setattr(
        spy_scanner, "SwitchboardOrchestrator",
        _fake_orchestrator_cls(memory_log, decision=decision),
    )
    candidates = [{"ticker": t, "signal": "BUY", "conviction": 8} for t in tickers]
    enriched = spy_scanner.run_deep_dives(
        scan_id, candidates, "2026-07-29", config, ["market"],
    )
    return enriched, TradingMemoryLog(config)


def test_deep_dive_stores_pending_decision(tmp_db, monkeypatch, tmp_path):
    enriched, log = _run(tmp_db, monkeypatch, tmp_path)
    assert not enriched[0].get("error")
    entries = log.load_entries()
    assert len(entries) == 1
    assert entries[0]["ticker"] == "AAPL"
    assert entries[0]["pending"] is True
    assert entries[0]["rating"] == "Buy"


def test_store_failure_never_fails_the_dive(tmp_db, monkeypatch, tmp_path):
    """A memory-log write failure must not turn a good analysis into an
    'error' row (which would get dropped before vetting)."""
    enriched, _ = _run(tmp_db, monkeypatch, tmp_path, store_raises=True)
    assert not enriched[0].get("error"), "store failure leaked into the dive result"
    assert enriched[0]["signal"] == "BUY"


def test_empty_decision_stores_nothing(tmp_db, monkeypatch, tmp_path):
    _, log = _run(tmp_db, monkeypatch, tmp_path, decision="")
    assert log.load_entries() == []


def test_kill_switch_disables_store(tmp_db, monkeypatch, tmp_path):
    _, log = _run(tmp_db, monkeypatch, tmp_path,
                  config_extra={"deep_dive_store_decisions": False})
    assert log.load_entries() == []


def test_same_day_rerun_dedupes(tmp_db, monkeypatch, tmp_path):
    """Options and equity scans deep-diving the same ticker on the same date
    must not double-count it in calibration."""
    _run(tmp_db, monkeypatch, tmp_path)
    _, log = _run(tmp_db, monkeypatch, tmp_path)
    assert len(log.load_entries()) == 1


class TestQuickScanPriceDataCache:
    """Same-day TTL cache for the bulk price fetch that feeds run_quick_scan."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        spy_scanner._PRICE_DATA_CACHE.clear()
        yield
        spy_scanner._PRICE_DATA_CACHE.clear()

    @staticmethod
    def _frame(tickers, n=5):
        # The cache gate now counts a ticker as usable only when it has
        # >=5 non-NaN closes, so ensure generated frames always clear that bar.
        n = max(n, 5)
        data = {}
        for i, t in enumerate(tickers):
            data[("Close", t)] = [float(100 + i * 10 + j) for j in range(n)]
            data[("Volume", t)] = [1000] * n
        return pd.DataFrame(data)

    @staticmethod
    def _all_nan_frame(real_tickers, nan_tickers, n=5):
        n = max(n, 5)
        data = {}
        for i, t in enumerate(real_tickers):
            data[("Close", t)] = [float(100 + i * 10 + j) for j in range(n)]
            data[("Volume", t)] = [1000] * n
        for t in nan_tickers:
            data[("Close", t)] = [float("nan")] * n
            data[("Volume", t)] = [float("nan")] * n
        return pd.DataFrame(data)

    def test_same_day_same_tickers_downloads_once(self, monkeypatch):
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(tickers)

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        m1 = spy_scanner._fetch_price_data_map(1, ["AAA", "BBB"], "2026-08-08")
        m2 = spy_scanner._fetch_price_data_map(2, ["AAA", "BBB"], "2026-08-08")

        assert calls["count"] == 1
        assert m1 is m2

    def test_ticker_order_does_not_matter(self, monkeypatch):
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(tickers)

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        m1 = spy_scanner._fetch_price_data_map(1, ["AAA", "BBB"], "2026-08-08")
        m2 = spy_scanner._fetch_price_data_map(2, ["BBB", "AAA"], "2026-08-08")

        assert calls["count"] == 1
        assert m1 == m2

    def test_different_ticker_set_is_a_separate_key(self, monkeypatch):
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(tickers)

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        spy_scanner._fetch_price_data_map(1, ["AAA", "BBB"], "2026-08-08")
        spy_scanner._fetch_price_data_map(2, ["AAA", "BBB", "CCC"], "2026-08-08")

        assert calls["count"] == 2

    def test_different_trade_date_refetches(self, monkeypatch):
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(tickers)

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        spy_scanner._fetch_price_data_map(1, ["AAA", "BBB"], "2026-08-08")
        spy_scanner._fetch_price_data_map(2, ["AAA", "BBB"], "2026-08-09")

        assert calls["count"] == 2
        assert spy_scanner._PRICE_DATA_CACHE.stats()["size"] == 1

    def test_prior_day_entries_are_evicted(self, monkeypatch):
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(tickers)

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        spy_scanner._fetch_price_data_map(1, ["AAA", "BBB"], "2026-08-08")
        spy_scanner._fetch_price_data_map(2, ["AAA", "BBB"], "2026-08-09")

        assert spy_scanner._PRICE_DATA_CACHE.stats()["size"] == 1

        spy_scanner._fetch_price_data_map(3, ["AAA", "BBB"], "2026-08-08")

        assert calls["count"] == 3

    def test_ttl_expiry_refetches(self, monkeypatch):
        now = [0.0]
        monkeypatch.setattr(spy_scanner.market_cache, "_now", lambda: now[0])

        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(tickers)

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        spy_scanner._fetch_price_data_map(1, ["AAA"], "2026-08-08")
        now[0] += spy_scanner._PRICE_DATA_TTL_SECONDS + 1
        spy_scanner._fetch_price_data_map(2, ["AAA"], "2026-08-08")

        assert calls["count"] == 2

    @pytest.mark.parametrize("behavior", ["exception", "empty"])
    def test_failed_download_is_not_cached(self, monkeypatch, behavior):
        calls = {"count": 0}

        def _download(*a, **kw):
            calls["count"] += 1
            if behavior == "exception":
                raise RuntimeError("boom")
            return pd.DataFrame()

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        m1 = spy_scanner._fetch_price_data_map(1, ["AAA"], "2026-08-08")
        assert m1 == {}
        assert spy_scanner._PRICE_DATA_CACHE.stats()["size"] == 0

        m2 = spy_scanner._fetch_price_data_map(2, ["AAA"], "2026-08-08")
        assert m2 == {}
        assert calls["count"] == 2

    def test_concurrent_callers_share_the_cache(self, monkeypatch):
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(tickers, n=3)

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        results = [None, None]
        errors = [None, None]

        def worker(idx):
            try:
                results[idx] = spy_scanner._fetch_price_data_map(
                    idx + 10, ["X", "Y"], "2026-08-08"
                )
            except Exception as exc:
                errors[idx] = exc

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert all(not t.is_alive() for t in threads)
        assert errors == [None, None]
        assert results[0] is not None
        assert results[0] == results[1]
        assert calls["count"] <= 2
        assert spy_scanner._PRICE_DATA_CACHE.stats()["size"] == 1

    def test_partial_download_below_threshold_is_not_cached(self, monkeypatch):
        """Only 1 of 3 tickers came back — below 80% completeness, so the
        cache write is skipped even though the function still returns the
        partial map to the caller.
        """
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(["AAA"])

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        tickers = ["AAA", "BBB", "CCC"]
        m1 = spy_scanner._fetch_price_data_map(1, tickers, "2026-08-08")
        m2 = spy_scanner._fetch_price_data_map(2, tickers, "2026-08-08")

        assert calls["count"] == 2
        assert spy_scanner._PRICE_DATA_CACHE.stats()["size"] == 0
        assert list(m1.keys()) == ["AAA"]
        assert m1 == m2

    def test_partial_download_still_returns_usable_tickers(self, monkeypatch):
        """The completeness gate must block only the cache write, never the
        return value.
        """
        def _download(tickers, *a, **kw):
            return self._frame(["AAA"])

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        m = spy_scanner._fetch_price_data_map(1, ["AAA", "BBB", "CCC"], "2026-08-08")
        assert list(m.keys()) == ["AAA"]
        assert m["AAA"]["close"]
        assert m["AAA"]["volume"]

    def test_download_just_above_threshold_still_caches(self, monkeypatch):
        """4 of 5 tickers present clears the 80% bar and is cached for reuse."""
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(["T1", "T2", "T3", "T4"])

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        tickers = ["T1", "T2", "T3", "T4", "T5"]
        m1 = spy_scanner._fetch_price_data_map(1, tickers, "2026-08-08")
        m2 = spy_scanner._fetch_price_data_map(2, tickers, "2026-08-08")

        assert calls["count"] == 1
        assert m1 is m2
        assert spy_scanner._PRICE_DATA_CACHE.stats()["size"] == 1
        assert sorted(m1.keys()) == ["T1", "T2", "T3", "T4"]

    def test_all_nan_ticker_does_not_count_as_usable(self, monkeypatch):
        """A ticker whose Close/Volume columns are all NaN still gets a dict
        entry with empty lists, but it must not count as usable toward the
        80% cache-completeness gate.
        """
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._all_nan_frame(["AAA"], ["BBB", "CCC"])

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        tickers = ["AAA", "BBB", "CCC"]
        m1 = spy_scanner._fetch_price_data_map(1, tickers, "2026-08-08")
        m2 = spy_scanner._fetch_price_data_map(2, tickers, "2026-08-08")

        # dict-key coverage would be 3/3 = 100%, but usable coverage is
        # 1/3 = 33%, so the cache write is skipped.
        assert calls["count"] == 2
        assert spy_scanner._PRICE_DATA_CACHE.stats()["size"] == 0

        # The function still returns the all-NaN entries to the caller.
        assert sorted(m1.keys()) == ["AAA", "BBB", "CCC"]
        assert len(m1["AAA"]["close"]) >= 5
        assert m1["BBB"]["close"] == []
        assert m1["BBB"]["volume"] == []
        assert m1["CCC"]["close"] == []
        assert m1["CCC"]["volume"] == []
        assert m1 == m2


class TestHeartbeat:
    """_Heartbeat stamps spy_scans.updated_at from a background thread."""

    def test_heartbeat_beats_while_inside_and_stops_after_exit(self):
        calls = []

        def beat():
            n = len(calls)
            calls.append(n)
            if n == 3:
                raise RuntimeError("heartbeat boom")

        with spy_scanner._Heartbeat(beat, interval=0.02):
            time.sleep(0.18)

        inside_count = len(calls)
        assert inside_count >= 2
        assert 3 in calls  # the 4th beat happened and raised
        assert inside_count >= 4  # beating continued after the swallowed exception
        final = len(calls)
        time.sleep(0.1)
        assert len(calls) == final

    def test_heartbeat_touches_scan_while_dives_in_flight(self, tmp_db, monkeypatch, tmp_path):
        monkeypatch.setattr(spy_scanner, "SCAN_HEARTBEAT_SECONDS", 0.05)

        def _fake_cls(memory_log, *, sleep_seconds=0.3):
            class FakeOrchestrator:
                def __init__(self, config=None, selected_analysts=None, **kw):
                    self.memory_log = memory_log

                def run(self, ticker, trade_date, **kw):
                    time.sleep(sleep_seconds)
                    return {"final_trade_decision": "Rating: Buy"}, "BUY"

            return FakeOrchestrator

        memory_log = TradingMemoryLog({"memory_log_path": str(tmp_path / "mem.md")})
        monkeypatch.setattr(
            spy_scanner,
            "SwitchboardOrchestrator",
            _fake_cls(memory_log, sleep_seconds=0.3),
        )

        recorded = []
        real_update = spy_scanner.db.update_spy_scan

        def recorder(scan_id, **kwargs):
            recorded.append((threading.current_thread().name, kwargs))
            return real_update(scan_id, **kwargs)

        monkeypatch.setattr(spy_scanner.db, "update_spy_scan", recorder)

        scan_id = db.create_spy_scan("2026-08-08", kind="options")
        config = {"deep_dive_reuse": False}
        candidates = [{"ticker": "AAPL", "signal": "BUY", "conviction": 8}]

        spy_scanner.run_deep_dives(scan_id, candidates, "2026-08-08", config, ["market"])

        heartbeat_calls = [
            r for r in recorded
            if r[0] == "scan-heartbeat" and "deep_count" in r[1]
        ]
        assert len(heartbeat_calls) >= 2


class TestFetchLivePrices:
    """fetch_live_prices prefers Schwab and falls back to yfinance."""

    @staticmethod
    def _price_frame(tickers, n=2):
        n = max(n, 2)
        data = {}
        for i, t in enumerate(tickers):
            data[("Close", t)] = [float(100 + i * 10 + j) for j in range(n)]
        return pd.DataFrame(data)

    @staticmethod
    def _single_price_frame(ticker, n=2):
        n = max(n, 2)
        return pd.DataFrame({"Close": [float(100 + j) for j in range(n)]})

    def test_schwab_enabled_returns_schwab_prices(self, monkeypatch):
        def _download(*a, **kw):
            raise AssertionError("yf.download should not be called when Schwab prices are available")

        monkeypatch.setattr(spy_scanner.yf, "download", _download)
        monkeypatch.setattr(spy_scanner.schwab_mcp, "market_data_enabled", lambda: True)

        def _get_quotes(tickers):
            return {t: {"lastPrice": 100.0 + ord(t[0])} for t in tickers}

        monkeypatch.setattr(spy_scanner.schwab_mcp, "get_quotes", _get_quotes)
        monkeypatch.setattr(spy_scanner.schwab_mcp, "quote_price", lambda q: q.get("lastPrice"))

        prices = spy_scanner.fetch_live_prices(["AAPL", "MSFT", "AAPL"], log_label="test")
        assert prices == {"AAPL": 165.0, "MSFT": 177.0}

    def test_schwab_raises_falls_back_to_yf(self, monkeypatch):
        monkeypatch.setattr(spy_scanner.schwab_mcp, "market_data_enabled", lambda: True)
        monkeypatch.setattr(
            spy_scanner.schwab_mcp,
            "get_quotes",
            lambda t: (_ for _ in ()).throw(RuntimeError("schwab down")),
        )
        monkeypatch.setattr(
            spy_scanner.yf,
            "download",
            lambda tickers, *a, **kw: self._price_frame(tickers),
        )
        prices = spy_scanner.fetch_live_prices(["AAPL", "MSFT"])
        assert prices == {"AAPL": 101.0, "MSFT": 111.0}

    def test_schwab_returns_no_prices_falls_back_to_yf(self, monkeypatch):
        monkeypatch.setattr(spy_scanner.schwab_mcp, "market_data_enabled", lambda: True)
        monkeypatch.setattr(spy_scanner.schwab_mcp, "get_quotes", lambda t: {})
        monkeypatch.setattr(
            spy_scanner.yf,
            "download",
            lambda tickers, *a, **kw: self._price_frame(tickers),
        )
        prices = spy_scanner.fetch_live_prices(["AAPL", "MSFT"])
        assert prices == {"AAPL": 101.0, "MSFT": 111.0}

    def test_single_ticker_yf_frame(self, monkeypatch):
        monkeypatch.setattr(spy_scanner.schwab_mcp, "market_data_enabled", lambda: False)
        monkeypatch.setattr(
            spy_scanner.yf,
            "download",
            lambda tickers, *a, **kw: self._single_price_frame(tickers[0]),
        )
        prices = spy_scanner.fetch_live_prices(["AAPL"])
        assert prices == {"AAPL": 101.0}

    def test_yf_raises_propagates(self, monkeypatch):
        monkeypatch.setattr(spy_scanner.schwab_mcp, "market_data_enabled", lambda: False)
        monkeypatch.setattr(
            spy_scanner.yf,
            "download",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("yf down")),
        )
        with pytest.raises(RuntimeError, match="yf down"):
            spy_scanner.fetch_live_prices(["AAPL"])

    def test_empty_list_returns_empty(self):
        assert spy_scanner.fetch_live_prices([]) == {}

    def test_refresh_portfolio_prices_returns_error_on_yf_failure(self, tmp_db, monkeypatch):
        scan_id = db.create_spy_scan("2026-08-08", kind="equity")
        db.complete_spy_scan(
            scan_id,
            allocator_report="",
            portfolio_json=[
                {
                    "ticker": "AAPL",
                    "signal": "BUY",
                    "action": "NEW",
                    "entry_price": 150.0,
                    "shares": 10,
                    "dollar_amount": 1500.0,
                }
            ],
        )
        monkeypatch.setattr(spy_scanner.schwab_mcp, "market_data_enabled", lambda: False)
        monkeypatch.setattr(
            spy_scanner.yf,
            "download",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("market data down")),
        )
        result = spy_scanner.refresh_portfolio_prices(scan_id)
        assert result == {"error": "market data down"}
