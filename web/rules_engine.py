"""Rules-only options paper account (the strategy lab's "core plan") — no LLM anywhere.

Landon (10/06/2026): run the strategy lab's core plan on the desk "without having any AI make
the trades". Every decision below is a fixed rule copied from the backtest
(~/Projects/strategy-sandbox, frozen rule `frozen/pead_calls_rule_2026-10-04.md`, signals
`pead` + `congress_any` in `sandbox/plan.py`, account in `sandbox/portfolio.py::run_account`,
variant "switch moves idle cash only" in `sandbox/core_plan.py`).

Isolation: this module has its OWN tables (rules_*) and never touches paper_accounts,
options_positions, the research scan, the allocators, the stop monitor or any LLM client, so no
AI path can see or trade this account. Paper only: it reads market data (Schwab MCP quotes and
chains, Alpha Vantage earnings and Congress filings) and writes rows; there is no order tool.

Rules
- Signals (prepare() runs each trading morning, ~06:30 ET, from completed sessions only; the
  trade happens that afternoon or later):
  * pead: a quarterly report with surprisePercentage > 0 and close[reaction] > close[prior]
    (prior = last session before the report date, reaction = first session after it). Signal day
    = reaction day; entry = the session after. Rank = -|surprise| (biggest first).
  * congress: a Congress filing whose transaction type starts with BUY. Signal day = first
    session after filed_date; entry = the session after. Rank -1 (ties broken by a fixed key).
  All pead signals of a day go ahead of all congress signals.
- Contract (entry day, ~15:45 ET, live chain): CALLs 45-90 days out with bid > 0, ask >= bid and
  a delta; the single expiry closest to 60 days; in it keep (ask-bid)/mid <= 3% (none left = no
  trade); strike with delta nearest 0.50, no trade if |delta-0.50| > 0.10; no trade if a
  scheduled earnings date falls between the entry day and the planned exit (expiry - 7 days).
  Cost per contract = ask x 100 + $0.65.
- Exit (checked once a day near the close, live bid as the close): value = bid x 100 - $0.65
  (floored at 0). The trail arms on the first check with value >= 1.5 x cost (no sell that day);
  then sell on the first check with value <= 0.90 x the best value since arming. Otherwise sell on
  the last session on or before expiry - 7 days at the bid (intrinsic value if unquoted).
- Account: 2% of equity per trade (whole contracts, capped by liquid money; 0 = skip), at most 4
  open per signal type, 1 per stock. Idle money in SPY when SPY closed above its 200-day average
  the previous session, else cash; call entries are taken either way.

Known differences from the backtest (logged, not hidden): entries/exits use ~15:45 ET live
quotes instead of the exact closing quotes; the earnings filter uses SCHEDULED report dates; cash
earns nothing (the backtest paid Fed funds); tickers with a class suffix (BRK-B, BF-B) are skipped.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import db, market_calendar, options_fills

log = logging.getLogger(__name__)

COMMISSION = 0.65
FRACTION = 0.02
MAX_OPEN_PER_SYSTEM = 4
DTE_MIN, DTE_MAX, DTE_TARGET = 45, 90, 60
DELTA_TARGET, DELTA_TOL = 0.50, 0.10
MAX_SPREAD = 0.03
TRAIL_ARM, TRAIL_DROP = 0.50, 0.10
DAYS_LEFT = 7
SYSTEMS = ("pead", "congress")  # priority order
SMA_DAYS = 200
EARNINGS_LOOKBACK_DAYS = 10     # calendar days of recent reports re-checked each night
CONGRESS_LATE_SESSIONS = 10     # filings first seen this many sessions late are logged as 'late'
AV_URL = "https://www.alphavantage.co/query"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    starting_capital REAL NOT NULL,
    created_at TEXT NOT NULL,
    cash REAL NOT NULL,
    spy_shares REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rules_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    system TEXT NOT NULL,
    ticker TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    rank REAL NOT NULL,
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE (account_id, system, ticker, signal_date)
);
CREATE TABLE IF NOT EXISTS rules_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    signal_id INTEGER,
    system TEXT NOT NULL,
    ticker TEXT NOT NULL,
    occ_symbol TEXT NOT NULL,
    strike REAL NOT NULL,
    expiration_date TEXT NOT NULL,
    planned_exit TEXT NOT NULL,
    contracts INTEGER NOT NULL,
    entry_date TEXT NOT NULL,
    entry_bid REAL,
    entry_ask REAL NOT NULL,
    entry_delta REAL,
    entry_underlying REAL,
    cost_per REAL NOT NULL,
    armed INTEGER NOT NULL DEFAULT 0,
    peak_value REAL,
    last_value REAL,
    last_bid REAL,
    last_marked TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    exit_date TEXT,
    exit_bid REAL,
    exit_value REAL,
    exit_reason TEXT,
    proceeds REAL,
    pnl REAL
);
CREATE INDEX IF NOT EXISTS idx_rules_positions_acct ON rules_positions (account_id, status);
CREATE TABLE IF NOT EXISTS rules_marks (
    position_id INTEGER NOT NULL,
    mark_date TEXT NOT NULL,
    bid REAL,
    ask REAL,
    delta REAL,
    value REAL,
    source TEXT,
    PRIMARY KEY (position_id, mark_date)
);
CREATE TABLE IF NOT EXISTS rules_daily (
    account_id INTEGER NOT NULL,
    run_date TEXT NOT NULL,
    switch_on INTEGER NOT NULL,
    spy_price REAL NOT NULL,
    spy_shares REAL NOT NULL,
    cash REAL NOT NULL,
    open_value REAL NOT NULL,
    equity REAL NOT NULL,
    n_open INTEGER NOT NULL,
    entries INTEGER NOT NULL,
    exits INTEGER NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, run_date)
);
CREATE TABLE IF NOT EXISTS rules_calendar (
    ticker TEXT NOT NULL,
    report_date TEXT NOT NULL,
    fetched_on TEXT NOT NULL,
    PRIMARY KEY (ticker, report_date)
);
CREATE TABLE IF NOT EXISTS rules_earnings_seen (
    ticker TEXT NOT NULL,
    reported_date TEXT NOT NULL,
    surprise_pct REAL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (ticker, reported_date)
);
CREATE TABLE IF NOT EXISTS rules_congress_seen (
    ticker TEXT NOT NULL,
    filing_key TEXT NOT NULL,
    filed_date TEXT,
    transaction_type TEXT,
    bioguide_id TEXT,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (ticker, filing_key)
);
CREATE TABLE IF NOT EXISTS rules_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    summary TEXT
);
"""

_RUN_LOCK = threading.Lock()
_PREPARE_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_tables() -> None:
    with db.connect() as conn:
        conn.executescript(SCHEMA)


# ── Trading calendar ─────────────────────────────────────────────────────────

def next_session(d: date) -> date:
    d += timedelta(days=1)
    while not market_calendar.is_trading_day(d):
        d += timedelta(days=1)
    return d


def session_on_or_before(d: date) -> date:
    while not market_calendar.is_trading_day(d):
        d -= timedelta(days=1)
    return d


def sessions_between(a: date, b: date) -> int:
    """Trading sessions in (a, b]."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        n += market_calendar.is_trading_day(d)
    return n


# ── Pure rule functions (unit-tested) ────────────────────────────────────────

def pead_signal(reported: date, surprise: float | None, closes: dict[date, float]) -> tuple[date | None, str]:
    """(signal_day, '') when the report is a beat followed by an up move, else (None, reason).

    closes: final daily closes by session date (only sessions already closed)."""
    if surprise is None or not surprise > 0:
        return None, "not a beat"
    before = [d for d in closes if d < reported]
    after = sorted(d for d in closes if d > reported)
    if not before:
        return None, "no close before the report"
    if not after:
        return None, "reaction session not closed yet"
    prior, reaction = max(before), after[0]
    if closes[reaction] > closes[prior]:
        return reaction, ""
    return None, "stock did not rise"


def congress_days(filed: date) -> tuple[date, date]:
    """(signal_day, entry_day): the session after the filing date, and the session after that."""
    signal = next_session(filed)
    return signal, next_session(signal)


def pick_contract(cands: list[dict[str, Any]], entry: date,
                  earnings_dates: list[date]) -> tuple[dict[str, Any] | None, str]:
    """Frozen contract rule. cands: normalized CALL candidates (options_data._candidate shape)."""
    usable = [c for c in cands
              if c.get("put_call") == "CALL" and DTE_MIN <= int(c.get("dte") or -1) <= DTE_MAX
              and isinstance(c.get("bid"), (int, float)) and c["bid"] > 0
              and isinstance(c.get("ask"), (int, float)) and c["ask"] >= c["bid"]
              and c.get("delta") is not None]
    if not usable:
        return None, "no usable quotes"
    # single expiry closest to 60 days; a tie goes to the earlier expiry
    best = min(sorted({int(c["dte"]) for c in usable}), key=lambda d: abs(d - DTE_TARGET))
    pool = [c for c in usable if int(c["dte"]) == best]
    pool = [c for c in pool if (c["ask"] - c["bid"]) / ((c["ask"] + c["bid"]) / 2) <= MAX_SPREAD]
    if not pool:
        return None, "spread over 3%"
    pick = min(sorted(pool, key=lambda c: c["strike"]), key=lambda c: abs(c["delta"] - DELTA_TARGET))
    if abs(pick["delta"] - DELTA_TARGET) > DELTA_TOL:
        return None, "no contract near delta 0.50"
    planned = date.fromisoformat(pick["expiration_date"]) - timedelta(days=DAYS_LEFT)
    if any(entry <= e <= planned for e in earnings_dates):
        return None, "earnings before exit"
    return pick, ""


def cost_per(ask: float) -> float:
    return ask * 100 + COMMISSION


def value_per(bid: float) -> float:
    return max(bid * 100 - COMMISSION, 0.0)


def size(equity: float, liquid: float, cost: float) -> int:
    n = math.floor(FRACTION * equity / cost)
    return max(0, min(n, math.floor(liquid / cost)))


def exit_decision(armed: bool, peak: float | None, value: float, cost: float) -> tuple[bool, float | None, bool]:
    """One daily trail check on a quoted day. Returns (armed, peak, sell)."""
    if not armed:
        if value >= (1 + TRAIL_ARM) * cost:
            return True, value, False  # arms; no sell check on the arming day
        return False, None, False
    peak = max(peak or value, value)
    return True, peak, value <= peak * (1 - TRAIL_DROP)


def tie_key(ticker: str, entry: date) -> int:
    # same tie key as the backtest: crc32 of "TICKER|YYYY-MM-DD 00:00:00"
    return zlib.crc32(f"{ticker}|{entry.isoformat()} 00:00:00".encode())


def switch_on(spy_closes: dict[date, float], today: date) -> bool | None:
    """SPY closed above its 200-day average on the last session BEFORE today."""
    past = sorted(d for d in spy_closes if d < today)
    if len(past) < SMA_DAYS:
        return None
    last = past[-SMA_DAYS:]
    sma = sum(spy_closes[d] for d in last) / SMA_DAYS
    return spy_closes[past[-1]] > sma


# ── Data feeds (replaceable in tests) ────────────────────────────────────────

@dataclass
class Feeds:
    sp500: Callable[[], list[str]]
    av: Callable[..., Any]
    daily_closes: Callable[[str], dict[date, float]]
    call_chain: Callable[[str, date], list[dict[str, Any]]]
    quotes: Callable[[list[str]], dict[str, dict[str, Any]]]


class _AvClient:
    """Alpha Vantage with a per-minute cap (the key is shared with Quant's backfill)."""

    def __init__(self, per_min: float | None = None):
        self.per_min = per_min or float(os.environ.get("RULES_AV_PER_MIN", "6"))
        self._last = 0.0

    def __call__(self, function: str, **params: Any) -> Any:
        import requests
        key = os.environ.get("ALPHA_VANTAGE_API_KEY")
        if not key:
            raise RuntimeError("ALPHA_VANTAGE_API_KEY is not set (dashboard settings)")
        for attempt in range(3):
            wait = 60.0 / self.per_min - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            r = requests.get(AV_URL, params={"function": function, "apikey": key, **params}, timeout=60)
            r.raise_for_status()
            if params.get("datatype") == "csv" or function == "EARNINGS_CALENDAR":
                if r.text.lstrip().startswith("{"):
                    data = r.json()
                else:
                    return r.text
            else:
                data = r.json()
            if isinstance(data, dict) and ("Note" in data or "Information" in data) and len(data) == 1:
                log.warning("[rules] Alpha Vantage throttled (%s); backing off", function)
                time.sleep(30 * (attempt + 1))
                continue
            return data
        raise RuntimeError(f"Alpha Vantage kept throttling {function}")


def _candle_date(raw: Any) -> date:
    """Schwab MCP candles carry an ISO string ('2026-10-05T05:00:00.000Z' = that session) or epoch ms."""
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000, market_calendar._ET).date()
    return date.fromisoformat(str(raw)[:10])


def _schwab_closes(symbol: str) -> dict[date, float]:
    """Final daily closes of COMPLETED sessions (before today); signals are prepared the next
    morning, so the latest needed close is always yesterday's."""
    from tradingagents.dataflows import schwab_mcp
    data = schwab_mcp.get_price_history(symbol, period_type="year", period=2) or {}
    today = market_calendar.today_et()
    out: dict[date, float] = {}
    for c in data.get("candles") or []:
        d = _candle_date(c.get("datetime"))
        if d < today and _num(c.get("close")):
            out[d] = float(c["close"])
    return out


def _schwab_chain(symbol: str, ref: date) -> list[dict[str, Any]]:
    from tradingagents.dataflows import schwab_mcp

    from . import options_data
    payload = schwab_mcp.get_option_chain(symbol, contract_type="CALL", strike_count=40,
                                          from_date=(ref + timedelta(days=DTE_MIN)).isoformat(),
                                          to_date=(ref + timedelta(days=DTE_MAX)).isoformat())
    return options_data.normalize_schwab_chain(payload or {}, symbol, "CALL", ref)


def _schwab_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    from tradingagents.dataflows import schwab_mcp
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(symbols), 50):
        data = schwab_mcp.get_quotes(symbols[i:i + 50]) or {}
        for sym, obj in data.items():
            q = (obj or {}).get("quote") or {}
            out[sym] = {"bid": q.get("bidPrice"), "ask": q.get("askPrice"), "delta": q.get("delta"),
                        "last": q.get("lastPrice") or q.get("mark"),
                        "underlying": q.get("underlyingPrice")}
    return out


def default_feeds() -> Feeds:
    from . import spy_tickers
    return Feeds(sp500=spy_tickers.get_sp500_tickers, av=_AvClient(), daily_closes=_schwab_closes,
                 call_chain=_schwab_chain, quotes=_schwab_quotes)


def _ok_ticker(t: str) -> bool:
    return t.isalpha()


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# ── Accounts ─────────────────────────────────────────────────────────────────

def create_account(name: str, starting_capital: float = 50_000.0) -> int:
    init_tables()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO rules_accounts (name, starting_capital, created_at, cash) VALUES (?, ?, ?, ?)",
            (name, float(starting_capital), _now(), float(starting_capital)))
        return int(cur.lastrowid)


def list_accounts(active_only: bool = False) -> list[dict[str, Any]]:
    init_tables()
    with db.connect() as conn:
        sql = "SELECT * FROM rules_accounts" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
        return [dict(r) for r in conn.execute(sql)]


# ── Nightly signal preparation ───────────────────────────────────────────────

def _insert_signal(conn, account_id: int, system: str, ticker: str, signal: date, entry: date,
                   rank: float, detail: dict[str, Any], today: date) -> None:
    status = "pending" if entry >= today else "late"
    reason = None if status == "pending" else "data arrived after the entry day"
    conn.execute(
        "INSERT OR IGNORE INTO rules_signals (account_id, system, ticker, signal_date, entry_date, rank, detail, "
        "status, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, system, ticker, signal.isoformat(), entry.isoformat(), rank, json.dumps(detail),
         status, reason, _now()))


def prepare(feeds: Feeds | None = None, today: date | None = None,
            congress: bool = True) -> dict[str, Any]:
    """Each trading morning: refresh the earnings calendar, find beats with an up move, and new
    Congress buy filings, using completed sessions only. Writes pending signals whose entry day is
    today or later; anything whose entry day already passed is logged as 'late'. Idempotent."""
    feeds = feeds or default_feeds()
    today = today or market_calendar.today_et()
    init_tables()
    accounts = list_accounts(active_only=True)
    if not accounts:
        return {"skipped": "no rules accounts"}
    if not _PREPARE_LOCK.acquire(blocking=False):
        return {"skipped": "prepare already running"}
    with db.connect() as conn:
        run_id = conn.execute("INSERT INTO rules_runs (kind, started_at, status) VALUES ('prepare', ?, 'running')",
                              (_now(),)).lastrowid
    out: dict[str, Any] = {"pead": 0, "congress": 0, "late": 0, "errors": []}
    try:
        universe = sorted({t for t in feeds.sp500() if _ok_ticker(t)})
        uni = set(universe)
        # 1. earnings calendar (one call): who reports, and the forward dates for the exit filter
        try:
            cal = feeds.av("EARNINGS_CALENDAR", horizon="3month")
        except Exception as exc:  # keep going: Congress signals don't need the calendar
            log.exception("[rules] earnings calendar failed")
            out["errors"].append(f"earnings calendar: {exc}")
            cal = None
        rows = list(csv.DictReader(io.StringIO(cal))) if isinstance(cal, str) else []
        with db.connect() as conn:
            if rows:  # forward dates move: replace every future date with today's list
                conn.execute("DELETE FROM rules_calendar WHERE report_date >= ?", (today.isoformat(),))
            for r in rows:
                sym = (r.get("symbol") or "").strip().upper()
                if sym in uni and r.get("reportDate"):
                    conn.execute("INSERT OR REPLACE INTO rules_calendar (ticker, report_date, fetched_on) VALUES (?, ?, ?)",
                                 (sym, r["reportDate"], today.isoformat()))
            recent = [r["ticker"] for r in conn.execute(
                "SELECT DISTINCT ticker FROM rules_calendar WHERE report_date BETWEEN ? AND ?",
                ((today - timedelta(days=EARNINGS_LOOKBACK_DAYS)).isoformat(), today.isoformat()))]
        out["calendar_rows"] = len(rows)
        # 2. earnings beats followed by an up move
        for t in recent:
            try:
                data = feeds.av("EARNINGS", symbol=t)
                q = (data or {}).get("quarterlyEarnings") or []
                closes = None
                seen: set[str] = set()
                for e in q:
                    rd = e.get("reportedDate")
                    if not rd or rd in seen:
                        continue
                    seen.add(rd)  # duplicate report dates: keep the first
                    reported = date.fromisoformat(rd)
                    if reported < today - timedelta(days=EARNINGS_LOOKBACK_DAYS) or reported > today:
                        continue
                    surprise = _num(e.get("surprisePercentage"))
                    with db.connect() as conn:
                        conn.execute("INSERT OR IGNORE INTO rules_earnings_seen (ticker, reported_date, surprise_pct, first_seen) "
                                     "VALUES (?, ?, ?, ?)", (t, rd, surprise, _now()))
                    if surprise is None or surprise <= 0:
                        continue
                    if closes is None:
                        closes = feeds.daily_closes(t)
                    signal, why = pead_signal(reported, surprise, closes)
                    if signal is None:
                        continue
                    entry = next_session(signal)
                    with db.connect() as conn:
                        for a in accounts:
                            _insert_signal(conn, a["id"], "pead", t, signal, entry, -abs(surprise),
                                           {"reported_date": rd, "surprise_pct": surprise,
                                            "report_time": e.get("reportTime")}, today)
                    out["pead"] += 1
            except Exception as exc:  # one bad ticker never stops the night
                log.exception("[rules] earnings check failed for %s", t)
                out["errors"].append(f"earnings {t}: {exc}")
        # 3. Congress buy filings (one call per stock, rate-limited)
        if congress:
            for t in universe:
                try:
                    data = feeds.av("CONGRESS_TRADES", symbol=t)
                    trades = (data or {}).get("data") or (data or {}).get("trades") or []
                    new_days: dict[date, set[str]] = {}
                    with db.connect() as conn:
                        for tr in trades:
                            fk = "|".join(str(tr.get(k) or "") for k in (
                                "bioguide_id", "transaction_date", "filed_date", "transaction_type",
                                "amount_min", "amount_max", "owner_code", "asset_name"))
                            cur = conn.execute(
                                "INSERT OR IGNORE INTO rules_congress_seen (ticker, filing_key, filed_date, "
                                "transaction_type, bioguide_id, first_seen) VALUES (?, ?, ?, ?, ?, ?)",
                                (t, fk, tr.get("filed_date"), tr.get("transaction_type"), tr.get("bioguide_id"), _now()))
                            if cur.rowcount == 0:
                                continue
                            if not str(tr.get("transaction_type") or "").upper().startswith("BUY"):
                                continue
                            try:
                                filed = date.fromisoformat(str(tr.get("filed_date"))[:10])
                            except ValueError:
                                continue
                            signal, entry = congress_days(filed)
                            if sessions_between(entry, today) > CONGRESS_LATE_SESSIONS:
                                out["congress_old"] = out.get("congress_old", 0) + 1
                                continue  # an old filing surfacing now: not a tradeable signal
                            new_days.setdefault(signal, set()).add(str(tr.get("bioguide_id") or ""))
                        for signal, members in new_days.items():
                            entry = next_session(signal)
                            for a in accounts:
                                _insert_signal(conn, a["id"], "congress", t, signal, entry, -1.0,
                                               {"members": sorted(members)}, today)
                            out["congress"] += 1
                except Exception as exc:
                    log.exception("[rules] Congress check failed for %s", t)
                    out["errors"].append(f"congress {t}: {exc}")
        with db.connect() as conn:
            out["late"] = conn.execute("SELECT COUNT(*) FROM rules_signals WHERE status = 'late' AND created_at >= ?",
                                       (today.isoformat(),)).fetchone()[0]
        status = "done"
    except Exception as exc:
        log.exception("[rules] prepare failed")
        out["errors"].append(str(exc))
        status = "failed"
    finally:
        _PREPARE_LOCK.release()
    out["errors"] = out["errors"][:50]
    if status != "done" or len(out["errors"]) > 25:
        _alert(f"⚠️ Rules account signal check {status} ({today.isoformat()}), {len(out['errors'])} errors.",
               json.dumps(out, default=str)[:1500])
    with db.connect() as conn:
        conn.execute("UPDATE rules_runs SET finished_at = ?, status = ?, summary = ? WHERE id = ?",
                     (_now(), status, json.dumps(out), run_id))
    return out


# ── Daily run (exits, entries, idle money) ───────────────────────────────────

def run_daily(feeds: Feeds | None = None, today: date | None = None, force: bool = False) -> dict[str, Any]:
    """Near the close on each trading day, for every active rules account."""
    feeds = feeds or default_feeds()
    today = today or market_calendar.today_et()
    init_tables()
    if not market_calendar.is_trading_day(today) and not force:
        return {"skipped": "not a trading day"}
    if not _RUN_LOCK.acquire(blocking=False):
        return {"skipped": "run already in progress"}
    with db.connect() as conn:
        run_id = conn.execute("INSERT INTO rules_runs (kind, started_at, status) VALUES ('run', ?, 'running')",
                              (_now(),)).lastrowid
    out: dict[str, Any] = {}
    status = "failed"
    try:
        out = _run_all(feeds, today)
        status = "done" if not any("error" in r for r in out["accounts"].values()) else "failed"
        return out
    except Exception as exc:
        log.exception("[rules] daily run failed")
        out = {"error": str(exc)}
        raise
    finally:
        with db.connect() as conn:
            conn.execute("UPDATE rules_runs SET finished_at = ?, status = ?, summary = ? WHERE id = ?",
                         (_now(), status, json.dumps(out, default=str)[:4000], run_id))
        if status != "done":
            _alert(f"⚠️ Rules account daily run failed ({today.isoformat()}); today's signals may be missed.",
                   json.dumps(out, default=str)[:1500])
        _RUN_LOCK.release()


def _alert(summary: str, detail: str) -> None:
    try:
        from . import alerts
        alerts.notify(summary, detail)
    except Exception:
        log.exception("[rules] alert failed")


def _run_all(feeds: Feeds, today: date) -> dict[str, Any]:
    spy_closes = feeds.daily_closes("SPY")
    on = switch_on(spy_closes, today)
    if on is None:
        raise RuntimeError("not enough SPY history for the 200-day average")
    spy_q = feeds.quotes(["SPY"]).get("SPY") or {}
    spy_px = _num(spy_q.get("last")) or _num(spy_q.get("bid"))
    if not spy_px:
        raise RuntimeError("no SPY price")
    results = {}
    for acct in list_accounts(active_only=True):
        try:
            results[acct["name"]] = _run_account(acct, feeds, today, on, spy_px)
        except Exception as exc:  # one account failing must not block the others
            log.exception("[rules] account %s failed", acct["name"])
            results[acct["name"]] = {"error": str(exc)}
    return {"date": today.isoformat(), "switch_on": on, "spy": spy_px, "accounts": results}


def _calendar_dates(ticker: str) -> list[date]:
    with db.connect() as conn:
        return [date.fromisoformat(r[0]) for r in conn.execute(
            "SELECT report_date FROM rules_calendar WHERE ticker = ?", (ticker,))]


def _run_account(acct: dict[str, Any], feeds: Feeds, today: date, on: bool, spy_px: float) -> dict[str, Any]:
    aid, iso = acct["id"], today.isoformat()
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM rules_daily WHERE account_id = ? AND run_date = ?", (aid, iso)).fetchone():
            return {"skipped": "already ran today"}  # one run per day, even when forced
        positions = [dict(r) for r in conn.execute(
            "SELECT * FROM rules_positions WHERE account_id = ? AND status = 'open'", (aid,))]
        signals = [dict(r) for r in conn.execute(
            "SELECT * FROM rules_signals WHERE account_id = ? AND status = 'pending' AND entry_date <= ?", (aid, iso))]
    liquid = acct["cash"] + acct["spy_shares"] * spy_px
    notes: list[str] = []
    marks: list[tuple] = []
    closed: list[dict[str, Any]] = []
    quotes = feeds.quotes([p["occ_symbol"] for p in positions] + sorted({p["ticker"] for p in positions})) if positions else {}

    # 1. exits
    for p in positions:
        q = quotes.get(p["occ_symbol"]) or {}
        bid, ask = options_fills.quote_bid(q.get("bid"), q.get("ask")), _num(q.get("ask"))
        last_session = session_on_or_before(date.fromisoformat(p["planned_exit"]))
        quoted = bid is not None  # 0/0 (empty quote) counts as unquoted, never as a $0 sale
        value = value_per(bid) if quoted else None
        if quoted:
            marks.append((p["id"], iso, bid, ask, _num(q.get("delta")), value, "quote"))
        sell, reason = False, None
        if quoted:
            armed, peak, hit = exit_decision(bool(p["armed"]), p["peak_value"], value, p["cost_per"])
            p["armed"], p["peak_value"] = int(armed), peak
            if hit:
                sell, reason = True, "trailing_stop"
        if not sell and today >= last_session:
            if not quoted:
                und = _num((quotes.get(p["ticker"]) or {}).get("last"))
                if und is None:
                    notes.append(f"{p['ticker']}: no option or stock quote on its exit day; retrying next run")
                    continue
                value = max((und - p["strike"]) * 100 - COMMISSION, 0.0)
                bid = None
                marks.append((p["id"], iso, None, None, None, value, "intrinsic"))
            sell, reason = True, "time_exit"
        if quoted:
            p["last_value"], p["last_bid"], p["last_marked"] = value, bid, iso
        if sell:
            proceeds = value * p["contracts"]
            liquid += proceeds
            p.update(status="closed", exit_date=iso, exit_bid=bid, exit_value=value, exit_reason=reason,
                     proceeds=proceeds, pnl=proceeds - p["cost_per"] * p["contracts"])
            closed.append(p)
    still_open = [p for p in positions if p["status"] == "open"]

    # 2. entries: pead first, then congress; rank; fixed tie key
    order = {s: i for i, s in enumerate(SYSTEMS)}
    todays, decided = [], []
    for s in signals:
        if s["entry_date"] < iso:
            decided.append((s["id"], "missed", "the desk did not run on the entry day"))
        else:
            todays.append(s)
    todays.sort(key=lambda s: (order[s["system"]], s["rank"], tie_key(s["ticker"], today)))
    opened: list[dict[str, Any]] = []

    def equity_now() -> float:
        return liquid + sum(p["contracts"] * (p["last_value"] if p["last_value"] is not None else p["cost_per"])
                            for p in still_open + opened)

    for s in todays:
        live = still_open + opened
        if sum(p["system"] == s["system"] for p in live) >= MAX_OPEN_PER_SYSTEM:
            decided.append((s["id"], "skipped", "no open slot")); continue
        if any(p["ticker"] == s["ticker"] for p in live):
            decided.append((s["id"], "skipped", "already holding this stock")); continue
        try:
            cands = feeds.call_chain(s["ticker"], today)
        except Exception as exc:
            decided.append((s["id"], "skipped", f"chain fetch failed: {exc}"[:200])); continue
        pick, why = pick_contract(cands, today, _calendar_dates(s["ticker"]))
        if pick is None:
            decided.append((s["id"], "skipped", why)); continue
        c = cost_per(pick["ask"])
        n = size(equity_now(), liquid, c)
        if n <= 0:
            decided.append((s["id"], "skipped", f"too expensive (${c:,.0f} a contract)")); continue
        liquid -= n * c
        v = value_per(pick["bid"])
        pos = {"account_id": aid, "signal_id": s["id"], "system": s["system"], "ticker": s["ticker"],
               "occ_symbol": pick["occ_symbol"], "strike": pick["strike"], "expiration_date": pick["expiration_date"],
               "planned_exit": (date.fromisoformat(pick["expiration_date"]) - timedelta(days=DAYS_LEFT)).isoformat(),
               "contracts": n, "entry_date": iso, "entry_bid": pick["bid"], "entry_ask": pick["ask"],
               "entry_delta": pick["delta"], "entry_underlying": pick.get("underlying_price"), "cost_per": c,
               "armed": 0, "peak_value": None, "last_value": v, "last_bid": pick["bid"], "last_marked": iso,
               "status": "open"}
        opened.append(pos)
        decided.append((s["id"], "opened", f"{n} x {pick['occ_symbol'].strip()} at ask {pick['ask']:.2f}"))

    # 3. idle money: SPY when the switch is on, cash otherwise
    spy_shares, cash = (liquid / spy_px, 0.0) if on else (0.0, liquid)
    open_value = sum(p["contracts"] * (p["last_value"] if p["last_value"] is not None else p["cost_per"])
                     for p in still_open + opened)
    equity = liquid + open_value

    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for p in positions:
                conn.execute("UPDATE rules_positions SET armed=?, peak_value=?, last_value=?, last_bid=?, last_marked=?, "
                             "status=?, exit_date=?, exit_bid=?, exit_value=?, exit_reason=?, proceeds=?, pnl=? WHERE id=?",
                             (p["armed"], p["peak_value"], p["last_value"], p["last_bid"], p["last_marked"], p["status"],
                              p.get("exit_date"), p.get("exit_bid"), p.get("exit_value"), p.get("exit_reason"),
                              p.get("proceeds"), p.get("pnl"), p["id"]))
            for pos in opened:
                cols = list(pos)
                cur = conn.execute(f"INSERT INTO rules_positions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                   [pos[k] for k in cols])
                marks.append((cur.lastrowid, iso, pos["entry_bid"], pos["entry_ask"], pos["entry_delta"],
                              pos["last_value"], "entry"))
            conn.executemany("INSERT OR REPLACE INTO rules_marks VALUES (?, ?, ?, ?, ?, ?, ?)", marks)
            conn.executemany("UPDATE rules_signals SET status = ?, reason = ?, decided_at = ? WHERE id = ?",
                             [(st, why, _now(), sid) for sid, st, why in decided])
            conn.execute("UPDATE rules_accounts SET cash = ?, spy_shares = ? WHERE id = ?", (cash, spy_shares, aid))
            conn.execute("INSERT INTO rules_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (aid, iso, int(on), spy_px, spy_shares, cash, open_value, equity,
                          len(still_open) + len(opened), len(opened), len(closed), json.dumps(notes), _now()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"equity": round(equity, 2), "opened": len(opened), "closed": len(closed),
            "skipped": sum(st == "skipped" for _, st, _ in decided), "notes": notes}


# ── Read side ────────────────────────────────────────────────────────────────

def summary() -> dict[str, Any]:
    init_tables()
    out = []
    with db.connect() as conn:
        for a in list_accounts():
            aid = a["id"]
            daily = [dict(r) for r in conn.execute(
                "SELECT run_date, equity, spy_price, switch_on, n_open, entries, exits, notes FROM rules_daily "
                "WHERE account_id = ? ORDER BY run_date", (aid,))]
            pos = [dict(r) for r in conn.execute(
                "SELECT * FROM rules_positions WHERE account_id = ? ORDER BY status DESC, entry_date DESC, id DESC", (aid,))]
            sig = [dict(r) for r in conn.execute(
                "SELECT system, ticker, signal_date, entry_date, status, reason, detail FROM rules_signals "
                "WHERE account_id = ? ORDER BY entry_date DESC, id DESC LIMIT 200", (aid,))]
            last = daily[-1] if daily else None
            first = daily[0] if daily else None
            equity = last["equity"] if last else a["starting_capital"]
            spy_bh = (a["starting_capital"] * last["spy_price"] / first["spy_price"]) if last else a["starting_capital"]
            peak, worst = 0.0, 0.0
            for d in daily:
                peak = max(peak, d["equity"], a["starting_capital"])
                worst = max(worst, 1 - d["equity"] / peak)
            closed = [p for p in pos if p["status"] == "closed"]
            out.append({"account": a, "equity": equity, "spy_buy_hold": spy_bh, "worst_drop_pct": 100 * worst,
                        "closed_trades": len(closed), "wins": sum((p["pnl"] or 0) > 0 for p in closed),
                        "realized_pnl": sum(p["pnl"] or 0 for p in closed), "positions": pos,
                        "signals": sig, "daily": daily})
        runs = [dict(r) for r in conn.execute("SELECT * FROM rules_runs ORDER BY id DESC LIMIT 10")]
    return {"accounts": out, "runs": runs}
