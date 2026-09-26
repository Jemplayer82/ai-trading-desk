"""Mocked end-to-end test of the shared daily research (design doc §13).

One kind='research' row (151 quick rows, 51 deep dives) feeds two equity and
three options allocation rows through the real HTTP routes, the real worker
wrappers and the real DB. Only the edges are faked: the clock, the S&P
universe, the pre-screen, the quick/deep LLM passes, live quotes, the option
chain, both allocators' LLMs and the alert channels. Workers run inline via a
patched ``scan_queue.spawn_worker``.

Tier 4 only (it drives options_routes); scripts/make_tier.py strips it below.
No network, no real sleeps, no LLM.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta
from typing import Any

import pytest

from web import (
    alerts,
    db,
    features,
    market_calendar,
    options_allocator,
    options_data,
    options_engine,
    portfolio_main,
    research_engine,
    scan_queue,
    spy_allocator,
    spy_scanner,
)

pytestmark = pytest.mark.unit

TD = "2026-09-29"  # a Tuesday, an NYSE trading day
_HEADERS = {"x-internal-token": "test-secret-token"}  # pragma: allowlist secret


def _et(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 29, hour, minute, tzinfo=market_calendar._ET)


# ── Fakes ────────────────────────────────────────────────────────────────────

def _fake_quick_scan(scan_id: int, tickers: list[str], trade_date: str, config: dict[str, Any]):
    db.update_spy_scan(scan_id, status="running_quick", quick_total=len(tickers))
    rows: list[dict[str, Any]] = []
    for t in tickers:
        if t == "SPY":
            signal, conviction = "HOLD", 5
        elif t.startswith("T") and int(t[1:]) < 50:
            signal, conviction = "BUY", 9
        else:
            signal, conviction = "HOLD", 3
        reasoning = f"quick {t}"
        db.upsert_spy_quick_result(scan_id, t, signal=signal, conviction=conviction, reasoning=reasoning)
        rows.append({"ticker": t, "signal": signal, "conviction": conviction,
                     "reasoning": reasoning, "error": None})
    db.update_spy_scan(scan_id, quick_count=len(rows))
    return rows


def _fake_deep_dives(scan_id: int, candidates: list[dict[str, Any]], trade_date: str,
                     config: dict[str, Any], selected_analysts: list[str]):
    db.update_spy_scan(scan_id, status="running_deep", deep_total=len(candidates))
    out: list[dict[str, Any]] = []
    for i, c in enumerate(candidates, start=1):
        t = c["ticker"]
        aid = db.create_analysis({"ticker": t, "trade_date": trade_date})
        db.complete_analysis(aid, {"final_trade_decision": "Rating: Buy"}, "Buy")
        db.upsert_spy_quick_result(scan_id, t, signal="Buy", conviction=9,
                                   reasoning=f"deep {t}", analysis_id=aid)
        db.update_spy_scan(scan_id, deep_count=i)
        out.append({**c, "signal": "Buy", "analysis_id": aid, "final_decision": "Rating: Buy",
                    "error": None, "reused_from": None})
    return out


def _fake_fetch_candidates(signals: list[dict[str, Any]], progress_cb: Any = None):
    contracts: list[dict[str, Any]] = []
    for row in signals:
        sig = (row.get("signal") or "").upper()
        if sig not in ("BUY", "OVERWEIGHT"):
            continue
        t = row["ticker"]
        contracts.append({
            "occ_symbol": f"{t}261016C00100000",
            "underlying": t,
            "ticker": t,
            "put_call": "C",
            "strike": 100.0,
            "expiration_date": "2026-10-16",
            "mid": 2.0,
            "bid": 1.9,
            "ask": 2.1,
            "underlying_price": row.get("entry_price"),
            "delta": 0.45,
            "open_interest": 1000,
            "signal": "CALL",
            "conviction": row.get("conviction"),
            "source": "test",
        })
    return contracts, []


def _fake_options_run(candidates, open_positions, trade_date, config, **kwargs):
    return {
        "closes": [],
        "holds": [],
        "opens": [{"contract": candidates[0], "contracts": 1, "cost": 200.0, "rationale": "t"}],
        "report_md": "r",
    }


class _RaisingLLM:
    def invoke(self, *args: Any, **kwargs: Any):
        raise RuntimeError("no LLM in tests")


def _no_sleep(_seconds: float) -> None:
    raise AssertionError("must not sleep: research completes inline and the open has passed")


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def clock(monkeypatch):
    holder = {"now": _et(9, 40)}
    monkeypatch.setattr(market_calendar, "now_et", lambda: holder["now"])
    return holder


@pytest.fixture()
def alert_log(monkeypatch):
    calls: list[tuple[str, Any, Any]] = []
    monkeypatch.setattr(alerts, "notify_run_failed",
                        lambda *a, **k: calls.append(("notify_run_failed", a, k)))
    monkeypatch.setattr(alerts, "notify", lambda *a, **k: calls.append(("notify", a, k)))
    return calls


@pytest.fixture()
def e2e(tmp_path, monkeypatch, clock, alert_log):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-secret-token")  # gitleaks:allow pragma: allowlist secret
    monkeypatch.delenv("TIER", raising=False)
    monkeypatch.delenv("FEATURES", raising=False)

    monkeypatch.setattr(research_engine.time_mod, "sleep", _no_sleep)
    monkeypatch.setattr(scan_queue, "spawn_worker", lambda target, *a: target(*a))
    monkeypatch.setattr(research_engine, "get_sp500_tickers", lambda: [f"T{i:03d}" for i in range(200)])
    monkeypatch.setattr(research_engine, "prescreen", lambda u, n, trade_date=None: list(u[:n]))
    monkeypatch.setattr(spy_scanner, "run_quick_scan", _fake_quick_scan)
    monkeypatch.setattr(spy_scanner, "run_deep_dives", _fake_deep_dives)
    monkeypatch.setattr(spy_scanner, "fetch_live_prices", lambda tickers, **k: {t: 100.0 for t in tickers})
    monkeypatch.setattr(spy_scanner, "refresh_portfolio_prices", lambda sid: {})
    monkeypatch.setattr(spy_allocator, "_llm", lambda config: _RaisingLLM())
    monkeypatch.setattr(options_engine, "refresh_positions", lambda *a, **k: {})
    monkeypatch.setattr(options_data, "fetch_candidates", _fake_fetch_candidates)
    monkeypatch.setattr(options_allocator, "run", _fake_options_run)

    importlib.reload(features)
    importlib.reload(portfolio_main)
    from fastapi.testclient import TestClient

    with TestClient(portfolio_main.app) as c:
        yield {"client": c, "clock": clock, "alerts": alert_log}
    importlib.reload(features)
    importlib.reload(portfolio_main)


def _create_account(client, name: str, kind: str) -> int:
    resp = client.post("/api/paper-accounts", json={"name": name, "kind": kind}, headers=_HEADERS)
    assert resp.status_code == 200, resp.text
    return int(resp.json()["account"]["id"])


# ── Tests ────────────────────────────────────────────────────────────────────

def test_happy_path_one_research_feeds_five_allocations(e2e):
    client = e2e["client"]
    equity_ids = [_create_account(client, f"Equity {i}", "equity") for i in range(2)]
    options_ids = [_create_account(client, f"Options {i}", "options") for i in range(3)]

    resp = client.post("/api/research-scan", json={}, headers=_HEADERS)
    assert resp.status_code == 200, resp.text
    rid = int(resp.json()["scan_id"])

    research = db.get_spy_scan(rid)
    assert research["status"] == "completed", research.get("error")
    assert research["kind"] == "research"
    assert research["paper_account_id"] is None
    assert len(research["quick_results"]) == 151
    assert sum(1 for r in research["quick_results"] if r.get("analysis_id")) == 51
    assert research["portfolio_json"] == []

    for aid in equity_ids:
        resp = client.post("/api/spy-scan", json={"account_id": aid}, headers=_HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.json()["new"] is True
    resp = client.post("/api/options-scan", json={}, headers=_HEADERS)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["scans"]) == 3

    rows = db.list_spy_scans(kind="equity") + db.list_spy_scans(kind="options")
    assert len(rows) == 5
    for summary in rows:
        row = db.get_spy_scan(int(summary["id"]))
        assert row["status"] == "completed", row.get("error")
        assert row["research_scan_id"] == rid
        assert len(row["quick_results"]) == 151
        if row["kind"] == "equity":
            portfolio = row["portfolio_json"]
            assert portfolio, "equity allocation must produce a portfolio"
            new_rows = [p for p in portfolio if p.get("action") == "NEW"]
            assert new_rows
            assert all(p["entry_price"] == 100.0 for p in new_rows)

    assert {int(r["paper_account_id"]) for r in rows if r["kind"] == "equity"} == set(equity_ids)
    assert {int(r["paper_account_id"]) for r in rows if r["kind"] == "options"} == set(options_ids)
    for aid in options_ids:
        assert len(db.list_options_positions(aid, status="open")) >= 1

    assert db.count_research_attempts(TD) == 1
    resp = client.post("/api/spy-scan", json={"account_id": equity_ids[0]}, headers=_HEADERS)
    assert resp.status_code == 200, resp.text
    assert resp.json()["new"] is False
    assert e2e["alerts"] == []


def test_waiting_rows_show_in_portfolio_status(e2e, monkeypatch):
    client = e2e["client"]
    e2e["clock"]["now"] = _et(9, 10)
    spawned: list[tuple[Any, tuple[Any, ...]]] = []
    monkeypatch.setattr(scan_queue, "spawn_worker", lambda target, *a: spawned.append((target, a)))

    aid = _create_account(client, "Waiter", "equity")
    resp = client.post("/api/spy-scan", json={"account_id": aid}, headers=_HEADERS)
    assert resp.status_code == 200, resp.text
    alloc_id = int(resp.json()["scan_id"])
    research_id = int(resp.json()["research"]["scan_id"])
    assert len(spawned) == 2  # research + allocation workers

    status = client.get("/api/portfolio/status", headers=_HEADERS).json()
    waiting = {int(w["id"]): w for w in status["waiting"]}
    assert alloc_id in waiting
    assert waiting[alloc_id]["status"] == "running_wait_research"
    assert status["running"] is not None
    assert int(status["running"]["id"]) == research_id
    assert status["running"]["kind"] == "research"
    assert status["running"]["status"] == "pending"


def test_missing_research_skips_the_day(e2e, monkeypatch):
    client = e2e["client"]
    clock = e2e["clock"]
    clock["now"] = _et(11, 0)
    monkeypatch.setenv("RESEARCH_WAIT_MAX_MIN", "1")

    def _boom():
        raise RuntimeError("universe unavailable")

    def _advance(seconds: float) -> None:
        clock["now"] = clock["now"] + timedelta(seconds=seconds)

    monkeypatch.setattr(research_engine, "get_sp500_tickers", _boom)
    monkeypatch.setattr(research_engine.time_mod, "sleep", _advance)

    aid = _create_account(client, "Skipper", "equity")
    resp = client.post("/api/spy-scan", json={"account_id": aid}, headers=_HEADERS)
    assert resp.status_code == 200, resp.text
    alloc_id = int(resp.json()["scan_id"])

    research = db.latest_research_scan(TD)
    assert research is not None
    assert research["status"] == "failed"

    alloc = db.get_spy_scan(alloc_id)
    assert alloc["status"] == "failed"
    assert "research" in (alloc.get("error") or "").lower()
    assert not alloc.get("portfolio_json")
    assert db.get_latest_completed_spy_scan(paper_account_id=aid) is None

    failed_ids = {k["run_id"] for name, _a, k in e2e["alerts"] if name == "notify_run_failed"}
    assert failed_ids == {research["id"], alloc_id}
