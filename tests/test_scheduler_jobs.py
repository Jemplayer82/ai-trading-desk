"""Tests for web/scheduler.py's tier-gated register_jobs()."""
from __future__ import annotations

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

import web.db
from web import scheduler


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, tmp_path):
    monkeypatch.setattr(web.db, "DB_PATH", tmp_path / "scheduler.db")
    web.db.init_db()


def _registered_ids(monkeypatch, features_env: str) -> set[str]:
    monkeypatch.setenv("FEATURES", features_env)
    sched = BackgroundScheduler()
    scheduler.register_jobs(sched)
    return {job.id for job in sched.get_jobs()}


class TestRegisterJobs:
    def test_tier1_registers_only_universal_jobs(self, monkeypatch):
        ids = _registered_ids(monkeypatch, "")
        assert ids == {"reap_stuck_runs", "outcome_sweep", "schedule_reconciler"}

    def test_tier2_adds_schwab_jobs(self, monkeypatch):
        ids = _registered_ids(monkeypatch, "schwab")
        assert ids == {
            "reap_stuck_runs", "outcome_sweep", "schedule_reconciler",
            "nightly_scan", "morning_newsletter", "token_health",
        }

    def test_tier3_adds_sp500_jobs(self, monkeypatch):
        ids = _registered_ids(monkeypatch, "schwab,sp500")
        assert ids == {
            "reap_stuck_runs", "outcome_sweep", "schedule_reconciler",
            "nightly_scan", "morning_newsletter", "token_health",
            "spy_price_refresh", "research_scan", "research_retry",
        }

    def test_tier4_registers_all_seventeen_jobs(self, monkeypatch):
        ids = _registered_ids(monkeypatch, "schwab,sp500,options")
        assert ids == {
            "reap_stuck_runs", "outcome_sweep", "schedule_reconciler",
            "nightly_scan", "morning_newsletter", "token_health",
            "spy_price_refresh", "research_scan", "research_retry",
            "options_refresh", "options_refresh_close",
            "options_settle", "options_grade", "rules_run", "rules_prepare", "spreads_scan", "spreads_eod",
        }
        assert len(ids) == 17

    def test_schwab_alone_has_no_research_jobs(self, monkeypatch):
        ids = _registered_ids(monkeypatch, "schwab")
        assert "research_scan" not in ids
        assert "research_retry" not in ids

    def test_options_alone_without_sp500_does_not_register_sp500_jobs(self, monkeypatch):
        # Not a real tier combination (options implies sp500 in the cumulative
        # model), but register_jobs should still gate each block independently.
        ids = _registered_ids(monkeypatch, "options")
        assert "spy_price_refresh" not in ids
        assert "options_refresh" in ids

    def test_no_legacy_account_scan_ids_at_any_tier(self, monkeypatch):
        for features_env in ("", "schwab", "schwab,sp500", "schwab,sp500,options", "options"):
            ids = _registered_ids(monkeypatch, features_env)
            assert "spy_scan" not in ids, features_env
            assert "options_scan" not in ids, features_env

    def test_schedule_reconciler_registered_at_every_tier(self, monkeypatch):
        for features_env in ("", "schwab", "schwab,sp500", "schwab,sp500,options"):
            monkeypatch.setenv("FEATURES", features_env)
            sched = BackgroundScheduler()
            scheduler.register_jobs(sched)
            job = sched.get_job("schedule_reconciler")
            assert job is not None, features_env
            assert isinstance(job.trigger, IntervalTrigger), features_env
            assert job.trigger.interval.total_seconds() == 60, features_env

    def test_nightly_scan_time_uses_settings_value(self, monkeypatch):
        monkeypatch.setenv("FEATURES", "schwab")
        web.db.set_app_setting("SCHEDULE_NIGHTLY_SCAN_TIME", "06:45")
        sched = BackgroundScheduler()
        scheduler.register_jobs(sched)
        job = sched.get_job("nightly_scan")
        trigger_str = str(job.trigger)
        assert "hour='6'" in trigger_str
        assert "minute='45'" in trigger_str
        assert "mon-fri" in trigger_str

    def test_nightly_scan_time_defaults_to_2200_when_unset(self, monkeypatch):
        monkeypatch.setenv("FEATURES", "schwab")
        sched = BackgroundScheduler()
        scheduler.register_jobs(sched)
        job = sched.get_job("nightly_scan")
        trigger_str = str(job.trigger)
        assert "hour='22'" in trigger_str
        assert "minute='0'" in trigger_str
        assert "mon-fri" in trigger_str

    def test_nightly_scan_time_defaults_to_2200_on_garbage_setting(self, monkeypatch):
        monkeypatch.setenv("FEATURES", "schwab")
        web.db.set_app_setting("SCHEDULE_NIGHTLY_SCAN_TIME", "nope")
        sched = BackgroundScheduler()
        scheduler.register_jobs(sched)  # must not raise
        job = sched.get_job("nightly_scan")
        trigger_str = str(job.trigger)
        assert "hour='22'" in trigger_str
        assert "minute='0'" in trigger_str

    def test_research_time_uses_settings_value(self, monkeypatch):
        monkeypatch.setenv("FEATURES", "schwab,sp500")
        web.db.set_app_setting("SCHEDULE_RESEARCH_TIME", "01:15")
        sched = BackgroundScheduler()
        scheduler.register_jobs(sched)
        job = sched.get_job("research_scan")
        trigger_str = str(job.trigger)
        assert "hour='1'" in trigger_str
        assert "minute='15'" in trigger_str
        assert "mon-fri" in trigger_str

    def test_research_time_defaults_to_midnight_when_unset(self, monkeypatch):
        monkeypatch.setenv("FEATURES", "schwab,sp500")
        sched = BackgroundScheduler()
        scheduler.register_jobs(sched)
        job = sched.get_job("research_scan")
        trigger_str = str(job.trigger)
        assert "hour='0'" in trigger_str
        assert "minute='0'" in trigger_str
        assert "mon-fri" in trigger_str

    def test_research_time_defaults_to_midnight_on_garbage_setting(self, monkeypatch):
        monkeypatch.setenv("FEATURES", "schwab,sp500")
        web.db.set_app_setting("SCHEDULE_RESEARCH_TIME", "nope")
        sched = BackgroundScheduler()
        scheduler.register_jobs(sched)  # must not raise
        job = sched.get_job("research_scan")
        trigger_str = str(job.trigger)
        assert "hour='0'" in trigger_str
        assert "minute='0'" in trigger_str

    def test_research_retry_is_fifteen_minute_interval(self, monkeypatch):
        monkeypatch.setenv("FEATURES", "schwab,sp500")
        sched = BackgroundScheduler()
        scheduler.register_jobs(sched)
        job = sched.get_job("research_retry")
        assert job is not None
        assert job.func is scheduler.job_research_retry
        assert isinstance(job.trigger, IntervalTrigger)
        assert job.trigger.interval.total_seconds() == 15 * 60
