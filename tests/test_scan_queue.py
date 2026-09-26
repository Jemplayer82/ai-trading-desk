"""Tests for web/scan_queue.py — the runner registry and its tier degrade path.

The queue itself ships at every tier, but the workers live in tier-gated route
modules (portfolio_routes / spy_routes / options_routes). A lower tier simply
never imports the module for a scan kind, so _dequeue_next_scan can meet a
queued row it has no runner for. That must fail the row and move on, not raise
and not wedge the queue.

Ordering/locking behavior for the normal dispatch paths lives in
tests/test_options_lifecycle.py.
"""
from __future__ import annotations

import importlib
import types
from types import SimpleNamespace

import pytest

from web import db, features, portfolio_main, scan_queue

pytestmark = pytest.mark.unit


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


@pytest.fixture()
def thread_spy(monkeypatch):
    """Capture threading.Thread(...) calls made by _dequeue_next_scan.

    Patches only scan_queue's reference to the threading module, so the real
    threading module (and the already-constructed _SCAN_LOCK) are untouched.
    """
    calls: list[dict] = []

    def _thread(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(start=lambda: None)

    monkeypatch.setattr(scan_queue, "threading", SimpleNamespace(Thread=_thread))
    return calls


class TestMissingRunnerDegrades:
    """No runner registered for a scan kind => fail the row, start nothing."""

    def test_queued_spy_row_is_failed(self, tmp_db, monkeypatch, thread_spy):
        monkeypatch.setattr(scan_queue, "_RUNNERS", {})
        scan_id = db.create_spy_scan("2026-08-03", status="queued", kind="equity")

        scan_queue._dequeue_next_scan()  # must not raise

        assert db.get_spy_scan(scan_id)["status"] == "failed"
        assert thread_spy == [], "no worker thread may be started"

    def test_queued_options_row_is_failed(self, tmp_db, monkeypatch, thread_spy):
        """Options rows live in spy_scans too — they must take the spy fail path."""
        monkeypatch.setattr(scan_queue, "_RUNNERS", {})
        acct = db.create_paper_account("Tier Test", kind="options")
        scan_id = db.create_spy_scan("2026-08-03", paper_account_id=acct,
                                     status="queued", kind="options")

        scan_queue._dequeue_next_scan()

        assert db.get_spy_scan(scan_id)["status"] == "failed"
        assert thread_spy == []

    def test_queued_portfolio_row_is_failed(self, tmp_db, monkeypatch, thread_spy):
        monkeypatch.setattr(scan_queue, "_RUNNERS", {})
        scan_id = db.create_portfolio_scan("2026-08-03", status="queued")

        scan_queue._dequeue_next_scan()

        assert db.get_portfolio_scan(scan_id)["status"] == "failed"
        assert thread_spy == []

    def test_failed_row_leaves_the_queue(self, tmp_db, monkeypatch, thread_spy):
        """The row must not be re-selected forever: failing it drops it out of
        the 'queued' set so the next dequeue moves on to the following row."""
        monkeypatch.setattr(scan_queue, "_RUNNERS", {})
        first = db.create_spy_scan("2026-08-03", status="queued", kind="equity")
        second = db.create_spy_scan("2026-08-03", status="queued", kind="equity")
        with db.connect() as conn:
            conn.execute("UPDATE spy_scans SET created_at = '2026-08-03T00:00:00Z' WHERE id = ?", (first,))
            conn.execute("UPDATE spy_scans SET created_at = '2026-08-03T00:00:01Z' WHERE id = ?", (second,))

        scan_queue._dequeue_next_scan()
        scan_queue._dequeue_next_scan()

        assert db.get_spy_scan(first)["status"] == "failed"
        assert db.get_spy_scan(second)["status"] == "failed"


class TestRunnerRegistry:
    def test_dispatch_resolves_the_runner_live(self, tmp_db, monkeypatch, thread_spy):
        """register_runner stores (module, name), not the function object, so a
        later monkeypatch of that attribute is what actually gets dispatched.
        Capturing the function at registration time would silently run the real
        worker under test — see the monkeypatch-based tests in
        tests/test_options_lifecycle.py."""
        holder = SimpleNamespace(_worker=lambda scan_id, trade_date: None)
        monkeypatch.setattr(scan_queue, "_RUNNERS", {})
        scan_queue.register_runner("spy", holder, "_worker")

        def _patched(scan_id, trade_date):
            return None

        holder._worker = _patched  # rebind AFTER registration
        scan_id = db.create_spy_scan("2026-08-03", status="queued", kind="equity")

        scan_queue._dequeue_next_scan()

        assert len(thread_spy) == 1
        assert thread_spy[0]["target"] is _patched
        assert thread_spy[0]["args"] == (scan_id, "2026-08-03")
        assert db.get_spy_scan(scan_id)["status"] == "running_quick"


class TestResearchDispatch:
    """kind='research' spy rows dispatch to the runner registered under "research"."""

    def test_queued_research_row_dispatches_to_research_runner(self, tmp_db, monkeypatch):
        td = "2026-08-03"
        calls: list[tuple] = []

        def recorder(*args):
            calls.append(args)

        ns = types.SimpleNamespace(run=recorder)
        monkeypatch.setitem(scan_queue._RUNNERS, "research", (ns, "run"))
        monkeypatch.setattr(scan_queue, "spawn_worker", lambda target, *a: target(*a))
        scan_id = db.create_spy_scan(td, kind="research", status="queued")

        scan_queue._dequeue_next_scan()

        assert calls == [(scan_id, td)]
        assert db.get_spy_scan(scan_id)["status"] == "running_quick"

    def test_queued_research_row_fails_without_runner(self, tmp_db, monkeypatch):
        monkeypatch.delitem(scan_queue._RUNNERS, "research", raising=False)
        started: list[tuple] = []
        monkeypatch.setattr(scan_queue, "spawn_worker", lambda target, *a: started.append((target, a)))
        scan_id = db.create_spy_scan("2026-08-03", kind="research", status="queued")

        scan_queue._dequeue_next_scan()

        row = db.get_spy_scan(scan_id)
        assert row["status"] == "failed"
        assert row["error"] == "scan kind not supported at this tier"
        assert started == []

    def test_research_routes_runner_resolved_live(self, tmp_db, monkeypatch, thread_spy):
        """Importing web.research_routes registers its runner. Because
        resolve_runner uses a live getattr, monkeypatching the module attribute
        after registration is what gets dispatched."""
        rr = pytest.importorskip("web.research_routes")
        scan_queue.register_runner("research", rr, "_run_research_thread")
        sentinel = lambda scan_id, trade_date: None
        monkeypatch.setattr(rr, "_run_research_thread", sentinel)
        td = "2026-08-03"
        scan_id = db.create_spy_scan(td, kind="research", status="queued")

        scan_queue._dequeue_next_scan()

        assert len(thread_spy) == 1
        assert thread_spy[0]["target"] is sentinel
        assert thread_spy[0]["args"] == (scan_id, td)
        assert db.get_spy_scan(scan_id)["status"] == "running_quick"


class TestSpawnWorkerAndResolveRunner:
    def test_spawn_worker_runs_target_in_daemon_thread(self):
        calls: list[tuple] = []

        def fn(*args):
            calls.append(args)

        t = scan_queue.spawn_worker(fn, 1, "x")
        t.join(timeout=5)

        assert t.daemon is True
        assert not t.is_alive()
        assert calls == [(1, "x")]

    def test_resolve_runner_unknown_key_is_none(self):
        assert scan_queue.resolve_runner("nope") is None


class TestPortfolioAppTierGating:
    """The shell mounts route modules per tier; the default tier must expose the
    exact pre-split path set."""

    @pytest.fixture()
    def app_paths(self, monkeypatch):
        def _load(**env):
            monkeypatch.delenv("TIER", raising=False)
            monkeypatch.delenv("FEATURES", raising=False)
            for k, v in env.items():
                monkeypatch.setenv(k, v)
            importlib.reload(features)
            importlib.reload(portfolio_main)
            return {r.path for r in portfolio_main.app.routes if hasattr(r, "path")}

        yield _load
        # Leave web.portfolio_main.app back at the default tier for any test
        # that imports it later (e.g. tests/test_clear_history.py's TestClient).
        monkeypatch.delenv("TIER", raising=False)
        monkeypatch.delenv("FEATURES", raising=False)
        importlib.reload(features)
        importlib.reload(portfolio_main)

    def test_default_tier_has_every_path_for_its_features(self, app_paths):
        # No env override: exercises whatever DEFAULT_TIER is physically on
        # disk (4 on master, rewritten to 1/2/3 by scripts/make_tier.py on a
        # stripped tree). Forcing FEATURES beyond what's physically present
        # would ImportError — a stripped tier's route modules for a
        # not-enabled feature are genuinely deleted, not just gated off — so
        # the expected set is derived from features.enabled(), not hardcoded.
        paths = app_paths()
        expected = {"/api/health", "/api/auth/schwab/status",
                    "/api/portfolio/status", "/api/portfolio/advance-queue"}
        if features.enabled("schwab"):
            expected |= {"/api/portfolio-scan", "/api/portfolio-scans",
                         "/api/portfolio-scans/{scan_id}", "/api/accounts"}
        if features.enabled("sp500"):
            expected |= {"/api/paper-accounts", "/api/research-scan",
                         "/api/research-scans/today", "/api/spy-scan",
                         "/api/spy-scans", "/api/spy-account",
                         "/api/spy-account/compare"}
        if features.enabled("options"):
            expected |= {"/api/options-scan", "/api/options-positions", "/api/options-summary"}
        assert expected <= paths

    def test_tier_2_drops_spy_and_options(self, app_paths):
        # Safe at any physical tier this file runs at (2+, since the whole
        # file is deleted below tier 2): TIER="2" only needs portfolio_routes.py,
        # which exists at every physical tier >= 2.
        paths = app_paths(TIER="2")
        assert "/api/portfolio-scan" in paths
        assert "/api/accounts" in paths
        assert not [p for p in paths if p.startswith(
            ("/api/spy", "/api/options", "/api/paper-accounts", "/api/research")
        )]

    @pytest.mark.skipif(features.DEFAULT_TIER < 3,
                         reason="spy_routes.py isn't physically present below tier 3 — "
                                "TIER=3 can't be faked via env once it's deleted")
    def test_tier_3_drops_options_only(self, app_paths):
        paths = app_paths(TIER="3")
        assert "/api/spy-scan" in paths
        assert "/api/paper-accounts" in paths
        assert not [p for p in paths if p.startswith("/api/options")]

    def test_queue_introspection_survives_tier_1(self, app_paths):
        """The queue endpoints are tier-agnostic and stay in the shell."""
        paths = app_paths(TIER="1")
        assert {"/api/portfolio/status", "/api/portfolio/advance-queue"} <= paths
        assert not [p for p in paths if p.startswith(("/api/spy", "/api/options", "/api/portfolio-"))]


class TestWaitMarketReleasesTheQueue:
    """A scan in running_wait_market is alive but idle; it must not block the
    queue from dispatching real work behind it."""

    def test_running_portfolio_still_busy(self, tmp_db):
        scan_id = db.create_portfolio_scan("2026-08-03")
        db.update_portfolio_scan(scan_id, status="running")

        with db.connect() as conn:
            busy = scan_queue._is_any_scan_running(conn)

        assert busy is not None
        assert busy["scan_type"] == "portfolio"

    def test_waiter_does_not_block_dequeue(self, tmp_db, monkeypatch, thread_spy):
        holder = SimpleNamespace(_worker=lambda scan_id, trade_date: None)
        monkeypatch.setattr(scan_queue, "_RUNNERS", {})
        scan_queue.register_runner("spy", holder, "_worker")

        waiter_id = db.create_spy_scan("2026-08-03", status="running_wait_market")
        queued_id = db.create_spy_scan("2026-08-03", status="queued")

        scan_queue._dequeue_next_scan()

        assert len(thread_spy) == 1
        assert thread_spy[0]["args"][0] == queued_id
        assert db.get_spy_scan(queued_id)["status"] == "running_quick"
        assert db.get_spy_scan(waiter_id)["status"] == "running_wait_market"

    def test_advance_queue_starts_next_while_a_scan_waits(self, tmp_db, monkeypatch, thread_spy):
        holder = SimpleNamespace(_worker=lambda scan_id, trade_date: None)
        monkeypatch.setattr(scan_queue, "_RUNNERS", {})
        scan_queue.register_runner("spy", holder, "_worker")

        waiter_id = db.create_spy_scan("2026-08-03", status="running_wait_market")
        queued_id = db.create_spy_scan("2026-08-03", status="queued")

        running = scan_queue._advance_queue_if_idle()

        assert running is not None
        assert running["id"] == queued_id
        assert len(thread_spy) == 1
        assert db.get_spy_scan(queued_id)["status"] == "running_quick"
        assert db.get_spy_scan(waiter_id)["status"] == "running_wait_market"

    def test_status_endpoint_reports_waiters(self, tmp_db):
        waiter_id = db.create_spy_scan("2026-08-03", status="running_wait_market")
        queued_id = db.create_spy_scan("2026-08-03", status="queued")

        result = portfolio_main.scan_status()

        assert result["running"] is None
        assert len(result["waiting"]) == 1
        assert result["waiting"][0]["id"] == waiter_id
        assert result["waiting"][0]["status"] == "running_wait_market"
        assert len(result["queued"]) == 1
        assert result["queued"][0]["id"] == queued_id


    def test_status_endpoint_reports_research_waiters(self, tmp_db):
        waiter_id = db.create_spy_scan("2026-08-03", status="running_wait_research")

        result = portfolio_main.scan_status()

        assert result["running"] is None
        assert len(result["waiting"]) == 1
        assert result["waiting"][0]["id"] == waiter_id
        assert result["waiting"][0]["status"] == "running_wait_research"


class TestWaitMarketKillSwitchRestoresBusy:
    """OPTIONS_RELEASE_SLOT_DURING_WAIT=0 must roll back the whole
    concurrency guard, not just the proactive dequeue — a parked build
    has to read busy again so nothing else can start beside it."""

    def test_running_wait_market_counts_as_busy_when_switch_off(self, tmp_db, monkeypatch):
        monkeypatch.setenv("OPTIONS_RELEASE_SLOT_DURING_WAIT", "0")
        waiter_id = db.create_spy_scan("2026-08-03", status="running_wait_market")

        with db.connect() as conn:
            busy = scan_queue._is_any_scan_running(conn)

        assert busy is not None
        assert busy["id"] == waiter_id

    def test_advance_queue_does_not_start_next_while_a_scan_waits_and_switch_off(
        self, tmp_db, monkeypatch, thread_spy
    ):
        monkeypatch.setenv("OPTIONS_RELEASE_SLOT_DURING_WAIT", "0")
        holder = SimpleNamespace(_worker=lambda scan_id, trade_date: None)
        monkeypatch.setattr(scan_queue, "_RUNNERS", {})
        scan_queue.register_runner("spy", holder, "_worker")

        db.create_spy_scan("2026-08-03", status="running_wait_market")
        queued_id = db.create_spy_scan("2026-08-03", status="queued")

        running = scan_queue._advance_queue_if_idle()

        assert running is None
        assert thread_spy == []
        assert db.get_spy_scan(queued_id)["status"] == "queued"

    def test_falsy_variants_all_restore_busy(self, tmp_db, monkeypatch):
        """"0", "false", "no", "off" (any case) all disable release,
        mirroring scan_queue's own OPTIONS_RELEASE_SLOT_DURING_WAIT switch."""
        for value in ("0", "false", "No", "OFF"):
            monkeypatch.setenv("OPTIONS_RELEASE_SLOT_DURING_WAIT", value)
            scan_id = db.create_spy_scan("2026-08-03", status="running_wait_market")
            with db.connect() as conn:
                busy = scan_queue._is_any_scan_running(conn)
            assert busy is not None, f"value={value!r} must count the waiter as busy"
            db.update_spy_scan(scan_id, status="completed")

    def test_default_and_explicit_1_still_release(self, tmp_db, monkeypatch):
        for value in (None, "1"):
            if value is None:
                monkeypatch.delenv("OPTIONS_RELEASE_SLOT_DURING_WAIT", raising=False)
            else:
                monkeypatch.setenv("OPTIONS_RELEASE_SLOT_DURING_WAIT", value)
            scan_id = db.create_spy_scan("2026-08-03", status="running_wait_market")
            with db.connect() as conn:
                busy = scan_queue._is_any_scan_running(conn)
            assert busy is None, f"value={value!r} must NOT count the waiter as busy"
            db.update_spy_scan(scan_id, status="completed")


class TestStatusRunningProgressFields:
    """``/api/portfolio/status`` single-row polls carry live progress counters."""

    def test_running_portfolio_row_carries_progress(self, tmp_db):
        sid = db.create_portfolio_scan("2026-08-03", status="running")
        db.update_portfolio_scan(sid, scanned_count=7, scan_total=42, current_ticker="AAPL")

        running = portfolio_main.scan_status()["running"]

        assert running["scan_type"] == "portfolio"
        assert running["status"] == "running"
        assert running["scanned_count"] == 7
        assert running["scan_total"] == 42
        assert running["current_ticker"] == "AAPL"
        assert running["quick_count"] is None
        assert running["quick_total"] is None
        assert running["deep_count"] is None
        assert running["deep_total"] is None

    def test_running_spy_row_carries_progress(self, tmp_db):
        sid = db.create_spy_scan("2026-08-03", status="running_quick")
        db.update_spy_scan(sid, quick_count=120, quick_total=500, deep_count=3, deep_total=50)

        running = portfolio_main.scan_status()["running"]

        assert running["scan_type"] == "spy"
        assert running["status"] == "running_quick"
        assert running["quick_count"] == 120
        assert running["quick_total"] == 500
        assert running["deep_count"] == 3
        assert running["deep_total"] == 50
        assert running["scanned_count"] is None
        assert running["scan_total"] is None
        assert running["current_ticker"] is None

    def test_waiting_spy_row_carries_progress(self, tmp_db):
        sid = db.create_spy_scan("2026-08-03", status="running_wait_market")
        db.update_spy_scan(sid, quick_count=500, quick_total=500, deep_count=50, deep_total=50)

        waiting = portfolio_main.scan_status()["waiting"][0]

        assert waiting["status"] == "running_wait_market"
        assert waiting["quick_count"] == 500
        assert waiting["quick_total"] == 500
        assert waiting["deep_count"] == 50
        assert waiting["deep_total"] == 50

    def test_original_running_keys_unchanged(self, tmp_db):
        sid = db.create_portfolio_scan("2026-08-03", status="running")
        db.update_portfolio_scan(sid, scanned_count=7, scan_total=42, current_ticker="AAPL")

        running = portfolio_main.scan_status()["running"]

        assert running["scan_type"] == "portfolio"
        assert running["id"] == sid
        assert running["trade_date"] == "2026-08-03"
        assert running["kind"] == "equity"
        assert running["created_at"] is not None

    def test_no_running_scan_still_none(self, tmp_db):
        db.create_portfolio_scan("2026-08-03", status="queued")
        assert portfolio_main.scan_status()["running"] is None
