"""Daily options paper-trading routes — T4 (options paper trading) only.

Split out of web/portfolio_main.py so the portfolio app's shell stays
byte-identical across every tier branch; portfolio_main.py mounts this router
only when features.enabled("options") is true (see web/features.py). Moved
verbatim.

Options runs are spy_scans rows with kind='options' (same progress/cancel/
reaper machinery); positions + cash live in their own normalized tables. nginx
routes /api/options* to the portfolio app via its own location block. They are
per-account allocation rows over the shared daily research
(web/research_engine.py) and bypass the compute queue: they serialize only on
the global allocation lock, and only the shared research holds the compute
slot.

Paper-account CRUD is NOT here — it lives in web/spy_routes.py (T3) and serves
both equity and options accounts, since the tiers are cumulative.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

from fastapi import APIRouter, HTTPException

from . import db, market_calendar, options_engine, options_recommend, research_engine, scan_queue
from .runner import build_config

log = logging.getLogger(__name__)

router = APIRouter()


def _run_options_scan_thread(scan_id: int, trade_date: str) -> None:
    """Thread entry for one options allocation row (dispatch key 'options').

    Allocation rows never hold the compute slot, so the shared wrapper uses
    the idle-guarded queue advance rather than an unguarded dequeue."""
    research_engine.run_worker(
        scan_id, trade_date,
        lambda s, t: options_engine.run_options_allocation(s, t),
        alert_kind="Options allocation",
        holds_slot=False,
    )


@router.post("/api/options-scan")
def start_options_scan(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start today's options allocation. Body {account_id} runs one account;
    omitted (the scheduler's form) runs every options paper account.

    Each account gets an allocation row that waits for the shared daily
    research (kicked here if today has none yet). Non-trading days answer
    409 unless body {force: true}."""
    body = body or {}
    today = market_calendar.today_et().isoformat()
    force = bool(body.get("force"))
    account_id = body.get("account_id")
    if account_id:
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="account_id must be an integer") from None
        account = db.get_paper_account(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="paper account not found")
        if account.get("kind") != "options":
            raise HTTPException(status_code=400, detail="not an options paper account")
        accounts = [account]
    else:
        accounts = db.list_paper_accounts(kind="options")
        if not accounts:
            raise HTTPException(
                status_code=409,
                detail="no options paper accounts exist — create one first",
            )
    try:
        results = [
            research_engine.start_allocation(
                a, today, "options", force=force,
                aggressiveness=body.get("aggressiveness"), bias=body.get("bias"),
            )
            for a in accounts
        ]
    except research_engine.NotTradingDay as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if len(results) == 1:
        return {**results[0], "scans": results}
    return {"scans": results}


@router.get("/api/options-scans")
def list_options_scans(limit: int = 50, account_id: int | None = None) -> dict[str, Any]:
    return {"scans": db.list_spy_scans(limit=limit, paper_account_id=account_id, kind="options")}


@router.get("/api/options-scans/{scan_id}/status")
def get_options_scan_status(scan_id: int) -> dict[str, Any]:
    """Cheap poll target — see db.get_spy_scan_status for why this exists."""
    status = db.get_spy_scan_status(scan_id)
    if not status or status.get("kind") != "options":
        raise HTTPException(status_code=404, detail="not found")
    return status


@router.get("/api/options-scans/{scan_id}")
def get_options_scan(scan_id: int) -> dict[str, Any]:
    scan = db.get_spy_scan(scan_id)
    if not scan or scan.get("kind") != "options":
        raise HTTPException(status_code=404, detail="not found")
    scan["opened_positions"] = db.list_options_positions(open_scan_id=scan_id)
    scan["closed_positions"] = db.list_options_positions(close_scan_id=scan_id)
    if scan.get("paper_account_id"):
        scan["account_summary"] = options_engine.account_summary(int(scan["paper_account_id"]))
    return scan


@router.get("/api/options-positions")
def list_options_positions(
    account_id: int | None = None, status: str | None = None
) -> dict[str, Any]:
    """status: open | closed | expired_itm | expired_worthless | settled (any
    non-open) | omitted (all)."""
    return {"positions": db.list_options_positions(account_id, status=status)}


@router.post("/api/options-positions/refresh")
def refresh_options_positions() -> dict[str, Any]:
    """Settle due expiries + mark all open contracts to market (hourly cron + UI)."""
    return options_engine.refresh_positions()


@router.post("/api/options-positions/settle")
def settle_options_positions() -> dict[str, Any]:
    """Nightly expiry-settlement sweep (idempotent)."""
    return options_engine.settle_expired()


@router.get("/api/options-summary")
def options_summary(account_id: int) -> dict[str, Any]:
    if not db.get_paper_account(account_id):
        raise HTTPException(status_code=404, detail="paper account not found")
    return options_engine.account_summary(account_id)


@router.post("/api/options-recommend")
def options_recommend_endpoint(body: dict[str, Any]) -> dict[str, Any]:
    """On-demand options recommendation for one ticker (advisory only).

    Sync def on purpose: FastAPI runs it in the threadpool, and the flow makes
    two LLM calls + a chain fetch (~30-90s). nginx's /api/options prefix block
    routes this here with scan-grade timeouts. Applies DB-stored credentials
    first, same as the scan worker paths.
    """
    ticker = options_recommend.valid_ticker(str((body or {}).get("ticker") or ""))
    if not ticker:
        raise HTTPException(status_code=400, detail="invalid ticker")
    scan_queue.refresh_creds_from_db()
    prefs = db.get_preferences()
    config = build_config(dict(prefs))
    try:
        return options_recommend.recommend(ticker, config)
    except ValueError as exc:  # no price data / bad symbol
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("[recommend] failed for %s", ticker)
        raise HTTPException(status_code=502, detail=f"recommendation failed: {exc}") from exc


# ---------- Scan-queue registration ----------
# See web/portfolio_routes.py for why this lives at module level / bottom-of-file.
scan_queue.register_runner("options", sys.modules[__name__], "_run_options_scan_thread")
