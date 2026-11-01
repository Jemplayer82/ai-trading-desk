"""Shared LLM factory.

Centralises the duplicated `_llm_for()` pattern previously copied across
`web/portfolio/aggregator.py` and `web/spy_allocator.py`. Use this helper
anywhere the web layer needs a quick LLM client driven by the user's saved
preferences (provider, deep_model, quick_model, backend_url).

API keys are resolved in this order:
  1. config["api_key"]            (explicit override from caller)
  2. provider_credentials row     (set via the dashboard's API Keys tab)
  3. process env var              (PROVIDER_API_KEY_ENV mapping)
  4. provider-specific fallback   (e.g. Ollama uses "ollama" as a dummy key)

For OpenAI-compatible providers this builds `langchain_openai.ChatOpenAI`.
For the "switchboard" provider it delegates to `create_llm_client` so the
bus is used consistently with the main orchestrator pipeline.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from langchain_openai import ChatOpenAI

from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
from tradingagents.llm_clients.defaults import resolve_ollama_base_url

log = logging.getLogger(__name__)


def _resolve_api_key(provider: str, config: dict[str, Any]) -> str | None:
    """Find the best available API key for `provider`."""
    if config.get("api_key"):
        return str(config["api_key"])

    # DB-stored credential — imported lazily to avoid an import cycle
    # when `db.py` ever needs to import anything from this module.
    try:
        from . import db as _db
        row = _db.get_credential(provider)
        if row and row.get("api_key"):
            return row["api_key"]
    except Exception:
        pass

    env_var = PROVIDER_API_KEY_ENV.get(provider)
    if env_var:
        env_val = os.environ.get(env_var)
        if env_val:
            return env_val

    if provider == "ollama":
        return os.environ.get("OLLAMA_API_KEY", "ollama")
    return None


def _resolve_base_url(provider: str, config: dict[str, Any], *, deep: bool = True) -> str | None:
    """Find the best base URL for `provider`, preferring the role-specific override."""
    role_key = "deep_backend_url" if deep else "quick_backend_url"
    explicit = config.get(role_key) or config.get("backend_url") or config.get("base_url")
    if explicit:
        return str(explicit)

    try:
        from . import db as _db
        row = _db.get_credential(provider)
        if row and row.get("base_url"):
            return row["base_url"]
    except Exception:
        pass

    if provider == "ollama":
        return resolve_ollama_base_url()
    return None


def llm_for(config: dict[str, Any], *, deep: bool = True, temperature: float = 0.2):
    """Build an LLM client from the user's prefs dict.

    Args:
        config: prefs dict (e.g. from `db.get_preferences()`). Recognised keys:
            llm_provider, deep_think_llm, quick_think_llm, backend_url/base_url, api_key.
        deep: if True (default), use `deep_think_llm`; else use `quick_think_llm`.
        temperature: model temperature; callers override per use-case.

    Returns:
        A LangChain BaseChatModel instance (ChatOpenAI for OpenAI-compatible
        providers, SwitchboardChatModel when provider is "switchboard").
    """
    role_key = "deep_llm_provider" if deep else "quick_llm_provider"
    provider = (config.get(role_key) or config.get("llm_provider") or "ollama").lower()
    if deep:
        model = config.get("deep_think_llm") or "gpt-oss:120b"
    else:
        model = config.get("quick_think_llm") or "gpt-oss:20b"

    if provider == "switchboard":
        from tradingagents.llm_clients import create_llm_client
        return create_llm_client(provider="switchboard", model=model).get_llm()

    kwargs: dict[str, Any] = {"model": model, "temperature": temperature}
    base_url = _resolve_base_url(provider, config, deep=deep)
    if base_url:
        kwargs["base_url"] = base_url
    api_key = _resolve_api_key(provider, config)
    if api_key:
        kwargs["api_key"] = api_key
    return ChatOpenAI(**kwargs)


def _total_budget() -> int:
    """Max concurrent LLM calls shared with single-ticker analyses.

    Read at call time so a value set via the Settings UI (OLLAMA_MAX_CONCURRENCY)
    takes effect on the next scan without a redeploy.
    """
    try:
        return max(1, int(os.environ.get("OLLAMA_MAX_CONCURRENCY", "5")))
    except (TypeError, ValueError):
        return 5


class DynamicGate:
    """A weighted resizable concurrency limiter.

    Workers acquire `weight` units of the gate before their LLM call and
    release the same amount afterwards. A call that packs `n` tickers
    into a single `llm.invoke()` should acquire `weight=n`, so the gate
    tracks ticker-equivalent prompt volume/backend load against the budget
    rather than just call count. A monitor thread calls set_limit() to
    shrink/grow the number of permitted concurrent units. Shrinking below
    the in-use count is allowed — in-flight calls finish, and no new calls
    are admitted until usage drops below the new limit. When the gate is
    completely idle, a request is admitted immediately regardless of its
    weight, so a very large batch is never starved forever; it just runs
    alone until it releases.
    """

    def __init__(self, limit: int) -> None:
        self._cv = threading.Condition()
        self._limit = max(1, limit)
        self._in_use = 0

    def set_limit(self, n: int) -> None:
        with self._cv:
            self._limit = max(1, n)
            self._cv.notify_all()

    @property
    def limit(self) -> int:
        return self._limit

    def acquire(self, weight: int = 1) -> None:
        with self._cv:
            weight = max(1, weight)
            while self._in_use > 0 and self._in_use + weight > self._limit:
                self._cv.wait(timeout=1.0)
            self._in_use += weight

    def release(self, weight: int = 1) -> None:
        with self._cv:
            weight = max(1, weight)
            self._in_use = max(0, self._in_use - weight)
            self._cv.notify_all()

    def __enter__(self) -> DynamicGate:
        self.acquire(1)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release(1)


class _GateMonitor:
    """Background thread that keeps a DynamicGate sized to the live budget.

    scan_limit = max(1, TOTAL - active_single_ticker_analyses)
    so single-ticker analyses always get priority and the scan floors at 1.
    The active count comes from the shared llm_activity table (written by the
    api container, heartbeat TTL pruning), which is what makes the throttling
    work across containers.
    """

    def __init__(self, gate: DynamicGate, poll_seconds: float = 3.0) -> None:
        self._gate = gate
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _recompute(self) -> int:
        total = _total_budget()
        active = 0
        try:
            from . import db as _db
            active = _db.count_active_single()
        except Exception:
            log.debug("[gate] count_active_single failed", exc_info=True)
        return max(1, total - active)

    def _run(self) -> None:
        while not self._stop.is_set():
            limit = self._recompute()
            if limit != self._gate.limit:
                log.info("[gate] resizing scan concurrency to %d", limit)
            self._gate.set_limit(limit)
            self._stop.wait(self._poll)

    def __enter__(self) -> DynamicGate:
        # Apply an initial limit synchronously before any work starts.
        self._gate.set_limit(self._recompute())
        self._thread.start()
        return self._gate

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
