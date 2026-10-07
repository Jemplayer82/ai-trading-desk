"""Synthetic monitor safety checks: isolated SQLite, injected quotes, no orders."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from web import db
from web import stop_monitor as sm

pytestmark = pytest.mark.unit
NOW = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)
SYMBOL = "AAPL  261120C00250000"


@pytest.fixture()
def monitor_db(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    return path


def open_position(name="bull", stop_type="trailing_staged", stop_value=20, symbol=SYMBOL):
    aid = db.create_paper_account(
        name, kind="options", stop_type=stop_type, stop_value=stop_value,
        stage_trigger_pct=20, stage_trail_pct=10,
    )
    scan = db.create_spy_scan("2026-10-02", paper_account_id=aid, kind="options")
    pid = db.open_options_position(aid, scan, dict(
        occ_symbol=symbol, underlying="AAPL", put_call="CALL", strike=250,
        expiration_date="2026-11-20", contracts=1, entry_premium=10,
        entry_bid=10, entry_ask=10.5,
    ))
    return aid, pid


def quote(bid=10, ask=None, now=NOW, age=0):
    return {"quote": {"bidPrice": bid, "askPrice": bid + .5 if ask is None and isinstance(bid, (int, float)) else ask,
                      "quoteTime": int((now - timedelta(seconds=age)).timestamp() * 1000)}}


class QuotesOnly:
    """A provider whose order methods fail the test if execution escapes paper DB."""
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.order_calls = 0

    def get_quotes(self, symbols):
        self.calls.append(symbols)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def place_order(self, *args, **kwargs):
        self.order_calls += 1
        pytest.fail("Monitor attempted a real brokerage order")

    modify_order = place_order
    cancel_order = place_order


def row(path, pid):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM options_positions WHERE id=?", (pid,)).fetchone())


def rows(path, table):
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


def monitor(provider, tmp_path, **kwargs):
    return sm.StopMonitor(provider, kill_file=tmp_path / "KILL", heartbeat_file=tmp_path / "heartbeat", **kwargs)


def poll_bid(tmp_path, bid, **kwargs):
    provider = QuotesOnly({SYMBOL: quote(bid=bid)})
    result = monitor(provider, tmp_path, **kwargs).poll(now=NOW)
    assert provider.order_calls == 0
    return result


def test_peak_ratchets_and_staged_switch_at_exact_threshold(monitor_db, tmp_path):
    _, pid = open_position()
    assert poll_bid(tmp_path, 11)["closed"] == 0
    assert row(monitor_db, pid)["peak_premium"] == 11
    assert poll_bid(tmp_path, 10)["closed"] == 0
    assert row(monitor_db, pid)["peak_premium"] == 11
    assert poll_bid(tmp_path, 12)["closed"] == 0
    assert row(monitor_db, pid)["peak_premium"] == 12
    assert poll_bid(tmp_path, 10.8)["closed"] == 1
    closed = row(monitor_db, pid)
    assert closed["status"] == "closed"
    assert closed["exit_premium"] == 10.8
    assert closed["exit_reason"] == "trail_stop"


def test_gap_fills_observed_bid_not_stop_level(monitor_db, tmp_path):
    _, pid = open_position()
    poll_bid(tmp_path, 12)
    assert poll_bid(tmp_path, 7)["closed"] == 1
    position = row(monitor_db, pid)
    assert position["exit_premium"] == 7
    assert position["realized_pnl"] == -350  # entry fills at the ask (10.5), exit at the observed bid (7)
    closes = [r for r in rows(monitor_db, "options_cash_ledger") if r[3] == "close"]
    assert len(closes) == 1


def test_entry_bid_is_stop_reference(monitor_db, tmp_path):
    _, pid = open_position(stop_type="stop")
    with sqlite3.connect(monitor_db) as conn:
        conn.execute("UPDATE options_positions SET entry_premium=11, peak_premium=10 WHERE id=?", (pid,))
    assert poll_bid(tmp_path, 8.5)["closed"] == 0
    assert poll_bid(tmp_path, 8)["closed"] == 1
    assert row(monitor_db, pid)["exit_reason"] == "stop_loss"


@pytest.mark.parametrize("bad", [
    quote(bid=0), quote(bid=None, ask=1), quote(bid=8, ask=7),
    quote(bid=8, ask=8), quote(bid=8, age=21), quote(bid=8, age=-3),
    quote(bid=float("nan")), quote(bid=float("inf")),
    quote(bid=8, ask=float("nan")), quote(bid="garbage", ask=9),
    {"quote": {"bidPrice": 8, "askPrice": 9}},
    {"quote": {"bidPrice": 8, "askPrice": 9, "quoteTime": "garbage"}},
    {"quote": []}, {},
])
def test_invalid_quotes_are_counted_exclusions(monitor_db, tmp_path, bad):
    _, pid = open_position()
    before = row(monitor_db, pid)
    provider = QuotesOnly({SYMBOL: bad})
    result = monitor(provider, tmp_path).poll(now=NOW)
    assert result["closed"] == 0
    assert sum(result["exclusions"].values()) >= 1
    assert row(monitor_db, pid) == before
    assert provider.order_calls == 0


@pytest.mark.parametrize("age", [20, -2])
def test_quote_freshness_boundaries_are_inclusive(monitor_db, tmp_path, age, monkeypatch):
    monkeypatch.setattr(sm.time, "monotonic", lambda: 100.0)
    _, pid = open_position()
    provider = QuotesOnly({SYMBOL: quote(bid=8, age=age)})
    assert monitor(provider, tmp_path).poll(now=NOW)["closed"] == 1
    assert row(monitor_db, pid)["exit_premium"] == 8


def test_missing_entry_bid_is_excluded(monitor_db, tmp_path):
    _, pid = open_position()
    with sqlite3.connect(monitor_db) as conn:
        conn.execute("UPDATE options_positions SET entry_bid=NULL WHERE id=?", (pid,))
    result = poll_bid(tmp_path, 5)
    assert result["closed"] == 0
    assert sum(result["exclusions"].values()) >= 1
    assert row(monitor_db, pid)["status"] == "open"


def test_restart_and_two_accounts_same_symbol_close_once_each(monitor_db, tmp_path):
    _, first = open_position("first")
    _, second = open_position("second")
    provider = QuotesOnly({SYMBOL: quote(bid=8)})
    assert monitor(provider, tmp_path).poll(now=NOW)["closed"] == 2
    assert len(provider.calls) == 1
    assert provider.calls[0] == [SYMBOL]
    cash_after = rows(monitor_db, "options_cash_ledger")
    assert monitor(provider, tmp_path).poll(now=NOW)["closed"] == 0
    assert rows(monitor_db, "options_cash_ledger") == cash_after
    assert row(monitor_db, first)["status"] == row(monitor_db, second)["status"] == "closed"
    assert provider.order_calls == 0


def test_dry_run_preserves_positions_cash_and_peak(monitor_db, tmp_path):
    _, pid = open_position()
    before = row(monitor_db, pid)
    cash_before = rows(monitor_db, "options_cash_ledger")
    assert poll_bid(tmp_path, 15, dry_run=True)["closed"] == 0
    result = poll_bid(tmp_path, 7, dry_run=True)
    assert result["would_close"] == 1
    assert result["closed"] == 0
    assert row(monitor_db, pid) == before
    assert rows(monitor_db, "options_cash_ledger") == cash_before


def test_kill_file_prevents_quotes_and_closing(monitor_db, tmp_path):
    _, pid = open_position()
    (tmp_path / "KILL").touch()
    provider = QuotesOnly({SYMBOL: quote(bid=7)})
    result = monitor(provider, tmp_path).poll(now=NOW)
    assert result["killed"]
    assert provider.calls == []
    assert row(monitor_db, pid)["status"] == "open"


@pytest.mark.parametrize("when", [
    datetime(2026, 10, 2, 13, 29, 59, tzinfo=timezone.utc),
    datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc),
    datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc),
    datetime(2026, 12, 25, 15, 0, tzinfo=timezone.utc),
])
def test_market_hours_and_holiday_gate(monitor_db, tmp_path, when):
    open_position()
    provider = QuotesOnly({SYMBOL: quote(bid=7, now=when)})
    result = monitor(provider, tmp_path).poll(now=when)
    assert result["market_closed"]
    assert provider.calls == []


def test_market_open_boundary(monitor_db, tmp_path):
    open_position()
    when = datetime(2026, 10, 2, 13, 30, tzinfo=timezone.utc)
    provider = QuotesOnly({SYMBOL: quote(bid=8, now=when)})
    assert monitor(provider, tmp_path).poll(now=when)["closed"] == 1


def test_rate_guard_and_heartbeat(monitor_db, tmp_path, monkeypatch):
    open_position()
    clock = [100.0]
    monkeypatch.setattr(sm.time, "monotonic", lambda: clock[0])
    provider = QuotesOnly({SYMBOL: quote(bid=10)})
    instance = monitor(provider, tmp_path)
    instance.poll(now=NOW)
    assert (tmp_path / "heartbeat").exists()
    clock[0] = 101.9
    assert instance.poll(now=NOW)["rate_limited"]
    assert len(provider.calls) == 1
    clock[0] = 102.0
    assert not instance.poll(now=NOW)["rate_limited"]
    assert len(provider.calls) == 2


def test_batches_at_most_100_symbols(monitor_db, tmp_path):
    response = {}
    for i in range(101):
        symbol = f"AAPL  261120C{250000 + i:08d}"
        open_position(f"account-{i}", symbol=symbol)
        response[symbol] = quote(bid=10)
    provider = QuotesOnly(response)
    result = monitor(provider, tmp_path).poll(now=NOW)
    assert result["closed"] == 0
    assert sorted(map(len, provider.calls)) == [1, 100]
    assert len({symbol for batch in provider.calls for symbol in batch}) == 101
    assert provider.order_calls == 0


def test_failure_alert_after_three_polls_and_recovery(monitor_db, tmp_path, monkeypatch):
    _, pid = open_position()
    clock = [100.0]
    monkeypatch.setattr(sm.time, "monotonic", lambda: clock[0])
    provider = QuotesOnly(RuntimeError("synthetic quote outage"))
    instance = monitor(provider, tmp_path)
    for attempt in range(3):
        clock[0] = 100.0 + 2 * attempt
        result = instance.poll(now=NOW)
        assert result["failed"]
        assert result["closed"] == 0
        assert len(rows(monitor_db, "stop_monitor_alerts")) == (1 if attempt == 2 else 0)
    assert row(monitor_db, pid)["status"] == "open"
    provider.response = {SYMBOL: quote(bid=10)}
    clock[0] = 106
    assert not instance.poll(now=NOW)["failed"]
    provider.response = RuntimeError("another isolated outage")
    clock[0] = 108
    instance.poll(now=NOW)
    assert len(rows(monitor_db, "stop_monitor_alerts")) == 1
    assert provider.order_calls == 0


def test_trigger_audit_records_quote_peak_level_and_is_idempotent(monitor_db, tmp_path):
    _, pid = open_position()
    poll_bid(tmp_path, 12)
    provider = QuotesOnly({SYMBOL: quote(bid=7, ask=7.5, age=4)})
    assert monitor(provider, tmp_path).poll(now=NOW)["closed"] == 1
    with sqlite3.connect(monitor_db) as conn:
        conn.row_factory = sqlite3.Row
        audits = [dict(r) for r in conn.execute("SELECT * FROM stop_monitor_audit")]
    assert len(audits) == 1
    audit = audits[0]
    assert audit["position_id"] == pid
    assert audit["bid"] == 7
    assert audit["ask"] == 7.5
    assert audit["peak"] == 12
    assert audit["level"] == pytest.approx(10.8)
    assert audit["exit_reason"] == "trail_stop"
    assert audit["trigger_time"]
    assert audit["quote_time"]
    assert audit["latency"] >= 0
    monitor(provider, tmp_path).poll(now=NOW)
    assert len(rows(monitor_db, "stop_monitor_audit")) == 1


def test_audit_failure_rolls_back_close_cash_and_peak(monitor_db, tmp_path):
    _, pid = open_position()
    provider = QuotesOnly({SYMBOL: quote(bid=7)})
    instance = monitor(provider, tmp_path)
    before = row(monitor_db, pid)
    cash_before = rows(monitor_db, "options_cash_ledger")
    with sqlite3.connect(monitor_db) as conn:
        conn.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON stop_monitor_audit BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
        instance.poll(now=NOW)
    assert row(monitor_db, pid) == before
    assert rows(monitor_db, "options_cash_ledger") == cash_before
    assert rows(monitor_db, "stop_monitor_audit") == []


def test_stop_limit_gap_arms_then_rebound_fills_observed_bid(monitor_db, tmp_path):
    aid, pid = open_position(stop_type="stop")
    with sqlite3.connect(monitor_db) as conn:
        conn.execute("UPDATE paper_accounts SET stop_type='stop_limit', stop_limit_offset=10 WHERE id=?", (aid,))
    assert poll_bid(tmp_path, 7)["closed"] == 0
    armed = row(monitor_db, pid)
    assert armed["status"] == "open"
    assert armed["stop_triggered_at"]
    assert poll_bid(tmp_path, 7.4)["closed"] == 1
    closed = row(monitor_db, pid)
    assert closed["exit_premium"] == 7.4
    assert closed["exit_reason"] == "stop_limit"


@pytest.mark.parametrize("stamp", [NOW.isoformat(), NOW.isoformat().replace("+00:00", "Z")])
def test_actual_mcp_iso_quote_time(monitor_db, tmp_path, stamp):
    _, pid = open_position()
    raw = quote(bid=8)
    raw["quote"]["quoteTime"] = stamp
    provider = QuotesOnly({SYMBOL: raw})
    assert monitor(provider, tmp_path).poll(now=NOW)["closed"] == 1
    assert row(monitor_db, pid)["exit_premium"] == 8
