"""Unit tests for stop-loss / weekly-rebalance EXITED row carryover and the
``spy_allocator.run()`` rebalance seam.

Most tests exercise the allocator's canonical live/stopped filters and the
fallback rebalance path without hitting the database. Additional tests mock
the LLM (via ``monkeypatch.setattr(spy_allocator, \"llm_for\", ...)``) and call
``run()`` directly to prove that the ``carry_forward_state`` call site inside
``run()`` is load-bearing, that ``peak_price`` does not silently reseed to
``entry_price`` on rebalance, and that ``build_rebalance_user_message``'s
output is exactly what is sent to the LLM.
"""

import json
from unittest.mock import MagicMock

import pytest

from web import spy_allocator

pytestmark = pytest.mark.unit


def _mock_llm_for_run(monkeypatch, payload):
    """Patch ``spy_allocator.llm_for`` so ``run()`` gets a mocked LLM.

    ``payload`` is either a JSON-serialisable list of allocation dicts or an
    exception to raise from ``llm.invoke``.
    """
    llm = MagicMock()
    if isinstance(payload, Exception):
        llm.invoke.side_effect = payload
    else:
        llm.invoke.return_value = MagicMock(content=json.dumps(payload))
    captured: dict = {}

    def _fake_llm_for(*args, **kwargs):
        captured.update(kwargs)
        return llm

    monkeypatch.setattr(spy_allocator, "llm_for", _fake_llm_for)
    llm.call_kwargs = captured
    return llm


def test_live_positions_filters_exited_and_preserves_order():
    portfolio = [
        {"ticker": "AAPL", "action": "HOLD", "entry_price": 100.0},
        {"ticker": "MSFT", "action": "EXITED", "entry_price": 200.0},
        {"ticker": "GOOGL", "action": "NEW", "entry_price": 300.0},
        {"ticker": "AMZN", "action": "EXITED", "entry_price": 400.0},
    ]
    live = spy_allocator.live_positions(portfolio)
    assert live == [
        {"ticker": "AAPL", "action": "HOLD", "entry_price": 100.0},
        {"ticker": "GOOGL", "action": "NEW", "entry_price": 300.0},
    ]
    assert spy_allocator.live_positions(None) == []
    assert spy_allocator.live_positions([]) == []


def test_stopped_positions_selects_only_stop_reasons():
    portfolio = [
        {"ticker": "AAPL", "action": "EXITED", "exit_reason": "stop_loss"},
        {"ticker": "MSFT", "action": "EXITED", "exit_reason": "trail_stop"},
        {"ticker": "GOOGL", "action": "EXITED", "exit_reason": "stop_limit"},
        {"ticker": "AMZN", "action": "EXITED"},  # daily allocation exit — no reason
        {"ticker": "META", "action": "HOLD"},  # still live
    ]
    stopped = spy_allocator.stopped_positions(portfolio)
    assert [p["ticker"] for p in stopped] == ["AAPL", "MSFT", "GOOGL"]


def test_fallback_rebalance_stopped_out_then_recandidated_is_new():
    previous = [
        {
            "ticker": "AAPL",
            "action": "EXITED",
            "exit_reason": "stop_loss",
            "entry_price": 100.0,
            "exit_price": 90.0,
            "dollar_amount": 0,
        },
    ]
    candidates = [
        {"ticker": "AAPL", "entry_price": 95.0, "signal": "BUY", "conviction": 8},
    ]
    result = spy_allocator._fallback_rebalance(candidates, previous, 100_000.0)

    aapl_rows = [r for r in result if r["ticker"] == "AAPL"]
    assert len(aapl_rows) == 1
    assert aapl_rows[0]["action"] == "NEW"
    assert aapl_rows[0]["entry_price"] == 95.0


def test_fallback_rebalance_already_exited_rows_never_reach_exit_marking_loop():
    """Previously-EXITED rows are stripped by ``live_positions()`` before the
    fallback rebalance's exit-marking loop is ever reached. This test guards
    that upstream filtering behavior — the loop never sees TSLA — rather than
    the exit-marking logic itself.
    """
    previous = [
        {"ticker": "MSFT", "action": "HOLD", "entry_price": 200.0, "dollar_amount": 5000},
        {"ticker": "TSLA", "action": "EXITED", "entry_price": 300.0, "dollar_amount": 0},
    ]
    candidates = [
        {"ticker": "MSFT", "entry_price": 220.0, "signal": "BUY", "conviction": 7},
    ]
    result = spy_allocator._fallback_rebalance(candidates, previous, 100_000.0)

    msft_rows = [r for r in result if r["ticker"] == "MSFT"]
    tsla_rows = [r for r in result if r["ticker"] == "TSLA"]
    assert len(msft_rows) == 1
    assert msft_rows[0]["action"] == "HOLD"
    assert tsla_rows == []


def test_fallback_rebalance_emits_exited_for_live_position_dropped_from_candidates():
    """A still-LIVE previous position whose ticker is absent from the new
    candidate list must produce a fresh EXITED row in the fallback result.
    """
    previous = [
        {"ticker": "AAPL", "action": "HOLD", "entry_price": 100.0, "dollar_amount": 5000},
    ]
    candidates = [
        {"ticker": "MSFT", "entry_price": 200.0, "signal": "BUY", "conviction": 8},
    ]
    result = spy_allocator._fallback_rebalance(candidates, previous, 100_000.0)

    aapl_rows = [r for r in result if r["ticker"] == "AAPL"]
    assert len(aapl_rows) == 1
    assert aapl_rows[0]["action"] == "EXITED"
    assert aapl_rows[0]["allocation_pct"] == 0
    assert aapl_rows[0]["dollar_amount"] == 0
    assert aapl_rows[0]["entry_price"] == 100.0


def test_fallback_rebalance_emits_exited_for_live_position_present_but_not_active():
    """A still-LIVE previous position whose ticker is in the candidate list but
    not among the allocated BUY/top-20 positions must also produce a fresh
    EXITED row.
    """
    previous = [
        {"ticker": "AAPL", "action": "HOLD", "entry_price": 150.0, "dollar_amount": 5000},
    ]
    candidates = [
        {"ticker": "AAPL", "entry_price": 160.0, "signal": "HOLD", "conviction": 5},
        {"ticker": "MSFT", "entry_price": 200.0, "signal": "BUY", "conviction": 8},
    ]
    result = spy_allocator._fallback_rebalance(candidates, previous, 100_000.0)

    aapl_rows = [r for r in result if r["ticker"] == "AAPL"]
    assert len(aapl_rows) == 1
    assert aapl_rows[0]["action"] == "EXITED"
    assert aapl_rows[0]["allocation_pct"] == 0
    assert aapl_rows[0]["dollar_amount"] == 0
    assert aapl_rows[0]["entry_price"] == 150.0

    msft_rows = [r for r in result if r["ticker"] == "MSFT"]
    assert len(msft_rows) == 1
    assert msft_rows[0]["action"] == "NEW"


def test_fallback_rebalance_live_position_still_hold_at_original_entry():
    previous = [
        {"ticker": "AAPL", "action": "HOLD", "entry_price": 100.0, "dollar_amount": 5000},
    ]
    candidates = [
        {"ticker": "AAPL", "entry_price": 110.0, "signal": "BUY", "conviction": 8},
    ]
    result = spy_allocator._fallback_rebalance(candidates, previous, 100_000.0)

    aapl_rows = [r for r in result if r["ticker"] == "AAPL"]
    assert len(aapl_rows) == 1
    assert aapl_rows[0]["action"] == "HOLD"
    assert aapl_rows[0]["entry_price"] == 100.0


def test_carry_forward_state_keeps_peak_price_for_hold():
    previous = [
        {"ticker": "MSFT", "action": "HOLD", "entry_price": 200.0, "peak_price": 260.0},
    ]
    allocations = [
        {
            "ticker": "MSFT",
            "action": "HOLD",
            "allocation_pct": 5.0,
            "dollar_amount": 5000.0,
            "entry_price": 0.0,
            "rationale": "keep",
        },
    ]
    spy_allocator.carry_forward_state(allocations, previous)
    assert allocations[0]["peak_price"] == 260.0
    assert allocations[0]["entry_price"] == 200.0


def test_carry_forward_state_new_allocation_gets_no_peak():
    previous = [
        {"ticker": "AAPL", "action": "HOLD", "entry_price": 150.0, "peak_price": 170.0},
    ]
    allocations = [{"ticker": "AAPL", "action": "NEW", "entry_price": 160.0}]
    spy_allocator.carry_forward_state(allocations, previous)
    assert "peak_price" not in allocations[0]
    assert allocations[0]["entry_price"] == 160.0


def test_carry_forward_state_exited_previous_row_ignored():
    previous = [
        {"ticker": "TSLA", "action": "EXITED", "entry_price": 300.0, "peak_price": 350.0},
    ]
    allocations = [{"ticker": "TSLA", "action": "HOLD", "entry_price": 310.0}]
    spy_allocator.carry_forward_state(allocations, previous)
    assert "peak_price" not in allocations[0]
    assert allocations[0]["entry_price"] == 310.0


def test_carry_forward_state_carries_stop_limit_state():
    previous = [
        {
            "ticker": "NVDA",
            "action": "HOLD",
            "entry_price": 100.0,
            "peak_price": 120.0,
            "pending_stop_limit": True,
            "stop_limit_price": 95.0,
        },
    ]
    allocations = [{"ticker": "NVDA", "action": "HOLD", "entry_price": 0.0}]
    spy_allocator.carry_forward_state(allocations, previous)
    assert allocations[0]["pending_stop_limit"] is True
    assert allocations[0]["stop_limit_price"] == 95.0
    assert allocations[0]["peak_price"] == 120.0


def test_carry_forward_state_missing_peak_price_no_key_error():
    previous = [{"ticker": "META", "action": "HOLD", "entry_price": 180.0}]
    allocations = [{"ticker": "META", "action": "HOLD", "entry_price": 0.0}]
    spy_allocator.carry_forward_state(allocations, previous)
    assert "peak_price" not in allocations[0]
    assert allocations[0]["entry_price"] == 180.0


def test_carry_forward_state_clamps_peak_below_entry():
    previous = [
        {"ticker": "AMZN", "action": "HOLD", "entry_price": 200.0, "peak_price": 190.0},
    ]
    allocations = [{"ticker": "AMZN", "action": "HOLD", "entry_price": 0.0}]
    spy_allocator.carry_forward_state(allocations, previous)
    assert allocations[0]["peak_price"] == 200.0
    assert allocations[0]["entry_price"] == 200.0


def test_carry_forward_state_entry_price_patch_for_added_and_trimmed():
    previous = [
        {"ticker": "GOOGL", "action": "HOLD", "entry_price": 100.0, "peak_price": 110.0},
    ]
    for action in ("ADDED", "TRIMMED"):
        allocations = [{"ticker": "GOOGL", "action": action, "entry_price": 0.0}]
        spy_allocator.carry_forward_state(allocations, previous)
        assert allocations[0]["entry_price"] == 100.0
        assert allocations[0]["peak_price"] == 110.0


def test_carry_forward_state_overwrites_existing_stop_state():
    """Trusted previous-row state must always win over any model-supplied value."""
    previous = [
        {
            "ticker": "X",
            "action": "HOLD",
            "entry_price": 50.0,
            "peak_price": 60.0,
            "pending_stop_limit": True,
            "stop_limit_price": 45.0,
        },
    ]
    allocations = [
        {
            "ticker": "X",
            "action": "HOLD",
            "entry_price": 55.0,
            "peak_price": 70.0,
            "pending_stop_limit": False,
            "stop_limit_price": 40.0,
        },
    ]
    spy_allocator.carry_forward_state(allocations, previous)
    assert allocations[0]["entry_price"] == 50.0
    assert allocations[0]["peak_price"] == 60.0
    assert allocations[0]["pending_stop_limit"] is True
    assert allocations[0]["stop_limit_price"] == 45.0


def test_carry_forward_state_none_values_not_copied():
    previous = [
        {"ticker": "Y", "action": "HOLD", "entry_price": 100.0, "peak_price": None},
    ]
    allocations = [{"ticker": "Y", "action": "HOLD", "entry_price": 0.0}]
    spy_allocator.carry_forward_state(allocations, previous)
    assert "peak_price" not in allocations[0]
    assert allocations[0]["entry_price"] == 100.0


def test_build_rebalance_message_no_stopped_section_when_none():
    candidates = [
        {
            "ticker": "AAPL",
            "signal": "BUY",
            "conviction": 8,
            "entry_price": 150.0,
            "final_decision": "Strong momentum.",
        },
    ]
    previous = [
        {"ticker": "AAPL", "action": "HOLD", "entry_price": 140.0},
    ]
    msg = spy_allocator.build_rebalance_user_message(
        candidates, previous, "2024-01-01", 100_000.0
    )
    assert "STOPPED OUT" not in msg
    assert "=== CURRENT HOLDINGS (1 positions) ===" in msg
    assert "AAPL | signal: BUY | conviction: 8/10 | entry_price: $140.00" in msg


def test_build_rebalance_message_live_position_dropped_from_candidates():
    """A still-LIVE previous position whose ticker is not in the new candidate
    list must be shown under CURRENT HOLDINGS with a SELL/conviction 0 hint.
    """
    previous = [
        {"ticker": "AAPL", "action": "HOLD", "entry_price": 140.0},
    ]
    candidates = []
    msg = spy_allocator.build_rebalance_user_message(
        candidates, previous, "2024-01-01", 100_000.0
    )
    assert "=== CURRENT HOLDINGS (1 positions) ===" in msg
    assert (
        "AAPL | signal: SELL | conviction: 0/10 | entry_price: $140.00 | "
        "No longer in top candidates — consider exiting."
    ) in msg
    assert "STOPPED OUT" not in msg
    assert "NEW CANDIDATES" not in msg


def test_build_rebalance_message_shows_stopped_row():
    candidates = [
        {
            "ticker": "NVDA",
            "signal": "BUY",
            "conviction": 9,
            "entry_price": 700.0,
            "final_decision": "Great setup.",
        },
    ]
    previous = [
        {
            "ticker": "AAPL",
            "action": "EXITED",
            "exit_reason": "trail_stop",
            "entry_price": 100.0,
            "exit_price": 117.5,
        },
    ]
    msg = spy_allocator.build_rebalance_user_message(
        candidates, previous, "2024-01-02", 95_000.0
    )
    assert "=== STOPPED OUT SINCE LAST REBALANCE (1 positions" in msg
    assert "AAPL | closed: trail_stop | entry_price: $100.00 | exit_price: $117.50" in msg


def test_build_rebalance_message_weekly_exited_not_in_stopped_section():
    previous = [
        {"ticker": "TSLA", "action": "EXITED", "entry_price": 200.0, "exit_price": 180.0},
    ]
    msg = spy_allocator.build_rebalance_user_message(
        [], previous, "2024-01-03", 90_000.0
    )
    assert "STOPPED OUT" not in msg
    assert "TSLA" not in msg


def test_build_rebalance_message_stopped_not_in_current_holdings():
    previous = [
        {
            "ticker": "AMD",
            "action": "EXITED",
            "exit_reason": "stop_loss",
            "entry_price": 150.0,
            "exit_price": 140.0,
        },
    ]
    msg = spy_allocator.build_rebalance_user_message(
        [], previous, "2024-01-04", 100_000.0
    )
    holdings_part, _, stopped_part = msg.partition("=== STOPPED OUT")
    assert "AMD" not in holdings_part
    assert "AMD" in stopped_part


def test_build_rebalance_message_missing_exit_price_formats_zero():
    previous = [
        {
            "ticker": "META",
            "action": "EXITED",
            "exit_reason": "stop_limit",
            "entry_price": 300.0,
        },
    ]
    msg = spy_allocator.build_rebalance_user_message(
        [], previous, "2024-01-05", 100_000.0
    )
    assert "META | closed: stop_limit | entry_price: $300.00 | exit_price: $0.00" in msg


def test_build_rebalance_message_missing_entry_price_formats_zero():
    previous = [
        {"ticker": "XOM", "action": "EXITED", "exit_reason": "stop_loss", "exit_price": 50.0},
    ]
    msg = spy_allocator.build_rebalance_user_message(
        [], previous, "2024-01-06", 100_000.0
    )
    assert "XOM | closed: stop_loss | entry_price: $0.00 | exit_price: $50.00" in msg


# ── run()-level coverage: these hit the LLM call seam with a mocked LLM ──────

def test_run_rebalance_carries_forward_state_and_sizes_with_carried_entry(monkeypatch):
    """If carry_forward_state were deleted or moved below the sizing loop, this
    test fails: the LLM's bogus entry_price would drive ``shares``/``cost_basis``.
    """
    previous = [
        {
            "ticker": "AAPL",
            "action": "HOLD",
            "entry_price": 100.0,
            "peak_price": 150.0,
            "current_price": 140.0,
            "dollar_amount": 5000.0,
            "shares": 50,
            "cost_basis": 5000.0,
        },
    ]
    candidates = [
        {
            "ticker": "AAPL",
            "signal": "HOLD",
            "conviction": 6,
            "entry_price": 150.0,
            "final_decision": "Steady uptrend.",
        },
    ]
    # The LLM fabricates a false cost basis and omits peak_price. The allocator
    # must ignore both and carry the trusted previous-row state forward.
    llm_output = [
        {
            "ticker": "AAPL",
            "action": "HOLD",
            "allocation_pct": 5.0,
            "dollar_amount": 5000.0,
            "entry_price": 999.0,
            "rationale": "hold",
        },
    ]
    _mock_llm_for_run(monkeypatch, llm_output)

    result = spy_allocator.run(
        candidates,
        "2024-01-08",
        {},
        previous_portfolio=previous,
        starting_value=100_000.0,
    )
    by_ticker = {a["ticker"]: a for a in result["allocations"]}
    aapl = by_ticker["AAPL"]

    # (a) entry_price is carried from the previous portfolio, not the LLM.
    assert aapl["entry_price"] == 100.0
    # (b) peak_price is carried and not reseeded to the (carried) entry_price.
    assert aapl["peak_price"] == 150.0
    # (c) sizing used the carried entry_price: $5000 / $100 = 50 whole shares.
    assert aapl["shares"] == 50
    assert aapl["cost_basis"] == 5000.0


def test_run_rebalance_peak_price_does_not_reseed_to_entry_price(monkeypatch):
    """Direct seam-level guard for the regression: peak_price == entry_price
    after a rebalance when the account had rallied.
    """
    previous = [
        {
            "ticker": "AAPL",
            "action": "HOLD",
            "entry_price": 100.0,
            "peak_price": 150.0,
            "current_price": 140.0,
        },
    ]
    candidates = [
        {
            "ticker": "AAPL",
            "signal": "HOLD",
            "conviction": 7,
            "entry_price": 140.0,
            "final_decision": "Keep holding.",
        },
    ]
    # LLM supplies a stale peak_price that would reseed to entry if the
    # carry-forward logic were absent or broken.
    llm_output = [
        {
            "ticker": "AAPL",
            "action": "HOLD",
            "allocation_pct": 5.0,
            "dollar_amount": 5000.0,
            "entry_price": 140.0,
            "peak_price": 100.0,
            "rationale": "hold",
        },
    ]
    _mock_llm_for_run(monkeypatch, llm_output)

    result = spy_allocator.run(
        candidates,
        "2024-01-08",
        {},
        previous_portfolio=previous,
        starting_value=100_000.0,
    )
    aapl = next(a for a in result["allocations"] if a["ticker"] == "AAPL")

    assert aapl["entry_price"] == 100.0
    assert aapl["peak_price"] == 150.0
    assert aapl["peak_price"] != aapl["entry_price"]


def test_run_rebalance_sends_build_rebalance_user_message_to_llm(monkeypatch):
    previous = [
        {
            "ticker": "AAPL",
            "action": "HOLD",
            "entry_price": 100.0,
            "peak_price": 150.0,
            "current_price": 140.0,
        },
    ]
    candidates = [
        {
            "ticker": "AAPL",
            "signal": "HOLD",
            "conviction": 7,
            "entry_price": 140.0,
            "final_decision": "Keep holding.",
        },
    ]
    llm_output = [
        {
            "ticker": "AAPL",
            "action": "HOLD",
            "allocation_pct": 5.0,
            "dollar_amount": 5000.0,
            "entry_price": 140.0,
            "rationale": "hold",
        },
    ]
    llm = _mock_llm_for_run(monkeypatch, llm_output)

    spy_allocator.run(
        candidates,
        "2024-01-08",
        {},
        previous_portfolio=previous,
        starting_value=100_000.0,
    )

    messages = llm.invoke.call_args[0][0]
    user_content = next(m["content"] for m in messages if m["role"] == "user")
    expected = spy_allocator.build_rebalance_user_message(
        candidates, previous, "2024-01-08", 100_000.0
    )
    assert user_content == expected
    assert "=== CURRENT HOLDINGS (1 positions) ===" in user_content
    assert "AAPL | signal: HOLD | conviction: 7/10 | entry_price: $100.00" in user_content


def test_run_fresh_portfolio_returns_expected_shape_and_report(monkeypatch):
    candidates = [
        {
            "ticker": "AAPL",
            "signal": "BUY",
            "conviction": 8,
            "entry_price": 150.0,
            "final_decision": "Breakout.",
        },
    ]
    llm_output = [
        {
            "ticker": "AAPL",
            "action": "NEW",
            "allocation_pct": 10.0,
            "dollar_amount": 10_000.0,
            "entry_price": 150.0,
            "rationale": "buy",
        },
    ]
    _mock_llm_for_run(monkeypatch, llm_output)

    result = spy_allocator.run(
        candidates,
        "2024-01-08",
        {},
        previous_portfolio=None,
    )

    assert set(result.keys()) == {"allocations", "total", "cash", "report_md", "starting_value"}
    assert result["starting_value"] == 100_000.0
    assert result["report_md"].startswith("# S&P 500 Paper Portfolio — 2024-01-08 (Initial Portfolio)")
    assert "AAPL" in {a["ticker"] for a in result["allocations"]}


def test_run_fresh_falls_back_when_llm_raises(monkeypatch):
    candidates = [
        {
            "ticker": "AAPL",
            "signal": "BUY",
            "conviction": 8,
            "entry_price": 150.0,
            "final_decision": "Breakout.",
        },
    ]
    fallback_payload = spy_allocator._fallback_fresh(candidates)

    # First, run with the fallback payload coming back through the LLM path so
    # we know exactly what a successful run producing that payload looks like.
    _mock_llm_for_run(monkeypatch, fallback_payload)
    expected = spy_allocator.run(candidates, "2024-01-08", {}, previous_portfolio=None)

    # Now force the LLM to raise. run() must take the deterministic fallback
    # path and return an identical result.
    _mock_llm_for_run(monkeypatch, RuntimeError("LLM is unavailable"))
    result = spy_allocator.run(candidates, "2024-01-08", {}, previous_portfolio=None)

    assert result == expected
    assert result["allocations"]  # non-empty, valid portfolio


def test_run_rebalance_falls_back_when_llm_raises(monkeypatch):
    previous = [
        {
            "ticker": "AAPL",
            "action": "HOLD",
            "entry_price": 100.0,
            "peak_price": 150.0,
            "current_price": 140.0,
        },
    ]
    candidates = [
        {
            "ticker": "AAPL",
            "signal": "BUY",
            "conviction": 8,
            "entry_price": 140.0,
            "final_decision": "Still strong.",
        },
    ]
    fallback_payload = spy_allocator._fallback_rebalance(
        candidates, previous, 100_000.0
    )

    _mock_llm_for_run(monkeypatch, fallback_payload)
    expected = spy_allocator.run(
        candidates,
        "2024-01-08",
        {},
        previous_portfolio=previous,
        starting_value=100_000.0,
    )

    _mock_llm_for_run(monkeypatch, ConnectionError("LLM connection reset"))
    result = spy_allocator.run(
        candidates,
        "2024-01-08",
        {},
        previous_portfolio=previous,
        starting_value=100_000.0,
    )

    assert result == expected
    assert result["allocations"]
