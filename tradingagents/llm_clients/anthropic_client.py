import re
from inspect import signature
from typing import Any, Optional

from langchain_anthropic import ChatAnthropic

from .base_client import BaseLLMClient, normalize_content
from .validators import validate_model

_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "api_key", "max_tokens",
    "callbacks", "http_client", "http_async_client", "effort",
)

# Mirrors LangChain's list of model prefixes that cannot force a tool call.
_NO_FORCED_TOOL_PREFIXES = ("claude-opus-5-5", "claude-fable-5-1")

# Anthropic's ``effort`` parameter is accepted by the Opus, Sonnet, Fable and
# Mythos families from Opus 4.5 / Sonnet 4.6 onward. Haiku (any version shipped
# to date) 400s with ``"This model does not support the effort parameter"``
# (#831), and so does Sonnet 4.5 — it predates effort even though its Opus 4.5
# contemporary supports it, which is why it needs a carve-out by name.
_EFFORT_EXACT = {
    "claude-mythos-preview",  # non-standard preview name; effort-capable
}
_EFFORT_EXCLUDED = {
    "claude-sonnet-4-5",
    "claude-sonnet-4-5-20250929",
}
# Covers both ID shapes: the dateless two-segment snapshots used from the 4.6
# generation on (``claude-opus-4-8``) and the single-segment 5-series IDs
# (``claude-opus-5``, ``claude-sonnet-5``, ``claude-fable-5``). Haiku is absent
# deliberately, so future Haiku releases stay excluded by default. Dated IDs
# (three-plus segments) also fall through to "no effort" — conservative, since
# a missing effort is a silent default while a rejected one is a hard 400.
_EFFORT_PATTERN = re.compile(r"^claude-(opus|sonnet|fable|mythos)-\d+(-\d+)?$")


def _supports_effort(model: str) -> bool:
    """Whether Anthropic accepts the ``effort`` parameter for this model."""
    model_lc = model.lower()
    if model_lc in _EFFORT_EXCLUDED:
        return False
    return model_lc in _EFFORT_EXACT or bool(_EFFORT_PATTERN.match(model_lc))


class NormalizedChatAnthropic(ChatAnthropic):
    """ChatAnthropic with normalized content output.

    Claude models with extended thinking or tool use return content as a
    list of typed blocks. This normalizes to string for consistent
    downstream handling.
    """

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(super().invoke(input, config, **kwargs))

    def with_structured_output(self, schema, *, method=None, **kwargs):
        if method is None:
            thinking = self.thinking
            if self.model.startswith(_NO_FORCED_TOOL_PREFIXES) or (
                isinstance(thinking, dict)
                and thinking.get("type") in ("enabled", "adaptive")
            ):
                method = "json_schema"

        bind = super().with_structured_output
        if method is None:
            return bind(schema, **kwargs)
        if method == "json_schema" and "method" not in signature(bind).parameters:
            # Older LangChain releases ignore extra kwargs and bind a forced tool.
            raise NotImplementedError("This version of langchain-anthropic lacks json_schema support")
        return bind(schema, method=method, **kwargs)


class AnthropicClient(BaseLLMClient):
    """Client for Anthropic Claude models."""

    def __init__(self, model: str, base_url: Optional[str] = None, **kwargs):
        super().__init__(model, base_url, **kwargs)

    def get_llm(self) -> Any:
        """Return configured ChatAnthropic instance."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}

        if self.base_url:
            llm_kwargs["base_url"] = self.base_url

        for key in _PASSTHROUGH_KWARGS:
            if key not in self.kwargs:
                continue
            if key == "effort" and not _supports_effort(self.model):
                continue
            llm_kwargs[key] = self.kwargs[key]

        return NormalizedChatAnthropic(**llm_kwargs)

    def validate_model(self) -> bool:
        """Validate model for Anthropic."""
        return validate_model("anthropic", self.model)
