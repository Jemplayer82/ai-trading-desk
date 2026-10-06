"""Rules-only options paper account routes — T4 only (see web/rules_engine.py).

Mounted by web/portfolio_main.py when features.enabled("options"). Paths start with
/api/options-rules so nginx's existing /api/options location sends them to the portfolio app.
No LLM is involved in anything these routes start.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, HTTPException

from . import rules_engine

log = logging.getLogger(__name__)

router = APIRouter()


def _background(name: str, fn, **kw) -> None:
    def run() -> None:
        try:
            log.info("[rules] %s finished: %s", name, fn(**kw))
        except Exception:
            log.exception("[rules] %s failed", name)
    threading.Thread(target=run, name=f"rules-{name}", daemon=True).start()


@router.get("/api/options-rules")
def rules_summary() -> dict[str, Any]:
    return rules_engine.summary()


@router.post("/api/options-rules/accounts")
def create_rules_account(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    try:
        capital = float(body.get("starting_capital") or 50_000)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="starting_capital must be a number") from None
    if not 1_000 <= capital <= 10_000_000:
        raise HTTPException(status_code=400, detail="starting_capital must be between 1,000 and 10,000,000")
    if any(a["name"] == name for a in rules_engine.list_accounts()):
        raise HTTPException(status_code=409, detail=f"a rules account named {name!r} already exists")
    return {"id": rules_engine.create_account(name, capital)}


@router.post("/api/options-rules/prepare")
def start_prepare() -> dict[str, Any]:
    """After the close: pull earnings and Congress filings, write tomorrow's signals (background)."""
    if not rules_engine.list_accounts(active_only=True):
        raise HTTPException(status_code=409, detail="no rules accounts exist")
    _background("prepare", rules_engine.prepare)
    return {"started": True}


@router.post("/api/options-rules/run")
def start_run(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Near the close: exits, entries and idle money for every active rules account (background)."""
    if not rules_engine.list_accounts(active_only=True):
        raise HTTPException(status_code=409, detail="no rules accounts exist")
    force = bool((body or {}).get("force"))
    _background("run", rules_engine.run_daily, force=force)
    return {"started": True}
