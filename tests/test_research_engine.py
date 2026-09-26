"""Unit tests for the shared research engine.

The research engine is tier-3 code: it must not import any ``options_*``
module at module level and it ships in every tier that has the shared
S&P 500 research pipeline.
"""
from __future__ import annotations

import threading
import time as time_mod
from datetime import datetime
from typing import Any

import pandas as pd
import pytest

from web import db, market_cache, market_calendar, research_engine, spy_scanner

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
