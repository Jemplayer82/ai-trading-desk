"""run_options_allocation end-to-end over a seeded shared research row: the
global allocation lock is held during the post-wait phase and released
afterward, the account's stop policy reaches the allocator, and the research's
quick rows and live spot hints flow through. The market-wait and allocation-
slot tests live in tests/test_research_engine.py (tier-3 shared code).

No network access. Run with: uv run pytest tests/test_options_market_wait.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from web import (
    alerts,
    db,
    market_calendar,
    options_engine,
    options_routes,
    research_engine,
    scan_queue,
    spy_scanner,
)
from web.account_policy import StopPolicy

pytestmark = pytest.mark.unit

TD = "2026-06-06"  # a Saturday: the market-open wait returns at once


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()


def _seed_research(trade_date: str, deep_total: int = 1,
                   failed_dives: tuple[str, ...] = (), usable: bool = True) -> int:
    """Seed a completed shared research row. ``deep_total`` is set explicitly
    (the schema default of 50 would otherwise leak into the zero-candidate
    reason). AAPL is a successful Buy deep dive when ``usable``; each ticker in
    ``failed_dives`` is a directional quick row whose deep dive failed (no
    analysis), so db.list_deep_dived_results excludes it."""
    rid = db.create_spy_scan(trade_date, kind="research")
    if usable:
        aid = db.create_analysis({"ticker": "AAPL", "trade_date": trade_date})
        db.complete_analysis(aid, {"final_trade_decision": "Rating: Buy"}, "Buy")
        db.upsert_spy_quick_result(rid, "AAPL", signal="Buy", conviction=8,
                                   reasoning="r", analysis_id=aid)
    for t in failed_dives:
        db.upsert_spy_quick_result(rid, t, signal="Buy", conviction=7, reasoning="r")
    db.update_spy_scan(rid, deep_total=deep_total)
    db.complete_spy_scan(rid, "research", [])
    return rid


def _no_sleep(_seconds):
    raise AssertionError("must not sleep: research is complete and the market wait is a no-op")


class TestRunOptionsAllocation:
    """run_options_allocation end-to-end: lock held during refresh_positions,
    released after; stop policy passed to the allocator; research linked and
    copied; live quote used as the chain spot hint."""

    def _install_fakes(self, monkeypatch, captured):
        monkeypatch.setattr(
            market_calendar, "now_et",
            lambda: datetime(2026, 6, 6, 10, 0, tzinfo=market_calendar._ET),
        )
        monkeypatch.setattr(research_engine.time_mod, "sleep", _no_sleep)
        monkeypatch.setattr(spy_scanner, "fetch_live_prices", lambda t, **k: {"AAPL": 200.0})

        def fake_fetch_candidates(signals, **kw):
            captured["fetch_rows"] = [dict(r) for r in signals]
            return [], []

        monkeypatch.setattr(options_engine.options_data, "fetch_candidates", fake_fetch_candidates)

        def fake_refresh_positions(paper_account_id=None):
            captured["lock_during_refresh"] = research_engine._ALLOC_LOCK.locked()
            assert captured["lock_during_refresh"] is True
            return {}

        monkeypatch.setattr(options_engine, "refresh_positions", fake_refresh_positions)

        def fake_allocator_run(*args, **kwargs):
            captured["allocator_kwargs"] = kwargs
            return {"closes": [], "holds": [], "opens": [], "report_md": "x"}

        monkeypatch.setattr(options_engine.options_allocator, "run", fake_allocator_run)

    def _run(self, monkeypatch, account_id, **seed: Any) -> tuple[int, int, dict[str, Any]]:
        rid = _seed_research(TD, **seed)
        scan_id = db.create_spy_scan(TD, kind="options", paper_account_id=account_id,
                                     status="running_wait_research")
        captured: dict[str, Any] = {}
        self._install_fakes(monkeypatch, captured)
        options_engine.run_options_allocation(scan_id, TD)
        return scan_id, rid, captured

    def test_holds_allocation_lock_and_consumes_research(self, tmp_db, monkeypatch):
        account_id = db.create_paper_account("lock-e2e", 100_000.0, kind="options")
        scan_id, rid, captured = self._run(monkeypatch, account_id)

        assert captured["lock_during_refresh"] is True
        assert not research_engine._ALLOC_LOCK.locked()
        assert db.get_spy_scan_status(scan_id)["status"] == "completed"

        row = db.get_spy_scan(scan_id)
        assert row["research_scan_id"] == rid
        assert "AAPL" in {r["ticker"] for r in row["quick_results"]}

        fetched = {r["ticker"]: r for r in captured["fetch_rows"]}
        assert "AAPL" in fetched
        assert fetched["AAPL"]["entry_price"] == 200.0

    def test_passes_trailing_pct_policy(self, tmp_db, monkeypatch):
        account_id = db.create_paper_account(
            "policy-trailing", starting_capital=100_000.0,
            kind="options", stop_type="trailing_pct", stop_value=25.0,
        )
        scan_id, _, captured = self._run(monkeypatch, account_id)

        assert captured["allocator_kwargs"]["policy"] == StopPolicy("trailing_pct", 25.0, None)
        assert db.get_spy_scan_status(scan_id)["status"] == "completed"

    def test_passes_none_policy_for_defaults(self, tmp_db, monkeypatch):
        account_id = db.create_paper_account(
            "policy-default", starting_capital=100_000.0, kind="options"
        )
        scan_id, _, captured = self._run(monkeypatch, account_id)

        assert captured["allocator_kwargs"]["policy"] == StopPolicy("none", None, None)
        assert db.get_spy_scan_status(scan_id)["status"] == "completed"

    # --- zero-candidate reason: n_targets must come from research deep_total ---

    @staticmethod
    def _why_no_new(scan_id: int) -> str:
        report = db.get_spy_scan(scan_id)["allocator_report"] or ""
        assert "## Why no new positions" in report
        return report.split("## Why no new positions", 1)[1]

    def test_all_deep_dives_failed_is_not_reported_as_quiet_market(self, tmp_db, monkeypatch):
        # deep_total=3, every dive failed -> usable=[]. n_targets must be 3
        # (research deep_total), not len(usable)=0, or the report would blame a
        # quiet market ("Nothing to trade") for a total deep-dive failure.
        account_id = db.create_paper_account("all-dives-failed", 100_000.0, kind="options")
        scan_id, _, captured = self._run(monkeypatch, account_id, deep_total=3,
                                         failed_dives=("MSFT", "NVDA", "TSLA"), usable=False)

        assert captured["fetch_rows"] == []
        assert db.get_spy_scan_status(scan_id)["status"] == "completed"
        why = self._why_no_new(scan_id)
        assert "All 3 deep dives failed" in why
        assert "Nothing to trade" not in why

    def test_partial_deep_dive_failure_counts_against_research_deep_total(
            self, tmp_db, monkeypatch):
        account_id = db.create_paper_account("partial-dives", 100_000.0, kind="options")
        scan_id, _, _ = self._run(monkeypatch, account_id, deep_total=3,
                                  failed_dives=("MSFT", "NVDA"))

        why = self._why_no_new(scan_id)
        assert "1 of 1 deep dives rated a directional call" in why
        assert "(2 further dives failed and were skipped)" in why

    def test_all_dives_usable_reports_no_further_failures(self, tmp_db, monkeypatch):
        account_id = db.create_paper_account("all-dives-ok", 100_000.0, kind="options")
        scan_id, _, _ = self._run(monkeypatch, account_id, deep_total=1)

        why = self._why_no_new(scan_id)
        assert "1 of 1 deep dives rated a directional call" in why
        assert "further dives failed" not in why


def _advancing_clock(start: datetime, step: timedelta):
    """now_et fake that moves forward by ``step`` on every call, so a wait
    loop always reaches its deadline however many other callers read it."""
    state = {"now": start}

    def _now():
        cur = state["now"]
        state["now"] = cur + step
        return cur

    return _now


def test_missing_research_past_deadline_fails_allocation(tmp_db, monkeypatch):
    td = "2026-06-09"  # a Tuesday
    account_id = db.create_paper_account("no-research", 100_000.0, kind="options")
    scan_id = db.create_spy_scan(td, kind="options", paper_account_id=account_id,
                                 status="running_wait_research")
    monkeypatch.setenv("RESEARCH_WAIT_MAX_MIN", "1")

    def clock():
        return _advancing_clock(datetime(2026, 6, 9, 11, 0, tzinfo=market_calendar._ET),
                                timedelta(minutes=2))

    monkeypatch.setattr(market_calendar, "now_et", clock())
    monkeypatch.setattr(research_engine.time_mod, "sleep", lambda s: None)

    with pytest.raises(RuntimeError, match="research"):
        options_engine.run_options_allocation(scan_id, td)

    # The thread wrapper records the failure and advances the queue with the
    # idle guard (allocation rows never hold the compute slot).
    monkeypatch.setattr(market_calendar, "now_et", clock())
    alerts_seen: list[dict[str, Any]] = []
    monkeypatch.setattr(alerts, "notify_run_failed", lambda **kw: alerts_seen.append(kw))
    advanced: list[int] = []
    dequeued: list[int] = []
    monkeypatch.setattr(scan_queue, "_advance_queue_if_idle", lambda: advanced.append(1))
    monkeypatch.setattr(scan_queue, "_dequeue_next_scan", lambda: dequeued.append(1))
    monkeypatch.setattr(scan_queue, "refresh_creds_from_db", lambda: None)

    options_routes._run_options_scan_thread(scan_id, td)

    assert db.get_spy_scan_status(scan_id)["status"] == "failed"
    assert [a["kind"] for a in alerts_seen] == ["Options allocation"]
    assert advanced == [1]
    assert dequeued == []
