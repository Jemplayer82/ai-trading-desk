from datetime import date
from typing import Any

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import web.research_routes  # noqa: F401  (registers the 'research' runner)
from web import db, market_calendar, scan_queue, spy_routes, spy_scanner

pytestmark = pytest.mark.unit

_TODAY = date(2026, 9, 29)  # a Tuesday (NYSE trading day)


@pytest.fixture(autouse=True)
def _no_yfinance_network(monkeypatch):
    """Disable real yfinance downloads; tests inject prices via Schwab."""
    monkeypatch.setattr(spy_scanner.yf, "download", lambda *a, **k: pd.DataFrame())


@pytest.fixture(autouse=True)
def spawn_calls(monkeypatch):
    """Record worker spawns instead of starting threads; pin the ET date."""
    calls: list[tuple[Any, tuple[Any, ...]]] = []

    def recorder(target, *args):
        calls.append((target, args))

    monkeypatch.setattr(scan_queue, "spawn_worker", recorder)
    monkeypatch.setattr(market_calendar, "today_et", lambda: _TODAY)
    return calls


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    app = FastAPI()
    app.include_router(spy_routes.router)
    with TestClient(app) as c:
        yield c


def _create_account(client, **kwargs):
    defaults = {"name": "Test", "kind": "equity"}
    defaults.update(kwargs)
    resp = client.post("/api/paper-accounts", json=defaults)
    assert resp.status_code == 200
    return resp.json()["account"]


def test_reject_bad_schedule_time(client):
    for bad in ("9:15", "25:00", "abc"):
        resp = client.post(
            "/api/paper-accounts",
            json={"name": f"bad-{bad}", "kind": "equity", "schedule_time": bad},
        )
        assert resp.status_code == 400, bad
        assert "schedule_time" in resp.json()["detail"]


def test_reject_bad_stop_type(client):
    resp = client.post(
        "/api/paper-accounts",
        json={"name": "bad-stop", "kind": "equity", "stop_type": "parabolic"},
    )
    assert resp.status_code == 400
    assert "stop_type" in resp.json()["detail"]


def test_reject_stop_without_value(client):
    resp = client.post(
        "/api/paper-accounts",
        json={"name": "no-value", "kind": "equity", "stop_type": "stop"},
    )
    assert resp.status_code == 400
    assert "stop_value" in resp.json()["detail"]


def test_reject_trailing_pct_zero(client):
    resp = client.post(
        "/api/paper-accounts",
        json={
            "name": "trail-zero",
            "kind": "equity",
            "stop_type": "trailing_pct",
            "stop_value": 0,
        },
    )
    assert resp.status_code == 400


def test_reject_trailing_dollar_negative(client):
    resp = client.post(
        "/api/paper-accounts",
        json={
            "name": "trail-neg",
            "kind": "equity",
            "stop_type": "trailing_dollar",
            "stop_value": -5,
        },
    )
    assert resp.status_code == 400


def test_reject_stop_limit_without_offset(client):
    resp = client.post(
        "/api/paper-accounts",
        json={
            "name": "sl-no-off",
            "kind": "equity",
            "stop_type": "stop_limit",
            "stop_value": 60,
        },
    )
    assert resp.status_code == 400
    assert "stop_limit_offset" in resp.json()["detail"]


def test_reject_stop_value_abc(client):
    resp = client.post(
        "/api/paper-accounts",
        json={
            "name": "val-abc",
            "kind": "equity",
            "stop_type": "stop",
            "stop_value": "abc",
        },
    )
    assert resp.status_code == 400


def test_default_schedule_time_equity(client):
    acct = _create_account(client, name="eq-default")
    assert acct["schedule_time"] == "09:00"


def test_default_schedule_time_options(client):
    acct = _create_account(client, name="opt-default", kind="options")
    assert acct["schedule_time"] == "09:00"


def test_schedule_time_roundtrip(client):
    acct = _create_account(client, name="eq-945", schedule_time="09:45")
    assert acct["schedule_time"] == "09:45"


def test_schedule_time_null_manual(client):
    acct = _create_account(client, name="eq-null", schedule_time=None)
    assert acct["schedule_time"] is None


def test_schedule_time_empty_manual(client):
    acct = _create_account(client, name="eq-empty", schedule_time="")
    assert acct["schedule_time"] is None


def test_no_stop_keys_defaults_to_none(client):
    acct = _create_account(client, name="no-stop")
    assert acct["stop_type"] == "none"
    assert acct["stop_value"] is None
    assert acct["stop_limit_offset"] is None


def test_stop_roundtrip(client):
    acct = _create_account(client, name="stop-60", stop_type="stop", stop_value=60)
    assert acct["stop_type"] == "stop"
    assert acct["stop_value"] == 60.0
    assert acct["stop_limit_offset"] is None


def test_stop_limit_roundtrip(client):
    acct = _create_account(
        client,
        name="sl-60-5",
        stop_type="stop_limit",
        stop_value=60,
        stop_limit_offset=5,
    )
    assert acct["stop_type"] == "stop_limit"
    assert acct["stop_value"] == 60.0
    assert acct["stop_limit_offset"] == 5.0


def test_stop_none_normalizes_value(client):
    acct = _create_account(client, name="none-60", stop_type="none", stop_value=60)
    assert acct["stop_type"] == "none"
    assert acct["stop_value"] is None
    assert acct["stop_limit_offset"] is None


def test_put_name_leaves_other_fields_unchanged(client):
    acct = _create_account(
        client,
        name="sentinel",
        kind="equity",
        schedule_time="09:15",
        bias="bullish",
        aggressiveness=8,
        stop_type="stop",
        stop_value=60,
    )
    resp = client.put(f"/api/paper-accounts/{acct['id']}", json={"name": "Renamed"})
    assert resp.status_code == 200
    updated = resp.json()["account"]
    assert updated["name"] == "Renamed"
    assert updated["schedule_time"] == "09:15"
    assert updated["bias"] == "bullish"
    assert updated["aggressiveness"] == 8
    assert updated["stop_type"] == "stop"
    assert updated["stop_value"] == 60.0
    assert updated["stop_limit_offset"] is None


def test_put_schedule_time_null_clears(client):
    acct = _create_account(client, name="clear-null", schedule_time="09:15")
    resp = client.put(
        f"/api/paper-accounts/{acct['id']}", json={"schedule_time": None}
    )
    assert resp.status_code == 200
    assert resp.json()["account"]["schedule_time"] is None


def test_put_schedule_time_empty_clears(client):
    acct = _create_account(client, name="clear-empty", schedule_time="09:15")
    resp = client.put(
        f"/api/paper-accounts/{acct['id']}", json={"schedule_time": ""}
    )
    assert resp.status_code == 200
    assert resp.json()["account"]["schedule_time"] is None


def test_put_schedule_time_invalid_unchanged(client):
    acct = _create_account(client, name="bad-put", schedule_time="09:15")
    resp = client.put(
        f"/api/paper-accounts/{acct['id']}", json={"schedule_time": "99:99"}
    )
    assert resp.status_code == 400
    row = db.get_paper_account(acct["id"])
    assert row["schedule_time"] == "09:15"


def test_put_stop_type_none_nulls_values(client):
    acct = _create_account(
        client, name="stop-to-none", stop_type="stop", stop_value=60
    )
    resp = client.put(
        f"/api/paper-accounts/{acct['id']}", json={"stop_type": "none"}
    )
    assert resp.status_code == 200
    updated = resp.json()["account"]
    assert updated["stop_type"] == "none"
    assert updated["stop_value"] is None
    assert updated["stop_limit_offset"] is None


def test_put_stop_value_partial_update(client):
    acct = _create_account(
        client, name="stop-partial", stop_type="stop", stop_value=60
    )
    resp = client.put(
        f"/api/paper-accounts/{acct['id']}", json={"stop_value": 45}
    )
    assert resp.status_code == 200
    updated = resp.json()["account"]
    assert updated["stop_type"] == "stop"
    assert updated["stop_value"] == 45.0


def test_put_stop_limit_without_offset_rejected(client):
    acct = _create_account(
        client, name="no-offset", stop_type="stop", stop_value=60
    )
    resp = client.put(
        f"/api/paper-accounts/{acct['id']}", json={"stop_type": "stop_limit"}
    )
    assert resp.status_code == 400
    row = db.get_paper_account(acct["id"])
    assert row["stop_type"] == "stop"
    assert row["stop_value"] == 60.0


_SAMPLE_PORTFOLIO_ROW = {
    "ticker": "TICK",
    "action": "BUY",
    "entry_price": 100.0,
    "shares": 10,
    "cost_basis": 1000.0,
    "dollar_amount": 1000.0,
    "signal": "BUY",
    "current_price": 95.0,
}


def _completed_scan(account_id, portfolio):
    """Create a completed spy scan for the account with the given portfolio."""
    scan_id = db.create_spy_scan("2026-08-10", paper_account_id=account_id)
    db.update_spy_scan_prices(
        scan_id, current_value=0.0, rebalance_notes="", portfolio_json=portfolio
    )
    with db.connect() as conn:
        conn.execute("UPDATE spy_scans SET status = 'completed' WHERE id = ?", (scan_id,))
    return scan_id


@pytest.fixture
def captured_alerts(monkeypatch):
    """Intercept the outage page instead of delivering it over webhook/email."""
    calls = []

    def _record(summary, detail="", *, link=None):
        calls.append((summary, detail, link))

    monkeypatch.setattr(spy_scanner.alerts, "notify", _record)
    return calls


def test_latest_refresh_prices_fans_out_across_accounts(client, captured_alerts, monkeypatch):
    acct1 = _create_account(client, name="fan-out-1")
    acct2 = _create_account(client, name="fan-out-2")

    _completed_scan(acct1["id"], [_SAMPLE_PORTFOLIO_ROW])
    _completed_scan(acct2["id"], [_SAMPLE_PORTFOLIO_ROW])

    monkeypatch.setattr(spy_scanner.schwab_mcp, "market_data_enabled", lambda: True)
    monkeypatch.setattr(
        spy_scanner.schwab_mcp,
        "get_quotes",
        lambda ts: {t: {"last": 95.0} for t in ts},
    )
    monkeypatch.setattr(spy_scanner.schwab_mcp, "quote_price", lambda q: q.get("last"))

    resp = client.post("/api/spy-scans/latest/refresh-prices")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["scans"]) == 2
    for entry in body["scans"].values():
        assert "current_value" in entry
        assert "error" not in entry
    assert captured_alerts == []


def test_latest_refresh_prices_404_when_no_scans(client):
    resp = client.post("/api/spy-scans/latest/refresh-prices")
    assert resp.status_code == 404


def test_latest_refresh_prices_500s_and_alerts_when_every_account_errors(
    client, captured_alerts, monkeypatch
):
    acct1 = _create_account(client, name="error-1")
    acct2 = _create_account(client, name="error-2")

    _completed_scan(acct1["id"], [_SAMPLE_PORTFOLIO_ROW])
    _completed_scan(acct2["id"], [_SAMPLE_PORTFOLIO_ROW])

    def _always_fail(_scan_id):
        return {"error": "quote provider exploded"}

    monkeypatch.setattr(spy_scanner, "refresh_portfolio_prices", _always_fail)

    resp = client.post("/api/spy-scans/latest/refresh-prices")
    assert resp.status_code == 500
    scans = resp.json()["detail"]["scans"]
    assert len(scans) == 2
    for entry in scans.values():
        assert "error" in entry
    assert len(captured_alerts) == 1


def test_latest_refresh_prices_500_when_one_errors_and_one_is_empty(
    client, captured_alerts, monkeypatch
):
    acct_a = _create_account(client, name="partial-error")
    acct_b = _create_account(client, name="partial-empty")

    scan_a = _completed_scan(acct_a["id"], [_SAMPLE_PORTFOLIO_ROW])
    _completed_scan(acct_b["id"], [])

    original = spy_scanner.refresh_portfolio_prices

    def _selective_fail(scan_id):
        if scan_id == scan_a:
            return {"error": "quote provider exploded"}
        return original(scan_id)

    monkeypatch.setattr(spy_scanner, "refresh_portfolio_prices", _selective_fail)

    resp = client.post("/api/spy-scans/latest/refresh-prices")
    assert resp.status_code == 500
    scans = resp.json()["detail"]["scans"]
    assert len(scans) == 2
    assert "error" in scans[str(acct_a["id"])]
    assert scans[str(acct_b["id"])]["error"] == "allocation produced no positions"
    assert "skipped" not in scans[str(acct_b["id"])]
    assert len(captured_alerts) == 1


def test_latest_refresh_prices_500s_and_alerts_when_yfinance_download_fails_in_real_refresh(
    client, captured_alerts, monkeypatch
):
    """Exercise the real refresh_portfolio_prices body, not a monkeypatched stand-in.

    Complement to test_latest_refresh_prices_500s_and_alerts_when_every_account_errors,
    which only covers the fan-out except path. Here we force the in-function
    yfinance-download except branch (return {"error": str(exc)}) by disabling
    Schwab quotes and making yf.download raise.
    """
    acct1 = _create_account(client, name="yf-fail-1")
    acct2 = _create_account(client, name="yf-fail-2")

    _completed_scan(acct1["id"], [_SAMPLE_PORTFOLIO_ROW])
    _completed_scan(acct2["id"], [_SAMPLE_PORTFOLIO_ROW])

    # Disable Schwab so the real refresh_portfolio_prices falls through to yfinance.
    monkeypatch.setattr(
        spy_scanner.schwab_mcp, "market_data_enabled", lambda: False
    )

    yf_error_message = "yfinance download failed in unit test"

    def _boom(*_args, **_kwargs):
        raise Exception(yf_error_message)

    # Override the autouse _no_yfinance_network fixture with a real exception.
    monkeypatch.setattr(spy_scanner.yf, "download", _boom)

    resp = client.post("/api/spy-scans/latest/refresh-prices")
    assert resp.status_code == 500
    scans = resp.json()["detail"]["scans"]
    assert len(scans) == 2
    for entry in scans.values():
        assert entry == {"error": yf_error_message}
    assert len(captured_alerts) == 1

# ---------- POST /api/spy-scan (daily equity allocation) ----------

def _completed_research(td: str = "2026-09-29") -> int:
    rid = db.create_spy_scan(td, kind="research")
    db.complete_spy_scan(rid, "r", [])
    return rid


def test_spy_scan_requires_account_id(client, spawn_calls):
    resp = client.post("/api/spy-scan", json={})
    assert resp.status_code == 400
    assert "account_id" in resp.json()["detail"]
    assert spawn_calls == []


def test_spy_scan_non_int_account_id_400(client):
    resp = client.post("/api/spy-scan", json={"account_id": "abc"})
    assert resp.status_code == 400


def test_spy_scan_unknown_account_404(client):
    resp = client.post("/api/spy-scan", json={"account_id": 9999})
    assert resp.status_code == 404


def test_spy_scan_rejects_options_account(client, spawn_calls):
    acct = _create_account(client, name="opt-acct", kind="options")
    resp = client.post("/api/spy-scan", json={"account_id": acct["id"]})
    assert resp.status_code == 400
    assert spawn_calls == []


def test_spy_scan_non_trading_day_409_unless_forced(client, monkeypatch, spawn_calls):
    monkeypatch.setattr(market_calendar, "today_et", lambda: date(2026, 9, 26))  # Saturday
    acct = _create_account(client, name="sat-acct")

    resp = client.post("/api/spy-scan", json={"account_id": acct["id"]})
    assert resp.status_code == 409
    assert spawn_calls == []

    resp = client.post("/api/spy-scan", json={"account_id": acct["id"], "force": True})
    assert resp.status_code == 200
    assert resp.json()["new"] is True


def test_spy_scan_with_completed_research_spawns_allocation_only(client, spawn_calls):
    rid = _completed_research()
    acct = _create_account(client, name="eq-alloc")

    resp = client.post("/api/spy-scan", json={"account_id": acct["id"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running_wait_research"
    assert body["new"] is True

    row = db.get_spy_scan(body["scan_id"])
    assert row["kind"] == "equity"
    assert row["paper_account_id"] == acct["id"]
    assert row["research_scan_id"] == rid

    assert len(spawn_calls) == 1
    target, args = spawn_calls[0]
    assert target.__name__ == "_run_spy_scan_thread"
    assert args == (body["scan_id"], "2026-09-29")

    again = client.post("/api/spy-scan", json={"account_id": acct["id"]})
    assert again.status_code == 200
    assert again.json()["new"] is False
    assert again.json()["scan_id"] == body["scan_id"]
    assert len(spawn_calls) == 1


def test_spy_scan_without_research_kicks_research(client, spawn_calls):
    acct = _create_account(client, name="eq-kick")

    resp = client.post("/api/spy-scan", json={"account_id": acct["id"]})
    assert resp.status_code == 200
    assert resp.json()["new"] is True

    research = db.latest_research_scan("2026-09-29")
    assert research is not None
    assert len(spawn_calls) == 2
    names = sorted(t.__name__ for t, _ in spawn_calls)
    assert "_run_spy_scan_thread" in names


def test_staged_options_create_partial_update_and_switch(client):
    acct = _create_account(client, name='staged', kind='options', stop_type='trailing_staged', stop_value=20)
    assert (acct['stage_trigger_pct'], acct['stage_trail_pct']) == (20, 10)
    resp = client.put(f"/api/paper-accounts/{acct['id']}", json={'stage_trail_pct': 5})
    assert resp.status_code == 200
    assert (resp.json()['account']['stage_trigger_pct'], resp.json()['account']['stage_trail_pct']) == (20, 5)
    resp = client.put(f"/api/paper-accounts/{acct['id']}", json={'stop_value': 4})
    assert resp.status_code == 200
    assert db.get_paper_account(acct['id'])['stop_value'] == 4
    resp = client.put(f"/api/paper-accounts/{acct['id']}", json={'stop_type': 'trailing_pct'})
    assert resp.status_code == 200
    assert resp.json()['account']['stage_trigger_pct'] is None
    assert resp.json()['account']['stage_trail_pct'] is None
    resp = client.put(f"/api/paper-accounts/{acct['id']}", json={'stop_type': 'trailing_staged'})
    assert resp.status_code == 200
    assert resp.json()['account']['stage_trail_pct'] == 10


def test_equity_rejects_staged_create_and_update(client):
    resp = client.post('/api/paper-accounts', json=dict(name='bad-stage', kind='equity', stop_type='trailing_staged', stop_value=20))
    assert resp.status_code == 400
    assert 'options accounts only' in resp.json()['detail']
    acct = _create_account(client, name='equity')
    resp = client.put(f"/api/paper-accounts/{acct['id']}", json={'stop_type': 'trailing_staged', 'stop_value': 20, 'kind': 'options'})
    assert resp.status_code == 400
    assert db.get_paper_account(acct['id'])['stop_type'] == 'none'


@pytest.mark.parametrize('extra', [dict(stage_trigger_pct=0), dict(stage_trail_pct=0), dict(stage_trail_pct=100)])
def test_staged_route_bounds(client, extra):
    resp = client.post('/api/paper-accounts', json=dict(name='bad-stage', kind='options', stop_type='trailing_staged', stop_value=20, **extra))
    assert resp.status_code == 400


@pytest.mark.parametrize('trail', [1, 40, 99])
def test_staged_route_allows_looser_and_bounds(client, trail):
    acct = _create_account(client, name='staged-wide', kind='options', stop_type='trailing_staged', stop_value=20, stage_trail_pct=trail)
    assert acct['stage_trail_pct'] == trail
    resp = client.put(f"/api/paper-accounts/{acct['id']}", json={'stage_trail_pct': 40, 'stage_trigger_pct': 25, 'stop_value': 15})
    assert resp.status_code == 200
    assert (resp.json()['account']['stage_trail_pct'], resp.json()['account']['stop_value']) == (40, 15)
