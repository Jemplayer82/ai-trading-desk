"""APScheduler daemon — cron triggers for scans, newsletter, and health checks.

Runs inside the tradingagents-scheduler container (`python -m web.scheduler`).
It does no analysis itself: scan jobs are fire-and-forget HTTP POSTs to the
portfolio/api containers over the Docker network, authenticated with the
X-Internal-Token header (INTERNAL_API_TOKEN, verified server-side in
web/auth_app.py with hmac.compare_digest). The newsletter is the exception —
it sends from this process directly, which is why _apply_db_config() must
pull SMTP/notifier settings from the shared DB first.

Schedule (all times SCHEDULER_TIMEZONE, default America/New_York):
    settings-driven      portfolio scan      POST /api/portfolio-scan (default 22:00 Mon-Fri, kept current by schedule_reconciler)
    23:30 Mon-Fri        outcome sweep       (in-process — resolves pending memory-log entries)
    05:00 daily          morning newsletter  (in-process)
    hourly               Schwab token health GET /api/auth/schwab/status
    per-account          S&P 500 allocation  POST /api/spy-scan (Mon-Fri at each account's time, default 09:00; fills after 09:35)
    Mon-Fri 09:00-16:00  SPY price refresh   POST /api/spy-scans/latest/refresh-prices
    per-account          options allocation  POST /api/options-scan (Mon-Fri at each account's time, default 09:00; fills after 09:35)
    Mon-Fri 10:00-16:00  options mark        POST /api/options-positions/refresh (+16:45 close pass)
    Mon-Fri 20:00        options settlement  POST /api/options-positions/settle
    settings-driven      research_scan       POST /api/research-scan (default 00:00 Mon-Fri, NYSE holidays skipped)
    every 15 min         research_retry      re-POSTs research if missing/failed, only before 05:30 ET
    every 60s            schedule_reconciler  syncs per-account scan jobs + nightly scan/research times

Each job also has a --run-*-now CLI flag for one-shot manual runs (handy for
testing inside the container without waiting for cron).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Any

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import account_policy, alerts, db, features, market_calendar
from . import credentials as creds
from ._logging import configure_logging

configure_logging()
log = logging.getLogger("scheduler")

API_URL = (os.environ.get("TRADINGAGENTS_API_URL") or "http://tradingagents-api:8000").rstrip("/")
PORTFOLIO_URL = (os.environ.get("TRADINGAGENTS_PORTFOLIO_URL") or "http://tradingagents-portfolio:8000").rstrip("/")
TIMEZONE = os.environ.get("SCHEDULER_TIMEZONE", "America/New_York")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://trading.txferguson.net").rstrip("/")

# Stuck-run reaper thresholds (minutes). A run quiet past its limit is treated as a
# crashed worker. The scan heartbeat (every 2 min by default) keeps live scans fresh,
# so 120 only trips on a dead worker.
STUCK_SCAN_STALL_MIN = int(os.environ.get("STUCK_SCAN_STALL_MIN", "120"))
STUCK_ANALYSIS_MIN = int(os.environ.get("STUCK_ANALYSIS_MIN", "90"))

_ALERT_DETAIL_MAX = 400  # cap response bodies/tracebacks embedded in scheduler alerts

# Re-alert cadence while Schwab stays de-authorized (hours). The first alert
# fires immediately on the connected->not-connected transition; after that,
# re-nag periodically instead of hourly (spammy) or once (a missed alert
# means days of silent nightly-scan failures, as happened 2026-07-25).
SCHWAB_REALERT_HOURS = int(os.environ.get("SCHWAB_REALERT_HOURS", "6"))
# Last known Schwab connection state (None=unknown at startup, True/False
# after the first check). Module-level and in-memory: resets on container
# restart (rare — restart: unless-stopped), acceptable since a restart also
# re-checks within the hour and re-alerts if still down.
_schwab_last_connected: bool | None = None
_schwab_down_since: datetime | None = None
_schwab_last_alert_at: datetime | None = None

NIGHTLY_SCAN_TIME_SETTING = "SCHEDULE_NIGHTLY_SCAN_TIME"
DEFAULT_NIGHTLY_SCAN_TIME = (22, 0)
RESEARCH_TIME_SETTING = "SCHEDULE_RESEARCH_TIME"
DEFAULT_RESEARCH_TIME = (0, 0)

# Shared daily research retry policy (design doc §8). The retry job re-POSTs
# /api/research-scan when today's research is missing past its grace window or
# failed, at most RESEARCH_MAX_ATTEMPTS research rows per trade date, and never
# at/after RESEARCH_RETRY_CUTOFF_ET (a run started later cannot finish before
# the 10:30 ET allocation deadline).
RESEARCH_MAX_ATTEMPTS = int(os.environ.get("RESEARCH_MAX_ATTEMPTS", "2"))
RESEARCH_RETRY_CUTOFF_ET: tuple[int, int] = (
    account_policy.parse_hhmm(os.environ.get("RESEARCH_RETRY_CUTOFF_ET", "05:30")) or (5, 30)
)
RESEARCH_MISSING_GRACE_MIN = 15
RECONCILE_JOB_ID = "schedule_reconciler"
_ACCOUNT_JOB_PREFIXES = ("spy_scan_acct_", "options_scan_acct_")


def _internal_headers() -> dict[str, str]:
    """Auth bypass header so cron calls pass the api/portfolio login gate."""
    tok = os.environ.get("INTERNAL_API_TOKEN")
    return {"X-Internal-Token": tok} if tok else {}


def _setting_time(setting: str, default: tuple[int, int]) -> tuple[int, int]:
    """Resolve an HH:MM schedule setting: DB app setting, then env, then default.

    Read at CALL time (not import time) so the reconciler picks up a change the
    user saved in the dashboard without a container restart. A malformed stored
    value logs a warning and yields the default. Never raises.
    """
    stored_value = None
    try:
        stored_value = db.get_app_setting(setting)
    except Exception:
        log.exception("[schedule] DB read of %s failed; falling back to env/default", setting)
    if stored_value is not None and stored_value.strip():
        parsed = account_policy.parse_hhmm(stored_value)
        if parsed is not None:
            return parsed
        log.warning(
            "[schedule] stored %s %r is malformed; using default %s:%02d",
            setting,
            stored_value,
            *default,
        )
        return default
    env_value = os.environ.get(setting)
    parsed = account_policy.parse_hhmm(env_value)
    if parsed is not None:
        return parsed
    return default


def nightly_scan_time() -> tuple[int, int]:
    """Fire time for job_nightly_scan: DB setting, then env, then 22:00."""
    return _setting_time(NIGHTLY_SCAN_TIME_SETTING, DEFAULT_NIGHTLY_SCAN_TIME)


def research_time() -> tuple[int, int]:
    """Fire time for job_research_scan: DB setting, then env, then 00:00."""
    return _setting_time(RESEARCH_TIME_SETTING, DEFAULT_RESEARCH_TIME)


def _apply_db_config() -> None:
    """Pull UI-saved credentials + settings (SMTP, notifier, etc.) onto env.

    The scheduler sends the newsletter and notifications directly, so it
    needs SMTP_* / FRED_NOTIFY_URL from the shared DB — it has no other
    way to see values the user saved in the dashboard.
    """
    try:
        db.init_db()
        creds.apply_to_env()
        creds.apply_settings_to_env()
    except Exception:
        log.exception("[scheduler] applying DB config failed")


def job_nightly_scan() -> None:
    """Kick off the portfolio container's nightly scan.

    The endpoint queues a background task and returns immediately, so the
    60s timeout covers request startup only — never the multi-hour scan.
    The endpoint is idempotent per trade date, so a retry can't double-scan.
    """
    log.info("[nightly_scan] firing portfolio scan at %s", PORTFOLIO_URL)
    try:
        r = httpx.post(f"{PORTFOLIO_URL}/api/portfolio-scan", timeout=60, headers=_internal_headers())
        log.info("[nightly_scan] response %s: %s", r.status_code, r.text[:400])
        if r.status_code >= 400:
            # e.g. 400 "Schwab MCP not connected" — the cron fired but the scan
            # was rejected before a row was even created. Previously only
            # logged, so a dead Schwab token silently killed every nightly
            # scan with no user-visible signal beyond a log line no one reads.
            alerts.notify(
                f"⚠️ Nightly portfolio scan rejected ({r.status_code}).",
                r.text[:_ALERT_DETAIL_MAX],
                link=DASHBOARD_URL,
            )
    except Exception as exc:
        log.exception("[nightly_scan] failed: %s", exc)
        alerts.notify(
            "⚠️ Nightly portfolio scan failed to start.",
            str(exc),
            link=DASHBOARD_URL,
        )


def _latest_scan_for_today() -> dict[str, Any] | None:
    """Latest portfolio scan, with a warning (not a skip) when it isn't from today."""
    today_iso = datetime.utcnow().date().isoformat()
    try:
        scan = db.latest_portfolio_scan()
    except Exception:
        log.exception("[newsletter] db read failed")
        return None
    if not scan:
        return None
    trade_date = (scan.get("trade_date") or "")[:10]
    if trade_date != today_iso:
        log.warning("[newsletter] latest scan trade_date=%s != today=%s — sending anyway", trade_date, today_iso)
    return scan


def job_morning_newsletter() -> None:
    """Email the overnight scan as the morning newsletter.

    Deliberately permissive: a stale or still-running scan logs a warning but
    sends anyway — a slightly old briefing beats no briefing.
    """
    log.info("[newsletter] morning newsletter job firing")
    from . import newsletter  # lazy: T2-only module, this job only registers at T2+
    _apply_db_config()  # ensure latest SMTP_* / notifier settings from the UI
    scan = _latest_scan_for_today()
    if not scan:
        log.warning("[newsletter] no scan to send")
        alerts.notify("Morning newsletter skipped: no overnight scan found.", link=DASHBOARD_URL)
        return
    if scan.get("status") != "completed":
        log.warning("[newsletter] latest scan status=%s — sending anyway", scan.get("status"))
    try:
        msg_id = newsletter.send(scan)
        if msg_id:
            db.mark_newsletter_sent(int(scan["id"]), msg_id)
            log.info("[newsletter] sent scan id=%s msg_id=%s", scan["id"], msg_id)
        else:
            log.warning("[newsletter] send returned None")
    except Exception:
        log.exception("[newsletter] crash during send")


def job_token_health() -> None:
    """Hourly Schwab MCP session check; notifies the user when re-auth is needed.

    Alerts on the connected->not-connected transition (dual-channel, so a
    dead Fred webhook can't swallow it — see 2026-07-25: the token died,
    token_health logged a WARNING every hour for 13+ hours, and the ONLY
    notification channel (FRED_NOTIFY_URL) was connection-refused the whole
    time, so nothing ever reached the user). Re-alerts every
    SCHWAB_REALERT_HOURS while still down, in case the first alert is missed,
    instead of hourly (spammy) or once (silent for days if missed).
    """
    global _schwab_last_connected, _schwab_down_since, _schwab_last_alert_at
    try:
        r = httpx.get(f"{API_URL}/api/auth/schwab/status", timeout=15, headers=_internal_headers())
        if r.status_code != 200:
            log.warning("[token_health] status %s: %s", r.status_code, r.text[:200])
            return
        data = r.json()
    except Exception:
        log.exception("[token_health] failed to reach api")
        return
    # Master switch off → user has no Schwab; nothing to nag about.
    if not data.get("enabled", True):
        log.info("[token_health] Schwab disabled — skipping")
        return

    now = datetime.utcnow()
    if not data.get("connected"):
        log.warning("[token_health] Schwab MCP not authorized")
        became_down_now = _schwab_last_connected is not False  # True or None (startup)
        due_for_realert = (
            _schwab_last_alert_at is not None
            and (now - _schwab_last_alert_at) >= timedelta(hours=SCHWAB_REALERT_HOURS)
        )
        if became_down_now or due_for_realert:
            if _schwab_down_since is None:
                _schwab_down_since = now
            down_for = now - _schwab_down_since
            hours = int(down_for.total_seconds() // 3600)
            alerts.notify(
                "⚠️ Schwab MCP session is not authorized — nightly/portfolio scans and "
                "live quotes are down.",
                f"Down for ~{hours}h. Re-authorize at https://schwab.txferguson.net/auth",
                link="https://schwab.txferguson.net/auth",
            )
            _schwab_last_alert_at = now
        _schwab_last_connected = False
        return
    if _schwab_last_connected is False:
        log.info("[token_health] Schwab MCP reconnected after outage")
        down_for = now - _schwab_down_since if _schwab_down_since else None
        hours = int(down_for.total_seconds() // 3600) if down_for else 0
        alerts.notify(
            "✅ Schwab MCP re-authorized — scans and live quotes are back.",
            f"Was down for ~{hours}h." if hours else "",
            link=DASHBOARD_URL,
        )
    _schwab_last_connected = True
    _schwab_down_since = None
    _schwab_last_alert_at = None
    log.info("[token_health] Schwab MCP connected")


def _post_research(reason: str) -> None:
    """POST the portfolio container's shared daily research endpoint.

    The endpoint starts research in a background worker and returns at once, so
    the 60s timeout covers request startup only. It is idempotent per trade
    date for pending/running/completed rows. Never raises; failures alert.
    """
    log.info("[research_scan] firing daily research (%s) at %s", reason, PORTFOLIO_URL)
    try:
        r = httpx.post(
            f"{PORTFOLIO_URL}/api/research-scan",
            json={},
            timeout=60,
            headers=_internal_headers(),
        )
        log.info("[research_scan] (%s) response %s: %s", reason, r.status_code, r.text[:400])
        if r.status_code >= 400:
            alerts.notify(
                f"⚠️ Daily research rejected ({r.status_code}).",
                r.text[:_ALERT_DETAIL_MAX],
                link=DASHBOARD_URL,
            )
    except Exception as exc:
        log.exception("[research_scan] (%s) failed: %s", reason, exc)
        alerts.notify(
            "⚠️ Daily research failed to start.",
            str(exc),
            link=DASHBOARD_URL,
        )


def job_research_scan() -> None:
    """Start today's shared research (settings-driven time, default 00:00 ET)."""
    if not market_calendar.is_trading_day(market_calendar.today_et()):
        log.info("[research_scan] skipped: not an NYSE trading day")
        return
    _post_research("scheduled")


def job_research_retry() -> None:
    """Every 15 min: re-start today's research if it is missing or failed.

    Never raises.
    """
    try:
        now = market_calendar.now_et()
        if not market_calendar.is_trading_day(now):
            return
        # The cutoff applies to BOTH the missing and the failed branch: a
        # research run started after it cannot finish before the 10:30 ET
        # allocation deadline, so starting one would only burn LLM budget.
        if (now.hour, now.minute) >= RESEARCH_RETRY_CUTOFF_ET:
            return
        today = now.date().isoformat()
        latest = db.latest_research_scan(today)
        attempts = db.count_research_attempts(today)
        h, m = research_time()
        due = now.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(
            minutes=RESEARCH_MISSING_GRACE_MIN
        )
        if latest is None and now >= due:
            log.info("[research_retry] no research row for %s past %s; starting it", today, due)
            _post_research("missing")
        elif latest and latest["status"] == "failed" and attempts < RESEARCH_MAX_ATTEMPTS:
            # Failed rows don't satisfy the endpoint's idempotency, so this
            # creates a fresh research row; completed deep dives from the failed
            # attempt are reused via find_reusable_analysis.
            log.info("[research_retry] retrying failed research #%s", latest["id"])
            _post_research("retry")
    except Exception:
        log.exception("[research_retry] crashed")


def job_spy_scan_account(account_id: int) -> None:
    """Daily S&P 500 allocation for ONE equity paper account.

    Skips NYSE holidays; the endpoint waits for today's shared research and
    fills after the open.
    """
    if not market_calendar.is_trading_day(market_calendar.today_et()):
        log.info("[spy_scan_acct_%d] not a trading day — Daily S&P 500 allocation skipped", account_id)
        return
    log.info("[spy_scan_acct_%d] firing Daily S&P 500 allocation at %s", account_id, PORTFOLIO_URL)
    try:
        r = httpx.post(
            f"{PORTFOLIO_URL}/api/spy-scan",
            json={"account_id": account_id},
            timeout=60,
            headers=_internal_headers(),
        )
        log.info("[spy_scan_acct_%d] response %s: %s", account_id, r.status_code, r.text[:400])
        if r.status_code >= 400:
            alerts.notify(
                f"⚠️ Daily S&P 500 allocation rejected for account {account_id} ({r.status_code}).",
                r.text[:_ALERT_DETAIL_MAX],
                link=DASHBOARD_URL,
            )
    except Exception as exc:
        log.exception("[spy_scan_acct_%d] failed: %s", account_id, exc)
        alerts.notify(
            f"⚠️ Daily S&P 500 allocation failed to start for account {account_id}.",
            str(exc),
            link=DASHBOARD_URL,
        )


def job_spy_price_refresh() -> None:
    """Mark the latest SPY paper portfolio to market (also records signal flips)."""
    log.info("[spy_price_refresh] refreshing latest SPY scan prices")
    try:
        r = httpx.post(f"{PORTFOLIO_URL}/api/spy-scans/latest/refresh-prices", timeout=120, headers=_internal_headers())
        log.info("[spy_price_refresh] response %s: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.exception("[spy_price_refresh] failed: %s", exc)


def job_options_scan() -> None:
    """Kick off the Daily options allocation for every options paper account.

    Skips NYSE holidays. The endpoint is idempotent per (account, day) and
    queues background workers, so the 60s timeout covers request startup only.
    A 409 means no options accounts exist yet — informational, not a failure.
    """
    if not market_calendar.is_trading_day(market_calendar.today_et()):
        log.info("[options_scan] not a trading day — Daily options allocation skipped")
        return
    log.info("[options_scan] firing Daily options allocation at %s", PORTFOLIO_URL)
    try:
        r = httpx.post(f"{PORTFOLIO_URL}/api/options-scan", timeout=60, headers=_internal_headers())
        if r.status_code == 409:
            log.info("[options_scan] skipped: %s", r.text[:200])
        else:
            log.info("[options_scan] response %s: %s", r.status_code, r.text[:400])
    except Exception as exc:
        log.exception("[options_scan] failed: %s", exc)


def job_options_scan_account(account_id: int) -> None:
    """Daily options allocation for ONE options paper account (skips NYSE holidays)."""
    if not market_calendar.is_trading_day(market_calendar.today_et()):
        log.info("[options_scan_acct_%d] not a trading day — Daily options allocation skipped", account_id)
        return
    log.info("[options_scan_acct_%d] firing Daily options allocation at %s", account_id, PORTFOLIO_URL)
    try:
        r = httpx.post(
            f"{PORTFOLIO_URL}/api/options-scan",
            json={"account_id": account_id},
            timeout=60,
            headers=_internal_headers(),
        )
        if r.status_code == 409:
            log.info("[options_scan_acct_%d] skipped: %s", account_id, r.text[:200])
        else:
            log.info("[options_scan_acct_%d] response %s: %s", account_id, r.status_code, r.text[:400])
            if r.status_code >= 400:
                alerts.notify(
                    f"⚠️ Daily options allocation rejected for account {account_id} ({r.status_code}).",
                    r.text[:_ALERT_DETAIL_MAX],
                    link=DASHBOARD_URL,
                )
    except Exception as exc:
        log.exception("[options_scan_acct_%d] failed: %s", account_id, exc)
        alerts.notify(
            f"⚠️ Daily options allocation failed to start for account {account_id}.",
            str(exc),
            link=DASHBOARD_URL,
        )


def job_options_refresh() -> None:
    """Settle due expiries + mark all open option positions to market."""
    log.info("[options_refresh] refreshing option marks")
    try:
        r = httpx.post(f"{PORTFOLIO_URL}/api/options-positions/refresh", timeout=300,
                       headers=_internal_headers())
        log.info("[options_refresh] response %s: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.exception("[options_refresh] failed: %s", exc)


def job_options_settle() -> None:
    """Nightly expiry-settlement sweep (idempotent; daily bars final after close)."""
    log.info("[options_settle] running expiry settlement sweep")
    try:
        r = httpx.post(f"{PORTFOLIO_URL}/api/options-positions/settle", timeout=300,
                       headers=_internal_headers())
        log.info("[options_settle] response %s: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.exception("[options_settle] failed: %s", exc)


def job_options_grade() -> None:
    """Nightly options-ledger grading: backfill exit spots + batch-reflect.

    In-process like job_outcome_sweep (same shared volume/DB rationale). Runs
    AFTER the 20:00 settle so expired_* rows exist, BEFORE the 23:30 memory
    sweep. Per options account: fill missing exit underlyings (enables the
    directional-vs-decay attribution), then ONE quick-LLM reflection over
    recent closes — gated inside run_batch_reflection on enough NEW closes,
    so most nights this is a free no-op. LLM failure degrades to backfill-only
    (positions stay unreflected and retry tomorrow).
    """
    log.info("[options_grade] starting")
    _apply_db_config()
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.llm_clients import create_llm_client

        from . import db as webdb
        from . import options_learning

        config = dict(DEFAULT_CONFIG)
        if config.get("llm_provider", "openai").lower() == "openai" and not os.environ.get("OPENAI_API_KEY"):
            # Same credential fallback as job_outcome_sweep below: this
            # container carries no provider keys; route via the switchboard.
            config["llm_provider"] = "switchboard"
            # Bare family name, not a pinned ID: the switchboard hands this
            # straight to `claude --model`, which resolves it to whatever the
            # current Sonnet is. Keeps the nightly jobs off a snapshot that
            # eventually gets retired out from under them.
            #
            # Sonnet rather than Haiku despite this being the "quick" role.
            # These jobs bind tools and run unattended overnight, so a silently
            # empty result is expensive to notice; Sonnet has the strongest
            # tool-call record on the bus. This is a fallback for when no
            # provider key is configured — an operator's explicit choice in the
            # Run Analysis form always wins over it (web/runner.py
            # _resolve_model gives explicit params top precedence).
            config["quick_think_llm"] = "sonnet"
        llm = None
        try:
            llm = create_llm_client(
                provider=config["llm_provider"],
                model=config["quick_think_llm"],
                base_url=config.get("backend_url"),
            ).get_llm()
        except Exception:
            log.exception("[options_grade] LLM client unavailable — backfill only")

        accounts = webdb.list_paper_accounts(kind="options")
        for acct in accounts:
            aid = int(acct["id"])
            try:
                filled = options_learning.backfill_exit_underlyings(aid)
                reflected = options_learning.run_batch_reflection(aid, llm, config)
                log.info("[options_grade] account %s: backfilled=%d reflected=%s",
                         aid, filled, reflected)
            except Exception:
                log.exception("[options_grade] account %s failed", aid)
        if not accounts:
            log.info("[options_grade] no options accounts — nothing to grade")
    except Exception:
        log.exception("[options_grade] crashed")


_ACCOUNT_JOBS = {
    # kind -> (job-id template, job function, FIXED day-of-week)
    "equity":  ("spy_scan_acct_{}",     job_spy_scan_account,     "mon-fri"),
    "options": ("options_scan_acct_{}", job_options_scan_account, "mon-fri"),
}


# Global Mon-Fri cron jobs whose HH:MM comes from an app setting:
# (job id, time-resolver function NAME, job function NAME, required feature).
# Names, not objects, so the reconciler resolves them at call time and tests
# that monkeypatch the module attributes keep working.
_SETTING_JOBS = (
    ("nightly_scan", "nightly_scan_time", "job_nightly_scan", "schwab"),
    ("research_scan", "research_time", "job_research_scan", "sp500"),
)


def job_reconcile_schedules(sched) -> None:
    """Sync APScheduler with per-account schedule_time + the setting-driven times.

    The setting-driven jobs are listed in _SETTING_JOBS (nightly scan, daily
    research).

    Runs every 60s so a schedule edited in the dashboard takes effect without a
    container restart. Must never raise: a malformed stored time is logged and
    that account skipped, and a DB failure aborts this pass only.
    """
    try:
        try:
            accounts = db.list_paper_accounts()
        except Exception:
            log.exception("[schedule] failed to list paper accounts; aborting reconcile pass")
            return

        wanted: dict[str, tuple] = {}
        for acct in accounts:
            aid = acct.get("id")
            kind = acct.get("kind")
            schedule_time = acct.get("schedule_time")
            if kind not in _ACCOUNT_JOBS:
                continue
            if not schedule_time:
                continue
            if kind == "options" and not features.enabled("options"):
                continue
            if kind == "equity" and not features.enabled("sp500"):
                continue
            parsed = account_policy.parse_hhmm(schedule_time)
            if parsed is None:
                if str(schedule_time).strip():
                    log.warning(
                        "[schedule] account %s has malformed schedule_time %r — skipped",
                        aid,
                        schedule_time,
                    )
                continue
            hour, minute = parsed
            template, func, dow = _ACCOUNT_JOBS[kind]
            job_id = template.format(aid)
            wanted[job_id] = (func, hour, minute, dow, aid)

        for job_id, (func, hour, minute, dow, aid) in wanted.items():
            try:
                trigger = CronTrigger(day_of_week=dow, hour=hour, minute=minute, timezone=TIMEZONE)
                existing = sched.get_job(job_id)
                if existing is None or str(existing.trigger) != str(trigger):
                    if existing is not None:
                        # add_job(replace_existing=True) only replaces jobs already
                        # in a jobstore. On a scheduler that hasn't started yet,
                        # add_job() just appends to an in-memory pending-jobs list
                        # without deduping by id, so replace_existing silently
                        # no-ops there and get_job() keeps returning the stale
                        # trigger. Removing first makes the update unconditional.
                        try:
                            sched.remove_job(job_id)
                        except Exception:
                            log.exception("[schedule] failed to remove stale trigger for %s before re-add", job_id)
                    sched.add_job(func, trigger, args=[aid], id=job_id, replace_existing=True)
                    log.info(
                        "[schedule] added/updated job %s (%s %02d:%02d %s)",
                        job_id,
                        dow,
                        hour,
                        minute,
                        TIMEZONE,
                    )
            except Exception:
                log.exception("[schedule] failed to add job %s", job_id)

        try:
            for existing in sched.get_jobs():
                job_id = existing.id
                if job_id.startswith(_ACCOUNT_JOB_PREFIXES) and job_id not in wanted:
                    try:
                        sched.remove_job(job_id)
                        log.info("[schedule] removed stale job %s", job_id)
                    except Exception:
                        log.exception("[schedule] failed to remove job %s", job_id)
        except Exception:
            log.exception("[schedule] failed to enumerate jobs during stale removal")

        for job_id, time_fn_name, func_name, feature in _SETTING_JOBS:
            if not features.enabled(feature):
                continue
            try:
                existing = sched.get_job(job_id)
                if existing is None:
                    continue
                # Resolved at call time so monkeypatched module attributes win.
                time_fn = globals()[time_fn_name]
                func = globals()[func_name]
                hour, minute = time_fn()
                trigger = CronTrigger(
                    day_of_week="mon-fri", hour=hour, minute=minute, timezone=TIMEZONE
                )
                if str(trigger) != str(existing.trigger):
                    # Same pending-jobs caveat as the per-account loop above:
                    # remove before re-add so the update takes effect even on
                    # a not-yet-started scheduler.
                    try:
                        sched.remove_job(job_id)
                    except Exception:
                        log.exception("[schedule] failed to remove stale %s trigger before re-add", job_id)
                    sched.add_job(func, trigger, id=job_id, replace_existing=True)
                    log.info(
                        "[schedule] updated %s to %02d:%02d %s",
                        job_id,
                        hour,
                        minute,
                        TIMEZONE,
                    )
            except Exception:
                log.exception("[schedule] failed to reconcile %s", job_id)
    except Exception:
        log.exception("[schedule] reconcile pass crashed")


# Tickers that are always worth an LLM reflection regardless of holdings.
# Mirrors the ALWAYS_DEEP tuple in web/research_engine.py byte-for-byte rather
# than importing it: research_engine is tier-3, while this scheduler ships at
# every tier and may import only tier-agnostic modules. (The leak canary in
# scripts/make_tier.py is AST-based and flags only unconditional top-level
# imports, but a top-level import here would be exactly that.) Keep in sync.
_ALWAYS_RELEVANT = ("SPY",)

# How far back an analysis counts as "someone is watching this ticker".
# MUST comfortably exceed reflection_holding_days (5): an entry is analyzed
# ON its trade date and only matures ~a week later, so a shorter window
# would gate out the very entries that analysis created.
SWEEP_RELEVANCE_DAYS = 14


def _sweep_relevant_tickers() -> set[str] | None:
    """Uppercased tickers whose resolved outcomes anyone will actually read."""
    try:
        out: set[str] = set()
        attempted = 0
        failed = 0

        # 1. Equity paper holdings.
        if features.enabled("sp500") or features.enabled("options"):
            attempted += 1
            try:
                for acct in db.list_paper_accounts(kind="equity"):
                    latest = db.get_latest_completed_spy_scan(
                        paper_account_id=acct["id"],
                        kind="equity",
                    )
                    if not latest:
                        continue
                    portfolio = latest.get("portfolio_json")
                    if isinstance(portfolio, str):
                        try:
                            portfolio = json.loads(portfolio)
                        except (TypeError, ValueError):
                            portfolio = None
                    if not isinstance(portfolio, list):
                        continue
                    for pos in portfolio:
                        if not isinstance(pos, dict):
                            continue
                        if pos.get("action") != "EXITED":
                            ticker = pos.get("ticker")
                            if ticker:
                                out.add(str(ticker).upper())
            except Exception:
                failed += 1
                log.warning(
                    "[outcome_sweep] equity holdings relevance source failed",
                    exc_info=True,
                )

        # 2. Open options underlyings.
        if features.enabled("options"):
            attempted += 1
            try:
                for pos in db.list_options_positions(status="open"):
                    underlying = pos.get("underlying")
                    if underlying:
                        out.add(str(underlying).upper())
            except Exception:
                failed += 1
                log.warning(
                    "[outcome_sweep] options underlyings relevance source failed",
                    exc_info=True,
                )

        # 3. Recently analyzed tickers.
        attempted += 1
        try:
            out.update(db.recent_analysis_tickers(_cutoff_iso(SWEEP_RELEVANCE_DAYS * 24 * 60)))
        except Exception:
            failed += 1
            log.warning(
                "[outcome_sweep] recent analysis relevance source failed",
                exc_info=True,
            )

        # If any DB-backed source we attempted errored, the gate has an
        # incomplete view of the world. Fail open rather than over-suppress.
        if failed:
            log.warning(
                "[outcome_sweep] %d of %d relevance sources failed — disabling gate",
                failed,
                attempted,
            )
            return None

        # Empty DB-backed set before applying the always-relevant floor.
        # If there is no floor at all, disable the gate; otherwise keep it
        # armed with the floor tickers.
        if not out:
            if not _ALWAYS_RELEVANT:
                log.warning("[outcome_sweep] relevance set assembled empty — disabling gate")
                return None

        # 4. Always relevant.
        out.update(_ALWAYS_RELEVANT)
        return out
    except Exception:
        log.exception("[outcome_sweep] relevance computation crashed")
        return None


def job_outcome_sweep() -> None:
    """Resolve matured pending memory-log decisions.

    In-process like the newsletter: this container mounts the same
    ``tradingagents_data`` volume that holds the memory log, so no HTTP hop is
    needed. Without this sweep, entries only resolve when the same ticker is
    re-run through the CLI graph — in the deployed topology that almost never
    happens, so decisions sit pending forever and the agents learn from a
    sparse, survivor-biased subset of their own track record.

    LLM cost is now bounded by BOTH ``sweep_max_reflections_per_run`` AND a
    relevance gate computed here in the scheduler (only this container has DB
    access; outcome_resolution.py is tier-agnostic). Tickers not held in any
    paper account, not in the always-relevant set, and not analyzed within
    ``SWEEP_RELEVANCE_DAYS`` days receive a free canned NOT-RELEVANT reflection
    instead of consuming budget. The gate disables itself (falls back to
    ungated behavior) on any error or an empty result, so a DB hiccup cannot
    silently stop the agents from learning.

    Runs after US close so day-0 bars are final; the maturity guard inside
    ``resolve_all_pending`` ensures each entry waits for its full holding
    window. Canned NOISE/CENSORED reflections remain free.
    """
    log.info("[outcome_sweep] starting")
    _apply_db_config()  # LLM provider keys may live in the shared DB
    try:
        from tradingagents.agents.utils.memory import TradingMemoryLog
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.outcome_resolution import resolve_all_pending
        from tradingagents.graph.reflection import Reflector
        from tradingagents.llm_clients import create_llm_client

        config = dict(DEFAULT_CONFIG)
        if config.get("llm_provider", "openai").lower() == "openai" and not os.environ.get("OPENAI_API_KEY"):
            # DEFAULT_CONFIG hardcodes provider="openai", which needs
            # OPENAI_API_KEY — not configured on this deployment (the
            # scheduler container carries no LLM credentials of its own).
            # Every other scan path here (portfolio/SPY/chat) resolves
            # through the switchboard bus instead of a raw provider key;
            # do the same rather than crash-degrade to canned-only
            # resolution every night (was: "Missing OPENAI_API_KEY" in
            # logs since at least 2026-07-23).
            config["llm_provider"] = "switchboard"
            # Bare family name, not a pinned ID: the switchboard hands this
            # straight to `claude --model`, which resolves it to whatever the
            # current Sonnet is. Keeps the nightly jobs off a snapshot that
            # eventually gets retired out from under them.
            #
            # Sonnet rather than Haiku despite this being the "quick" role.
            # These jobs bind tools and run unattended overnight, so a silently
            # empty result is expensive to notice; Sonnet has the strongest
            # tool-call record on the bus. This is a fallback for when no
            # provider key is configured — an operator's explicit choice in the
            # Run Analysis form always wins over it (web/runner.py
            # _resolve_model gives explicit params top precedence).
            config["quick_think_llm"] = "sonnet"
        memory_log = TradingMemoryLog(config)
        # Missing/misconfigured LLM keys degrade the sweep (NOISE/CENSORED
        # entries still resolve; LLM-graded ones defer) instead of killing it.
        reflector = None
        try:
            llm = create_llm_client(
                provider=config["llm_provider"],
                model=config["quick_think_llm"],
                base_url=config.get("backend_url"),
            ).get_llm()
            reflector = Reflector(llm)
        except Exception:
            log.exception("[outcome_sweep] LLM client unavailable — resolving canned entries only")
        relevant = _sweep_relevant_tickers()
        log.info("[outcome_sweep] relevance gate: %s", "off" if relevant is None else f"{len(relevant)} tickers")
        summary = resolve_all_pending(
            memory_log,
            reflector,
            config,
            max_reflections=config.get("sweep_max_reflections_per_run", 50),
            relevant_tickers=relevant,
        )
        log.info("[outcome_sweep] done: %s", summary)
        try:
            # Backlog visibility: deep dives now feed ~50 pendings/day into this
            # queue; a growing number here means the reflection budget is
            # starving (relief valve: TRADINGAGENTS_SWEEP_MAX_REFLECTIONS_PER_RUN).
            log.info("[outcome_sweep] pending backlog now: %d",
                     len(memory_log.get_pending_entries()))
        except Exception:
            pass
        if summary["errors"]:
            log.warning("[outcome_sweep] %d entries errored — see log above", summary["errors"])
    except Exception:
        log.exception("[outcome_sweep] crashed")


def _cutoff_iso(minutes: int) -> str:
    """UTC cutoff `minutes` in the past, in the same ISO+Z format rows are stored."""
    return (datetime.utcnow() - timedelta(minutes=minutes)).isoformat(timespec="seconds") + "Z"


def job_reap_stuck_runs() -> None:
    """Fail + alert runs whose worker died silently (status stuck in 'running').

    A crash/OOM never ran the in-process `except` that would mark a run failed, so
    these rows would otherwise sit 'running' forever — invisible to the user and
    showing as phantom progress on the Analysis tab. We detect them by a stalled
    liveness stamp (scans) or age (analyses), close them out, and notify once each.
    """
    scan_cutoff = _cutoff_iso(STUCK_SCAN_STALL_MIN)
    analysis_cutoff = _cutoff_iso(STUCK_ANALYSIS_MIN)
    scan_err = f"abandoned — no progress for {STUCK_SCAN_STALL_MIN} min, worker likely crashed"
    analysis_err = f"abandoned — still running after {STUCK_ANALYSIS_MIN} min, worker likely crashed"
    try:
        if features.enabled("schwab"):
            for scan in db.find_stuck_portfolio_scans(scan_cutoff):
                db.fail_portfolio_scan(scan["id"], scan_err)
                log.warning("[reaper] failed stuck portfolio scan %s", scan["id"])
                alerts.notify_run_failed(kind="Portfolio scan", run_id=scan["id"],
                                         label=scan.get("trade_date") or "", error=scan_err)
        if features.enabled("sp500") or features.enabled("options"):
            for scan in db.find_stuck_spy_scans(scan_cutoff):
                db.fail_spy_scan(scan["id"], scan_err)
                kind_label = {
                    "options": "Options scan",
                    "research": "Research",
                    "equity": "S&P 500 scan",
                }.get(scan.get("kind"), "S&P 500 scan")
                if scan.get("kind") != "research" and (
                    scan.get("research_scan_id") is not None
                    or scan.get("status") == "running_wait_research"
                ):
                    kind_label += " (allocation)"
                log.warning("[reaper] failed stuck %s %s", kind_label, scan["id"])
                alerts.notify_run_failed(kind=kind_label, run_id=scan["id"],
                                         label=scan.get("trade_date") or "", error=scan_err)
        for a in db.find_stuck_analyses(analysis_cutoff):
            db.fail_analysis(a["id"], analysis_err)
            log.warning("[reaper] failed stuck analysis %s", a["id"])
            alerts.notify_run_failed(kind="Analysis", run_id=a["id"],
                                     label=a.get("ticker") or "", error=analysis_err)
    except Exception:
        log.exception("[reaper] sweep failed")

    # A silently-dead worker never ran its finally-dequeue, so a scan queued
    # behind it would sit stranded 'queued' forever with nothing running. Kick
    # the queue on every sweep (idle-guarded no-op if a scan is live), so a
    # crash can't permanently wedge the pipeline. Runs regardless of whether
    # this sweep failed anything — it also recovers a queue stranded earlier.
    # Gated: the portfolio container (and its /api/portfolio/advance-queue
    # route) doesn't exist below tier 2, so this would just be a connection
    # error every 20 minutes.
    if features.enabled("schwab"):
        try:
            r = httpx.post(f"{PORTFOLIO_URL}/api/portfolio/advance-queue",
                           timeout=30, headers=_internal_headers())
            started = (r.json() or {}).get("started") if r.is_success else None
            if started:
                log.info("[reaper] advanced stranded queue → scan %s", started.get("id"))
        except Exception:
            log.exception("[reaper] advance-queue kick failed")


def register_jobs(sched: BlockingScheduler) -> None:
    """Register cron jobs, gated by feature tier (see web/features.py).

    reap_stuck_runs, outcome_sweep, and schedule_reconciler always register.
    The reconciler syncs per-account scan times and the setting-driven times
    (nightly scan, daily research), so cron registrations that used to be
    global (spy_scan, options_scan) are now created per account at runtime.
    With sp500, research_scan starts the shared daily research and
    research_retry re-starts it when missing or failed.
    """
    if features.enabled("schwab"):
        _h, _m = nightly_scan_time()
        sched.add_job(
            job_nightly_scan,
            # Mon-Fri only: holdings don't move over the closed-market weekend, so a
            # Sat/Sun scan would burn a full multi-agent LLM pass on stale Friday-close
            # data. Mirrors the spy_price_refresh weekday restriction below.
            # Time comes from the SCHEDULE_NIGHTLY_SCAN_TIME setting (default 22:00)
            # and is kept current by the schedule_reconciler.
            CronTrigger(day_of_week="mon-fri", hour=_h, minute=_m, timezone=TIMEZONE),
            id="nightly_scan",
            replace_existing=True,
        )
        sched.add_job(
            job_morning_newsletter,
            CronTrigger(hour=5, minute=0, timezone=TIMEZONE),
            id="morning_newsletter",
            replace_existing=True,
        )
        sched.add_job(
            job_token_health,
            IntervalTrigger(hours=1),
            id="token_health",
            replace_existing=True,
        )
    if features.enabled("sp500"):
        _rh, _rm = research_time()
        sched.add_job(
            job_research_scan,
            # Shared daily research, Mon-Fri (holidays skipped inside the job).
            # Time comes from the SCHEDULE_RESEARCH_TIME setting (default 00:00
            # ET) and is kept current by the schedule_reconciler.
            CronTrigger(day_of_week="mon-fri", hour=_rh, minute=_rm, timezone=TIMEZONE),
            id="research_scan",
            replace_existing=True,
        )
        sched.add_job(
            job_research_retry,
            # Re-starts today's research when it is missing past a grace window
            # or failed, bounded by RESEARCH_MAX_ATTEMPTS and the 05:30 ET cutoff.
            IntervalTrigger(minutes=15),
            id="research_retry",
            replace_existing=True,
        )
        sched.add_job(
            job_spy_price_refresh,
            CronTrigger(day_of_week="mon-fri", hour="9-16", minute=0, timezone=TIMEZONE),
            id="spy_price_refresh",
            replace_existing=True,
        )
    sched.add_job(
        job_reap_stuck_runs,
        IntervalTrigger(minutes=20),
        id="reap_stuck_runs",
        replace_existing=True,
    )
    sched.add_job(
        job_reconcile_schedules,
        IntervalTrigger(seconds=60),
        args=[sched],
        id=RECONCILE_JOB_ID,
        replace_existing=True,
    )
    sched.add_job(
        job_outcome_sweep,
        # Mon-Fri 23:30 ET: after US close (day-0 bars final) and after the
        # 22:00 nightly scan has queued, so the two don't contend.
        CronTrigger(day_of_week="mon-fri", hour=23, minute=30, timezone=TIMEZONE),
        id="outcome_sweep",
        replace_existing=True,
    )
    if features.enabled("options"):
        sched.add_job(
            job_options_refresh,
            CronTrigger(day_of_week="mon-fri", hour="10-16", minute=0, timezone=TIMEZONE),
            id="options_refresh",
            replace_existing=True,
        )
        sched.add_job(
            job_options_refresh,
            # Extra pass just after the close so the day ends on settled marks.
            CronTrigger(day_of_week="mon-fri", hour=16, minute=45, timezone=TIMEZONE),
            id="options_refresh_close",
            replace_existing=True,
        )
        sched.add_job(
            job_options_settle,
            # 20:00 ET: daily bars final; catches Friday expiries same evening and
            # holiday-shifted Thursday expiries without a market calendar.
            CronTrigger(day_of_week="mon-fri", hour=20, minute=0, timezone=TIMEZONE),
            id="options_settle",
            replace_existing=True,
        )
        sched.add_job(
            job_options_grade,
            # 20:15 ET: after the settle writes expired_* rows, before the 23:30
            # memory sweep — the learning loop grades a complete day.
            CronTrigger(day_of_week="mon-fri", hour=20, minute=15, timezone=TIMEZONE),
            id="options_grade",
            replace_existing=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-newsletter-now", action="store_true", help="Run newsletter once and exit")
    parser.add_argument("--run-scan-now", action="store_true", help="Trigger Schwab scan once and exit")
    parser.add_argument("--run-research-now", action="store_true", help="Trigger today's shared research once and exit")
    parser.add_argument("--run-spy-scan-now", action="store_true",
                        help="Trigger today's shared research once and exit (alias of --run-research-now)")
    parser.add_argument("--refresh-spy-prices", action="store_true", help="Refresh SPY prices once and exit")
    parser.add_argument("--run-sweep-now", action="store_true", help="Run outcome-resolution sweep once and exit")
    parser.add_argument("--run-options-scan-now", action="store_true", help="Trigger daily options allocation once and exit")
    parser.add_argument("--refresh-options-now", action="store_true", help="Refresh option marks once and exit")
    parser.add_argument("--settle-options-now", action="store_true", help="Run options expiry settlement once and exit")
    parser.add_argument("--grade-options-now", action="store_true", help="Run options-ledger grading/reflection once and exit")
    args = parser.parse_args()

    _apply_db_config()  # pull UI-saved SMTP/notifier/credentials onto env

    if args.send_newsletter_now:
        job_morning_newsletter()
        return
    if args.run_scan_now:
        job_nightly_scan()
        return
    if args.run_research_now or args.run_spy_scan_now:
        job_research_scan()
        return
    if args.refresh_spy_prices:
        job_spy_price_refresh()
        return
    if args.run_sweep_now:
        job_outcome_sweep()
        return
    if args.run_options_scan_now:
        job_options_scan()
        return
    if args.refresh_options_now:
        job_options_refresh()
        return
    if args.settle_options_now:
        job_options_settle()
        return
    if args.grade_options_now:
        job_options_grade()
        return

    sched = BlockingScheduler(
        timezone=TIMEZONE,
        job_defaults={
            # APScheduler default misfire_grace_time is 1 second — a fire time
            # missed by more than that (container briefly busy/restarting/
            # clock-jumped right at 22:00/00:00) is silently SKIPPED, not run
            # late. An hour of slack means a scan still happens even if the
            # process was mid-restart exactly at its cron time.
            "misfire_grace_time": 3600,
            "coalesce": True,
        },
    )
    register_jobs(sched)
    # Sweep once at startup too — a crash that happened while the scheduler was
    # down should be caught and alerted immediately, not up to 20 min later.
    job_reap_stuck_runs()
    # Sync per-account schedules immediately so the first scan window isn't
    # delayed by up to the reconciler's 60s cadence.
    job_reconcile_schedules(sched)
    log.info("Scheduler starting (tz=%s)", TIMEZONE)
    if features.enabled("schwab"):
        nh, nm = nightly_scan_time()
        log.info(" - nightly_scan       cron %02d:%02d Mon-Fri %s (settings-driven)", nh, nm, TIMEZONE)
        log.info(" - morning_newsletter cron 05:00 %s", TIMEZONE)
        log.info(" - token_health       every 1h")
    if features.enabled("sp500"):
        rh, rm = research_time()
        log.info(" - research_scan      cron %02d:%02d Mon-Fri %s (settings-driven, NYSE holidays skipped)", rh, rm, TIMEZONE)
        log.info(" - research_retry     every 15m before 05:30 ET (missing/failed research)")
        log.info(" - spy_scan           per-account daily allocation Mon-Fri (reconciled by schedule_reconciler)")
        log.info(" - spy_price_refresh  cron hourly Mon-Fri 09:00-16:00 %s", TIMEZONE)
    log.info(" - reap_stuck_runs    every 20m (stall>%dm scans / %dm analyses)",
             STUCK_SCAN_STALL_MIN, STUCK_ANALYSIS_MIN)
    log.info(" - schedule_reconciler every 60s (syncs per-account jobs + nightly scan/research times)")
    log.info(" - outcome_sweep      cron 23:30 Mon-Fri %s", TIMEZONE)
    if features.enabled("options"):
        log.info(" - options_scan       per-account daily allocation Mon-Fri (reconciled by schedule_reconciler)")
        log.info(" - options_refresh    cron hourly Mon-Fri 10:00-16:00 + 16:45 %s", TIMEZONE)
        log.info(" - options_settle     cron 20:00 Mon-Fri %s", TIMEZONE)
        log.info(" - options_grade      cron 20:15 Mon-Fri %s", TIMEZONE)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler shutting down")
        sys.exit(0)


if __name__ == "__main__":
    main()
