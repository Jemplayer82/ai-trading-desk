"""Unit tests for web/options_allocator.py — hard guardrails, LLM parse/clamp,
and the deterministic fallback."""

import json
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from web import options_allocator
from web.account_policy import StopPolicy
from web.options_allocator import forced_closes, position_caps, run

pytestmark = pytest.mark.unit

TODAY = date(2026, 7, 17)
POLICY_STOP60 = StopPolicy("stop", 60.0)


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    monkeypatch.setattr(options_allocator.options_data, "today_et", lambda: TODAY)


def _exp(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def _pos(pid, occ, dte=30, entry=4.0, mark=4.0, contracts=2, **over):
    base = {
        "id": pid, "occ_symbol": occ, "underlying": occ.split()[0],
        "put_call": "CALL", "strike": 230.0, "expiration_date": _exp(dte),
        "contracts": contracts, "entry_premium": entry, "entry_bid": entry, "current_bid": mark,
        "cost_basis": round(entry * 100 * contracts, 2),
        "current_premium": mark, "current_value": round(mark * 100 * contracts, 2),
    }
    base.update(over)
    return base


def _cand(ticker, mid=5.0, conviction=8, dte=21):
    return {
        "occ_symbol": f"{ticker:<6s}260821C00100000", "ticker": ticker,
        "underlying": ticker, "put_call": "CALL", "strike": 100.0,
        "expiration_date": _exp(dte), "dte": dte, "bid": mid - 0.1,
        "ask": mid + 0.1, "mid": mid, "delta": 0.45, "open_interest": 500,
        "underlying_price": 100.0, "signal": "BUY", "conviction": conviction,
        "rationale": "test", "source": "schwab",
    }


def _mock_llm(monkeypatch, payload):
    llm = MagicMock()
    if isinstance(payload, Exception):
        llm.invoke.side_effect = payload
    else:
        llm.invoke.return_value = MagicMock(content=json.dumps(payload))
    captured: dict = {}
    def _fake_llm_for(*args, **kwargs):
        captured.update(kwargs)
        return llm
    monkeypatch.setattr(options_allocator, "llm_for", _fake_llm_for)
    llm.call_kwargs = captured
    return llm


# ── Hard guardrails ──────────────────────────────────────────────────────────

def test_forced_close_dte_floor():
    positions = [_pos(1, "AAPL  X", dte=2), _pos(2, "MSFT  X", dte=30)]
    forced = forced_closes(positions, POLICY_STOP60)
    assert [(p["id"], reason) for p, reason, fill in forced] == [(1, "dte_floor")]


def test_forced_close_stop_loss():
    positions = [
        _pos(1, "AAPL  X", entry=4.0, mark=1.55),  # -61% -> stopped
        _pos(2, "MSFT  X", entry=4.0, mark=1.70),  # -57% -> survives
    ]
    forced = forced_closes(positions, POLICY_STOP60)
    assert [(p["id"], reason) for p, reason, fill in forced] == [(1, "stop_loss")]


def test_forced_close_stop_loss_at_zero_mark():
    """A contract marked to 0.00 is a total loss and MUST stop out.

    Regression: _mark() used `v > 0`, so a real 0.00 mark was treated as "no
    quote" and fell through to entry_premium — mark == entry, so the stop-loss
    comparison could never fire on the worst possible position.
    """
    positions = [_pos(1, "AAPL  X", entry=4.0, mark=0.0)]
    forced = forced_closes(positions, POLICY_STOP60)
    assert [(p["id"], reason) for p, reason, fill in forced] == [(1, "stop_loss")]


def test_forced_closes_precede_llm(monkeypatch):
    """A guardrail close happens even if the LLM says HOLD (its decision for a
    force-closed contract is simply an unknown symbol by then)."""
    stopped = _pos(1, "AAPL  260821C00230000", entry=4.0, mark=1.0)
    _mock_llm(monkeypatch, [
        {"occ_symbol": stopped["occ_symbol"], "action": "HOLD", "rationale": "diamond hands"},
    ])
    result = run([], [stopped], "2026-07-17", {}, equity=100_000, cash=99_000,
                 policy=POLICY_STOP60)
    assert [c["exit_reason"] for c in result["closes"]] == ["stop_loss"]
    assert result["holds"] == []


def test_forced_closes_trailing_pct():
    """Trailing pct: 25% drawdown from peak closes; 15% drawdown survives."""
    peak = 20.0
    closes = [
        _pos(1, "AAPL  X", entry=10.0, mark=15.0, peak_premium=peak, dte=30),  # 25% off peak
        _pos(2, "MSFT  X", entry=10.0, mark=17.0, peak_premium=peak, dte=30),  # 15% off peak
    ]
    forced = forced_closes(closes, StopPolicy("trailing_pct", 20.0))
    assert [(p["id"], reason) for p, reason, fill in forced] == [(1, "trail_stop")]


def test_forced_closes_trailing_dollar():
    pos = _pos(1, "AAPL  X", entry=10.0, mark=17.9, peak_premium=20.0, dte=30)
    forced = forced_closes([pos], StopPolicy("trailing_dollar", 2.0))
    assert [(p["id"], reason) for p, reason, fill in forced] == [(1, "trail_stop")]


def test_forced_closes_stop_limit_filled_when_mark_at_limit():
    pos = _pos(1, "AAPL  X", entry=10.0, mark=4.5, peak=10.0, dte=30,
               stop_triggered_at="2026-07-17T10:00:00Z")
    forced = forced_closes([pos], StopPolicy("stop_limit", 60.0))
    assert [(p["id"], reason) for p, reason, fill in forced] == [(1, "stop_limit")]
    assert forced[0][2] == pytest.approx(4.5)


def test_forced_closes_stop_limit_not_filled_below_limit():
    """A stop-limit resting fill must never be chased below its limit price."""
    pos = _pos(1, "AAPL  X", entry=10.0, mark=3.9, peak=10.0, dte=30,
               stop_triggered_at="2026-07-17T10:00:00Z")
    forced = forced_closes([pos], StopPolicy("stop_limit", 60.0))
    assert forced == []


def test_forced_closes_dte_floor_overrides_none_policy():
    """DTE floor closes even when stops are disabled; a deep loss under 'none'
    is NOT force-closed."""
    positions = [
        _pos(1, "AAPL  X", entry=10.0, mark=3.0, peak=10.0, dte=3),
        _pos(2, "MSFT  X", entry=10.0, mark=3.0, peak=10.0, dte=30),
    ]
    forced = forced_closes(positions, StopPolicy("none"))
    assert [(p["id"], reason) for p, reason, fill in forced] == [(1, "dte_floor")]


def test_position_caps_tiers():
    assert position_caps(2) == (0.05, 0.15)
    assert position_caps(5) == (0.08, 0.30)
    assert position_caps(9) == (0.12, 0.50)


# ── LLM decision parsing + clamping ──────────────────────────────────────────

def test_llm_decisions_parsed_and_clamped(monkeypatch):
    held = _pos(1, "NVDA  260821C00190000", dte=30)
    ignored = _pos(2, "MSFT  260821C00420000", dte=30)
    cand = _cand("AAPL", mid=10.0, conviction=9)
    _mock_llm(monkeypatch, [
        {"occ_symbol": held["occ_symbol"], "action": "CLOSE", "rationale": "thesis done"},
        # wants 20 contracts @ $1010 ask/contract; the $8k cap permits 7.
        {"occ_symbol": cand["occ_symbol"], "action": "NEW", "contracts": 20, "rationale": "moon"},
        {"occ_symbol": "HALLU 260821C00001000", "action": "NEW", "contracts": 5},
    ])
    result = run([cand], [held, ignored], "2026-07-17", {},
                 equity=100_000, cash=60_000, aggressiveness=5,
                 policy=POLICY_STOP60)
    assert [c["exit_reason"] for c in result["closes"]] == ["llm_close"]
    # Ignored open position defaults to HOLD.
    assert [h["position_id"] for h in result["holds"]] == [2]
    assert len(result["opens"]) == 1
    assert result["opens"][0]["contracts"] == 7
    assert result["opens"][0]["cost"] == pytest.approx(7_070)


def test_allocator_uses_quick_model(monkeypatch):
    cand = _cand("AAPL")
    llm = _mock_llm(monkeypatch, [
        {"occ_symbol": cand["occ_symbol"], "action": "NEW", "contracts": 1, "rationale": "x"},
    ])
    run([cand], [], "2026-07-17", {}, equity=100_000, cash=100_000,
        policy=POLICY_STOP60)
    assert llm.call_kwargs.get("deep") is False


def test_total_premium_cap_across_opens(monkeypatch):
    # agg 5: the $30k total cap less $9k held leaves $21k.
    # At $10.10 ask, the $8k per-position cap permits 7 contracts ($7070).
    # The third conviction-ranked request is limited to 6 contracts ($6060).
    held = _pos(1, "MSFT  260821C00420000", dte=30, entry=45.0, mark=45.0, contracts=2)  # cost 9000
    cands = [_cand("AAA", mid=10.0, conviction=9), _cand("BBB", mid=10.0, conviction=8),
             _cand("CCC", mid=10.0, conviction=7)]
    _mock_llm(monkeypatch, [
        {"occ_symbol": c["occ_symbol"], "action": "NEW", "contracts": 10} for c in cands
    ])
    result = run(cands, [held], "2026-07-17", {},
                 equity=100_000, cash=91_000, aggressiveness=5,
                 policy=POLICY_STOP60)
    costs = {o["contract"]["ticker"]: o["cost"] for o in result["opens"]}
    assert costs["AAA"] == pytest.approx(7_070)   # per-position cap
    assert costs["BBB"] == pytest.approx(7_070)
    assert costs["CCC"] == pytest.approx(6_060)           # leftover budget only
    total_new = sum(o["cost"] for o in result["opens"])
    assert total_new <= 21_000 + 1e-6


def test_max_open_positions(monkeypatch):
    held = [_pos(i, f"T{i:<5d}260821C00100000", dte=30) for i in range(1, 15)]  # 14 holds
    cands = [_cand("AAA"), _cand("BBB")]
    _mock_llm(monkeypatch, [
        {"occ_symbol": c["occ_symbol"], "action": "NEW", "contracts": 1} for c in cands
    ])
    result = run(cands, held, "2026-07-17", {}, equity=1_000_000, cash=900_000,
                 policy=POLICY_STOP60)
    # 14 holds + max 15 -> only one new position fits.
    assert len(result["opens"]) == 1


def test_llm_failure_uses_fallback(monkeypatch):
    held = _pos(1, "NVDA  260821C00190000", dte=30)
    cands = [_cand("AAA", mid=5.0, conviction=9), _cand("BBB", mid=5.0, conviction=6)]
    _mock_llm(monkeypatch, RuntimeError("LLM down"))
    result = run(cands, [held], "2026-07-17", {}, equity=100_000, cash=99_000,
                 policy=POLICY_STOP60)
    # Everything held, conviction-ranked equal-dollar opens under caps.
    assert [h["position_id"] for h in result["holds"]] == [1]
    assert [o["contract"]["ticker"] for o in result["opens"]] == ["AAA", "BBB"]
    for o in result["opens"]:
        assert o["cost"] <= 0.08 * 100_000 + 1e-6


def test_llm_garbage_json_uses_fallback(monkeypatch):
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content="I think you should buy calls!")
    monkeypatch.setattr(options_allocator, "llm_for", lambda *a, **k: llm)
    result = run([_cand("AAA")], [], "2026-07-17", {}, equity=100_000, cash=100_000,
                 policy=POLICY_STOP60)
    assert len(result["opens"]) == 1  # fallback still produced a decision set
    assert "report_md" in result


def test_no_candidates_no_positions(monkeypatch):
    _mock_llm(monkeypatch, [])
    result = run([], [], "2026-07-17", {}, equity=100_000, cash=100_000,
                 policy=POLICY_STOP60)
    assert result["closes"] == [] and result["holds"] == [] and result["opens"] == []


# ── Lessons injection (learning loop) ────────────────────────────────────────

def test_lessons_context_reaches_system_prompt(monkeypatch):
    llm = _mock_llm(monkeypatch, [])
    block = "=== OPTIONS TRACK RECORD (this paper account, 12 closed) ===\n- watch for decay"
    run([_cand("NVDA")], [], "2026-07-17", {}, equity=100_000, cash=100_000,
        lessons_context=block, policy=POLICY_STOP60)
    system = llm.invoke.call_args[0][0][0]["content"]
    assert "OPTIONS TRACK RECORD" in system
    assert "watch for decay" in system


def test_empty_lessons_renders_the_pre_learning_prompt(monkeypatch):
    """With no lessons the {lessons_context} placeholder must vanish without a
    trace — the exact byte sequence the pre-learning template produced around
    it (neutral bias renders '' too, leaving one blank line) must survive."""
    llm = _mock_llm(monkeypatch, [])
    run([_cand("NVDA")], [], "2026-07-17", {}, equity=100_000, cash=100_000,
        policy=POLICY_STOP60)
    system = llm.invoke.call_args[0][0][0]["content"]
    # Pre-learning byte sequence across the placeholder site (neutral bias):
    assert "must earn its theta.\n\nYou will receive" in system
    assert "TRACK RECORD" not in system
    assert "{lessons_context}" not in system  # placeholder actually substituted

    # And a non-empty block gets clean newline separation, not concatenation.
    llm2 = _mock_llm(monkeypatch, [])
    run([_cand("NVDA")], [], "2026-07-17", {}, equity=100_000, cash=100_000,
        lessons_context="=== OPTIONS TRACK RECORD (this paper account, 12 closed) ===",
        policy=POLICY_STOP60)
    system2 = llm2.invoke.call_args[0][0][0]["content"]
    assert "theta.\n\n=== OPTIONS TRACK RECORD" in system2
    assert "===\n\nYou will receive" in system2