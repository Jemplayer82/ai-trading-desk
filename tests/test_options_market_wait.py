"""run_options_build end-to-end: the global allocation lock is held during
the post-wait phase and released afterward. The market-wait and allocation-
slot tests now live in tests/test_research_engine.py because their helpers
were extracted into web/research_engine.py (tier-3 shared code).

No network access. Run with: uv run pytest tests/test_options_market_wait.py -v
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from web import db, market_calendar, options_engine, research_engine
from web.account_policy import StopPolicy

pytestmark = pytest.mark.unit


class TestRunOptionsBuildHoldsAllocationLock:
    """run_options_build end-to-end: the global allocation lock is held during
    the post-wait phase and released afterward. Also guards that prescreen
    is invoked with the keyword-only trade_date argument, and that the
    account's stop policy is passed to the allocator."""

    def _install_fakes(self, monkeypatch, captured, trade_date):
        monkeypatch.setattr(options_engine, "get_sp500_tickers", lambda: ["AAPL"])

        def fake_prescreen(tickers, top_n=None, *, trade_date=None):
            captured["prescreen_trade_date"] = trade_date
            assert trade_date == trade_date  # keyword-only, asserted below too
            return ["AAPL"]

        monkeypatch.setattr(options_engine, "prescreen", fake_prescreen)

        def fake_run_quick_scan(scan_id_arg, movers, trade_date_arg, config):
            return [{"ticker": "AAPL", "signal": "HOLD", "conviction": 1}]

        monkeypatch.setattr(
            options_engine.spy_scanner, "run_quick_scan", fake_run_quick_scan
        )

        monkeypatch.setattr(
            market_calendar, "now_et",
            lambda: datetime(2026, 6, 6, 10, 0, tzinfo=market_calendar._ET),
        )

        def fake_refresh_positions(paper_account_id=None):
            captured["lock_during_refresh"] = research_engine._ALLOC_LOCK.locked()
            assert captured["lock_during_refresh"] is True

        monkeypatch.setattr(options_engine, "refresh_positions", fake_refresh_positions)

        def fake_allocator_run(*args, **kwargs):
            captured["allocator_kwargs"] = kwargs
            return {
                "closes": [],
                "holds": [],
                "opens": [],
                "report_md": "test allocation report",
            }

        monkeypatch.setattr(options_engine.options_allocator, "run", fake_allocator_run)

    def test_run_options_build_holds_allocation_lock_during_refresh_positions(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()

        trade_date = "2026-06-06"
        account_id = db.create_paper_account("lock-e2e", 100_000.0, "options")
        scan_id = db.create_spy_scan(
            trade_date, kind="options", paper_account_id=account_id
        )

        captured: dict[str, Any] = {}
        self._install_fakes(monkeypatch, captured, trade_date)

        options_engine.run_options_build(scan_id, trade_date)

        assert captured["prescreen_trade_date"] == trade_date
        assert captured["lock_during_refresh"] is True
        assert not research_engine._ALLOC_LOCK.locked()
        assert db.get_spy_scan_status(scan_id)["status"] == "completed"

    def test_run_options_build_passes_trailing_pct_policy(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()

        trade_date = "2026-06-06"
        account_id = db.create_paper_account(
            "policy-trailing", starting_capital=100_000.0,
            kind="options", stop_type="trailing_pct", stop_value=25.0,
        )
        scan_id = db.create_spy_scan(
            trade_date, kind="options", paper_account_id=account_id
        )

        captured: dict[str, Any] = {}
        self._install_fakes(monkeypatch, captured, trade_date)

        options_engine.run_options_build(scan_id, trade_date)

        assert captured["allocator_kwargs"]["policy"] == StopPolicy("trailing_pct", 25.0, None)
        assert db.get_spy_scan_status(scan_id)["status"] == "completed"

    def test_run_options_build_passes_none_policy_for_defaults(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()

        trade_date = "2026-06-06"
        account_id = db.create_paper_account(
            "policy-default", starting_capital=100_000.0, kind="options"
        )
        scan_id = db.create_spy_scan(
            trade_date, kind="options", paper_account_id=account_id
        )

        captured: dict[str, Any] = {}
        self._install_fakes(monkeypatch, captured, trade_date)

        options_engine.run_options_build(scan_id, trade_date)

        assert captured["allocator_kwargs"]["policy"] == StopPolicy("none", None, None)
        assert db.get_spy_scan_status(scan_id)["status"] == "completed"
