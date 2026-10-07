"""Rules-only credit-spread paper accounts (iron condors or credit verticals) filled from LIVE quotes.

Landon (10/07/2026): run the Option Alpha-style rule on the desk, "have schwab stream the quotes for
the options that are chosen", "you dont do market orders, you set your price and wait", and "a
version that just does verticals". The strategy-lab backtest (2016-2026, real closing quotes) found
this rule profitable ONLY when fills land at/near the midpoint, so this account exists to measure
real fill quality and real P&L. No LLM anywhere; paper only (no order tools exist on this path).

Rule (each account has a `structure`: 'condor' or 'vertical'):
- Scan (10:00 ET trading days): S&P 500 + a liquid ETF list; Friday expiries 3-45 days out; Schwab
  chain per ticker. Condor = OTM short put + OTM short call with equal-width wings; vertical = an
  OTM credit put spread or credit call spread. P(max profit) from each short strike's own implied
  volatility (lognormal N(d2)): condor = P(between the shorts), put spread = P(above the short put),
  call spread = P(below the short call). Keep P >= 55% AND mid credit / (width - mid credit) >= 1.5.
  No earnings report before expiry. Best candidate per ticker by the model's expected value; up to
  MAX_NEW_PER_DAY new orders a day, best first.
- Size: max loss per spread <= 5% of account equity (whole contracts); all open + working max loss
  <= 50% of equity; <= 10% of equity per underlying (several spreads per stock allowed).
- Entry: a net-credit LIMIT at the mid ("set your price and wait"). The quote loop fills it only when
  the live natural credit (short bids - long asks) reaches the limit, at the limit. Every 10 minutes
  unfilled, the limit walks 1 cent toward the natural price but never below the price that keeps
  reward/risk at 1.5. Not filled by the close -> re-priced to the new mid next morning; dropped after
  3 trading days.
- Exit: a resting buy-to-close limit at 60% of the credit (40% kept), filled when the live natural
  cost to close (short asks - long bids) reaches it. No stop. Spreads still open after their expiry
  settle at intrinsic value against the underlying's price.
- $0.65 per contract per leg each way.
Quotes: Schwab MCP getQuotes every ~3 s for every leg of every working order and open spread
(version 1 polls; a Schwab Streamer websocket can replace `fetch_quotes` once the desk holds its own
Schwab OAuth tokens). Quotes older than 20 s, crossed, or missing are ignored.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import db, market_calendar

log = logging.getLogger(__name__)

COMMISSION = 0.65
P_MIN, RR_MIN, TP_KEEP = 0.55, 1.5, 0.40
DTE_MIN, DTE_MAX = 3, 45
RISK_PER, RISK_TOTAL, RISK_PER_TICKER = 0.05, 0.50, 0.10
MAX_NEW_PER_DAY = 10
ENTRY_DAYS = 3
WALK_EVERY_S, WALK_STEP = 600, 0.01
POLL_S, STALE_S = 3.0, 20.0
STRIKE_COUNT = 40
ETFS = ["SPY", "QQQ", "IWM", "DIA", "GLD", "TLT", "XLE", "XOP", "XLF", "SMH", "EEM", "GDX", "XBI", "KRE"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS spread_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    structure TEXT NOT NULL,
    starting_capital REAL NOT NULL,
    cash REAL NOT NULL,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS spread_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    structure TEXT NOT NULL,
    legs TEXT NOT NULL,
    expiration_date TEXT NOT NULL,
    width REAL NOT NULL,
    contracts INTEGER NOT NULL,
    pmax REAL,
    model_ev REAL,
    initial_mid REAL NOT NULL,
    initial_natural REAL,
    limit_price REAL NOT NULL,
    floor_price REAL NOT NULL,
    placed_at TEXT NOT NULL,
    last_walk_at TEXT,
    trading_days INTEGER NOT NULL DEFAULT 1,
    reprice INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'working',
    filled_at TEXT,
    fill_price REAL,
    fill_mid REAL,
    fill_natural REAL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_spread_orders_status ON spread_orders (status);
CREATE TABLE IF NOT EXISTS spread_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    structure TEXT NOT NULL,
    legs TEXT NOT NULL,
    expiration_date TEXT NOT NULL,
    width REAL NOT NULL,
    contracts INTEGER NOT NULL,
    credit REAL NOT NULL,
    max_loss REAL NOT NULL,
    tp_price REAL NOT NULL,
    opened_at TEXT NOT NULL,
    last_close_natural REAL,
    last_close_mid REAL,
    last_marked_at TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    closed_at TEXT,
    close_price REAL,
    close_reason TEXT,
    pnl REAL
);
CREATE INDEX IF NOT EXISTS idx_spread_positions_status ON spread_positions (status);
CREATE TABLE IF NOT EXISTS spread_daily (
    account_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    open_cost REAL NOT NULL,
    spy_price REAL,
    PRIMARY KEY (account_id, day)
);
CREATE TABLE IF NOT EXISTS spread_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    summary TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_tables() -> None:
    with db.connect() as conn:
        conn.executescript(SCHEMA)


def _num(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def p_above(spot: float, strike: float, iv: float, dte: int) -> float:
    """Lognormal P(price at expiry > strike), zero drift, the strike's own implied volatility."""
    t = max(dte, 1) / 365
    return _ncdf((math.log(spot / strike) - 0.5 * iv * iv * t) / (iv * math.sqrt(t)))


# ── chain → candidates (pure) ────────────────────────────────────────────────

def parse_chain(payload: dict[str, Any], ref: date) -> tuple[float | None, list[dict[str, Any]]]:
    """Schwab getOptionChain payload -> (spot, contracts with symbol/put_call/strike/expiry/dte/bid/ask/iv)."""
    spot = _num(payload.get("underlyingPrice"))
    out = []
    for key, side in (("callExpDateMap", "CALL"), ("putExpDateMap", "PUT")):
        for exp_key, strikes in (payload.get(key) or {}).items():
            exp = str(exp_key).split(":", 1)[0]
            try:
                exp_d = date.fromisoformat(exp)
            except ValueError:
                continue
            for contracts in (strikes or {}).values():
                c = contracts[0] if isinstance(contracts, list) and contracts else contracts
                if not isinstance(c, dict):
                    continue
                bid, ask, iv, k = _num(c.get("bid")), _num(c.get("ask")), _num(c.get("volatility")), _num(c.get("strikePrice"))
                if None in (bid, ask, iv, k) or bid <= 0 or ask < bid or not 1 < iv < 500 or not c.get("symbol"):
                    continue
                if c.get("nonStandard") or c.get("mini"):
                    continue
                out.append({"symbol": c["symbol"], "put_call": side, "strike": k, "expiration_date": exp,
                            "dte": (exp_d - ref).days, "friday": exp_d.weekday() == 4, "bid": bid, "ask": ask,
                            "mid": (bid + ask) / 2, "iv": iv / 100})
    return spot, out


def _ev(p: float, credit: float, width: float) -> float:
    return p * credit - (1 - p) * (width - credit) * 0.5   # crude model value, ranking only


def scan(contracts: list[dict[str, Any]], spot: float, structure: str) -> dict[str, Any] | None:
    """Best passing spread (by model EV) among Friday expiries 3-45 days out, or None."""
    best = None
    by_exp: dict[str, list] = {}
    for c in contracts:
        if c["friday"] and DTE_MIN <= c["dte"] <= DTE_MAX:
            by_exp.setdefault(c["expiration_date"], []).append(c)
    for exp, cs in by_exp.items():
        dte = cs[0]["dte"]
        puts = sorted((c for c in cs if c["put_call"] == "PUT" and c["strike"] < spot), key=lambda c: c["strike"])
        calls = sorted((c for c in cs if c["put_call"] == "CALL" and c["strike"] > spot), key=lambda c: c["strike"])
        pk = {round(c["strike"], 4): c for c in puts}
        ck = {round(c["strike"], 4): c for c in calls}
        steps = sorted({round(b["strike"] - a["strike"], 4) for a, b in zip(puts, puts[1:], strict=False)}
                       | {round(b["strike"] - a["strike"], 4) for a, b in zip(calls, calls[1:], strict=False)})
        widths = sorted({round(s * m, 4) for s in steps[:2] for m in (1, 2, 4, 5, 10)})
        for w in widths:
            put_spreads = []
            for sp in puts:
                lp = pk.get(round(sp["strike"] - w, 4))
                if lp:
                    put_spreads.append((sp, lp, sp["mid"] - lp["mid"], p_above(spot, sp["strike"], sp["iv"], dte)))
            call_spreads = []
            for sc in calls:
                lc = ck.get(round(sc["strike"] + w, 4))
                if lc:
                    call_spreads.append((sc, lc, sc["mid"] - lc["mid"], p_above(spot, sc["strike"], sc["iv"], dte)))
            cands = []
            if structure == "vertical":
                for sp, lp, cr, pa in put_spreads:
                    cands.append(([("short", sp), ("long", lp)], cr, pa))
                for sc, lc, cr, pa in call_spreads:
                    cands.append(([("short", sc), ("long", lc)], cr, 1 - pa))
            else:
                for sp, lp, pcr, ppa in put_spreads:
                    for sc, lc, ccr, cpa in call_spreads:
                        cands.append(([("short", sp), ("long", lp), ("short", sc), ("long", lc)], pcr + ccr, ppa - cpa))
            for legs, credit, p in cands:
                if not (0 < credit < w) or p < P_MIN or credit / (w - credit) < RR_MIN:
                    continue
                ev = _ev(p, credit, w)
                if best is None or ev > best["model_ev"]:
                    best = {"legs": [{"side": s, "symbol": c["symbol"], "put_call": c["put_call"], "strike": c["strike"]}
                                     for s, c in legs],
                            "expiration_date": exp, "dte": dte, "width": w, "mid": credit, "pmax": p, "model_ev": ev,
                            "natural": sum(c["bid"] if s == "short" else -c["ask"] for s, c in legs)}
    return best


def floor_price(width: float) -> float:
    """Lowest credit that still keeps reward/risk >= 1.5: credit / (width - credit) >= 1.5."""
    return round(RR_MIN * width / (1 + RR_MIN), 2)


def max_loss_per(width: float, credit: float, n_legs: int) -> float:
    return (width - credit) * 100 + COMMISSION * n_legs * 2


def size(equity: float, width: float, credit: float, n_legs: int) -> int:
    return max(0, math.floor(RISK_PER * equity / max_loss_per(width, credit, n_legs)))


# ── live prices (pure) ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    stamp: float


def validate(raw: Any, now: float) -> Quote | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("quote"), dict):
        return None
    q = raw["quote"]
    bid, ask = _num(q.get("bidPrice")), _num(q.get("askPrice"))
    if bid is None or ask is None or bid < 0 or ask <= 0 or bid > ask:
        return None
    t = q.get("quoteTime")
    stamp = None
    if isinstance(t, str):
        try:
            stamp = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
        except ValueError:
            stamp = None
    elif _num(t) is not None:
        stamp = float(t) / 1000
    if stamp is None or now - stamp > STALE_S or stamp - now > 2:
        return None
    return Quote(bid, ask, stamp)


def open_natural(legs: list[dict], quotes: dict[str, Quote]) -> float | None:
    if any(l["symbol"] not in quotes for l in legs):
        return None
    return sum(quotes[l["symbol"]].bid if l["side"] == "short" else -quotes[l["symbol"]].ask for l in legs)


def close_natural(legs: list[dict], quotes: dict[str, Quote]) -> float | None:
    if any(l["symbol"] not in quotes for l in legs):
        return None
    return sum(quotes[l["symbol"]].ask if l["side"] == "short" else -quotes[l["symbol"]].bid for l in legs)


def mid_price(legs: list[dict], quotes: dict[str, Quote]) -> float | None:
    if any(l["symbol"] not in quotes for l in legs):
        return None
    return sum((1 if l["side"] == "short" else -1) * (quotes[l["symbol"]].bid + quotes[l["symbol"]].ask) / 2 for l in legs)


def walked_limit(limit: float, floor: float) -> float:
    return round(max(floor, limit - WALK_STEP), 2)


def intrinsic(legs: list[dict], spot: float) -> float:
    v = 0.0
    for l in legs:
        k = l["strike"]
        val = max(spot - k, 0) if l["put_call"] == "CALL" else max(k - spot, 0)
        v += val if l["side"] == "short" else -val
    return max(v, 0.0)


# ── feeds (replaceable in tests) ─────────────────────────────────────────────

@dataclass
class Feeds:
    universe: Callable[[], list[str]]
    chain: Callable[[str, date], dict[str, Any] | None]
    raw_quotes: Callable[[list[str]], dict[str, Any]]
    earnings_dates: Callable[[str], list[date]]


def _schwab_chain(ticker: str, ref: date) -> dict[str, Any] | None:
    from tradingagents.dataflows import schwab_mcp
    return schwab_mcp.get_option_chain(ticker, contract_type="ALL", strike_count=STRIKE_COUNT,
                                       from_date=(ref + timedelta(days=DTE_MIN)).isoformat(),
                                       to_date=(ref + timedelta(days=DTE_MAX)).isoformat())


def _schwab_quotes(symbols: list[str]) -> dict[str, Any]:
    from tradingagents.dataflows import schwab_mcp
    out: dict[str, Any] = {}
    for i in range(0, len(symbols), 100):
        got = schwab_mcp.get_quotes(symbols[i:i + 100])
        if isinstance(got, dict):
            out.update(got)
    return out


def _universe() -> list[str]:
    from . import spy_tickers
    names = [t for t in spy_tickers.get_sp500_tickers() if t.isalpha()]
    return list(dict.fromkeys(ETFS + names))


_CAL: dict[str, Any] = {"day": None, "dates": {}}


def _earnings_dates(ticker: str) -> list[date]:
    """Scheduled report dates (Alpha Vantage EARNINGS_CALENDAR, fetched once a day)."""
    today = market_calendar.today_et()
    if _CAL["day"] != today:
        import csv
        import io

        from .rules_engine import _AvClient
        dates: dict[str, list[date]] = {}
        try:
            text = _AvClient()("EARNINGS_CALENDAR", horizon="3month")
            for r in csv.DictReader(io.StringIO(text if isinstance(text, str) else "")):
                try:
                    dates.setdefault(r["symbol"].upper(), []).append(date.fromisoformat(r["reportDate"]))
                except (KeyError, ValueError):
                    continue
        except Exception:
            log.exception("[spreads] earnings calendar failed; no earnings filter today")
        _CAL.update(day=today, dates=dates)
    return _CAL["dates"].get(ticker, [])


def default_feeds() -> Feeds:
    return Feeds(universe=_universe, chain=_schwab_chain, raw_quotes=_schwab_quotes, earnings_dates=_earnings_dates)


# ── accounts and equity ──────────────────────────────────────────────────────

def create_account(name: str, structure: str, capital: float = 50_000.0) -> int:
    if structure not in ("condor", "vertical"):
        raise ValueError("structure must be 'condor' or 'vertical'")
    init_tables()
    with db.connect() as conn:
        return int(conn.execute(
            "INSERT INTO spread_accounts (name, structure, starting_capital, cash, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, structure, float(capital), float(capital), _now())).lastrowid)


def list_accounts(active_only: bool = False) -> list[dict[str, Any]]:
    init_tables()
    with db.connect() as conn:
        sql = "SELECT * FROM spread_accounts" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
        return [dict(r) for r in conn.execute(sql)]


def _equity(conn, acct: dict[str, Any]) -> tuple[float, float]:
    """(equity, cost to close all open spreads at the last natural price)."""
    open_cost = 0.0
    for p in conn.execute("SELECT * FROM spread_positions WHERE account_id = ? AND status = 'open'", (acct["id"],)):
        mark = p["last_close_natural"] if p["last_close_natural"] is not None else p["credit"]
        open_cost += min(max(mark, 0.0), p["width"]) * 100 * p["contracts"]
    return acct["cash"] - open_cost, open_cost


# ── daily scan (10:00 ET) ────────────────────────────────────────────────────

_SCAN_LOCK = threading.Lock()


def _log_run(kind: str):
    with db.connect() as conn:
        return conn.execute("INSERT INTO spread_runs (kind, started_at, status) VALUES (?, ?, 'running')",
                            (kind, _now())).lastrowid


def _end_run(run_id, status: str, summary: dict[str, Any]) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE spread_runs SET finished_at = ?, status = ?, summary = ? WHERE id = ?",
                     (_now(), status, json.dumps(summary, default=str)[:4000], run_id))


def run_scan(feeds: Feeds | None = None, today: date | None = None) -> dict[str, Any]:
    feeds = feeds or default_feeds()
    today = today or market_calendar.today_et()
    init_tables()
    accounts = list_accounts(active_only=True)
    if not accounts or not market_calendar.is_trading_day(today):
        return {"skipped": "no accounts or not a trading day"}
    if not _SCAN_LOCK.acquire(blocking=False):
        return {"skipped": "scan already running"}
    run_id, out = None, {"accounts": {}, "errors": []}
    try:
        run_id = _log_run("scan")
        found: dict[str, dict[str, dict]] = {"condor": {}, "vertical": {}}
        structures = {a["structure"] for a in accounts}

        def one(t: str):
            payload = feeds.chain(t, today)
            if not payload:
                return t, {}
            spot, contracts = parse_chain(payload, today)
            if not spot or not contracts:
                return t, {}
            picks = {}
            for s in structures:
                pick = scan(contracts, spot, s)
                if pick is None:
                    continue
                exp = date.fromisoformat(pick["expiration_date"])
                if any(today <= e <= exp for e in feeds.earnings_dates(t)):
                    continue
                picks[s] = {**pick, "ticker": t}
            return t, picks

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(4) as ex:
            futures = {t: ex.submit(one, t) for t in feeds.universe()}
            for t, f in futures.items():
                try:
                    _, picks = f.result()
                    for s, pick in picks.items():
                        found[s][t] = pick
                except Exception as exc:
                    out["errors"].append(f"{t}: {exc}"[:200])
        for acct in accounts:
            out["accounts"][acct["name"]] = _place(acct, sorted(found[acct["structure"]].values(),
                                                                key=lambda c: -c["model_ev"]))
        _end_run(run_id, "done", out)
        return out
    except Exception as exc:
        log.exception("[spreads] scan failed")
        out["errors"].append(str(exc))
        if run_id is not None:
            _end_run(run_id, "failed", out)
        _alert("⚠️ Spread account scan failed", json.dumps(out, default=str)[:1500])
        raise
    finally:
        _SCAN_LOCK.release()


def _place(acct: dict[str, Any], cands: list[dict[str, Any]]) -> dict[str, Any]:
    placed, skipped = 0, {}
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            equity, _ = _equity(conn, acct)
            busy = {r[0] for r in conn.execute(
                "SELECT legs FROM spread_orders WHERE account_id = ? AND status = 'working' "
                "UNION SELECT legs FROM spread_positions WHERE account_id = ? AND status = 'open'", (acct["id"], acct["id"]))}
            live = [dict(r) for r in conn.execute(
                "SELECT ticker, width, contracts, limit_price AS credit, legs FROM spread_orders WHERE account_id = ? AND status = 'working' "
                "UNION ALL SELECT ticker, width, contracts, credit, legs FROM spread_positions WHERE account_id = ? AND status = 'open'",
                (acct["id"], acct["id"]))]
            risk = sum(max_loss_per(r["width"], r["credit"], len(json.loads(r["legs"]))) * r["contracts"] for r in live)
            per_ticker: dict[str, float] = {}
            for r in live:
                per_ticker[r["ticker"]] = per_ticker.get(r["ticker"], 0) + max_loss_per(r["width"], r["credit"], len(json.loads(r["legs"]))) * r["contracts"]
            for c in cands:
                if placed >= MAX_NEW_PER_DAY:
                    break
                if json.dumps(c["legs"]) in busy:
                    skipped["same_spread_open"] = skipped.get("same_spread_open", 0) + 1
                    continue
                n_legs = len(c["legs"])
                limit = round(c["mid"], 2)
                n = size(equity, c["width"], limit, n_legs)
                if n <= 0:
                    skipped["too_small"] = skipped.get("too_small", 0) + 1
                    continue
                add = max_loss_per(c["width"], limit, n_legs) * n
                if risk + add > RISK_TOTAL * equity:
                    skipped["total_risk"] = skipped.get("total_risk", 0) + 1
                    continue
                if per_ticker.get(c["ticker"], 0) + add > RISK_PER_TICKER * equity:
                    skipped["per_ticker"] = skipped.get("per_ticker", 0) + 1
                    continue
                conn.execute(
                    "INSERT INTO spread_orders (account_id, ticker, structure, legs, expiration_date, width, contracts, pmax, model_ev, "
                    "initial_mid, initial_natural, limit_price, floor_price, placed_at, last_walk_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (acct["id"], c["ticker"], acct["structure"], json.dumps(c["legs"]), c["expiration_date"], c["width"], n,
                     c["pmax"], c["model_ev"], c["mid"], c.get("natural"), limit, floor_price(c["width"]), _now(), _now()))
                risk += add
                per_ticker[c["ticker"]] = per_ticker.get(c["ticker"], 0) + add
                placed += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"placed": placed, "candidates": len(cands), "skipped": skipped}


def _alert(summary: str, detail: str) -> None:
    try:
        from . import alerts
        alerts.notify(summary, detail)
    except Exception:
        log.exception("[spreads] alert failed")


# ── the quote loop (fills) ───────────────────────────────────────────────────

def market_open(now: datetime) -> bool:
    et = now.astimezone(market_calendar._ET)
    return market_calendar.is_trading_day(et.date()) and (9, 30) <= (et.hour, et.minute) < (16, 0)


def poll_once(feeds: Feeds, now: datetime | None = None) -> dict[str, int]:
    """One pass: fetch live quotes for every working order and open spread; fill touched orders,
    walk stale limits, take profits. Each fill commits in its own transaction."""
    now = now or datetime.now(timezone.utc)
    report = {"orders": 0, "positions": 0, "filled": 0, "walked": 0, "closed": 0, "unquoted": 0}
    with db.connect() as conn:
        orders = [dict(r) for r in conn.execute("SELECT * FROM spread_orders WHERE status = 'working'")]
        positions = [dict(r) for r in conn.execute("SELECT * FROM spread_positions WHERE status = 'open'")]
    if not orders and not positions:
        return report
    symbols = sorted({l["symbol"] for x in orders + positions for l in json.loads(x["legs"])})
    raw = feeds.raw_quotes(symbols)
    stamp = now.timestamp()
    quotes = {s: q for s in symbols if (q := validate(raw.get(s), stamp)) is not None}
    report["orders"], report["positions"] = len(orders), len(positions)
    for o in orders:
        legs = json.loads(o["legs"])
        nat, mid = open_natural(legs, quotes), mid_price(legs, quotes)
        if nat is None or mid is None:
            report["unquoted"] += 1
            continue
        limit = o["limit_price"]
        if o["reprice"]:
            limit = max(round(mid, 2), o["floor_price"])
        if nat >= limit:
            _fill(o, limit, mid, nat, now)
            report["filled"] += 1
            continue
        walk = o["reprice"] or (now - datetime.fromisoformat(o["last_walk_at"])).total_seconds() >= WALK_EVERY_S
        if walk:
            new = limit if o["reprice"] else walked_limit(limit, o["floor_price"])
            with db.connect() as conn:
                conn.execute("UPDATE spread_orders SET limit_price = ?, last_walk_at = ?, reprice = 0 WHERE id = ? AND status = 'working'",
                             (new, now.isoformat(timespec="seconds"), o["id"]))
            report["walked"] += 1
    for p in positions:
        legs = json.loads(p["legs"])
        nat, mid = close_natural(legs, quotes), mid_price(legs, quotes)
        if nat is None:
            report["unquoted"] += 1
            continue
        if nat <= p["tp_price"]:
            _close(p, p["tp_price"], "profit_target", now)
            report["closed"] += 1
            continue
        with db.connect() as conn:
            conn.execute("UPDATE spread_positions SET last_close_natural = ?, last_close_mid = ?, last_marked_at = ? WHERE id = ? AND status = 'open'",
                         (nat, mid, now.isoformat(timespec="seconds"), p["id"]))
    return report


def _fill(o: dict[str, Any], price: float, mid: float, nat: float, now: datetime) -> None:
    n_legs = len(json.loads(o["legs"]))
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute("UPDATE spread_orders SET status = 'filled', filled_at = ?, fill_price = ?, fill_mid = ?, fill_natural = ? "
                               "WHERE id = ? AND status = 'working'", (now.isoformat(timespec="seconds"), price, mid, nat, o["id"]))
            if cur.rowcount == 1:
                conn.execute(
                    "INSERT INTO spread_positions (account_id, order_id, ticker, structure, legs, expiration_date, width, contracts, credit, "
                    "max_loss, tp_price, opened_at, last_close_natural) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (o["account_id"], o["id"], o["ticker"], o["structure"], o["legs"], o["expiration_date"], o["width"], o["contracts"],
                     price, max_loss_per(o["width"], price, n_legs) * o["contracts"], round((1 - TP_KEEP) * price, 2),
                     now.isoformat(timespec="seconds"), price))
                conn.execute("UPDATE spread_accounts SET cash = cash + ? WHERE id = ?",
                             (price * 100 * o["contracts"] - COMMISSION * n_legs * o["contracts"], o["account_id"]))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _close(p: dict[str, Any], price: float, reason: str, now: datetime, commission: bool = True) -> None:
    n_legs = len(json.loads(p["legs"]))
    fee = COMMISSION * n_legs * p["contracts"] if commission else 0.0
    cost = price * 100 * p["contracts"] + fee
    pnl = p["credit"] * 100 * p["contracts"] - COMMISSION * n_legs * p["contracts"] - cost
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute("UPDATE spread_positions SET status = 'closed', closed_at = ?, close_price = ?, close_reason = ?, pnl = ? "
                               "WHERE id = ? AND status = 'open'", (now.isoformat(timespec="seconds"), price, reason, pnl, p["id"]))
            if cur.rowcount == 1:
                conn.execute("UPDATE spread_accounts SET cash = cash - ? WHERE id = ?", (cost, p["account_id"]))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


# ── end of day (16:10 ET) ────────────────────────────────────────────────────

def end_of_day(feeds: Feeds | None = None, today: date | None = None) -> dict[str, Any]:
    """Unfilled orders: re-price next morning, dropped after 3 trading days. Spreads past expiry
    settle at intrinsic value. Daily equity snapshot vs SPY."""
    feeds = feeds or default_feeds()
    today = today or market_calendar.today_et()
    init_tables()
    now = datetime.now(timezone.utc)
    out = {"dropped": 0, "carried": 0, "settled": 0}
    with db.connect() as conn:
        for o in [dict(r) for r in conn.execute("SELECT * FROM spread_orders WHERE status = 'working'")]:
            expired = date.fromisoformat(o["expiration_date"]) <= today
            if o["trading_days"] >= ENTRY_DAYS or expired:
                conn.execute("UPDATE spread_orders SET status = 'dropped', note = ? WHERE id = ?",
                             ("not filled in 3 trading days" if not expired else "expired unfilled", o["id"]))
                out["dropped"] += 1
            else:
                conn.execute("UPDATE spread_orders SET trading_days = trading_days + 1, reprice = 1 WHERE id = ?", (o["id"],))
                out["carried"] += 1
        due = [dict(r) for r in conn.execute("SELECT * FROM spread_positions WHERE status = 'open' AND expiration_date <= ?",
                                             (today.isoformat(),))]
    if due:
        raw = feeds.raw_quotes(sorted({p["ticker"] for p in due} | {"SPY"}))
        for p in due:
            q = (raw.get(p["ticker"]) or {}).get("quote") or {}
            spot = _num(q.get("lastPrice")) or _num(q.get("closePrice"))
            if spot is None:
                continue
            _close(p, min(intrinsic(json.loads(p["legs"]), spot), p["width"]), "expiry", now, commission=False)
            out["settled"] += 1
    spy = ((feeds.raw_quotes(["SPY"]).get("SPY") or {}).get("quote") or {}).get("lastPrice")
    with db.connect() as conn:
        for a in [dict(r) for r in conn.execute("SELECT * FROM spread_accounts WHERE active = 1")]:
            equity, open_cost = _equity(conn, a)
            conn.execute("INSERT OR REPLACE INTO spread_daily VALUES (?, ?, ?, ?, ?, ?)",
                         (a["id"], today.isoformat(), equity, a["cash"], open_cost, _num(spy)))
    return out


# ── background loop (started by the portfolio app) ───────────────────────────

_LOOP: dict[str, Any] = {"thread": None, "last": None, "failures": 0}


def _loop() -> None:
    import fcntl
    lock = (db.DB_PATH.parent / "spread-monitor.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("[spreads] another quote loop owns the DB; this process stays idle")
        return
    feeds = default_feeds()
    while True:
        started = time.monotonic()
        now = datetime.now(timezone.utc)
        try:
            if market_open(now):
                _LOOP["last"] = poll_once(feeds, now)
                _LOOP["failures"] = 0
        except Exception:
            _LOOP["failures"] += 1
            log.exception("[spreads] quote loop pass failed")
            if _LOOP["failures"] == 5:
                _alert("⚠️ Spread account quote loop failing", "5 passes in a row failed; fills are paused.")
        time.sleep(max(0.5, POLL_S - (time.monotonic() - started)) if market_open(now) else 30)


def start_loop() -> None:
    if _LOOP["thread"] is None:
        t = threading.Thread(target=_loop, name="spread-quote-loop", daemon=True)
        t.start()
        _LOOP["thread"] = t


# ── read side ────────────────────────────────────────────────────────────────

def summary() -> dict[str, Any]:
    init_tables()
    out = []
    with db.connect() as conn:
        for a in [dict(r) for r in conn.execute("SELECT * FROM spread_accounts ORDER BY id")]:
            equity, open_cost = _equity(conn, a)
            orders = [dict(r) for r in conn.execute("SELECT * FROM spread_orders WHERE account_id = ? ORDER BY id DESC LIMIT 200", (a["id"],))]
            pos = [dict(r) for r in conn.execute("SELECT * FROM spread_positions WHERE account_id = ? ORDER BY id DESC LIMIT 200", (a["id"],))]
            daily = [dict(r) for r in conn.execute("SELECT * FROM spread_daily WHERE account_id = ? ORDER BY day", (a["id"],))]
            done = [o for o in orders if o["status"] in ("filled", "dropped")]
            filled = [o for o in done if o["status"] == "filled"]
            closed = [p for p in pos if p["status"] == "closed"]
            out.append({"account": a, "equity": equity, "open_cost": open_cost,
                        "fill_rate": (len(filled) / len(done)) if done else None,
                        "avg_fill_vs_mid": (sum(o["fill_price"] - o["initial_mid"] for o in filled) / len(filled)) if filled else None,
                        "closed": len(closed), "wins": sum((p["pnl"] or 0) > 0 for p in closed),
                        "realized": sum(p["pnl"] or 0 for p in closed),
                        "orders": orders, "positions": pos, "daily": daily})
        runs = [dict(r) for r in conn.execute("SELECT * FROM spread_runs ORDER BY id DESC LIMIT 10")]
    return {"accounts": out, "runs": runs, "loop": _LOOP.get("last")}
