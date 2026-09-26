"""S&P 500 scanner + paper-account routes — T3 (S&P 500 scanner) only.

Split out of web/portfolio_main.py so the portfolio app's shell stays
byte-identical across every tier branch; portfolio_main.py mounts this router
only when features.enabled("sp500") is true (see web/features.py). Moved
verbatim — the _SCAN_LOCK hold spanning busy-check-and-create in start_spy_scan
and the "/latest/... before /{scan_id}/..." declaration order are both
load-bearing, do not reorder or simplify them in isolation.

Owns the S&P pipeline (_run_spy_scan): quick-screen all ~500 tickers, deep-dive
the top ~50 by conviction, then spy_allocator builds/rebalances a $100k paper
portfolio. Cancellation is cooperative via spy_scans.cancel_requested. Progress
contract: quick_count/quick_total + deep_count/deep_total, polled by the
frontend every 5s.

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
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from tradingagents.dataflows import schwab_mcp

from . import account_policy, alerts, db, scan_queue, spy_allocator, spy_scanner
from .research_engine import _phase
from .runner import build_config
from .spy_tickers import get_sp500_tickers

log = logging.getLogger(__name__)

router = APIRouter()


# Default auto-run time for a NEW account of each kind, matching the fixed
# crons the per-account schedules replace.
_DEFAULT_SCHEDULE_TIME = {"equity": "00:00", "options": "07:30"}


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
async def start_spy_scan(
    body: dict[str, Any] | None = None,
    background_tasks: BackgroundTasks = None,
) -> dict[str, Any]:
    """Trigger a full S&P 500 scan. Idempotent for today.

    Optional body: {account_id: int} — ties the scan to a paper account and
    inherits its starting_capital, aggressiveness, and bias settings.
    """
    body = body or {}
    today = datetime.utcnow().date().isoformat()

    account_id: int | None = body.get("account_id")
    account: dict[str, Any] | None = None
    if account_id:
        account = db.get_paper_account(int(account_id))
        if not account:
            raise HTTPException(status_code=404, detail="paper account not found")

    aggressiveness = int(body.get("aggressiveness") or (account or {}).get("aggressiveness") or 5)
    bias = body.get("bias") or (account or {}).get("bias") or "neutral"

    # Idempotency: don't create a second spy scan for the same account+date unless
    # the last failed/cancelled. Per-account so different paper accounts can each
    # have their own scan queued (cross-account concurrency is prevented by the
    # queue below); the kind filter keeps daily options runs from blocking the
    # equity scan.
    with db.connect() as conn:
        where = "trade_date = ? AND status NOT IN ('failed', 'cancelled') AND kind = 'equity'"
        params: list[Any] = [today]
        if account_id:
            where += " AND paper_account_id = ?"
            params.append(account_id)
        else:
            where += " AND paper_account_id IS NULL"
        row = conn.execute(
            f"SELECT id, status FROM spy_scans WHERE {where} ORDER BY id DESC LIMIT 1",
            params,
        ).fetchone()
    if row:
        return {"scan_id": int(row["id"]), "status": row["status"], "new": False}

    with scan_queue._SCAN_LOCK:  # see start_scan — busy-check + create must be atomic
        with db.connect() as conn:
            busy = scan_queue._is_any_scan_running(conn)
        if busy:
            scan_id = db.create_spy_scan(
                today,
                paper_account_id=account_id,
                aggressiveness=aggressiveness,
                bias=bias,
                status="queued",
            )
            log.info("[queue] spy scan %s queued behind %s scan #%s", scan_id, busy["scan_type"], busy["id"])
            return {"scan_id": scan_id, "status": "queued", "new": True, "queued_behind": busy}

        scan_id = db.create_spy_scan(
            today,
            paper_account_id=account_id,
            aggressiveness=aggressiveness,
            bias=bias,
        )
    background_tasks.add_task(_run_spy_scan_thread, scan_id, today)
    return {"scan_id": scan_id, "status": "running_quick", "new": True}


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


# ---------- S&P 500 scan worker ----------

def _run_spy_scan_thread(scan_id: int, trade_date: str) -> None:
    """Thread entry: route ScanCancelled to status 'cancelled', anything else to 'failed'."""
    scan_queue.refresh_creds_from_db()
    try:
        _run_spy_scan(scan_id, trade_date)
    except spy_scanner.ScanCancelled:
        # A user cancel is not a failure — don't alert.
        log.info("SPY scan %s cancelled by user", scan_id)
        db.update_spy_scan(scan_id, status="cancelled")
    except Exception as exc:
        log.exception("SPY scan %s crashed", scan_id)
        db.fail_spy_scan(scan_id, str(exc))
        alerts.notify_run_failed(
            kind="S&P 500 scan", run_id=scan_id, label=trade_date, error=str(exc)
        )
    finally:
        scan_queue._dequeue_next_scan()


def _run_spy_scan(scan_id: int, trade_date: str) -> None:
    """S&P scan worker: quick scan -> deep dives -> allocator.

    If the previous completed scan left active positions, its last refreshed
    value becomes this week's starting capital and the allocator runs in
    rebalance mode; otherwise it's a fresh $100k. The cancel flag is checked
    between phases here and per-ticker inside spy_scanner.
    """
    log.info("[spy %s] starting for %s", scan_id, trade_date)
    prefs = db.get_preferences() or {}
    selected_analysts = prefs.get("analysts") or ["market", "social", "news", "fundamentals"]

    # Read aggressiveness and bias from the scan row (set at creation from the account).
    scan_row = db.get_spy_scan(scan_id) or {}
    aggressiveness = int(scan_row.get("aggressiveness") or 5)
    bias = scan_row.get("bias") or "neutral"
    paper_account_id = scan_row.get("paper_account_id")

    config = build_config({**prefs, "aggressiveness": aggressiveness, "bias": bias})

    # Look up the previous completed scan for the same account to enable rebalancing.
    prev_scan = db.get_latest_completed_spy_scan(
        exclude_id=scan_id,
        paper_account_id=paper_account_id,
    )
    previous_portfolio: list[dict[str, Any]] | None = None
    previous_scan_id: int | None = None
    starting_value: float = float(
        (db.get_paper_account(paper_account_id) or {}).get("starting_capital") or 100_000.0
    ) if paper_account_id else 100_000.0

    if prev_scan:
        prev_portfolio_raw = prev_scan.get("portfolio_json") or []
        # Use the full previous portfolio (including EXITED/stopped rows) so the
        # allocator enters rebalance mode; active_prev is only for fallback capital.
        active_prev = [
            p for p in spy_allocator.live_positions(prev_portfolio_raw)
            if p.get("dollar_amount", 0) > 0
        ]
        previous_portfolio = prev_portfolio_raw
        previous_scan_id = int(prev_scan["id"])
        # Use last refreshed value as capital; fall back to sum of live allocations.
        if prev_scan.get("current_value"):
            starting_value = float(prev_scan["current_value"])
        elif active_prev:
            starting_value = float(sum(
                p.get("dollar_amount", 0) for p in active_prev
            )) or starting_value
        log.info(
            "[spy %s] rebalancing from scan #%s, capital $%s",
            scan_id, previous_scan_id, f"{starting_value:,.0f}",
        )

    # Phase 1: quick scan all S&P 500
    with _phase("Couldn't fetch the S&P 500 ticker list"):
        tickers = get_sp500_tickers()
    with _phase("Quick scan failed"):
        quick_results = spy_scanner.run_quick_scan(scan_id, tickers, trade_date, config)

    if db.is_spy_scan_cancelled(scan_id):
        raise spy_scanner.ScanCancelled()
    # Half or more of the quick scans erroring is infrastructure, not a quiet
    # market — fail rather than "degrade" into building a portfolio out of
    # error rows. This sits ABOVE the least-bad-50 fallback below on purpose:
    # that fallback exists for a pathological but WORKING scan, not a dead one.
    spy_scanner.assert_quick_scan_healthy(quick_results)

    # Phase 2: deep dive top 50 by conviction
    buy_or_hold = [r for r in quick_results if (r.get("signal") or "").upper() in ("BUY", "HOLD")]
    top50 = sorted(buy_or_hold, key=lambda r: -(r.get("conviction") or 0))[:50]
    if not top50:
        # Pathological quick scan (everything SELL or errored): deep-dive the
        # least-bad 50 anyway rather than abort the whole weekly run.
        top50 = sorted(quick_results, key=lambda r: -(r.get("conviction") or 0))[:50]
    with _phase("Deep-dive analysis failed"):
        enriched = spy_scanner.run_deep_dives(scan_id, top50, trade_date, config, selected_analysts)
    spy_scanner.assert_deep_dives_healthy(enriched)

    if db.is_spy_scan_cancelled(scan_id):
        raise spy_scanner.ScanCancelled()

    # Phase 3: allocator (rebalance if a previous portfolio exists, else fresh capital)
    db.update_spy_scan(scan_id, status="running_alloc")
    # A failed dive keeps its quick-scan signal/conviction (run_deep_dives
    # returns {**candidate, "error": ...}) and the allocator never inspects
    # `error` — so unfiltered, a partial deep-dive outage gets equal-weighted
    # into the paper portfolio at a fabricated entry_price. Drop those rows.
    usable = [e for e in enriched if not e.get("error")]
    if len(usable) != len(enriched):
        log.warning("[spy %s] dropping %d failed deep dives before allocation",
                    scan_id, len(enriched) - len(usable))
    with _phase("Portfolio allocation failed"):
        alloc_result = spy_allocator.run(
            usable,
            trade_date,
            config,
            previous_portfolio=previous_portfolio,
            starting_value=starting_value,
            aggressiveness=aggressiveness,
            bias=bias,
        )
    portfolio = alloc_result.get("allocations", [])

    db.complete_spy_scan(
        scan_id=scan_id,
        allocator_report=alloc_result.get("report_md", ""),
        portfolio_json=portfolio,
        previous_scan_id=previous_scan_id,
        starting_value=alloc_result.get("starting_value", starting_value),
    )
    log.info("[spy %s] done — %d positions, capital $%s → deployed $%s", scan_id,
             len(spy_allocator.live_positions(portfolio)),
             f"{starting_value:,.0f}", f"{alloc_result.get('total', 0):,.0f}")

    # Mark the fresh portfolio to market immediately so the table shows live
    # share counts / current prices / P&L without waiting for the hourly cron.
    try:
        spy_scanner.refresh_portfolio_prices(scan_id)
    except Exception:
        log.exception("[spy %s] initial price refresh failed (non-fatal)", scan_id)


# ---------- Scan-queue registration ----------
# See web/portfolio_routes.py for why this lives at module level / bottom-of-file.
scan_queue.register_runner("spy", sys.modules[__name__], "_run_spy_scan_thread")
