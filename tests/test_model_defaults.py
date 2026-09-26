"""Provider-aware model defaults (web.runner.build_config / _catalog_default).

Regression cover for the production bug where empty preferences + a non-OpenAI
provider still yielded an OpenAI model name. Pure T1 — touches only
web.runner and web.providers, no spy_scanner/options_engine dependency, so
this stays in every tier (unlike test_scan_health_guard.py, which needs
spy_scanner and is T3+ only).
"""

import pytest

from web import runner

pytestmark = pytest.mark.unit


def test_catalog_default_skips_custom_sentinel(monkeypatch):
    monkeypatch.setattr(runner, "__name__", runner.__name__)  # no-op, keeps lint quiet
    val = runner._catalog_default("ollama", "quick")
    assert val and val != "custom"


def test_explicit_param_wins_over_catalog():
    cfg = runner.build_config({"provider": "ollama", "quick_model": "my-model:tag"})
    assert cfg["quick_think_llm"] == "my-model:tag"


def test_env_override_beats_catalog(monkeypatch):
    """TRADINGAGENTS_QUICK_THINK_LLM is a deliberate operator override and must
    outrank the provider catalog."""
    monkeypatch.setenv("TRADINGAGENTS_QUICK_THINK_LLM", "operator-choice")
    monkeypatch.setitem(runner.DEFAULT_CONFIG, "quick_think_llm", "operator-choice")
    cfg = runner.build_config({"provider": "ollama"})
    assert cfg["quick_think_llm"] == "operator-choice"


def test_fresh_db_gets_a_provider_appropriate_model(monkeypatch):
    """The actual production bug: empty preferences + provider=ollama must not
    yield an OpenAI model name."""
    monkeypatch.delenv("TRADINGAGENTS_QUICK_THINK_LLM", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_DEEP_THINK_LLM", raising=False)
    monkeypatch.setitem(runner.DEFAULT_CONFIG, "quick_think_llm", "gpt-5.4-mini")
    monkeypatch.setitem(runner.DEFAULT_CONFIG, "deep_think_llm", "gpt-5.4")
    cfg = runner.build_config({})
    assert cfg["quick_llm_provider"] == "ollama"
    assert not cfg["quick_think_llm"].startswith("gpt-5."), cfg["quick_think_llm"]
    assert not cfg["deep_think_llm"].startswith("gpt-5."), cfg["deep_think_llm"]


def test_same_provider_default_is_left_alone(monkeypatch):
    """An OpenAI user with empty prefs keeps DEFAULT_CONFIG's OpenAI model —
    the catalog fallback is only for cross-provider mismatch."""
    monkeypatch.delenv("TRADINGAGENTS_DEEP_THINK_LLM", raising=False)
    cfg = runner.build_config({"provider": "openai"})
    assert cfg["deep_think_llm"] == runner.DEFAULT_CONFIG["deep_think_llm"]


def test_catalog_has_no_retired_models():
    """Retired Ollama Cloud names return HTTP 410 while still looking valid.
    These four went dead in production while still being offered."""
    from web.providers import _OLLAMA_CLOUD_MODELS

    retired = {"kimi-k2:1t-cloud", "glm-4.6:cloud",
               "deepseek-v3.1:671b-cloud", "qwen3-coder:480b-cloud"}
    offered = {v for mode in _OLLAMA_CLOUD_MODELS.values() for _, v in mode}
    assert not (offered & retired), f"retired models still offered: {offered & retired}"


def test_switchboard_default_is_an_always_latest_alias():
    """The switchboard menu leads with the Claude CLI's bare family names.

    Cleo hands the value straight to `claude --model`, which resolves `opus` /
    `sonnet` to whatever is current at call time — so background scans track
    the latest model without a commit. `_catalog_default` takes the first
    non-"custom" entry, which is what makes leading position load-bearing
    rather than cosmetic. `haiku` is deliberately absent from this provider's
    menus — see the model_catalog.py comment for the tool-call reliability
    finding that got it removed.
    """
    assert runner._catalog_default("switchboard", "deep") == "opus"
    assert runner._catalog_default("switchboard", "quick") == "sonnet"


def test_haiku_is_not_offered_on_switchboard():
    """Unreliable tool-call compliance on Cleo's inline marker protocol —
    live-tested 2026-08-06, stalled through both the original attempt and the
    corrective retry on market/news/fundamentals. Still fine as a direct
    Anthropic-API model (real tool binding, different code path) — this
    exclusion is switchboard-only."""
    from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS

    for mode in ("quick", "deep"):
        values = [v for _, v in MODEL_OPTIONS["switchboard"][mode]]
        assert not any("haiku" in v.lower() for v in values), (mode, values)


def test_switchboard_offers_only_always_latest_aliases():
    """Landon 2026-09-26: no pinned snapshots in the switchboard menus — every
    entry runs the latest model of its family. Pinning stays possible through
    the "custom" entry, which must remain in both lists."""
    from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS

    for mode in ("quick", "deep"):
        values = [v for _, v in MODEL_OPTIONS["switchboard"][mode]]
        assert not any(v.startswith("claude-") for v in values), (mode, values)
        assert "custom" in values, mode
    assert "fable" in [v for _, v in MODEL_OPTIONS["switchboard"]["deep"]]
    assert "fable" not in [v for _, v in MODEL_OPTIONS["switchboard"]["quick"]]
