"""Unit tests for the shared research engine.

The research engine is tier-3 code: it must not import any ``options_*``
module at module level and it ships in every tier that has the shared
S&P 500 research pipeline.
"""
from __future__ import annotations

import threading
import time as time_mod
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from web import alerts, db, market_cache, market_calendar, research_engine, scan_queue, spy_scanner

pytestmark = pytest.mark.unit


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


def _q(ticker, signal="BUY", conviction=5):
    return {"ticker": ticker, "signal": signal, "conviction": conviction}


# ── Deep-dive target selection ───────────────────────────────────────────────

def test_deep_targets_top_by_conviction_capped_at_deep_top():
    """Only the top DEEP_TOP directional names by conviction get a deep dive."""
    rows = [_q(f"T{i}", "BUY", conviction=i) for i in range(research_engine.DEEP_TOP + 40)]
    top = research_engine.select_deep_dive_targets(rows)
    # DEEP_TOP directional picks; SPY wasn't in the quick set so nothing forced.
    assert len(top) == research_engine.DEEP_TOP
    # Highest-conviction names survive; the low-conviction tail is dropped.
    picked = {r["ticker"] for r in top}
    assert f"T{research_engine.DEEP_TOP + 39}" in picked
    assert "T0" not in picked


def test_deep_targets_include_both_buy_and_sell():
    rows = [_q("BULL", "BUY", 9), _q("BEAR", "SELL", 8), _q("MEH", "HOLD", 9)]
    picked = {r["ticker"] for r in research_engine.select_deep_dive_targets(rows)}
    assert picked == {"BULL", "BEAR"}  # HOLD is not directional


def test_spy_always_deep_dived_even_when_hold():
    """SPY is guaranteed a deep dive every run — the '+1 done on SPY' rule —
    even when its quick scan is HOLD and it's outside the directional set."""
    rows = [_q(f"T{i}", "BUY", 9) for i in range(research_engine.DEEP_TOP)]
    rows.append(_q("SPY", "HOLD", 1))
    top = research_engine.select_deep_dive_targets(rows)
    assert any(r["ticker"] == "SPY" for r in top), "SPY must always be deep-dived"
    assert len(top) == research_engine.DEEP_TOP + 1  # the forced +1


def test_spy_not_duplicated_when_already_directional():
    rows = [_q("SPY", "BUY", 10)] + [_q(f"T{i}", "BUY", 5) for i in range(3)]
    top = research_engine.select_deep_dive_targets(rows)
    assert [r["ticker"] for r in top].count("SPY") == 1


# ── Pre-screen scoring ───────────────────────────────────────────────────────

def test_mover_score_direction_agnostic():
    closes_up = [100 + i for i in range(21)]
    closes_down = [100 - i * 0.8 for i in range(21)]
    flat = [100.0] * 21
    vols = [1_000_000] * 21
    up = research_engine._mover_score(closes_up, vols)
    down = research_engine._mover_score(closes_down, vols)
    quiet = research_engine._mover_score(flat, vols)
    assert up > quiet and down > quiet   # losers are put candidates, not noise
    assert research_engine._mover_score([100, 101], vols) is None  # too short


# ── Same-day movers pre-screen cache ─────────────────────────────────────────

class TestPrescreenSameDayCache:
    @pytest.fixture(autouse=True)
    def _clear_prescreen_cache(self):
        research_engine._PRESCREEN_CACHE.clear()
        yield
        research_engine._PRESCREEN_CACHE.clear()

    @pytest.fixture()
    def fake_download(self, monkeypatch):
        calls = {"n": 0}
        rows = 21
        data = {}
        for i, ticker in enumerate(["AAA", "BBB", "CCC"]):
            start = 100.0 + i * 10.0
            data[("Close", ticker)] = [start + j for j in range(rows)]
            data[("Volume", ticker)] = [1_000_000 + i * 100_000] * rows
        df = pd.DataFrame(data)

        def _download(*args, **kwargs):
            calls["n"] += 1
            return df

        monkeypatch.setattr(research_engine.yf, "download", _download)
        return {"calls": calls, "df": df, "download": _download}

    @staticmethod
    def _partial_df(present_tickers, rows=21):
        data = {}
        for i, ticker in enumerate(present_tickers):
            start = 100.0 + i * 10.0
            data[("Close", ticker)] = [start + j for j in range(rows)]
            data[("Volume", ticker)] = [1_000_000 + i * 100_000] * rows
        return pd.DataFrame(data)

    def test_second_same_day_call_is_a_cache_hit(self, fake_download):
        tickers = ["AAA", "BBB", "CCC"]
        trade_date = "2026-07-29"
        r1 = research_engine.prescreen(tickers, 3, trade_date=trade_date)
        r2 = research_engine.prescreen(tickers, 3, trade_date=trade_date)
        assert fake_download["calls"]["n"] == 1
        assert r1 == r2

    def test_returns_a_copy_not_the_cached_list(self, fake_download):
        tickers = ["AAA", "BBB", "CCC"]
        trade_date = "2026-07-29"
        r1 = research_engine.prescreen(tickers, 3, trade_date=trade_date)
        r1.append("SPY")
        r2 = research_engine.prescreen(tickers, 3, trade_date=trade_date)
        assert "SPY" not in r2
        assert r1 is not r2
        assert fake_download["calls"]["n"] == 1

    def test_different_trade_date_refetches(self, fake_download):
        tickers = ["AAA", "BBB", "CCC"]
        r1 = research_engine.prescreen(tickers, 3, trade_date="2026-07-29")
        r2 = research_engine.prescreen(tickers, 3, trade_date="2026-07-30")
        assert fake_download["calls"]["n"] == 2
        assert r1 == r2

    def test_prior_day_entry_is_evicted(self, fake_download):
        tickers = ["AAA", "BBB", "CCC"]
        research_engine.prescreen(tickers, 3, trade_date="2026-07-29")
        research_engine.prescreen(tickers, 3, trade_date="2026-07-30")
        assert research_engine._PRESCREEN_CACHE.stats()["size"] == 1
        research_engine.prescreen(tickers, 3, trade_date="2026-07-29")
        assert fake_download["calls"]["n"] == 3
        assert research_engine._PRESCREEN_CACHE.stats()["size"] == 1

    def test_different_top_n_refetches(self, fake_download):
        tickers = ["AAA", "BBB", "CCC"]
        research_engine.prescreen(tickers, 3, trade_date="2026-07-29")
        research_engine.prescreen(tickers, 2, trade_date="2026-07-29")
        assert fake_download["calls"]["n"] == 2

    def test_different_ticker_set_refetches(self, fake_download):
        research_engine.prescreen(["AAA", "BBB", "CCC"], 3, trade_date="2026-07-29")
        research_engine.prescreen(["AAA", "BBB"], 3, trade_date="2026-07-29")
        assert fake_download["calls"]["n"] == 2

    def test_ticker_order_does_not_matter(self, fake_download):
        r1 = research_engine.prescreen(["AAA", "BBB", "CCC"], 3, trade_date="2026-07-29")
        r2 = research_engine.prescreen(["CCC", "AAA", "BBB"], 3, trade_date="2026-07-29")
        assert fake_download["calls"]["n"] == 1
        assert r1 == r2

    def test_no_trade_date_bypasses_the_cache(self, fake_download):
        tickers = ["AAA", "BBB", "CCC"]
        r1 = research_engine.prescreen(tickers, 3, trade_date=None)
        r2 = research_engine.prescreen(tickers, 3, trade_date=None)
        assert fake_download["calls"]["n"] == 2
        assert r1 == r2
        assert research_engine._PRESCREEN_CACHE.stats()["size"] == 0

    def test_ttl_expiry_refetches(self, fake_download, monkeypatch):
        tickers = ["AAA", "BBB", "CCC"]
        trade_date = "2026-07-29"
        start = 1000.0
        monkeypatch.setattr(market_cache, "_now", lambda: start)
        r1 = research_engine.prescreen(tickers, 3, trade_date=trade_date)
        monkeypatch.setattr(
            market_cache,
            "_now",
            lambda: start + research_engine._PRESCREEN_TTL_SECONDS + 1,
        )
        r2 = research_engine.prescreen(tickers, 3, trade_date=trade_date)
        assert fake_download["calls"]["n"] == 2
        assert r1 == r2

    def test_empty_result_is_not_cached(self, monkeypatch):
        calls = {"n": 0}

        def empty_download(*args, **kwargs):
            calls["n"] += 1
            return pd.DataFrame()

        monkeypatch.setattr(research_engine.yf, "download", empty_download)
        tickers = ["AAA", "BBB", "CCC"]
        trade_date = "2026-07-29"
        r1 = research_engine.prescreen(tickers, 3, trade_date=trade_date)
        r2 = research_engine.prescreen(tickers, 3, trade_date=trade_date)
        assert r1 == []
        assert r2 == []
        assert calls["n"] == 2
        assert research_engine._PRESCREEN_CACHE.stats()["size"] == 0

    def test_download_failure_is_not_cached(self, fake_download, monkeypatch):
        calls = {"n": 0}
        df = fake_download["df"]

        def flaky_download(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("network down")
            return df

        monkeypatch.setattr(research_engine.yf, "download", flaky_download)
        tickers = ["AAA", "BBB", "CCC"]
        trade_date = "2026-07-29"
        with pytest.raises(RuntimeError, match="pre-screen bulk download failed"):
            research_engine.prescreen(tickers, 3, trade_date=trade_date)
        assert research_engine._PRESCREEN_CACHE.stats()["size"] == 0
        r = research_engine.prescreen(tickers, 3, trade_date=trade_date)
        assert calls["n"] == 2
        assert r == ["AAA", "BBB", "CCC"]

    def test_concurrent_callers_get_consistent_results(self, fake_download):
        results = []
        errors = []

        def worker():
            try:
                results.append(
                    research_engine.prescreen(
                        ["AAA", "BBB", "CCC"], 3, trade_date="2026-07-29"
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not errors
        assert len(results) == 3
        assert results[0] == results[1] == results[2]
        assert results[0] is not results[1]
        assert results[0] is not results[2]
        assert results[1] is not results[2]

    def test_partial_prescreen_below_threshold_is_not_cached(self, monkeypatch):
        """Only 1 of 5 tickers has usable data — below the 80% completeness
        gate, so the cache write is skipped.
        """
        calls = {"n": 0}

        def _download(*args, **kwargs):
            calls["n"] += 1
            return self._partial_df(["A"])

        monkeypatch.setattr(research_engine.yf, "download", _download)

        tickers = ["A", "B", "C", "D", "E"]
        r1 = research_engine.prescreen(tickers, 5, trade_date="2026-07-29")
        r2 = research_engine.prescreen(tickers, 5, trade_date="2026-07-29")

        assert calls["n"] == 2
        assert research_engine._PRESCREEN_CACHE.stats()["size"] == 0
        assert r1 == ["A"]
        assert r2 == ["A"]

    def test_partial_prescreen_still_returns_usable_tickers(self, monkeypatch):
        """The completeness gate must block only the cache write, never the
        return value.
        """
        def _download(*args, **kwargs):
            return self._partial_df(["A"])

        monkeypatch.setattr(research_engine.yf, "download", _download)

        tickers = ["A", "B", "C", "D", "E"]
        r = research_engine.prescreen(tickers, 5, trade_date="2026-07-29")
        assert r
        assert set(r).issubset({"A"})

    def test_prescreen_threshold_uses_scored_not_truncated_result(self, monkeypatch):
        """4 of 5 tickers are scoreable (meets the 80% gate) but top_n=2
        truncates the returned list to two names. The gate must see the
        pre-truncation scored count, not the shorter result.
        """
        calls = {"n": 0}

        def _download(*args, **kwargs):
            calls["n"] += 1
            return self._partial_df(["A", "B", "C", "D"])

        monkeypatch.setattr(research_engine.yf, "download", _download)

        tickers = ["A", "B", "C", "D", "E"]
        r1 = research_engine.prescreen(tickers, 2, trade_date="2026-07-29")
        r2 = research_engine.prescreen(tickers, 2, trade_date="2026-07-29")

        assert calls["n"] == 1
        assert research_engine._PRESCREEN_CACHE.stats()["size"] == 1
        assert len(r1) == 2
        assert set(r1).issubset({"A", "B", "C", "D"})
        assert r1 == r2


# ── Allocation slot ──────────────────────────────────────────────────────────

class TestAllocationSlot:
    """_allocation_slot serializes the post-wait phase and remains safe when
    cancelled or timed out while waiting for the global lock."""

    def test_mutual_exclusion(self, monkeypatch, tmp_db):
        scan_id = db.create_spy_scan("2026-06-01", kind="options")
        monkeypatch.setattr(research_engine, "_ALLOC_POLL_SECONDS", 0.05)

        seq: list[str] = []
        seq_lock = threading.Lock()

        def worker(n: int):
            with research_engine._allocation_slot(scan_id):
                with seq_lock:
                    seq.append(f"enter-{n}")
                time_mod.sleep(0.05)
                with seq_lock:
                    seq.append(f"exit-{n}")

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert not t1.is_alive()
        assert not t2.is_alive()

        assert len(seq) == 4
        for i in range(0, len(seq), 2):
            enter = seq[i]
            exit = seq[i + 1]
            n = enter.split("-")[1]
            assert exit == f"exit-{n}"

    def test_released_on_exception(self, monkeypatch, tmp_db):
        scan_id = db.create_spy_scan("2026-06-01", kind="options")

        with pytest.raises(RuntimeError, match="boom"):
            with research_engine._allocation_slot(scan_id):
                raise RuntimeError("boom")

        acquired = research_engine._ALLOC_LOCK.acquire(timeout=0.1)
        assert acquired
        lock_held = True
        try:
            research_engine._ALLOC_LOCK.release()
            lock_held = False
        finally:
            if lock_held:
                research_engine._ALLOC_LOCK.release()
                lock_held = False

    def test_heartbeats_while_blocked(self, monkeypatch, tmp_db):
        scan_id = db.create_spy_scan("2026-06-01", kind="options")
        monkeypatch.setattr(research_engine, "_ALLOC_POLL_SECONDS", 0.05)

        lock_held = research_engine._ALLOC_LOCK.acquire()
        assert lock_held
        caught: dict[str, Any] = {}

        def worker():
            try:
                with research_engine._allocation_slot(scan_id):
                    caught["entered"] = True
            except Exception as exc:
                caught["exc"] = exc

        t = threading.Thread(target=worker)
        t.start()
        try:
            time_mod.sleep(0.3)
            status = db.get_spy_scan_status(scan_id)["status"]
            assert status == "running_wait_alloc"
            research_engine._ALLOC_LOCK.release()
            lock_held = False
            t.join(timeout=5)
            assert not t.is_alive()
            assert "entered" in caught
            assert "exc" not in caught
        finally:
            if lock_held:
                research_engine._ALLOC_LOCK.release()
                lock_held = False
            t.join(timeout=5)

    def test_cancel_while_blocked_raises_scan_cancelled(self, monkeypatch, tmp_db):
        scan_id = db.create_spy_scan("2026-06-01", kind="options")
        monkeypatch.setattr(research_engine, "_ALLOC_POLL_SECONDS", 0.05)

        lock_held = research_engine._ALLOC_LOCK.acquire()
        assert lock_held
        db.request_spy_scan_cancel(scan_id)
        caught: dict[str, Any] = {}

        def worker():
            try:
                with research_engine._allocation_slot(scan_id):
                    caught["entered"] = True
            except Exception as exc:
                caught["exc"] = exc

        t = threading.Thread(target=worker)
        t.start()
        try:
            t.join(timeout=5)
            assert not t.is_alive()
            assert isinstance(caught.get("exc"), spy_scanner.ScanCancelled)
            assert "entered" not in caught
        finally:
            if lock_held:
                research_engine._ALLOC_LOCK.release()
                lock_held = False

    def test_cancel_requested_before_entry_does_not_yield(self, monkeypatch, tmp_db):
        scan_id = db.create_spy_scan("2026-06-01", kind="options")
        db.request_spy_scan_cancel(scan_id)

        body_ran = False
        with pytest.raises(spy_scanner.ScanCancelled):
            with research_engine._allocation_slot(scan_id):
                body_ran = True

        assert body_ran is False
        assert not research_engine._ALLOC_LOCK.locked()

    def test_timeout_raises_instead_of_hanging(self, monkeypatch, tmp_db):
        scan_id = db.create_spy_scan("2026-06-01", kind="options")
        monkeypatch.setattr(research_engine, "_ALLOC_POLL_SECONDS", 0.05)
        monkeypatch.setattr(research_engine, "_ALLOC_TIMEOUT_SECONDS", 0.1)

        lock_held = research_engine._ALLOC_LOCK.acquire()
        assert lock_held
        caught: dict[str, Any] = {}

        def worker():
            try:
                with research_engine._allocation_slot(scan_id):
                    caught["entered"] = True
            except Exception as exc:
                caught["exc"] = exc

        t = threading.Thread(target=worker)
        start = time_mod.monotonic()
        t.start()
        try:
            t.join(timeout=5)
            elapsed = time_mod.monotonic() - start
            assert not t.is_alive()
            exc = caught.get("exc")
            assert isinstance(exc, RuntimeError)
            assert "allocation slot" in str(exc)
            assert elapsed < 2
        finally:
            if lock_held:
                research_engine._ALLOC_LOCK.release()
                lock_held = False


# ── Market-open wait ─────────────────────────────────────────────────────────

class TestMarketWaitStatusSplit:
    """wait_for_market_open should label itself running_wait_market — distinct
    from running_alloc, which now means real vetting/allocation work — and the
    caller should flip back to running_alloc the moment the wait actually ends."""

    def test_wait_market_sets_status_before_open(self, monkeypatch, tmp_db):
        scan_id = db.create_spy_scan("2026-01-01", kind="options")

        # A Monday well before market open — the loop must write the wait
        # status at least once, then we cut it short via cancellation.
        calls = {"n": 0}

        def fake_now_et():
            calls["n"] += 1
            if calls["n"] > 2:
                # Let the loop exit after a couple of iterations by reporting
                # a cancelled scan next tick.
                db.request_spy_scan_cancel(scan_id)
            return datetime(2026, 6, 1, 7, 30, tzinfo=market_calendar._ET)  # Monday, before open

        monkeypatch.setattr(market_calendar, "now_et", fake_now_et)
        monkeypatch.setattr(research_engine.time_mod, "sleep", lambda _s: None)

        with pytest.raises(spy_scanner.ScanCancelled):
            research_engine.wait_for_market_open(scan_id)

        assert db.get_spy_scan_status(scan_id)["status"] == "running_wait_market"

    def test_wait_market_returns_immediately_after_open(self, monkeypatch, tmp_db):
        scan_id = db.create_spy_scan("2026-01-01", kind="options")

        monkeypatch.setattr(
            market_calendar, "now_et",
            lambda: datetime(2026, 6, 1, 10, 0, tzinfo=market_calendar._ET),  # Monday, after open
        )

        # Must return without ever sleeping or touching the DB status.
        monkeypatch.setattr(
            research_engine.time_mod, "sleep",
            lambda _s: (_ for _ in ()).throw(AssertionError("should not sleep")),
        )
        research_engine.wait_for_market_open(scan_id)  # no exception = pass
        assert db.get_spy_scan_status(scan_id)["status"] == "pending"


class TestWaitForMarketOpenHolidayAware:
    """Weekends and NYSE holidays return immediately; trading-day pre-open waits."""

    def _patch_sleep(self, monkeypatch):
        def raise_on_sleep(_s):
            raise AssertionError("should not sleep")
        monkeypatch.setattr(research_engine.time_mod, "sleep", raise_on_sleep)

    def test_returns_immediately_on_saturday(self, monkeypatch, tmp_db):
        scan_id = db.create_spy_scan("2026-06-06", kind="options")
        self._patch_sleep(monkeypatch)
        monkeypatch.setattr(
            market_calendar, "now_et",
            lambda: datetime(2026, 6, 6, 7, 0, tzinfo=market_calendar._ET),
        )
        research_engine.wait_for_market_open(scan_id)
        assert db.get_spy_scan_status(scan_id)["status"] == "pending"

    def test_returns_immediately_on_thanksgiving(self, monkeypatch, tmp_db):
        scan_id = db.create_spy_scan("2026-11-26", kind="options")
        self._patch_sleep(monkeypatch)
        monkeypatch.setattr(
            market_calendar, "now_et",
            lambda: datetime(2026, 11, 26, 7, 0, tzinfo=market_calendar._ET),
        )
        research_engine.wait_for_market_open(scan_id)
        assert db.get_spy_scan_status(scan_id)["status"] == "pending"

    def test_returns_immediately_at_market_open_on_trading_day(self, monkeypatch, tmp_db):
        scan_id = db.create_spy_scan("2026-06-01", kind="options")
        self._patch_sleep(monkeypatch)
        monkeypatch.setattr(
            market_calendar, "now_et",
            lambda: datetime(2026, 6, 1, 9, 35, tzinfo=market_calendar._ET),
        )
        research_engine.wait_for_market_open(scan_id)
        assert db.get_spy_scan_status(scan_id)["status"] == "pending"


# ── Research orchestration ───────────────────────────────────────────────────

def test_run_research_happy_path(tmp_db, monkeypatch):
    td = "2026-06-02"

    monkeypatch.setattr(research_engine, "get_sp500_tickers", lambda: ["AAA", "BBB", "CCC"])

    prescreen_calls: list[tuple[list[str], int, str]] = []

    def fake_prescreen(tickers, top_n, *, trade_date):
        prescreen_calls.append((list(tickers), top_n, trade_date))
        return ["AAA", "BBB"]

    monkeypatch.setattr(research_engine, "prescreen", fake_prescreen)

    config_calls: list[dict[str, Any]] = []

    def fake_build_config(params):
        config_calls.append(params)
        return {}

    monkeypatch.setattr(research_engine, "build_config", fake_build_config)

    quick_calls: list[tuple[int, list[str], str, dict[str, Any]]] = []

    def fake_quick_scan(scan_id, tickers, trade_date, config):
        quick_calls.append((scan_id, list(tickers), trade_date, config))
        return [
            {"ticker": "AAA", "signal": "BUY", "conviction": 9},
            {"ticker": "BBB", "signal": "SELL", "conviction": 8},
            {"ticker": "SPY", "signal": "HOLD", "conviction": 3},
        ]

    monkeypatch.setattr(spy_scanner, "run_quick_scan", fake_quick_scan)

    deep_calls: list[tuple[int, list[str], str, dict[str, Any], list[str]]] = []

    def fake_deep_dives(scan_id, candidates, trade_date, config, analysts):
        deep_calls.append((scan_id, [c["ticker"] for c in candidates], trade_date, config, analysts))
        return [
            {"ticker": "AAA", "signal": "BUY", "reused_from": 5},
            {"ticker": "BBB", "signal": "SELL", "error": "bad thing"},
            {"ticker": "SPY", "signal": "HOLD"},
        ]

    monkeypatch.setattr(spy_scanner, "run_deep_dives", fake_deep_dives)

    sid = db.create_spy_scan(td, kind="research", aggressiveness=10)
    research_engine.run_research(sid, td)

    scan = db.get_spy_scan(sid)
    assert scan["status"] == "completed"
    assert scan["portfolio_json"] == []

    assert len(config_calls) == 1
    assert config_calls[0]["bias"] == "neutral"
    assert config_calls[0]["aggressiveness"] == 10

    assert len(prescreen_calls) == 1
    assert prescreen_calls[0][1] == research_engine.PRESCREEN_TOP
    assert prescreen_calls[0][2] == td

    assert len(quick_calls) == 1
    assert quick_calls[0][1] == ["AAA", "BBB", "SPY"]

    assert len(deep_calls) == 1
    assert set(deep_calls[0][1]) == {"AAA", "BBB", "SPY"}

    report = scan["allocator_report"]
    assert "BUY" in report
    assert "reused: 1" in report
    assert "BBB" in report
    assert "bad thing" in report


def test_run_research_no_directional_signals_skips_deep_dives(tmp_db, monkeypatch):
    td = "2026-06-02"

    monkeypatch.setattr(research_engine, "get_sp500_tickers", lambda: ["AAA", "BBB"])
    monkeypatch.setattr(
        research_engine, "prescreen", lambda _t, _n, *, trade_date: ["AAA", "BBB"]
    )
    monkeypatch.setattr(research_engine, "build_config", lambda _p: {})

    def fake_quick_scan(_scan_id, _tickers, _trade_date, _config):
        return [
            {"ticker": "AAA", "signal": "HOLD", "conviction": 3},
            {"ticker": "BBB", "signal": "HOLD", "conviction": 2},
        ]

    monkeypatch.setattr(spy_scanner, "run_quick_scan", fake_quick_scan)

    def fail_deep(*args, **kwargs):
        raise AssertionError("run_deep_dives should not be called")

    monkeypatch.setattr(spy_scanner, "run_deep_dives", fail_deep)

    sid = db.create_spy_scan(td, kind="research", aggressiveness=5)
    research_engine.run_research(sid, td)

    scan = db.get_spy_scan(sid)
    assert scan["status"] == "completed"
    assert scan["deep_total"] == 0


def test_run_research_empty_prescreen_raises(tmp_db, monkeypatch):
    td = "2026-06-02"

    monkeypatch.setattr(research_engine, "get_sp500_tickers", lambda: ["AAA"])
    monkeypatch.setattr(
        research_engine, "prescreen", lambda _t, _n, *, trade_date: []
    )
    monkeypatch.setattr(research_engine, "build_config", lambda _p: {})

    sid = db.create_spy_scan(td, kind="research")
    with pytest.raises(RuntimeError, match="Movers pre-screen returned no tickers"):
        research_engine.run_research(sid, td)


def test_run_research_cancel_during_quick_scan(tmp_db, monkeypatch):
    td = "2026-06-02"

    monkeypatch.setattr(research_engine, "get_sp500_tickers", lambda: ["AAA"])
    monkeypatch.setattr(
        research_engine, "prescreen", lambda _t, _n, *, trade_date: ["AAA"]
    )
    monkeypatch.setattr(research_engine, "build_config", lambda _p: {})

    def fake_quick_scan(scan_id, _tickers, _trade_date, _config):
        db.request_spy_scan_cancel(scan_id)
        return [{"ticker": "AAA", "signal": "BUY", "conviction": 5}]

    monkeypatch.setattr(spy_scanner, "run_quick_scan", fake_quick_scan)

    sid = db.create_spy_scan(td, kind="research")
    with pytest.raises(spy_scanner.ScanCancelled):
        research_engine.run_research(sid, td)


def test_research_aggressiveness_default_with_no_accounts(tmp_db):
    assert research_engine.research_aggressiveness() == 5


def test_research_aggressiveness_uses_max_account_value(tmp_db):
    db.create_paper_account("conservative", aggressiveness=3)
    db.create_paper_account("aggressive", aggressiveness=10)
    assert research_engine.research_aggressiveness() == 10


def test_run_worker_cancels_on_scan_cancelled(tmp_db, monkeypatch):
    monkeypatch.setattr(scan_queue, "refresh_creds_from_db", lambda: None)
    monkeypatch.setattr(scan_queue, "_dequeue_next_scan", lambda: None)

    sid = db.create_spy_scan("2026-06-02", kind="research")

    def work(_s, _t):
        raise spy_scanner.ScanCancelled()

    research_engine.run_worker(
        sid, "2026-06-02", work, alert_kind="research", holds_slot=True
    )

    assert db.get_spy_scan_status(sid)["status"] == "cancelled"


def test_run_worker_fails_and_alerts_on_exception(tmp_db, monkeypatch):
    monkeypatch.setattr(scan_queue, "refresh_creds_from_db", lambda: None)
    monkeypatch.setattr(scan_queue, "_advance_queue_if_idle", lambda: None)

    notified: list[dict[str, Any]] = []

    def capture_notify(*, kind, run_id, label, error, link=None):
        notified.append({"kind": kind, "run_id": run_id, "label": label, "error": error})

    monkeypatch.setattr(alerts, "notify_run_failed", capture_notify)

    sid = db.create_spy_scan("2026-06-02", kind="research")

    def work(_s, _t):
        raise RuntimeError("boom")

    research_engine.run_worker(
        sid, "2026-06-02", work, alert_kind="research", holds_slot=False
    )

    assert db.get_spy_scan_status(sid)["status"] == "failed"
    assert db.get_spy_scan(sid)["error"] == "boom"
    assert len(notified) == 1
    assert notified[0]["kind"] == "research"
    assert notified[0]["run_id"] == sid
    assert notified[0]["label"] == "2026-06-02"
    assert notified[0]["error"] == "boom"


@pytest.mark.parametrize(
    "holds_slot,expected",
    [
        (True, "_dequeue_next_scan"),
        (False, "_advance_queue_if_idle"),
    ],
)
def test_run_worker_advancement_depends_on_holds_slot(
    tmp_db, monkeypatch, holds_slot, expected
):
    monkeypatch.setattr(scan_queue, "refresh_creds_from_db", lambda: None)

    called: dict[str, str | None] = {"name": None}

    def dequeue():
        called["name"] = "_dequeue_next_scan"

    def advance():
        called["name"] = "_advance_queue_if_idle"

    monkeypatch.setattr(scan_queue, "_dequeue_next_scan", dequeue)
    monkeypatch.setattr(scan_queue, "_advance_queue_if_idle", advance)

    sid = db.create_spy_scan("2026-06-02", kind="research")

    research_engine.run_worker(
        sid, "2026-06-02", lambda _s, _t: None, alert_kind="research", holds_slot=holds_slot
    )

    assert called["name"] == expected


def test_ensure_research_scan_creates_and_spawns(tmp_db, monkeypatch):
    spawns: list[tuple[Any, int, str]] = []

    def record_spawn(target, sid, td):
        spawns.append((target, sid, td))

    monkeypatch.setattr(scan_queue, "spawn_worker", record_spawn)
    monkeypatch.setitem(
        scan_queue._RUNNERS,
        "research",
        (SimpleNamespace(run=lambda s, t: None), "run"),
    )

    td = "2026-06-02"
    result = research_engine.ensure_research_scan(td)

    assert result["new"] is True
    assert result["status"] == "pending"
    sid = result["scan_id"]

    scan = db.get_spy_scan(sid)
    assert scan["kind"] == "research"
    assert scan["paper_account_id"] is None
    assert scan["bias"] == "neutral"
    assert len(spawns) == 1

    result2 = research_engine.ensure_research_scan(td)
    assert result2["new"] is False
    assert result2["scan_id"] == sid
    assert result2["status"] == "pending"
    assert len(spawns) == 1


def test_ensure_research_scan_after_failed_creates_new(tmp_db, monkeypatch):
    spawns: list[tuple[Any, int, str]] = []

    def record_spawn(target, sid, td):
        spawns.append((target, sid, td))

    monkeypatch.setattr(scan_queue, "spawn_worker", record_spawn)
    monkeypatch.setitem(
        scan_queue._RUNNERS,
        "research",
        (SimpleNamespace(run=lambda s, t: None), "run"),
    )

    td = "2026-06-02"
    sid = db.create_spy_scan(td, kind="research", aggressiveness=5)
    db.fail_spy_scan(sid, "old failure")

    result = research_engine.ensure_research_scan(td)

    assert result["new"] is True
    assert result["scan_id"] != sid
    assert result["status"] == "pending"
    assert len(spawns) == 1


def test_ensure_research_scan_queues_when_busy(tmp_db, monkeypatch):
    spawns: list[Any] = []

    def fail_spawn(*args, **kwargs):
        spawns.append(True)

    monkeypatch.setattr(scan_queue, "spawn_worker", fail_spawn)
    monkeypatch.setitem(
        scan_queue._RUNNERS,
        "research",
        (SimpleNamespace(run=lambda s, t: None), "run"),
    )

    td = "2026-06-02"
    busy_id = db.create_spy_scan(td, kind="equity", aggressiveness=5)
    db.update_spy_scan(busy_id, status="running_deep")

    result = research_engine.ensure_research_scan(td)

    assert result["new"] is True
    assert result["status"] == "queued"
    assert "queued_behind" in result
    assert result["queued_behind"]["id"] == busy_id
    assert len(spawns) == 0


def test_ensure_research_scan_missing_runner_fails(tmp_db, monkeypatch):
    monkeypatch.delitem(scan_queue._RUNNERS, "research", raising=False)

    td = "2026-06-02"
    with pytest.raises(RuntimeError, match="research runner not registered"):
        research_engine.ensure_research_scan(td)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM spy_scans WHERE kind = 'research' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert row is not None
    assert row["status"] == "failed"
    assert "research runner not registered" in row["error"]


def test_require_trading_day_saturday_raises():
    with pytest.raises(research_engine.NotTradingDay, match="not an NYSE trading day"):
        research_engine.require_trading_day("2026-06-06", force=False)


def test_require_trading_day_saturday_force_passes():
    research_engine.require_trading_day("2026-06-06", force=True)


def test_require_trading_day_tuesday_passes():
    research_engine.require_trading_day("2026-06-02", force=False)


# ── Per-account allocation core ──────────────────────────────────────────────

_ALLOC_TD = "2026-09-29"  # a Tuesday


def _et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=market_calendar._ET)


@pytest.fixture()
def clock(monkeypatch):
    """Mutable fake ET clock; the patched sleep advances it and never blocks."""
    c = SimpleNamespace(now=_et(2026, 9, 29, 10, 0), sleeps=[], on_sleep=None)
    monkeypatch.setattr(market_calendar, "now_et", lambda: c.now)

    def fake_sleep(seconds):
        c.sleeps.append(seconds)
        c.now = c.now + timedelta(seconds=seconds)
        if c.on_sleep is not None:
            c.on_sleep(len(c.sleeps))

    monkeypatch.setattr(research_engine.time_mod, "sleep", fake_sleep)
    monkeypatch.delenv("RESEARCH_DEADLINE_ET", raising=False)
    monkeypatch.delenv("RESEARCH_WAIT_MAX_MIN", raising=False)
    return c


def _record_statuses(monkeypatch, scan_id):
    statuses: list[str] = []
    real = db.update_spy_scan

    def recording(sid, **kwargs):
        if sid == scan_id and "status" in kwargs:
            statuses.append(kwargs["status"])
        real(sid, **kwargs)

    monkeypatch.setattr(db, "update_spy_scan", recording)
    return statuses


def _completed_research(td=_ALLOC_TD):
    rid = db.create_spy_scan(td, kind="research")
    db.complete_spy_scan(rid, "r", [])
    return rid


def _failed_research(td=_ALLOC_TD):
    rid = db.create_spy_scan(td, kind="research")
    db.fail_spy_scan(rid, "boom")
    return rid


def test_wait_for_research_returns_completed_row_without_sleeping(tmp_db, clock):
    rid = _completed_research()
    sid = db.create_spy_scan(_ALLOC_TD, kind="equity")

    research = research_engine.wait_for_research(sid, _ALLOC_TD)

    assert research["id"] == rid
    assert "quick_results" in research
    assert db.get_spy_scan(sid)["research_scan_id"] == rid
    assert clock.sleeps == []


def test_wait_for_research_picks_up_newer_completed_row_after_failure(tmp_db, clock):
    _failed_research()
    sid = db.create_spy_scan(_ALLOC_TD, kind="equity")
    created: list[int] = []

    def on_sleep(n):
        if n == 1:
            created.append(_completed_research())

    clock.on_sleep = on_sleep

    research = research_engine.wait_for_research(sid, _ALLOC_TD)

    assert research["id"] == created[0]
    assert len(clock.sleeps) == 1


def test_wait_for_research_missing_raises_after_max_wait(tmp_db, clock):
    sid = db.create_spy_scan(_ALLOC_TD, kind="equity")

    with pytest.raises(RuntimeError, match="status=missing"):
        research_engine.wait_for_research(sid, _ALLOC_TD)

    # start + 90 min (11:30) beats the 10:30 deadline.
    assert clock.now >= _et(2026, 9, 29, 11, 30)


def test_wait_for_research_env_deadline_failed_row(tmp_db, clock, monkeypatch):
    monkeypatch.setenv("RESEARCH_DEADLINE_ET", "09:10")
    monkeypatch.setenv("RESEARCH_WAIT_MAX_MIN", "5")
    clock.now = _et(2026, 9, 29, 9, 0)
    _failed_research()
    sid = db.create_spy_scan(_ALLOC_TD, kind="equity")

    with pytest.raises(RuntimeError, match="status=failed"):
        research_engine.wait_for_research(sid, _ALLOC_TD)

    assert clock.now >= _et(2026, 9, 29, 9, 10)
    assert clock.now < _et(2026, 9, 29, 9, 12)


@pytest.mark.parametrize("deadline,max_min", [("bogus", "x"), ("25:99", "0"), ("", "-3")])
def test_research_deadline_malformed_env_falls_back(monkeypatch, deadline, max_min):
    monkeypatch.setenv("RESEARCH_DEADLINE_ET", deadline)
    monkeypatch.setenv("RESEARCH_WAIT_MAX_MIN", max_min)
    start = _et(2026, 9, 29, 7, 0)
    assert research_engine._research_deadline(start) == _et(2026, 9, 29, 10, 30)
    late = _et(2026, 9, 29, 10, 0)
    assert research_engine._research_deadline(late) == _et(2026, 9, 29, 11, 30)


def test_wait_for_research_first_tick_heartbeats_status(tmp_db, clock, monkeypatch):
    sid = db.create_spy_scan(_ALLOC_TD, kind="equity", status="pending")
    statuses = _record_statuses(monkeypatch, sid)
    seen: list[str] = []

    def on_sleep(n):
        seen.append(db.get_spy_scan(sid)["status"])
        _completed_research()

    clock.on_sleep = on_sleep

    research_engine.wait_for_research(sid, _ALLOC_TD)

    assert statuses[0] == "running_wait_research"
    assert seen == ["running_wait_research"]


def test_wait_for_research_cancel_raises(tmp_db, clock):
    sid = db.create_spy_scan(_ALLOC_TD, kind="equity")
    db.request_spy_scan_cancel(sid)

    with pytest.raises(spy_scanner.ScanCancelled):
        research_engine.wait_for_research(sid, _ALLOC_TD)
    assert clock.sleeps == []


def test_wait_for_research_ignores_other_dates(tmp_db, clock, monkeypatch):
    monkeypatch.setenv("RESEARCH_DEADLINE_ET", "10:00")
    monkeypatch.setenv("RESEARCH_WAIT_MAX_MIN", "1")
    _completed_research("2026-09-28")
    sid = db.create_spy_scan(_ALLOC_TD, kind="equity")

    with pytest.raises(RuntimeError, match="status=missing"):
        research_engine.wait_for_research(sid, _ALLOC_TD)
    assert db.get_spy_scan(sid)["research_scan_id"] is None


@pytest.fixture()
def seeded_allocation(tmp_db, clock, monkeypatch):
    """A completed research row plus an equity allocation row at Tue 09:40 ET."""
    td = _ALLOC_TD
    rid = db.create_spy_scan(td, kind="research")

    aid_a = db.create_analysis({"ticker": "AAPL", "trade_date": td})
    db.complete_analysis(aid_a, {"final_trade_decision": "Rating: Buy"}, "Buy")
    db.upsert_spy_quick_result(rid, "AAPL", signal="Buy", conviction=9,
                               reasoning="good", analysis_id=aid_a)

    aid_b = db.create_analysis({"ticker": "BBB", "trade_date": td})
    db.fail_analysis(aid_b, "boom")
    db.upsert_spy_quick_result(rid, "BBB", signal="Sell", conviction=8,
                               reasoning="bad", analysis_id=aid_b)

    db.upsert_spy_quick_result(rid, "CCC", signal="Hold", conviction=3, reasoning="meh")

    db.update_spy_scan(rid, quick_count=3, quick_total=3, deep_count=2, deep_total=2,
                       deep_reused_count=1, quick_fingerprint="fp")
    db.complete_spy_scan(rid, "r", [])

    pid = db.create_paper_account("eq", aggressiveness=3, bias="bearish")
    sid = db.create_spy_scan(td, paper_account_id=pid, aggressiveness=7,
                             bias="bullish", kind="equity")

    clock.now = _et(2026, 9, 29, 9, 40)
    configs: list[dict[str, Any]] = []
    monkeypatch.setattr(research_engine, "build_config",
                        lambda p: configs.append(p) or {"cfg": True})
    price_calls: list[list[str]] = []

    def fake_prices(tickers, *, log_label="prices"):
        price_calls.append(list(tickers))
        return {"AAPL": 101.0, "ZZZ": 5.0}

    monkeypatch.setattr(spy_scanner, "fetch_live_prices", fake_prices)
    return SimpleNamespace(td=td, rid=rid, sid=sid, pid=pid,
                           configs=configs, price_calls=price_calls)


def test_run_allocation_builds_context_and_copies_research(seeded_allocation, monkeypatch):
    s = seeded_allocation
    statuses = _record_statuses(monkeypatch, s.sid)
    calls: list[tuple[Any, bool]] = []

    def allocate(ctx):
        calls.append((ctx, research_engine._ALLOC_LOCK.locked()))

    research_engine.run_allocation(s.sid, s.td, allocate, extra_tickers=["zzz"])

    assert len(calls) == 1
    ctx, locked = calls[0]
    assert locked is True
    assert research_engine._ALLOC_LOCK.locked() is False
    assert isinstance(ctx, research_engine.AllocationContext)
    assert ctx.research["id"] == s.rid
    assert [r["ticker"] for r in ctx.usable] == ["AAPL"]
    assert ctx.usable[0]["entry_price"] == 101.0
    assert len(ctx.quick_results) == 3
    assert ctx.aggressiveness == 7
    assert ctx.bias == "bullish"
    assert ctx.account["id"] == s.pid
    assert ctx.trade_date == s.td
    assert ctx.live_prices == {"AAPL": 101.0, "ZZZ": 5.0}
    assert ctx.config == {"cfg": True}
    assert s.configs[0]["aggressiveness"] == 7
    assert s.configs[0]["bias"] == "bullish"

    assert s.price_calls == [["AAPL", "ZZZ"]]

    row = db.get_spy_scan(s.sid)
    assert row["research_scan_id"] == s.rid
    assert row["quick_fingerprint"] == "fp"
    assert (row["quick_count"], row["quick_total"], row["deep_count"],
            row["deep_total"], row["deep_reused_count"]) == (3, 3, 2, 2, 1)
    assert len(row["quick_results"]) == 3

    wanted = ["running_wait_research", "running_wait_market", "running_alloc"]
    idx = [statuses.index(w) for w in wanted]
    assert idx == sorted(idx)


def test_run_allocation_price_failure_still_allocates(seeded_allocation, monkeypatch):
    s = seeded_allocation

    def boom(tickers, *, log_label="prices"):
        raise RuntimeError("quotes down")

    monkeypatch.setattr(spy_scanner, "fetch_live_prices", boom)
    calls: list[Any] = []

    research_engine.run_allocation(s.sid, s.td, calls.append)

    assert len(calls) == 1
    assert calls[0].live_prices == {}
    assert calls[0].usable[0]["entry_price"] is None


def test_run_allocation_allocate_error_propagates_and_releases_lock(seeded_allocation):
    s = seeded_allocation

    def allocate(ctx):
        raise ValueError("bad allocation")

    with pytest.raises(ValueError, match="bad allocation"):
        research_engine.run_allocation(s.sid, s.td, allocate)
    assert research_engine._ALLOC_LOCK.locked() is False


def test_run_allocation_requires_paper_account(tmp_db, clock):
    sid = db.create_spy_scan(_ALLOC_TD, kind="equity")
    with pytest.raises(RuntimeError, match="paper_account_id"):
        research_engine.run_allocation(sid, _ALLOC_TD, lambda ctx: None)


_SIGNALS = ["Buy", "overweight", "HOLD", "Underweight", "sell"]
_KEEP = {"BUY", "OVERWEIGHT", "HOLD"}


@pytest.mark.parametrize("signal", _SIGNALS)
@pytest.mark.parametrize("held", [True, False])
@pytest.mark.parametrize("price", [42, None])
def test_equity_candidates_matrix(signal, held, price):
    row = {"ticker": "abc", "signal": signal, "conviction": 5, "entry_price": price}
    before = dict(row)
    held_tickers = ["ABC"] if held else ["XYZ"]

    out = research_engine.equity_candidates([row], held_tickers)

    assert row == before
    expected = (signal.upper() in _KEEP or held) and price is not None
    if not expected:
        assert out == []
        return
    assert len(out) == 1
    assert out[0] is not row
    assert out[0]["ticker"] == "ABC"
    assert out[0]["signal"] == signal
    assert isinstance(out[0]["entry_price"], float)
    assert out[0]["entry_price"] == 42.0


def test_equity_candidates_keeps_held_sell_and_preserves_order():
    usable = [
        {"ticker": "aaa", "signal": "Sell", "entry_price": 10},
        {"ticker": "BBB", "signal": "Buy", "entry_price": 0},
        {"ticker": "CCC", "signal": "Hold", "entry_price": "12.5"},
        {"ticker": "DDD", "signal": "Sell", "entry_price": 3},
    ]
    out = research_engine.equity_candidates(usable, {"Aaa"})
    assert [r["ticker"] for r in out] == ["AAA", "CCC"]
    assert out[1]["entry_price"] == 12.5
    assert usable[0]["ticker"] == "aaa"


@pytest.fixture()
def start_env(tmp_db, monkeypatch):
    spawns: list[tuple[Any, int, str]] = []
    monkeypatch.setattr(scan_queue, "spawn_worker",
                        lambda target, sid, td: spawns.append((target, sid, td)))

    def spy_runner(s, t):
        return None

    def options_runner(s, t):
        return None

    def research_runner(s, t):
        return None

    monkeypatch.setitem(scan_queue._RUNNERS, "spy", (SimpleNamespace(run=spy_runner), "run"))
    monkeypatch.setitem(scan_queue._RUNNERS, "options",
                        (SimpleNamespace(run=options_runner), "run"))
    monkeypatch.setitem(scan_queue._RUNNERS, "research",
                        (SimpleNamespace(run=research_runner), "run"))
    pid = db.create_paper_account("acct", aggressiveness=6, bias="bearish")
    return SimpleNamespace(spawns=spawns, account=db.get_paper_account(pid), pid=pid,
                           spy=spy_runner, options=options_runner, research=research_runner)


def _research_rows(td):
    with db.connect() as conn:
        return conn.execute(
            "SELECT id, status FROM spy_scans WHERE kind = 'research' AND trade_date = ?",
            (td,),
        ).fetchall()


def test_start_allocation_saturday_requires_force(start_env):
    sat = "2026-09-26"
    with pytest.raises(research_engine.NotTradingDay):
        research_engine.start_allocation(start_env.account, sat, "equity")
    assert start_env.spawns == []

    _completed_research(sat)
    res = research_engine.start_allocation(start_env.account, sat, "equity", force=True)
    assert res["new"] is True
    assert len(start_env.spawns) == 1


def test_start_allocation_bad_kind(start_env):
    with pytest.raises(ValueError):
        research_engine.start_allocation(start_env.account, _ALLOC_TD, "research")
    assert start_env.spawns == []


def test_start_allocation_with_completed_research_is_idempotent(start_env):
    rid = _completed_research()

    res = research_engine.start_allocation(start_env.account, _ALLOC_TD, "equity")

    assert res["new"] is True
    assert res["account_id"] == start_env.pid
    assert res["status"] == "running_wait_research"
    assert res["research"] == {"scan_id": rid, "status": "completed", "new": False}
    row = db.get_spy_scan(res["scan_id"])
    assert row["kind"] == "equity"
    assert row["paper_account_id"] == start_env.pid
    assert row["aggressiveness"] == 6
    assert row["bias"] == "bearish"
    assert row["status"] == "running_wait_research"
    assert row["research_scan_id"] == rid
    assert start_env.spawns == [(start_env.spy, res["scan_id"], _ALLOC_TD)]

    again = research_engine.start_allocation(start_env.account, _ALLOC_TD, "equity")
    assert again["new"] is False
    assert again["scan_id"] == res["scan_id"]
    assert again["status"] == "running_wait_research"
    assert len(start_env.spawns) == 1


def test_start_allocation_explicit_overrides_and_options_runner(start_env):
    _completed_research()

    res = research_engine.start_allocation(start_env.account, _ALLOC_TD, "options",
                                           aggressiveness=9, bias="bullish")

    row = db.get_spy_scan(res["scan_id"])
    assert row["kind"] == "options"
    assert row["aggressiveness"] == 9
    assert row["bias"] == "bullish"
    assert start_env.spawns == [(start_env.options, res["scan_id"], _ALLOC_TD)]


def test_start_allocation_kicks_research_when_none_today(start_env):
    res = research_engine.start_allocation(start_env.account, _ALLOC_TD, "equity")

    rows = _research_rows(_ALLOC_TD)
    assert len(rows) == 1
    assert res["research"]["new"] is True
    assert res["research"]["scan_id"] == rows[0]["id"]
    assert db.get_spy_scan(res["scan_id"])["research_scan_id"] is None
    assert [t for t, _, _ in start_env.spawns] == [start_env.research, start_env.spy]
    assert start_env.spawns[0][1] == rows[0]["id"]
    assert start_env.spawns[1][1] == res["scan_id"]


def test_start_allocation_after_failed_research_does_not_rekick(start_env):
    fid = _failed_research()

    res = research_engine.start_allocation(start_env.account, _ALLOC_TD, "equity")

    rows = _research_rows(_ALLOC_TD)
    assert [r["id"] for r in rows] == [fid]
    assert res["research"] == {"scan_id": fid, "status": "failed", "new": False}
    assert start_env.spawns == [(start_env.spy, res["scan_id"], _ALLOC_TD)]


def test_start_allocation_missing_runner_fails_row(start_env, monkeypatch):
    _completed_research()
    monkeypatch.delitem(scan_queue._RUNNERS, "spy")

    with pytest.raises(RuntimeError, match="no allocation runner"):
        research_engine.start_allocation(start_env.account, _ALLOC_TD, "equity")

    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, error FROM spy_scans WHERE kind = 'equity' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["status"] == "failed"
    assert "no allocation runner" in row["error"]
    assert start_env.spawns == []
