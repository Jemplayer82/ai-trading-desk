"""Account names are unique per tab (kind), and the legacy global-UNIQUE table
is migrated without ever reusing an account id."""
from __future__ import annotations

import sqlite3

import pytest

import web.db as db

pytestmark = pytest.mark.unit

_LEGACY = """
CREATE TABLE paper_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    starting_capital REAL NOT NULL DEFAULT 100000,
    aggressiveness INTEGER NOT NULL DEFAULT 5,
    bias TEXT NOT NULL DEFAULT 'neutral',
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'equity',
    schedule_time TEXT,
    stop_type TEXT NOT NULL DEFAULT 'none',
    stop_value REAL,
    stop_limit_offset REAL,
    stage_trigger_pct REAL,
    stage_trail_pct REAL
);
"""


def _create(name, kind):
    return db.create_paper_account(
        name=name, starting_capital=1000.0, aggressiveness=5, bias="bullish", kind=kind,
        schedule_time="09:00", stop_type="none", stop_value=None, stop_limit_offset=None,
    )


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()


@pytest.fixture()
def legacy_db(tmp_path, monkeypatch):
    """The production shape before 2026-10-05: global UNIQUE(name), accounts
    1/4/5 deleted (their ids still referenced by positions), sequence at 5."""
    path = tmp_path / "web.db"
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY)
    for i, name, kind in ((1, "bull", "options"), (2, "Bull", "equity"), (3, "Bear", "equity"),
                          (4, "bear", "options"), (5, "Small", "options")):
        conn.execute("INSERT INTO paper_accounts (id, name, created_at, kind, stop_type, stop_value)"
                     " VALUES (?, ?, '2026-07-20', ?, 'trailing_pct', 20)", (i, name, kind))
    conn.execute("DELETE FROM paper_accounts WHERE id IN (1, 4, 5)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def test_same_name_allowed_across_tabs_but_not_within_one(fresh_db):
    _create("Bull", "equity")
    assert _create("Bull", "options")
    with pytest.raises(sqlite3.IntegrityError):
        _create("Bull", "options")


def test_legacy_table_is_migrated_keeping_rows_and_id_counter(legacy_db):
    db.init_db()
    conn = sqlite3.connect(legacy_db)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'paper_accounts'").fetchone()[0]
    assert "UNIQUE (kind, name)" in sql
    assert "name TEXT NOT NULL UNIQUE" not in sql
    rows = conn.execute("SELECT id, name, kind, stop_type, stop_value FROM paper_accounts ORDER BY id").fetchall()
    assert rows == [(2, "Bull", "equity", "trailing_pct", 20.0), (3, "Bear", "equity", "trailing_pct", 20.0)]
    conn.close()

    new_id = _create("Bull", "options")  # the exact case that returned 409 in production
    assert new_id == 6, "ids 1, 4 and 5 still own trades and ledgers; they must never be reused"
    assert _create("Bear", "options") == 7


def test_migration_is_idempotent(legacy_db):
    db.init_db()
    db.init_db()
    conn = sqlite3.connect(legacy_db)
    assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'paper_accounts'").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM paper_accounts").fetchone()[0] == 2


def test_create_route_explains_a_same_tab_conflict(fresh_db):
    from fastapi import HTTPException

    from web import spy_routes

    body = {"name": "Bull", "kind": "options", "starting_capital": 1000}
    spy_routes.create_paper_account(dict(body))
    with pytest.raises(HTTPException) as exc:
        spy_routes.create_paper_account(dict(body))
    assert exc.value.status_code == 409
    assert exc.value.detail == "An options account named 'Bull' already exists — pick another name"
