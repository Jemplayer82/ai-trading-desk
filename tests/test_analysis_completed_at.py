"""analyses.completed_at is stamped when complete_analysis runs.

created_at is written at INSERT, before the model runs, so research that asks
"did this call exist before a given market open?" needs the completion time.
Failed runs and still-running rows keep completed_at NULL, and databases
created before the column existed gain it through init_db's migration.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from web import db

pytestmark = pytest.mark.unit

PARAMS = {"ticker": "AAPL", "trade_date": "2026-09-25", "provider": "anthropic",
          "deep_model": "opus", "quick_model": "opus", "analysts": ["market"],
          "research_depth": 1, "language": "en", "config_fingerprint": "fp"}


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


def _row(path, analysis_id):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone())
    finally:
        conn.close()


def _parse(ts):
    return datetime.fromisoformat(ts.rstrip("Z"))


def test_complete_stamps_completed_at(tmp_db):
    aid = db.create_analysis(PARAMS)
    assert _row(tmp_db, aid)["completed_at"] is None
    before = datetime.utcnow().replace(microsecond=0)
    db.complete_analysis(aid, {"final_trade_decision": "Rating: Buy"}, "BUY")
    row = _row(tmp_db, aid)
    assert row["status"] == "completed"
    stamped = _parse(row["completed_at"])
    assert before <= stamped <= datetime.utcnow() + timedelta(seconds=1)
    assert stamped >= _parse(row["created_at"])
    assert row["completed_at"].endswith("Z")


def test_failed_analysis_has_no_completed_at(tmp_db):
    aid = db.create_analysis(PARAMS)
    db.fail_analysis(aid, "boom")
    assert _row(tmp_db, aid)["completed_at"] is None


def test_migration_adds_column_to_old_database(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE analyses (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
                 "ticker TEXT NOT NULL, trade_date TEXT NOT NULL, status TEXT NOT NULL)")
    conn.execute("INSERT INTO analyses (created_at, ticker, trade_date, status) "
                 "VALUES ('2026-09-01T12:00:00Z', 'MSFT', '2026-09-01', 'completed')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(analyses)")}
    assert "completed_at" in cols
    assert _row(path, 1)["completed_at"] is None  # historical rows are not back-filled
