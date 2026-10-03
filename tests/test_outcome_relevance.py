"""Tier-agnostic tests for the outcome-sweep relevance gate.

The sweep's relevance set is computed in web.scheduler.py (the only container
with DB access) and passed into resolve_all_pending. These tests exercise
web.db's new recent_analysis_tickers helper and scheduler._sweep_relevant_tickers
without touching any tier-4-only modules.
"""
from datetime import datetime, timedelta
from unittest import mock

import pytest

import web.db as db
import web.scheduler as scheduler

pytestmark = pytest.mark.unit


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


class TestRecentAnalysisTickers:
    def test_recent_analysis_returns_ticker(self, tmp_db):
        db.create_analysis({"ticker": "NVDA", "trade_date": "2026-08-03"})
        cutoff = (datetime.utcnow() - timedelta(days=365)).isoformat(timespec="seconds") + "Z"
        assert db.recent_analysis_tickers(cutoff) == ["NVDA"]

    def test_stale_analysis_filtered_by_cutoff(self, tmp_db):
        sid = db.create_analysis({"ticker": "INTC", "trade_date": "2026-08-03"})
        stale = (datetime.utcnow() - timedelta(days=60)).isoformat(timespec="seconds") + "Z"
        with db.connect() as conn:
            conn.execute("UPDATE analyses SET created_at = ? WHERE id = ?", (stale, sid))
        cutoff_14 = (datetime.utcnow() - timedelta(days=14)).isoformat(timespec="seconds") + "Z"
        cutoff_90 = (datetime.utcnow() - timedelta(days=90)).isoformat(timespec="seconds") + "Z"
        assert "INTC" not in db.recent_analysis_tickers(cutoff_14)
        assert "INTC" in db.recent_analysis_tickers(cutoff_90)

    def test_distinct_tickers(self, tmp_db):
        db.create_analysis({"ticker": "AMD", "trade_date": "2026-08-03"})
        db.create_analysis({"ticker": "AMD", "trade_date": "2026-08-04"})
        cutoff = (datetime.utcnow() - timedelta(days=1)).isoformat(timespec="seconds") + "Z"
        assert db.recent_analysis_tickers(cutoff) == ["AMD"]

    def test_uppercases_tickers(self, tmp_db):
        db.create_analysis({"ticker": "nvda", "trade_date": "2026-08-03"})
        cutoff = (datetime.utcnow() - timedelta(days=1)).isoformat(timespec="seconds") + "Z"
        assert db.recent_analysis_tickers(cutoff) == ["NVDA"]

    def test_empty_db(self, tmp_db):
        cutoff = (datetime.utcnow() - timedelta(days=1)).isoformat(timespec="seconds") + "Z"
        assert db.recent_analysis_tickers(cutoff) == []


class TestSweepRelevantTickers:
    def test_includes_always_relevant(self, tmp_db, monkeypatch):
        monkeypatch.setenv("FEATURES", "")
        result = scheduler._sweep_relevant_tickers()
        assert result is not None
        assert "SPY" in result

    def test_includes_recent_analysis_ticker(self, tmp_db):
        db.create_analysis({"ticker": "AMD", "trade_date": "2026-08-03"})
        sid = db.create_analysis({"ticker": "STALE", "trade_date": "2026-08-03"})
        stale = (datetime.utcnow() - timedelta(days=60)).isoformat(timespec="seconds") + "Z"
        with db.connect() as conn:
            conn.execute("UPDATE analyses SET created_at = ? WHERE id = ?", (stale, sid))
        result = scheduler._sweep_relevant_tickers()
        assert result is not None
        assert "AMD" in result
        assert "STALE" not in result

    def test_includes_open_equity_holdings(self, tmp_db, monkeypatch):
        monkeypatch.setenv("FEATURES", "schwab,sp500")
        acct = db.create_paper_account(name="Test Equity", kind="equity")
        sid = db.create_spy_scan("2026-08-03", paper_account_id=acct, kind="equity")
        db.complete_spy_scan(sid, "report", [
            {"ticker": "AAPL", "action": "BUY"},
            {"ticker": "MSFT", "action": "EXITED"},
        ])
        result = scheduler._sweep_relevant_tickers()
        assert result is not None
        assert "AAPL" in result
        assert "MSFT" not in result

    def test_equity_holdings_skipped_when_not_enabled(self, tmp_db, monkeypatch):
        monkeypatch.setenv("FEATURES", "")
        acct = db.create_paper_account(name="Test Equity", kind="equity")
        sid = db.create_spy_scan("2026-08-03", paper_account_id=acct, kind="equity")
        db.complete_spy_scan(sid, "report", [
            {"ticker": "AAPL", "action": "BUY"},
        ])
        result = scheduler._sweep_relevant_tickers()
        assert result is not None
        assert "SPY" in result
        assert "AAPL" not in result

    def test_includes_open_options_underlyings(self, tmp_db, monkeypatch):
        monkeypatch.setenv("FEATURES", "schwab,sp500,options")
        acct = db.create_paper_account(name="Test Options", kind="options")
        scan_id = db.create_spy_scan("2026-08-03", paper_account_id=acct, kind="options")
        db.open_options_position(acct, scan_id, {
            "occ_symbol": "TSLA 240801C00250000",
            "underlying": "TSLA",
            "put_call": "C",
            "strike": 250.0,
            "expiration_date": "2026-08-01",
            "contracts": 1,
            "entry_premium": 5.0,
            "entry_bid": 5.0, "entry_ask": 5.0,
        })
        result = scheduler._sweep_relevant_tickers()
        assert result is not None
        assert "TSLA" in result

    def test_everything_uppercase(self, tmp_db):
        db.create_analysis({"ticker": "amd", "trade_date": "2026-08-03"})
        result = scheduler._sweep_relevant_tickers()
        assert result is not None
        assert "AMD" in result
        assert "amd" not in result

    def test_single_source_failure_fails_open(self, tmp_db, monkeypatch):
        def boom(cutoff):
            raise RuntimeError("boom")
        monkeypatch.setattr(db, "recent_analysis_tickers", boom)
        result = scheduler._sweep_relevant_tickers()
        assert result is None

    def test_empty_set_returns_none(self, tmp_db, monkeypatch):
        monkeypatch.setenv("FEATURES", "")
        monkeypatch.setattr(scheduler, "_ALWAYS_RELEVANT", ())
        monkeypatch.setattr(db, "recent_analysis_tickers", lambda cutoff: [])
        result = scheduler._sweep_relevant_tickers()
        assert result is None

    def test_whole_body_failure_fails_open_to_none(self, tmp_db, monkeypatch):
        """An exception outside the per-source try/except branches must hit the outer catch.

        Patching _ALWAYS_RELEVANT to a non-iterable makes ``out.update(_ALWAYS_RELEVANT)``
        raise after all inner sources have succeeded, so the only handler that can catch it
        is the function-level ``except Exception`` that returns ``None``.
        """
        monkeypatch.setenv("FEATURES", "")
        monkeypatch.setattr(db, "recent_analysis_tickers", lambda cutoff: [])
        monkeypatch.setattr(scheduler, "_ALWAYS_RELEVANT", 1)
        result = scheduler._sweep_relevant_tickers()
        assert result is None


class TestSweepWiring:
    def test_sweep_receives_relevant_tickers(self, tmp_db, monkeypatch):
        monkeypatch.setattr(scheduler, "_sweep_relevant_tickers", lambda: {"SPY", "NVDA"})
        mock_resolve = mock.MagicMock(return_value={
            "resolved": 0,
            "noise": 0,
            "censored": 0,
            "immature": 0,
            "budget_deferred": 0,
            "errors": 0,
            "llm_reflections": 0,
            "irrelevant": 0,
        })
        monkeypatch.setattr(
            "tradingagents.graph.outcome_resolution.resolve_all_pending",
            mock_resolve,
        )
        monkeypatch.setattr(
            "tradingagents.agents.utils.memory.TradingMemoryLog",
            mock.MagicMock(),
        )
        monkeypatch.setattr(
            "tradingagents.llm_clients.create_llm_client",
            mock.MagicMock(),
        )

        scheduler.job_outcome_sweep()

        assert mock_resolve.called
        call_kwargs = mock_resolve.call_args.kwargs
        assert call_kwargs.get("relevant_tickers") == {"SPY", "NVDA"}
        assert "max_reflections" in call_kwargs

    def test_sweep_runs_ungated_when_relevance_fails_open(self, tmp_db, monkeypatch):
        monkeypatch.setattr(scheduler, "_sweep_relevant_tickers", lambda: None)
        mock_resolve = mock.MagicMock(return_value={
            "resolved": 0,
            "noise": 0,
            "censored": 0,
            "immature": 0,
            "budget_deferred": 0,
            "errors": 0,
            "llm_reflections": 0,
            "irrelevant": 0,
        })
        monkeypatch.setattr(
            "tradingagents.graph.outcome_resolution.resolve_all_pending",
            mock_resolve,
        )
        monkeypatch.setattr(
            "tradingagents.agents.utils.memory.TradingMemoryLog",
            mock.MagicMock(),
        )
        monkeypatch.setattr(
            "tradingagents.llm_clients.create_llm_client",
            mock.MagicMock(),
        )

        scheduler.job_outcome_sweep()

        assert mock_resolve.called
        assert mock_resolve.call_args.kwargs.get("relevant_tickers") is None
