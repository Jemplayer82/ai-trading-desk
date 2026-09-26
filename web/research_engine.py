"""Shared daily research and per-account allocation orchestration.

One `kind='research'` row per NYSE trading day feeds per-account allocation
rows for the options paper-trading engine (tier 4) and the equity S&P 500
paper-account engine (tier 3).  This module contains the pre-screen,
target selection, market-open wait and the global allocation lock that are
shared by both engines.

TIER RULE: tier-3 builds delete ``options_*.py``, so this module must never
import any ``options_*`` module at module level.  Keep options-specific
code out of it entirely.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time as time_mod
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import yfinance as yf

from . import account_policy, alerts, db, market_cache, market_calendar, scan_queue, spy_scanner
from .runner import build_config
from .spy_tickers import get_sp500_tickers

log = logging.getLogger(__name__)

PRESCREEN_TOP = 150     # top movers (by momentum/volume) that get the quick LLM scan
DEEP_TOP = 50           # directional names that get the full agent graph
ALWAYS_DEEP = ("SPY",)  # tickers guaranteed a deep dive every run, HOLD or not

# Same-day cache of the movers pre-screen. All three daily options
# accounts run the identical screen over the identical (24h-cached)
# S&P 500 universe, each otherwise paying its own ~500-ticker
# yf.download. trade_date scoping means a result can never be served
# into a later trading day; the 4h TTL means a build kicked off
# manually in the afternoon re-screens rather than trading off a stale
# morning list.
_PRESCREEN_TTL_SECONDS = 4 * 3600

# A pre-screen built from fewer than 80% of the requested tickers is treated
# as a degraded download. The truncated result is still returned to the
# caller, but a degraded movers list is not pinned for the full TTL.
_PRESCREEN_MIN_COMPLETENESS = 0.8

_PRESCREEN_CACHE = market_cache.SameDayCache("options-prescreen",
                                             ttl_seconds=_PRESCREEN_TTL_SECONDS)


def _parse_alloc_timeout_seconds() -> float:
    """Allocation-slot timeout from the environment, read once at import time.

    Env var ``OPTIONS_ALLOC_TIMEOUT_SECONDS`` overrides the default of 3600
    seconds. Unparsable, empty, zero, or negative values fall back to the
    default so the slot can never silently wait forever.
    """
    raw = os.environ.get("OPTIONS_ALLOC_TIMEOUT_SECONDS", "3600").strip()
    try:
        val = float(raw)
    except ValueError:
        return 3600.0
    if val <= 0:
        return 3600.0
    return val


# Serializes the post-open allocation phase of EVERY paper account, options
# and equity, one at a time. Allocation rows never hold the scan-queue slot.
#
# Deadlock-free by construction: it is acquired at exactly ONE call site,
# nothing inside the guarded region re-acquires it, and a thread holding
# _ALLOC_LOCK can never block on the scan-queue slot lock.
_ALLOC_LOCK = threading.Lock()
_ALLOC_POLL_SECONDS = 30.0
# Global allocation-slot hard timeout. Overridable at module load via the
# OPTIONS_ALLOC_TIMEOUT_SECONDS environment variable; unparsable, zero, or
# negative values fall back to 3600 seconds. Tests may monkeypatch this
# module-level attribute directly.
_ALLOC_TIMEOUT_SECONDS = _parse_alloc_timeout_seconds()


@contextmanager
def _phase(label: str) -> Iterator[None]:
    """Tag failures with a phase prefix; cancellations pass through untouched."""
    try:
        yield
    except spy_scanner.ScanCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — re-raised with friendlier context
        raise RuntimeError(f"{label}: {exc}") from exc


@contextmanager
def _allocation_slot(scan_id: int) -> Iterator[None]:
    """Hold the global allocation lock for one build's post-wait phase.

    Blocks in _ALLOC_POLL_SECONDS slices rather than one open-ended
    acquire so a queued waiter (a) keeps writing updated_at and cannot
    be mistaken for a crashed worker by the stuck-run reaper
    (web/scheduler.py STUCK_SCAN_STALL_MIN, default 120 min) and (b)
    still honours a cancel request while blocked. Fails loudly past
    _ALLOC_TIMEOUT_SECONDS instead of hanging a thread forever.

    The timeout value defaults to 3600 seconds and may be overridden at
    module load by the ``OPTIONS_ALLOC_TIMEOUT_SECONDS`` environment
    variable. Unparsable, empty, zero, or negative values fall back to the
    default so the slot can never silently wait forever.

    If the timeout fires before this waiter ever acquires the lock, the
    raised RuntimeError makes clear the scan never entered the allocation
    phase (it remained queued behind another build).
    """
    waited = 0.0
    while not _ALLOC_LOCK.acquire(timeout=_ALLOC_POLL_SECONDS):
        waited += _ALLOC_POLL_SECONDS
        if db.is_spy_scan_cancelled(scan_id):
            raise spy_scanner.ScanCancelled()
        if waited >= _ALLOC_TIMEOUT_SECONDS:
            raise RuntimeError(
                f"scan never acquired the allocation lock (queued behind another build); "
                f"timed out after {waited:.0f}s waiting for the allocation slot")
        # Heartbeat: this waiter now uses its own running_wait_alloc status,
        # distinct from wait_for_market_open's running_wait_market, precisely so
        # downstream consumers (the dashboard, /api/portfolio/status) can tell
        # a pre-open parker apart from a build queued behind another account's
        # allocation.
        db.update_spy_scan(scan_id, status="running_wait_alloc")
        log.info("[alloc %s] waiting for the allocation slot (%.0fs)", scan_id, waited)
    try:
        # A cancel requested while queued must not still allocate.
        if db.is_spy_scan_cancelled(scan_id):
            raise spy_scanner.ScanCancelled()
        yield
    finally:
        _ALLOC_LOCK.release()


# ── Pre-screen ───────────────────────────────────────────────────────────────

def _mover_score(closes: list[float], volumes: list[float]) -> float | None:
    """Direction-agnostic 'is something happening here' score: |5d| + half |20d|
    momentum plus a volume-surge kicker. Big losers rank too — they're put
    candidates."""
    if len(closes) < 5:
        return None
    ret5 = abs((closes[-1] / closes[-5]) - 1) * 100
    ret20 = abs((closes[-1] / closes[0]) - 1) * 100 if len(closes) >= 20 else 0.0
    vol_kick = 0.0
    if len(volumes) >= 20 and volumes[-1]:
        avg = sum(volumes[-20:-1]) / 19
        if avg:
            vol_kick = max(0.0, float(volumes[-1]) / avg - 1.0)
    return ret5 + 0.5 * ret20 + 3.0 * min(vol_kick, 3.0)


def prescreen(
    tickers: list[str],
    top_n: int = PRESCREEN_TOP,
    *,
    trade_date: str | None = None,
) -> list[str]:
    """Rank the universe by mover score from one bulk download; top_n survive.

    Cached same-trading-day so the three daily options accounts don't each
    pay for an identical ~500-ticker yfinance download.  trade_date=None
    bypasses the cache for callers that need a fresh screen. Downloads covering
    fewer than 80% of the requested universe are never cached, so a partial or
    rate-limited download doesn't pin a degraded movers list for the full TTL.
    """
    if trade_date is not None:
        key = (
            top_n,
            hashlib.sha256("\n".join(sorted(tickers)).encode()).hexdigest()[:16],
        )
        cached = _PRESCREEN_CACHE.get(trade_date, key)
        if cached is not None:
            log.info("[options] reusing same-day movers pre-screen (%d tickers)", len(cached))
            return list(cached)

    try:
        raw = yf.download(tickers, period="1mo", auto_adjust=True, progress=False, threads=True)
    except Exception as exc:
        raise RuntimeError(f"pre-screen bulk download failed: {exc}") from exc
    scored: list[tuple[float, str]] = []
    if raw is not None and not raw.empty:
        if hasattr(raw.columns, "levels"):
            for t in tickers:
                try:
                    closes = raw["Close"][t].dropna().tolist()
                    volumes = raw["Volume"][t].dropna().tolist()
                except (KeyError, TypeError):
                    continue
                s = _mover_score(closes, volumes)
                if s is not None:
                    scored.append((s, t))
        elif tickers:
            closes = raw["Close"].dropna().tolist()
            volumes = raw["Volume"].dropna().tolist()
            s = _mover_score(closes, volumes)
            if s is not None:
                scored.append((s, tickers[0]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    result = [t for _, t in scored[:top_n]]
    if result and trade_date is not None and len(scored) >= _PRESCREEN_MIN_COMPLETENESS * len(tickers):
        _PRESCREEN_CACHE.put(trade_date, key, list(result))
    return result


def select_deep_dive_targets(quick_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows that get a full multi-agent deep dive.

    Top DEEP_TOP directional names (BUY *and* SELL, by conviction) PLUS every
    ALWAYS_DEEP ticker (SPY) guaranteed a dive even when its quick scan came back
    HOLD or it fell outside the top DEEP_TOP — the full agent graph gets its own
    shot at a directional call on the index gauge. ALWAYS_DEEP names are appended
    once (never duplicated when they already made the directional cut)."""
    directional = [r for r in quick_results
                   if (r.get("signal") or "").upper() in ("BUY", "SELL")]
    top = sorted(directional, key=lambda r: -(r.get("conviction") or 0))[:DEEP_TOP]
    have = {(r.get("ticker") or "").upper() for r in top}
    for sym in ALWAYS_DEEP:
        if sym not in have:
            row = next((r for r in quick_results
                        if (r.get("ticker") or "").upper() == sym), None)
            if row:
                top.append(row)
    return top


_MARKET_POLL_SECONDS = 30.0


def wait_for_market_open(scan_id: int) -> None:
    """Block until MARKET_OPEN_ET on NYSE trading days so entries fill at live mids.

    Weekends and NYSE holidays return immediately, which preserves the ability
    to force a manual run on a non-trading day. Heartbeats update the scan row
    every ~3 minutes so the stuck-run reaper doesn't mistake the wait for a
    crashed worker.

    Status is "running_wait_market", distinct from "running_alloc" (real
    vetting/allocation work). This wait runs 07:30-09:35 ET daily doing
    nothing but sleeping — with a single "running_alloc" label the frontend
    couldn't tell "blocked" from "working" and polled the full scan payload
    every 5s for up to 2 hours a day for zero new information. See
    run_allocation for where the label flips back once real work starts.
    """
    ticks = 0
    while True:
        now = market_calendar.now_et()
        if not market_calendar.is_trading_day(now.date()) or (now.hour, now.minute) >= market_calendar.MARKET_OPEN_ET:
            return
        if db.is_spy_scan_cancelled(scan_id):
            raise spy_scanner.ScanCancelled()
        if ticks % 6 == 0:
            db.update_spy_scan(scan_id, status="running_wait_market")
        ticks += 1
        time_mod.sleep(_MARKET_POLL_SECONDS)


# ── Research orchestration ───────────────────────────────────────────────────

def research_aggressiveness() -> int:
    """The most aggressive paper-account setting drives shared research."""
    return max(
        (int(a.get("aggressiveness") or 5) for a in db.list_paper_accounts()),
        default=5,
    )


def _research_report(
    trade_date: str,
    quick: list[dict[str, Any]],
    top: list[dict[str, Any]],
    enriched: list[dict[str, Any]],
) -> str:
    """Build a markdown summary of the shared research run."""
    total = len(quick)
    quick_errored = [r for r in quick if r.get("error")]
    quick_signals: dict[str, int] = {}
    for r in quick:
        sig = (r.get("signal") or "HOLD").upper()
        quick_signals[sig] = quick_signals.get(sig, 0) + 1

    lines: list[str] = [
        f"# Shared research — {trade_date}",
        "",
        f"Quick-scan total: {total} (errored: {len(quick_errored)})",
        "Quick-scan signals: "
        + (
            ", ".join(f"{k}: {v}" for k, v in sorted(quick_signals.items()))
            or "none"
        ),
        "",
    ]

    completed = [e for e in enriched if not e.get("error")]
    failed = [e for e in enriched if e.get("error")]
    reused = [e for e in enriched if e.get("reused_from") is not None]

    lines.append(
        f"Deep-dive targets: {len(top)}, completed: {len(completed)}, "
        f"reused: {len(reused)}, failed: {len(failed)}"
    )

    deep_signals: dict[str, int] = {}
    for e in enriched:
        sig = (e.get("signal") or "HOLD").upper()
        deep_signals[sig] = deep_signals.get(sig, 0) + 1

    lines.append(
        "Deep-dive ratings: "
        + (
            ", ".join(f"{k}: {v}" for k, v in sorted(deep_signals.items()))
            or "none"
        )
    )

    if failed:
        lines.append("")
        lines.append("Failed tickers:")
        for e in failed:
            ticker = e.get("ticker", "UNKNOWN")
            err = str(e.get("error", ""))[:120]
            lines.append(f"- {ticker}: {err}")

    return "\n".join(lines)


def run_research(scan_id: int, trade_date: str) -> None:
    """Run the shared daily research scan (phases 1-2 of the old options build).

    Creates a single `kind='research'` row whose results feed every daily
    allocation pass. Raises on failure; the caller's worker wrapper records
    cancelled/failed status.
    """
    scan = db.get_spy_scan(scan_id)
    if not scan:
        raise RuntimeError(f"Research scan {scan_id} not found")

    prefs = db.get_preferences() or {}
    selected_analysts = prefs.get("analysts") or ["market", "social", "news", "fundamentals"]
    config = build_config({
        **prefs,
        "aggressiveness": int(scan.get("aggressiveness") or research_aggressiveness()),
        "bias": "neutral",
    })

    log.info("[research %s] starting research scan for %s", scan_id, trade_date)

    with _phase("Couldn't fetch the S&P 500 ticker list"):
        universe = get_sp500_tickers()

    with _phase("Movers pre-screen failed"):
        movers = prescreen(universe, PRESCREEN_TOP, trade_date=trade_date)

    if not movers:
        raise RuntimeError("Movers pre-screen returned no tickers")

    for sym in ALWAYS_DEEP:
        if sym not in movers:
            movers.append(sym)

    log.info(
        "[research %s] quick-scanning %d movers (top %d + %s)",
        scan_id,
        len(movers),
        PRESCREEN_TOP,
        ",".join(ALWAYS_DEEP),
    )

    with _phase("Quick scan failed"):
        quick = spy_scanner.run_quick_scan(scan_id, movers, trade_date, config)

    if db.is_spy_scan_cancelled(scan_id):
        raise spy_scanner.ScanCancelled()

    spy_scanner.assert_quick_scan_healthy(quick)

    top = select_deep_dive_targets(quick)
    enriched: list[dict[str, Any]] = []

    if top:
        with _phase("Deep-dive analysis failed"):
            enriched = spy_scanner.run_deep_dives(
                scan_id, top, trade_date, config, selected_analysts
            )
        spy_scanner.assert_deep_dives_healthy(enriched)
    else:
        db.update_spy_scan(scan_id, deep_total=0, deep_count=0)

    if db.is_spy_scan_cancelled(scan_id):
        raise spy_scanner.ScanCancelled()

    db.complete_spy_scan(
        scan_id,
        allocator_report=_research_report(trade_date, quick, top, enriched),
        portfolio_json=[],
        previous_scan_id=None,
        starting_value=None,
    )

    log.info("[research %s] completed", scan_id)


class NotTradingDay(Exception):
    """Raised when a research run is requested for a non-NYSE trading day."""


def require_trading_day(today: str, force: bool) -> None:
    """Refuse to start a daily research scan on weekends/holidays unless forced."""
    if not force and not market_calendar.is_trading_day(date.fromisoformat(today)):
        raise NotTradingDay(
            f"{today} is not an NYSE trading day (weekend or holiday) "
            "— pass force to run anyway"
        )


def run_worker(
    scan_id: int,
    trade_date: str,
    work: Callable[[int, str], None],
    *,
    alert_kind: str,
    holds_slot: bool,
) -> None:
    """Shared worker-thread wrapper for research and allocation runs.

    Refreshes credentials, runs ``work(scan_id, trade_date)``, and records
    cancellation, failure, or queue advancement.

    Allocation rows never hold the compute slot. An unguarded dequeue from one
    would start queued work beside a running research scan, so allocation
    workers use the idle-guarded advance.
    """
    scan_queue.refresh_creds_from_db()
    try:
        work(scan_id, trade_date)
    except spy_scanner.ScanCancelled:
        log.info("[worker %s] scan cancelled", scan_id)
        db.update_spy_scan(scan_id, status="cancelled")
    except Exception as exc:
        log.exception("[worker %s] work failed: %s", scan_id, exc)
        db.fail_spy_scan(scan_id, str(exc))
        alerts.notify_run_failed(
            kind=alert_kind,
            run_id=scan_id,
            label=trade_date,
            error=str(exc),
        )
    finally:
        if holds_slot:
            scan_queue._dequeue_next_scan()
        else:
            scan_queue._advance_queue_if_idle()


def ensure_research_scan(today: str) -> dict[str, Any]:
    """Idempotently create and start the daily shared research scan for ``today``."""
    with scan_queue._SCAN_LOCK:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id, status FROM spy_scans "
                "WHERE kind = 'research' AND trade_date = ? "
                "AND status NOT IN ('failed', 'cancelled') "
                "ORDER BY id DESC LIMIT 1",
                (today,),
            ).fetchone()
            if row:
                return {"scan_id": row["id"], "status": row["status"], "new": False}

        with db.connect() as conn:
            busy = scan_queue._is_any_scan_running(conn)

        aggr = research_aggressiveness()

        if busy:
            sid = db.create_spy_scan(
                today,
                paper_account_id=None,
                aggressiveness=aggr,
                bias="neutral",
                status="queued",
                kind="research",
            )
            log.info(
                "[queue] research scan %s queued behind %s scan #%s",
                sid,
                busy.get("scan_type") or busy.get("kind"),
                busy["id"],
            )
            return {
                "scan_id": sid,
                "status": "queued",
                "new": True,
                "queued_behind": dict(busy),
            }

        sid = db.create_spy_scan(
            today,
            paper_account_id=None,
            aggressiveness=aggr,
            bias="neutral",
            kind="research",
        )

    target = scan_queue.resolve_runner("research")
    if target is None:
        db.fail_spy_scan(sid, "research runner not registered")
        raise RuntimeError("research runner not registered")

    scan_queue.spawn_worker(target, sid, today)
    return {"scan_id": sid, "status": "pending", "new": True}


# ── Per-account allocation ───────────────────────────────────────────────────

_RESEARCH_POLL_SECONDS = 30.0
_DEFAULT_RESEARCH_DEADLINE = (10, 30)
_DEFAULT_RESEARCH_WAIT_MAX_MIN = 90


def _research_deadline(start: datetime) -> datetime:
    """When an allocation stops waiting for today's research.

    The later of RESEARCH_DEADLINE_ET (default 10:30 ET) on ``start``'s day and
    ``start`` + RESEARCH_WAIT_MAX_MIN (default 90) minutes. Both env vars are
    read at call time; malformed values fall back to the defaults.
    """
    hm = account_policy.parse_hhmm(os.environ.get("RESEARCH_DEADLINE_ET", "10:30"))
    h, m = hm if hm is not None else _DEFAULT_RESEARCH_DEADLINE
    try:
        max_min = int(os.environ.get("RESEARCH_WAIT_MAX_MIN", str(_DEFAULT_RESEARCH_WAIT_MAX_MIN)).strip())
    except ValueError:
        max_min = _DEFAULT_RESEARCH_WAIT_MAX_MIN
    if max_min <= 0:
        max_min = _DEFAULT_RESEARCH_WAIT_MAX_MIN
    return max(
        start.replace(hour=h, minute=m, second=0, microsecond=0),
        start + timedelta(minutes=max_min),
    )


def wait_for_research(scan_id: int, trade_date: str) -> dict[str, Any]:
    """Block until ``trade_date``'s shared research completes; return it.

    Failed, cancelled, running and missing research rows all keep the wait
    going, because a retry may still create a newer row. Past the deadline
    (see _research_deadline) this raises RuntimeError. Heartbeats the
    allocation row every ~3 minutes so the stuck-run reaper leaves it alone.
    """
    start = market_calendar.now_et()
    deadline = _research_deadline(start)
    ticks = 0
    while True:
        if db.is_spy_scan_cancelled(scan_id):
            raise spy_scanner.ScanCancelled()
        done = db.latest_research_scan(trade_date, completed_only=True)
        if done:
            db.update_spy_scan(scan_id, research_scan_id=done["id"])
            return db.get_spy_scan(done["id"]) or {}
        if ticks % 6 == 0:
            db.update_spy_scan(scan_id, status="running_wait_research")  # heartbeat
            newest = db.latest_research_scan(trade_date)
            log.info("[alloc %s] waiting for research (%s)",
                     scan_id, newest["status"] if newest else "missing")
        if market_calendar.now_et() >= deadline:
            newest = db.latest_research_scan(trade_date)
            status = newest["status"] if newest else "missing"
            raise RuntimeError(
                f"today's research is not complete (status={status}) "
                f"— allocation abandoned at {deadline:%H:%M} ET")
        ticks += 1
        time_mod.sleep(_RESEARCH_POLL_SECONDS)


@dataclass
class AllocationContext:
    """Everything an engine-specific allocate() callback needs."""

    scan: dict[str, Any]
    account: dict[str, Any]
    research: dict[str, Any]
    trade_date: str
    quick_results: list[dict[str, Any]]
    usable: list[dict[str, Any]]
    live_prices: dict[str, float]
    config: dict[str, Any]
    aggressiveness: int
    bias: str


def run_allocation(
    scan_id: int,
    trade_date: str,
    allocate: Callable[[AllocationContext], None],
    *,
    extra_tickers: Iterable[str] = (),
) -> None:
    """Wait for today's research and the open, then call ``allocate`` once.

    Copies the research's quick rows and counters onto the allocation row,
    waits for MARKET_OPEN_ET, then under the global allocation lock fetches
    live quotes for the usable deep-dived names (plus ``extra_tickers``, e.g.
    current holdings) and hands an AllocationContext to ``allocate``.

    Callers own completion (``complete_spy_scan``). The ~1 min of post-open
    work runs one account at a time under the global lock.
    """
    # (0) setup
    scan = db.get_spy_scan(scan_id) or {}
    pid = scan.get("paper_account_id")
    if pid is None:
        raise RuntimeError("allocation requires a paper_account_id")
    account = db.get_paper_account(int(pid))
    if not account:
        raise RuntimeError(f"paper account {pid} not found")
    aggressiveness = int(scan.get("aggressiveness") or account.get("aggressiveness") or 5)
    bias = scan.get("bias") or account.get("bias") or "neutral"
    config = build_config({
        **(db.get_preferences() or {}),
        "aggressiveness": aggressiveness,
        "bias": bias,
    })

    # (1) wait for today's shared research
    db.update_spy_scan(scan_id, status="running_wait_research")
    research = wait_for_research(scan_id, trade_date)

    # (2) copy the research onto the allocation row
    db.copy_spy_quick_results(research["id"], scan_id)
    db.update_spy_scan(
        scan_id,
        quick_fingerprint=research.get("quick_fingerprint"),
        quick_count=research.get("quick_count") or 0,
        quick_total=research.get("quick_total") or 0,
        deep_count=research.get("deep_count") or 0,
        deep_total=research.get("deep_total") or 0,
        deep_reused_count=research.get("deep_reused_count") or 0,
    )
    if db.is_spy_scan_cancelled(scan_id):
        raise spy_scanner.ScanCancelled()

    # (3) wait for the open
    db.update_spy_scan(scan_id, status="running_wait_market")
    wait_for_market_open(scan_id)

    # (4) allocate under the global lock
    with _allocation_slot(scan_id):
        db.update_spy_scan(scan_id, status="running_alloc")
        usable = [dict(r) for r in db.list_deep_dived_results(research["id"])]
        tickers: list[str] = []
        seen: set[str] = set()
        for raw in [r.get("ticker") for r in usable] + list(extra_tickers):
            t = (raw or "").upper()
            if t and t not in seen:
                seen.add(t)
                tickers.append(t)
        try:
            live = (spy_scanner.fetch_live_prices(tickers, log_label=f"alloc {scan_id}")
                    if tickers else {})
        except Exception:
            log.exception("[alloc %s] live quote fetch failed", scan_id)
            live = {}
        for r in usable:
            r["entry_price"] = live.get((r.get("ticker") or "").upper())
        allocate(AllocationContext(
            scan=db.get_spy_scan(scan_id) or scan,
            account=account,
            research=research,
            trade_date=trade_date,
            quick_results=research.get("quick_results") or [],
            usable=usable,
            live_prices=live,
            config=config,
            aggressiveness=aggressiveness,
            bias=bias,
        ))


_EQUITY_KEEP_SIGNALS = frozenset({"BUY", "OVERWEIGHT", "HOLD"})


def equity_candidates(
    usable: list[dict[str, Any]],
    held_tickers: Iterable[str],
) -> list[dict[str, Any]]:
    """Equity rebalance candidates from the usable deep-dived rows.

    Keeps Buy/Overweight/Hold names plus anything currently held (a held
    Sell-rated name reaches the rebalance as SELL and exits). Rows without a
    live price are dropped. Returns new dicts; inputs are never mutated.
    """
    held = {(t or "").upper() for t in held_tickers}
    out: list[dict[str, Any]] = []
    unpriced: list[str] = []
    for row in usable:
        t = (row.get("ticker") or "").upper()
        sig = (row.get("signal") or "").upper()
        if sig not in _EQUITY_KEEP_SIGNALS and t not in held:
            continue
        if not row.get("entry_price"):
            unpriced.append(t)
            continue
        out.append({**row, "ticker": t, "entry_price": float(row["entry_price"])})
    if unpriced:
        log.warning("[alloc] dropping %d candidate(s) with no live price: %s",
                    len(unpriced), ", ".join(unpriced))
    return out


_START_LOCK = threading.Lock()
_RUNNER_KEY = {"options": "options", "equity": "spy"}


def start_allocation(
    account: dict[str, Any],
    today: str,
    kind: str,
    *,
    force: bool = False,
    aggressiveness: int | None = None,
    bias: str | None = None,
) -> dict[str, Any]:
    """Idempotently create and start today's allocation row for one account.

    Allocation rows bypass the compute queue (they are never busy until
    running_alloc) and serialize only on _ALLOC_LOCK, so no _SCAN_LOCK is
    taken here. Workers run in their own daemon threads via
    scan_queue.spawn_worker, because Starlette runs one request's background
    tasks sequentially.

    Research is kicked only when today has no research row at all; after a
    failed attempt the scheduler's retry job owns re-kicks.
    """
    if kind not in _RUNNER_KEY:
        raise ValueError(f"unknown allocation kind {kind!r}")
    require_trading_day(today, force)
    account_id = int(account["id"])

    with _START_LOCK:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id, status FROM spy_scans "
                "WHERE trade_date = ? AND kind = ? AND paper_account_id = ? "
                "AND status NOT IN ('failed', 'cancelled') "
                "ORDER BY id DESC LIMIT 1",
                (today, kind, account_id),
            ).fetchone()
        if row:
            return {"scan_id": row["id"], "account_id": account_id,
                    "status": row["status"], "new": False}
        completed = db.latest_research_scan(today, completed_only=True)
        kick_research = db.count_research_attempts(today) == 0
        scan_id = db.create_spy_scan(
            today,
            paper_account_id=account_id,
            aggressiveness=int(aggressiveness or account.get("aggressiveness") or 5),
            bias=bias or account.get("bias") or "neutral",
            status="running_wait_research",
            kind=kind,
            research_scan_id=completed["id"] if completed else None,
        )

    research = ensure_research_scan(today) if kick_research else None
    latest = db.latest_research_scan(today)

    target = scan_queue.resolve_runner(_RUNNER_KEY[kind])
    if target is None:
        db.fail_spy_scan(scan_id, "no allocation runner registered for this tier")
        raise RuntimeError("no allocation runner registered for this tier")
    scan_queue.spawn_worker(target, scan_id, today)

    return {
        "scan_id": scan_id,
        "account_id": account_id,
        "status": "running_wait_research",
        "new": True,
        "research": research or (
            {"scan_id": latest["id"], "status": latest["status"], "new": False}
            if latest else None
        ),
    }
