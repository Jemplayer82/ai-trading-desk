"""Daily options paper-trading engine.

Pipeline (run_options_build, behind POST /api/options-scan, cron Mon-Fri):
  settle expiries -> pre-screen the whole S&P 500 by momentum/volume ->
  quick LLM scan of the top 150 + SPY -> full deep dive on the top DEEP_TOP (50)
  directional names + SPY (BUY *and* SELL — puts need bearish candidates, unlike
  the equity scan's BUY/HOLD filter) -> market-open gate -> chain fetch +
  contract vetting (options_data) -> LLM allocator (options_allocator) ->
  apply decisions through db's transactional position/ledger helpers.

Options runs are spy_scans rows with kind='options', so progress counters,
cooperative cancel, and the stuck-run reaper all work unchanged.

Also owns the two standing maintenance passes:
  settle_expired    — idempotent expiry settlement (safety net; the DTE floor
                      force-close means it mostly fires after downtime),
  refresh_positions — mark open contracts to market (Schwab bulk quotes ->
                      yfinance chain fallback -> carry floored at intrinsic).

Paper only: nothing here (or anywhere) places real orders — the Schwab client
exposes market data and account reads exclusively.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time as time_mod
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import yfinance as yf

from tradingagents.dataflows import schwab_mcp

from . import (
    account_policy,
    db,
    market_cache,
    options_allocator,
    options_data,
    options_learning,
    scan_queue,
    spy_scanner,
)
from .runner import build_config
from .spy_tickers import get_sp500_tickers

log = logging.getLogger(__name__)

PRESCREEN_TOP = 150     # top movers (by momentum/volume) that get the quick LLM scan
DEEP_TOP = 50           # directional names that get the full agent graph
ALWAYS_DEEP = ("SPY",)  # tickers guaranteed a deep dive every run, HOLD or not
MARKET_OPEN_ET = (9, 35)  # allocation waits for live quotes on trading days
SETTLE_HOUR_ET = 17     # expiry-day positions settle only after this hour
STALE_ALERT_THRESHOLD = 3

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


# Serializes the POST-WAIT phase (mark-to-market -> chain fetch ->
# allocator -> open/close -> complete_spy_scan) across concurrent
# options builds. A build parked in _wait_for_market_open no longer
# holds the scan-queue slot (see scan_queue._is_any_scan_running), so
# all three daily accounts can be computing at once; this lock is what
# keeps them from allocating against each other's cash and position
# reads. Acquired only AFTER the wait returns, so accounts allocate one
# at a time in the order they finish waiting.
#
# Deadlock-free by construction: it is acquired at exactly ONE call site,
# nothing inside the guarded region re-acquires it, and no code inside the
# _allocation_slot block ever acquires scan_queue._SCAN_LOCK. A thread
# holding _ALLOC_LOCK can never block on _SCAN_LOCK, even when _SCAN_LOCK is held across the worker start.
_ALLOC_LOCK = threading.Lock()
_ALLOC_POLL_SECONDS = 30.0
# Global allocation-slot hard timeout. Overridable at module load via the
# OPTIONS_ALLOC_TIMEOUT_SECONDS environment variable; unparsable, zero, or
# negative values fall back to 3600 seconds. Tests may monkeypatch this
# module-level attribute directly.
_ALLOC_TIMEOUT_SECONDS = _parse_alloc_timeout_seconds()


def _slot_release_enabled() -> bool:
    """Kill switch for the proactive queue hand-off AND the busy predicate (env, read at call time).

    Three concurrent full pipelines in one 4g-capped container is exactly
    the shape that reproduced the host OOM the scan queue was built to
    prevent (see the _SCAN_LOCK comment in portfolio_routes.start_scan),
    so a no-code-change rollback has to exist. Flipping this to 0 does two
    things together: it stops the PROACTIVE dequeue below, AND (via
    scan_queue._wait_market_release_enabled reading this same env var) it
    puts 'running_wait_market' back into scan_queue._is_any_scan_running's
    busy set, so a newly REQUESTED scan queues behind a parked waiter again
    instead of starting beside it. Both halves have to move together —
    stopping only the proactive hand-off while the busy check still ignored
    waiters would leave the container reading as idle for the whole ~2h
    daily wait window, which defeats the rollback this switch exists for.
    """
    return os.environ.get("OPTIONS_RELEASE_SLOT_DURING_WAIT", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }


def _release_scan_slot(scan_id: int) -> None:
    """Best-effort hand-off of the scan-queue slot when a build starts waiting.

    A queue error must never sink a live build.
    """
    if not _slot_release_enabled():
        return
    try:
        scan_queue._dequeue_next_scan()
    except Exception:
        log.exception("[options %s] queue advance on wait entry failed", scan_id)


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
        # distinct from _wait_for_market_open's running_wait_market, precisely so
        # downstream consumers (the dashboard, /api/portfolio/status) can tell
        # a pre-open parker apart from a build queued behind another account's
        # allocation.
        db.update_spy_scan(scan_id, status="running_wait_alloc")
        log.info("[options %s] waiting for the allocation slot (%.0fs)", scan_id, waited)
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


# ── Expiry settlement ────────────────────────────────────────────────────────

def is_settleable(expiration_date: str, now: datetime | None = None) -> bool:
    """True once the contract's last session is over: any day after expiry, or
    expiry day itself after SETTLE_HOUR_ET. Never intraday on expiry day."""
    now = now or options_data.now_et()
    try:
        exp = datetime.strptime(expiration_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    today = now.date()
    return today > exp or (today == exp and now.hour >= SETTLE_HOUR_ET)


def intrinsic_value(put_call: str, strike: float, underlying_close: float) -> float:
    if str(put_call).upper().startswith("C"):
        return max(0.0, float(underlying_close) - float(strike))
    return max(0.0, float(strike) - float(underlying_close))


def underlying_close_on_or_before(underlying: str, expiration_date: str) -> float | None:
    """Last available close on/before the expiration date (as-of pattern —
    covers holidays, half-days, and downtime catch-up without a market
    calendar)."""
    try:
        exp = datetime.strptime(expiration_date, "%Y-%m-%d").date()
        hist = yf.Ticker(underlying).history(
            start=(exp - timedelta(days=10)).isoformat(),
            end=(exp + timedelta(days=1)).isoformat(),
        )
        if hist is None or hist.empty:
            return None
        onto = hist[[d <= exp for d in hist.index.date]]
        if onto.empty:
            return None
        return float(onto["Close"].dropna().iloc[-1])
    except Exception:
        log.exception("[options] close lookup failed for %s @ %s", underlying, expiration_date)
        return None


def settle_expired(paper_account_id: int | None = None) -> dict[str, Any]:
    """Settle every open position whose expiry has passed. Idempotent (the
    status='open' guard in db.settle_options_position); safe to run from the
    nightly sweep, the start of every build, and the start of every refresh."""
    now = options_data.now_et()
    open_positions = db.list_options_positions(paper_account_id, status="open")
    due = [p for p in open_positions if is_settleable(p["expiration_date"], now)]
    settled = worthless = failed = 0
    close_cache: dict[tuple[str, str], float | None] = {}
    for pos in due:
        key = (pos["underlying"], pos["expiration_date"])
        if key not in close_cache:
            close_cache[key] = underlying_close_on_or_before(*key)
        close = close_cache[key]
        if close is None:
            n = db.bump_options_position_stale(pos["id"])
            if n >= STALE_ALERT_THRESHOLD:
                log.error(
                    "[options] cannot settle position %s (%s exp %s) after %d attempts — "
                    "no underlying close available",
                    pos["id"], pos["occ_symbol"], pos["expiration_date"], n,
                )
            failed += 1
            continue
        intr = intrinsic_value(pos["put_call"], pos["strike"], close)
        if db.settle_options_position(pos["id"], intr, close):
            if intr >= 0.01:
                settled += 1
            else:
                worthless += 1
            log.info("[options] settled %s at intrinsic $%.2f (close %.2f)",
                     pos["occ_symbol"], intr, close)
    return {"due": len(due), "settled_itm": settled,
            "expired_worthless": worthless, "failed": failed}


# ── Mark-to-market ───────────────────────────────────────────────────────────

def _yf_contract_price(pos: dict[str, Any], chain_cache: dict[tuple[str, str], Any]) -> float | None:
    """Price one contract off a yfinance chain (matched by strike + side —
    NOT contractSymbol, which yfinance formats unpadded)."""
    key = (pos["underlying"], pos["expiration_date"])
    if key not in chain_cache:
        try:
            chain_cache[key] = yf.Ticker(pos["underlying"]).option_chain(pos["expiration_date"])
        except Exception:
            log.debug("[options] yf chain fetch failed for %s %s", *key, exc_info=True)
            chain_cache[key] = None
    chain = chain_cache[key]
    if chain is None:
        return None
    frame = chain.calls if pos["put_call"].upper().startswith("C") else chain.puts
    if frame is None or getattr(frame, "empty", True):
        return None
    rows = frame[abs(frame["strike"] - float(pos["strike"])) < 0.001]
    if rows.empty:
        return None
    row = rows.iloc[0]
    bid, ask = row.get("bid"), row.get("ask")
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > 0 and ask >= bid:
        return round((float(bid) + float(ask)) / 2, 4)
    last = row.get("lastPrice")
    if isinstance(last, (int, float)) and last > 0:
        return float(last)
    return None


def _underlying_prices(underlyings: list[str]) -> dict[str, float]:
    """Fresh underlying prices for the intrinsic floor on carried marks."""
    out: dict[str, float] = {}
    if not underlyings:
        return out
    if schwab_mcp.market_data_enabled():
        try:
            quotes = schwab_mcp.get_quotes(underlyings)
            if quotes:
                for u in underlyings:
                    p = schwab_mcp.quote_price(quotes.get(u, {}))
                    if p:
                        out[u] = p
        except Exception:
            log.debug("[options] underlying quotes via Schwab failed", exc_info=True)
    missing = [u for u in underlyings if u not in out]
    if missing:
        try:
            df = yf.download(missing, period="1d", auto_adjust=True, progress=False)
            if df is not None and not df.empty:
                if hasattr(df.columns, "levels"):
                    for u in missing:
                        try:
                            series = df["Close"][u].dropna()
                            if not series.empty:
                                out[u] = float(series.iloc[-1])
                        except (KeyError, TypeError):
                            continue
                else:
                    series = df["Close"].dropna()
                    if not series.empty:
                        out[missing[0]] = float(series.iloc[-1])
        except Exception:
            log.debug("[options] underlying prices via yfinance failed", exc_info=True)
    return out


def refresh_positions(paper_account_id: int | None = None) -> dict[str, Any]:
    """Settle due expiries, then mark every open position to market and roll
    the account equity onto its latest completed options scan row."""
    settle_summary = settle_expired(paper_account_id)
    positions = db.list_options_positions(paper_account_id, status="open")

    marked = 0
    priced: dict[int, tuple[float, str]] = {}
    if positions and schwab_mcp.market_data_enabled():
        try:
            quotes = schwab_mcp.get_quotes([p["occ_symbol"] for p in positions])
        except Exception:
            log.exception("[options] Schwab option quotes failed")
            quotes = None
        if quotes:
            for p in positions:
                price = schwab_mcp.option_quote_price(quotes.get(p["occ_symbol"], {}))
                if price:
                    priced[p["id"]] = (price, "schwab")

    chain_cache: dict[tuple[str, str], Any] = {}
    for p in positions:
        if p["id"] in priced:
            continue
        price = _yf_contract_price(p, chain_cache)
        if price:
            priced[p["id"]] = (price, "yfinance")

    # Carry-with-intrinsic-floor for anything still unpriced. Never mark to 0
    # on a missing quote.
    unpriced = [p for p in positions if p["id"] not in priced]
    spots = _underlying_prices(sorted({p["underlying"] for p in unpriced})) if unpriced else {}
    for p in unpriced:
        carried = float(p.get("current_premium") or p.get("entry_premium") or 0)
        spot = spots.get(p["underlying"])
        intr = intrinsic_value(p["put_call"], p["strike"], spot) if spot else 0.0
        price = max(carried, intr)
        if price <= 0:
            n = db.bump_options_position_stale(p["id"])
            if n >= STALE_ALERT_THRESHOLD:
                log.error("[options] no price for %s after %d refreshes", p["occ_symbol"], n)
            continue
        source = "intrinsic" if intr > carried else "carried"
        db.mark_options_position(p["id"], price, price * 100 * int(p["contracts"]),
                                 source, reset_stale=False)
        n = int(p.get("stale_count") or 0) + 1
        if n >= STALE_ALERT_THRESHOLD:
            log.warning("[options] %s marked '%s' %d refreshes in a row",
                        p["occ_symbol"], source, n)
        marked += 1

    for p in positions:
        got = priced.get(p["id"])
        if got:
            price, source = got
            db.mark_options_position(p["id"], price, price * 100 * int(p["contracts"]), source)
            marked += 1

    policies = {int(a["id"]): account_policy.StopPolicy.from_account(a)
                for a in db.list_paper_accounts(kind="options")}
    stopped = _apply_intraday_stops(positions, priced, policies)

    # Roll fresh equity onto each affected account's latest completed scan row.
    accounts = ([db.get_paper_account(paper_account_id)] if paper_account_id
                else db.list_paper_accounts(kind="options"))
    account_values: dict[int, float] = {}
    for acct in accounts:
        if not acct:
            continue
        equity = account_equity(acct["id"])["equity"]
        account_values[acct["id"]] = equity
        latest = db.get_latest_completed_spy_scan(paper_account_id=acct["id"], kind="options")
        if latest:
            db.update_spy_scan(latest["id"], current_value=equity,
                               last_price_check=datetime.utcnow().isoformat(timespec="seconds") + "Z")
    return {"marked": marked, "open": len(positions), "stopped": stopped,
            "settle": settle_summary, "account_values": account_values}


def _backtrack_stop_crossing(
    pos: dict[str, Any],
    prev_mark: float,
    stop_level: float,
    new_price: float,
) -> tuple[str, float] | None:
    """Find the minute the premium crossed the stop inside the last interval.

    There is NO intraday price history for the option contract itself (neither
    Schwab nor yfinance serve it), so this is the finest honest reconstruction
    available: map the stop premium to an implied UNDERLYING level by linear
    interpolation between the two observed (premium, underlying-time) points,
    then walk the underlying's 1-minute bars and take the FIRST bar that
    crossed that level on the adverse side (below for calls, above for puts).
    Returns (closed_at UTC ISO, implied underlying at the stop) — minute
    precision, because minute bars are the smallest unit the market data has.
    None on any gap in data; the caller keeps refresh-time behavior.
    """
    import pandas as pd

    t0_raw = pos.get("last_marked_at") or pos.get("opened_at")
    if not t0_raw:
        return None
    try:
        t0 = pd.Timestamp(str(t0_raw).replace("Z", "+00:00"))
        t1 = pd.Timestamp.now(tz="UTC")
        underlying = pos["underlying"]
        bars = yf.Ticker(underlying).history(period="2d", interval="1m")
        if bars is None or bars.empty:
            return None
        idx = bars.index.tz_convert("UTC")
        window = bars[(idx > t0) & (idx <= t1)]
        if window.empty:
            return None
        u0 = float(bars[idx <= t0]["Close"].iloc[-1]) if (idx <= t0).any() \
            else float(window["Open"].iloc[0])
        u1 = float(window["Close"].iloc[-1])
        if prev_mark == new_price or u0 == u1:
            return None
        # Premium -> underlying, linear between the two observations. Crude
        # (ignores gamma/theta inside the hour) but directionally sound over
        # a single mark interval, and clamped to the observed range so a bad
        # slope can't invent a level the underlying never traded.
        u_star = u0 + (stop_level - float(prev_mark)) * (u1 - u0) / (new_price - float(prev_mark))
        lo, hi = min(u0, u1), max(u0, u1)
        u_star = max(lo, min(hi, u_star))
        is_call = str(pos.get("put_call") or "").upper().startswith("C")
        if is_call:
            crossed = window[window["Low"] <= u_star]
        else:
            crossed = window[window["High"] >= u_star]
        if crossed.empty:
            return None
        ts = crossed.index[0].tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        return ts, round(u_star, 4)
    except Exception:
        log.exception("[options] stop backtrack failed for %s", pos.get("occ_symbol"))
        return None


def _apply_intraday_stops(
    positions: list[dict[str, Any]],
    priced: dict[int, tuple[float, str]],
    policies: dict[int, account_policy.StopPolicy],
) -> int:
    """Emulate a standing stop order between daily allocations.

    The level now comes from each account's configured stop policy
    (StopPolicy). Previously a flat -60% stop was enforced only once a day,
    at the 09:35 allocation, filled at THAT moment's price — a position
    could crash through the stop at 10:30 and ride a full day past it. Now
    every hourly refresh checks freshly quoted positions and closes breaches
    immediately.

    Fill convention (standard backtest rule):
      - previous mark ABOVE the stop, new quote at/below it -> the price
        crossed the level sometime this interval, so fill AT the stop level,
        like a working stop order would have;
      - first observation already below the stop (overnight gap / never
        marked) -> fill at the observed quote, because a real stop order gaps
        through too. No pretending we caught a level the market never traded.

    Stop-limit adds an arm/resting-fill state: a trigger that gaps through
    the limit becomes a resting stop-limit order; it fills on a later
    refresh if the fresh quote is back at or above the limit price, and
    remains armed (no fill) while the quote stays below the limit.

    Only fresh quotes (schwab/yfinance) can trigger — a carried or intrinsic
    mark is a guess, and a guess must never realize a loss. Positions the
    refresh couldn't price simply wait for the next refresh or the daily
    allocator's forced_closes, which stays as the backstop. Kill switch:
    options_intraday_stop / TRADINGAGENTS_OPTIONS_INTRADAY_STOP.
    """
    from tradingagents.default_config import DEFAULT_CONFIG

    if not DEFAULT_CONFIG.get("options_intraday_stop", True):
        return 0

    stopped = 0
    spot_cache: dict[str, float | None] = {}

    # Resting stop-limit fills must be evaluated before any newly-triggering
    # position in the same pass, so an armed position that can now fill does
    # so before anything else runs.
    ordered = [p for p in positions if p.get("stop_triggered_at")] + [
        p for p in positions if not p.get("stop_triggered_at")
    ]

    for p in ordered:
        got = priced.get(p["id"])
        if not got:
            continue
        price, _source = got
        entry = float(p.get("entry_premium") or 0)
        if entry <= 0:
            continue

        policy = policies.get(int(p.get("paper_account_id") or 0), account_policy.NONE)
        prev_mark = p.get("current_premium")
        outcome = options_allocator.effective_stop_level(
            dict(p, current_premium=price),
            policy,
            prev_mark=float(prev_mark) if prev_mark is not None else None,
        )

        if outcome.action == "arm":
            db.arm_options_stop_limit(int(p["id"]))
            log.info(
                "[options] stop-limit triggered for %s (level $%.2f, limit $%.2f) "
                "but gapped below limit; now resting",
                p["occ_symbol"], outcome.level, outcome.limit_price,
            )
            continue

        if outcome.action != "fill":
            continue

        stop_level = outcome.level
        stop_reason = outcome.exit_reason
        fill = outcome.fill_price
        crossed_this_interval = outcome.crossed

        # Book the sale at the minute the level was actually crossed, not at
        # the top of the hour the refresh happened to notice it.
        closed_at = None
        exit_u: float | None = None
        exit_src: str | None = None
        if crossed_this_interval:
            back = _backtrack_stop_crossing(p, float(prev_mark), stop_level, price)
            if back:
                closed_at, exit_u = back
                exit_src = "backtracked"
        if exit_u is None:
            if p["underlying"] not in spot_cache:
                try:
                    spot_cache.update(_underlying_prices([p["underlying"]]))
                except Exception:
                    spot_cache[p["underlying"]] = None
            exit_u = spot_cache.get(p["underlying"])
            exit_src = "live" if exit_u else None
        ok = db.close_options_position(
            int(p["id"]), exit_premium=fill, exit_reason=stop_reason,
            exit_underlying=exit_u, exit_underlying_source=exit_src,
            closed_at=closed_at,
        )
        if ok:
            stopped += 1
            log.info("[options] intraday %s: %s filled $%.2f (stop $%.2f, mark $%.2f, %s%s)",
                     stop_reason, p["occ_symbol"], fill, stop_level, price,
                     "crossed this interval" if crossed_this_interval else "gapped through",
                     f", crossing at {closed_at}" if closed_at else "")
    return stopped


# ── Account math ─────────────────────────────────────────────────────────────

def account_equity(paper_account_id: int) -> dict[str, float]:
    cash = db.options_cash_balance(paper_account_id)
    open_positions = db.list_options_positions(paper_account_id, status="open")
    open_value = sum(float(p.get("current_value") or p.get("cost_basis") or 0)
                     for p in open_positions)
    deployed = sum(float(p.get("cost_basis") or 0) for p in open_positions)
    return {"cash": round(cash, 2), "open_value": round(open_value, 2),
            "deployed": round(deployed, 2), "equity": round(cash + open_value, 2)}


def account_summary(paper_account_id: int) -> dict[str, Any]:
    acct = db.get_paper_account(paper_account_id) or {}
    eq = account_equity(paper_account_id)
    realized = db.options_realized_pnl(paper_account_id)
    starting = float(acct.get("starting_capital") or 100_000.0)
    open_positions = db.list_options_positions(paper_account_id, status="open")
    settled = db.list_options_positions(paper_account_id, status="settled")
    funded = db.has_options_deposit(paper_account_id)
    if not funded:
        # The deposit ledger row lands on the first build; until then show the
        # account at its starting capital instead of a phantom -100% return.
        eq = {**eq, "cash": starting, "equity": starting}
    return {
        **eq,
        "realized_pnl": realized,
        "starting_capital": starting,
        "return_pct": round((eq["equity"] - starting) / starting * 100, 2) if starting else 0.0,
        "open_count": len(open_positions),
        "closed_count": len(settled),
        "funded": funded,
    }


# ── The daily build ──────────────────────────────────────────────────────────

def _wait_for_market_open(scan_id: int) -> None:
    """Block until MARKET_OPEN_ET on trading days so entries fill at live mids.
    Weekend manual runs proceed immediately (weekday check only). Heartbeats
    updated_at every few minutes so the stuck-run reaper doesn't mistake the
    wait for a crashed worker.

    Status is "running_wait_market", distinct from "running_alloc" (real
    vetting/allocation work). This wait runs 07:30-09:35 ET daily doing
    nothing but sleeping — with a single "running_alloc" label the frontend
    couldn't tell "blocked" from "working" and polled the full scan payload
    every 5s for up to 2 hours a day for zero new information. See
    run_options_build for where the label flips back once real work starts.

    The first time the loop actually blocks it hands the container-wide scan
    queue slot to the next queued options account. Because all three daily
    accounts' rows are usually created in one request loop, accounts 2 and 3
    land 'queued' behind account 1's 'pending'; account 1 parking dequeues
    account 2, and account 2 parking dequeues account 3. Allocation is still
    re-serialized by _allocation_slot(), so parallel compute cannot double-
    allocate.
    """
    ticks = 0
    while True:
        now = options_data.now_et()
        if now.weekday() >= 5 or (now.hour, now.minute) >= MARKET_OPEN_ET:
            return
        if db.is_spy_scan_cancelled(scan_id):
            raise spy_scanner.ScanCancelled()
        if ticks == 0:
            # This wait is a sleep, not work — hand the container-wide scan slot
            # to the next queued options account so it can run its own compute
            # phase now instead of after this whole build. Allocation is
            # re-serialized by _allocation_slot(), so parallel compute cannot
            # double-allocate.
            _release_scan_slot(scan_id)
        if ticks % 6 == 0:  # every ~3 minutes
            db.update_spy_scan(scan_id, status="running_wait_market")
        ticks += 1
        time_mod.sleep(30)


def _zero_candidate_reason(
    quick_results: list[dict[str, Any]],
    directional: list[dict[str, Any]],
    enriched: list[dict[str, Any]],
    usable: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str | None:
    """Explain a zero-candidate run, or None when there were candidates.

    A bare "0 new / 0 hold / 0 close" is indistinguishable from a broken run,
    which is exactly the confusion that made a fully-failed scan look like a
    quiet market. Counts are derived from each stage separately so the text can
    never misattribute a failure as "nothing passed vetting".

    Reachable only when the run was not TOTALLY broken — the guards in
    run_options_build fail the scan outright in that case.
    """
    if candidates:
        return None
    errored_quick = sum(1 for r in quick_results if r.get("error"))
    noise = f" ({errored_quick} of {len(quick_results)} quick scans errored)" if errored_quick else ""

    if not directional:
        return (f"No ticker in the movers pre-screen scored BUY or SELL today — "
                f"every name came back HOLD{noise}. Nothing to trade.")
    if not enriched:
        return f"{len(directional)} directional signals, but no deep dive ran{noise}."
    failed = len(enriched) - len(usable)
    if not usable:
        return (f"All {len(enriched)} deep dives failed{noise}. No contracts were vetted — "
                f"check the analysis history for the underlying error.")
    extra = f" ({failed} further dives failed and were skipped)" if failed else ""
    # usable carries each deep dive's OWN final rating (the 5-tier Buy/Overweight/
    # Hold/Underweight/Sell scale — tradingagents/agents/utils/rating.py), which
    # overwrites the quick scan's BUY/SELL signal and can legitimately land on
    # Hold after full analysis. That's zero candidates with zero vetting notes
    # too (options_data.fetch_candidates never calls fetch_contract for it) —
    # distinguish "nothing directional to vet" from an actual vetting failure.
    directional_final = sum(
        1 for r in usable
        if (r.get("signal") or "").upper() in options_data._DIRECTION_BY_SIGNAL
    )
    if not directional_final:
        return (f"{len(usable)} of {len(enriched)} deep dives completed, but every one "
                f"rated Hold after full analysis — no directional call to vet{extra}.")
    return (f"{directional_final} of {len(usable)} deep dives rated a directional call, but no "
            f"contract passed liquidity/delta/DTE vetting{extra} — see the vetting notes below.")


def run_options_build(scan_id: int, trade_date: str) -> None:
    """Worker for one daily options run. Raises on failure (the endpoint's
    thread wrapper records failed/cancelled status, mirroring the equity scan)."""
    scan = db.get_spy_scan(scan_id) or {}
    account_id = scan.get("paper_account_id")
    if not account_id:
        raise RuntimeError("options scan requires a paper_account_id")
    account = db.get_paper_account(int(account_id))
    if not account:
        raise RuntimeError(f"paper account {account_id} not found")
    account_id = int(account_id)
    aggressiveness = int(scan.get("aggressiveness") or account.get("aggressiveness") or 5)
    bias = scan.get("bias") or account.get("bias") or "neutral"
    stop_policy = account_policy.StopPolicy.from_account(account)
    log.info("[options %s] stop policy: %s", scan_id, account_policy.describe_policy(stop_policy))

    prefs = db.get_preferences() or {}
    selected_analysts = prefs.get("analysts") or ["market", "social", "news", "fundamentals"]
    config = build_config({**prefs, "aggressiveness": aggressiveness, "bias": bias})

    log.info("[options %s] starting for %s (account %s)", scan_id, trade_date, account_id)

    # Fund the account on first build; settle anything that expired since the
    # last run so carry-forward math never sees a dead position.
    if not db.has_options_deposit(account_id):
        db.append_options_cash(account_id, "deposit",
                               float(account.get("starting_capital") or 100_000.0),
                               scan_id=scan_id, note="initial deposit")
    with _phase("Expiry settlement failed"):
        settle_expired(account_id)

    # Phase 1: movers pre-screen + quick scan.
    with _phase("Couldn't fetch the S&P 500 ticker list"):
        universe = get_sp500_tickers()
    with _phase("Movers pre-screen failed"):
        movers = prescreen(universe, PRESCREEN_TOP, trade_date=trade_date)
    if not movers:
        raise RuntimeError("Movers pre-screen returned no tickers")
    # Quick-scan the top 250 movers + SPY (the index gauge, always included).
    for sym in ALWAYS_DEEP:
        if sym not in movers:
            movers.append(sym)
    log.info("[options %s] quick-scanning %d movers (top %d + %s)",
             scan_id, len(movers), PRESCREEN_TOP, ",".join(ALWAYS_DEEP))
    with _phase("Quick scan failed"):
        quick_results = spy_scanner.run_quick_scan(scan_id, movers, trade_date, config)
    if db.is_spy_scan_cancelled(scan_id):
        raise spy_scanner.ScanCancelled()
    # Wholesale failure must fail the run, not complete green with an empty
    # portfolio. Checked here rather than inside run_quick_scan because the
    # equity scan has its own deliberate degrade policy for a bad quick scan.
    spy_scanner.assert_quick_scan_healthy(quick_results)

    # Phase 2: deep dive the top directional names — BUY *and* SELL (puts need
    # bearish candidates; deliberately unlike the equity scan's BUY/HOLD cut).
    top = select_deep_dive_targets(quick_results)
    enriched: list[dict[str, Any]] = []
    if top:
        with _phase("Deep-dive analysis failed"):
            enriched = spy_scanner.run_deep_dives(scan_id, top, trade_date, config, selected_analysts)
        # Quick and deep resolve independent providers/models, so a deep-only
        # outage passes the quick guard above and lands here.
        spy_scanner.assert_deep_dives_healthy(enriched)
    else:
        log.info("[options %s] no directional quick-scan signals today", scan_id)
    if db.is_spy_scan_cancelled(scan_id):
        raise spy_scanner.ScanCancelled()

    # Phase 3: wait for live quotes, then vet contracts and allocate.
    #
    # Set to running_wait_market first — _wait_for_market_open owns the label
    # while it's actually blocked, and only flips back to running_alloc the
    # moment real work resumes below. A run outside the wait window (weekend,
    # already past open) returns immediately and this line is a no-op status
    # bounce, not an extra poll cycle.
    db.update_spy_scan(scan_id, status="running_wait_market")
    _wait_for_market_open(scan_id)
    with _allocation_slot(scan_id):
        db.update_spy_scan(scan_id, status="running_alloc")
        with _phase("Position mark-to-market failed"):
            refresh_positions(account_id)

        # A FAILED deep dive still carries its quick-scan signal/conviction
        # (run_deep_dives returns {**candidate, "error": ...}), and fetch_candidates
        # filters only on signal/conviction — so without this, contracts get vetted
        # and real paper positions opened off analyses that crashed. Drop them.
        usable = [e for e in enriched if not e.get("error")]
        if len(usable) != len(enriched):
            log.warning("[options %s] dropping %d failed deep dives before vetting",
                        scan_id, len(enriched) - len(usable))
        with _phase("Chain fetch failed"):
            candidates, chain_notes = options_data.fetch_candidates(usable)
        log.info("[options %s] %d vetted candidates from %d usable deep dives (%d total)",
                 scan_id, len(candidates), len(usable), len(enriched))

        open_positions = db.list_options_positions(account_id, status="open")
        eq = account_equity(account_id)
        realized = db.options_realized_pnl(account_id)
        starting_equity = eq["equity"]
        fresh_signals = {
            (r.get("ticker") or "").upper(): {"signal": (r.get("signal") or "").upper(),
                                              "conviction": r.get("conviction")}
            for r in quick_results if r.get("ticker")
        }

        # Learning loop, read side: latest batch-reflected lessons + mechanical
        # track record for THIS account (options_learning). Pure Python + one DB
        # read — zero LLM cost at scan time; "" until enough closes exist.
        lessons_context = ""
        try:
            settled = db.list_options_positions(account_id, status="settled")
            last_lesson = db.latest_options_lesson(account_id)
            stats = options_learning.compute_options_stats(
                settled, min_closed=int(config.get("options_lessons_min_closed", 10)))
            lessons_context = options_learning.format_track_record(
                stats, (last_lesson or {}).get("lessons_md"),
                max_chars=int(config.get("options_lessons_max_chars", 1200)))
            log.info("[options %s] lessons block: %d chars (%d closed)",
                     scan_id, len(lessons_context), len(settled))
        except Exception:
            log.exception("[options %s] lessons context failed — allocating without it", scan_id)

        with _phase("Options allocation failed"):
            alloc = options_allocator.run(
                candidates, open_positions, trade_date, config,
                equity=eq["equity"], cash=eq["cash"], realized_pnl=realized,
                aggressiveness=aggressiveness, bias=bias, fresh_signals=fresh_signals,
                lessons_context=lessons_context, policy=stop_policy,
            )

        # Learning loop, write side: today's chain data carries the underlying spot
        # for every candidate — capture it on closes so attribution doesn't have to
        # backfill with a less precise EOD close.
        spot_by_underlying = {
            c["underlying"]: c.get("underlying_price")
            for c in candidates if c.get("underlying_price")
        }

        # Phase 4: apply decisions through the transactional helpers.
        decisions_log: list[dict[str, Any]] = []
        for c in alloc["closes"]:
            pos = db.get_options_position(int(c["position_id"])) or {}
            exit_premium = float(c.get("exit_premium") or pos.get("current_premium")
                                 or pos.get("entry_premium") or 0)
            exit_spot = spot_by_underlying.get(pos.get("underlying"))
            if db.close_options_position(int(c["position_id"]), exit_premium,
                                         c["exit_reason"], close_scan_id=scan_id,
                                         exit_underlying=exit_spot,
                                         exit_underlying_source="live" if exit_spot else None):
                decisions_log.append({
                    "occ_symbol": c["occ_symbol"], "action": "CLOSE",
                    "exit_reason": c["exit_reason"], "exit_premium": exit_premium,
                    "contracts": pos.get("contracts"), "rationale": c.get("rationale"),
                    # Contract identity for the dashboard's decisions table — without
                    # these the row renders as "? $0?" (the display can't name the
                    # contract from an OCC symbol alone on old browsers/rows).
                    "underlying": pos.get("underlying"), "put_call": pos.get("put_call"),
                    "strike": pos.get("strike"), "expiration_date": pos.get("expiration_date"),
                })
        skipped_opens: list[str] = []
        for o in alloc["opens"]:
            contract = o["contract"]
            cash_now = db.options_cash_balance(account_id)
            if o["cost"] > cash_now + 0.01:
                skipped_opens.append(f"{contract['occ_symbol']}: cost ${o['cost']:,.0f} > cash ${cash_now:,.0f}")
                continue
            db.open_options_position(account_id, scan_id, {
                "occ_symbol": contract["occ_symbol"],
                "underlying": contract["underlying"],
                "put_call": contract["put_call"],
                "strike": contract["strike"],
                "expiration_date": contract["expiration_date"],
                "contracts": o["contracts"],
                "entry_premium": contract["mid"],
                "entry_underlying": contract.get("underlying_price"),
                "entry_delta": contract.get("delta"),
                "entry_bid": contract.get("bid"),
                "entry_ask": contract.get("ask"),
                "entry_oi": contract.get("open_interest"),
                "signal": contract.get("signal"),
                "conviction": contract.get("conviction"),
                "rationale": o.get("rationale"),
                "data_source": contract.get("source"),
            })
            decisions_log.append({
                "occ_symbol": contract["occ_symbol"], "action": "NEW",
                "contracts": o["contracts"], "entry_premium": contract["mid"],
                "cost": o["cost"], "rationale": o.get("rationale"),
                "underlying": contract["underlying"], "put_call": contract["put_call"],
                "strike": contract["strike"], "expiration_date": contract["expiration_date"],
            })
        for h in alloc["holds"]:
            hp = db.get_options_position(int(h["position_id"])) if h.get("position_id") else None
            hp = hp or {}
            decisions_log.append({"occ_symbol": h["occ_symbol"], "action": "HOLD",
                                  "rationale": h.get("rationale"),
                                  "underlying": hp.get("underlying"), "put_call": hp.get("put_call"),
                                  "strike": hp.get("strike"),
                                  "expiration_date": hp.get("expiration_date")})

        report = alloc["report_md"]
        reason = _zero_candidate_reason(quick_results, top, enriched, usable, candidates)
        if reason:
            report += f"\n## Why no new positions\n{reason}\n"
        if skipped_opens:
            report += "\n## Skipped opens (cash)\n" + "\n".join(f"- {s}" for s in skipped_opens) + "\n"
        if chain_notes:
            report += ("\n## Contract vetting notes\n"
                       + "\n".join(f"- {n}" for n in chain_notes[:40]) + "\n")

        prev = db.get_latest_completed_spy_scan(exclude_id=scan_id,
                                                paper_account_id=account_id, kind="options")
        db.complete_spy_scan(
            scan_id=scan_id,
            allocator_report=report,
            portfolio_json=decisions_log,
            previous_scan_id=int(prev["id"]) if prev else None,
            starting_value=starting_equity,
        )

        final = account_equity(account_id)
        db.update_spy_scan(scan_id, current_value=final["equity"],
                           last_price_check=datetime.utcnow().isoformat(timespec="seconds") + "Z")
        if final["cash"] < -0.01:
            log.error("[options %s] LEDGER INVARIANT VIOLATION: cash $%.2f < 0 on account %s",
                      scan_id, final["cash"], account_id)
        log.info("[options %s] done — %d closes / %d opens / %d holds, equity $%s (cash $%s)",
                 scan_id, len(alloc["closes"]), len(alloc["opens"]), len(alloc["holds"]),
                 f"{final['equity']:,.0f}", f"{final['cash']:,.0f}")
