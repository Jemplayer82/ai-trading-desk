"""Unit tests for the shared same-day market cache primitive.

These tests live at the tier-independent layer (`web.market_cache`) so
they survive builds where tier-gated modules such as `web.spy_scanner.py`
or `web.research_engine.py` are removed.
"""

from __future__ import annotations

import threading

import pytest

from web import market_cache
from web.market_cache import SameDayCache

pytestmark = pytest.mark.unit


def test_miss_put_hit_and_stats(monkeypatch):
    monkeypatch.setattr(market_cache, "_now", lambda: 0.0)
    cache = SameDayCache("test", 60.0)
    value = {"data": 1}

    assert cache.get("2026-08-07", "a") is None
    cache.put("2026-08-07", "a", value)
    assert cache.get("2026-08-07", "a") is value

    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["size"] == 1


def test_two_keys_same_date_coexist(monkeypatch):
    monkeypatch.setattr(market_cache, "_now", lambda: 0.0)
    cache = SameDayCache("test", 60.0)

    cache.put("2026-08-07", "a", 1)
    cache.put("2026-08-07", "b", 2)

    assert cache.get("2026-08-07", "a") == 1
    assert cache.get("2026-08-07", "b") == 2
    assert cache.stats()["size"] == 2


def test_expiry_at_ttl_boundary(monkeypatch):
    monkeypatch.setattr(market_cache, "_now", lambda: 0.0)
    cache = SameDayCache("test", 100.0)

    cache.put("2026-08-07", "k", "v")
    monkeypatch.setattr(market_cache, "_now", lambda: 100.0)

    assert cache.get("2026-08-07", "k") is None

    stats = cache.stats()
    assert stats["expired"] == 1
    assert stats["misses"] == 1
    assert stats["size"] == 0


def test_not_yet_expired_before_ttl(monkeypatch):
    monkeypatch.setattr(market_cache, "_now", lambda: 0.0)
    cache = SameDayCache("test", 100.0)
    value = object()

    cache.put("2026-08-07", "k", value)
    monkeypatch.setattr(market_cache, "_now", lambda: 99.9)

    assert cache.get("2026-08-07", "k") is value
    assert cache.stats()["hits"] == 1


def test_date_rollover_evicts_prior_day(monkeypatch):
    monkeypatch.setattr(market_cache, "_now", lambda: 0.0)
    cache = SameDayCache("test", 1000.0)
    v1 = object()
    v2 = object()

    cache.put("2026-08-07", "k", v1)
    cache.put("2026-08-08", "k", v2)

    assert cache.get("2026-08-07", "k") is None
    assert cache.get("2026-08-08", "k") is v2

    stats = cache.stats()
    assert stats["evicted"] == 1
    assert stats["size"] == 1


def test_rollover_evicts_all_prior_day_keys(monkeypatch):
    monkeypatch.setattr(market_cache, "_now", lambda: 0.0)
    cache = SameDayCache("test", 1000.0)

    cache.put("2026-08-07", "k1", "v1")
    cache.put("2026-08-07", "k2", "v2")
    cache.put("2026-08-07", "k3", "v3")
    cache.put("2026-08-08", "k1", "v_new")

    assert cache.get("2026-08-07", "k1") is None
    assert cache.get("2026-08-07", "k2") is None
    assert cache.get("2026-08-07", "k3") is None
    assert cache.get("2026-08-08", "k1") == "v_new"

    stats = cache.stats()
    assert stats["evicted"] == 3
    assert stats["size"] == 1


def test_clear_empties_store(monkeypatch):
    monkeypatch.setattr(market_cache, "_now", lambda: 0.0)
    cache = SameDayCache("test", 60.0)

    cache.put("2026-08-07", "k", "v")
    cache.clear()

    assert cache.get("2026-08-07", "k") is None
    assert cache.stats()["size"] == 0


def test_concurrent_get_put_smoke():
    cache = SameDayCache("concurrent", 3600.0)
    date = "2026-08-07"
    keys = ["a", "b", "c", "d", "e"]
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for i in range(200):
                key = keys[i % len(keys)]
                cache.get(date, key)
                cache.put(date, key, f"{key}-{i}")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert cache.stats()["size"] == len(keys)


def test_no_compute_under_lock_api():
    # SameDayCache deliberately exposes no get_or_compute helper.  Holding
    # a lock across a long-running yfinance download would serialize fetches
    # and create lock-ordering hazards against research_engine._ALLOC_LOCK.
    assert not hasattr(SameDayCache, "get_or_compute")
