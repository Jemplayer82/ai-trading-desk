"""The model lists must offer the newest verified models first (11/01/2026), because the
background scans take the FIRST non-custom entry of each list as the default."""
import re

import pytest

from tradingagents.llm_clients.capabilities import get_capabilities
from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS, get_model_options

pytestmark = pytest.mark.unit


def first(provider, mode):
    return next(v for _, v in get_model_options(provider, mode) if v != "custom")


@pytest.mark.parametrize("provider,mode,expected", [
    ("anthropic", "deep", "claude-opus-5-5"),
    ("anthropic", "quick", "claude-sonnet-5-5"),
    ("openai", "deep", "gpt-6.1-sol"),
    ("openai", "quick", "gpt-6-luna"),
    ("google", "deep", "gemini-4-argon"),
    ("google", "quick", "gemini-3.8-flash"),
    ("xai", "deep", "grok-4.7"),
    ("deepseek", "deep", "deepseek-v4-pro"),
    ("deepseek", "quick", "deepseek-flash"),
    ("glm", "deep", "glm-5.3"),
    ("qwen", "deep", "qwen3.8-max"),
    ("minimax", "deep", "MiniMax-M3.1-Flash"),
])
def test_defaults_are_the_newest_models(provider, mode, expected):
    assert first(provider, mode) == expected


def test_anthropic_list_has_only_the_current_generation():
    ids = {v for mode in ("quick", "deep") for _, v in MODEL_OPTIONS["anthropic"][mode]}
    assert ids == {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"}


def test_retired_ollama_cloud_models_are_gone():
    ids = {v for mode in ("quick", "deep") for _, v in MODEL_OPTIONS["ollama"][mode]}
    assert "qwen3.5:397b" not in ids and "deepseek-v4-flash" not in ids      # HTTP 410 since 09/25/2026


def test_no_list_repeats_a_model_within_one_mode():
    for provider, modes in MODEL_OPTIONS.items():
        for mode, options in modes.items():
            values = [v for _, v in options if v != "custom"]
            assert len(values) == len(set(values)), (provider, mode)


def test_new_deepseek_name_gets_the_thinking_rules():
    assert get_capabilities("deepseek-flash").requires_reasoning_content_roundtrip is True


def test_anthropic_5_5_ids_get_the_effort_setting_but_haiku_does_not():
    from tradingagents.llm_clients import anthropic_client as mod
    assert mod._supports_effort("claude-opus-5-5") and mod._supports_effort("claude-sonnet-5-5")
    assert not mod._supports_effort("claude-haiku-5-5")


def test_google_thinking_level_is_used_for_gemini_4(monkeypatch):
    from tradingagents.llm_clients import google_client as gc
    seen = {}

    class Fake:
        def __init__(self, **kw):
            seen.update(kw)
    monkeypatch.setattr(gc, "NormalizedChatGoogleGenerativeAI", Fake)
    gc.GoogleClient("gemini-4-argon", thinking_level="high", google_api_key="x").get_llm()
    assert seen.get("thinking_level") == "high" and "thinking_budget" not in seen


def test_ids_look_sane():
    for provider, modes in MODEL_OPTIONS.items():
        for options in modes.values():
            for _, v in options:
                assert v == "custom" or re.match(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$", v), (provider, v)
