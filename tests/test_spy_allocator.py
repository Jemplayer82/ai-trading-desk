"""Unit tests for web/spy_allocator.py's LLM role selection."""

import json
from unittest.mock import MagicMock

import pytest

from web import spy_allocator

pytestmark = pytest.mark.unit


def test_llm_uses_quick_model(monkeypatch):
    captured: dict = {}

    def _fake_llm_for(*args, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(spy_allocator, "llm_for", _fake_llm_for)

    spy_allocator._llm({})

    assert captured.get("deep") is False


# ─── Daily cadence ────────────────────────────────────────────────────────────

EXPECTED_ADDENDUM = (
    "\nDAILY CADENCE (overrides the weekly framing above): This is a DAILY check-in "
    "against a fresh research pass, not a weekly rebalance. Default every existing "
    "position to HOLD at its current size. EXIT only on a SELL/Underweight rating or "
    "a conviction collapse; open NEW positions only for BUY/Overweight candidates with "
    "conviction ≥ 8; never ADD/TRIM merely to re-weight. Holdings marked 'not re-rated "
    "today' have NO new signal — HOLD them. For an existing holding return only HOLD or "
    "EXITED (ADDED/TRIMMED are treated as HOLD on the daily cadence); use NEW only for "
    "tickers you do not already hold. Turnover is a cost.\n"
)

PREVIOUS = [
    {"ticker": "AAPL", "action": "HOLD", "allocation_pct": 10.0, "dollar_amount": 10_000,
     "entry_price": 100.0, "shares": 100, "cost_basis": 10_000.0, "conviction": 7},
    {"ticker": "MSFT", "action": "NEW", "allocation_pct": 10.0, "dollar_amount": 10_000,
     "entry_price": 200.0, "shares": 50, "cost_basis": 10_000.0, "conviction": 6},
    {"ticker": "OLD", "action": "EXITED", "allocation_pct": 0, "dollar_amount": 0,
     "entry_price": 50.0, "shares": 0, "cost_basis": 0.0},
]


def _fake_llm(monkeypatch, content="[]", raises=None):
    """Patch ``spy_allocator._llm``; the fake records every message list it sees."""
    calls: list = []

    class _Fake:
        def invoke(self, messages):
            calls.append(messages)
            if raises is not None:
                raise raises
            return MagicMock(content=content)

    monkeypatch.setattr(spy_allocator, "_llm", lambda config: _Fake())
    return calls


def _run(candidates, previous=None, **kwargs):
    return spy_allocator.run(
        candidates, "2026-09-25", {}, previous_portfolio=previous,
        starting_value=100_000.0, **kwargs,
    )


def test_daily_addendum_text_matches_spec():
    assert spy_allocator._DAILY_REBALANCE_ADDENDUM == EXPECTED_ADDENDUM


def test_daily_cadence_adds_low_churn_block(monkeypatch):
    cands = [{"ticker": "AAPL", "signal": "Buy", "conviction": 8, "entry_price": 110.0}]
    _, max_pct, min_cash_pct = spy_allocator._position_limits(5, 100_000.0)
    weekly_system = spy_allocator._REBALANCE_SYSTEM_TEMPLATE.format(
        max_pct=max_pct, min_cash_pct=min_cash_pct,
        bias_context=spy_allocator._BIAS_CONTEXT["neutral"],
    )

    calls = _fake_llm(monkeypatch)
    _run(cands, PREVIOUS, cadence="daily")
    _run(cands, PREVIOUS, cadence="weekly")
    _run(cands, PREVIOUS)

    daily_sys, weekly_sys, default_sys = (c[0]["content"] for c in calls)
    assert "DAILY check-in" in daily_sys
    assert "Turnover is a cost" in daily_sys
    assert daily_sys == weekly_system + EXPECTED_ADDENDUM
    assert weekly_sys == weekly_system
    assert default_sys == weekly_system
    assert "DAILY" not in weekly_sys
    assert "DAILY" not in default_sys


def test_weekly_user_message_unchanged():
    cands = [{"ticker": "AAPL", "signal": "buy", "conviction": 8, "entry_price": 110.0}]
    default = spy_allocator.build_rebalance_user_message(cands, PREVIOUS, "2026-09-25", 100_000)
    weekly = spy_allocator.build_rebalance_user_message(
        cands, PREVIOUS, "2026-09-25", 100_000, cadence="weekly"
    )
    assert default == weekly
    assert (
        "MSFT | signal: SELL | conviction: 0/10 | entry_price: $200.00 | "
        "No longer in top candidates — consider exiting.\n"
    ) in weekly
    assert "OLD |" not in weekly


def test_daily_unrated_holding_is_hold_not_sell(monkeypatch):
    cands = [{"ticker": "AAPL", "signal": "buy", "conviction": 8, "entry_price": 110.0}]
    msg = spy_allocator.build_rebalance_user_message(
        cands, PREVIOUS, "2026-09-25", 100_000, cadence="daily"
    )
    assert (
        "MSFT | signal: HOLD | conviction: 6/10 | entry_price: $200.00 | "
        "held: 50 shares, cost basis $10,000.00 | "
        "Not re-rated in today's research — no new signal; default HOLD.\n"
    ) in msg
    assert "MSFT | signal: SELL" not in msg
    assert "AAPL | signal: BUY | conviction: 8/10" in msg

    calls = _fake_llm(monkeypatch)
    _run(cands, PREVIOUS, cadence="daily")
    assert calls[0][1]["content"] == msg


def test_daily_report_title(monkeypatch):
    cands = [{"ticker": "AAPL", "signal": "buy", "conviction": 8, "entry_price": 110.0}]
    _fake_llm(monkeypatch)
    daily = _run(cands, PREVIOUS, cadence="daily")
    weekly = _run(cands, PREVIOUS)
    assert "(Daily allocation)" in daily["report_md"].splitlines()[0]
    assert "**New positions:**" in daily["report_md"]
    assert "(Rebalance)" in weekly["report_md"].splitlines()[0]
    assert "Daily allocation" not in weekly["report_md"]


def test_fresh_mode_daily_has_no_addendum(monkeypatch):
    cands = [{"ticker": "AAPL", "signal": "buy", "conviction": 8, "entry_price": 110.0}]
    calls = _fake_llm(monkeypatch)
    daily = _run(cands, None, cadence="daily")
    weekly = _run(cands, None)
    assert calls[0] == calls[1]
    assert "DAILY" not in calls[0][0]["content"]
    assert "(Initial Portfolio)" in daily["report_md"].splitlines()[0]
    assert daily["report_md"] == weekly["report_md"]


def test_fallback_treats_overweight_as_buy():
    cands = [
        {"ticker": "AAPL", "signal": "Overweight", "conviction": 7, "entry_price": 100.0},
        {"ticker": "MSFT", "signal": "Hold", "conviction": 9, "entry_price": 200.0},
        {"ticker": "NVDA", "signal": "Buy", "conviction": 6, "entry_price": 150.0},
    ]
    fresh = spy_allocator._fallback_fresh(cands)
    assert sorted(r["ticker"] for r in fresh) == ["AAPL", "NVDA"]

    previous = [{"ticker": "AAPL", "action": "HOLD", "entry_price": 90.0}]
    rebal = spy_allocator._fallback_rebalance(cands, previous, 100_000)
    by_ticker = {r["ticker"]: r for r in rebal}
    assert by_ticker["AAPL"]["action"] == "HOLD"
    assert by_ticker["NVDA"]["action"] == "NEW"
    assert "MSFT" not in by_ticker


def test_daily_fallback_holds_unrated_exits_sell_opens_high_conviction_buy(monkeypatch):
    previous = [
        {"ticker": "AAPL", "action": "HOLD", "allocation_pct": 20.0, "dollar_amount": 20_000,
         "entry_price": 100.0, "shares": 200, "cost_basis": 20_000.0},
        {"ticker": "MSFT", "action": "NEW", "allocation_pct": 20.0, "dollar_amount": 20_000,
         "entry_price": 200.0, "shares": 100, "cost_basis": 20_000.0},
        {"ticker": "TSLA", "action": "HOLD", "allocation_pct": 10.0, "dollar_amount": 10_000,
         "entry_price": 250.0, "shares": 40, "cost_basis": 10_000.0},
    ]
    cands = [
        {"ticker": "MSFT", "signal": "Underweight", "conviction": 3, "entry_price": 180.0},
        {"ticker": "TSLA", "signal": "Buy", "conviction": 5, "entry_price": 260.0},
        {"ticker": "NVDA", "signal": "Overweight", "conviction": 8, "entry_price": 100.0},
        {"ticker": "AMD", "signal": "Buy", "conviction": 9, "entry_price": 50.0},
        {"ticker": "INTC", "signal": "Buy", "conviction": 7, "entry_price": 20.0},
        {"ticker": "PFE", "signal": "Hold", "conviction": 10, "entry_price": 25.0},
    ]

    rows = spy_allocator._fallback_daily(cands, previous, 100_000.0)
    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["AAPL"]["action"] == "HOLD"
    assert by_ticker["AAPL"]["dollar_amount"] == 20_000
    assert by_ticker["AAPL"]["shares"] == 200
    assert by_ticker["AAPL"]["rationale"] == "Fallback: daily hold (no actionable signal change)."
    assert by_ticker["TSLA"]["action"] == "HOLD"
    assert by_ticker["MSFT"]["action"] == "EXITED"
    assert by_ticker["MSFT"]["dollar_amount"] == 0
    assert by_ticker["MSFT"]["allocation_pct"] == 0
    new = [r for r in rows if r["action"] == "NEW"]
    assert [r["ticker"] for r in new] == ["AMD", "NVDA"]
    # free cash = 100k − (20k AAPL + 10k TSLA) = 70k, split two ways
    assert all(r["dollar_amount"] == 35_000 for r in new)
    assert by_ticker["AMD"]["entry_price"] == 50.0
    assert "INTC" not in by_ticker and "PFE" not in by_ticker

    _fake_llm(monkeypatch, raises=RuntimeError("llm down"))
    result = _run(cands, previous, cadence="daily")
    actions = {a["ticker"]: a["action"] for a in result["allocations"]}
    assert actions == {
        "AAPL": "HOLD", "TSLA": "HOLD", "MSFT": "EXITED", "AMD": "NEW", "NVDA": "NEW",
    }
    by_ticker = {a["ticker"]: a for a in result["allocations"]}
    assert by_ticker["AAPL"]["shares"] == 200
    assert by_ticker["TSLA"]["entry_price"] == 250.0
    # run() passes aggressiveness 5 limits: 60k free after the 10% cash
    # buffer, split two ways (30k) and capped at the 12% max position (12k).
    assert by_ticker["AMD"]["shares"] == 240
    assert by_ticker["NVDA"]["shares"] == 120


def test_daily_fallback_skips_new_when_no_free_cash():
    previous = [{"ticker": "AAPL", "action": "HOLD", "allocation_pct": 100.0,
                 "dollar_amount": 100_000, "entry_price": 100.0}]
    cands = [{"ticker": "AMD", "signal": "Buy", "conviction": 10, "entry_price": 50.0}]
    rows = spy_allocator._fallback_daily(cands, previous, 100_000.0)
    assert [(r["ticker"], r["action"]) for r in rows] == [("AAPL", "HOLD")]


def test_daily_fallback_caps_new_position_and_keeps_min_cash(monkeypatch):
    previous = [{"ticker": "AAA", "action": "HOLD", "allocation_pct": 1.0,
                 "dollar_amount": 1_000, "entry_price": 100.0, "shares": 10,
                 "cost_basis": 1_000.0}]
    cands = [{"ticker": "BBB", "signal": "Buy", "conviction": 9, "entry_price": 100.0}]
    _fake_llm(monkeypatch, raises=RuntimeError("rate limited"))
    result = _run(cands, previous, cadence="daily", aggressiveness=2)
    by_ticker = {a["ticker"]: a for a in result["allocations"]}
    assert by_ticker["AAA"]["action"] == "HOLD"
    assert by_ticker["BBB"]["action"] == "NEW"
    assert by_ticker["BBB"]["cost_basis"] <= 7_000
    assert by_ticker["BBB"]["shares"] == 70
    assert result["cash"] >= 20_000


def test_daily_fallback_min_cash_limits_new_buys():
    previous = [{"ticker": "AAPL", "action": "HOLD", "allocation_pct": 85.0,
                 "dollar_amount": 85_000, "entry_price": 100.0}]
    cands = [
        {"ticker": "AMD", "signal": "Buy", "conviction": 10, "entry_price": 50.0},
        {"ticker": "NVDA", "signal": "Buy", "conviction": 9, "entry_price": 100.0},
    ]
    rows = spy_allocator._fallback_daily(
        cands, previous, 100_000.0, max_pos=12_000.0, min_cash_pct=10,
    )
    new = [r for r in rows if r["action"] == "NEW"]
    # 90k deployable − 85k held = 5k, split two ways
    assert [r["dollar_amount"] for r in new] == [2_500, 2_500]

    rows = spy_allocator._fallback_daily(
        cands, previous, 100_000.0, max_pos=12_000.0, min_cash_pct=20,
    )
    assert [r["action"] for r in rows] == ["HOLD"]


def test_daily_empty_candidates_keeps_holdings(monkeypatch):
    calls = _fake_llm(monkeypatch, content="not json")
    result = _run([], PREVIOUS, cadence="daily")
    assert result["report_md"] != "No candidates provided."
    held = {a["ticker"]: a for a in result["allocations"]}
    assert set(held) == {"AAPL", "MSFT"}
    assert all(a["action"] == "HOLD" for a in held.values())
    assert held["AAPL"]["shares"] == 100
    user_msg = calls[0][1]["content"]
    assert user_msg.count("Not re-rated in today's research") == 2

    weekly = _run([], PREVIOUS)
    assert weekly["allocations"] == []
    assert weekly["report_md"] == "No candidates provided."
    fresh_daily = _run([], None, cadence="daily")
    assert fresh_daily["allocations"] == []


def test_invalid_cadence_raises(monkeypatch):
    _fake_llm(monkeypatch)
    cands = [{"ticker": "AAPL", "signal": "buy", "conviction": 8, "entry_price": 110.0}]
    with pytest.raises(ValueError):
        _run(cands, PREVIOUS, cadence="monthly")
    with pytest.raises(ValueError):
        _run([], None, cadence="hourly")


# ─── Daily HOLD keeps its exact size ──────────────────────────────────────────

SIZED_PREVIOUS = [
    {"ticker": "AAPL", "action": "HOLD", "allocation_pct": 10.0, "dollar_amount": 10_000,
     "entry_price": 100.0, "shares": 100, "cost_basis": 10_000.0,
     "current_price": 120.0, "current_value": 12_000.0},
]
HOLD_AT_MARKET = json.dumps([
    {"ticker": "AAPL", "action": "HOLD", "allocation_pct": 12.0, "dollar_amount": 12_000,
     "entry_price": 120.0, "rationale": "keep"},
])


def test_daily_hold_keeps_previous_share_count(monkeypatch):
    cands = [{"ticker": "AAPL", "signal": "Buy", "conviction": 7, "entry_price": 120.0}]
    _fake_llm(monkeypatch, content=HOLD_AT_MARKET)
    result = _run(cands, SIZED_PREVIOUS, cadence="daily")
    (row,) = result["allocations"]
    assert row["action"] == "HOLD"
    assert row["shares"] == 100
    assert row["entry_price"] == 100.0
    assert row["cost_basis"] == 10_000.0
    assert row["dollar_amount"] == 10_000.0
    assert row["allocation_pct"] == 10.0
    assert result["total"] == 10_000.0


def test_weekly_hold_still_derives_shares_from_dollar_amount(monkeypatch):
    cands = [{"ticker": "AAPL", "signal": "Buy", "conviction": 7, "entry_price": 120.0}]
    _fake_llm(monkeypatch, content=HOLD_AT_MARKET)
    result = _run(cands, SIZED_PREVIOUS, cadence="weekly")
    (row,) = result["allocations"]
    assert row["shares"] == 120
    assert row["cost_basis"] == 12_000.0


def test_daily_message_shows_holding_size_weekly_does_not():
    cands = [{"ticker": "AAPL", "signal": "Buy", "conviction": 7, "entry_price": 120.0}]
    daily = spy_allocator.build_rebalance_user_message(
        cands, SIZED_PREVIOUS, "2026-09-25", 100_000, cadence="daily"
    )
    weekly = spy_allocator.build_rebalance_user_message(
        cands, SIZED_PREVIOUS, "2026-09-25", 100_000, cadence="weekly"
    )
    assert (
        "AAPL | signal: BUY | conviction: 7/10 | entry_price: $100.00 | "
        "held: 100 shares, cost basis $10,000.00, current_price $120.00, "
        "current_value $12,000.00 | "
    ) in daily
    assert "held:" not in weekly
    assert "current_value" not in weekly


# ── Daily-cadence accounting (close-out review findings) ─────────────────────

def test_daily_exit_realizes_pnl_at_live_mark(monkeypatch):
    """A daily EXITED holding is closed at today's quote, not at cost."""
    cands = [
        {"ticker": "AAPL", "signal": "Underweight", "conviction": 7, "entry_price": 130.0},
        {"ticker": "MSFT", "signal": "Hold", "conviction": 6, "entry_price": 200.0},
    ]
    llm = json.dumps([
        {"ticker": "AAPL", "action": "EXITED", "dollar_amount": 0, "entry_price": 130.0},
        {"ticker": "MSFT", "action": "HOLD", "dollar_amount": 10_000, "entry_price": 200.0},
        # the model trying to author system-owned exit fields must not stick
        {"ticker": "ZZZ", "action": "NEW", "dollar_amount": 0, "entry_price": 1.0,
         "exit_proceeds": 1e9},
    ])
    _fake_llm(monkeypatch, content=llm)
    result = _run(cands, PREVIOUS, cadence="daily")
    rows = {r["ticker"]: r for r in result["allocations"]}
    aapl = rows["AAPL"]
    assert aapl["action"] == "EXITED"
    assert aapl["exit_reason"] == "allocator"
    assert aapl["shares"] == 100
    assert aapl["cost_basis"] == 10_000.0
    assert aapl["exit_price"] == 130.0
    assert aapl["exit_proceeds"] == 13_000.0
    # cash = capital - live cost (MSFT 10k) + realized (3k)
    assert result["cash"] == pytest.approx(100_000 - 10_000 + 3_000)


def test_daily_exit_without_live_quote_uses_previous_mark(monkeypatch):
    prev = [dict(PREVIOUS[0], current_price=90.0), PREVIOUS[1]]
    _fake_llm(monkeypatch, content=json.dumps([{"ticker": "AAPL", "action": "EXITED"}]))
    result = _run([], prev, cadence="daily")
    aapl = next(r for r in result["allocations"] if r["ticker"] == "AAPL")
    assert aapl["exit_price"] == 90.0
    assert aapl["exit_proceeds"] == 9_000.0


def test_daily_omitted_holding_is_kept_as_hold(monkeypatch):
    """A holding the model leaves out is held, never silently liquidated at cost."""
    cands = [{"ticker": "AAPL", "signal": "Buy", "conviction": 7, "entry_price": 120.0}]
    _fake_llm(monkeypatch, content=json.dumps([{"ticker": "AAPL", "action": "HOLD"}]))
    result = _run(cands, PREVIOUS, cadence="daily")
    rows = {r["ticker"]: r for r in result["allocations"]}
    assert rows["MSFT"]["action"] == "HOLD"
    assert rows["MSFT"]["shares"] == 50
    assert rows["MSFT"]["cost_basis"] == 10_000.0
    assert "OLD" not in rows  # previously exited rows are not resurrected


@pytest.mark.parametrize("action", ["ADDED", "TRIMMED", "NEW"])
def test_daily_resize_of_holding_becomes_hold(monkeypatch, action):
    """Daily re-sizes would price new shares at the carried old cost; they hold instead."""
    cands = [{"ticker": "AAPL", "signal": "Buy", "conviction": 9, "entry_price": 160.0}]
    llm = json.dumps([{"ticker": "AAPL", "action": action, "dollar_amount": 16_000,
                       "entry_price": 160.0}])
    _fake_llm(monkeypatch, content=llm)
    result = _run(cands, PREVIOUS, cadence="daily")
    aapl = next(r for r in result["allocations"] if r["ticker"] == "AAPL")
    assert aapl["action"] == "HOLD"
    assert aapl["shares"] == 100
    assert aapl["entry_price"] == 100.0
    assert aapl["cost_basis"] == 10_000.0


def test_weekly_resize_behaviour_unchanged(monkeypatch):
    cands = [{"ticker": "AAPL", "signal": "Buy", "conviction": 9, "entry_price": 160.0}]
    llm = json.dumps([{"ticker": "AAPL", "action": "ADDED", "dollar_amount": 16_000,
                       "entry_price": 160.0}])
    _fake_llm(monkeypatch, content=llm)
    result = _run(cands, PREVIOUS, cadence="weekly")
    (aapl,) = [r for r in result["allocations"] if r["ticker"] == "AAPL"]
    assert aapl["action"] == "ADDED"
    assert "exit_proceeds" not in aapl


def test_daily_exit_gain_survives_the_price_refresh(monkeypatch):
    """End to end: book-value capital + allocator exit + refresh keeps the gain."""
    from web import account_policy, spy_scanner

    cands = [
        {"ticker": "AAPL", "signal": "Sell", "conviction": 7, "entry_price": 130.0},
        {"ticker": "MSFT", "signal": "Hold", "conviction": 6, "entry_price": 200.0},
    ]
    _fake_llm(monkeypatch, content=json.dumps([
        {"ticker": "AAPL", "action": "EXITED"},
        {"ticker": "MSFT", "action": "HOLD"},
    ]))
    # Book value of PREVIOUS: $80k cash + $20k cost basis.
    result = _run(cands, PREVIOUS, cadence="daily")
    market = spy_scanner.apply_stops_and_value(
        result["allocations"], basis=100_000.0,
        policy=account_policy.StopPolicy(), prices={"MSFT": 200.0},
    )
    assert market["realized"] == pytest.approx(3_000.0)
    assert market["positions_value"] + market["cash"] == pytest.approx(103_000.0)
