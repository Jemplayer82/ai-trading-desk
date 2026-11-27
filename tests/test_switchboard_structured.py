from unittest.mock import Mock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from tradingagents.agents.schemas import (
    PortfolioDecision,
    ResearchPlan,
    render_pm_decision,
    render_research_plan,
)
from tradingagents.agents.utils.rating import parse_rating
from tradingagents.agents.utils.structured import bind_structured, invoke_structured_or_freetext
from tradingagents.llm_clients.switchboard_client import (
    SwitchboardChatModel,
    _format_tools,
    _to_anthropic_format,
)
from tradingagents.orchestrator.gated_llm import GatedLLM


@pytest.fixture
def bus():
    return SwitchboardChatModel(model_name="chatgpt:luna", bus_url="http://bus.invalid", token="dummy")


@pytest.fixture
def plan():
    return ResearchPlan(recommendation="Sell", rationale="Weak earnings.", strategic_actions="Reduce exposure.")


def _reply(content):
    return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])


@pytest.mark.parametrize("template", ["{}", "```json\n{}\n```", "Here is the plan: {} Good luck."])
def test_structured_reply_formats(bus, plan, template):
    with patch.object(SwitchboardChatModel, "_generate", autospec=True) as generate:
        generate.return_value = _reply(template.format(plan.model_dump_json()))
        result = bus.with_structured_output(ResearchPlan).invoke("Make a plan.")

    assert isinstance(result, ResearchPlan)
    assert result == plan
    generate.assert_called_once()
    messages = generate.call_args.args[1]
    assert len(messages) == 2
    assert messages[0] == HumanMessage(content="Make a plan.")


def test_structured_request_preserves_system_prompt_without_sending_tools(bus, plan):
    prompt = [
        {"role": "system", "content": "You are the Research Manager."},
        {"role": "user", "content": "Make a plan."},
    ]
    with patch.object(SwitchboardChatModel, "_generate", autospec=True) as generate:
        generate.return_value = _reply(plan.model_dump_json())
        assert bus.with_structured_output(ResearchPlan).invoke(prompt) == plan

    generate.assert_called_once()
    messages = generate.call_args.args[1]
    assert len(messages) == 3
    assert isinstance(messages[-1], HumanMessage)
    assert "tools" not in generate.call_args.kwargs
    assert "tool_choice" not in generate.call_args.kwargs
    system, wire_messages = _to_anthropic_format(messages)
    instructions = PydanticOutputParser(pydantic_object=ResearchPlan).get_format_instructions()
    assert system == "You are the Research Manager."
    assert wire_messages == [
        {"role": "user", "content": "Make a plan."},
        {"role": "user", "content": instructions + "\nReply with only the JSON object."},
    ]
    assert len(prompt) == 2


def test_invalid_structured_reply_falls_back_to_one_plain_call(bus):
    prompt = "Make a plan."
    with patch.object(SwitchboardChatModel, "_generate", autospec=True) as generate:
        generate.side_effect = [_reply('{"recommendation": "Unknown"}'), _reply("Sell after earnings.")]
        result = invoke_structured_or_freetext(
            bind_structured(bus, ResearchPlan, "Research Manager"),
            bus, prompt, render_research_plan, "Research Manager",
        )

    assert result == "Sell after earnings."
    structured_call, plain_call = generate.call_args_list
    assert len(structured_call.args[1]) == 2
    assert plain_call.args[1] == [HumanMessage(content=prompt)]


def test_structured_output_takes_one_gate_permit(bus, plan):
    gate = Mock(spec=["acquire", "release"])
    with patch.object(SwitchboardChatModel, "_generate", autospec=True) as generate:
        generate.return_value = _reply(plan.model_dump_json())
        result = GatedLLM(bus, gate).with_structured_output(ResearchPlan).invoke("Make a plan.")

    assert result == plan
    generate.assert_called_once()
    gate.acquire.assert_called_once_with(1)
    gate.release.assert_called_once_with(1)


def test_include_raw_is_unsupported(bus):
    with pytest.raises(NotImplementedError, match="include_raw"):
        bus.with_structured_output(ResearchPlan, include_raw=True)


@pytest.mark.filterwarnings("ignore:The `schema` method is deprecated")
def test_format_tools_preserves_langchain_tool_output():
    @tool
    def quote(symbol: str) -> str:
        """Return a quote."""
        return symbol

    assert _format_tools([quote]) == [{
        "name": "quote",
        "description": "Return a quote.",
        "input_schema": {
            "title": "quote",
            "description": "Return a quote.",
            "type": "object",
            "properties": {"symbol": {"title": "Symbol", "type": "string"}},
            "required": ["symbol"],
        },
    }]


def test_format_tools_converts_pydantic_schema_name_and_parameters():
    formatted, = _format_tools([ResearchPlan])

    assert formatted["name"] == "ResearchPlan"
    assert formatted["input_schema"]["type"] == "object"
    assert formatted["input_schema"]["required"] == ["recommendation", "rationale", "strategic_actions"]
    assert formatted["input_schema"]["properties"]["recommendation"]["enum"] == [
        "Buy", "Overweight", "Hold", "Underweight", "Sell",
    ]


def test_format_tools_preserves_parameters_in_a_json_schema_dict():
    schema = ResearchPlan.model_json_schema()
    formatted, = _format_tools([schema])

    assert formatted["name"] == "ResearchPlan"
    assert formatted["input_schema"] == {
        key: value for key, value in schema.items() if key not in {"title", "description"}
    }


def test_rendered_portfolio_rating_takes_precedence_over_earlier_rating_word():
    free_text = "After weighing the Buy case, I recommend Sell."
    decision = PortfolioDecision(
        rating="Sell", executive_summary="Reduce exposure.", investment_thesis=free_text,
    )

    assert parse_rating(free_text) == "Buy"
    # The structured rating is authoritative even when the thesis mentions Buy.
    assert parse_rating(render_pm_decision(decision)) == "Sell"
