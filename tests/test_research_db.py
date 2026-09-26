import pytest

import web.db as db

pytestmark = pytest.mark.unit


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


def test_create_spy_scan_round_trips_research_scan_id(tmp_db):
    td = "2026-07-30"
    sid = db.create_spy_scan(td, kind="equity", research_scan_id=7)

    got = db.get_spy_scan(sid)
    assert got["research_scan_id"] == 7

    listed = db.list_spy_scans()
    assert listed[0]["research_scan_id"] == 7


def test_update_spy_scan_allows_research_scan_id(tmp_db):
    td = "2026-07-30"
    sid = db.create_spy_scan(td, kind="equity")
    db.update_spy_scan(sid, research_scan_id=9)

    assert db.get_spy_scan_status(sid)["research_scan_id"] == 9


def test_latest_research_scan(tmp_db):
    td = "2026-07-30"
    assert db.latest_research_scan(td) is None

    older = db.create_spy_scan(td, kind="research", status="failed")
    db.fail_spy_scan(older, "boom")
    newer = db.create_spy_scan(td, kind="research", status="running_deep")

    got = db.latest_research_scan(td)
    assert got["id"] == newer

    # completed_only returns the older completed row even when a newer
    # non-completed row exists.
    rid = db.create_spy_scan(td, kind="research", status="running")
    db.complete_spy_scan(rid, "report", [])
    newest = db.create_spy_scan(td, kind="research", status="running")
    assert db.latest_research_scan(td)["id"] == newest
    assert db.latest_research_scan(td, completed_only=True)["id"] == rid

    # Other dates and other kinds are ignored.
    db.create_spy_scan("2026-07-29", kind="research", status="completed")
    db.complete_spy_scan(db.latest_spy_scan(kind="research")["id"], "report", [])
    db.create_spy_scan(td, kind="equity", status="completed")
    db.complete_spy_scan(db.latest_spy_scan(kind="equity")["id"], "report", [])
    assert db.latest_research_scan(td, completed_only=True)["id"] == rid


def test_count_research_attempts(tmp_db):
    td = "2026-07-30"
    assert db.count_research_attempts(td) == 0

    db.create_spy_scan(td, kind="research", status="failed")
    db.create_spy_scan(td, kind="research", status="completed")
    db.create_spy_scan("2026-07-29", kind="research", status="completed")

    assert db.count_research_attempts(td) == 2


def test_copy_spy_quick_results(tmp_db):
    td = "2026-07-30"
    src = db.create_spy_scan(td, kind="research")
    dst = db.create_spy_scan(td, kind="equity")

    db.upsert_spy_quick_result(
        src, "A", signal="BUY", conviction=8, reasoning="good", analysis_id=1
    )
    db.upsert_spy_quick_result(
        src, "B", signal="SELL", conviction=7, reasoning="bad", error="boom"
    )
    db.upsert_spy_quick_result(
        src, "C", signal="HOLD", conviction=5, reasoning="ok", analysis_id=2
    )

    n = db.copy_spy_quick_results(src, dst)
    assert n == 3

    for ticker in ("A", "B", "C"):
        src_row = db.get_spy_quick_result(src, ticker)
        dst_row = db.get_spy_quick_result(dst, ticker)
        assert dst_row["signal"] == src_row["signal"]
        assert dst_row["conviction"] == src_row["conviction"]
        assert dst_row["reasoning"] == src_row["reasoning"]
        assert dst_row["analysis_id"] == src_row["analysis_id"]
        assert dst_row["error"] == src_row["error"]

    # Re-copy is idempotent.
    db.copy_spy_quick_results(src, dst)
    with db.connect() as c:
        count = c.execute(
            "SELECT COUNT(*) AS n FROM spy_quick_results WHERE scan_id = ?",
            (dst,),
        ).fetchone()["n"]
    assert count == 3


def test_list_deep_dived_results(tmp_db):
    td = "2026-07-30"
    scan = db.create_spy_scan(td, kind="research")

    # A: completed analysis with a clean quick row.
    aid_a = db.create_analysis({"ticker": "A", "trade_date": td})
    db.complete_analysis(aid_a, {"final_trade_decision": "Rating: Buy"}, "Buy")
    db.upsert_spy_quick_result(
        scan, "A", signal="BUY", conviction=9, reasoning="good", analysis_id=aid_a
    )

    # B: failed analysis.
    aid_b = db.create_analysis({"ticker": "B", "trade_date": td})
    db.fail_analysis(aid_b, "boom")
    db.upsert_spy_quick_result(
        scan, "B", signal="SELL", conviction=8, reasoning="bad", analysis_id=aid_b
    )

    # C: completed analysis but the quick row itself has an error.
    aid_c = db.create_analysis({"ticker": "C", "trade_date": td})
    db.complete_analysis(aid_c, {"final_trade_decision": "Rating: Sell"}, "Sell")
    db.upsert_spy_quick_result(
        scan, "C", signal="SELL", conviction=7, reasoning="err",
        analysis_id=aid_c, error="llm error",
    )

    # D: no analysis_id at all.
    db.upsert_spy_quick_result(
        scan, "D", signal="HOLD", conviction=6, reasoning="none"
    )

    rows = db.list_deep_dived_results(scan)
    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "A"
    assert row["signal"] == "BUY"
    assert row["conviction"] == 9
    assert row["reasoning"] == "good"
    assert row["analysis_id"] == aid_a
    assert row["final_decision"] == "Rating: Buy"
