"""Unit tests for the shared daily research routes."""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web import alerts, db, market_calendar, research_engine, research_routes, scan_queue

pytestmark = pytest.mark.unit


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    app = FastAPI()
    app.include_router(research_routes.router)
    with TestClient(app) as tc:
        yield tc


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(market_calendar, "today_et", lambda: date(2026, 9, 29))


@pytest.fixture(autouse=True)
def spawn_calls(monkeypatch):
    calls: list[tuple[Any, tuple[Any, ...]]] = []

    def recorder(target, *args):
        calls.append((target, args))

    monkeypatch.setattr(scan_queue, "spawn_worker", recorder)
    return calls


def test_start_research_scan_creates_pending_and_spawns(client, spawn_calls):
    db.create_paper_account("Conservative", aggressiveness=3, kind="equity")
    db.create_paper_account("Aggressive", aggressiveness=9, kind="equity")

    resp = client.post("/api/research-scan")
    assert resp.status_code == 200

    body = resp.json()
    assert body["status"] == "pending"
    assert body["new"] is True
    assert body["trade_date"] == "2026-09-29"

    sid = body["scan_id"]
    row = db.get_spy_scan(sid)
    assert row["kind"] == "research"
    assert row["paper_account_id"] is None
    assert row["aggressiveness"] == 9

    assert len(spawn_calls) == 1
    target, args = spawn_calls[0]
    assert target is research_routes._run_research_thread
    assert args == (sid, "2026-09-29")


def test_start_research_scan_is_idempotent(client, spawn_calls):
    db.create_paper_account("Default", aggressiveness=5, kind="equity")

    first = client.post("/api/research-scan").json()
    second = client.post("/api/research-scan").json()

    assert second["scan_id"] == first["scan_id"]
    assert second["new"] is False
    assert second["status"] == first["status"]
    assert len(spawn_calls) == 1


def test_start_after_failure_creates_new_row(client, spawn_calls):
    db.create_paper_account("Default", aggressiveness=5, kind="equity")

    first = client.post("/api/research-scan").json()["scan_id"]
    db.fail_spy_scan(first, "x")

    resp = client.post("/api/research-scan")
    body = resp.json()

    assert body["scan_id"] != first
    assert body["new"] is True
    assert body["status"] == "pending"
    assert len(spawn_calls) == 2


def test_start_queues_when_another_scan_is_busy(client, spawn_calls):
    td = "2026-09-29"
    db.create_spy_scan(td, kind="options", status="running_deep")
    db.create_paper_account("Default", aggressiveness=5, kind="equity")

    resp = client.post("/api/research-scan")
    assert resp.status_code == 200

    body = resp.json()
    assert body["status"] == "queued"
    assert body["new"] is True
    assert "queued_behind" in body
    assert len(spawn_calls) == 0


def test_non_trading_day_returns_409_force_overrides(
    client, spawn_calls, monkeypatch
):
    monkeypatch.setattr(market_calendar, "today_et", lambda: date(2026, 9, 26))

    resp = client.post("/api/research-scan")
    assert resp.status_code == 409
    assert "not an NYSE trading day" in resp.json()["detail"]

    resp2 = client.post("/api/research-scan", json={"force": True})
    assert resp2.status_code == 200
    assert resp2.json()["trade_date"] == "2026-09-26"
    assert len(spawn_calls) == 1


def test_research_scans_today(client, spawn_calls):
    td = "2026-09-29"

    before = client.get("/api/research-scans/today").json()
    assert before["trade_date"] == td
    assert before["scan"] is None
    assert before["attempts"] == 0

    sid = client.post("/api/research-scan").json()["scan_id"]

    after = client.get("/api/research-scans/today").json()
    assert after["scan"]["id"] == sid
    assert after["attempts"] == 1


def test_list_research_scans_filters_kind(client, spawn_calls):
    td = "2026-09-29"
    research_id = client.post("/api/research-scan").json()["scan_id"]
    equity_id = db.create_spy_scan(td, kind="equity", status="pending")

    resp = client.get("/api/research-scans?limit=5")
    assert resp.status_code == 200

    scans = resp.json()["scans"]
    ids = {s["id"] for s in scans}
    assert research_id in ids
    assert equity_id not in ids
    assert all(s["kind"] == "research" for s in scans)
    assert len(scans) <= 5


def test_run_research_thread_wraps_and_reports_failure(client, monkeypatch):
    td = "2026-09-29"
    sid = db.create_spy_scan(td, kind="research", status="pending")

    errors: list[dict[str, Any]] = []
    dequeues: list[bool] = []

    def boom(scan_id: int, trade_date: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(research_engine, "run_research", boom)
    monkeypatch.setattr(
        alerts,
        "notify_run_failed",
        lambda *, kind, run_id, label, error, link=None: errors.append(
            {"kind": kind, "run_id": run_id, "label": label, "error": error}
        ),
    )
    monkeypatch.setattr(scan_queue, "_dequeue_next_scan", lambda: dequeues.append(True))

    research_routes._run_research_thread(sid, td)

    row = db.get_spy_scan(sid)
    assert row["status"] == "failed"
    assert row["error"] == "boom"

    assert len(errors) == 1
    assert errors[0]["kind"] == "Research"
    assert errors[0]["run_id"] == sid

    assert len(dequeues) == 1
