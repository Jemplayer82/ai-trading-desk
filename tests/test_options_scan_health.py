"""Options-scan health guards that depend on web.options_engine (tier 4 only).

Split out of test_scan_health_guard.py, which needs spy_scanner (tier 3+) and
is itself deleted below tier 3 by the tier strip — this file additionally
needs options_engine, so it's deleted below tier 4.
"""

import pytest

from web import db, options_engine

pytestmark = pytest.mark.unit

MODEL_404 = 'Error code: 404 - {"error": {"message": "model \\"gpt-5.4-mini\\" not found"}}'


def _row(ticker, signal="BUY", conviction=8, error=None):
    row = {"ticker": ticker, "signal": signal, "conviction": conviction}
    if error:
        # Mirrors _quick_scan_one's catch-all: degraded scores + an error key.
        row.update({"signal": "HOLD", "conviction": 1, "error": error})
    return row


# ── never trade on a crashed analysis ────────────────────────────────────────

def test_failed_dives_are_not_vetted(monkeypatch, tmp_path):
    """A failed dive keeps its BUY/conviction from the quick scan, so if it
    reached fetch_candidates it would open real positions. The allocation's
    usable rows come from db.list_deep_dived_results, which must exclude
    errored rows and dives whose analysis never completed."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    td = "2026-06-09"
    rid = db.create_spy_scan(td, kind="research")

    good = db.create_analysis({"ticker": "AAPL", "trade_date": td})
    db.complete_analysis(good, {"final_trade_decision": "Rating: Buy"}, "Buy")
    db.upsert_spy_quick_result(rid, "AAPL", signal="Buy", conviction=8, analysis_id=good)

    crashed = db.create_analysis({"ticker": "MSFT", "trade_date": td})  # never completed
    db.upsert_spy_quick_result(rid, "MSFT", signal="BUY", conviction=9, analysis_id=crashed)

    errored = db.create_analysis({"ticker": "NVDA", "trade_date": td})
    db.complete_analysis(errored, {"final_trade_decision": "Rating: Buy"}, "Buy")
    db.upsert_spy_quick_result(rid, "NVDA", signal="BUY", conviction=9,
                               analysis_id=errored, error="deep model 410")

    db.upsert_spy_quick_result(rid, "TSLA", signal="BUY", conviction=9)  # no dive at all

    usable = db.list_deep_dived_results(rid)
    assert [r["ticker"] for r in usable] == ["AAPL"], \
        "crashed analyses must never be vetted into contracts"


# ── zero-candidate explanations ──────────────────────────────────────────────

def test_reason_none_when_candidates_exist():
    assert options_engine._zero_candidate_reason([], 0, [], [{"occ_symbol": "X"}]) is None


def test_reason_all_hold():
    quick = [_row(f"T{i}", signal="HOLD") for i in range(10)]
    msg = options_engine._zero_candidate_reason(quick, 0, [], [])
    assert "BUY or SELL" in msg


def test_reason_all_dives_failed_is_not_blamed_on_vetting():
    """The failure mode the feature exists to surface must not be reported as
    'nothing passed vetting'."""
    quick = [_row("AAPL")]
    msg = options_engine._zero_candidate_reason(quick, 1, [], [])
    assert "All 1 deep dives failed" in msg
    assert "vetting" not in msg.lower()


def test_reason_vetting_when_dives_usable():
    quick = [_row("AAPL")]
    msg = options_engine._zero_candidate_reason(quick, 1, quick, [])
    assert "vetting" in msg.lower()


def test_reason_counts_partially_failed_dives():
    quick = [_row("AAPL"), _row("MSFT"), _row("NVDA")]
    msg = options_engine._zero_candidate_reason(quick, 3, [_row("AAPL")], [])
    assert "2 further dives failed" in msg
    assert "vetting" in msg.lower()


def test_reason_all_hold_after_deep_dive_is_not_blamed_on_vetting():
    """usable rows carry the deep dive's OWN final 5-tier rating (Buy/Overweight/
    Hold/Underweight/Sell — tradingagents/agents/utils/rating.py), which
    overwrites the quick scan's BUY/SELL signal. All-Hold here is a real "no
    directional call" day, not a vetting failure — the report must say so,
    not claim contracts were vetted and rejected when none were even tried."""
    quick = [_row(f"T{i}") for i in range(5)]  # quick scan directional (BUY)
    usable = [_row(f"T{i}", signal="Hold") for i in range(5)]  # deep dive: all Hold
    msg = options_engine._zero_candidate_reason(quick, 5, usable, [])
    assert "hold" in msg.lower()
    assert "vetting" not in msg.lower()


def test_reason_vetting_when_dives_rate_overweight_underweight():
    """A real vetting failure after the fix: deep dives DO produce directional
    5-tier calls (Overweight/Underweight), but none of them survive contract
    vetting. This must still say "vetting", not "all Hold"."""
    quick = [_row("AAPL"), _row("MSFT")]
    usable = [_row("AAPL", signal="Overweight"), _row("MSFT", signal="Underweight")]
    msg = options_engine._zero_candidate_reason(quick, 2, usable, [])
    assert "vetting" in msg.lower()
    assert "hold" not in msg.lower()
