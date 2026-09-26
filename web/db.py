"""SQLite persistence for the web service.

One database file (DB_PATH, default ~/.tradingagents/web.db) shared by the
api, portfolio, and scheduler containers via the common Docker volume. That
sharing is load-bearing: it's why a login session minted by the api app also
authenticates requests hitting the portfolio app. Helpers open a short-lived
WAL-mode autocommit connection per call (see connect()); no pool, no ORM.

Tables:
  preferences          - single row of user form defaults
  provider_credentials - per-LLM-provider API key + optional base URL
  app_settings         - env-style key/value config managed from the UI
                         (Schwab app key/secret/callback, Ollama base URL, etc.)
  users                - dashboard login accounts (username + pbkdf2 hash)
  sessions             - active login sessions (cookie token -> username)
  login_attempts       - failed dashboard logins (brute-force throttling)
  analyses               - one row per single-ticker analysis
  portfolio_scans      - one row per nightly portfolio sweep (Schwab)
  portfolio_tickers    - join row connecting a scan to the analyses it generated
  spy_scans            - one row per shared daily research run or per-account
                         daily allocation
  spy_quick_results    - one row per ticker per SPY scan (quick + deep results)
  llm_activity         - cross-container registry of in-flight LLM consumers
  ticker_info          - cached company name/website per ticker

Schema changes: init_db() only runs CREATE TABLE IF NOT EXISTS, which never
alters an existing table — a new column must be added in BOTH SCHEMA (fresh
installs) and _COLUMN_MIGRATIONS (idempotent ALTERs applied on every boot;
the upgrade path for already-deployed databases).

Secrets at rest: provider API keys and app-setting values go through
web/secret_box.py — Fernet-encrypted (ciphertext prefix "enc:v1:") when
TOKEN_ENCRYPTION_KEY is set, transparently plaintext when it isn't. The DB
file is chmod 0600 at init; it also holds password hashes and live session
tokens.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import secret_box

_HOME = Path(os.path.expanduser("~")) / ".tradingagents"
# Same default path in every container — ~/.tradingagents is the shared
# `tradingagents_data` volume in production. Override (e.g. in tests) via
# the TRADINGAGENTS_WEB_DB env var.
DB_PATH = Path(os.environ.get("TRADINGAGENTS_WEB_DB", str(_HOME / "web.db")))


class _Unset:
    """Sentinel: 'caller did not supply this field'. Distinct from None, which
    means 'explicitly set this column to NULL'."""
    __slots__ = ()
    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "UNSET"


UNSET: Any = _Unset()


SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_credentials (
    provider TEXT PRIMARY KEY,
    api_key TEXT NOT NULL,
    base_url TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions (expires_at);

CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    ip TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_user ON login_attempts (username, attempted_at);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts (ip, attempted_at);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    status TEXT NOT NULL,
    provider TEXT,
    deep_model TEXT,
    quick_model TEXT,
    analysts TEXT,
    research_depth INTEGER,
    language TEXT,
    final_decision TEXT,
    processed_signal TEXT,
    market_report TEXT,
    sentiment_report TEXT,
    news_report TEXT,
    fundamentals_report TEXT,
    investment_plan TEXT,
    trader_plan TEXT,
    risk_judge TEXT,
    full_state TEXT,
    error TEXT,
    config_fingerprint TEXT,
    reused_from_analysis_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_analyses_created_at ON analyses (created_at DESC);
-- Same-day deep-dive reuse: looks up a completed analysis for the same
-- ticker/date to reuse the shared pipeline stage (see find_reusable_analysis).
CREATE INDEX IF NOT EXISTS idx_analyses_ticker_date ON analyses (ticker, trade_date);

CREATE TABLE IF NOT EXISTS portfolio_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    status TEXT NOT NULL,
    num_tickers INTEGER DEFAULT 0,
    scanned_count INTEGER DEFAULT 0,
    scan_total INTEGER DEFAULT 0,
    current_ticker TEXT,
    updated_at TEXT,
    signal_counts TEXT,
    aggregator_report TEXT,
    full_payload TEXT,
    error TEXT,
    newsletter_sent_at TEXT,
    newsletter_message_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_portfolio_scans_created_at ON portfolio_scans (created_at DESC);

CREATE TABLE IF NOT EXISTS spy_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    status TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'equity',
    quick_count INTEGER DEFAULT 0,
    quick_total INTEGER DEFAULT 0,
    deep_count INTEGER DEFAULT 0,
    deep_total INTEGER DEFAULT 50,
    allocator_report TEXT,
    portfolio_json TEXT,
    current_value REAL,
    last_price_check TEXT,
    rebalance_notes TEXT,
    cancel_requested INTEGER DEFAULT 0,
    previous_scan_id INTEGER,
    starting_value REAL,
    error TEXT,
    updated_at TEXT,
    paper_account_id INTEGER,
    aggressiveness INTEGER DEFAULT 5,
    bias TEXT DEFAULT 'neutral',
    quick_fingerprint TEXT,
    deep_reused_count INTEGER DEFAULT 0,
    research_scan_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_spy_scans_created_at ON spy_scans (created_at DESC);

CREATE TABLE IF NOT EXISTS spy_quick_results (
    scan_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    signal TEXT,
    conviction INTEGER,
    reasoning TEXT,
    analysis_id INTEGER,
    error TEXT,
    PRIMARY KEY (scan_id, ticker),
    FOREIGN KEY (scan_id) REFERENCES spy_scans (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS portfolio_tickers (
    scan_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    analysis_id INTEGER,
    quantity REAL,
    market_value REAL,
    signal TEXT,
    error TEXT,
    PRIMARY KEY (scan_id, ticker),
    FOREIGN KEY (scan_id) REFERENCES portfolio_scans (id) ON DELETE CASCADE
);

-- Cross-container LLM concurrency registry. The api container inserts a row
-- per in-flight single-ticker analysis; the portfolio scanner reads the live
-- count to dynamically size its worker pool (single-ticker gets priority).
CREATE TABLE IF NOT EXISTS llm_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    label TEXT,
    started_at TEXT NOT NULL,
    heartbeat TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_activity_kind ON llm_activity (kind, heartbeat);

-- Cached company name + website per ticker (resolved via yfinance on demand).
CREATE TABLE IF NOT EXISTS ticker_info (
    ticker TEXT PRIMARY KEY,
    name TEXT,
    website TEXT,
    fetched_at TEXT NOT NULL
);

-- Named paper trading accounts for S&P 500 scans (kind 'equity') and the
-- daily options paper trader (kind 'options').
-- schedule_time is "HH:MM" 24-hour in the scheduler's timezone, NULL meaning
-- manual-only; the stop_* columns are the per-account simulated stop policy
-- (see web/account_policy.py).
CREATE TABLE IF NOT EXISTS paper_accounts (
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
    stop_limit_offset REAL
);

-- One row per paper option contract position over its whole life. Unlike the
-- equity paper portfolio (a daily JSON snapshot in spy_scans.portfolio_json),
-- option positions open/close/expire on different days, so cash and realized
-- P&L must be authoritative — normalized rows + the append-only cash ledger
-- below, mutated only through the transactional helpers in this module.
CREATE TABLE IF NOT EXISTS options_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_account_id INTEGER NOT NULL,
    open_scan_id INTEGER NOT NULL,
    close_scan_id INTEGER,
    occ_symbol TEXT NOT NULL,
    underlying TEXT NOT NULL,
    put_call TEXT NOT NULL,
    strike REAL NOT NULL,
    expiration_date TEXT NOT NULL,
    contracts INTEGER NOT NULL,
    entry_premium REAL NOT NULL,
    cost_basis REAL NOT NULL,
    entry_underlying REAL,
    entry_delta REAL,
    entry_bid REAL,
    entry_ask REAL,
    entry_oi INTEGER,
    signal TEXT,
    conviction INTEGER,
    rationale TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    exit_premium REAL,
    exit_value REAL,
    realized_pnl REAL,
    exit_reason TEXT,
    settlement_close REAL,
    current_premium REAL,
    peak_premium REAL,
    current_value REAL,
    last_marked_at TEXT,
    price_source TEXT,
    stale_count INTEGER DEFAULT 0,
    data_source TEXT,
    exit_underlying REAL,
    exit_underlying_source TEXT,
    stop_triggered_at TEXT  -- set when a stop-limit trigger fired but the mark gapped below the limit price, so a resting fill is still pending
);

CREATE INDEX IF NOT EXISTS idx_options_positions_acct
    ON options_positions (paper_account_id, status);

-- Batch reflections over closed/settled options positions (the options-ledger
-- analogue of System C's memory-log reflections). One row per nightly grading
-- run that had enough NEW closes to reflect on; the latest row's lessons_md is
-- what gets injected into the allocator prompt. stats_json snapshots the
-- mechanical track-record stats the reflection saw.
CREATE TABLE IF NOT EXISTS options_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_account_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    n_closed_total INTEGER,
    n_new INTEGER,
    stats_json TEXT,
    lessons_md TEXT,
    model TEXT
);

CREATE INDEX IF NOT EXISTS idx_options_lessons_acct
    ON options_lessons (paper_account_id, id);

-- Append-only cash ledger for options paper accounts; cash = SUM(amount).
-- kind: deposit | open | close | expire. Opens are negative.
CREATE TABLE IF NOT EXISTS options_cash_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_account_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    scan_id INTEGER,
    position_id INTEGER,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_options_ledger_acct
    ON options_cash_ledger (paper_account_id, id);
"""


# Lightweight column migrations for tables that predate a new column.
# (init_db only runs CREATE TABLE IF NOT EXISTS, which never alters an
# existing table — so additive columns need an explicit guarded ALTER.)
# Append-only and idempotent: every entry is checked on every boot and
# skipped if the column already exists. New columns go in SCHEMA *and* here.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("spy_scans", "cancel_requested", "INTEGER DEFAULT 0"),
    ("spy_scans", "previous_scan_id", "INTEGER"),
    ("spy_scans", "starting_value", "REAL"),
    # Portfolio scan live-progress counters (mirrored in SCHEMA above).
    ("portfolio_scans", "scanned_count", "INTEGER DEFAULT 0"),
    ("portfolio_scans", "scan_total", "INTEGER DEFAULT 0"),
    ("portfolio_scans", "current_ticker", "TEXT"),
    # Liveness stamp for the stuck-run reaper (touched on every progress write).
    ("portfolio_scans", "updated_at", "TEXT"),
    ("spy_scans", "updated_at", "TEXT"),
    ("spy_scans", "paper_account_id", "INTEGER"),
    ("spy_scans", "aggressiveness", "INTEGER DEFAULT 5"),
    ("spy_scans", "bias", "TEXT DEFAULT 'neutral'"),
    # Daily options paper trading: options runs reuse spy_scans (same progress
    # counters / cancel / reaper machinery), discriminated by kind.
    ("spy_scans", "kind", "TEXT NOT NULL DEFAULT 'equity'"),
    ("paper_accounts", "kind", "TEXT NOT NULL DEFAULT 'equity'"),
    # Options learning loop: underlying spot at exit enables directional-vs-
    # decay P&L attribution for closed rows (expiries carry settlement_close).
    ("options_positions", "exit_underlying", "REAL"),
    ("options_positions", "exit_underlying_source", "TEXT"),
    # Trailing stop: highest premium ever marked (seeded at entry). Enables
    # "lock gains once armed" without a full mark-history table.
    ("options_positions", "peak_premium", "REAL"),
    # Same-day deep-dive reuse: shared-stage config fingerprint (see
    # web/spy_scanner.py _deep_dive_fingerprint) + provenance pointer to the
    # donor analysis a row's portfolio-manager rerun was based on.
    ("analyses", "config_fingerprint", "TEXT"),
    ("analyses", "reused_from_analysis_id", "INTEGER"),
    # Quick-scan reuse: fingerprint the LLM-relevant quick-scan config so a
    # later same-day scan can tell whether this scan's spy_quick_results rows
    # are safe to copy (see web/spy_scanner.py _quick_scan_fingerprint).
    ("spy_scans", "quick_fingerprint", "TEXT"),
    ("spy_scans", "deep_reused_count", "INTEGER DEFAULT 0"),
    ("spy_scans", "research_scan_id", "INTEGER"),
    # Per-account scheduled scan time and simulated stop policy.
    ("paper_accounts", "schedule_time", "TEXT"),
    ("paper_accounts", "stop_type", "TEXT NOT NULL DEFAULT 'none'"),
    ("paper_accounts", "stop_value", "REAL"),
    ("paper_accounts", "stop_limit_offset", "REAL"),
    # Stop-limit arm timestamp: trigger fired but the mark gapped through the
    # limit price, so a resting fill is still pending.
    ("options_positions", "stop_triggered_at", "TEXT"),
]


# Backfills that run exactly once — on the boot where the column is added.
# Keyed on the ALTER firing, NOT on a NULL check: a NULL check would re-fill a
# schedule the user deliberately cleared, every boot, forever.
_POST_MIGRATION_BACKFILLS: list[tuple[tuple[tuple[str, str], ...], str]] = [
    ((("paper_accounts", "schedule_time"),),
     "UPDATE paper_accounts SET schedule_time = '09:00'"),
    # The paired stop policy columns must backfill together or not at all.
    ((("paper_accounts", "stop_type"), ("paper_accounts", "stop_value")),
     "UPDATE paper_accounts SET stop_type = 'stop', stop_value = 60 WHERE kind = 'options'"),
    # Shared daily research scan: every scheduled account now allocates at 09:00 ET
    # off the shared 00:00 research; NULL means manual-only and stays NULL; it fires
    # only on the boot where the ALTER adds the column.
    ((("spy_scans", "research_scan_id"),),
     "UPDATE paper_accounts SET schedule_time = '09:00' WHERE schedule_time IS NOT NULL"),
]


def _run_column_migrations(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    applied: set[tuple[str, str]] = set()
    for table, column, decl in _COLUMN_MIGRATIONS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            applied.add((table, column))
    return applied


def init_db() -> None:
    """Create-or-upgrade the database. Runs on every container boot; idempotent.

    Order matters: base schema first, then additive column migrations, then any
    one-time backfills keyed on those migrations, then a one-time re-encrypt of
    any plaintext secret rows (no-op without a key).
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Surface a malformed TOKEN_ENCRYPTION_KEY immediately, before any request
    # tries to read or write a secret.
    secret_box.validate_key()
    with connect() as conn:
        conn.executescript(SCHEMA)
        applied = _run_column_migrations(conn)
        for key, sql in _POST_MIGRATION_BACKFILLS:
            # key is a tuple of one or more (table, column) pairs; all must have been added.
            if all((table, column) in applied for (table, column) in key):
                conn.execute(sql)
        _encrypt_existing_secrets(conn)
    # The DB holds API keys, OAuth secrets and password hashes — keep it readable
    # only by the owning service account, not world/group (umask can leave it 644).
    try:
        DB_PATH.chmod(0o600)
    except OSError:
        pass


def _encrypt_existing_secrets(conn: sqlite3.Connection) -> None:
    """Re-encrypt any plaintext secret rows once a key is configured.

    No-op when encryption is disabled (no key) — values simply stay plaintext,
    matching historical behavior. Idempotent: already-encrypted rows are skipped
    by ``encrypt_secret``.
    """
    if not secret_box.encryption_enabled():
        return
    for provider, api_key in conn.execute(
        "SELECT provider, api_key FROM provider_credentials"
    ).fetchall():
        enc = secret_box.encrypt_secret(api_key)
        if enc != api_key:
            conn.execute(
                "UPDATE provider_credentials SET api_key = ? WHERE provider = ?",
                (enc, provider),
            )
    for key, value in conn.execute("SELECT key, value FROM app_settings").fetchall():
        enc = secret_box.encrypt_secret(value)
        if enc != value:
            conn.execute("UPDATE app_settings SET value = ? WHERE key = ?", (enc, key))


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Yield a fresh connection, closed on exit. No pooling — open per call.

    isolation_level=None puts sqlite3 in autocommit, so each execute commits
    immediately; WAL lets the api/portfolio/scheduler processes read while
    another writes. foreign_keys is per-connection in SQLite and must be
    re-enabled here every time (portfolio_tickers and spy_quick_results rely
    on ON DELETE CASCADE).

    timeout is explicit because the portfolio scanner now runs ~9 concurrent
    writers (8 workers + the collector thread) against SQLite's single WAL write
    lock. Without an explicit value the connection inherits sqlite3's 5.0s
    default, which is too short under that contention and surfaces as
    OperationalError: database is locked.
    """
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def get_preferences() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT data FROM preferences WHERE id = 1").fetchone()
    if not row:
        return {}
    return json.loads(row["data"])


def save_preferences(data: dict[str, Any]) -> None:
    payload = json.dumps(data)
    with connect() as conn:
        conn.execute(
            "INSERT INTO preferences (id, data) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (payload,),
        )


# ---------- provider credentials (LLM API keys) ----------

def set_credential(provider: str, api_key: str, base_url: str | None = None) -> None:
    """Insert-or-update an API key (and optional base URL) for a provider.

    The api_key is encrypted at rest (when TOKEN_ENCRYPTION_KEY is configured);
    base_url is not secret and is stored as-is.
    """
    with connect() as conn:
        conn.execute(
            "INSERT INTO provider_credentials (provider, api_key, base_url, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "api_key = excluded.api_key, base_url = excluded.base_url, updated_at = excluded.updated_at",
            (provider, secret_box.encrypt_secret(api_key), base_url, datetime.utcnow().isoformat()),
        )


def get_credential(provider: str) -> dict[str, Any] | None:
    """Return the full credential row for `provider`, or None if not set."""
    with connect() as conn:
        row = conn.execute(
            "SELECT provider, api_key, base_url, updated_at FROM provider_credentials WHERE provider = ?",
            (provider,),
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["api_key"] = secret_box.decrypt_secret(out["api_key"])
    return out


def list_credentials() -> list[dict[str, Any]]:
    """Return all stored credential rows (api_key included — caller must mask before returning to client)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT provider, api_key, base_url, updated_at FROM provider_credentials"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["api_key"] = secret_box.decrypt_secret(d["api_key"])
        out.append(d)
    return out


def delete_credential(provider: str) -> bool:
    """Remove a provider's credential. Returns True if a row was deleted."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM provider_credentials WHERE provider = ?", (provider,))
    return cur.rowcount > 0


# ---------- app settings (Schwab, Ollama, etc. — env-style config) ----------

def set_app_setting(key: str, value: str) -> None:
    """Insert-or-update a UI-managed env-style setting.

    Values are encrypted at rest (when TOKEN_ENCRYPTION_KEY is configured). All
    settings are encrypted uniformly — several hold secrets (SMTP_PASS, Schwab
    app secret, Alpaca secret) and treating them all the same avoids a
    secret/non-secret classification that could miss one.
    """
    with connect() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, secret_box.encrypt_secret(value), datetime.utcnow().isoformat()),
        )


def get_app_setting(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (key,)
        ).fetchone()
    return secret_box.decrypt_secret(row["value"]) if row else None


def list_app_settings() -> list[dict[str, Any]]:
    """All stored settings — caller must mask before returning to client."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT key, value, updated_at FROM app_settings"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["value"] = secret_box.decrypt_secret(d["value"])
        out.append(d)
    return out


def delete_app_setting(key: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    return cur.rowcount > 0


# ---------- auth: users + sessions ----------

def count_users() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return int(row["n"])


def create_user(username: str, password_hash: str) -> None:
    """Insert a new user. Raises sqlite3.IntegrityError if username exists."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, datetime.utcnow().isoformat()),
        )


def get_user(username: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT username, password_hash, created_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT username, created_at FROM users ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def set_user_password(username: str, password_hash: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (password_hash, username),
        )
    return cur.rowcount > 0


def delete_user(username: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
    return cur.rowcount > 0


def create_session(token: str, username: str, expires_at: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, username, datetime.utcnow().isoformat(), expires_at),
        )


def get_session(token: str) -> dict[str, Any] | None:
    """Return a non-expired session row, or None. Expired rows are ignored."""
    now = datetime.utcnow().isoformat()
    with connect() as conn:
        row = conn.execute(
            "SELECT token, username, created_at, expires_at FROM sessions "
            "WHERE token = ? AND expires_at > ?",
            (token, now),
        ).fetchone()
    return dict(row) if row else None


def delete_session(token: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    return cur.rowcount > 0


def purge_expired_sessions() -> int:
    now = datetime.utcnow().isoformat()
    with connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
    return cur.rowcount


# ---------- login throttling (see web/auth_app.py for the policy) ----------

def record_failed_login(username: str, ip: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO login_attempts (username, ip, attempted_at) VALUES (?, ?, ?)",
            (username, ip, datetime.utcnow().isoformat()),
        )


def count_failed_logins_for_user(username: str, since: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM login_attempts WHERE username = ? AND attempted_at > ?",
            (username, since),
        ).fetchone()
    return int(row["n"])


def count_failed_logins_for_ip(ip: str, since: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM login_attempts WHERE ip = ? AND attempted_at > ?",
            (ip, since),
        ).fetchone()
    return int(row["n"])


def clear_failed_logins(username: str) -> None:
    """A successful login wipes that username's failures (fresh window)."""
    with connect() as conn:
        conn.execute("DELETE FROM login_attempts WHERE username = ?", (username,))


def purge_stale_login_attempts(window_minutes: int) -> int:
    """Drop attempts older than the lockout window; they can't affect any count."""
    cutoff = (datetime.utcnow() - timedelta(minutes=window_minutes)).isoformat()
    with connect() as conn:
        cur = conn.execute("DELETE FROM login_attempts WHERE attempted_at <= ?", (cutoff,))
    return cur.rowcount


# ---------- single-ticker analyses ----------

def create_analysis(params: dict[str, Any]) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO analyses (
                created_at, ticker, trade_date, status,
                provider, deep_model, quick_model, analysts, research_depth, language,
                config_fingerprint
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
                params["ticker"],
                params["trade_date"],
                params.get("provider"),
                params.get("deep_model"),
                params.get("quick_model"),
                json.dumps(params.get("analysts", [])),
                params.get("research_depth"),
                params.get("language"),
                params.get("config_fingerprint"),
            ),
        )
        return int(cur.lastrowid)


def complete_analysis(analysis_id: int, final_state: dict[str, Any], processed_signal: str) -> None:
    """Mark an analysis completed and persist the final graph state.

    Non-string report fields are JSON-encoded so every report column stays
    TEXT; full_state goes through _serialize first so LangChain/pydantic
    objects in the state don't break json.dumps.
    """
    def _get(key: str) -> str:
        val = final_state.get(key, "")
        return val if isinstance(val, str) else json.dumps(val)

    risk = final_state.get("risk_debate_state") or {}
    risk_judge = risk.get("judge_decision", "") if isinstance(risk, dict) else ""

    with connect() as conn:
        conn.execute(
            """
            UPDATE analyses SET
                status = 'completed',
                final_decision = ?,
                processed_signal = ?,
                market_report = ?,
                sentiment_report = ?,
                news_report = ?,
                fundamentals_report = ?,
                investment_plan = ?,
                trader_plan = ?,
                risk_judge = ?,
                full_state = ?
            WHERE id = ?
            """,
            (
                _get("final_trade_decision"),
                processed_signal,
                _get("market_report"),
                _get("sentiment_report"),
                _get("news_report"),
                _get("fundamentals_report"),
                _get("investment_plan"),
                _get("trader_investment_plan"),
                risk_judge,
                json.dumps(_serialize(final_state)),
                analysis_id,
            ),
        )


def fail_analysis(analysis_id: int, error: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE analyses SET status = 'failed', error = ? WHERE id = ?",
            (error, analysis_id),
        )


def delete_analysis(analysis_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))
        return cur.rowcount > 0


def delete_all_analyses() -> int:
    with connect() as conn:
        cur = conn.execute("DELETE FROM analyses")
        return cur.rowcount


def list_analyses(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, ticker, trade_date, status,
                   provider, deep_model, quick_model, final_decision, processed_signal
            FROM analyses ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def recent_analysis_tickers(created_after_iso: str) -> list[str]:
    """Distinct, uppercased tickers with any analysis row created at/after the cutoff.

    Cheap relevance probe for the nightly outcome sweep: the analyses table
    is written both by ad-hoc single-ticker runs and by the S&P/options
    scanner's deep dives (web/spy_scanner.py's db.create_analysis calls), so
    "appears here recently" is "was deep-dived recently" in practice.
    Deliberately unfiltered by status — a failed or in-flight run still means
    somebody cared about the name. ``created_after_iso`` uses the same UTC
    ``isoformat(timespec="seconds") + "Z"`` format rows are stored in (see
    create_analysis, line 688); idx_analyses_created_at (line 127) covers the scan.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM analyses WHERE created_at >= ? ORDER BY ticker",
            (created_after_iso,),
        ).fetchall()
    return [str(r["ticker"]).upper() for r in rows if r["ticker"]]


def get_analysis(analysis_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    for key in ("analysts", "full_state"):
        if data.get(key):
            try:
                data[key] = json.loads(data[key])
            except (TypeError, ValueError):
                pass
    return data


def find_reusable_analysis(
    ticker: str,
    trade_date: str,
    config_fingerprint: str,
    max_age_hours: float,
) -> dict[str, Any] | None:
    """Latest completed same-day analysis with a matching shared-stage fingerprint.

    Donates the shared pipeline stage (analyst reports, debate, research-plan)
    to a same-day scan for a different paper account so only the portfolio
    manager needs to rerun (see SwitchboardOrchestrator.rerun_decision).

    Only original runs qualify — ``reused_from_analysis_id IS NULL`` — so a
    reused row never becomes a donor itself (copies never chain off copies).
    ``full_state`` must be present and decode to a dict; a corrupt/legacy
    row is treated as a miss, never an error, so the caller always has a
    full-pipeline fallback.
    """
    cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat(timespec="seconds") + "Z"
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM analyses
            WHERE ticker = ? AND trade_date = ? AND status = 'completed'
              AND config_fingerprint = ? AND reused_from_analysis_id IS NULL
              AND full_state IS NOT NULL AND created_at >= ?
            ORDER BY id DESC LIMIT 1
            """,
            (ticker, trade_date, config_fingerprint, cutoff),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        full_state = json.loads(data["full_state"])
    except (TypeError, ValueError):
        return None
    if not isinstance(full_state, dict):
        return None
    data["full_state"] = full_state
    if data.get("analysts"):
        try:
            data["analysts"] = json.loads(data["analysts"])
        except (TypeError, ValueError):
            pass
    return data


def mark_analysis_reused(analysis_id: int, source_analysis_id: int) -> None:
    """Record that ``analysis_id`` was produced by rerunning the portfolio
    manager over ``source_analysis_id``'s shared-stage state (provenance
    only — does not touch status/reports, which complete_analysis sets)."""
    with connect() as conn:
        conn.execute(
            "UPDATE analyses SET reused_from_analysis_id = ? WHERE id = ?",
            (source_analysis_id, analysis_id),
        )


# ---------- portfolio scans (nightly holdings sweep) ----------

def create_portfolio_scan(trade_date: str, status: str = "running") -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO portfolio_scans (created_at, trade_date, status) VALUES (?, ?, ?)",
            (datetime.utcnow().isoformat(timespec="seconds") + "Z", trade_date, status),
        )
        return int(cur.lastrowid)


def add_scan_ticker(
    scan_id: int,
    ticker: str,
    analysis_id: int | None,
    quantity: float,
    market_value: float,
    signal: str | None,
    error: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO portfolio_tickers (scan_id, ticker, analysis_id, quantity, market_value, signal, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scan_id, ticker) DO UPDATE SET
                analysis_id = excluded.analysis_id,
                quantity = excluded.quantity,
                market_value = excluded.market_value,
                signal = excluded.signal,
                error = excluded.error
            """,
            (scan_id, ticker, analysis_id, quantity, market_value, signal, error),
        )


def complete_portfolio_scan(
    scan_id: int,
    aggregator_report: str,
    signal_counts: dict[str, int],
    num_tickers: int,
    full_payload: dict[str, Any] | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE portfolio_scans SET
                status = 'completed',
                num_tickers = ?,
                signal_counts = ?,
                aggregator_report = ?,
                full_payload = ?
            WHERE id = ?
            """,
            (
                num_tickers,
                json.dumps(signal_counts),
                aggregator_report,
                json.dumps(_serialize(full_payload or {})),
                scan_id,
            ),
        )


def fail_portfolio_scan(scan_id: int, error: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE portfolio_scans SET status = 'failed', error = ? WHERE id = ?",
            (error, scan_id),
        )


def mark_newsletter_sent(scan_id: int, message_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE portfolio_scans SET newsletter_sent_at = ?, newsletter_message_id = ? WHERE id = ?",
            (datetime.utcnow().isoformat(timespec="seconds") + "Z", message_id, scan_id),
        )


def list_portfolio_scans(
    limit: int = 50, statuses: list[str] | None = None
) -> list[dict[str, Any]]:
    with connect() as conn:
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            rows = conn.execute(
                f"SELECT id, created_at, trade_date, status, num_tickers, signal_counts,"
                f" newsletter_sent_at FROM portfolio_scans"
                f" WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT ?",
                (*statuses, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, created_at, trade_date, status, num_tickers, signal_counts,"
                " newsletter_sent_at FROM portfolio_scans ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("signal_counts"):
            try:
                d["signal_counts"] = json.loads(d["signal_counts"])
            except (TypeError, ValueError):
                pass
        out.append(d)
    return out


def get_portfolio_scan(scan_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM portfolio_scans WHERE id = ?", (scan_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        tickers = conn.execute(
            "SELECT * FROM portfolio_tickers WHERE scan_id = ? ORDER BY ticker",
            (scan_id,),
        ).fetchall()
    for key in ("signal_counts", "full_payload"):
        if data.get(key):
            try:
                data[key] = json.loads(data[key])
            except (TypeError, ValueError):
                pass
    data["tickers"] = [dict(t) for t in tickers]
    return data


def latest_portfolio_scan() -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM portfolio_scans ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return get_portfolio_scan(int(row["id"]))


# ---------- S&P 500 scanner ----------

def create_spy_scan(
    trade_date: str,
    paper_account_id: int | None = None,
    research_scan_id: int | None = None,
    aggressiveness: int = 5,
    bias: str = "neutral",
    status: str = "pending",
    kind: str = "equity",
) -> int:
    with connect() as conn:
        cur = conn.execute(
            # quick_total / deep_total go in as NULL ("not known yet") rather
            # than the column DEFAULTs (0 / 50), so the UI's 151/51 placeholders
            # apply until a scan writes its real totals.
            "INSERT INTO spy_scans (created_at, trade_date, status, cancel_requested, "
            "paper_account_id, aggressiveness, bias, kind, research_scan_id, "
            "quick_total, deep_total) "
            "VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, NULL, NULL)",
            (datetime.utcnow().isoformat(timespec="seconds") + "Z", trade_date,
             status, paper_account_id, aggressiveness, bias, kind, research_scan_id),
        )
        return int(cur.lastrowid)


def request_spy_scan_cancel(scan_id: int) -> None:
    """Flag a running scan for cooperative cancellation."""
    with connect() as conn:
        conn.execute("UPDATE spy_scans SET cancel_requested = 1 WHERE id = ?", (scan_id,))


def is_spy_scan_cancelled(scan_id: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT cancel_requested FROM spy_scans WHERE id = ?", (scan_id,)
        ).fetchone()
    return bool(row and row["cancel_requested"])


# Columns update_spy_scan is allowed to set. Callers only ever pass these
# (progress counters + status), but since the column names are interpolated into
# SQL rather than parameterized, an allow-list keeps that interpolation safe even
# if a caller is ever changed.
_SPY_SCAN_UPDATABLE = {
    "status", "quick_count", "quick_total", "deep_count", "deep_total",
    "current_value", "last_price_check", "rebalance_notes", "error",
    "paper_account_id", "aggressiveness", "bias", "research_scan_id",
    "quick_fingerprint", "deep_reused_count",
}


def update_spy_scan(scan_id: int, **kwargs: Any) -> None:
    if not kwargs:
        return
    bad = set(kwargs) - _SPY_SCAN_UPDATABLE
    if bad:
        raise ValueError(f"update_spy_scan: disallowed column(s) {sorted(bad)}")
    # Always stamp updated_at (a fixed literal column, not caller-controlled) so the
    # stuck-run reaper can tell a live-but-slow scan from a crashed one.
    sets = ", ".join(k + " = ?" for k in kwargs) + ", updated_at = ?"
    vals = list(kwargs.values()) + [datetime.utcnow().isoformat(timespec="seconds") + "Z", scan_id]
    with connect() as conn:
        conn.execute("UPDATE spy_scans SET " + sets + " WHERE id = ?", vals)


# Columns update_portfolio_scan is allowed to set. Column names are interpolated
# into SQL (not parameterized), so this allow-list is a security control — keep it
# tight to exactly the three progress columns.
_PORTFOLIO_SCAN_UPDATABLE = {"scanned_count", "scan_total", "current_ticker", "status"}


def update_portfolio_scan(scan_id: int, **kwargs: Any) -> None:
    if not kwargs:
        return
    bad = set(kwargs) - _PORTFOLIO_SCAN_UPDATABLE
    if bad:
        raise ValueError(f"update_portfolio_scan: disallowed column(s) {sorted(bad)}")
    # Always stamp updated_at (a fixed literal column, not caller-controlled) so the
    # stuck-run reaper can tell a live-but-slow scan from a crashed one.
    sets = ", ".join(k + " = ?" for k in kwargs) + ", updated_at = ?"
    vals = list(kwargs.values()) + [datetime.utcnow().isoformat(timespec="seconds") + "Z", scan_id]
    with connect() as conn:
        conn.execute("UPDATE portfolio_scans SET " + sets + " WHERE id = ?", vals)


def complete_spy_scan(
    scan_id: int,
    allocator_report: str,
    portfolio_json: list[dict[str, Any]],
    previous_scan_id: int | None = None,
    starting_value: float | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE spy_scans
               SET status = 'completed', allocator_report = ?, portfolio_json = ?,
                   previous_scan_id = ?, starting_value = ?
               WHERE id = ?""",
            (allocator_report, json.dumps(_serialize(portfolio_json)),
             previous_scan_id, starting_value, scan_id),
        )


def get_latest_completed_spy_scan(
    exclude_id: int | None = None,
    paper_account_id: int | None = None,
    kind: str = "equity",
) -> dict[str, Any] | None:
    """Return the most recent completed scan, optionally filtered by account or excluding one ID."""
    conditions = ["status = 'completed'", "kind = ?"]
    params: list[Any] = [kind]
    if exclude_id is not None:
        conditions.append("id != ?")
        params.append(exclude_id)
    if paper_account_id is not None:
        conditions.append("paper_account_id = ?")
        params.append(paper_account_id)
    where = " AND ".join(conditions)
    with connect() as conn:
        row = conn.execute(
            f"SELECT id FROM spy_scans WHERE {where} ORDER BY id DESC LIMIT 1",
            params,
        ).fetchone()
    if not row:
        return None
    return get_spy_scan(int(row["id"]))


def fail_spy_scan(scan_id: int, error: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE spy_scans SET status = 'failed', error = ? WHERE id = ?",
            (error, scan_id),
        )


# ---------- stuck-run reaper queries (see web/scheduler.py:job_reap_stuck_runs) ----------
# A run whose worker crashed/OOM'd never transitions out of its running state, so
# these find rows still "running" that have gone quiet past a cutoff. Scans use
# COALESCE(updated_at, created_at) so rows predating the updated_at column — and
# scans that died before their first progress write — fall back to created_at.

def find_stuck_portfolio_scans(stall_before_iso: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, trade_date FROM portfolio_scans "
            "WHERE status = 'running' AND COALESCE(updated_at, created_at) < ?",
            (stall_before_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def find_stuck_spy_scans(stall_before_iso: str) -> list[dict[str, Any]]:
    """Covers both equity and options runs; reaper labels use kind, status and research_scan_id."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, trade_date, kind, status, research_scan_id FROM spy_scans "
            "WHERE status NOT IN ('completed', 'cancelled', 'failed', 'queued') "
            "AND COALESCE(updated_at, created_at) < ?",
            (stall_before_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def fail_interrupted_spy_scans(error: str) -> list[dict[str, Any]]:
    """Fail every in-flight spy_scans row; return them (pre-update values).

    Called at portfolio startup: every spy_scans worker is a thread inside that
    single process, so any row still in flight when it starts is an orphan from
    the previous process. 'queued' rows are left for the queue to dispatch.
    """
    live = "status NOT IN ('completed', 'cancelled', 'failed', 'queued')"
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id, trade_date, kind, status, research_scan_id FROM spy_scans WHERE {live}"
        ).fetchall()
        if rows:
            conn.execute(
                f"UPDATE spy_scans SET status = 'failed', error = ? WHERE {live}",
                (error,),
            )
    return [dict(r) for r in rows]


def find_stuck_analyses(created_before_iso: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, ticker FROM analyses WHERE status = 'running' AND created_at < ?",
            (created_before_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_spy_scan_prices(
    scan_id: int,
    current_value: float,
    rebalance_notes: str,
    portfolio_json: list[dict[str, Any]] | None = None,
) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with connect() as conn:
        if portfolio_json is not None:
            conn.execute(
                "UPDATE spy_scans SET current_value = ?, last_price_check = ?, rebalance_notes = ?, portfolio_json = ? WHERE id = ?",
                (current_value, now, rebalance_notes, json.dumps(_serialize(portfolio_json)), scan_id),
            )
        else:
            conn.execute(
                "UPDATE spy_scans SET current_value = ?, last_price_check = ?, rebalance_notes = ? WHERE id = ?",
                (current_value, now, rebalance_notes, scan_id),
            )


# Cap the free-text fields on a quick-result row. GET /api/spy-scans/{id} (and
# the options equivalent) return up to ~500 of these rows in one payload,
# polled every 5s for a running scan's full duration — a confirmed ~38 MiB/hr
# leak was traced to that endpoint. The polling fix (get_spy_scan_status)
# addresses the poll cadence; this bounds the worst case at write time so a
# terminal full-fetch, or any other future reader, can't reintroduce the same
# shape. Matches the existing truncate-free-text convention (alerts.py's
# _ERROR_MAX, scheduler.py's _ALERT_DETAIL_MAX), applied here at the DB layer
# instead of at each call site since every reader benefits.
_QUICK_RESULT_TEXT_MAX = 500


def upsert_spy_quick_result(
    scan_id: int,
    ticker: str,
    signal: str | None = None,
    conviction: int | None = None,
    reasoning: str | None = None,
    analysis_id: int | None = None,
    error: str | None = None,
) -> None:
    """Insert or merge one ticker's result row for a SPY scan.

    COALESCE merge: a later partial upsert (e.g. the deep pass adding only
    analysis_id) fills just the fields it passes and never nulls out earlier
    ones. Consequence: a field cannot be reset to NULL through this helper.
    """
    if reasoning is not None:
        reasoning = reasoning[:_QUICK_RESULT_TEXT_MAX]
    if error is not None:
        error = error[:_QUICK_RESULT_TEXT_MAX]
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO spy_quick_results (scan_id, ticker, signal, conviction, reasoning, analysis_id, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scan_id, ticker) DO UPDATE SET
                signal = COALESCE(excluded.signal, signal),
                conviction = COALESCE(excluded.conviction, conviction),
                reasoning = COALESCE(excluded.reasoning, reasoning),
                analysis_id = COALESCE(excluded.analysis_id, analysis_id),
                error = COALESCE(excluded.error, error)
            """,
            (scan_id, ticker, signal, conviction, reasoning, analysis_id, error),
        )


def find_reusable_quick_results(
    trade_date: str,
    quick_fingerprint: str,
    exclude_scan_id: int,
    max_age_hours: float,
) -> dict[str, dict[str, Any]]:
    """``{ticker: {signal, conviction, reasoning}}`` from same-day completed
    scans with a matching quick-scan fingerprint.

    Only rows from ``status = 'completed'`` source scans donate — a
    failed/cancelled scan's coverage is partial and not trustworthy for
    reuse. Error rows (the quick LLM call itself failed) and rows with no
    signal are excluded so they can never poison another scan's top-N
    selection. ``entry_price`` is deliberately not stored here or returned —
    callers must always recompute it fresh. Ordered oldest-donor-first so
    that when more than one same-day scan qualifies, the most recent one's
    values win as the dict is built.
    """
    cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat(timespec="seconds") + "Z"
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT r.ticker, r.signal, r.conviction, r.reasoning
            FROM spy_quick_results r
            JOIN spy_scans s ON s.id = r.scan_id
            WHERE s.trade_date = ? AND s.quick_fingerprint = ? AND s.status = 'completed'
              AND s.id != ? AND s.created_at >= ?
              AND r.error IS NULL AND r.signal IS NOT NULL
            ORDER BY s.id ASC
            """,
            (trade_date, quick_fingerprint, exclude_scan_id, cutoff),
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out[row["ticker"]] = {
            "signal": row["signal"],
            "conviction": row["conviction"],
            "reasoning": row["reasoning"],
        }
    return out


def list_spy_scans(
    limit: int = 50,
    paper_account_id: int | None = None,
    statuses: list[str] | None = None,
    kind: str = "equity",
) -> list[dict[str, Any]]:
    """List scans of one kind ('equity' default keeps the S&P tab unchanged;
    pass 'options' for the daily options runs), optionally filtered by status
    (the Queue/History sidebar tabs)."""
    cols = (
        "id, created_at, trade_date, status, quick_count, quick_total,"
        " deep_count, deep_total, current_value, last_price_check,"
        " paper_account_id, aggressiveness, bias, previous_scan_id,"
        " cancel_requested, kind, research_scan_id"
    )
    conditions: list[str] = ["kind = ?"]
    params: list[Any] = [kind]
    if paper_account_id is not None:
        conditions.append("paper_account_id = ?")
        params.append(paper_account_id)
    if statuses:
        placeholders = ",".join("?" * len(statuses))
        conditions.append(f"status IN ({placeholders})")
        params.extend(statuses)
    where = "WHERE " + " AND ".join(conditions)
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {cols} FROM spy_scans {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_spy_scan_status(scan_id: int) -> dict[str, Any] | None:
    """Cheap poll target: just the mutable progress fields, no join.

    get_spy_scan does `SELECT *` plus a full spy_quick_results join (up to
    ~500 rows, each with reasoning/error text) and is polled every 5s for a
    running scan's full duration — that's the confirmed ~38 MiB/hr leak. This
    is O(1) regardless of row count; the frontend polls it instead and only
    fetches the full payload when something in here actually changed.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT id, status, kind, research_scan_id, quick_count, quick_total, deep_count,"
            " deep_total, cancel_requested, updated_at FROM spy_scans WHERE id = ?",
            (scan_id,),
        ).fetchone()
    return dict(row) if row else None


def get_spy_scan(scan_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM spy_scans WHERE id = ?", (scan_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        results = conn.execute(
            "SELECT * FROM spy_quick_results WHERE scan_id = ? ORDER BY conviction DESC, ticker",
            (scan_id,),
        ).fetchall()
    if data.get("portfolio_json"):
        try:
            data["portfolio_json"] = json.loads(data["portfolio_json"])
        except (TypeError, ValueError):
            pass
    data["quick_results"] = [dict(r) for r in results]
    return data


def get_spy_quick_result(scan_id: int, ticker: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM spy_quick_results WHERE scan_id = ? AND ticker = ?",
            (scan_id, ticker),
        ).fetchone()
    return dict(row) if row else None


def latest_spy_scan(kind: str = "equity") -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM spy_scans WHERE kind = ? ORDER BY id DESC LIMIT 1",
            (kind,),
        ).fetchone()
    if not row:
        return None
    return get_spy_scan(int(row["id"]))


def delete_spy_scan(scan_id: int) -> bool:
    """Delete a scan and its quick results (FK ON DELETE CASCADE).

    Deep-dive `analyses` rows the scan created are intentionally kept — they
    remain accessible from the Run Analysis history.
    """
    with connect() as conn:
        cur = conn.execute("DELETE FROM spy_scans WHERE id = ?", (scan_id,))
        return cur.rowcount > 0


def delete_all_spy_scans(kind: str = "equity") -> int:
    """Delete all scans of one kind and their quick results (FK ON DELETE
    CASCADE). Scoped by kind so the S&P tab's "clear" can't nuke options
    history (and vice versa).

    Deep-dive `analyses` rows the scans created are intentionally kept — they
    remain accessible from the Run Analysis history.
    """
    with connect() as conn:
        cur = conn.execute("DELETE FROM spy_scans WHERE kind = ?", (kind,))
        return cur.rowcount


# ---------- research-scan helpers (shared daily research) ----------
def latest_research_scan(trade_date: str, *, completed_only: bool = False) -> dict[str, Any] | None:
    """kind='research' rows are the shared daily research, `paper_account_id` is NULL on them, and their `spy_quick_results` + `analyses` rows ARE the research."""
    params: list[Any] = [trade_date]
    status_filter = ""
    if completed_only:
        status_filter = " AND status = 'completed'"
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT id, trade_date, status, created_at, updated_at, error,
                   quick_count, quick_total, deep_count, deep_total, deep_reused_count,
                   quick_fingerprint, aggressiveness
            FROM spy_scans
            WHERE kind = 'research' AND trade_date = ?{status_filter}
            ORDER BY id DESC LIMIT 1
            """,
            params,
        ).fetchone()
    if not row:
        return None
    return dict(row)


def count_research_attempts(trade_date: str) -> int:
    """Count every kind='research' scan attempt for a trade date."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM spy_scans WHERE kind = 'research' AND trade_date = ?",
            (trade_date,),
        ).fetchone()
    return int(row["n"]) if row else 0


def copy_spy_quick_results(src_scan_id: int, dst_scan_id: int) -> int:
    """Copy all spy_quick_results rows from ``src_scan_id`` to ``dst_scan_id``.

    The destination rows are inserted or replaced; the number of source rows
    is returned.
    """
    with connect() as conn:
        count_row = conn.execute(
            "SELECT COUNT(*) AS n FROM spy_quick_results WHERE scan_id = ?",
            (src_scan_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT OR REPLACE INTO spy_quick_results
                (scan_id, ticker, signal, conviction, reasoning, analysis_id, error)
            SELECT ?, ticker, signal, conviction, reasoning, analysis_id, error
            FROM spy_quick_results
            WHERE scan_id = ?
            """,
            (dst_scan_id, src_scan_id),
        )
    return int(count_row["n"]) if count_row else 0


def list_deep_dived_results(scan_id: int) -> list[dict[str, Any]]:
    """run_deep_dives._finish upserts the deep rating and `analysis_id` onto the quick row; failed dives never touch it."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT r.ticker, r.signal, r.conviction, r.reasoning, r.analysis_id, a.final_decision
            FROM spy_quick_results r
            JOIN analyses a ON a.id = r.analysis_id
            WHERE r.scan_id = ? AND r.error IS NULL AND a.status = 'completed'
            ORDER BY r.conviction DESC, r.ticker
            """,
            (scan_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- cross-container LLM concurrency registry ----------

def register_activity(kind: str, label: str | None = None) -> int:
    """Record an in-flight LLM consumer (e.g. a single-ticker analysis).

    Returns the row id; pass it to heartbeat_activity()/clear_activity().
    """
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO llm_activity (kind, label, started_at, heartbeat) VALUES (?, ?, ?, ?)",
            (kind, label, now, now),
        )
        return int(cur.lastrowid)


def heartbeat_activity(activity_id: int) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with connect() as conn:
        conn.execute("UPDATE llm_activity SET heartbeat = ? WHERE id = ?", (now, activity_id))


def clear_activity(activity_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM llm_activity WHERE id = ?", (activity_id,))


def count_active_single(stale_seconds: int = 120) -> int:
    """Count fresh single-ticker analyses currently consuming LLM slots.

    Rows whose heartbeat is older than stale_seconds are ignored, so a crashed
    api container can't permanently starve the scanner. (Actual deletion of
    stale rows is purge_stale_activity's job; this only filters.)
    """
    cutoff = (
        datetime.utcnow() - timedelta(seconds=stale_seconds)
    ).isoformat(timespec="seconds") + "Z"
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM llm_activity WHERE kind = 'single' AND heartbeat >= ?",
            (cutoff,),
        ).fetchone()
    return int(row["n"]) if row else 0


def purge_stale_activity(stale_seconds: int = 120) -> None:
    cutoff = (
        datetime.utcnow() - timedelta(seconds=stale_seconds)
    ).isoformat(timespec="seconds") + "Z"
    with connect() as conn:
        conn.execute("DELETE FROM llm_activity WHERE heartbeat < ?", (cutoff,))


# ---------- ticker company-info cache ----------

def get_ticker_info(ticker: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT ticker, name, website FROM ticker_info WHERE ticker = ?", (ticker,)
        ).fetchone()
    return dict(row) if row else None


def set_ticker_info(ticker: str, name: str | None, website: str | None) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO ticker_info (ticker, name, website, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                name = excluded.name,
                website = excluded.website,
                fetched_at = excluded.fetched_at
            """,
            (ticker, name, website, now),
        )


# ---------- paper trading accounts ----------
_PAPER_ACCOUNT_COLUMNS = (
    "id", "name", "starting_capital", "aggressiveness", "bias", "created_at",
    "kind", "schedule_time", "stop_type", "stop_value", "stop_limit_offset",
)
_PAPER_ACCOUNT_SELECT = ", ".join(_PAPER_ACCOUNT_COLUMNS)


def create_paper_account(
    name: str,
    starting_capital: float = 100_000.0,
    aggressiveness: int = 5,
    bias: str = "neutral",
    kind: str = "equity",
    schedule_time: str | None = None,
    stop_type: str = "none",
    stop_value: float | None = None,
    stop_limit_offset: float | None = None,
) -> int:
    """Create a named paper account. Raises sqlite3.IntegrityError if name exists.

    No validation or defaulting here — the routes own policy validation.
    """
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO paper_accounts (name, starting_capital, aggressiveness, bias, created_at, kind, schedule_time, stop_type, stop_value, stop_limit_offset) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, starting_capital, aggressiveness, bias,
             datetime.utcnow().isoformat(timespec="seconds") + "Z", kind,
             schedule_time, stop_type, stop_value, stop_limit_offset),
        )
        return int(cur.lastrowid)


def list_paper_accounts(kind: str | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        if kind is not None:
            rows = conn.execute(
                f"SELECT {_PAPER_ACCOUNT_SELECT} FROM paper_accounts WHERE kind = ? ORDER BY id",
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_PAPER_ACCOUNT_SELECT} FROM paper_accounts ORDER BY id"
            ).fetchall()
    return [dict(r) for r in rows]


def get_paper_account(account_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT {_PAPER_ACCOUNT_SELECT} FROM paper_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
    return dict(row) if row else None


def update_paper_account(
    account_id: int,
    name: str | Any = UNSET,
    starting_capital: float | Any = UNSET,
    aggressiveness: int | Any = UNSET,
    bias: str | Any = UNSET,
    schedule_time: str | None | Any = UNSET,
    stop_type: str | Any = UNSET,
    stop_value: float | None | Any = UNSET,
    stop_limit_offset: float | None | Any = UNSET,
) -> bool:
    """Update one or more paper_account columns. Pass None to NULL a column;
    omit the argument (or pass UNSET) to leave it untouched. Returns True if
    the row existed, False otherwise. With no fields to change, returns True.
    """
    sets, vals = [], []
    if name is not UNSET:
        sets.append("name = ?"); vals.append(name)
    if starting_capital is not UNSET:
        sets.append("starting_capital = ?"); vals.append(starting_capital)
    if aggressiveness is not UNSET:
        sets.append("aggressiveness = ?"); vals.append(aggressiveness)
    if bias is not UNSET:
        sets.append("bias = ?"); vals.append(bias)
    if schedule_time is not UNSET:
        sets.append("schedule_time = ?"); vals.append(schedule_time)
    if stop_type is not UNSET:
        sets.append("stop_type = ?"); vals.append(stop_type)
    if stop_value is not UNSET:
        sets.append("stop_value = ?"); vals.append(stop_value)
    if stop_limit_offset is not UNSET:
        sets.append("stop_limit_offset = ?"); vals.append(stop_limit_offset)
    if not sets:
        return True
    vals.append(account_id)
    with connect() as conn:
        cur = conn.execute(
            "UPDATE paper_accounts SET " + ", ".join(sets) + " WHERE id = ?", vals
        )
    return cur.rowcount > 0


def delete_paper_account(account_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM paper_accounts WHERE id = ?", (account_id,))
    return cur.rowcount > 0


# ---------- options paper trading (positions + cash ledger) ----------
# Cash and realized P&L are authoritative here (unlike the equity paper
# portfolio's derived-from-snapshot math): every position open/close/settle
# writes the position row and its ledger entry inside ONE explicit transaction
# (connect() is autocommit-per-statement, so BEGIN IMMEDIATE is required).
# Close/settle are guarded by `status = 'open'` — re-running them is a no-op,
# which is the double-settlement protection.

OPTION_POSITION_OPEN = "open"
OPTION_POSITION_CLOSED = "closed"
OPTION_POSITION_EXPIRED_ITM = "expired_itm"
OPTION_POSITION_EXPIRED_WORTHLESS = "expired_worthless"


def append_options_cash(
    paper_account_id: int,
    kind: str,
    amount: float,
    scan_id: int | None = None,
    position_id: int | None = None,
    note: str | None = None,
) -> int:
    """Append one ledger row (used directly only for 'deposit'; open/close/expire
    rows are written by the transactional position helpers below)."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO options_cash_ledger (paper_account_id, ts, kind, amount, scan_id, position_id, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (paper_account_id, datetime.utcnow().isoformat(timespec="seconds") + "Z",
             kind, round(float(amount), 2), scan_id, position_id, note),
        )
        return int(cur.lastrowid)


def options_cash_balance(paper_account_id: int) -> float:
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS cash FROM options_cash_ledger WHERE paper_account_id = ?",
            (paper_account_id,),
        ).fetchone()
    return round(float(row["cash"]), 2) if row else 0.0


def has_options_deposit(paper_account_id: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM options_cash_ledger WHERE paper_account_id = ? AND kind = 'deposit' LIMIT 1",
            (paper_account_id,),
        ).fetchone()
    return row is not None


def options_realized_pnl(paper_account_id: int) -> float:
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM options_positions "
            "WHERE paper_account_id = ? AND status != 'open'",
            (paper_account_id,),
        ).fetchone()
    return round(float(row["pnl"]), 2) if row else 0.0


def open_options_position(paper_account_id: int, scan_id: int, pos: dict[str, Any]) -> int:
    """Open a position and debit its premium from the ledger atomically.

    pos requires: occ_symbol, underlying, put_call, strike, expiration_date,
    contracts, entry_premium; optional: entry_underlying, entry_delta,
    entry_bid, entry_ask, entry_oi, signal, conviction, rationale, data_source.
    cost_basis is computed here (premium x 100 x contracts).
    """
    contracts = int(pos["contracts"])
    entry_premium = float(pos["entry_premium"])
    cost_basis = round(entry_premium * 100 * contracts, 2)
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """INSERT INTO options_positions (
                       paper_account_id, open_scan_id, occ_symbol, underlying, put_call,
                       strike, expiration_date, contracts, entry_premium, cost_basis,
                       entry_underlying, entry_delta, entry_bid, entry_ask, entry_oi,
                       signal, conviction, rationale, status, opened_at,
                       current_premium, current_value, last_marked_at, price_source, data_source,
                       peak_premium)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)""",
                (paper_account_id, scan_id, pos["occ_symbol"], pos["underlying"], pos["put_call"],
                 float(pos["strike"]), pos["expiration_date"], contracts, entry_premium, cost_basis,
                 pos.get("entry_underlying"), pos.get("entry_delta"), pos.get("entry_bid"),
                 pos.get("entry_ask"), pos.get("entry_oi"),
                 pos.get("signal"), pos.get("conviction"), pos.get("rationale"), now,
                 entry_premium, cost_basis, now, pos.get("data_source"), pos.get("data_source"),
                 entry_premium),
            )
            position_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO options_cash_ledger (paper_account_id, ts, kind, amount, scan_id, position_id, note) "
                "VALUES (?, ?, 'open', ?, ?, ?, ?)",
                (paper_account_id, now, -cost_basis, scan_id, position_id,
                 f"open {pos['occ_symbol']} x{contracts}"),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return position_id


def close_options_position(
    position_id: int,
    exit_premium: float,
    exit_reason: str,
    close_scan_id: int | None = None,
    exit_underlying: float | None = None,
    exit_underlying_source: str | None = None,
    closed_at: str | None = None,
) -> bool:
    """Close an open position at exit_premium and credit proceeds atomically.

    Returns False (writing nothing) if the position is not open — safe to call
    from concurrent paths. exit_underlying (spot at exit, source 'live' when
    captured from the scan's fresh chain data) feeds the learning loop's
    directional-vs-decay attribution; NULL rows get a nightly 'eod_close'
    backfill (web/options_learning.py).

    closed_at: optional backdated exit time (UTC ISO ...Z) — the intraday stop
    backtracks to the minute the premium crossed the stop level. The LEDGER
    row always stamps wall-clock now: books record when the entry was made,
    the position records when the fill happened.
    """
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    closed_at = closed_at or now
    exit_premium = round(float(exit_premium), 4)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT paper_account_id, contracts, cost_basis, occ_symbol FROM options_positions "
                "WHERE id = ? AND status = 'open'",
                (position_id,),
            ).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                return False
            exit_value = round(exit_premium * 100 * int(row["contracts"]), 2)
            realized = round(exit_value - float(row["cost_basis"]), 2)
            conn.execute(
                """UPDATE options_positions
                   SET status = 'closed', closed_at = ?, exit_premium = ?, exit_value = ?,
                       realized_pnl = ?, exit_reason = ?, close_scan_id = ?,
                       current_premium = ?, current_value = ?, last_marked_at = ?,
                       exit_underlying = ?, exit_underlying_source = ?
                   WHERE id = ? AND status = 'open'""",
                (closed_at, exit_premium, exit_value, realized, exit_reason, close_scan_id,
                 exit_premium, exit_value, now,
                 exit_underlying, exit_underlying_source if exit_underlying is not None else None,
                 position_id),
            )
            conn.execute(
                "INSERT INTO options_cash_ledger (paper_account_id, ts, kind, amount, scan_id, position_id, note) "
                "VALUES (?, ?, 'close', ?, ?, ?, ?)",
                (int(row["paper_account_id"]), now, exit_value, close_scan_id, position_id,
                 f"close {row['occ_symbol']} ({exit_reason})"),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return True


def settle_options_position(
    position_id: int,
    intrinsic: float,
    settlement_close: float,
) -> bool:
    """Settle an expired position at intrinsic value (0 => expired worthless).

    Models OCC auto-exercise: ITM by >= $0.01 settles at intrinsic computed from
    the underlying's close; anything less expires worthless. Idempotent via the
    status = 'open' guard.
    """
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    intrinsic = max(0.0, round(float(intrinsic), 4))
    itm = intrinsic >= 0.01
    status = OPTION_POSITION_EXPIRED_ITM if itm else OPTION_POSITION_EXPIRED_WORTHLESS
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT paper_account_id, contracts, cost_basis, occ_symbol FROM options_positions "
                "WHERE id = ? AND status = 'open'",
                (position_id,),
            ).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                return False
            exit_premium = intrinsic if itm else 0.0
            exit_value = round(exit_premium * 100 * int(row["contracts"]), 2)
            realized = round(exit_value - float(row["cost_basis"]), 2)
            conn.execute(
                """UPDATE options_positions
                   SET status = ?, closed_at = ?, exit_premium = ?, exit_value = ?,
                       realized_pnl = ?, exit_reason = 'expiry', settlement_close = ?,
                       current_premium = ?, current_value = ?, last_marked_at = ?
                   WHERE id = ? AND status = 'open'""",
                (status, now, exit_premium, exit_value, realized, float(settlement_close),
                 exit_premium, exit_value, now, position_id),
            )
            # Zero-amount rows for worthless expiries are kept as audit records.
            conn.execute(
                "INSERT INTO options_cash_ledger (paper_account_id, ts, kind, amount, scan_id, position_id, note) "
                "VALUES (?, ?, 'expire', ?, NULL, ?, ?)",
                (int(row["paper_account_id"]), now, exit_value, position_id,
                 f"expire {row['occ_symbol']} ({status})"),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return True


def list_options_positions(
    paper_account_id: int | None = None,
    status: str | None = None,
    open_scan_id: int | None = None,
    close_scan_id: int | None = None,
) -> list[dict[str, Any]]:
    """status filter: 'open' | 'closed' | 'expired_itm' | 'expired_worthless'
    | 'settled' (any non-open) | None (all)."""
    conditions: list[str] = []
    params: list[Any] = []
    if paper_account_id is not None:
        conditions.append("paper_account_id = ?")
        params.append(paper_account_id)
    if status == "settled":
        conditions.append("status != 'open'")
    elif status is not None:
        conditions.append("status = ?")
        params.append(status)
    if open_scan_id is not None:
        conditions.append("open_scan_id = ?")
        params.append(open_scan_id)
    if close_scan_id is not None:
        conditions.append("close_scan_id = ?")
        params.append(close_scan_id)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM options_positions" + where + " ORDER BY id DESC",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_options_position(position_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM options_positions WHERE id = ?", (position_id,)
        ).fetchone()
    return dict(row) if row else None


def mark_options_position(
    position_id: int,
    premium: float,
    value: float,
    price_source: str,
    reset_stale: bool = True,
) -> None:
    """Record a mark-to-market price on an open position (read-only w.r.t. cash)."""
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    stale_sql = ", stale_count = 0" if reset_stale else ", stale_count = stale_count + 1"
    with connect() as conn:
        conn.execute(
            "UPDATE options_positions SET current_premium = ?, current_value = ?, "
            "peak_premium = MAX(COALESCE(peak_premium, entry_premium), ?), "
            "last_marked_at = ?, price_source = ?" + stale_sql + " WHERE id = ? AND status = 'open'",
            (round(float(premium), 4), round(float(value), 2), round(float(premium), 4),
             now, price_source, position_id),
        )


def arm_options_stop_limit(position_id: int, when: str | None = None) -> bool:
    """Mark a stop-limit trigger that could not fill yet.

    The next refresh fills it at the limit price. Idempotent: only the FIRST
    arming timestamp is kept.
    """
    stamp = when or (datetime.utcnow().isoformat(timespec="seconds") + "Z")
    with connect() as conn:
        cur = conn.execute(
            "UPDATE options_positions SET stop_triggered_at = ? "
            "WHERE id = ? AND status = 'open' AND stop_triggered_at IS NULL",
            (stamp, position_id),
        )
    return cur.rowcount > 0


def bump_options_position_stale(position_id: int) -> int:
    """Increment the stale counter (quote unavailable); returns the new count."""
    with connect() as conn:
        conn.execute(
            "UPDATE options_positions SET stale_count = COALESCE(stale_count, 0) + 1 "
            "WHERE id = ? AND status = 'open'",
            (position_id,),
        )
        row = conn.execute(
            "SELECT stale_count FROM options_positions WHERE id = ?", (position_id,)
        ).fetchone()
    return int(row["stale_count"]) if row and row["stale_count"] is not None else 0


# ---------- options lessons (learning loop) ----------
# Written by the nightly grading job (web/scheduler.py::job_options_grade via
# web/options_learning.py); read at scan time to inject the latest lessons +
# track-record stats into the allocator prompt.


def set_options_exit_underlying(position_id: int, value: float, source: str) -> None:
    """Backfill the underlying spot at exit on a non-open position.

    Guarded on exit_underlying IS NULL so a later backfill can never overwrite
    a 'live' capture taken at close time with a less precise EOD close.
    """
    with connect() as conn:
        conn.execute(
            "UPDATE options_positions SET exit_underlying = ?, exit_underlying_source = ? "
            "WHERE id = ? AND status != 'open' AND exit_underlying IS NULL",
            (round(float(value), 4), source, position_id),
        )


def insert_options_lesson(
    paper_account_id: int,
    n_closed_total: int,
    n_new: int,
    stats_json: str,
    lessons_md: str,
    model: str | None = None,
) -> int:
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO options_lessons (paper_account_id, created_at, n_closed_total, "
            "n_new, stats_json, lessons_md, model) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (paper_account_id, now, n_closed_total, n_new, stats_json, lessons_md, model),
        )
        return int(cur.lastrowid)


def latest_options_lesson(paper_account_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM options_lessons WHERE paper_account_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (paper_account_id,),
        ).fetchone()
    return dict(row) if row else None


def count_closed_options_since(paper_account_id: int, closed_after_iso: str | None) -> int:
    """Non-open positions closed after the given UTC ISO timestamp (all when None).

    Drives the 'enough NEW closes to reflect on' gate — lessons regenerate only
    when new outcomes exist, never by rewriting themselves over the same data.
    """
    with connect() as conn:
        if closed_after_iso:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM options_positions "
                "WHERE paper_account_id = ? AND status != 'open' AND closed_at > ?",
                (paper_account_id, closed_after_iso),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM options_positions "
                "WHERE paper_account_id = ? AND status != 'open'",
                (paper_account_id,),
            ).fetchone()
    return int(row["n"]) if row else 0


def _serialize(obj: Any) -> Any:
    """Best-effort conversion of graph state to JSON-safe primitives.

    Handles pydantic v2 (.model_dump) and v1 (.dict) objects that LangChain
    leaves in the state; anything still unrecognized is stringified rather
    than raising — losing fidelity beats losing the whole analysis row.
    """
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    if hasattr(obj, "model_dump"):
        try:
            return _serialize(obj.model_dump())
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return _serialize(obj.dict())
        except Exception:
            pass
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)
