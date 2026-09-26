"""The shared daily research routes, mounted by portfolio_main under the sp500
gate and served by nginx at /api/research. Detail and cancel reuse the generic
`/api/spy-scans/{id}` routes.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

from fastapi import APIRouter, HTTPException

from . import db, market_calendar, research_engine, scan_queue

log = logging.getLogger(__name__)

router = APIRouter()


def _run_research_thread(scan_id: int, trade_date: str) -> None:
    research_engine.run_worker(
        scan_id,
        trade_date,
        lambda s, t: research_engine.run_research(s, t),
        alert_kind="Research",
        holds_slot=True,
    )


@router.post("/api/research-scan")
def start_research_scan(body: dict[str, Any] | None = None) -> dict[str, Any]:
    today = market_calendar.today_et().isoformat()
    try:
        research_engine.require_trading_day(
            today, bool((body or {}).get("force"))
        )
    except research_engine.NotTradingDay as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {**research_engine.ensure_research_scan(today), "trade_date": today}


@router.get("/api/research-scans/today")
def research_scan_today() -> dict[str, Any]:
    td = market_calendar.today_et().isoformat()
    return {
        "trade_date": td,
        "scan": db.latest_research_scan(td),
        "attempts": db.count_research_attempts(td),
    }


@router.get("/api/research-scans")
def list_research_scans(limit: int = 20) -> dict[str, Any]:
    return {"scans": db.list_spy_scans(limit=limit, kind="research")}


scan_queue.register_runner(
    "research", sys.modules[__name__], "_run_research_thread"
)
