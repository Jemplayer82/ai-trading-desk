"""Daily options paper-trading engine: per-account allocation.

The daily research (movers pre-screen, quick scan, deep dives) is shared
across every account and runs once per NYSE trading day at 00:00 ET in
``web/research_engine.py``. This module is the per-account options
allocation that consumes it (run_options_allocation, behind
POST /api/options-scan, cron Mon-Fri):

  settle expiries -> wait for the shared research -> wait for 09:35 ET ->
  under this account's allocation lock (accounts run in parallel): mark open
  contracts to market -> contract vetting over the research's usable deep
  dives, shared by all options accounts (shared_vetted_candidates) -> LLM allocator (options_allocator) -> apply decisions
  through db's transactional position/ledger helpers.

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

import copy
import logging
import threading
import time as time_mod
from datetime import datetime, timedelta
from typing import Any

import yfinance as yf

from tradingagents.dataflows import schwab_mcp

from . import (
    account_policy,
    db,
    options_allocator,
    options_data,
    options_learning,
    research_engine,
)
from .research_engine import _phase

log = logging.getLogger(__name__)

SETTLE_HOUR_ET = 17     # expiry-day positions settle only after this hour
STALE_ALERT_THRESHOLD = 3


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

def _zero_candidate_reason(
    quick_results: list[dict[str, Any]],
    n_targets: int,
    usable: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str | None:
    """Explain a zero-candidate run, or None when there were candidates.

    A bare "0 new / 0 hold / 0 close" is indistinguishable from a broken run,
    which is exactly the confusion that made a fully-failed scan look like a
    quiet market. Counts are derived from each stage separately so the text can
    never misattribute a failure as "nothing passed vetting".

    ``n_targets`` is the shared research's deep-dive target count
    (``deep_total``); ``usable`` is its successfully deep-dived rows
    (db.list_deep_dived_results, failed dives excluded).
    """
    if candidates:
        return None
    errored_quick = sum(1 for r in quick_results if r.get("error"))
    noise = f" ({errored_quick} of {len(quick_results)} quick scans errored)" if errored_quick else ""

    if not n_targets:
        return (f"No ticker in the movers pre-screen scored BUY or SELL today — "
                f"every name came back HOLD{noise}. Nothing to trade.")
    if not usable:
        return (f"All {n_targets} deep dives failed{noise}. No contracts were vetted — "
                f"check the analysis history for the underlying error.")
    failed = max(0, n_targets - len(usable))
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
        return (f"{len(usable)} of {n_targets} deep dives completed, but every one "
                f"rated Hold after full analysis — no directional call to vet{extra}.")
    return (f"{directional_final} of {len(usable)} deep dives rated a directional call, but no "
            f"contract passed liquidity/delta/DTE vetting{extra} — see the vetting notes below.")


# ── Shared contract vetting ──────────────────────────────────────────────────
# Contract vetting (chain fetch + liquidity/delta/DTE selection) depends only on
# the shared research, not on the account, and every options account allocates
# at the open in parallel. Vet once per research row and hand each account its
# own copy: one set of Schwab chain calls instead of one per account, and every
# account sees identical contracts and quotes. Failures are never cached.
_VETTED_TTL_SECONDS = 10 * 60
_VETTED: dict[tuple[Any, ...], tuple[float, tuple[list[dict[str, Any]], list[str]]]] = {}
_VETTED_LOCKS: dict[tuple[Any, ...], threading.Lock] = {}
_VETTED_GUARD = threading.Lock()


def shared_vetted_candidates(
    research: dict[str, Any], usable: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """``options_data.fetch_candidates(usable)``, computed once per research row.

    Concurrent callers for the same research wait for the first one (single
    flight) and then share its result for ``_VETTED_TTL_SECONDS``. Returns deep
    copies so no account can mutate another's candidates.
    """
    key = (research.get("id"), research.get("created_at"))
    with _VETTED_GUARD:
        lock = _VETTED_LOCKS.setdefault(key, threading.Lock())
    with lock:
        now = time_mod.monotonic()
        hit = _VETTED.get(key)
        if hit is None or now - hit[0] >= _VETTED_TTL_SECONDS:
            result = options_data.fetch_candidates(usable)
            with _VETTED_GUARD:
                # Keep only the current entry; research rows are one per day.
                for stale in [k for k in _VETTED if k != key]:
                    _VETTED.pop(stale, None)
                    _VETTED_LOCKS.pop(stale, None)
                _VETTED[key] = (now, result)
            hit = _VETTED[key]
        return copy.deepcopy(hit[1])


def run_options_allocation(scan_id: int, trade_date: str) -> None:
    """Worker for one account's daily options allocation. Raises on failure
    (the endpoint's thread wrapper records failed/cancelled status).

    Settles expiries, then hands off to research_engine.run_allocation, which
    waits for the shared research and the open and calls ``_allocate`` under
    this account's allocation lock (other accounts allocate in parallel)."""
    scan = db.get_spy_scan(scan_id) or {}
    account_id = scan.get("paper_account_id")
    if not account_id:
        raise RuntimeError("options scan requires a paper_account_id")
    account = db.get_paper_account(int(account_id))
    if not account:
        raise RuntimeError(f"paper account {account_id} not found")
    account_id = int(account_id)
    stop_policy = account_policy.StopPolicy.from_account(account)
    log.info("[options %s] stop policy: %s", scan_id, account_policy.describe_policy(stop_policy))

    log.info("[options %s] starting for %s (account %s)", scan_id, trade_date, account_id)

    # Fund the account on first build; settle anything that expired since the
    # last run so carry-forward math never sees a dead position.
    if not db.has_options_deposit(account_id):
        db.append_options_cash(account_id, "deposit",
                               float(account.get("starting_capital") or 100_000.0),
                               scan_id=scan_id, note="initial deposit")
    with _phase("Expiry settlement failed"):
        settle_expired(account_id)

    held = [p["underlying"] for p in db.list_options_positions(account_id, status="open")]

    def _allocate(ctx: research_engine.AllocationContext) -> None:
        with _phase("Position mark-to-market failed"):
            refresh_positions(account_id)

        # Research rows come from db.list_deep_dived_results, which already
        # excludes failed dives; each carries entry_price = the live quote (or
        # None), which fetch_candidates uses as the chain spot hint.
        usable = ctx.usable
        with _phase("Chain fetch failed"):
            candidates, chain_notes = shared_vetted_candidates(ctx.research, usable)
        log.info("[options %s] %d vetted candidates from %d usable deep dives (research #%s)",
                 scan_id, len(candidates), len(usable), ctx.research.get("id"))

        open_positions = db.list_options_positions(account_id, status="open")
        eq = account_equity(account_id)
        realized = db.options_realized_pnl(account_id)
        starting_equity = eq["equity"]
        fresh_signals = {
            (r.get("ticker") or "").upper(): {"signal": (r.get("signal") or "").upper(),
                                              "conviction": r.get("conviction")}
            for r in ctx.quick_results if r.get("ticker")
        }

        # Learning loop, read side: latest batch-reflected lessons + mechanical
        # track record for THIS account (options_learning). Pure Python + one DB
        # read — zero LLM cost at scan time; "" until enough closes exist.
        lessons_context = ""
        try:
            settled = db.list_options_positions(account_id, status="settled")
            last_lesson = db.latest_options_lesson(account_id)
            stats = options_learning.compute_options_stats(
                settled, min_closed=int(ctx.config.get("options_lessons_min_closed", 10)))
            lessons_context = options_learning.format_track_record(
                stats, (last_lesson or {}).get("lessons_md"),
                max_chars=int(ctx.config.get("options_lessons_max_chars", 1200)))
            log.info("[options %s] lessons block: %d chars (%d closed)",
                     scan_id, len(lessons_context), len(settled))
        except Exception:
            log.exception("[options %s] lessons context failed — allocating without it", scan_id)

        with _phase("Options allocation failed"):
            alloc = options_allocator.run(
                candidates, open_positions, ctx.trade_date, ctx.config,
                equity=eq["equity"], cash=eq["cash"], realized_pnl=realized,
                aggressiveness=ctx.aggressiveness, bias=ctx.bias, fresh_signals=fresh_signals,
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
        reason = _zero_candidate_reason(ctx.quick_results, int(ctx.research.get("deep_total") or 0),
                                        usable, candidates)
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

    research_engine.run_allocation(scan_id, trade_date, _allocate, extra_tickers=held)
