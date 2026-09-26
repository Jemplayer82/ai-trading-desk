import sqlite3

import pytest

import web.db as db

pytestmark = pytest.mark.unit


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


def test_create_paper_account_round_trip_new_fields(tmp_db):
    acct_id = db.create_paper_account(
        "full-fields",
        50_000.0,
        7,
        "bullish",
        "equity",
        schedule_time="09:30",
        stop_type="stop",
        stop_value=5.0,
        stop_limit_offset=1.0,
    )
    got = db.get_paper_account(acct_id)
    assert got["schedule_time"] == "09:30"
    assert got["stop_type"] == "stop"
    assert got["stop_value"] == 5.0
    assert got["stop_limit_offset"] == 1.0

    listed = db.list_paper_accounts()
    assert len(listed) == 1
    assert listed[0]["schedule_time"] == "09:30"
    assert listed[0]["stop_type"] == "stop"
    assert listed[0]["stop_value"] == 5.0
    assert listed[0]["stop_limit_offset"] == 1.0


def test_create_paper_account_defaults_new_fields(tmp_db):
    acct_id = db.create_paper_account("defaults")
    got = db.get_paper_account(acct_id)
    assert got["schedule_time"] is None
    assert got["stop_type"] == "none"
    assert got["stop_value"] is None
    assert got["stop_limit_offset"] is None


def test_update_paper_account_selective_and_null(tmp_db):
    acct_id = db.create_paper_account("mutate", 100_000.0, 5, "neutral", "equity")

    # Set schedule_time
    assert db.update_paper_account(acct_id, schedule_time="09:15") is True
    assert db.get_paper_account(acct_id)["schedule_time"] == "09:15"

    # A later update that does not mention schedule_time leaves it alone
    assert db.update_paper_account(acct_id, name="renamed") is True
    got = db.get_paper_account(acct_id)
    assert got["name"] == "renamed"
    assert got["schedule_time"] == "09:15"

    # Explicit None nulls it
    assert db.update_paper_account(acct_id, schedule_time=None) is True
    assert db.get_paper_account(acct_id)["schedule_time"] is None

    # Same three-way assertion for stop_value / stop_limit_offset
    assert db.update_paper_account(acct_id, stop_value=12.5, stop_limit_offset=2.0) is True
    got = db.get_paper_account(acct_id)
    assert got["stop_value"] == 12.5
    assert got["stop_limit_offset"] == 2.0

    assert db.update_paper_account(acct_id, stop_type="trailing_pct") is True
    got = db.get_paper_account(acct_id)
    assert got["stop_type"] == "trailing_pct"
    assert got["stop_value"] == 12.5
    assert got["stop_limit_offset"] == 2.0

    assert db.update_paper_account(acct_id, stop_value=None, stop_limit_offset=None) is True
    got = db.get_paper_account(acct_id)
    assert got["stop_value"] is None
    assert got["stop_limit_offset"] is None
    assert got["stop_type"] == "trailing_pct"  # untouched


def test_update_paper_account_no_fields_is_no_op(tmp_db):
    acct_id = db.create_paper_account("noop", 100_000.0, 5, "neutral", "equity",
                                      schedule_time="10:00")
    assert db.update_paper_account(acct_id) is True
    got = db.get_paper_account(acct_id)
    assert got["schedule_time"] == "10:00"
    assert got["stop_type"] == "none"


def test_update_paper_account_stop_type_round_trip(tmp_db):
    acct_id = db.create_paper_account("stop-type")
    assert db.update_paper_account(acct_id, stop_type="trailing_pct", stop_value=25.0) is True
    got = db.get_paper_account(acct_id)
    assert got["stop_type"] == "trailing_pct"
    assert got["stop_value"] == 25.0


def test_legacy_upgrade_backfill(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    # Hand-build a pre-feature paper_accounts table
    raw = sqlite3.connect(db_path)
    raw.execute(
        """
        CREATE TABLE paper_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            starting_capital REAL NOT NULL DEFAULT 100000,
            aggressiveness INTEGER NOT NULL DEFAULT 5,
            bias TEXT NOT NULL DEFAULT 'neutral',
            created_at TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'equity'
        )
        """
    )
    raw.execute(
        "INSERT INTO paper_accounts (name, created_at, kind) VALUES (?, ?, ?)",
        ("legacy-equity", "2024-01-01T00:00:00Z", "equity"),
    )
    raw.execute(
        "INSERT INTO paper_accounts (name, created_at, kind) VALUES (?, ?, ?)",
        ("legacy-options", "2024-01-01T00:00:00Z", "options"),
    )
    raw.commit()
    raw.close()

    db.init_db()

    rows = {r["name"]: r for r in db.list_paper_accounts()}
    assert rows["legacy-equity"]["schedule_time"] == "09:00"
    assert rows["legacy-equity"]["stop_type"] == "none"
    assert rows["legacy-equity"]["stop_value"] is None

    assert rows["legacy-options"]["schedule_time"] == "09:00"
    assert rows["legacy-options"]["stop_type"] == "stop"
    assert rows["legacy-options"]["stop_value"] == 60


def test_research_scan_id_backfill_moves_schedules_to_0900(tmp_path, monkeypatch):
    db_path = tmp_path / "research.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    db.init_db()

    # Simulate a database where spy_scans predates the research_scan_id column.
    raw = sqlite3.connect(db_path)
    cols = [
        r[1] for r in raw.execute("PRAGMA table_info(spy_scans)").fetchall()
        if r[1] != "research_scan_id"
    ]
    raw.execute(
        f"CREATE TABLE spy_scans_new AS SELECT {', '.join(cols)} FROM spy_scans"
    )
    raw.execute("DROP TABLE spy_scans")
    raw.execute("ALTER TABLE spy_scans_new RENAME TO spy_scans")
    raw.commit()
    raw.close()

    opt = db.create_paper_account(
        "opt", kind="options", schedule_time="02:30",
        stop_type="stop", stop_value=60,
    )
    eq = db.create_paper_account("eq", kind="equity", schedule_time="00:00")
    manual = db.create_paper_account("manual", kind="equity")

    db.init_db()

    assert db.get_paper_account(opt)["schedule_time"] == "09:00"
    assert db.get_paper_account(eq)["schedule_time"] == "09:00"
    assert db.get_paper_account(manual)["schedule_time"] is None

    # A deliberate user change is not overwritten by a second init.
    assert db.update_paper_account(opt, schedule_time="10:15") is True
    db.init_db()
    db.init_db()
    assert db.get_paper_account(opt)["schedule_time"] == "10:15"


def test_backfill_is_one_shot(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    raw = sqlite3.connect(db_path)
    raw.execute(
        """
        CREATE TABLE paper_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            starting_capital REAL NOT NULL DEFAULT 100000,
            aggressiveness INTEGER NOT NULL DEFAULT 5,
            bias TEXT NOT NULL DEFAULT 'neutral',
            created_at TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'equity'
        )
        """
    )
    raw.execute(
        "INSERT INTO paper_accounts (name, created_at, kind) VALUES (?, ?, ?)",
        ("legacy-options", "2024-01-01T00:00:00Z", "options"),
    )
    raw.commit()
    raw.close()

    db.init_db()
    row = db.get_paper_account(1)
    assert row["schedule_time"] == "09:00"

    # Clear the schedule manually
    assert db.update_paper_account(1, schedule_time=None) is True
    assert db.get_paper_account(1)["schedule_time"] is None

    # Two more init_db() calls must not re-backfill
    db.init_db()
    db.init_db()
    assert db.get_paper_account(1)["schedule_time"] is None


def test_init_db_idempotent_on_fresh_db(tmp_db):
    db.init_db()
    db.init_db()
    assert db.list_paper_accounts() == []


def test_arm_options_stop_limit(tmp_db):
    acct_id = db.create_paper_account("opt")
    scan_id = db.create_spy_scan("2024-01-02", paper_account_id=acct_id, kind="options")
    pos_id = db.open_options_position(
        acct_id,
        scan_id,
        {
            "occ_symbol": "SPY240102P00450000",
            "underlying": "SPY",
            "put_call": "PUT",
            "strike": 450.0,
            "expiration_date": "2024-01-05",
            "contracts": 1,
            "entry_premium": 2.50,
        },
    )

    stamp = "2024-01-02T14:30:00Z"
    assert db.arm_options_stop_limit(pos_id, when=stamp) is True
    pos = db.get_options_position(pos_id)
    assert pos["stop_triggered_at"] == stamp

    # Readable through list as well
    positions = db.list_options_positions(paper_account_id=acct_id)
    assert len(positions) == 1
    assert positions[0]["stop_triggered_at"] == stamp

    # Second arming is a no-op (keeps original timestamp)
    assert db.arm_options_stop_limit(pos_id, when="2024-01-02T15:00:00Z") is False
    pos = db.get_options_position(pos_id)
    assert pos["stop_triggered_at"] == stamp

    # Close the position and ensure arming is a no-op
    db.close_options_position(pos_id, 1.0, "stop_fill")
    assert db.arm_options_stop_limit(pos_id, when="2024-01-02T16:00:00Z") is False
    pos = db.get_options_position(pos_id)
    assert pos["stop_triggered_at"] == stamp
