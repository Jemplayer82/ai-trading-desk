import warnings

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from tradingagents.agents.schemas import TraderProposal
from tradingagents.agents.utils.structured import bind_structured
from tradingagents.llm_clients.anthropic_client import NormalizedChatAnthropic


@pytest.mark.parametrize(
    ("model", "thinking"),
    [
        ("claude-opus-5-5", None),
        ("claude-fable-5-1-snapshot", None),
        ("claude-sonnet-4-6", {"type": "enabled", "budget_tokens": 1024}),
        ("claude-sonnet-4-6", {"type": "adaptive"}),
    ],
)
def test_native_json_schema_for_models_that_cannot_force_tools(monkeypatch, model, thinking):
    llm = NormalizedChatAnthropic(
        model=model, thinking=thinking, api_key="dummy", max_tokens=2048,
    )
    proposal = TraderProposal(action="Sell", reasoning="Weak earnings.")
    payloads = []

    def generate(self, messages, stop=None, run_manager=None, **kwargs):
        payloads.append(self._get_request_payload(messages, stop=stop, **kwargs))
        return ChatResult(generations=[ChatGeneration(
            message=AIMessage(content=proposal.model_dump_json()),
        )])

    monkeypatch.setattr(NormalizedChatAnthropic, "_generate", generate)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = llm.with_structured_output(TraderProposal).invoke("Propose a trade.")

    assert not caught
    assert isinstance(result, TraderProposal)
    assert result == proposal
    payload, = payloads
    output_format = payload["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    assert "action" in output_format["schema"]["properties"]
    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_non_thinking_model_keeps_default_forced_tool_call():
    llm = NormalizedChatAnthropic(model="claude-sonnet-4-6", api_key="dummy")
    bound_model = llm.with_structured_output(TraderProposal).first

    assert bound_model.kwargs["tool_choice"] == {"type": "tool", "name": "TraderProposal"}
    assert bound_model.kwargs["tools"][0]["name"] == "TraderProposal"
    assert "output_config" not in bound_model.kwargs


def test_explicit_method_overrides_the_model_default():
    llm = NormalizedChatAnthropic(model="claude-opus-5-5", api_key="dummy")

    with pytest.warns(UserWarning, match="forced tool calling.*not supported"):
        bound_model = llm.with_structured_output(TraderProposal, method="function_calling").first

    assert bound_model.kwargs["tools"][0]["name"] == "TraderProposal"
    assert "output_config" not in bound_model.kwargs


def test_legacy_langchain_falls_back_without_binding_a_tool(monkeypatch):
    def old_bind(self, schema, **kwargs):
        pytest.fail("Legacy LangChain would ignore method and bind a tool")

    monkeypatch.setattr(ChatAnthropic, "with_structured_output", old_bind)
    llm = NormalizedChatAnthropic(model="claude-opus-5-5", api_key="dummy")

    with pytest.raises(NotImplementedError, match="lacks json_schema support"):
        llm.with_structured_output(TraderProposal)
    assert bind_structured(llm, TraderProposal, "Trader") is None
