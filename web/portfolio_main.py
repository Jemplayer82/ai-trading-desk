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

from . import alerts, auth_app, db, features, scan_queue
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
    _recover_interrupted_scans()


_INTERRUPTED_ERR = "interrupted — portfolio service restarted while this run was in progress"


# Allocation kind -> scan-queue dispatch key (equity allocations run on "spy").
_ALLOCATION_RUNNER_KEY = {"options": "options", "equity": "spy"}


def _today_et_iso() -> str:
    from . import market_calendar  # tier-agnostic stdlib module
    return market_calendar.today_et().isoformat()


def _resume_interrupted_allocations() -> list[int]:
    """Re-spawn today's allocation rows that were only waiting when we died.

    A restart between the 09:00 ET allocation kicks and the fills would
    otherwise fail every account for the day: the per-account cron jobs have
    already fired and the scheduler only re-kicks research. Rows still waiting
    for research / the open / the allocation lock have made no changes, and
    the allocation workers are safe to re-run from the top. Never raises.
    """
    try:
        rows = db.resume_interrupted_allocations(_today_et_iso())
    except Exception:
        log.exception("[startup] allocation resume lookup failed")
        return []
    resumed: list[int] = []
    for row in rows:
        target = scan_queue.resolve_runner(_ALLOCATION_RUNNER_KEY.get(row.get("kind") or "", ""))
        if target is None:
            continue  # left in running_wait_research; the fail sweep below closes it
        try:
            scan_queue.spawn_worker(target, int(row["id"]), row["trade_date"])
            resumed.append(int(row["id"]))
            log.warning("[startup] resumed interrupted %s allocation %s (account %s)",
                        row.get("kind"), row["id"], row.get("paper_account_id"))
        except Exception:
            log.exception("[startup] failed to resume allocation %s", row["id"])
    return resumed


def _recover_interrupted_scans() -> None:
    """Fail spy_scans rows orphaned by a crash/OOM/restart, then kick the queue.

    Safe because this single-process app owns every spy_scans worker thread, so
    nothing in flight can be alive at startup. Without this the dead rows block
    ensure_research_scan / start_allocation (they skip only failed/cancelled
    rows) until the scheduler reaper's 120-min stall trips, which can land past
    the 05:30 ET research retry cutoff. Never raises.
    """
    if features.enabled("sp500") or features.enabled("options"):
        resumed = _resume_interrupted_allocations()
        try:
            for row in db.fail_interrupted_spy_scans(_INTERRUPTED_ERR, exclude_ids=resumed):
                kind = row.get("kind")
                label = {
                    "options": "Options scan",
                    "research": "Research",
                    "equity": "S&P 500 scan",
                }.get(kind, "S&P 500 scan")
                if kind != "research" and (
                    row.get("research_scan_id") is not None
                    or row.get("status") == "running_wait_research"
                ):
                    label += " (allocation)"
                log.warning("[startup] failed interrupted %s %s (was %s)",
                            label, row["id"], row.get("status"))
                alerts.notify_run_failed(kind=label, run_id=row["id"],
                                         label=row.get("trade_date") or "",
                                         error=_INTERRUPTED_ERR)
        except Exception:
            log.exception("[startup] interrupted-scan recovery failed")
    # Queued rows stranded behind a dead worker start now, not at the next reaper sweep.
    try:
        scan_queue._advance_queue_if_idle()
    except Exception:
        log.exception("[startup] advance-queue kick failed")


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
