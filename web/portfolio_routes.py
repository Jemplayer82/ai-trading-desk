"""Portfolio-scan + live-accounts routes — T2 (Schwab brokerage) only.

Split out of web/portfolio_main.py so the portfolio app's shell stays
byte-identical across every tier branch; portfolio_main.py mounts this router
only when features.enabled("schwab") is true (see web/features.py). Moved
verbatim — the _SCAN_LOCK hold spanning busy-check-and-create in start_scan is
load-bearing (two scans in one 4g container reproduces a host OOM), do not
simplify it in isolation.

Owns the portfolio scan pipeline: real holdings via
brokerages.fetch_all_accounts() (normalized cross-brokerage dicts, account ids
namespaced "schwab:12345678"), each equity through SwitchboardOrchestrator,
then the aggregator briefing. Option positions are display-only on the
dashboard and are skipped (logged) before the analysis loop.

Progress contract: _run_scan writes scan_total once, then an initial progress
update (scanned_count=0, current_ticker=<first ticker>) before the pool starts
so the frontend banner shows "0/N analyzed, <first ticker>" immediately.
After each holding finishes, scanned_count / current_ticker is updated again.
scanned_count is "completed-so-far" and reaches the full scan_total when the
last holding finishes (previously it topped out at N-1 because it was written
before each ticker started). current_ticker now reports the most recently
completed holding, since under concurrency there is no single "in-flight"
ticker. Concurrency is bounded by the same cross-container
OLLAMA_MAX_CONCURRENCY budget the S&P scanner uses (via
_total_budget/_GateMonitor), so single-ticker ad-hoc analyses keep priority
and the scan never fully starves (floors at 1 worker).
"""
from __future__ import annotations

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from tradingagents.constants import SIGNALS
from tradingagents.dataflows import schwab_mcp

from . import alerts, brokerages, db, scan_queue
from .llm_helpers import DynamicGate, _GateMonitor, _total_budget
from .portfolio import aggregator
from .runner import build_config

log = logging.getLogger(__name__)

router = APIRouter()


# NOTE: /api/health and /api/auth/schwab/status below are UNREACHABLE in
# production — nginx's generic /api/ block sends both to the api container
# instead (see the ROUTE PRIORITY header comment in web/nginx.conf). They are
# kept verbatim rather than deleted so this split changes no behavior.

@router.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "portfolio"}


@router.get("/api/auth/schwab/status")
def schwab_status() -> dict[str, Any]:
    """Schwab connectivity via the MCP server (the scheduler hits whichever container).

    `enabled` is the master SCHWAB_ENABLED switch; `connected` reflects whether
    the MCP server currently returns account data (its Schwab session is authed).
    """
    if not schwab_mcp.schwab_enabled():
        return {"enabled": False, "connected": False, "source": "mcp"}
    accounts = None
    try:
        accounts = schwab_mcp.get_accounts(fields="positions")
    except Exception:
        log.debug("[schwab_status] MCP read failed", exc_info=True)
    return {
        "enabled": True,
        "connected": bool(accounts),
        "num_accounts": len(accounts) if isinstance(accounts, list) else 0,
        "source": "mcp",
    }


@router.post("/api/portfolio-scan")
async def start_scan(
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Kick off a portfolio scan. Idempotent for the same date — returns the
    existing scan_id if a non-failed scan was already created today.

    Optional body: {aggressiveness: 1-10, bias: bullish|neutral|bearish}.
    """
    body = body or {}
    aggressiveness = int(body.get("aggressiveness") or 5)
    bias = body.get("bias") or "neutral"
    today = datetime.utcnow().date().isoformat()
    # Idempotency check: don't create a second portfolio scan for today unless the last one failed.
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, status FROM portfolio_scans WHERE trade_date = ? AND status != 'failed' ORDER BY id DESC LIMIT 1",
            (today,),
        ).fetchone()
    if row:
        return {"scan_id": int(row["id"]), "status": row["status"], "new": False}

    if not schwab_mcp.schwab_enabled():
        raise HTTPException(status_code=400, detail="Schwab is disabled (SCHWAB_ENABLED=0). Enable it in Settings to run a portfolio scan.")
    if not schwab_mcp.get_accounts(fields="positions"):
        raise HTTPException(status_code=400, detail="Schwab MCP not connected — re-authorize at https://schwab.txferguson.net/auth")

    # Lock spans the busy-check AND the create: db.connect() is autocommit with
    # no transaction around the pair, so without it two near-simultaneous
    # callers can both read "nothing running" and both start a scan — which
    # puts two scans in the same 4g-capped container and reproduces the host
    # OOM the queue exists to prevent.
    with scan_queue._SCAN_LOCK:
        with db.connect() as conn:
            busy = scan_queue._is_any_scan_running(conn)
        if busy:
            scan_id = db.create_portfolio_scan(today, status="queued")
            log.info("[queue] portfolio scan %s queued behind %s scan #%s", scan_id, busy["scan_type"], busy["id"])
            return {"scan_id": scan_id, "status": "queued", "new": True, "queued_behind": busy}

        scan_id = db.create_portfolio_scan(today)
    scan_queue.spawn_worker(_run_scan_thread, scan_id, today, aggressiveness, bias)
    return {"scan_id": scan_id, "status": "running", "new": True}


@router.get("/api/portfolio-scans")
def list_scans(
    limit: int = 50,
    status: list[str] | None = Query(default=None),
) -> dict[str, Any]:
    return {"scans": db.list_portfolio_scans(limit=limit, statuses=status)}


@router.get("/api/portfolio-scans/{scan_id}")
def get_scan(scan_id: int) -> dict[str, Any]:
    scan = db.get_portfolio_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="not found")
    return scan


@router.delete("/api/portfolio-scans/{scan_id}")
def delete_scan(scan_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM portfolio_scans WHERE id = ?", (scan_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="not found")
    return {"status": "deleted", "id": scan_id}


@router.delete("/api/portfolio-scans")
def delete_all_scans() -> dict[str, Any]:
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM portfolio_scans")
    return {"status": "deleted", "count": cur.rowcount}


# ---------- background worker ----------

def _run_scan_thread(scan_id: int, trade_date: str, aggressiveness: int = 5, bias: str = "neutral") -> None:
    """Synchronous worker, run in a daemon thread started by scan_queue.spawn_worker
    (both by start_scan directly and by the queue's _dequeue_next_scan)."""
    scan_queue.refresh_creds_from_db()
    try:
        _run_scan(scan_id, trade_date, aggressiveness, bias)
    except Exception as exc:
        log.exception("Scan %s crashed", scan_id)
        db.fail_portfolio_scan(scan_id, str(exc))
        alerts.notify_run_failed(
            kind="Portfolio scan", run_id=scan_id, label=trade_date, error=str(exc)
        )
    finally:
        scan_queue._dequeue_next_scan()


def _mcp_positions() -> list[dict[str, Any]]:
    """Real holdings across all enabled brokerages, aggregated by symbol.

    Returns [{symbol, quantity, market_value, asset_type}], or [] if no
    brokerage is reachable / authed.
    """
    agg: dict[str, dict[str, Any]] = {}
    for acct in brokerages.fetch_all_accounts():
        for pos in acct["positions"]:
            e = agg.setdefault(pos["symbol"], {
                "symbol": pos["symbol"], "quantity": 0.0, "market_value": 0.0,
                "asset_type": pos["asset_type"],
            })
            e["quantity"] += pos["shares"]
            e["market_value"] += pos["market_value"]
    return list(agg.values())


def _analyze_one_holding(scan_id, pos, trade_date, config, selected_analysts, language) -> dict[str, Any]:
    """Run the Switchboard analysis for a single holding and record its result.

    All DB calls here are safe from worker threads: web/db.py::connect() opens a
    fresh sqlite3.connect(..., check_same_thread=False) per call in WAL +
    autocommit (isolation_level=None) mode — no lock is needed around these DB
    writes, and one must not be added.
    """
    analysis_id: int | None = None
    try:
        ticker = pos["symbol"]

        # Keep this import local. The two existing portfolio-progress tests
        # monkeypatch "tradingagents.orchestrator.SwitchboardOrchestrator" and
        # depend on call-time resolution; hoisting it to module level would break
        # those tests and cause real runs to hit the live LLM.
        from tradingagents.orchestrator import SwitchboardOrchestrator

        analysis_id = db.create_analysis({
            "ticker": ticker,
            "trade_date": trade_date,
            "provider": config["llm_provider"],
            "deep_model": config["deep_think_llm"],
            "quick_model": config["quick_think_llm"],
            "analysts": selected_analysts,
            "research_depth": config["max_debate_rounds"],
            "language": language,
        })
        orch = SwitchboardOrchestrator(config=config, selected_analysts=selected_analysts)
        final_state, signal = orch.run(ticker, trade_date)
        signal = (signal or "").upper()
        db.complete_analysis(analysis_id, final_state, signal)
        # Log the decision for deferred outcome grading (nightly sweep in
        # web/scheduler.py resolves it once the holding window matures).
        # Best-effort, mirrors web/runner.py — never block the scan on it.
        try:
            orch.memory_log.store_decision(
                ticker=ticker,
                trade_date=trade_date,
                final_trade_decision=final_state.get("final_trade_decision", ""),
            )
        except Exception:
            log.exception("[scan %s] memory-log store failed for %s", scan_id, ticker)
        db.add_scan_ticker(scan_id, ticker, analysis_id, pos["quantity"], pos["market_value"], signal)
        return {
            "ticker": ticker,
            "signal": signal,
            "quantity": pos["quantity"],
            "market_value": pos["market_value"],
            "trader_plan": final_state.get("trader_investment_plan", ""),
            "final_decision": final_state.get("final_trade_decision", ""),
        }
    except Exception as exc:
        ticker = pos.get("symbol", "?")
        log.exception("[scan %s] failed for %s", scan_id, ticker)
        if analysis_id is not None:
            db.fail_analysis(analysis_id, str(exc))
        db.add_scan_ticker(
            scan_id, ticker, analysis_id,
            pos.get("quantity", 0.0), pos.get("market_value", 0.0),
            None, error=str(exc),
        )
        return {
            "ticker": ticker,
            "signal": "",
            "quantity": pos.get("quantity", 0.0),
            "market_value": pos.get("market_value", 0.0),
            "trader_plan": "",
            "final_decision": f"(failed: {exc})",
        }


def _run_scan(scan_id: int, trade_date: str, aggressiveness: int = 5, bias: str = "neutral") -> None:
    """Portfolio scan worker: holdings -> bounded per-ticker graph runs -> aggregator.

    Holdings are analyzed concurrently across a ThreadPoolExecutor sized to
    the shared OLLAMA_MAX_CONCURRENCY budget, gated so ad-hoc single-ticker
    analyses keep priority. The original holdings order is preserved in the
    payload via index-based result assembly, regardless of completion order.

    A per-ticker failure is recorded (fail_analysis + an error row in the
    payload) and the scan continues; only a failure outside the loop fails
    the whole scan.

    An initial progress update (scanned_count=0, current_ticker=<first ticker>)
    is written before the pool starts so the frontend shows a non-empty
    "0/N analyzed, <first ticker>" banner immediately. scanned_count is then
    updated after each holding completes and reaches the full scan_total on
    the final update; current_ticker is the most recently completed holding.
    """
    log.info("[scan %s] starting for %s", scan_id, trade_date)

    # Step 1: fetch positions via the Schwab MCP server
    pos_dicts = _mcp_positions()
    if not pos_dicts:
        raise RuntimeError("Schwab MCP returned no positions — re-authorize at https://schwab.txferguson.net/auth")
    log.info("[scan %s] %d positions from Schwab MCP", scan_id, len(pos_dicts))

    # Options are excluded from AI analysis — they still display on the
    # holdings cards (with expiration), but the agents only scan equities.
    skipped = [p for p in pos_dicts if p["asset_type"] == "OPTION"]
    pos_dicts = [p for p in pos_dicts if p["asset_type"] != "OPTION"]
    if skipped:
        log.info("[scan %s] skipping %d option position(s): %s",
                 scan_id, len(skipped), [p["symbol"] for p in skipped])
    if not pos_dicts:
        raise RuntimeError("Only option positions held — nothing to scan (options are excluded from AI analysis).")

    # Record the total number of tickers to be scanned so the frontend can show a progress bar.
    db.update_portfolio_scan(scan_id, scan_total=len(pos_dicts))

    # Show the first ticker in the banner before any work completes, so a long
    # first batch doesn't look like a stuck/wedged scan.
    db.update_portfolio_scan(scan_id, scanned_count=0, current_ticker=pos_dicts[0]["symbol"])

    # Step 2: load user preferences for LLM / analyst config. Aggressiveness
    # (from the Run Scan form) drives debate depth; bias flows to each ticker's
    # orchestrator just like the Run Analysis tab.
    prefs = db.get_preferences() or {}
    config = build_config({**prefs, "aggressiveness": aggressiveness, "bias": bias})
    selected_analysts = prefs.get("analysts") or ["market", "social", "news", "fundamentals"]

    # Step 3: analyze each position in parallel, bounded by the shared LLM budget.
    budget = _total_budget()
    results: list[dict[str, Any] | None] = [None] * len(pos_dicts)
    completed = 0
    with _GateMonitor(DynamicGate(budget)) as gate:
        def _run_one(index, pos):
            with gate:
                return index, _analyze_one_holding(
                    scan_id, pos, trade_date, config, selected_analysts,
                    prefs.get("language", "English"),
                )

        with ThreadPoolExecutor(max_workers=budget) as pool:
            futures = {pool.submit(_run_one, i, p): i for i, p in enumerate(pos_dicts)}
            for fut in as_completed(futures):
                futures.pop(fut)  # drop as consumed once handled — same memory reason as run_deep_dives
                index, entry = fut.result()
                results[index] = entry
                completed += 1
                db.update_portfolio_scan(scan_id, scanned_count=completed, current_ticker=entry["ticker"])
                log.info("[scan %s] %d/%d: %s", scan_id, completed, len(pos_dicts), entry["ticker"])

    per_ticker_payload = [e for e in results if e is not None]

    # Aggregate signal counts on the main thread only.
    counts = {sig: 0 for sig in SIGNALS}
    for entry in per_ticker_payload:
        if entry["signal"] in counts:
            counts[entry["signal"]] += 1

    # Step 4: aggregator pass
    log.info("[scan %s] running aggregator over %d tickers", scan_id, len(per_ticker_payload))
    aggregator_md = aggregator.run(per_ticker_payload, trade_date, config)

    # Step 5: persist final scan row
    db.complete_portfolio_scan(
        scan_id=scan_id,
        aggregator_report=aggregator_md,
        signal_counts=counts,
        num_tickers=len(per_ticker_payload),
        full_payload={"per_ticker": per_ticker_payload, "config": config},
    )
    log.info("[scan %s] done — %s", scan_id, counts)


# ---------- Live per-account holdings ----------

def _accounts_split() -> list[dict[str, Any]]:
    """Per-account live holdings for the live-holdings UI panel.

    Returns [all_entry, ...per_account] from all enabled brokerage providers
    (see web.brokerages). Each entry has: id, brokerage, label, positions,
    total_value, cash, cost_basis, gain_dollars, gain_percent. Each position
    is the normalized brokerages shape (incl. option fields), optionally with
    signal/analysis_id merged from the latest completed portfolio scan.
    """
    per_account = brokerages.fetch_all_accounts()
    if not per_account:
        return []

    # Build "All Accounts" aggregate across every brokerage, keyed by symbol.
    all_syms: dict[str, dict[str, Any]] = {}
    for acct in per_account:
        for pos in acct["positions"]:
            ae = all_syms.setdefault(pos["symbol"], {
                "symbol": pos["symbol"],
                "display_symbol": pos["display_symbol"],
                "shares": 0.0, "market_value": 0.0, "_cost": 0.0,
                "asset_type": pos["asset_type"],
                "multiplier": pos["multiplier"],
                "expiration_date": pos["expiration_date"],
                "strike": pos["strike"],
                "put_call": pos["put_call"],
                "underlying": pos["underlying"],
            })
            ae["shares"] += pos["shares"]
            ae["market_value"] += pos["market_value"]
            ae["_cost"] += pos["cost_basis"]

    all_positions: list[dict[str, Any]] = []
    all_cost = 0.0
    all_mv = 0.0
    for ae in all_syms.values():
        sh = ae["shares"]
        mult = ae["multiplier"]
        mv = ae["market_value"]
        cost = ae.pop("_cost")
        gain = mv - cost
        gain_pct = (gain / cost * 100) if cost else 0.0
        ae.update({
            "shares": round(sh, 4),
            "average_price": round(cost / (sh * mult), 4) if sh else 0.0,
            "current_price": round(mv / (sh * mult), 4) if sh else 0.0,
            "market_value": round(mv, 2),
            "cost_basis": round(cost, 2),
            "gain_dollars": round(gain, 2),
            "gain_percent": round(gain_pct, 4),
        })
        all_positions.append(ae)
        all_cost += cost
        all_mv += mv
    all_positions.sort(key=lambda x: -x["market_value"])
    all_gain = all_mv - all_cost
    all_entry: dict[str, Any] = {
        "id": "all",
        "label": "All Accounts",
        "positions": all_positions,
        "total_value": round(sum(a["total_value"] for a in per_account), 2),
        "cash": round(sum(a["cash"] for a in per_account), 2),
        "cost_basis": round(all_cost, 2),
        "gain_dollars": round(all_gain, 2),
        "gain_percent": round((all_gain / all_cost * 100) if all_cost else 0.0, 4),
    }

    # Merge latest scan signals onto matching positions (best-effort)
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM portfolio_scans WHERE status='completed' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row:
            latest = db.get_portfolio_scan(row["id"])
            ticker_map = {t["ticker"]: t for t in (latest.get("tickers") or [])}
            for acct in [all_entry] + per_account:
                for pos in acct["positions"]:
                    t = ticker_map.get(pos["symbol"])
                    if t:
                        pos["signal"] = t.get("signal")
                        pos["analysis_id"] = t.get("analysis_id")
    except Exception:
        pass

    return [all_entry] + per_account


@router.get("/api/accounts")
def accounts() -> dict[str, Any]:
    """Live per-account holdings with cost basis, gain/loss, and optional AI scan signals."""
    if not brokerages.any_enabled():
        return {"enabled": False, "connected": False}
    try:
        data = _accounts_split()
    except Exception:
        log.exception("[accounts] brokerage read failed")
        data = None
    if not data:
        return {"enabled": True, "connected": False}
    return {"enabled": True, "connected": True, "accounts": data}


# ---------- Scan-queue registration ----------
# Registered at import time (portfolio_main.py imports this module while
# building the app), so a runner is in place before anything can dequeue.
# Declared at the bottom so _run_scan_thread already exists; the registry
# resolves it by NAME at dispatch time regardless (see web/scan_queue.py).
scan_queue.register_runner("portfolio", sys.modules[__name__], "_run_scan_thread")
