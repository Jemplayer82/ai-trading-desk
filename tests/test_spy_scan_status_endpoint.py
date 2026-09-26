"""Unit tests for the /status polling fix (the confirmed ~38 MiB/hr leak).

GET /api/spy-scans/{id} (and the options equivalent) return the entire scan —
up to ~500 spy_quick_results rows — and were polled every 5s for a running
scan's full duration. get_spy_scan_status is the O(1) replacement the 5s
poller now hits instead; the full endpoint is only re-fetched when something
in the cheap row actually changes.

Covers:
  - get_spy_scan_status: payload stays small regardless of row count, returns
    None for a missing scan, includes kind (needed by the options 404 guard)
  - upsert_spy_quick_result: reasoning/error get truncated at write time
  - _is_any_scan_running: running_wait_market does NOT count as busy (a scan
    sitting in the daily market-open wait is doing no work; serialization of
    the real allocation phase is handled by research_engine.allocation_lock, while
    the proactive queue hand-off from the old options wait was removed)

See tests/test_research_engine.py for the running_wait_market /
running_alloc status-split tests around research_engine.wait_for_market_open
— those moved out because the market wait is now tier-3 shared code (this
file survives down to tier 3).

No network access. Run with: uv run pytest tests/test_spy_scan_status_endpoint.py -v
"""
from __future__ import annotations

import json

import pytest

from web import db, scan_queue


@pytest.mark.unit
class TestGetSpyScanStatus:
    def test_payload_size_independent_of_row_count(self, monkeypatch, tmp_path):
        """The whole point of this endpoint: O(1) regardless of quick_results size."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01")

        for i in range(500):
            db.upsert_spy_quick_result(
                scan_id=scan_id,
                ticker=f"T{i}",
                signal="BUY",
                conviction=7,
                reasoning="x" * 500,  # near the truncation cap
            )
        db.update_spy_scan(scan_id, quick_count=500, quick_total=500)

        status = db.get_spy_scan_status(scan_id)
        payload_bytes = len(json.dumps(status).encode("utf-8"))

        # The full endpoint with 500 such rows would be hundreds of KB; this
        # must stay tiny no matter how large quick_results grows.
        assert payload_bytes < 1024, (
            f"status payload was {payload_bytes} bytes — get_spy_scan_status "
            "must not scale with row count"
        )
        assert "quick_results" not in status, "status must not include the heavy join"
        assert status["quick_count"] == 500

    def test_missing_scan_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        assert db.get_spy_scan_status(999) is None

    def test_includes_kind_for_options_guard(self, monkeypatch, tmp_path):
        """The /api/options-scans/{id}/status route 404s when kind != 'options' —
        it needs kind in this row to enforce that, same as get_options_scan does."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        equity_id = db.create_spy_scan("2026-01-01", kind="equity")
        options_id = db.create_spy_scan("2026-01-01", kind="options")

        assert db.get_spy_scan_status(equity_id)["kind"] == "equity"
        assert db.get_spy_scan_status(options_id)["kind"] == "options"

    def test_reflects_live_progress_fields(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01")
        db.update_spy_scan(scan_id, status="running_deep", quick_count=150,
                           quick_total=150, deep_count=3, deep_total=50)

        status = db.get_spy_scan_status(scan_id)
        assert status["status"] == "running_deep"
        assert status["quick_count"] == 150
        assert status["deep_count"] == 3
        assert status["cancel_requested"] == 0


@pytest.mark.unit
class TestQuickResultTruncation:
    def test_reasoning_and_error_truncated_at_write(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01")

        db.upsert_spy_quick_result(
            scan_id=scan_id, ticker="AAPL",
            reasoning="x" * 5000, error="y" * 5000,
        )

        row = db.get_spy_quick_result(scan_id, "AAPL")
        assert len(row["reasoning"]) == db._QUICK_RESULT_TEXT_MAX
        assert len(row["error"]) == db._QUICK_RESULT_TEXT_MAX

    def test_short_text_unaffected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01")

        db.upsert_spy_quick_result(scan_id=scan_id, ticker="AAPL", reasoning="short")
        row = db.get_spy_quick_result(scan_id, "AAPL")
        assert row["reasoning"] == "short"

    def test_none_stays_none(self, monkeypatch, tmp_path):
        """A partial upsert (e.g. the deep pass adding only analysis_id) must not
        crash trying to slice None."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01")

        db.upsert_spy_quick_result(scan_id=scan_id, ticker="AAPL", signal="BUY")
        row = db.get_spy_quick_result(scan_id, "AAPL")
        assert row["reasoning"] is None
        assert row["error"] is None


@pytest.mark.unit
class TestIsAnyScanRunningIgnoresWaitMarket:
    """A scan parked in running_wait_market (up to ~2h/day, doing nothing but
    sleeping while waiting for 09:35 ET) must NOT count as 'busy' — it is
    consuming no compute, LLM budget, or CPU. Serialization of the real
    allocation phase is handled by research_engine.allocation_lock, not by this
    busy check; the proactive queue hand-off from the old options wait was
    removed."""

    def test_wait_market_status_does_not_count_as_running(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01", kind="options")
        db.update_spy_scan(scan_id, status="running_wait_market")

        with db.connect() as conn:
            busy = scan_queue._is_any_scan_running(conn)
        assert busy is None, "running_wait_market must not hold the queue slot"

    def test_terminal_statuses_do_not_count_as_running(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01", kind="options", status="completed")

        with db.connect() as conn:
            busy = scan_queue._is_any_scan_running(conn)
        assert busy is None
        assert scan_id  # keep the id referenced; the assertion is on `busy`

    @pytest.mark.parametrize(
        "status", ("pending", "running_quick", "running_deep", "running_alloc")
    )
    def test_active_statuses_still_count_as_running(self, status, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01")
        db.update_spy_scan(scan_id, status=status)

        with db.connect() as conn:
            busy = scan_queue._is_any_scan_running(conn)
        assert busy is not None, f"{status} must still be treated as busy"
        assert isinstance(busy, dict)
        assert busy["id"] == scan_id
