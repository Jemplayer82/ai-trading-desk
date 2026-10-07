"""Rules-only credit-spread paper accounts (web/spread_engine.py) — T4 only. No LLM.

Paths start with /api/options-spreads so nginx's existing /api/options location routes them to the
portfolio app. Mounted by web/portfolio_main.py when features.enabled("options").
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, HTTPException

from . import spread_engine

log = logging.getLogger(__name__)

router = APIRouter()


def _background(name: str, fn) -> None:
    def run() -> None:
        try:
            log.info("[spreads] %s finished: %s", name, fn())
        except Exception:
            log.exception("[spreads] %s failed", name)
    threading.Thread(target=run, name=f"spreads-{name}", daemon=True).start()


@router.get("/api/options-spreads")
def spreads_summary() -> dict[str, Any]:
    return spread_engine.summary()


@router.post("/api/options-spreads/accounts")
def create_spread_account(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    name = str(body.get("name") or "").strip()
    structure = str(body.get("structure") or "condor")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if structure not in ("condor", "vertical"):
        raise HTTPException(status_code=400, detail="structure must be 'condor' or 'vertical'")
    try:
        capital = float(body.get("starting_capital") or 50_000)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="starting_capital must be a number") from None
    if not 1_000 <= capital <= 10_000_000:
        raise HTTPException(status_code=400, detail="starting_capital must be between 1,000 and 10,000,000")
    if any(a["name"] == name for a in spread_engine.list_accounts()):
        raise HTTPException(status_code=409, detail=f"a spread account named {name!r} already exists")
    try:
        new_id = spread_engine.create_account(name, structure, capital)
    except Exception as exc:  # e.g. a same-name race hitting the UNIQUE constraint
        raise HTTPException(status_code=409, detail=f"could not create the account: {exc}") from None
    spread_engine.start_loop()
    return {"id": new_id}


@router.post("/api/options-spreads/scan")
def start_scan() -> dict[str, Any]:
    if not spread_engine.list_accounts(active_only=True):
        raise HTTPException(status_code=409, detail="no spread accounts exist")
    _background("scan", spread_engine.run_scan)
    return {"started": True}


@router.post("/api/options-spreads/eod")
def start_eod() -> dict[str, Any]:
    if not spread_engine.list_accounts(active_only=True):
        raise HTTPException(status_code=409, detail="no spread accounts exist")
    _background("eod", spread_engine.end_of_day)
    return {"started": True}
