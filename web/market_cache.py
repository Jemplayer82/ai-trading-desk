"""In-process, same-trading-day cache primitive.

Three web-layer caches (in `web/spy_scanner.py`, `web/research_engine.py`,
and `web/options_data.py`) need identical semantics: cache by
`(trade_date, key)`, expire entries by TTL, and evict a prior trading day's
data on the first write for a new date.  This module centralises that
behaviour so callers do not duplicate the locking / eviction logic.

The cache is process-local only.  This application has no Redis backend,
so every container keeps its own copy and there is no cross-instance
coherence guarantee.

Correctness across trade_date boundaries is more important than hit rate:
a prior day's entry can never be served once a new trade date has been
written, and the first `put` for a new date evicts *all* stale-date entries.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Hashable
from typing import Any


def _now() -> float:
    """Monotonic timestamp used by every cache operation.

    Tests monkeypatch this function to control expiry deterministically;
    `SameDayCache` must never call `time.monotonic()` directly.
    """
    return time.monotonic()


class SameDayCache:
    """Thread-safe in-process cache keyed by `(trade_date, key)`.

    The lock is held only while touching the internal dictionary and the
    counters.  There is intentionally no `get_or_compute` / single-flight
    API: on a cache miss every caller fetches independently and the last
    successful `put` wins.  Holding the lock across a long-running fetch
    (for example a 30-second yfinance download) would serialise those
    fetches and create lock-ordering hazards against the per-account
    allocation locks (`research_engine.allocation_lock`).

    Values are stored by reference.  Callers that mutate a cached value
    must copy it on `get` and/or `put` themselves.
    """

    def __init__(self, name: str, ttl_seconds: float) -> None:
        self._name = name
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._store: dict[tuple[str, Hashable], tuple[float, Any]] = {}
        self._hits = 0
        self._misses = 0
        self._expired = 0
        self._evicted = 0

    def get(self, trade_date: str, key: Hashable) -> Any | None:
        """Return the cached value, or None if absent or expired.

        A hit does not extend the TTL.  This method never evicts entries
        belonging to other trade dates.
        """
        with self._lock:
            entry = self._store.get((trade_date, key))
            if entry is None:
                self._misses += 1
                return None

            stored_at, value = entry
            if _now() - stored_at >= self._ttl_seconds:
                del self._store[(trade_date, key)]
                self._expired += 1
                self._misses += 1
                return None

            self._hits += 1
            return value

    def put(self, trade_date: str, key: Hashable, value: Any) -> None:
        """Store `value` under `(trade_date, key)`.

        Before storing, every entry whose stored `trade_date` differs from
        `trade_date` is evicted.  This is the date-rollover guarantee: a
        prior trading day's entry can never be served into a later date.
        """
        with self._lock:
            stale_keys = [k for k in self._store if k[0] != trade_date]
            for stale_key in stale_keys:
                del self._store[stale_key]
            self._evicted += len(stale_keys)

            self._store[(trade_date, key)] = (_now(), value)

    def clear(self) -> None:
        """Remove every entry from the cache."""
        with self._lock:
            self._store.clear()

    def stats(self) -> dict[str, int]:
        """Return a snapshot of cache counters and current size."""
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "expired": self._expired,
                "evicted": self._evicted,
                "size": len(self._store),
            }
