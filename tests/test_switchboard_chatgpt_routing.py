"""ChatGPT models on the switchboard provider go to the Codex handler agent."""
from __future__ import annotations

import pytest

from tradingagents.llm_clients import switchboard_client as sc


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_URL", "http://bus")
    monkeypatch.setenv("SWITCHBOARD_MCP_TOKEN", "t")
    monkeypatch.setenv("SWITCHBOARD_TARGET_AGENT", "cleo")
    monkeypatch.delenv("SWITCHBOARD_CHATGPT_AGENT", raising=False)


def _model(name):
    return sc.SwitchboardLLMClient(model=name).get_llm()


@pytest.mark.parametrize("name", ["chatgpt", "ChatGPT", "chatgpt:gpt-6-astra"])
def test_chatgpt_models_route_to_codex(name):
    assert _model(name).target_agent_id == "codex"


@pytest.mark.parametrize("name", ["sonnet", "opus", "fable", "llama3", "gpt-oss:120b"])
def test_other_models_keep_the_default_handler(name):
    assert _model(name).target_agent_id == "cleo"


def test_chatgpt_agent_is_configurable(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_CHATGPT_AGENT", "gpt-box")
    assert _model("chatgpt").target_agent_id == "gpt-box"


def test_menus_offer_every_chatgpt_family_in_both_roles():
    from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS
    for mode in ("quick", "deep"):
        values = [v for _, v in MODEL_OPTIONS["switchboard"][mode]]
        for fam in ("astra", "sol", "luna", "terra"):
            assert f"chatgpt:{fam}" in values, (mode, fam)
            assert _model(f"chatgpt:{fam}").target_agent_id == "codex"
