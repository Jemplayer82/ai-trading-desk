"""Lifecycle tests for the options paper trader: DB migrations, the cash
ledger's transactional invariants, expiry bid liquidation idempotency, and the
kind-scoped scan queries."""

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from web import db, options_data, options_engine
from web.account_policy import StopPolicy

pytestmark = pytest.mark.unit


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


@pytest.fixture()
def account_id(tmp_db):
    aid = db.create_paper_account("Options Test", starting_capital=100_000.0, kind="options",
                                  stop_type="stop", stop_value=60)
    db.append_options_cash(aid, "deposit", 100_000.0, note="initial deposit")
    return aid


def _pos_dict(**over):
    base = {
        "occ_symbol": "AAPL  260821C00230000",
        "underlying": "AAPL", "put_call": "CALL", "strike": 230.0,
        "expiration_date": "2026-08-21", "contracts": 2, "entry_premium": 4.20,
        "entry_underlying": 232.0, "entry_delta": 0.45,
        "entry_bid": 4.10, "entry_ask": 4.30, "entry_oi": 1500,
        "signal": "BUY", "conviction": 8, "rationale": "test", "data_source": "schwab",
    }
    if "entry_premium" in over:
        # Tests pass the desired stop reference; entry execution crosses the spread.
        base.update(entry_bid=over["entry_premium"], entry_ask=over["entry_premium"] + 0.2)
    base.update(over)
    return base


# ── Migrations ───────────────────────────────────────────────────────────────

def test_init_db_idempotent(tmp_db):
    db.init_db()
    db.init_db()  # migrations must be re-runnable on every boot


def test_kind_migration_on_pre_kind_db(tmp_path, monkeypatch):
    """A database created before the kind columns gains them (default 'equity')."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE spy_scans (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "created_at TEXT NOT NULL, trade_date TEXT NOT NULL, status TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE paper_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL UNIQUE, starting_capital REAL NOT NULL DEFAULT 100000, "
        "aggressiveness INTEGER NOT NULL DEFAULT 5, bias TEXT NOT NULL DEFAULT 'neutral', "
        "created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO spy_scans (created_at, trade_date, status) VALUES ('x', '2026-07-01', 'completed')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    with db.connect() as c:
        row = c.execute("SELECT kind FROM spy_scans WHERE id = 1").fetchone()
        assert row["kind"] == "equity"
        pa_cols = {r["name"] for r in c.execute("PRAGMA table_info(paper_accounts)")}
        assert "kind" in pa_cols
        spy_cols = {r["name"] for r in c.execute("PRAGMA table_info(spy_scans)")}
        assert "research_scan_id" in spy_cols


# ── Kind-scoped scan queries ─────────────────────────────────────────────────

def test_scan_kind_scoping(tmp_db):
    eq = db.create_spy_scan("2026-07-17", kind="equity")
    opt = db.create_spy_scan("2026-07-17", kind="options")
    assert [s["id"] for s in db.list_spy_scans()] == [eq]
    assert [s["id"] for s in db.list_spy_scans(kind="options")] == [opt]
    assert db.latest_spy_scan()["id"] == eq
    assert db.latest_spy_scan(kind="options")["id"] == opt

    db.complete_spy_scan(eq, "r", [], starting_value=100_000)
    db.complete_spy_scan(opt, "r", [], starting_value=100_000)
    assert db.get_latest_completed_spy_scan()["id"] == eq
    assert db.get_latest_completed_spy_scan(kind="options")["id"] == opt

    # Clearing equity history must not touch options runs (and vice versa).
    assert db.delete_all_spy_scans(kind="equity") == 1
    assert [s["id"] for s in db.list_spy_scans(kind="options")] == [opt]


def test_paper_account_kind_filter(tmp_db):
    e = db.create_paper_account("Equity A", kind="equity")
    o = db.create_paper_account("Options A", kind="options")
    assert [a["id"] for a in db.list_paper_accounts(kind="equity")] == [e]
    assert [a["id"] for a in db.list_paper_accounts(kind="options")] == [o]
    assert {a["id"] for a in db.list_paper_accounts()} == {e, o}


def test_status_endpoint_exposes_kind(tmp_db):
    """The sidebar queue on every tab filters on scan_type + kind, so
    /api/portfolio/status must label options runs (spy_scans, kind='options')
    distinctly from equity S&P runs — otherwise options leak into the S&P queue."""
    from web import portfolio_main

    pf = db.create_portfolio_scan("2026-07-20", status="queued")
    eq = db.create_spy_scan("2026-07-20", kind="equity", status="queued")
    opt = db.create_spy_scan("2026-07-20", kind="options", status="queued")

    status = portfolio_main.scan_status()
    by_key = {(q["scan_type"], q["kind"]): q["id"] for q in status["queued"]}
    assert by_key == {
        ("portfolio", "equity"): pf,
        ("spy", "equity"): eq,
        ("spy", "options"): opt,
    }


# ── Learning-loop schema (exit_underlying + options_lessons) ─────────────────

def test_migration_adds_exit_underlying_columns(tmp_path, monkeypatch):
    """A database created before the learning loop gains the exit columns."""
    db_path = tmp_path / "web.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE options_positions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "paper_account_id INTEGER NOT NULL, open_scan_id INTEGER NOT NULL, "
        "occ_symbol TEXT NOT NULL, underlying TEXT NOT NULL, put_call TEXT NOT NULL, "
        "strike REAL NOT NULL, expiration_date TEXT NOT NULL, contracts INTEGER NOT NULL, "
        "entry_premium REAL NOT NULL, cost_basis REAL NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open', opened_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    db.init_db()

    conn = sqlite3.connect(str(db_path))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(options_positions)")}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "exit_underlying" in cols
    assert "exit_underlying_source" in cols
    assert "options_lessons" in tables


def test_close_with_exit_underlying_roundtrip(account_id):
    scan = db.create_spy_scan("2026-07-29", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict())
    assert db.close_options_position(pid, 5.0, "llm_close", close_scan_id=scan,
                                     exit_underlying=234.5,
                                     exit_underlying_source="live")
    row = db.get_options_position(pid)
    assert row["exit_underlying"] == pytest.approx(234.5)
    assert row["exit_underlying_source"] == "live"
    # Plain close (no spot captured) leaves them NULL for the nightly backfill.
    pid2 = db.open_options_position(account_id, scan, _pos_dict(
        occ_symbol="AAPL  260821C00240000", strike=240.0))
    assert db.close_options_position(pid2, 5.0, "stop_loss")
    row2 = db.get_options_position(pid2)
    assert row2["exit_underlying"] is None
    assert row2["exit_underlying_source"] is None


# ── Ledger + position lifecycle ──────────────────────────────────────────────

def test_open_close_ledger_flow(account_id):
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict())
    # 2 contracts x $4.30 ASK x 100 = $860 debited.
    assert db.options_cash_balance(account_id) == pytest.approx(100_000 - 860)
    pos = db.get_options_position(pid)
    assert pos["status"] == "open"
    assert pos["cost_basis"] == pytest.approx(860)

    assert db.close_options_position(pid, 5.00, "llm_close", close_scan_id=scan, exit_bid=5.00)
    assert db.options_cash_balance(account_id) == pytest.approx(100_000 - 860 + 1000)
    pos = db.get_options_position(pid)
    assert pos["status"] == "closed"
    assert pos["realized_pnl"] == pytest.approx(140)
    assert db.options_realized_pnl(account_id) == pytest.approx(140)

    # Closing again is a no-op (no double credit).
    assert not db.close_options_position(pid, 6.00, "llm_close")
    assert db.options_cash_balance(account_id) == pytest.approx(100_140)


def test_settlement_itm_and_idempotency(account_id):
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict())
    # Intrinsic is ignored; last bid 4.10 * 100 * 2 = $820.
    assert db.settle_options_position(pid, 2.50, settlement_close=232.50)
    pos = db.get_options_position(pid)
    assert pos["status"] == "closed"
    assert pos["exit_reason"] == "expiry"
    assert pos["exit_value"] == pytest.approx(820)
    assert pos["realized_pnl"] == pytest.approx(820 - 860)
    assert pos["settlement_close"] is None
    assert pos["exit_bid"] == pytest.approx(4.1)
    cash_after = db.options_cash_balance(account_id)
    assert cash_after == pytest.approx(100_000 - 860 + 820)

    # Settling twice must not double-credit.
    assert not db.settle_options_position(pid, 2.50, settlement_close=232.50)
    assert db.options_cash_balance(account_id) == pytest.approx(cash_after)
    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM options_cash_ledger WHERE position_id = ? AND kind = 'close'",
            (pid,),
        ).fetchone()["n"]
    assert n == 1


def test_settlement_worthless(account_id):
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict())
    db.mark_options_position(pid, 0.0, 0.0, "schwab")
    assert db.settle_options_position(pid, 0.004, settlement_close=229.99)
    pos = db.get_options_position(pid)
    assert pos["status"] == "closed"
    assert pos["exit_reason"] == "expiry"
    assert pos["exit_value"] == 0
    assert pos["realized_pnl"] == pytest.approx(-860)
    assert db.options_cash_balance(account_id) == pytest.approx(100_000 - 860)


def test_equity_invariant_across_builds(account_id):
    """cash + open value stays consistent across two scans of activity."""
    scan1 = db.create_spy_scan("2026-07-16", paper_account_id=account_id, kind="options")
    p1 = db.open_options_position(account_id, scan1, _pos_dict())
    p2 = db.open_options_position(
        account_id, scan1,
        _pos_dict(occ_symbol="MSFT  260821P00420000", underlying="MSFT",
                  put_call="PUT", strike=420.0, entry_premium=6.00, contracts=1),
    )
    scan2 = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    db.close_options_position(p1, 5.50, "llm_close", close_scan_id=scan2, exit_bid=5.50)

    eq = options_engine.account_equity(account_id)
    open_positions = db.list_options_positions(account_id, status="open")
    assert [p["id"] for p in open_positions] == [p2]
    assert eq["cash"] == pytest.approx(100_000 - 860 - 620 + 1100)
    assert eq["open_value"] == pytest.approx(600)  # liquidation at bid, not ask cost
    assert eq["equity"] == pytest.approx(eq["cash"] + eq["open_value"])
    assert db.options_realized_pnl(account_id) == pytest.approx(1100 - 860)

    summary = options_engine.account_summary(account_id)
    assert summary["open_count"] == 1
    assert summary["closed_count"] == 1
    assert summary["realized_pnl"] == pytest.approx(240)


def test_mark_options_position(account_id):
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict())
    db.mark_options_position(pid, 5.10, 1020.0, "schwab")
    pos = db.get_options_position(pid)
    assert pos["current_bid"] == pytest.approx(5.10)
    assert pos["current_premium"] == pytest.approx(5.10)
    assert pos["current_value"] == pytest.approx(1020.0)
    assert pos["price_source"] == "schwab"
    assert pos["stale_count"] == 0
    # Marking does not touch cash.
    assert db.options_cash_balance(account_id) == pytest.approx(100_000 - 860)


# ── Engine settlement rules ──────────────────────────────────────────────────

def test_is_settleable_rules():
    exp = "2026-07-17"
    before_close = datetime(2026, 7, 17, 14, 0)
    after_close = datetime(2026, 7, 17, 17, 5)
    next_day = datetime(2026, 7, 18, 8, 0)
    prior_day = datetime(2026, 7, 16, 23, 0)
    assert not options_engine.is_settleable(exp, before_close)
    assert options_engine.is_settleable(exp, after_close)
    assert options_engine.is_settleable(exp, next_day)
    assert not options_engine.is_settleable(exp, prior_day)
    assert not options_engine.is_settleable("garbage", next_day)


def test_intrinsic_value():
    assert options_engine.intrinsic_value("CALL", 230.0, 232.5) == pytest.approx(2.5)
    assert options_engine.intrinsic_value("CALL", 230.0, 225.0) == 0.0
    assert options_engine.intrinsic_value("PUT", 230.0, 225.0) == pytest.approx(5.0)
    assert options_engine.intrinsic_value("PUT", 230.0, 232.5) == 0.0


def test_settle_expired_sweep(account_id, monkeypatch):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    due = db.open_options_position(
        account_id, scan, _pos_dict(expiration_date=yesterday))
    due2 = db.open_options_position(
        account_id, scan, _pos_dict(occ_symbol="AAPL  260821C00240000", strike=240, expiration_date=yesterday))
    live = db.open_options_position(
        account_id, scan,
        _pos_dict(occ_symbol="MSFT  270115C00420000", underlying="MSFT",
                  expiration_date=(date.today() + timedelta(days=180)).isoformat()))
    monkeypatch.setattr(options_engine, "underlying_close_on_or_before",
                        lambda u, e: 232.5)
    summary = options_engine.settle_expired(account_id)
    assert summary["due"] == 2
    assert summary["settled_itm"] == 2
    due_row = db.get_options_position(due)
    assert due_row["status"] == "closed"
    assert due_row["exit_reason"] == "expiry"
    assert due_row["exit_premium"] == pytest.approx(4.1)
    assert db.get_options_position(due2)["status"] == "closed"
    assert db.get_options_position(live)["status"] == "open"

    # Second sweep finds nothing (idempotent end to end).
    summary2 = options_engine.settle_expired(account_id)
    assert summary2["due"] == 0


def test_settle_expired_missing_underlying_close_uses_last_bid(account_id, monkeypatch):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict(expiration_date=yesterday))
    monkeypatch.setattr(options_engine, "underlying_close_on_or_before", lambda u, e: None)
    summary = options_engine.settle_expired(account_id)
    assert summary["failed"] == 0
    pos = db.get_options_position(pid)
    assert pos["status"] == "closed"
    assert pos["exit_premium"] == pytest.approx(4.1)
    assert pos["exit_reason"] == "expiry"
    assert pos["stale_count"] == 1


def test_dequeue_dispatches_options_rows_to_options_thread(tmp_db, monkeypatch):
    """A queued kind='options' spy_scans row must start the options build, not
    the equity pipeline (the queue predates the kind column)."""
    import threading

    from web import options_routes, portfolio_routes, scan_queue, spy_routes

    started: dict[str, int] = {}
    done = threading.Event()

    def _rec(name):
        def _target(scan_id, trade_date):
            started[name] = scan_id
            done.set()
        return _target

    # These patches are also what proves scan_queue's runner registry resolves
    # its target with a live getattr at dispatch time — a reference captured at
    # register_runner() time would run the real worker instead.
    monkeypatch.setattr(options_routes, "_run_options_scan_thread", _rec("options"))
    monkeypatch.setattr(spy_routes, "_run_spy_scan_thread", _rec("equity"))
    monkeypatch.setattr(portfolio_routes, "_run_scan_thread", _rec("portfolio"))

    acct = db.create_paper_account("Q Opt", kind="options")
    opt = db.create_spy_scan("2026-07-17", paper_account_id=acct,
                             status="queued", kind="options")
    eq = db.create_spy_scan("2026-07-17", status="queued", kind="equity")
    with db.connect() as conn:  # make the options row strictly older
        conn.execute("UPDATE spy_scans SET created_at = '2026-07-17T00:00:00Z' WHERE id = ?", (opt,))
        conn.execute("UPDATE spy_scans SET created_at = '2026-07-17T00:00:01Z' WHERE id = ?", (eq,))

    scan_queue._dequeue_next_scan()
    assert done.wait(5)
    assert started == {"options": opt}
    with db.connect() as conn:
        st = conn.execute("SELECT status FROM spy_scans WHERE id = ?", (opt,)).fetchone()["status"]
    assert st == "running_quick"

    # Simulate that run finishing; next dequeue starts the equity row.
    db.update_spy_scan(opt, status="completed")
    done.clear()
    scan_queue._dequeue_next_scan()
    assert done.wait(5)
    assert started["equity"] == eq


# ── POST /api/options-scan: allocation rows over the shared research ────────

@pytest.fixture()
def options_client(tmp_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import web.research_routes  # noqa: F401 — registers the 'research' runner
    from web import market_calendar, options_routes, scan_queue

    spawns: list[tuple] = []
    monkeypatch.setattr(scan_queue, "spawn_worker", lambda target, *args: spawns.append((target, args)))
    monkeypatch.setattr(market_calendar, "today_et", lambda: date(2026, 9, 29))
    app = FastAPI()
    app.include_router(options_routes.router)
    with TestClient(app) as tc:
        yield tc, spawns


def test_options_scan_creates_waiting_allocation_rows(options_client):
    client, spawns = options_client
    a1 = db.create_paper_account("Opt A", kind="options")
    db.create_paper_account("Opt B", kind="options")
    db.create_paper_account("Opt C", kind="options")

    resp = client.post("/api/options-scan", json={})
    assert resp.status_code == 200
    scans = resp.json()["scans"]
    assert len(scans) == 3
    assert all(s["status"] == "running_wait_research" and s["new"] for s in scans)
    for s in scans:
        row = db.get_spy_scan(s["scan_id"])
        assert row["kind"] == "options" and row["trade_date"] == "2026-09-29"

    names = [getattr(target, "__name__", "") for target, _ in spawns]
    assert len(spawns) == 4
    assert names.count("_run_options_scan_thread") == 3
    assert db.count_research_attempts("2026-09-29") == 1

    again = client.post("/api/options-scan", json={"account_id": a1})
    assert again.status_code == 200
    body = again.json()
    assert body["new"] is False
    assert body["scans"][0]["scan_id"] == body["scan_id"]
    assert len(spawns) == 4


def test_options_scan_rejects_equity_account(options_client):
    client, _ = options_client
    eq = db.create_paper_account("Equity", kind="equity")
    resp = client.post("/api/options-scan", json={"account_id": eq})
    assert resp.status_code == 400


def test_options_scan_non_trading_day_needs_force(options_client, monkeypatch):
    from web import market_calendar

    client, _ = options_client
    db.create_paper_account("Opt Sat", kind="options")
    monkeypatch.setattr(market_calendar, "today_et", lambda: date(2026, 9, 26))  # Saturday

    assert client.post("/api/options-scan", json={}).status_code == 409
    forced = client.post("/api/options-scan", json={"force": True})
    assert forced.status_code == 200
    assert forced.json()["scans"][0]["status"] == "running_wait_research"


def test_pending_counts_as_busy(tmp_db):
    from web import scan_queue

    db.create_spy_scan("2026-07-17", kind="options")  # status 'pending'
    with db.connect() as conn:
        busy = scan_queue._is_any_scan_running(conn)
    assert busy is not None and busy["scan_type"] == "spy"


def test_advance_queue_starts_next_when_idle(tmp_db, monkeypatch):
    """The stuck-run reaper's recovery hook. A silently dead worker never runs
    its finally-dequeue, so a scan queued behind it sits stranded forever with
    nothing running. Advancing must start the oldest queued scan. Regression for
    the production wedge (equity scan #10 stuck 'queued' behind a crashed run)."""
    import threading

    from web import scan_queue, spy_routes

    started: dict[str, int] = {}
    done = threading.Event()

    def _rec(name):
        def _t(scan_id, trade_date):
            started[name] = scan_id
            done.set()
        return _t

    monkeypatch.setattr(spy_routes, "_run_spy_scan_thread", _rec("equity"))
    q = db.create_spy_scan("2026-07-17", status="queued", kind="equity")

    kicked = scan_queue._advance_queue_if_idle()
    assert done.wait(5)
    assert started == {"equity": q}
    assert kicked is not None and kicked["id"] == q


def test_advance_queue_noop_when_busy(tmp_db, monkeypatch):
    """Advancing must not start a second scan while one is already running —
    otherwise the reaper's recovery kick could double-start a live scan."""
    from web import scan_queue

    calls: list[int] = []
    monkeypatch.setattr(scan_queue, "_dequeue_next_scan", lambda: calls.append(1))
    db.create_spy_scan("2026-07-17", status="running_quick", kind="options")  # busy

    assert scan_queue._advance_queue_if_idle() is None
    assert calls == [], "must not dequeue while a scan is running"


# ── Intraday stop-loss emulation (options_engine._apply_intraday_stops) ─────

def _open_marked(account_id, entry=10.0, prev_mark=None, **over):
    """Open a position and optionally set a pre-refresh mark."""
    scan = db.create_spy_scan("2026-07-29", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict(entry_premium=entry, **over))
    if prev_mark is not None:
        db.mark_options_position(pid, prev_mark, prev_mark * 100 * 2, "schwab")
    return pid


def _policies(account_id, policy):
    return {account_id: policy}


def _freeze_et(monkeypatch, when: datetime) -> None:
    """Pin the ET clock the options path reads (by module attribute) so tests
    that open hardcoded-expiry contracts don't rot once that date passes."""
    monkeypatch.setattr(options_data, "today_et", lambda: when.date())
    monkeypatch.setattr(options_data, "now_et", lambda: when)


def test_intraday_stop_fills_at_observed_bid_when_crossed(account_id, monkeypatch):
    """Crossing a stop fills at the fresh bid observed at the decision."""
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda syms: {s: 230.0 for s in syms})
    pid = _open_marked(account_id, entry=10.0, prev_mark=5.0)  # stop = 4.00
    pos = db.get_options_position(pid)
    stopped = options_engine._apply_intraday_stops(
        [pos], {pid: (3.0, "schwab")}, _policies(account_id, StopPolicy("stop", 60.0))
    )
    assert stopped == 1
    row = db.get_options_position(pid)
    assert row["status"] == "closed"
    assert row["exit_reason"] == "stop_loss"
    assert row["exit_premium"] == pytest.approx(3.0)
    assert row["exit_underlying"] == pytest.approx(230.0)


def test_intraday_stop_gap_fills_at_observed_quote(account_id, monkeypatch):
    """A gap below the fixed stop also fills at the observed bid."""
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda syms: {})
    pid = _open_marked(account_id, entry=10.0, prev_mark=3.9)  # already < 4.00 stop
    pos = db.get_options_position(pid)
    stopped = options_engine._apply_intraday_stops(
        [pos], {pid: (2.5, "yfinance")}, _policies(account_id, StopPolicy("stop", 60.0))
    )
    assert stopped == 1
    row = db.get_options_position(pid)
    assert row["exit_premium"] == pytest.approx(2.5)
    assert row["exit_underlying"] is None  # spot lookup failed -> nightly backfill


def test_intraday_stop_ignores_unpriced_and_healthy(account_id, monkeypatch):
    """No fresh quote (carried bid) must never realize a loss, and
    healthy marks above the stop are untouched."""
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda syms: {})
    pid_stale = _open_marked(account_id, entry=10.0, prev_mark=3.0)   # below stop but stale
    pid_ok = _open_marked(account_id, entry=10.0, prev_mark=9.0)      # healthy
    rows = [db.get_options_position(pid_stale), db.get_options_position(pid_ok)]
    stopped = options_engine._apply_intraday_stops(
        rows, {pid_ok: (8.5, "schwab")}, _policies(account_id, StopPolicy("stop", 60.0))
    )
    assert stopped == 0
    assert db.get_options_position(pid_stale)["status"] == "open"
    assert db.get_options_position(pid_ok)["status"] == "open"


def test_intraday_stop_kill_switch(account_id, monkeypatch):
    from tradingagents.default_config import DEFAULT_CONFIG
    monkeypatch.setitem(DEFAULT_CONFIG, "options_intraday_stop", False)
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda syms: {})
    pid = _open_marked(account_id, entry=10.0, prev_mark=5.0)
    pos = db.get_options_position(pid)
    assert options_engine._apply_intraday_stops(
        [pos], {pid: (3.0, "schwab")}, _policies(account_id, StopPolicy("stop", 60.0))
    ) == 0
    assert db.get_options_position(pid)["status"] == "open"


def test_intraday_stop_credits_ledger_at_fill(account_id, monkeypatch):
    """Cash reflects ask cost and the observed bid proceeds."""
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda syms: {})
    before = db.options_cash_balance(account_id)
    pid = _open_marked(account_id, entry=10.0, prev_mark=5.0)  # debit 10.2 ask*100*2 = 2040
    pos = db.get_options_position(pid)
    options_engine._apply_intraday_stops(
        [pos], {pid: (3.0, "schwab")}, _policies(account_id, StopPolicy("stop", 60.0))
    )
    after = db.options_cash_balance(account_id)
    # net: -2040 ask entry + 3.00*100*2 = 600 bid close
    assert after - before == pytest.approx(-2040 + 600)


# ── Legacy backtracking helper; bid/ask fills must not use it ─────────────

def _fake_bars(monkeypatch, closes_start, closes_end, minutes=40):
    """Install a fake yf.Ticker serving 1-min bars ending now, linear path."""
    import pandas as pd

    end = pd.Timestamp.now(tz="America/New_York").floor("min")
    idx = pd.date_range(end=end, periods=minutes, freq="1min")
    step = (closes_end - closes_start) / (minutes - 1)
    closes = [closes_start + i * step for i in range(minutes)]
    bars = pd.DataFrame({
        "Open": closes, "Close": closes,
        "Low": [c - 0.05 for c in closes], "High": [c + 0.05 for c in closes],
    }, index=idx)

    class FakeTicker:
        def __init__(self, sym):
            pass
        def history(self, period=None, interval=None):
            return bars

    monkeypatch.setattr(options_engine.yf, "Ticker", FakeTicker)
    return idx


def _iso_utc(ts):
    return ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def test_backtrack_finds_call_crossing_minute(monkeypatch):
    """CALL: underlying slid 100 -> 90 over the interval; premium 5 -> 3 with
    stop 4 implies the cross at underlying ~95, i.e. mid-interval — the booked
    minute must be that bar, not refresh time."""
    idx = _fake_bars(monkeypatch, 100.0, 90.0, minutes=41)
    pos = {"underlying": "AAPL", "put_call": "CALL", "occ_symbol": "X",
           "last_marked_at": _iso_utc(idx[0]), "opened_at": _iso_utc(idx[0])}
    out = options_engine._backtrack_stop_crossing(pos, prev_mark=5.0, stop_level=4.0, new_price=3.0)
    assert out is not None
    closed_at, u_star = out
    assert u_star == pytest.approx(95.0, abs=0.6)
    # The crossing bar sits strictly inside the interval (~minute 20 of 41).
    import pandas as pd
    t = pd.Timestamp(closed_at.replace("Z", "+00:00"))
    assert idx[5].tz_convert("UTC") < t < idx[-5].tz_convert("UTC")


def test_backtrack_put_uses_adverse_high(monkeypatch):
    """PUT loses as the underlying RISES — crossing is the first bar whose
    High reached the implied level on the way up."""
    idx = _fake_bars(monkeypatch, 90.0, 100.0, minutes=41)
    pos = {"underlying": "AAPL", "put_call": "PUT", "occ_symbol": "X",
           "last_marked_at": _iso_utc(idx[0]), "opened_at": _iso_utc(idx[0])}
    out = options_engine._backtrack_stop_crossing(pos, prev_mark=5.0, stop_level=4.0, new_price=3.0)
    assert out is not None
    _closed_at, u_star = out
    assert u_star == pytest.approx(95.0, abs=0.6)


def test_backtrack_degenerate_returns_none(monkeypatch):
    idx = _fake_bars(monkeypatch, 100.0, 100.0, minutes=10)  # flat underlying
    pos = {"underlying": "AAPL", "put_call": "CALL", "occ_symbol": "X",
           "last_marked_at": _iso_utc(idx[0]), "opened_at": _iso_utc(idx[0])}
    assert options_engine._backtrack_stop_crossing(pos, 5.0, 4.0, 3.0) is None


def test_stop_does_not_book_backtracked_time_and_spot(account_id, monkeypatch):
    """The decision uses its observed bid and never fabricated crossing evidence."""
    monkeypatch.setattr(options_engine, "_backtrack_stop_crossing",
                        lambda *a, **k: ("2026-07-29T14:32:00Z", 95.0))
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda syms: {})
    pid = _open_marked(account_id, entry=10.0, prev_mark=5.0)
    pos = db.get_options_position(pid)
    assert options_engine._apply_intraday_stops(
        [pos], {pid: (3.0, "schwab")}, _policies(account_id, StopPolicy("stop", 60.0))
    ) == 1
    row = db.get_options_position(pid)
    assert row["closed_at"] != "2026-07-29T14:32:00Z"
    assert row["exit_underlying"] is None
    assert row["exit_underlying_source"] is None
    assert row["exit_premium"] == pytest.approx(3.0)


# ── Trailing stop: winners ride, but gains lock once armed ───────────────────

def test_peak_premium_seeded_and_ratchets(account_id):
    scan = db.create_spy_scan("2026-07-30", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict(entry_premium=10.0))
    row = db.get_options_position(pid)
    assert row["entry_premium"] == pytest.approx(10.2)  # ask execution
    assert row["current_bid"] == pytest.approx(10.0)
    assert row["peak_premium"] == pytest.approx(10.0)  # seeded at entry bid
    db.mark_options_position(pid, 14.0, 2800, "schwab")
    assert db.get_options_position(pid)["peak_premium"] == pytest.approx(14.0)  # ratchets up
    db.mark_options_position(pid, 11.0, 2200, "schwab")
    assert db.get_options_position(pid)["peak_premium"] == pytest.approx(14.0)  # never down


def test_effective_stop_unarmed_is_base():
    from web import options_allocator as oa
    outcome = oa.effective_stop_level(
        {"entry_premium": 10.2, "entry_bid": 10.0, "current_bid": 3.5, "peak_premium": 14.9, "current_premium": 3.5},
        StopPolicy("stop", 60.0),
    )
    assert outcome.level == pytest.approx(4.0)
    assert outcome.exit_reason == "stop_loss"


def test_effective_stop_armed_locks_gains():
    from web import options_allocator as oa
    outcome = oa.effective_stop_level(
        {"entry_premium": 10.2, "entry_bid": 10.0, "current_bid": 3.5, "peak_premium": 20.0},
        StopPolicy("trailing_pct", 30.0),
    )
    assert outcome.exit_reason == "trail_stop"
    assert outcome.level == pytest.approx(14.0)


def test_effective_stop_kill_switch():
    from web import options_allocator as oa
    outcome = oa.effective_stop_level(
        {"entry_premium": 10.2, "entry_bid": 10.0, "current_bid": 3.5, "peak_premium": 30.0},
        StopPolicy("none"),
    )
    assert outcome.action == "hold"
    assert outcome.level == 0.0


def test_intraday_trail_closes_winner_at_observed_bid(account_id, monkeypatch):
    """Winner peaked +100% then crossed the trail and sells at the observed bid."""
    db.update_paper_account(account_id, stop_type="trailing_pct", stop_value=30)
    monkeypatch.setattr(options_engine, "_backtrack_stop_crossing", lambda *a, **k: None)
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda syms: {})
    pid = _open_marked(account_id, entry=10.0, prev_mark=20.0)  # peak ratchets to 20
    pos = db.get_options_position(pid)
    # fresh quote 13.5 < trail 14.0 but far above base 4.0
    stopped = options_engine._apply_intraday_stops(
        [pos], {pid: (13.5, "schwab")}, _policies(account_id, StopPolicy("trailing_pct", 30.0))
    )
    assert stopped == 1
    row = db.get_options_position(pid)
    assert row["exit_reason"] == "trail_stop"
    assert row["exit_premium"] == pytest.approx(13.5)
    assert row["realized_pnl"] == pytest.approx((13.5 - 10.2) * 100 * 2)  # profit kept


def test_forced_closes_trail():
    from web import options_allocator as oa
    pos = {"id": 1, "occ_symbol": "X", "underlying": "AAPL", "entry_premium": 10.0,
           "peak_premium": 20.0, "current_premium": 13.0, "entry_bid": 10.0, "current_bid": 13.0, "contracts": 2,
           "expiration_date": "2099-01-15", "cost_basis": 2000.0}
    out = oa.forced_closes([pos], StopPolicy("trailing_pct", 30.0))
    assert len(out) == 1 and out[0][1] == "trail_stop"
    healthy = dict(pos, current_premium=15.0, current_bid=15.0)  # above trail 14.0
    assert oa.forced_closes([healthy], StopPolicy("trailing_pct", 30.0)) == []


def test_prompt_shows_days_held_and_ride_guidance(monkeypatch):
    from unittest.mock import MagicMock

    from web import options_allocator as oa
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content="[]")
    monkeypatch.setattr(oa, "llm_for", lambda *a, **k: llm)
    pos = {"id": 1, "occ_symbol": "AAPL  260821C00230000", "underlying": "AAPL",
           "put_call": "CALL", "strike": 230.0, "expiration_date": "2099-01-15",
           "entry_premium": 10.0, "current_premium": 12.0, "peak_premium": 12.0,
           "contracts": 2, "cost_basis": 2000.0, "opened_at": "2026-07-27T14:00:00Z"}
    oa.run([], [pos], "2026-07-30", {}, equity=100_000, cash=50_000,
           policy=StopPolicy("trailing_pct", 30.0))
    system = llm.invoke.call_args[0][0][0]["content"]
    user = llm.invoke.call_args[0][0][1]["content"]
    assert "WINNERS RIDE" in system and "trailing stop" in system.lower()
    assert "30" in system
    assert "held " in user and "d left" in user  # days-held now in every open line


# ── Per-account stop policy + stop-limit resting semantics ───────────────────

def test_intraday_none_policy_never_stops(account_id, monkeypatch):
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda syms: {})
    pid = _open_marked(account_id, entry=10.0, prev_mark=1.0)  # down 90%
    pos = db.get_options_position(pid)
    stopped = options_engine._apply_intraday_stops(
        [pos], {pid: (1.0, "schwab")}, _policies(account_id, StopPolicy("none"))
    )
    assert stopped == 0
    assert db.get_options_position(pid)["status"] == "open"


def test_intraday_trailing_dollar_fills_at_observed_bid(account_id, monkeypatch):
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda syms: {})
    pid = _open_marked(account_id, entry=10.0, prev_mark=20.0)  # peak ratchets to 20
    pos = db.get_options_position(pid)
    stopped = options_engine._apply_intraday_stops(
        [pos], {pid: (17.5, "schwab")}, _policies(account_id, StopPolicy("trailing_dollar", 2.0))
    )
    assert stopped == 1
    row = db.get_options_position(pid)
    assert row["status"] == "closed"
    assert row["exit_reason"] == "trail_stop"
    assert row["exit_premium"] == pytest.approx(17.5)


def test_intraday_stop_limit_gapped_arms_resting(account_id, monkeypatch):
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda syms: {})
    pid = _open_marked(account_id, entry=10.0, prev_mark=5.0)
    pos = db.get_options_position(pid)
    # entry=10 -> level 4.0, limit 3.6; fresh quote 3.0 gaps through the limit.
    stopped = options_engine._apply_intraday_stops(
        [pos], {pid: (3.0, "schwab")}, _policies(account_id, StopPolicy("stop_limit", 60.0, 10.0))
    )
    assert stopped == 0
    row = db.get_options_position(pid)
    assert row["status"] == "open"
    assert row["stop_triggered_at"] is not None


def test_intraday_stop_limit_resting_fills_on_later_refresh(account_id, monkeypatch):
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda syms: {})
    pid = _open_marked(account_id, entry=10.0, prev_mark=5.0)
    # First pass gaps through the limit and arms the stop-limit.
    options_engine._apply_intraday_stops(
        [db.get_options_position(pid)], {pid: (3.0, "schwab")},
        _policies(account_id, StopPolicy("stop_limit", 60.0, 10.0)),
    )
    armed = db.get_options_position(pid)
    assert armed["stop_triggered_at"] is not None
    # A real refresh would have marked current_premium to the observed gap price.
    db.mark_options_position(pid, 3.0, 3.0 * 100 * 2, "schwab")
    pos = db.get_options_position(pid)
    # Second pass sees 3.7, back at or above the 3.60 limit -> fill at the better of limit and market.
    stopped = options_engine._apply_intraday_stops(
        [pos], {pid: (3.7, "schwab")}, _policies(account_id, StopPolicy("stop_limit", 60.0, 10.0))
    )
    assert stopped == 1
    row = db.get_options_position(pid)
    assert row["status"] == "closed"
    assert row["exit_reason"] == "stop_limit"
    assert row["exit_premium"] == pytest.approx(3.7)


def test_intraday_stop_limit_resting_stays_open_below_limit(account_id, monkeypatch):
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda syms: {})
    pid = _open_marked(account_id, entry=10.0, prev_mark=5.0)
    options_engine._apply_intraday_stops(
        [db.get_options_position(pid)], {pid: (3.0, "schwab")},
        _policies(account_id, StopPolicy("stop_limit", 60.0, 10.0)),
    )
    db.mark_options_position(pid, 3.0, 3.0 * 100 * 2, "schwab")
    pos = db.get_options_position(pid)
    before = pos["stop_triggered_at"]
    assert before is not None
    # Quote still below the 3.60 limit, so the resting order must not fill.
    stopped = options_engine._apply_intraday_stops(
        [pos], {pid: (3.5, "schwab")}, _policies(account_id, StopPolicy("stop_limit", 60.0, 10.0))
    )
    assert stopped == 0
    row = db.get_options_position(pid)
    assert row["status"] == "open"
    assert row["stop_triggered_at"] == before


def test_intraday_stop_limit_crosses_fills_immediately(account_id, monkeypatch):
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda syms: {})
    pid = _open_marked(account_id, entry=10.0, prev_mark=5.0)
    pos = db.get_options_position(pid)
    # 3.7 is below the 4.00 trigger but still >= the 3.60 limit, so it fills
    # immediately at the observed bid without ever arming.
    stopped = options_engine._apply_intraday_stops(
        [pos], {pid: (3.7, "schwab")}, _policies(account_id, StopPolicy("stop_limit", 60.0, 10.0))
    )
    assert stopped == 1
    row = db.get_options_position(pid)
    assert row["status"] == "closed"
    assert row["exit_reason"] == "stop_limit"
    assert row["exit_premium"] == pytest.approx(3.7)
    assert row["stop_triggered_at"] is None


def test_intraday_stops_use_per_account_policy_in_one_batch(account_id, monkeypatch):
    _freeze_et(monkeypatch, datetime(2026, 7, 20, 11, 0, tzinfo=options_data._ET))
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda syms: {})

    # Create a second options account with a trailing-pct policy.
    aid2 = db.create_paper_account(
        "Batch account 2", starting_capital=100_000.0, kind="options",
        stop_type="trailing_pct", stop_value=30,
    )
    db.append_options_cash(aid2, "deposit", 100_000.0, note="initial deposit")

    pid1 = _open_marked(account_id, entry=10.0, prev_mark=5.0)
    pid2 = _open_marked(
        aid2, entry=10.0, prev_mark=20.0,
        occ_symbol="MSFT  260821C00420000", underlying="MSFT", strike=420.0,
    )
    price_map = {
        "AAPL  260821C00230000": 3.0,
        "MSFT  260821C00420000": 13.5,
    }

    # Force Schwab pricing so both positions land in priced.
    monkeypatch.setattr(options_engine.schwab_mcp, "market_data_enabled", lambda: True)

    def fake_get_quotes(symbols):
        return {s: {"quote": {"bidPrice": price_map.get(s, 1.0), "askPrice": price_map.get(s, 1.0) + 0.2}} for s in symbols}

    monkeypatch.setattr(options_engine.schwab_mcp, "get_quotes", fake_get_quotes)
    monkeypatch.setattr(options_engine.schwab_mcp, "option_quote_price", lambda q: q.get("price"))

    # Count batched policy lookups and prove the policies dict came from one list.
    calls = {"list_options": 0}
    real_list = db.list_paper_accounts

    def counting_list(*args, **kwargs):
        if kwargs.get("kind") == "options":
            calls["list_options"] += 1
        return real_list(*args, **kwargs)

    monkeypatch.setattr(db, "list_paper_accounts", counting_list)

    real_apply = options_engine._apply_intraday_stops
    captured: dict = {}

    def wrapping_apply(positions, priced, policies):
        captured["policy_lookup_calls"] = calls["list_options"]
        return real_apply(positions, priced, policies)

    monkeypatch.setattr(options_engine, "_apply_intraday_stops", wrapping_apply)

    # The real scheduler calls refresh_positions(paper_account_id=None).
    summary = options_engine.refresh_positions(paper_account_id=None)

    assert captured["policy_lookup_calls"] == 1
    assert summary["stopped"] == 2

    row1 = db.get_options_position(pid1)
    row2 = db.get_options_position(pid2)
    assert row1["status"] == "closed"
    assert row1["exit_reason"] == "stop_loss"
    assert row1["exit_premium"] == pytest.approx(3.0)
    assert row2["status"] == "closed"
    assert row2["exit_reason"] == "trail_stop"
    assert row2["exit_premium"] == pytest.approx(13.5)


def test_staged_intraday_and_daily_share_policy(account_id, monkeypatch):
    from web import account_policy, options_allocator
    db.update_paper_account(account_id, stop_type='trailing_staged', stop_value=20,
                           stage_trigger_pct=20, stage_trail_pct=10)
    policy = account_policy.StopPolicy.from_account(db.get_paper_account(account_id))
    monkeypatch.setattr(options_engine, '_backtrack_stop_crossing', lambda *a, **k: None)
    monkeypatch.setattr(options_engine, '_underlying_prices', lambda syms: {})
    pid = _open_marked(account_id, entry=10, prev_mark=12)
    pos = db.get_options_position(pid)
    # Refresh passes only fresh quotes in priced; stale positions are absent.
    assert options_engine._apply_intraday_stops([pos], {}, {account_id: policy}) == 0
    assert db.get_options_position(pid)['status'] == 'open'
    assert options_engine._apply_intraday_stops([pos], {pid: (10.5, 'schwab')}, {account_id: policy}) == 1
    row = db.get_options_position(pid)
    assert row['exit_reason'] == 'trail_stop'
    assert row['exit_premium'] == pytest.approx(10.5)
    assert row['peak_premium'] == pytest.approx(12)
    daily = dict(pos, current_premium=10.5, current_bid=10.5, expiration_date='2099-01-15')
    closes = options_allocator.forced_closes([daily], policy)
    assert closes == [(daily, 'trail_stop', 10.5)]
    # The unconditional floor retains priority over the staged stop.
    floor = dict(daily, expiration_date='2000-01-01')
    assert options_allocator.forced_closes([floor], policy) == [(floor, 'dte_floor', 10.5)]


def test_staged_saved_ratchet_and_midtrade_account_edits(account_id, monkeypatch):
    from web import account_policy, options_allocator
    db.update_paper_account(account_id, stop_type='trailing_staged', stop_value=20,
                           stage_trigger_pct=20, stage_trail_pct=40)
    scan = db.create_spy_scan('2026-07-20', paper_account_id=account_id, kind='options')
    pid = db.open_options_position(account_id, scan, _pos_dict(entry_premium=10, expiration_date='2099-01-15'))
    assert db.get_options_position(pid)['stop_level_hwm'] is None
    # Save base stop before triggering a looser stage; a carried mark cannot fill.
    db.mark_options_position(pid, 11.99, 2398, 'carried', reset_stale=False)
    assert db.get_options_position(pid)['stop_level_hwm'] == 9.592
    db.mark_options_position(pid, 12, 2400, 'schwab')
    assert db.get_options_position(pid)['stop_level_hwm'] == 9.592
    db.mark_options_position(pid, 13, 2600, 'schwab')
    assert db.get_options_position(pid)['stop_level_hwm'] == 9.592
    stale_snapshot = db.get_options_position(pid)
    # Edit tighter without updating a mark; daily evaluation raises saved level.
    db.update_paper_account(account_id, stage_trail_pct=5)
    tight = account_policy.StopPolicy.from_account(db.get_paper_account(account_id))
    assert options_allocator.forced_closes([stale_snapshot], tight) == []
    assert db.get_options_position(pid)['stop_level_hwm'] == 12.35
    # Edit all parameters looser; stale caller snapshot must use the saved level.
    db.update_paper_account(account_id, stage_trail_pct=99, stage_trigger_pct=200, stop_value=40)
    loose = account_policy.StopPolicy.from_account(db.get_paper_account(account_id))
    monkeypatch.setattr(options_engine, '_backtrack_stop_crossing', lambda *a, **k: None)
    monkeypatch.setattr(options_engine, '_underlying_prices', lambda syms: {})
    assert options_engine._apply_intraday_stops([stale_snapshot], {}, {account_id: loose}) == 0
    assert options_engine._apply_intraday_stops([stale_snapshot], {pid: (12, 'schwab')}, {account_id: loose}) == 1
    row = db.get_options_position(pid)
    assert (row['status'], row['stop_level_hwm'], row['exit_premium'], row['exit_reason']) == ('closed', 12.35, 12.0, 'trail_stop')


def test_staged_policy_switch_preserves_ratchet(account_id):
    from web import account_policy, options_allocator
    db.update_paper_account(account_id, stop_type='trailing_staged', stop_value=20,
                           stage_trigger_pct=20, stage_trail_pct=10)
    scan = db.create_spy_scan('2026-07-20', paper_account_id=account_id, kind='options')
    pid = db.open_options_position(account_id, scan, _pos_dict(entry_premium=10, expiration_date='2099-01-15'))
    db.mark_options_position(pid, 20, 4000, 'schwab')
    assert db.get_options_position(pid)['stop_level_hwm'] == 18
    db.update_paper_account(account_id, stop_type='trailing_pct', stop_value=50)
    db.mark_options_position(pid, 25, 5000, 'schwab')
    assert db.get_options_position(pid)['stop_level_hwm'] == 18
    db.update_paper_account(account_id, stop_type='trailing_staged', stage_trail_pct=99)
    pol = account_policy.StopPolicy.from_account(db.get_paper_account(account_id))
    outcome = options_allocator.effective_stop_level(db.get_options_position(pid), pol)
    assert outcome.level == 18


def test_staged_tighter_edit_can_fill_at_next_daily_evaluation(account_id):
    from web import account_policy, options_allocator
    db.update_paper_account(account_id, stop_type='trailing_staged', stop_value=20,
                           stage_trigger_pct=20, stage_trail_pct=40)
    scan = db.create_spy_scan('2026-07-20', paper_account_id=account_id, kind='options')
    pid = db.open_options_position(account_id, scan, _pos_dict(entry_premium=10, expiration_date='2099-01-15'))
    db.mark_options_position(pid, 12, 2400, 'schwab')
    db.mark_options_position(pid, 10.5, 2100, 'schwab')
    loose = account_policy.StopPolicy.from_account(db.get_paper_account(account_id))
    pos = db.get_options_position(pid)
    assert options_allocator.forced_closes([pos], loose) == []
    db.update_paper_account(account_id, stage_trail_pct=10)
    tight = account_policy.StopPolicy.from_account(db.get_paper_account(account_id))
    assert options_allocator.forced_closes([pos], tight) == [(pos, 'trail_stop', 10.5)]
    assert db.get_options_position(pid)['stop_level_hwm'] == 10.8
