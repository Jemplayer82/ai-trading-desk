"""FastAPI service dedicated to portfolio + S&P 500 + options scans.

This is a SEPARATE app from web/main.py, run in its own container
(`uvicorn web.portfolio_main:app`), so multi-hour scans never block the ad-hoc
api app. nginx (web/nginx.conf) routes /api/spy*, /api/accounts and
/api/portfolio* here; everything else under /api/ goes to the api app. That
routing is a hard contract: an endpoint added here without a matching nginx
location block is a silent 404 in production — this has bitten us before.

This module is only the shell — app construction, the auth middleware, startup,
the queue-introspection endpoints, and tier-gated router mounting. The actual
routes and scan workers live in modules mounted per feature tier (see
web/features.py):

- web/scan_queue.py      — tier-agnostic queue: the lock, the busy-check, the
                           dequeue/dispatch, and the runner registry the route
                           modules plug their workers into.
- web/portfolio_routes.py — T2: portfolio scans, live per-account holdings.
- web/spy_routes.py       — T3: paper accounts, S&P 500 scans, /api/spy-account.
- web/options_routes.py   — T4: daily options paper trading.

Everything persists to the shared SQLite DB (web/db.py). Credentials the user
saves in the api container's UI are re-read from that DB before each scan (see
scan_queue.refresh_creds_from_db), so a new key takes effect without restarting
this container.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from . import auth_app, db, features, scan_queue
from . import credentials as creds
from ._logging import configure_logging

log = logging.getLogger(__name__)
configure_logging()

app = FastAPI(title="TradingAgents Portfolio")

# Same login gate as the api container. Validates the shared sessions
# table; scheduler->portfolio cron calls pass via the X-Internal-Token
# bypass in auth_app.
app.middleware("http")(auth_app.auth_middleware)

# Tier-gated routes. Importing each module is what registers its scan worker
# with scan_queue (module-level register_runner calls), so this must happen at
# app-construction time — before anything can dequeue a queued scan.
if features.enabled("schwab"):
    from . import portfolio_routes
    app.include_router(portfolio_routes.router)
if features.enabled("sp500"):
    from . import research_routes, spy_routes
    app.include_router(research_routes.router)
    app.include_router(spy_routes.router)
if features.enabled("options"):
    from . import options_routes
    app.include_router(options_routes.router)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    creds.apply_to_env()
    creds.apply_settings_to_env()
    # Clear any LLM-activity rows left stale by a previous crash so the
    # scanner's dynamic concurrency starts from an accurate count.
    try:
        db.purge_stale_activity()
    except Exception:
        log.exception("[startup] purge_stale_activity failed")


# ---------- Scan queue introspection ----------
# Tier-agnostic (the queue itself is), so these stay in the shell rather than
# moving into a route module.

@app.get("/api/portfolio/status")
def scan_status() -> dict[str, Any]:
    """Current scan queue state — used by the frontend and by agents before triggering.

    Returns the actively running scan (if any), the ordered queue of waiting scans,
    and a ``waiting`` list of scans in any of ``scan_queue.WAITING_STATUSES`` —
    parked on the shared research row (``running_wait_research``), the 09:35 ET
    market open (``running_wait_market``) or the allocation lock
    (``running_wait_alloc``). They are alive and heartbeating, but not holding
    the compute slot.

    The ``running`` object carries ``scan_type``, ``id``, ``trade_date``, ``kind``,
    ``created_at``, ``status``, and progress fields: for a portfolio row,
    ``scanned_count`` / ``scan_total`` / ``current_ticker`` with the spy counters
    NULL; for a spy row, ``quick_count`` / ``quick_total`` / ``deep_count`` /
    ``deep_total`` with the portfolio counters NULL. ``waiting`` rows carry the
    same base keys plus the spy progress counters too. The nginx /api/portfolio
    prefix block routes this to the portfolio app.
    """
    with db.connect() as conn:
        running_row = scan_queue._is_any_scan_running(conn)
        queued_rows = conn.execute(
            "SELECT 'portfolio' AS scan_type, id, trade_date, 'equity' AS kind, created_at"
            " FROM portfolio_scans WHERE status = 'queued'"
            " UNION SELECT 'spy', id, trade_date, kind, created_at"
            " FROM spy_scans WHERE status = 'queued'"
            " ORDER BY created_at"
        ).fetchall()
        waiting_placeholders = ",".join("?" for _ in scan_queue.WAITING_STATUSES)
        waiting_rows = conn.execute(
            "SELECT 'spy' AS scan_type, id, trade_date, kind, created_at, status,"
            " quick_count, quick_total, deep_count, deep_total"
            f" FROM spy_scans WHERE status IN ({waiting_placeholders}) ORDER BY created_at",
            scan_queue.WAITING_STATUSES,
        ).fetchall()
    return {
        "running": dict(running_row) if running_row else None,
        "queued": [dict(r) for r in queued_rows],
        "waiting": [dict(r) for r in waiting_rows],
    }


@app.post("/api/portfolio/advance-queue")
def advance_queue() -> dict[str, Any]:
    """Internal recovery hook (reaper → here): kick the scan queue if idle.

    nginx routes /api/portfolio* to this app; the scheduler calls it directly
    over the internal network with X-Internal-Token. No-op when a scan runs."""
    started = scan_queue._advance_queue_if_idle()
    return {"started": started}
