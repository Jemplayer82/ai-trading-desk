"""Lifecycle tests for the options paper trader: DB migrations, the cash
ledger's transactional invariants, expiry settlement idempotency, and the
kind-scoped scan queries."""

import sqlite3
import threading
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from web import db, market_cache, options_data, options_engine
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


# ── Deep-dive target selection ───────────────────────────────────────────────

def _q(ticker, signal="BUY", conviction=5):
    return {"ticker": ticker, "signal": signal, "conviction": conviction}


def test_deep_targets_top_by_conviction_capped_at_deep_top():
    """Only the top DEEP_TOP directional names by conviction get a deep dive."""
    rows = [_q(f"T{i}", "BUY", conviction=i) for i in range(options_engine.DEEP_TOP + 40)]
    top = options_engine.select_deep_dive_targets(rows)
    # DEEP_TOP directional picks; SPY wasn't in the quick set so nothing forced.
    assert len(top) == options_engine.DEEP_TOP
    # Highest-conviction names survive; the low-conviction tail is dropped.
    picked = {r["ticker"] for r in top}
    assert f"T{options_engine.DEEP_TOP + 39}" in picked
    assert "T0" not in picked


def test_deep_targets_include_both_buy_and_sell():
    rows = [_q("BULL", "BUY", 9), _q("BEAR", "SELL", 8), _q("MEH", "HOLD", 9)]
    picked = {r["ticker"] for r in options_engine.select_deep_dive_targets(rows)}
    assert picked == {"BULL", "BEAR"}  # HOLD is not directional


def test_spy_always_deep_dived_even_when_hold():
    """SPY is guaranteed a deep dive every run — the '+1 done on SPY' rule —
    even when its quick scan is HOLD and it's outside the directional set."""
    rows = [_q(f"T{i}", "BUY", 9) for i in range(options_engine.DEEP_TOP)]
    rows.append(_q("SPY", "HOLD", 1))
    top = options_engine.select_deep_dive_targets(rows)
    assert any(r["ticker"] == "SPY" for r in top), "SPY must always be deep-dived"
    assert len(top) == options_engine.DEEP_TOP + 1  # the forced +1


def test_spy_not_duplicated_when_already_directional():
    rows = [_q("SPY", "BUY", 10)] + [_q(f"T{i}", "BUY", 5) for i in range(3)]
    top = options_engine.select_deep_dive_targets(rows)
    assert [r["ticker"] for r in top].count("SPY") == 1


# ── Ledger + position lifecycle ──────────────────────────────────────────────

def test_open_close_ledger_flow(account_id):
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict())
    # 2 contracts x $4.20 x 100 = $840 debited.
    assert db.options_cash_balance(account_id) == pytest.approx(100_000 - 840)
    pos = db.get_options_position(pid)
    assert pos["status"] == "open"
    assert pos["cost_basis"] == pytest.approx(840)

    assert db.close_options_position(pid, 5.00, "llm_close", close_scan_id=scan)
    assert db.options_cash_balance(account_id) == pytest.approx(100_000 - 840 + 1000)
    pos = db.get_options_position(pid)
    assert pos["status"] == "closed"
    assert pos["realized_pnl"] == pytest.approx(160)
    assert db.options_realized_pnl(account_id) == pytest.approx(160)

    # Closing again is a no-op (no double credit).
    assert not db.close_options_position(pid, 6.00, "llm_close")
    assert db.options_cash_balance(account_id) == pytest.approx(100_160)


def test_settlement_itm_and_idempotency(account_id):
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict())
    # ITM by $2.50 at expiry: proceeds 2.50 * 100 * 2 = $500.
    assert db.settle_options_position(pid, 2.50, settlement_close=232.50)
    pos = db.get_options_position(pid)
    assert pos["status"] == "expired_itm"
    assert pos["exit_value"] == pytest.approx(500)
    assert pos["realized_pnl"] == pytest.approx(500 - 840)
    assert pos["settlement_close"] == pytest.approx(232.50)
    cash_after = db.options_cash_balance(account_id)
    assert cash_after == pytest.approx(100_000 - 840 + 500)

    # Settling twice must not double-credit.
    assert not db.settle_options_position(pid, 2.50, settlement_close=232.50)
    assert db.options_cash_balance(account_id) == pytest.approx(cash_after)
    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM options_cash_ledger WHERE position_id = ? AND kind = 'expire'",
            (pid,),
        ).fetchone()["n"]
    assert n == 1


def test_settlement_worthless(account_id):
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict())
    assert db.settle_options_position(pid, 0.004, settlement_close=229.99)
    pos = db.get_options_position(pid)
    assert pos["status"] == "expired_worthless"
    assert pos["exit_value"] == 0
    assert pos["realized_pnl"] == pytest.approx(-840)
    assert db.options_cash_balance(account_id) == pytest.approx(100_000 - 840)


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
    db.close_options_position(p1, 5.50, "llm_close", close_scan_id=scan2)

    eq = options_engine.account_equity(account_id)
    open_positions = db.list_options_positions(account_id, status="open")
    assert [p["id"] for p in open_positions] == [p2]
    assert eq["cash"] == pytest.approx(100_000 - 840 - 600 + 1100)
    assert eq["equity"] == pytest.approx(eq["cash"] + eq["open_value"])
    assert db.options_realized_pnl(account_id) == pytest.approx(1100 - 840)

    summary = options_engine.account_summary(account_id)
    assert summary["open_count"] == 1
    assert summary["closed_count"] == 1
    assert summary["realized_pnl"] == pytest.approx(260)


def test_mark_options_position(account_id):
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict())
    db.mark_options_position(pid, 5.10, 1020.0, "schwab")
    pos = db.get_options_position(pid)
    assert pos["current_premium"] == pytest.approx(5.10)
    assert pos["current_value"] == pytest.approx(1020.0)
    assert pos["price_source"] == "schwab"
    assert pos["stale_count"] == 0
    # Marking does not touch cash.
    assert db.options_cash_balance(account_id) == pytest.approx(100_000 - 840)


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
    live = db.open_options_position(
        account_id, scan,
        _pos_dict(occ_symbol="MSFT  270115C00420000", underlying="MSFT",
                  expiration_date=(date.today() + timedelta(days=180)).isoformat()))
    monkeypatch.setattr(options_engine, "underlying_close_on_or_before",
                        lambda u, e: 232.5)
    summary = options_engine.settle_expired(account_id)
    assert summary["due"] == 1
    assert summary["settled_itm"] == 1
    assert db.get_options_position(due)["status"] == "expired_itm"
    assert db.get_options_position(live)["status"] == "open"

    # Second sweep finds nothing (idempotent end to end).
    summary2 = options_engine.settle_expired(account_id)
    assert summary2["due"] == 0


def test_settle_expired_missing_close_leaves_open(account_id, monkeypatch):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict(expiration_date=yesterday))
    monkeypatch.setattr(options_engine, "underlying_close_on_or_before", lambda u, e: None)
    summary = options_engine.settle_expired(account_id)
    assert summary["failed"] == 1
    pos = db.get_options_position(pid)
    assert pos["status"] == "open"       # never guess a settlement price
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


def test_mover_score_direction_agnostic():
    closes_up = [100 + i for i in range(21)]
    closes_down = [100 - i * 0.8 for i in range(21)]
    flat = [100.0] * 21
    vols = [1_000_000] * 21
    up = options_engine._mover_score(closes_up, vols)
    down = options_engine._mover_score(closes_down, vols)
    quiet = options_engine._mover_score(flat, vols)
    assert up > quiet and down > quiet   # losers are put candidates, not noise
    assert options_engine._mover_score([100, 101], vols) is None  # too short


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


def test_intraday_stop_fills_at_stop_level_when_crossed(account_id, monkeypatch):
    """Prev mark above the stop, fresh quote below it -> the level was crossed
    this interval, so the fill is AT the stop (standing-stop emulation), not at
    the (worse) observed quote."""
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
    assert row["exit_premium"] == pytest.approx(4.0)
    assert row["exit_underlying"] == pytest.approx(230.0)


def test_intraday_stop_gap_fills_at_observed_quote(account_id, monkeypatch):
    """Previous mark ALREADY below the stop (e.g. stop was disabled or marks
    were stale while it slid) -> the level wasn't crossed this interval, so
    fill at the observed quote; pretending we caught $4.00 would be fiction.
    Note a never-marked position counts as crossed-from-above: open seeds
    current_premium = entry_premium, which sits above the stop by definition."""
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
    """No fresh quote (carried/intrinsic mark) must never realize a loss, and
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
    """Cash must reflect the STOP-level fill, not the observed quote."""
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda syms: {})
    before = db.options_cash_balance(account_id)
    pid = _open_marked(account_id, entry=10.0, prev_mark=5.0)  # debit 10*100*2 = 2000
    pos = db.get_options_position(pid)
    options_engine._apply_intraday_stops(
        [pos], {pid: (3.0, "schwab")}, _policies(account_id, StopPolicy("stop", 60.0))
    )
    after = db.options_cash_balance(account_id)
    # net: -2000 open + 4.00*100*2 = 800 close
    assert after - before == pytest.approx(-2000 + 800)


# ── Stop backtracking: book the sale at the minute the level was crossed ────

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


def test_stop_books_backtracked_time_and_spot(account_id, monkeypatch):
    """End to end through _apply_intraday_stops: closed_at is the crossing
    minute, exit_underlying the implied level, source 'backtracked'."""
    monkeypatch.setattr(options_engine, "_backtrack_stop_crossing",
                        lambda *a, **k: ("2026-07-29T14:32:00Z", 95.0))
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda syms: {})
    pid = _open_marked(account_id, entry=10.0, prev_mark=5.0)
    pos = db.get_options_position(pid)
    assert options_engine._apply_intraday_stops(
        [pos], {pid: (3.0, "schwab")}, _policies(account_id, StopPolicy("stop", 60.0))
    ) == 1
    row = db.get_options_position(pid)
    assert row["closed_at"] == "2026-07-29T14:32:00Z"
    assert row["exit_underlying"] == pytest.approx(95.0)
    assert row["exit_underlying_source"] == "backtracked"
    assert row["exit_premium"] == pytest.approx(4.0)


# ── Trailing stop: winners ride, but gains lock once armed ───────────────────

def test_peak_premium_seeded_and_ratchets(account_id):
    scan = db.create_spy_scan("2026-07-30", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict(entry_premium=10.0))
    assert db.get_options_position(pid)["peak_premium"] == pytest.approx(10.0)  # seeded at entry
    db.mark_options_position(pid, 14.0, 2800, "schwab")
    assert db.get_options_position(pid)["peak_premium"] == pytest.approx(14.0)  # ratchets up
    db.mark_options_position(pid, 11.0, 2200, "schwab")
    assert db.get_options_position(pid)["peak_premium"] == pytest.approx(14.0)  # never down


def test_effective_stop_unarmed_is_base():
    from web import options_allocator as oa
    outcome = oa.effective_stop_level(
        {"entry_premium": 10.0, "peak_premium": 14.9, "current_premium": 3.5},
        StopPolicy("stop", 60.0),
    )
    assert outcome.level == pytest.approx(4.0)
    assert outcome.exit_reason == "stop_loss"


def test_effective_stop_armed_locks_gains():
    from web import options_allocator as oa
    outcome = oa.effective_stop_level(
        {"entry_premium": 10.0, "peak_premium": 20.0},
        StopPolicy("trailing_pct", 30.0),
    )
    assert outcome.exit_reason == "trail_stop"
    assert outcome.level == pytest.approx(14.0)


def test_effective_stop_kill_switch():
    from web import options_allocator as oa
    outcome = oa.effective_stop_level(
        {"entry_premium": 10.0, "peak_premium": 30.0},
        StopPolicy("none"),
    )
    assert outcome.action == "hold"
    assert outcome.level == 0.0


def test_intraday_trail_closes_winner_at_trail_level(account_id, monkeypatch):
    """Winner peaked +100% then fell through the trail -> closed at the trail
    level with exit_reason trail_stop; a big win can't round-trip to -60%."""
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
    assert row["exit_premium"] == pytest.approx(14.0)
    assert row["realized_pnl"] == pytest.approx((14.0 - 10.0) * 100 * 2)  # profit kept


def test_forced_closes_trail():
    from web import options_allocator as oa
    pos = {"id": 1, "occ_symbol": "X", "underlying": "AAPL", "entry_premium": 10.0,
           "peak_premium": 20.0, "current_premium": 13.0, "contracts": 2,
           "expiration_date": "2099-01-15", "cost_basis": 2000.0}
    out = oa.forced_closes([pos], StopPolicy("trailing_pct", 30.0))
    assert len(out) == 1 and out[0][1] == "trail_stop"
    healthy = dict(pos, current_premium=15.0)  # above trail 14.0
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


def test_intraday_trailing_dollar_fills_at_level(account_id, monkeypatch):
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
    assert row["exit_premium"] == pytest.approx(18.0)


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
    # immediately at the trigger level without ever arming.
    stopped = options_engine._apply_intraday_stops(
        [pos], {pid: (3.7, "schwab")}, _policies(account_id, StopPolicy("stop_limit", 60.0, 10.0))
    )
    assert stopped == 1
    row = db.get_options_position(pid)
    assert row["status"] == "closed"
    assert row["exit_reason"] == "stop_limit"
    assert row["exit_premium"] == pytest.approx(4.0)
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
        return {s: {"price": price_map.get(s, 1.0)} for s in symbols}

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
    assert row1["exit_premium"] == pytest.approx(4.0)
    assert row2["status"] == "closed"
    assert row2["exit_reason"] == "trail_stop"
    assert row2["exit_premium"] == pytest.approx(14.0)


# ── Same-day movers pre-screen cache ─────────────────────────────────────────

class TestPrescreenSameDayCache:
    @pytest.fixture(autouse=True)
    def _clear_prescreen_cache(self):
        options_engine._PRESCREEN_CACHE.clear()
        yield
        options_engine._PRESCREEN_CACHE.clear()

    @pytest.fixture()
    def fake_download(self, monkeypatch):
        calls = {"n": 0}
        rows = 21
        data = {}
        for i, ticker in enumerate(["AAA", "BBB", "CCC"]):
            start = 100.0 + i * 10.0
            data[("Close", ticker)] = [start + j for j in range(rows)]
            data[("Volume", ticker)] = [1_000_000 + i * 100_000] * rows
        df = pd.DataFrame(data)

        def _download(*args, **kwargs):
            calls["n"] += 1
            return df

        monkeypatch.setattr(options_engine.yf, "download", _download)
        return {"calls": calls, "df": df, "download": _download}

    @staticmethod
    def _partial_df(present_tickers, rows=21):
        data = {}
        for i, ticker in enumerate(present_tickers):
            start = 100.0 + i * 10.0
            data[("Close", ticker)] = [start + j for j in range(rows)]
            data[("Volume", ticker)] = [1_000_000 + i * 100_000] * rows
        return pd.DataFrame(data)

    def test_second_same_day_call_is_a_cache_hit(self, fake_download):
        tickers = ["AAA", "BBB", "CCC"]
        trade_date = "2026-07-29"
        r1 = options_engine.prescreen(tickers, 3, trade_date=trade_date)
        r2 = options_engine.prescreen(tickers, 3, trade_date=trade_date)
        assert fake_download["calls"]["n"] == 1
        assert r1 == r2

    def test_returns_a_copy_not_the_cached_list(self, fake_download):
        tickers = ["AAA", "BBB", "CCC"]
        trade_date = "2026-07-29"
        r1 = options_engine.prescreen(tickers, 3, trade_date=trade_date)
        r1.append("SPY")
        r2 = options_engine.prescreen(tickers, 3, trade_date=trade_date)
        assert "SPY" not in r2
        assert r1 is not r2
        assert fake_download["calls"]["n"] == 1

    def test_different_trade_date_refetches(self, fake_download):
        tickers = ["AAA", "BBB", "CCC"]
        r1 = options_engine.prescreen(tickers, 3, trade_date="2026-07-29")
        r2 = options_engine.prescreen(tickers, 3, trade_date="2026-07-30")
        assert fake_download["calls"]["n"] == 2
        assert r1 == r2

    def test_prior_day_entry_is_evicted(self, fake_download):
        tickers = ["AAA", "BBB", "CCC"]
        options_engine.prescreen(tickers, 3, trade_date="2026-07-29")
        options_engine.prescreen(tickers, 3, trade_date="2026-07-30")
        assert options_engine._PRESCREEN_CACHE.stats()["size"] == 1
        options_engine.prescreen(tickers, 3, trade_date="2026-07-29")
        assert fake_download["calls"]["n"] == 3
        assert options_engine._PRESCREEN_CACHE.stats()["size"] == 1

    def test_different_top_n_refetches(self, fake_download):
        tickers = ["AAA", "BBB", "CCC"]
        options_engine.prescreen(tickers, 3, trade_date="2026-07-29")
        options_engine.prescreen(tickers, 2, trade_date="2026-07-29")
        assert fake_download["calls"]["n"] == 2

    def test_different_ticker_set_refetches(self, fake_download):
        options_engine.prescreen(["AAA", "BBB", "CCC"], 3, trade_date="2026-07-29")
        options_engine.prescreen(["AAA", "BBB"], 3, trade_date="2026-07-29")
        assert fake_download["calls"]["n"] == 2

    def test_ticker_order_does_not_matter(self, fake_download):
        r1 = options_engine.prescreen(["AAA", "BBB", "CCC"], 3, trade_date="2026-07-29")
        r2 = options_engine.prescreen(["CCC", "AAA", "BBB"], 3, trade_date="2026-07-29")
        assert fake_download["calls"]["n"] == 1
        assert r1 == r2

    def test_no_trade_date_bypasses_the_cache(self, fake_download):
        tickers = ["AAA", "BBB", "CCC"]
        r1 = options_engine.prescreen(tickers, 3, trade_date=None)
        r2 = options_engine.prescreen(tickers, 3, trade_date=None)
        assert fake_download["calls"]["n"] == 2
        assert r1 == r2
        assert options_engine._PRESCREEN_CACHE.stats()["size"] == 0

    def test_ttl_expiry_refetches(self, fake_download, monkeypatch):
        tickers = ["AAA", "BBB", "CCC"]
        trade_date = "2026-07-29"
        start = 1000.0
        monkeypatch.setattr(market_cache, "_now", lambda: start)
        r1 = options_engine.prescreen(tickers, 3, trade_date=trade_date)
        monkeypatch.setattr(
            market_cache,
            "_now",
            lambda: start + options_engine._PRESCREEN_TTL_SECONDS + 1,
        )
        r2 = options_engine.prescreen(tickers, 3, trade_date=trade_date)
        assert fake_download["calls"]["n"] == 2
        assert r1 == r2

    def test_empty_result_is_not_cached(self, monkeypatch):
        calls = {"n": 0}

        def empty_download(*args, **kwargs):
            calls["n"] += 1
            return pd.DataFrame()

        monkeypatch.setattr(options_engine.yf, "download", empty_download)
        tickers = ["AAA", "BBB", "CCC"]
        trade_date = "2026-07-29"
        r1 = options_engine.prescreen(tickers, 3, trade_date=trade_date)
        r2 = options_engine.prescreen(tickers, 3, trade_date=trade_date)
        assert r1 == []
        assert r2 == []
        assert calls["n"] == 2
        assert options_engine._PRESCREEN_CACHE.stats()["size"] == 0

    def test_download_failure_is_not_cached(self, fake_download, monkeypatch):
        calls = {"n": 0}
        df = fake_download["df"]

        def flaky_download(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("network down")
            return df

        monkeypatch.setattr(options_engine.yf, "download", flaky_download)
        tickers = ["AAA", "BBB", "CCC"]
        trade_date = "2026-07-29"
        with pytest.raises(RuntimeError, match="pre-screen bulk download failed"):
            options_engine.prescreen(tickers, 3, trade_date=trade_date)
        assert options_engine._PRESCREEN_CACHE.stats()["size"] == 0
        r = options_engine.prescreen(tickers, 3, trade_date=trade_date)
        assert calls["n"] == 2
        assert r == ["AAA", "BBB", "CCC"]

    def test_concurrent_callers_get_consistent_results(self, fake_download):
        results = []
        errors = []

        def worker():
            try:
                results.append(
                    options_engine.prescreen(
                        ["AAA", "BBB", "CCC"], 3, trade_date="2026-07-29"
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not errors
        assert len(results) == 3
        assert results[0] == results[1] == results[2]
        assert results[0] is not results[1]
        assert results[0] is not results[2]
        assert results[1] is not results[2]

    def test_partial_prescreen_below_threshold_is_not_cached(self, monkeypatch):
        """Only 1 of 5 tickers has usable data — below the 80% completeness
        gate, so the cache write is skipped.
        """
        calls = {"n": 0}

        def _download(*args, **kwargs):
            calls["n"] += 1
            return self._partial_df(["A"])

        monkeypatch.setattr(options_engine.yf, "download", _download)

        tickers = ["A", "B", "C", "D", "E"]
        r1 = options_engine.prescreen(tickers, 5, trade_date="2026-07-29")
        r2 = options_engine.prescreen(tickers, 5, trade_date="2026-07-29")

        assert calls["n"] == 2
        assert options_engine._PRESCREEN_CACHE.stats()["size"] == 0
        assert r1 == ["A"]
        assert r2 == ["A"]

    def test_partial_prescreen_still_returns_usable_tickers(self, monkeypatch):
        """The completeness gate must block only the cache write, never the
        return value.
        """
        def _download(*args, **kwargs):
            return self._partial_df(["A"])

        monkeypatch.setattr(options_engine.yf, "download", _download)

        tickers = ["A", "B", "C", "D", "E"]
        r = options_engine.prescreen(tickers, 5, trade_date="2026-07-29")
        assert r
        assert set(r).issubset({"A"})

    def test_prescreen_threshold_uses_scored_not_truncated_result(self, monkeypatch):
        """4 of 5 tickers are scoreable (meets the 80% gate) but top_n=2
        truncates the returned list to two names. The gate must see the
        pre-truncation scored count, not the shorter result.
        """
        calls = {"n": 0}

        def _download(*args, **kwargs):
            calls["n"] += 1
            return self._partial_df(["A", "B", "C", "D"])

        monkeypatch.setattr(options_engine.yf, "download", _download)

        tickers = ["A", "B", "C", "D", "E"]
        r1 = options_engine.prescreen(tickers, 2, trade_date="2026-07-29")
        r2 = options_engine.prescreen(tickers, 2, trade_date="2026-07-29")

        assert calls["n"] == 1
        assert options_engine._PRESCREEN_CACHE.stats()["size"] == 1
        assert len(r1) == 2
        assert set(r1).issubset({"A", "B", "C", "D"})
        assert r1 == r2