"""POST /api/portfolio-scan starts its worker through scan_queue.spawn_worker.

spawn_worker is the single place scan workers start (see web/scan_queue.py);
start_scan used to use FastAPI BackgroundTasks, so a test patching
spawn_worker would silently not intercept portfolio scans.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from web import db, scan_queue

portfolio_routes = pytest.importorskip("web.portfolio_routes")

pytestmark = pytest.mark.unit


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    monkeypatch.setattr(portfolio_routes.schwab_mcp, "schwab_enabled", lambda: True)
    monkeypatch.setattr(portfolio_routes.schwab_mcp, "get_accounts", lambda **kw: [{"id": 1}])
    spawned: list[tuple] = []
    monkeypatch.setattr(scan_queue, "spawn_worker", lambda target, *a: spawned.append((target, a)))
    return spawned


def test_start_scan_spawns_worker_via_spawn_worker(env):
    result = asyncio.run(portfolio_routes.start_scan(body={"aggressiveness": 7, "bias": "bullish"}))

    today = datetime.utcnow().date().isoformat()
    assert result["status"] == "running"
    assert result["new"] is True
    assert env == [(portfolio_routes._run_scan_thread, (result["scan_id"], today, 7, "bullish"))]


def test_busy_start_scan_queues_without_spawning(env, monkeypatch):
    monkeypatch.setattr(scan_queue, "_is_any_scan_running", lambda conn: {"scan_type": "spy", "id": 1})

    result = asyncio.run(portfolio_routes.start_scan(body=None))

    assert result["status"] == "queued"
    assert result["new"] is True
    assert db.get_portfolio_scan(result["scan_id"])["status"] == "queued"
    assert env == []
