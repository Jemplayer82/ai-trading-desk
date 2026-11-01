"""Tests for TRADINGAGENTS_* env-var overlay onto DEFAULT_CONFIG."""

from __future__ import annotations

import importlib

import pytest

import tradingagents.default_config as default_config_module


def _reload_with_env(monkeypatch, **overrides):
    """Set/clear env vars then reload default_config to re-evaluate DEFAULT_CONFIG."""
    for key in list(default_config_module._ENV_OVERRIDES):
        monkeypatch.delenv(key, raising=False)
    for key, val in overrides.items():
        monkeypatch.setenv(key, val)
    return importlib.reload(default_config_module)


def test_no_env_uses_built_in_defaults(monkeypatch):
    dc = _reload_with_env(monkeypatch)
    assert dc.DEFAULT_CONFIG["llm_provider"] == "openai"
    assert dc.DEFAULT_CONFIG["deep_think_llm"] == "gpt-6.1-sol"
    assert dc.DEFAULT_CONFIG["quick_think_llm"] == "gpt-6-luna"
    assert dc.DEFAULT_CONFIG["backend_url"] is None
    assert dc.DEFAULT_CONFIG["max_debate_rounds"] == 1
    assert dc.DEFAULT_CONFIG["checkpoint_enabled"] is False


def test_string_overrides(monkeypatch):
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_LLM_PROVIDER="google",
        TRADINGAGENTS_DEEP_THINK_LLM="gemini-3-pro-preview",
        TRADINGAGENTS_QUICK_THINK_LLM="gemini-3-flash-preview",
        TRADINGAGENTS_LLM_BACKEND_URL="https://example.invalid/v1",
        TRADINGAGENTS_OUTPUT_LANGUAGE="Chinese",
    )
    assert dc.DEFAULT_CONFIG["llm_provider"] == "google"
    assert dc.DEFAULT_CONFIG["deep_think_llm"] == "gemini-3-pro-preview"
    assert dc.DEFAULT_CONFIG["quick_think_llm"] == "gemini-3-flash-preview"
    assert dc.DEFAULT_CONFIG["backend_url"] == "https://example.invalid/v1"
    assert dc.DEFAULT_CONFIG["output_language"] == "Chinese"


def test_int_coercion(monkeypatch):
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_MAX_DEBATE_ROUNDS="3",
        TRADINGAGENTS_MAX_RISK_ROUNDS="2",
    )
    assert dc.DEFAULT_CONFIG["max_debate_rounds"] == 3
    assert isinstance(dc.DEFAULT_CONFIG["max_debate_rounds"], int)
    assert dc.DEFAULT_CONFIG["max_risk_discuss_rounds"] == 2
    assert isinstance(dc.DEFAULT_CONFIG["max_risk_discuss_rounds"], int)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
    ],
)
def test_bool_coercion(monkeypatch, raw, expected):
    dc = _reload_with_env(monkeypatch, TRADINGAGENTS_CHECKPOINT_ENABLED=raw)
    assert dc.DEFAULT_CONFIG["checkpoint_enabled"] is expected


def test_empty_env_value_is_passthrough(monkeypatch):
    """Empty TRADINGAGENTS_* values must not clobber the built-in default."""
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_LLM_PROVIDER="",
        TRADINGAGENTS_MAX_DEBATE_ROUNDS="",
    )
    assert dc.DEFAULT_CONFIG["llm_provider"] == "openai"
    assert dc.DEFAULT_CONFIG["max_debate_rounds"] == 1


def test_invalid_int_raises(monkeypatch):
    """Garbage int values should surface a ValueError at import, not silently misconfigure."""
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "not-a-number")
    with pytest.raises(ValueError):
        importlib.reload(default_config_module)
    # Restore module state for subsequent tests in this process
    monkeypatch.delenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", raising=False)
    importlib.reload(default_config_module)


def test_unknown_env_var_is_ignored(monkeypatch):
    """Env vars outside _ENV_OVERRIDES must not bleed into DEFAULT_CONFIG."""
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_NONEXISTENT_KEY="oops",
    )
    assert "nonexistent_key" not in dc.DEFAULT_CONFIG


def test_options_learning_overrides(monkeypatch):
    """The learning loop's operational knobs — including the documented kill
    switch — must actually be wired through _ENV_OVERRIDES with correct types."""
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_DEEP_DIVE_STORE_DECISIONS="false",
        TRADINGAGENTS_OPTIONS_LESSONS_MIN_CLOSED="25",
        TRADINGAGENTS_OPTIONS_REFLECT_MIN_NEW_CLOSED="9",
    )
    assert dc.DEFAULT_CONFIG["deep_dive_store_decisions"] is False
    assert dc.DEFAULT_CONFIG["options_lessons_min_closed"] == 25
    assert isinstance(dc.DEFAULT_CONFIG["options_lessons_min_closed"], int)
    assert dc.DEFAULT_CONFIG["options_reflect_min_new_closed"] == 9


def test_options_learning_defaults(monkeypatch):
    dc = _reload_with_env(monkeypatch)
    assert dc.DEFAULT_CONFIG["deep_dive_store_decisions"] is True
    assert dc.DEFAULT_CONFIG["options_lessons_min_closed"] == 10
    assert dc.DEFAULT_CONFIG["options_reflect_min_new_closed"] == 5
    assert dc.DEFAULT_CONFIG["memory_log_max_entries"] == 1000
    assert dc.DEFAULT_CONFIG["research_manager_role"] == "quick"


def test_research_manager_role_override(monkeypatch):
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_RESEARCH_MANAGER_ROLE="deep",
    )
    assert dc.DEFAULT_CONFIG["research_manager_role"] == "deep"


def test_memory_context_decision_max_chars_default_and_override(monkeypatch):
    dc = _reload_with_env(monkeypatch)
    assert dc.DEFAULT_CONFIG["memory_context_decision_max_chars"] == 400
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_MEMORY_CONTEXT_DECISION_MAX_CHARS="150",
    )
    assert dc.DEFAULT_CONFIG["memory_context_decision_max_chars"] == 150
    assert isinstance(dc.DEFAULT_CONFIG["memory_context_decision_max_chars"], int)
