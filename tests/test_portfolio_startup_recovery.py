"""Portfolio startup fails spy_scans rows orphaned by a crash/restart.

Every spy_scans worker is a thread inside the single-process portfolio app, so
a row still in flight at startup is dead. Leaving it would block
ensure_research_scan / start_allocation until the scheduler reaper's 120-min
stall, which can land past the 05:30 ET research retry cutoff.
"""
from __future__ import annotations

import pytest

from web import alerts, db, features, portfolio_main, research_engine, scan_queue
from web import credentials as creds

pytestmark = pytest.mark.unit

TODAY = "2026-09-25"


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


@pytest.fixture()
def startup_env(tmp_db, monkeypatch):
    sent: list[dict] = []
    kicks: list[int] = []
    monkeypatch.setattr(creds, "apply_to_env", lambda *a, **k: None)
    monkeypatch.setattr(creds, "apply_settings_to_env", lambda *a, **k: None)
    monkeypatch.setattr(features, "enabled", lambda name: True)
    monkeypatch.setattr(alerts, "notify_run_failed", lambda **kw: sent.append(kw))
    monkeypatch.setattr(scan_queue, "_advance_queue_if_idle", lambda: kicks.append(1))
    return sent, kicks


class TestFailInterruptedSpyScans:
    def test_fails_only_in_flight_rows(self, tmp_db):
        live = {
            "running": db.create_spy_scan(TODAY, status="running", kind="research"),
            "running_wait_research": db.create_spy_scan(
                TODAY, paper_account_id=1, status="running_wait_research", kind="equity"),
            "running_alloc": db.create_spy_scan(
                TODAY, paper_account_id=2, status="running_alloc", kind="options"),
            "pending": db.create_spy_scan(TODAY, status="pending", kind="research"),
        }
        idle = {s: db.create_spy_scan(TODAY, status=s, kind="equity")
                for s in ("queued", "completed", "cancelled")}
        prior_failed = db.create_spy_scan(TODAY, kind="equity")
        db.fail_spy_scan(prior_failed, "earlier failure")

        rows = db.fail_interrupted_spy_scans("interrupted")

        assert {r["id"]: r["status"] for r in rows} == {v: k for k, v in live.items()}
        for sid in live.values():
            got = db.get_spy_scan(sid)
            assert got["status"] == "failed"
            assert got["error"] == "interrupted"
        for status, sid in idle.items():
            got = db.get_spy_scan(sid)
            assert got["status"] == status
            assert not got.get("error")
        assert db.get_spy_scan(prior_failed)["error"] == "earlier failure"


class TestStartupRecovery:
    def test_fails_orphans_alerts_and_kicks_queue(self, startup_env):
        sent, kicks = startup_env
        research = db.create_spy_scan(TODAY, status="running", kind="research")
        alloc = db.create_spy_scan(
            TODAY, paper_account_id=1, status="running_wait_research", kind="equity")

        portfolio_main._startup()

        for sid in (research, alloc):
            got = db.get_spy_scan(sid)
            assert got["status"] == "failed"
            assert "restarted" in got["error"]
        assert sorted(a["kind"] for a in sent) == ["Research", "S&P 500 scan (allocation)"]
        assert kicks == [1]

    def test_dead_research_row_no_longer_blocks_new_research(self, startup_env, monkeypatch):
        dead = db.create_spy_scan(TODAY, status="running", kind="research")
        portfolio_main._startup()

        spawned: list[tuple] = []
        monkeypatch.setattr(scan_queue, "resolve_runner", lambda key: (lambda *a: None))
        monkeypatch.setattr(scan_queue, "spawn_worker", lambda *a: spawned.append(a))

        result = research_engine.ensure_research_scan(TODAY)

        assert result["new"] is True
        assert result["scan_id"] != dead
        assert len(spawned) == 1

    def test_no_orphans_no_alerts(self, startup_env):
        sent, kicks = startup_env
        db.create_spy_scan(TODAY, status="queued", kind="research")

        portfolio_main._startup()

        assert sent == []
        assert kicks == [1]

    def test_recovery_failure_never_raises(self, startup_env, monkeypatch):
        sent, kicks = startup_env

        def boom(_err):
            raise RuntimeError("db down")

        monkeypatch.setattr(db, "fail_interrupted_spy_scans", boom)

        portfolio_main._startup()  # must not raise

        assert sent == []
        assert kicks == [1]
