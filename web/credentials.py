"""Centralised LLM-provider credential management.

The core `tradingagents/` package reads provider API keys from
process-level env vars (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc. — see
`tradingagents/llm_clients/api_key_env.py`). To let the user enter
keys via the web UI without restarting containers, we persist them in
`provider_credentials` (sqlite, `web/db.py`) and copy them onto
`os.environ` on startup and on every save. Env vars set externally
(.env, docker-compose) remain visible if no DB row overrides them.

Never echo a raw key back to the client. All UI-facing endpoints go
through `list_meta()` which masks the secret.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV

from . import db

log = logging.getLogger(__name__)


def mask_key(key: str | None) -> str:
    """Reveal at most the last 4 characters of a key."""
    if not key:
        return ""
    if len(key) <= 6:
        return "•" * len(key)
    return "•••• " + key[-4:]


def apply_to_env() -> None:
    """Populate os.environ from DB-stored credentials.

    Called at FastAPI startup and after every PUT/DELETE so changes
    take effect immediately within this process. Other containers
    (portfolio, scheduler) pick up the change at their next startup.
    """
    rows = db.list_credentials()
    by_provider = {r["provider"]: r for r in rows}
    applied = 0
    for prov, env_var in PROVIDER_API_KEY_ENV.items():
        if not env_var:
            continue  # ollama uses no key
        row = by_provider.get(prov)
        if row and row.get("api_key"):
            os.environ[env_var] = row["api_key"]
            applied += 1
        # else: leave any externally-set env var (.env, compose) alone
    if applied:
        log.info("[credentials] applied %d provider keys from DB to env", applied)


def list_meta() -> list[dict[str, Any]]:
    """Per-provider credential metadata — masked, safe to return to the client.

    For each known provider, reports whether a key exists, where it
    comes from (db / env / none), and a 4-char preview.
    """
    rows = db.list_credentials()
    by_provider = {r["provider"]: r for r in rows}

    out: list[dict[str, Any]] = []
    for prov, env_var in PROVIDER_API_KEY_ENV.items():
        row = by_provider.get(prov)
        db_key = (row or {}).get("api_key") or ""
        env_val = os.environ.get(env_var) if env_var else None
        effective = db_key or env_val or ""
        source = "db" if db_key else ("env" if env_val else None)
        out.append({
            "provider": prov,
            "env_var": env_var,
            "has_key": bool(effective),
            "masked": mask_key(effective),
            "source": source,
            "base_url": (row or {}).get("base_url"),
            "updated_at": (row or {}).get("updated_at"),
        })
    return out


# ===================================================================
# App settings — env-style config the user manages from the UI.
#
# Anything NOT an LLM-provider key (those live in provider_credentials,
# above): Schwab OAuth, market-data keys, Ollama, SMTP/newsletter, the
# notifier webhook, plus arbitrary custom env vars the user adds.
#
# All of these env vars are read CALL-TIME inside functions, so copying
# a DB value onto os.environ at startup / on save takes effect without a
# restart. Import-time-read vars (paths, WEB_DB, TOKEN paths) are
# deliberately NOT in the registry — a DB value couldn't take effect for
# them, and they aren't credentials.
# ===================================================================

# Each entry: key (env var), label, group, secret (mask?), placeholder.
# T1 groups come first; the TIER:2 block below holds everything that only
# applies once the "schwab" feature is on (see web/features.py). Order here
# only affects Settings-page group display order — cosmetic, not functional.
SETTINGS_REGISTRY: list[dict[str, Any]] = [
    # Market data
    {"key": "TECHNICAL_INDICATOR_VENDOR", "label": "Technical indicators source", "group": "Market Data", "secret": False, "type": "select", "options": ["yfinance", "alpha_vantage"], "placeholder": "yfinance = free local calc (Schwab OHLCV feeds it when 'Use Schwab for market data' is on); alpha_vantage = pre-calculated (needs the key below)"},  # pragma: allowlist secret
    {"key": "ALPHA_VANTAGE_API_KEY", "label": "Alpha Vantage API Key", "group": "Market Data", "secret": True, "placeholder": "Only needed when the indicators source is alpha_vantage"},  # pragma: allowlist secret
    # Ollama / LLM infra
    {"key": "OLLAMA_BASE_URL", "label": "Ollama Base URL", "group": "Ollama & Bus Routing", "secret": False, "placeholder": "https://ollama.com/v1 or http://host:11434/v1"},  # pragma: allowlist secret
    {"key": "OLLAMA_API_KEY", "label": "Ollama API Key", "group": "Ollama & Bus Routing", "secret": True, "placeholder": "Ollama Cloud auth token"},  # pragma: allowlist secret
    {"key": "OLLAMA_MAX_CONCURRENCY", "label": "Max concurrent LLM analyses", "group": "Ollama & Bus Routing", "secret": False, "placeholder": "Shared concurrency budget across single-ticker analyses and (T3+) the S&P 500 scanner (default 3, floor 1)."},
    {"key": "SWITCHBOARD_TARGET_AGENT", "label": "Switchboard — LLM handler agent", "group": "Ollama & Bus Routing", "secret": False, "placeholder": "Bus agent that answers LLM requests: 'llm-router' (built-in → Ollama/OpenAI) or 'cleo' (your local free claude -p session)"},  # pragma: allowlist secret
    {"key": "SWITCHBOARD_CHATGPT_AGENT", "label": "Switchboard — ChatGPT handler agent", "group": "Ollama & Bus Routing", "secret": False, "placeholder": "Bus agent that answers the 'chatgpt' models (scripts/codex_llm_handler.py); blank uses 'codex'"},  # pragma: allowlist secret
    # Email / alerts + newsletter — SMTP/FRED_NOTIFY_URL are T1 (run-failure
    # alerts fire for single-ticker analyses too, see web/alerts.py +
    # web/mailer.py); NEWSLETTER_* only matter once the T2 morning newsletter
    # job registers, but the fields are harmless unused at T1.
    {"key": "SMTP_HOST", "label": "SMTP Host", "group": "Email / Alerts & Newsletter", "secret": False, "placeholder": "smtp.gmail.com"},
    {"key": "SMTP_PORT", "label": "SMTP Port", "group": "Email / Alerts & Newsletter", "secret": False, "placeholder": "587"},
    {"key": "SMTP_USER", "label": "SMTP Username", "group": "Email / Alerts & Newsletter", "secret": False, "placeholder": "you@example.com"},
    {"key": "SMTP_PASS", "label": "SMTP Password", "group": "Email / Alerts & Newsletter", "secret": True, "placeholder": "App password"},  # pragma: allowlist secret
    {"key": "NEWSLETTER_FROM", "label": "Newsletter From", "group": "Email / Alerts & Newsletter", "secret": False, "placeholder": "defaults to SMTP username"},
    {"key": "NEWSLETTER_TO", "label": "Newsletter To", "group": "Email / Alerts & Newsletter", "secret": False, "placeholder": "recipient@example.com"},
    # Notifications
    {"key": "FRED_NOTIFY_URL", "label": "Notify Webhook URL", "group": "Notifications", "secret": True, "placeholder": "WhatsApp/webhook URL (leave blank to disable)"},  # pragma: allowlist secret
    # TIER:2 BEGIN
    # Automation schedule — time-of-day for the nightly Schwab portfolio scan
    # (Mon-Fri, SCHEDULER_TIMEZONE). Read at reconcile time by web/scheduler.py,
    # so a change takes effect within ~60s with no container restart. Per-paper-
    # account scan times live on paper_accounts.schedule_time instead.
    {"key": "SCHEDULE_NIGHTLY_SCAN_TIME", "label": "Nightly portfolio scan time (ET, HH:MM)", "group": "Automation Schedule", "secret": False, "type": "text", "placeholder": "22:00 — 24-hour HH:MM, Mon-Fri; blank uses 22:00"},  # pragma: allowlist secret
    # Brokerage (Schwab) — data-source switch
    {"key": "SCHWAB_ENABLED", "label": "Data source — Schwab MCP (on) vs free built-in tools (off)", "group": "Brokerage (Schwab)", "secret": False, "type": "toggle", "on_label": "On — Schwab", "off_label": "Off — free / yfinance", "placeholder": "on = Schwab holdings + market data; off hides holdings and uses free yfinance"},
    # Brokerage (Schwab OAuth app credentials)
    {"key": "SCHWAB_APP_KEY", "label": "Schwab App Key", "group": "Brokerage (Schwab)", "secret": True, "placeholder": "Client ID from the Schwab developer portal"},  # pragma: allowlist secret
    {"key": "SCHWAB_APP_SECRET", "label": "Schwab App Secret", "group": "Brokerage (Schwab)", "secret": True, "placeholder": "Client secret"},  # pragma: allowlist secret
    {"key": "SCHWAB_CALLBACK_URL", "label": "Schwab Callback URL", "group": "Brokerage (Schwab)", "secret": False, "placeholder": "https://trading.txferguson.net/api/auth/schwab/callback"},
    {"key": "SCHWAB_MCP_URL", "label": "Schwab MCP URL", "group": "Brokerage (Schwab)", "secret": False, "placeholder": "http://100.112.40.124:3105/mcp"},
    {"key": "SCHWAB_MARKET_DATA", "label": "Use Schwab for market data (off = free yfinance)", "group": "Brokerage (Schwab)", "secret": False, "type": "toggle", "on_label": "On — Schwab quotes", "off_label": "Off — yfinance", "placeholder": ""},
    # Brokerage (Alpaca) — credential fields stored, no live holdings integration yet
    {"key": "ALPACA_ENABLED", "label": "Alpaca (credential fields only — holdings not yet wired)", "group": "Brokerage (Alpaca)", "secret": False, "type": "toggle", "on_label": "On", "off_label": "Off", "placeholder": ""},
    {"key": "ALPACA_API_KEY", "label": "Alpaca API Key", "group": "Brokerage (Alpaca)", "secret": True, "placeholder": "API key ID from alpaca.markets"},  # pragma: allowlist secret
    {"key": "ALPACA_API_SECRET", "label": "Alpaca API Secret", "group": "Brokerage (Alpaca)", "secret": True, "placeholder": "API secret"},  # pragma: allowlist secret
    {"key": "ALPACA_BASE_URL", "label": "Alpaca Base URL", "group": "Brokerage (Alpaca)", "secret": False, "placeholder": "https://api.alpaca.markets"},
    # TIER:2 END
    # TIER:3 BEGIN
    # Shared daily research start time (Mon-Fri, SCHEDULER_TIMEZONE); read at reconcile time by web/scheduler.py research_time(), so edits apply within ~60 s.
    {"key": "SCHEDULE_RESEARCH_TIME", "label": "Daily shared research start (ET, HH:MM)", "group": "Automation Schedule", "secret": False, "type": "text", "placeholder": "00:00 — Mon-Fri, must be before 05:30 (runs for that same day); the one research pass every paper account allocates from"},  # pragma: allowlist secret
    # TIER:3 END
]

def validate_setting_value(key: str, value: str) -> str | None:
    """Return an error message if ``value`` is not acceptable for ``key``.

    Schedule settings must be 24-hour HH:MM; the shared research time must
    also fall before the 05:30 ET research cutoff, because the scheduler
    silently falls back to 00:00 otherwise (web/scheduler.py research_time).
    """
    from . import account_policy  # local: credentials is imported early by db/main

    if key in ("SCHEDULE_NIGHTLY_SCAN_TIME", "SCHEDULE_RESEARCH_TIME"):
        hm = account_policy.parse_hhmm(value)
        if hm is None:
            return "must be a 24-hour time HH:MM"
        if key == "SCHEDULE_RESEARCH_TIME" and hm >= (5, 30):
            return ("must be between 00:00 and 05:29 ET — research runs for the day it "
                    "fires on and has to finish before that day's 10:30 allocation deadline")
    return None


# Secrets that may come ONLY from the stack environment: never accepted, stored, shown or overridden through
# the Settings UI/API (a DB value would win over the stack value in apply_settings_to_env).
STACK_ONLY_KEYS = frozenset({"CLEO_SCHWAB_MCP_TOKEN"})

_REGISTRY_BY_KEY = {s["key"]: s for s in SETTINGS_REGISTRY}
_REGISTRY_KEYS = set(_REGISTRY_BY_KEY)


def mask_setting(key: str, value: str | None) -> str:
    """Mask secrets; show non-secret config values verbatim."""
    spec = _REGISTRY_BY_KEY.get(key)
    is_secret = spec["secret"] if spec else True  # custom keys treated as secret
    if not value:
        return ""
    return mask_key(value) if is_secret else value


def apply_settings_to_env() -> None:
    """Copy every stored app_setting onto os.environ.

    Runs at startup in all three service containers (api, portfolio,
    scheduler) and again after every settings PUT/DELETE. Assignment is
    unconditional, so a DB-stored value overrides whatever .env / compose
    set for the same key — the Settings UI always wins.
    """
    applied = 0
    for row in db.list_app_settings():
        key, value = row["key"], row["value"]
        if key in STACK_ONLY_KEYS:
            continue
        if value:
            os.environ[key] = value
            applied += 1
    if applied:
        log.info("[settings] applied %d app settings from DB to env", applied)


def list_settings_meta() -> dict[str, Any]:
    """Registry settings + custom settings, masked. Safe to return to client."""
    stored = {r["key"]: r for r in db.list_app_settings()}

    registry_out: list[dict[str, Any]] = []
    for spec in SETTINGS_REGISTRY:
        key = spec["key"]
        row = stored.get(key)
        db_val = (row or {}).get("value") or ""
        env_val = os.environ.get(key) or ""
        effective = db_val or env_val
        source = "db" if db_val else ("env" if env_val else None)
        registry_out.append({
            "key": key,
            "label": spec["label"],
            "group": spec["group"],
            "secret": spec["secret"],
            "type": spec.get("type", "text"),
            "on_label": spec.get("on_label", "On"),
            "off_label": spec.get("off_label", "Off"),
            "options": spec.get("options", []),
            "placeholder": spec.get("placeholder", ""),
            "has_value": bool(effective),
            "masked": mask_setting(key, effective),
            "source": source,
            "updated_at": (row or {}).get("updated_at"),
        })

    custom_out: list[dict[str, Any]] = []
    for key, row in stored.items():
        if key in _REGISTRY_KEYS:
            continue
        val = row.get("value") or ""
        custom_out.append({
            "key": key,
            "secret": True,
            "has_value": bool(val),
            "masked": mask_key(val),
            "source": "db",
            "updated_at": row.get("updated_at"),
        })

    return {"registry": registry_out, "custom": custom_out}
