"""Daily equity allocation over the shared research (web/spy_routes).

_run_equity_allocation waits for today's research and the open, marks the
prior portfolio to market, then rebalances with cadence="daily" at live
quotes. With no quotes it fails and leaves the previous portfolio in place.

No network access. Run with: uv run pytest tests/test_equity_allocation.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from web import (
    alerts,
    db,
    market_calendar,
    research_engine,
    scan_queue,
    spy_allocator,
    spy_routes,
    spy_scanner,
)

pytestmark = pytest.mark.unit

TD = "2026-09-26"  # a Saturday: the research and market-open waits return at once

_ALLOC_RESULT = {
    "allocations": [{
        "ticker": "AAA", "action": "NEW", "shares": 2, "entry_price": 50.0,
        "dollar_amount": 100.0, "cost_basis": 100.0, "signal": "Buy",
    }],
    "report_md": "daily",
    "starting_value": 100000.0,
}


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()


def _seed_research(trade_date: str = TD) -> int:
    rid = db.create_spy_scan(trade_date, kind="research")
    for ticker, signal, conv in (("AAA", "Buy", 9), ("BBB", "Sell", 8)):
        aid = db.create_analysis({"ticker": ticker, "trade_date": trade_date})
        db.complete_analysis(aid, {"final_trade_decision": f"Rating: {signal}"}, signal)
        db.upsert_spy_quick_result(rid, ticker, signal=signal, conviction=conv,
                                   reasoning="r", analysis_id=aid)
    db.complete_spy_scan(rid, "research", [])
    return rid


def _no_sleep(_seconds):
    raise AssertionError("must not sleep: research is complete and the market wait is a no-op")


@pytest.fixture()
def env(tmp_db, monkeypatch):
    """Seeded account + research + allocation row, with recorders installed."""
    acct = db.create_paper_account("Daily", starting_capital=100000.0,
                                   aggressiveness=7, bias="bullish", kind="equity")
    rid = _seed_research()
    events: list[tuple[Any, ...]] = []
    run_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        market_calendar, "now_et",
        lambda: datetime(2026, 9, 26, 10, 0, tzinfo=market_calendar._ET),
    )
    monkeypatch.setattr(research_engine.time_mod, "sleep", _no_sleep)
    monkeypatch.setattr(spy_scanner, "fetch_live_prices",
                        lambda t, **k: {"AAA": 50.0, "BBB": 20.0})

    refreshes: list[dict[str, Any]] = []
    marks: dict[int, Any] = {}

    def fake_refresh(scan_id):
        # Record whether the allocation lock was held and the clock at refresh
        # time; asserted in the test body so a caller's try/except cannot
        # swallow the check.
        refreshes.append({"scan_id": scan_id,
                          "locked": research_engine._ALLOC_LOCK.locked(),
                          "now": market_calendar.now_et()})
        events.append(("refresh", scan_id))
        mark = marks.get(scan_id)
        if mark is not None:
            mark(scan_id)
        return {}

    monkeypatch.setattr(spy_scanner, "refresh_portfolio_prices", fake_refresh)

    def fake_run(candidates, trade_date, config, **kwargs):
        events.append(("run",))
        run_calls.append({"candidates": [dict(c) for c in candidates],
                          "trade_date": trade_date, **kwargs})
        return dict(_ALLOC_RESULT)

    monkeypatch.setattr(spy_allocator, "run", fake_run)

    # start_allocation copies the account's settings onto the row; mirror that.
    sid = db.create_spy_scan(TD, kind="equity", paper_account_id=acct,
                             aggressiveness=7, bias="bullish")
    return {"acct": acct, "rid": rid, "sid": sid, "events": events, "run_calls": run_calls,
            "refreshes": refreshes, "marks": marks}


def _prev_scan(acct: int) -> int:
    prev = db.create_spy_scan("2026-09-19", kind="equity", paper_account_id=acct)
    db.complete_spy_scan(prev, "weekly", [{
        "ticker": "BBB", "action": "HOLD", "shares": 5, "entry_price": 18.0,
        "dollar_amount": 90.0, "cost_basis": 90.0,
    }])
    db.update_spy_scan(prev, current_value=100500.0)
    return prev


def test_fresh_account_allocates_research_buys_at_live_prices(env):
    spy_routes._run_equity_allocation(env["sid"], TD)

    assert len(env["run_calls"]) == 1
    call = env["run_calls"][0]
    assert [c["ticker"] for c in call["candidates"]] == ["AAA"]  # BBB is Sell and not held
    assert call["candidates"][0]["entry_price"] == 50.0
    assert call["cadence"] == "daily"
    assert call["previous_portfolio"] is None
    assert call["starting_value"] == 100000.0
    assert call["aggressiveness"] == 7
    assert call["bias"] == "bullish"

    row = db.get_spy_scan(env["sid"])
    assert row["status"] == "completed"
    assert row["portfolio_json"][0]["ticker"] == "AAA"
    assert row["research_scan_id"] == env["rid"]
    assert ("refresh", env["sid"]) in env["events"]


def _mark_prev_up(scan_id: int) -> None:
    """Fake mark-to-market: BBB 18 -> 22, account value 100,500 -> 100,800."""
    db.update_spy_scan_prices(scan_id, 100800.0, "marked", [{
        "ticker": "BBB", "action": "HOLD", "shares": 5, "entry_price": 18.0,
        "dollar_amount": 90.0, "cost_basis": 90.0,
        "current_price": 22.0, "current_value": 110.0,
    }])


def _stop_out_prev(scan_id: int) -> None:
    """Fake mark-to-market that fires BBB's stop: the row becomes EXITED."""
    db.update_spy_scan_prices(scan_id, 100750.0, "BBB stopped", [{
        "ticker": "BBB", "action": "EXITED", "shares": 0, "entry_price": 18.0,
        "dollar_amount": 0.0, "cost_basis": 0.0, "current_value": 0.0,
    }])


def test_rebalances_from_previous_portfolio_marked_after_open(env):
    prev = _prev_scan(env["acct"])
    env["marks"][prev] = _mark_prev_up

    spy_routes._run_equity_allocation(env["sid"], TD)

    events = env["events"]
    assert events.index(("refresh", prev)) < events.index(("run",))
    assert events.index(("run",)) < events.index(("refresh", env["sid"]))
    prev_refresh = next(r for r in env["refreshes"] if r["scan_id"] == prev)
    assert prev_refresh["locked"], "prev portfolio must be marked inside the allocation lock"

    call = env["run_calls"][0]
    tickers = {c["ticker"] for c in call["candidates"]}
    assert tickers == {"AAA", "BBB"}  # BBB is Sell but held, so it reaches the rebalance
    prices = {c["ticker"]: c["entry_price"] for c in call["candidates"]}
    assert prices == {"AAA": 50.0, "BBB": 20.0}
    # The rebalance starts from the re-read, marked row, not the pre-open copy.
    assert call["previous_portfolio"][0]["ticker"] == "BBB"
    assert call["previous_portfolio"][0]["current_price"] == 22.0
    # Book value: 100,800 marked value minus BBB's +20 unrealized gain.
    assert call["starting_value"] == pytest.approx(100780.0)

    row = db.get_spy_scan(env["sid"])
    assert row["status"] == "completed"
    assert row["previous_scan_id"] == prev


def test_stop_exit_in_the_open_mark_reaches_the_rebalance(env):
    prev = _prev_scan(env["acct"])
    env["marks"][prev] = _stop_out_prev

    spy_routes._run_equity_allocation(env["sid"], TD)

    prev_refresh = next(r for r in env["refreshes"] if r["scan_id"] == prev)
    assert prev_refresh["locked"]
    call = env["run_calls"][0]
    assert call["previous_portfolio"][0]["action"] == "EXITED"
    assert call["starting_value"] == pytest.approx(100750.0)
    # BBB was stopped out, so it is no longer held; as a Sell it drops out.
    assert [c["ticker"] for c in call["candidates"]] == ["AAA"]


def test_trading_day_marks_previous_portfolio_only_after_the_open(env, monkeypatch):
    td = "2026-09-28"  # a Monday, not an NYSE holiday
    assert market_calendar.is_trading_day(datetime.fromisoformat(td).date())
    _seed_research(td)
    prev = _prev_scan(env["acct"])
    env["marks"][prev] = _mark_prev_up
    sid = db.create_spy_scan(td, kind="equity", paper_account_id=env["acct"],
                             aggressiveness=7, bias="bullish")

    clock = [datetime(2026, 9, 28, 9, 20, tzinfo=market_calendar._ET)]
    slept: list[float] = []

    def advancing_sleep(seconds):
        slept.append(seconds)
        clock[0] = clock[0] + timedelta(seconds=seconds)

    monkeypatch.setattr(market_calendar, "now_et", lambda: clock[0])
    monkeypatch.setattr(research_engine.time_mod, "sleep", advancing_sleep)

    spy_routes._run_equity_allocation(sid, td)

    assert slept, "the market-open wait must have slept before 09:35"
    prev_refresh = next(r for r in env["refreshes"] if r["scan_id"] == prev)
    opened = datetime(2026, 9, 28, *market_calendar.MARKET_OPEN_ET, tzinfo=market_calendar._ET)
    assert prev_refresh["now"] >= opened, "prev portfolio was marked before the open"
    assert prev_refresh["locked"]
    call = env["run_calls"][0]
    assert call["starting_value"] == pytest.approx(100780.0)
    assert db.get_spy_scan(sid)["status"] == "completed"


def test_no_live_quotes_fails_and_keeps_previous_portfolio(env, monkeypatch):
    prev = _prev_scan(env["acct"])
    monkeypatch.setattr(spy_scanner, "fetch_live_prices", lambda t, **k: {})
    monkeypatch.setattr(scan_queue, "refresh_creds_from_db", lambda: None)
    advanced: list[bool] = []
    monkeypatch.setattr(scan_queue, "_advance_queue_if_idle", lambda: advanced.append(True))
    monkeypatch.setattr(scan_queue, "_dequeue_next_scan",
                        lambda: pytest.fail("allocation rows must not dequeue unguarded"))
    alerted: list[dict[str, Any]] = []

    def capture_notify(*, kind, run_id, label, error, link=None):
        alerted.append({"kind": kind, "run_id": run_id, "label": label, "error": error})

    monkeypatch.setattr(alerts, "notify_run_failed", capture_notify)

    spy_routes._run_spy_scan_thread(env["sid"], TD)

    row = db.get_spy_scan(env["sid"])
    assert row["status"] == "failed"
    assert "no live quotes" in row["error"]
    assert env["run_calls"] == []
    assert db.get_latest_completed_spy_scan(paper_account_id=env["acct"])["id"] == prev
    assert len(alerted) == 1
    assert alerted[0]["kind"] == "S&P 500 allocation"
    assert alerted[0]["run_id"] == env["sid"]
    assert advanced == [True]


def test_allocation_row_without_account_raises(env):
    sid = db.create_spy_scan(TD, kind="equity", paper_account_id=None)
    with pytest.raises(RuntimeError, match="paper_account_id"):
        spy_routes._run_equity_allocation(sid, TD)


# ---------- Book-value capital (no phantom cash from unrealized P&L) ----------

def test_previous_state_starts_from_book_value_not_market_value():
    prev = {
        "id": 7,
        "current_value": 102000.0,
        "portfolio_json": [
            {"ticker": "AAA", "action": "HOLD", "shares": 100, "entry_price": 100.0,
             "dollar_amount": 10000.0, "cost_basis": 10000.0,
             "current_price": 120.0, "current_value": 12000.0},
            {"ticker": "BBB", "action": "EXITED", "shares": 0, "entry_price": 50.0,
             "cost_basis": 0.0, "current_value": 0.0},
        ],
    }
    portfolio, prev_id, starting_value = spy_routes._previous_portfolio_state(
        prev, {"starting_capital": 100000.0})
    assert prev_id == 7
    assert portfolio is prev["portfolio_json"]
    # The +$2,000 unrealized gain lives in the position's mark, not in cash.
    assert starting_value == pytest.approx(100000.0)


def test_repeated_daily_allocations_do_not_compound_unrealized_pnl(monkeypatch):
    from web import account_policy

    class _FailingLLM:
        def invoke(self, _messages):
            raise RuntimeError("force the deterministic daily fallback")

    monkeypatch.setattr(spy_allocator, "_llm", lambda _config: _FailingLLM())
    policy = account_policy.StopPolicy.from_account(None)
    prices = {"AAA": 120.0}

    portfolio = [{"ticker": "AAA", "action": "HOLD", "shares": 100, "entry_price": 100.0,
                  "allocation_pct": 10.0, "dollar_amount": 10000.0, "cost_basis": 10000.0}]
    basis = 100000.0
    values = []
    for _day in range(4):
        market = spy_scanner.apply_stops_and_value(
            portfolio, basis=basis, policy=policy, prices=prices)
        current_value = market["positions_value"] + market["cash"]
        values.append(round(current_value, 2))
        prev = {"id": 1, "portfolio_json": portfolio, "current_value": current_value}
        previous, _pid, basis = spy_routes._previous_portfolio_state(
            prev, {"starting_capital": 100000.0})
        alloc = spy_allocator.run([], "2026-09-28", {}, previous_portfolio=previous,
                                  starting_value=basis, cadence="daily")
        portfolio = alloc["allocations"]
        basis = alloc["starting_value"]

    assert values == [102000.0] * 4
