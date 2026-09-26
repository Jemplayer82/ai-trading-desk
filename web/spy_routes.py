"""S&P 500 paper-account routes + equity allocation worker — T3 (S&P 500 scanner) only.

Split out of web/portfolio_main.py so the portfolio app's shell stays
byte-identical across every tier branch; portfolio_main.py mounts this router
only when features.enabled("sp500") is true (see web/features.py). The
"/latest/... before /{scan_id}/..." declaration order is load-bearing; do not
reorder it.

There is no weekly S&P pipeline any more. The shared daily research (quick
screen + deep dives) lives in web/research_engine.py and web/research_routes.py;
this module owns the per-account EQUITY ALLOCATION worker
(_run_equity_allocation): it waits for today's research and the open, marks the
prior portfolio to market at live quotes, then spy_allocator rebalances it with
cadence="daily". Cancellation is cooperative via spy_scans.cancel_requested.

Also owns paper-account CRUD for BOTH equity and options accounts: tiers are
cumulative, so T4's options accounts reuse the T3 routes rather than
duplicating them.

/api/spy-account* read live Schwab data but live here rather than in the T2
module: they sit under nginx's /api/spy prefix, and T3 is cumulative on top of
T2 so Schwab is always available wherever these exist.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from tradingagents.dataflows import schwab_mcp

from . import (
    account_policy,
    db,
    market_calendar,
    research_engine,
    scan_queue,
    spy_allocator,
    spy_scanner,
)
from .research_engine import _phase

log = logging.getLogger(__name__)

router = APIRouter()


# The default allocation time for NEW accounts of each kind. The shared
# research runs at 00:00 ET and allocation fills wait for 09:35 ET, so a 09:00
# start just queues the account's row to fill at the open.
_DEFAULT_SCHEDULE_TIME = {"equity": "09:00", "options": "09:00"}


def _clean_schedule_time(raw: Any) -> str | None:
    """Validate an 'HH:MM' 24-hour time; None/'' means manual-only (NULL)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if account_policy.parse_hhmm(s) is None:
        raise HTTPException(status_code=400,
                            detail="schedule_time must be HH:MM (24-hour), or null for manual-only")
    return s


def _clean_stop_policy(stop_type: Any, stop_value: Any, stop_limit_offset: Any) -> account_policy.StopPolicy:
    try:
        return account_policy.validate_policy(stop_type, stop_value, stop_limit_offset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------- Paper trading accounts ----------

@router.get("/api/paper-accounts")
def list_paper_accounts(kind: str | None = None) -> dict[str, Any]:
    """kind filter: 'equity' (S&P tab) or 'options' (Options tab); omit for all."""
    return {"accounts": db.list_paper_accounts(kind=kind)}


@router.post("/api/paper-accounts")
def create_paper_account(body: dict[str, Any]) -> dict[str, Any]:
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    kind = (body.get("kind") or "equity").strip().lower()
    if kind not in ("equity", "options"):
        raise HTTPException(status_code=400, detail="kind must be 'equity' or 'options'")
    bias = (body.get("bias") or "neutral").strip().lower()
    if bias not in ("bullish", "neutral", "bearish"):
        raise HTTPException(status_code=400, detail="bias must be bullish, neutral, or bearish")
    schedule_time = _clean_schedule_time(body.get("schedule_time", _DEFAULT_SCHEDULE_TIME[kind]))
    policy = _clean_stop_policy(body.get("stop_type"), body.get("stop_value"), body.get("stop_limit_offset"))
    try:
        account_id = db.create_paper_account(
            name=name,
            starting_capital=float(body.get("starting_capital") or 100_000),
            aggressiveness=int(body.get("aggressiveness") or 5),
            bias=bias,
            kind=kind,
            schedule_time=schedule_time,
            stop_type=policy.stop_type,
            stop_value=policy.stop_value,
            stop_limit_offset=policy.stop_limit_offset,
        )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=409, detail=f"Account '{name}' already exists") from exc
        raise
    account = db.get_paper_account(account_id)
    return {"account": account}


@router.put("/api/paper-accounts/{account_id}")
def update_paper_account(account_id: int, body: dict[str, Any]) -> dict[str, Any]:
    if not db.get_paper_account(account_id):
        raise HTTPException(status_code=404, detail="not found")
    if body.get("bias") and body["bias"] not in ("bullish", "neutral", "bearish"):
        raise HTTPException(status_code=400, detail="bias must be bullish, neutral, or bearish")
    fields: dict[str, Any] = {}
    if "name" in body:
        name = (body["name"] or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        fields["name"] = name
    if "starting_capital" in body:
        if body["starting_capital"] is None:
            raise HTTPException(status_code=400, detail="starting_capital is required")
        try:
            fields["starting_capital"] = float(body["starting_capital"])
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="starting_capital must be a number") from exc
    if "aggressiveness" in body:
        if body["aggressiveness"] is None:
            raise HTTPException(status_code=400, detail="aggressiveness is required")
        try:
            fields["aggressiveness"] = int(body["aggressiveness"])
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="aggressiveness must be an integer") from exc
    if "bias" in body:
        fields["bias"] = body["bias"]
    if "schedule_time" in body:
        fields["schedule_time"] = _clean_schedule_time(body["schedule_time"])
    if "stop_type" in body or "stop_value" in body or "stop_limit_offset" in body:
        current = db.get_paper_account(account_id) or {}
        policy = _clean_stop_policy(
            body["stop_type"] if "stop_type" in body else current.get("stop_type"),
            body["stop_value"] if "stop_value" in body else current.get("stop_value"),
            body["stop_limit_offset"] if "stop_limit_offset" in body else current.get("stop_limit_offset"),
        )
        fields["stop_type"] = policy.stop_type
        fields["stop_value"] = policy.stop_value
        fields["stop_limit_offset"] = policy.stop_limit_offset
    try:
        db.update_paper_account(account_id=account_id, **fields)
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=409, detail=f"Account '{fields.get('name')}' already exists") from exc
        raise
    return {"account": db.get_paper_account(account_id)}


@router.delete("/api/paper-accounts/{account_id}")
def delete_paper_account(account_id: int) -> dict[str, Any]:
    if not db.delete_paper_account(account_id):
        raise HTTPException(status_code=404, detail="not found")
    return {"status": "deleted", "id": account_id}


# ---------- S&P 500 scanner endpoints ----------

@router.post("/api/spy-scan")
def start_spy_scan(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start today's equity allocation for one S&P paper account. Idempotent.

    Body {account_id: int} is required. The allocation row waits for the
    shared daily research (kicked here if today has none yet) and fills at
    live quotes after the open. Non-trading days answer 409 unless
    body {force: true}. Optional aggressiveness/bias override the account's.
    """
    body = body or {}
    raw_id = body.get("account_id")
    if raw_id is None or raw_id == "":
        raise HTTPException(
            status_code=400,
            detail="account_id is required — S&P accounts allocate from the shared daily research",
        )
    try:
        account_id = int(raw_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="account_id must be an integer") from None
    account = db.get_paper_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="paper account not found")
    if account.get("kind") != "equity":
        raise HTTPException(status_code=400, detail="not an equity (S&P) paper account")

    today = market_calendar.today_et().isoformat()
    try:
        return research_engine.start_allocation(
            account, today, "equity",
            force=bool(body.get("force")),
            aggressiveness=body.get("aggressiveness"),
            bias=body.get("bias"),
        )
    except research_engine.NotTradingDay as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.get("/api/spy-scans")
def list_spy_scans(
    limit: int = 50,
    account_id: int | None = None,
    status: list[str] | None = Query(default=None),
    kind: str = "equity",
) -> dict[str, Any]:
    return {"scans": db.list_spy_scans(limit=limit, paper_account_id=account_id, statuses=status, kind=kind)}


@router.get("/api/spy-scans/{scan_id}/status")
def get_spy_scan_status(scan_id: int) -> dict[str, Any]:
    """Cheap poll target — see db.get_spy_scan_status for why this exists."""
    status = db.get_spy_scan_status(scan_id)
    if not status:
        raise HTTPException(status_code=404, detail="not found")
    return status


@router.get("/api/spy-scans/{scan_id}")
def get_spy_scan(scan_id: int) -> dict[str, Any]:
    scan = db.get_spy_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="not found")
    return scan


@router.delete("/api/spy-scans/{scan_id}")
def delete_spy_scan_endpoint(scan_id: int) -> dict[str, Any]:
    if not db.delete_spy_scan(scan_id):
        raise HTTPException(status_code=404, detail="not found")
    return {"status": "deleted", "id": scan_id}


@router.delete("/api/spy-scans")
def delete_all_spy_scans_endpoint(kind: str = "equity") -> dict[str, Any]:
    return {"status": "deleted", "count": db.delete_all_spy_scans(kind=kind)}


@router.post("/api/spy-scans/{scan_id}/cancel")
def cancel_spy_scan(scan_id: int) -> dict[str, Any]:
    """Cooperatively cancel a running S&P 500 scan.

    Sets a flag the scan worker polls between LLM calls; the worker stops
    submitting new work, lets in-flight calls finish, and marks the scan
    'cancelled'. Returns immediately.
    """
    scan = db.get_spy_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="not found")
    if not str(scan.get("status", "")).startswith("running"):
        return {"status": scan.get("status"), "cancelling": False}
    db.request_spy_scan_cancel(scan_id)
    return {"status": "cancelling", "cancelling": True}


# NOTE: /latest/... must come BEFORE /{scan_id}/... — FastAPI matches routes
# in declaration order and would greedily bind "latest" as an int scan_id
# (returning 422) if the parameterised route is declared first.
@router.post("/api/spy-scans/latest/refresh-prices")
def refresh_spy_prices_latest() -> dict[str, Any]:
    """Hourly cron entry point: mark every equity paper account to market.

    500s only on a genuine total outage. Accounts that merely had nothing to
    re-price -- ``{"skipped": "no portfolio yet"}``, the normal state until an
    account's first weekly allocation runs -- come back 200 with their
    per-account entry intact in the body, and never trip the outage alert.
    """
    result = spy_scanner.refresh_all_portfolio_prices(kind="equity")
    if not result.get("scans"):
        raise HTTPException(status_code=404, detail="no scans found")
    # Outage classification is shared with spy_scanner.refresh_all_portfolio_prices,
    # which calls the same predicate to decide whether to page the user. Do not
    # re-derive it inline here -- a duplicated copy is how the 500 and the alert
    # would drift apart.
    if spy_scanner.is_total_price_refresh_outage(result.get("scans") or {}):
        raise HTTPException(status_code=500, detail=result)
    return result


@router.post("/api/spy-scans/{scan_id}/refresh-prices")
def refresh_spy_prices(scan_id: int) -> dict[str, Any]:
    return spy_scanner.refresh_portfolio_prices(scan_id)


# ---------- Live Schwab account (read-only, via Schwab MCP) ----------

def _parse_schwab_account(accounts: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Aggregate a Schwab getAccounts payload (possibly several accounts) into a
    single combined view: positions summed by symbol, plus total cash + value.

    Balance fields vary by account: currentBalances often omits
    liquidationValue/cashBalance, so we fall back to equity / initialBalances.

    DEPRECATED: this is the legacy Schwab-only parser kept for the /api/spy-account
    drift views. New per-account/holdings code goes through the brokerage-agnostic
    ``brokerages.fetch_all_accounts()`` (web/brokerages.py), which also handles
    options and multiple providers. Migrate /api/spy-account to it when convenient.
    """
    if not accounts:
        return None

    agg: dict[str, dict[str, Any]] = {}
    cash = 0.0
    total_value = 0.0
    for a in accounts:
        sec = a.get("securitiesAccount") or a
        for p in sec.get("positions") or []:
            instr = p.get("instrument") or {}
            sym = instr.get("symbol")
            qty = float(p.get("longQuantity") or 0) - float(p.get("shortQuantity") or 0)
            if not sym or qty == 0:
                continue
            e = agg.setdefault(sym, {"symbol": sym, "shares": 0.0, "market_value": 0.0, "_cost": 0.0})
            e["shares"] += qty
            e["market_value"] += float(p.get("marketValue") or 0)
            e["_cost"] += float(p.get("averagePrice") or 0) * qty

        cur = sec.get("currentBalances") or {}
        init = sec.get("initialBalances") or {}
        tv = cur.get("liquidationValue")
        if tv is None:
            tv = cur.get("equity")
        if tv is None:
            tv = init.get("liquidationValue") or init.get("accountValue") or 0
        total_value += float(tv or 0)
        c = cur.get("cashBalance")
        if c is None:
            c = init.get("cashBalance")
        if c is None:
            c = init.get("totalCash") or 0
        cash += float(c or 0)

    positions = []
    for e in agg.values():
        shares = e["shares"]
        positions.append({
            "symbol": e["symbol"],
            "shares": round(shares, 4),
            "market_value": round(e["market_value"], 2),
            "average_price": round(e["_cost"] / shares, 2) if shares else 0,
        })
    positions.sort(key=lambda x: -x["market_value"])
    return {
        "positions": positions,
        "cash": round(cash, 2),
        "liquidation_value": round(total_value, 2),
        "num_accounts": len(accounts),
    }


@router.get("/api/spy-account")
def spy_account() -> dict[str, Any]:
    """Live Schwab holdings + balances via the Schwab MCP server (read-only)."""
    if not schwab_mcp.schwab_enabled():
        return {"enabled": False, "connected": False}
    try:
        parsed = _parse_schwab_account(schwab_mcp.get_accounts(fields="positions"))
    except Exception:
        log.exception("[spy-account] Schwab MCP read failed")
        parsed = None
    if not parsed:
        return {"enabled": True, "connected": False}
    return {"enabled": True, "connected": True, **parsed}


@router.get("/api/spy-account/compare")
def spy_account_compare() -> dict[str, Any]:
    """Drift between the latest completed paper SPY portfolio and the real account."""
    if not schwab_mcp.schwab_enabled():
        return {"enabled": False, "connected": False}
    try:
        parsed = _parse_schwab_account(schwab_mcp.get_accounts(fields="positions"))
    except Exception:
        log.exception("[spy-account/compare] Schwab MCP read failed")
        parsed = None
    if not parsed:
        return {"enabled": True, "connected": False}

    real = {p["symbol"]: p for p in parsed["positions"]}
    scan = db.get_latest_completed_spy_scan()
    paper: dict[str, dict[str, Any]] = {}
    paper_value = 0.0
    if scan:
        for a in spy_allocator.live_positions(scan.get("portfolio_json") or []):
            shares = a.get("shares") or 0
            if shares <= 0:
                continue
            val = a.get("current_value")
            if val is None:
                val = shares * (a.get("current_price") or a.get("entry_price") or 0)
            paper[a["ticker"]] = {"shares": shares, "value": float(val)}
            paper_value += float(val)

    rows = []
    for sym in sorted(set(real) | set(paper)):
        pp = paper.get(sym)
        rr = real.get(sym)
        rows.append({
            "ticker": sym,
            "paper_shares": pp["shares"] if pp else 0,
            "paper_value": round(pp["value"], 2) if pp else 0,
            "real_shares": rr["shares"] if rr else 0,
            "real_value": round(rr["market_value"], 2) if rr else 0,
            "in_paper": pp is not None,
            "in_real": rr is not None,
        })

    return {
        "connected": True,
        "paper_scan_id": scan.get("id") if scan else None,
        "paper_value": round(paper_value, 2),
        "real_positions_value": round(sum(p["market_value"] for p in parsed["positions"]), 2),
        "real_cash": round(parsed["cash"], 2),
        "real_total": round(parsed["liquidation_value"], 2),
        "rows": rows,
    }


# ---------- Equity allocation worker ----------

def _run_spy_scan_thread(scan_id: int, trade_date: str) -> None:
    """Thread entry for one equity allocation row (runner key 'spy').

    Allocation rows never hold the compute slot, so the shared wrapper uses the
    idle-guarded queue advance rather than an unguarded dequeue.
    """
    research_engine.run_worker(
        scan_id, trade_date,
        lambda s, t: _run_equity_allocation(s, t),
        alert_kind="S&P 500 allocation",
        holds_slot=False,
    )


def _previous_portfolio_state(
    prev: dict[str, Any] | None, account: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, int | None, float]:
    """Return (previous_portfolio, previous_scan_id, starting_value).

    previous_portfolio is the FULL previous portfolio (including EXITED/stopped
    rows) so the allocator enters rebalance mode. starting_value is the prev
    scan's BOOK value: its last refreshed value minus the unrealized P&L of its
    live positions (i.e. cash + cost basis), else the sum of its live
    allocations, else the account's starting_capital. With no prev:
    (None, None, starting_capital).

    Why book value, not market value: retained HOLD/ADDED/TRIMMED rows keep
    their original entry_price, so the new scan's cost_basis is at cost and
    refresh_portfolio_prices derives cash = starting_value - cost_basis. A
    market-based starting_value would count the unrealized gain once in cash
    and again in the positions' marks, compounding phantom cash (or phantom
    losses) on every daily allocation and overstating the allocator's free cash.
    """
    starting_value = float((account or {}).get("starting_capital") or 100_000.0)
    if not prev:
        return None, None, starting_value

    prev_portfolio_raw = prev.get("portfolio_json") or []
    # active_prev is only for fallback capital.
    active_prev = [
        p for p in spy_allocator.live_positions(prev_portfolio_raw)
        if p.get("dollar_amount", 0) > 0
    ]
    previous_portfolio = prev_portfolio_raw
    previous_scan_id = int(prev["id"])
    # Use last refreshed value as capital; fall back to sum of live allocations.
    if prev.get("current_value"):
        unrealized = sum(
            float(p["current_value"]) - float(p["cost_basis"])
            for p in spy_allocator.live_positions(prev_portfolio_raw)
            if p.get("current_value") is not None and p.get("cost_basis") is not None
            and (p.get("shares") or 0) > 0
        )
        starting_value = float(prev["current_value"]) - unrealized
    elif active_prev:
        starting_value = float(sum(
            p.get("dollar_amount", 0) for p in active_prev
        )) or starting_value
    return previous_portfolio, previous_scan_id, starting_value


def _run_equity_allocation(scan_id: int, trade_date: str) -> None:
    """Daily equity allocation for one paper account over the shared research.

    research_engine.run_allocation waits for today's research and the open,
    then (under the global allocation lock) fetches live quotes and calls
    _allocate. The previous portfolio is marked to market first, so stops fire
    at live quotes and the rebalance starts from today's value. With no live
    quotes the row fails and the previous scan stays the latest completed one.
    """
    scan = db.get_spy_scan(scan_id) or {}
    pid = scan.get("paper_account_id")
    if pid is None:
        raise RuntimeError("equity allocation requires a paper_account_id")

    # kind defaults to equity: the last weekly (Saturday) scan counts, so day
    # one rebalances from it.
    prev0 = db.get_latest_completed_spy_scan(exclude_id=scan_id, paper_account_id=pid)
    held0 = [p["ticker"] for p in spy_allocator.live_positions((prev0 or {}).get("portfolio_json") or [])]

    def _allocate(ctx: research_engine.AllocationContext) -> None:
        prev = prev0
        if prev0:
            # Live marks + stop enforcement at live quotes. Non-fatal.
            try:
                r = spy_scanner.refresh_portfolio_prices(int(prev0["id"]))
                if isinstance(r, dict) and r.get("error"):
                    log.warning("[spy %s] pre-allocation refresh of scan #%s failed: %s",
                                scan_id, prev0["id"], r.get("error"))
            except Exception:
                log.exception("[spy %s] pre-allocation refresh of scan #%s crashed (non-fatal)",
                              scan_id, prev0["id"])
            prev = db.get_spy_scan(int(prev0["id"])) or prev0

        previous_portfolio, previous_scan_id, starting_value = _previous_portfolio_state(prev, ctx.account)
        if prev:
            log.info(
                "[spy %s] rebalancing from scan #%s, capital $%s",
                scan_id, previous_scan_id, f"{starting_value:,.0f}",
            )

        held = {p["ticker"].upper() for p in spy_allocator.live_positions(previous_portfolio or [])}
        if (ctx.usable or held) and not ctx.live_prices:
            raise RuntimeError("no live quotes at the open — allocation skipped; holdings unchanged")

        candidates = research_engine.equity_candidates(ctx.usable, held)
        if not candidates and not held:
            raise RuntimeError(
                "no priced Buy/Overweight/Hold candidates and nothing held — nothing to allocate today")

        with _phase("Portfolio allocation failed"):
            alloc = spy_allocator.run(
                candidates,
                ctx.trade_date,
                ctx.config,
                previous_portfolio=previous_portfolio,
                starting_value=starting_value,
                aggressiveness=ctx.aggressiveness,
                bias=ctx.bias,
                cadence="daily",
            )
        portfolio = alloc.get("allocations", [])

        db.complete_spy_scan(
            scan_id=scan_id,
            allocator_report=alloc.get("report_md", ""),
            portfolio_json=portfolio,
            previous_scan_id=previous_scan_id,
            starting_value=alloc.get("starting_value", starting_value),
        )
        log.info("[spy %s] done — %d positions, capital $%s → deployed $%s", scan_id,
                 len(spy_allocator.live_positions(portfolio)),
                 f"{starting_value:,.0f}", f"{alloc.get('total', 0):,.0f}")

        # Mark the fresh portfolio to market immediately so the table shows live
        # share counts / current prices / P&L without waiting for the hourly cron.
        try:
            spy_scanner.refresh_portfolio_prices(scan_id)
        except Exception:
            log.exception("[spy %s] initial price refresh failed (non-fatal)", scan_id)

    research_engine.run_allocation(scan_id, trade_date, _allocate, extra_tickers=held0)


# ---------- Scan-queue registration ----------
# See web/portfolio_routes.py for why this lives at module level / bottom-of-file.
scan_queue.register_runner("spy", sys.modules[__name__], "_run_spy_scan_thread")
