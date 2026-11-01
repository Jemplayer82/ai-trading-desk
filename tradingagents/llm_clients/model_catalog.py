"""Shared model catalog for CLI selections and validation."""

from __future__ import annotations

from typing import Dict, List, Tuple

ModelOption = Tuple[str, str]
ProviderModeOptions = Dict[str, Dict[str, List[ModelOption]]]


# Shared model list for GLM via Z.AI (international) and BigModel (China).
# Source: docs.z.ai (GLM Coding Plan supported models + LLM guides).
# All GLM 4.7+ entries support thinking mode via thinking={"type":"enabled"}.
_GLM_MODELS: Dict[str, List[ModelOption]] = {
    "quick": [
        ("GLM-5.3 - Latest flagship, 1M ctx (reasoning always on; set effort low for speed)", "glm-5.3"),
        ("GLM-5-Turbo - Fast, switchable thinking modes", "glm-5-turbo"),
        ("Custom model ID", "custom"),
    ],
    "deep": [
        ("GLM-5.3 - Latest flagship, 1M ctx, 128K output", "glm-5.3"),
        ("GLM-5.1 - Previous-gen flagship, 204K ctx", "glm-5.1"),
        ("Custom model ID", "custom"),
    ],
}


# Shared model list for Qwen's global (dashscope-intl) and CN (dashscope) endpoints.
# Source: modelstudio.console.alibabacloud.com (Featured Models — Flagship + Cost-optimized).
#
# Only versioned IDs are exposed in the dropdown. The version-less aliases
# (qwen-plus, qwen-flash) are documented by Alibaba as auto-upgrading
# pointers ("backbone, latest, and snapshot ... have been upgraded to the
# Qwen3 series"), which means their behavior shifts when Alibaba rotates
# the backing model. Users who want a specific generation pick it
# explicitly; users who really want auto-latest can enter the alias via
# "Custom model ID".
_QWEN_MODELS: Dict[str, List[ModelOption]] = {
    "quick": [
        ("Qwen 3.8 Flash - Latest fast and cost-effective", "qwen3.8-flash"),
        ("Custom model ID", "custom"),
    ],
    "deep": [
        ("Qwen 3.8 Max - Latest flagship, complex reasoning and coding", "qwen3.8-max"),
        ("Qwen 3.7 Plus - Balanced performance, speed and cost", "qwen3.7-plus"),
        ("Custom model ID", "custom"),
    ],
}


# Shared model list for MiniMax's global and CN endpoints (same IDs).
# Source: platform.minimax.io/docs/guides/text-generation (checked 11/01/2026: M3.1-Flash is the latest,
# M3 remains available; M2.x models have a 204,800-token context window).
_MINIMAX_MODELS: Dict[str, List[ModelOption]] = {
    "quick": [
        ("MiniMax-M3.1-Flash - Latest M-series, 1M ctx", "MiniMax-M3.1-Flash"),
        ("MiniMax-M2.7-highspeed - Faster M2.7, 204K ctx, ~100 TPS", "MiniMax-M2.7-highspeed"),
        ("Custom model ID", "custom"),
    ],
    "deep": [
        ("MiniMax-M3.1-Flash - Latest M-series, 1M ctx", "MiniMax-M3.1-Flash"),
        ("MiniMax-M3 - Flagship, 1M ctx", "MiniMax-M3"),
        ("MiniMax-M2.7 - Previous-gen flagship, 204K ctx", "MiniMax-M2.7"),
        ("Custom model ID", "custom"),
    ],
}


MODEL_OPTIONS: ProviderModeOptions = {
    "openai": {
        "quick": [
            ("GPT-6 Luna - Latest fast model", "gpt-6-luna"),
            ("GPT-6.1 Terra - Latest balanced model", "gpt-6.1-terra"),
        ],
        "deep": [
            ("GPT-6.1 Sol - Latest workhorse", "gpt-6.1-sol"),
            ("GPT-6 Astra - Latest frontier, most capable, highest cost", "gpt-6-astra"),
            ("GPT-6.1 Terra - Latest balanced model", "gpt-6.1-terra"),
        ],
    },
    # OpenAI. Source: the "Flagship models" cards of https://developers.openai.com/api/docs/models,
    # checked 11/01/2026 (no OpenAI key on the desk, so these IDs are NOT smoke-tested). The same four
    # families are what the ChatGPT-plan route (switchboard "chatgpt:<family>") resolves to.
    #
    # Anthropic. Source: the "Claude API alias" row of
    # https://platform.claude.com/docs/en/about-claude/models/overview.md
    #
    # From the 4.6 generation on, those dateless IDs are pinned snapshots, not
    # evergreen pointers — a stale entry here quietly runs an older model rather
    # than 404ing, so drift is silent. Check it (free, no API key needed) with:
    #
    #     python scripts/check_model_catalog.py
    #
    # There is no Sonnet 4.8: the Sonnet line runs 4.5 → 4.6 → 5, and only Opus
    # has a 4.8. Claude Fable 5 is deliberately absent — it is double Opus
    # pricing, thinking is always on (an explicit thinking=disabled is a 400),
    # it requires 30-day data retention, and it can answer with
    # stop_reason="refusal". AnthropicClient handles none of those yet.
    #
    # No always-latest entry is possible HERE. Anthropic retired evergreen
    # pointers with the 4.6 generation, so there is no `claude-opus-latest` and
    # `claude-opus-5-5` will never become Opus 6; a bare `opus` sent to the REST
    # API is a 404. The always-latest names live under "switchboard" below,
    # where the Claude CLI resolves them for us.
    #
    # Last verified 2026-11-01: the four IDs below were each called through AnthropicClient
    # (Opus 5.5 and Sonnet 5.5 with the effort setting) and the account's /v1/models list shows no newer ones.
    "anthropic": {
        "quick": [
            ("Claude Sonnet 5.5 - Latest, best speed and intelligence balance", "claude-sonnet-5-5"),
            ("Claude Haiku 5.5 - Latest, fastest", "claude-haiku-5-5"),
        ],
        "deep": [
            ("Claude Opus 5.5 - Latest frontier, complex agentic and enterprise work", "claude-opus-5-5"),
            ("Claude Sonnet 5.5 - Latest, near-Opus quality at Sonnet cost", "claude-sonnet-5-5"),
        ],
    },
    # Google. Source: https://ai.google.dev/gemini-api/docs/models, checked 11/01/2026 (no Google key on
    # the desk, so NOT smoke-tested). gemini-4-argon went GA 10/13/2026; its introductory price rises after 11/30/2026.
    "google": {
        "quick": [
            ("Gemini 3.8 Flash - Latest fast model (stable)", "gemini-3.8-flash"),
            ("Gemini 3.5 Flash Lite - Latest, most cost-efficient (stable)", "gemini-3.5-flash-lite"),
        ],
        "deep": [
            ("Gemini 4 Argon - Latest flagship reasoning model (stable)", "gemini-4-argon"),
            ("Gemini 3.1 Pro - Newest Pro, complex workflows (preview)", "gemini-3.1-pro-preview"),
            ("Gemini 3.8 Flash - Latest fast model (stable)", "gemini-3.8-flash"),
        ],
    },
    # xAI. Source: https://docs.x.ai/docs/models, checked 11/01/2026 (no xAI key on the desk, NOT smoke-tested).
    "xai": {
        "quick": [
            ("Grok 4.20 (Non-Reasoning) - Newest non-reasoning model listed", "grok-4.20-0309-non-reasoning"),
            ("Grok 4.3 - 1M context", "grok-4.3"),
        ],
        "deep": [
            ("Grok 4.7 - Latest, most capable, reasoning, 500K ctx", "grok-4.7"),
            ("Grok 4.6 - Previous-gen, 500K ctx", "grok-4.6"),
            ("Grok 4.20 (Reasoning) - 1M ctx", "grok-4.20-0309-reasoning"),
        ],
    },
    # DeepSeek. Source: https://api-docs.deepseek.com/quick_start/pricing, checked 11/01/2026 (NOT smoke-tested,
    # no DeepSeek key on the desk). The page serves only deepseek-flash (V4.1) and deepseek-v4-pro; the old
    # deepseek-chat / deepseek-reasoner / deepseek-v4-flash names are no longer listed.
    "deepseek": {
        "quick": [
            ("DeepSeek V4.1 Flash - Latest fast model", "deepseek-flash"),
            ("Custom model ID", "custom"),
        ],
        "deep": [
            ("DeepSeek V4 Pro - Latest flagship model", "deepseek-v4-pro"),
            ("DeepSeek V4.1 Flash - Latest fast model", "deepseek-flash"),
            ("Custom model ID", "custom"),
        ],
    },
    # Qwen: same model IDs across global (dashscope-intl) and China
    # (dashscope) endpoints, so the two provider keys share one model list.
    "qwen": _QWEN_MODELS,
    "qwen-cn": _QWEN_MODELS,
    # GLM: Z.AI (international) and BigModel (China) host the same model
    # IDs; the two provider keys share one model list.
    "glm": _GLM_MODELS,
    "glm-cn": _GLM_MODELS,
    # MiniMax: same model IDs across global (.io) and China (.com) regions,
    # so the two provider keys share one model list.
    "minimax": _MINIMAX_MODELS,
    "minimax-cn": _MINIMAX_MODELS,
    # OpenRouter: fetched dynamically. Azure: any deployed model name.
    # Ollama display labels intentionally omit a "local" marker — the
    # endpoint is now configurable via OLLAMA_BASE_URL, so the same labels
    # apply whether the user runs ollama-serve on localhost or against a
    # remote host. The actual resolved endpoint is surfaced separately by
    # cli.utils.confirm_ollama_endpoint() right after provider selection.
    # "Custom model ID" lets users pick any model they have pulled via
    # `ollama pull` beyond the three suggested defaults.
    # Revalidate against the live list before editing — Ollama Cloud retires
    # tags without notice and a stale entry here is a hard 404 at scan time,
    # not a graceful fallback (the previous qwen3:latest / gpt-oss:latest /
    # glm-4.7-flash:latest set all went dead this way). To refresh:
    #   curl -sH "Authorization: Bearer $OLLAMA_API_KEY" \
    #        https://ollama.com/v1/models | jq -r '.data[].id' | sort
    # Last verified 2026-11-01 (every id below smoke-tested with a live
    # chat.completions call, not just presence in the list). qwen3.5:397b and
    # deepseek-v4-flash were retired 09/25/2026 (HTTP 410) and are gone from this list.
    "ollama": {
        "quick": [
            ("GLM-5.3 Flash", "glm-5.3-flash"),
            ("DeepSeek V4.1 Flash", "deepseek-v4.1-flash"),
            ("MiniMax M3.1 Flash", "minimax-m3.1-flash"),
            ("GPT-OSS 20B", "gpt-oss:20b"),
            ("Custom model ID", "custom"),
        ],
        "deep": [
            ("Kimi K3", "kimi-k3"),
            ("GLM-5.3", "glm-5.3"),
            ("DeepSeek V4 Pro", "deepseek-v4-pro:0813"),
            ("MiniMax M3", "minimax-m3"),
            ("Mistral Large 4", "mistral-large-4"),
            ("Nemotron 3 Ultra", "nemotron-3-ultra"),
            ("GPT-OSS 120B", "gpt-oss:120b"),
            ("Custom model ID", "custom"),
        ],
    },
    # Switchboard reaches Claude through Cleo, which shells out to `claude -p`
    # against the host user's CLI subscription — so these cost nothing per token
    # and are the route the deployed stack actually uses. Values are handed to
    # `claude --model` verbatim; non-Claude entries fall through to llm-router.
    #
    # THIS is the one provider where an always-latest name genuinely exists,
    # and since 2026-09-26 it offers ONLY those names (Landon: "get rid of the
    # pinned and just make it where it runs the latest version of each model").
    # The Claude CLI resolves the bare family names at call time (verified on
    # WebServer, CLI 2.1.283, 2026-09-26):
    #
    #     opus   -> claude-opus-5-5
    #     sonnet -> claude-sonnet-5   (as verified 2026-09-26; the newest Sonnet is now 5.5 and the CLI
    #                                  should resolve to it, NOT re-verified: the host's Claude login is out)
    #     fable  -> claude-fable-5-1   (deep only: thinking is always on, ~24 s
    #                                   for a one-word reply — too slow for the
    #                                   quick/analyst path, never the default)
    #     haiku  -> claude-haiku-4-5-20251001  (resolves fine; not offered — see below)
    #
    # The first entry of each list is the default for background scans —
    # web/runner.py::_catalog_default takes the first non-"custom" entry.
    # To pin a specific snapshot anyway, type its ID into "Custom model ID".
    #
    # "chatgpt:<family>" entries are answered by a different bus agent: the
    # Codex handler (scripts/codex_llm_handler.py, agent id
    # SWITCHBOARD_CHATGPT_AGENT, default "codex"), which runs `codex exec` on the
    # host's ChatGPT sign-in. Each family (Astra frontier, Sol workhorse, Luna
    # fast, Terra balanced) resolves at call time to its newest model, so these
    # never pin a version (11/01/2026, read from `codex debug models` on the host:
    # gpt-6-astra, gpt-6.1-sol, gpt-6-luna, gpt-6.1-terra). Pin one with Custom model ID "chatgpt:<model-id>". Routing
    # is per model, so the quick and deep roles can use different handlers.
    #
    # The tradeoff: an always-latest name means the model under your analysis
    # changes the day Anthropic ships the next one, with no commit and no
    # warning. That is what these menus now choose deliberately; anything that
    # must stay comparable over time (e.g. the options reflection corpus in
    # web/options_learning.py) should pin a snapshot via Custom model ID.
    # ⚠️ TOOL-CALL RELIABILITY — two bugs fixed here 2026-08-06, both worth
    # knowing about before trusting a model choice on this provider.
    #
    # (1) The Claude CLI cannot accept Anthropic tool schemas, so Cleo teaches
    # the model an inline marker syntax instead (scripts/cleo_llm_handler.py).
    # Its parser used to accept only one dialect, so a near-miss (single
    # quotes, reordered attributes, the common {"name":..,"arguments":..}
    # envelope) had its call silently dropped AND its text suppressed — a
    # 0-character report, no error. The parser now accepts every observed
    # dialect and logs loudly when it still cannot read a marker.
    #
    # (2) That alone did NOT fix "blank market/news/fundamentals reports" —
    # the deeper bug was in the analyst nodes themselves
    # (tradingagents/agents/analysts/*.py), which treat "reply has zero
    # tool_calls" as the ONLY signal that a report is final. That can't
    # distinguish a genuine final report from a model narrating intent on its
    # first turn without ever calling anything ("I have made tool calls...
    # awaiting results"). Fixed via invoke_with_tool_call_retry() in
    # agents/utils/agent_utils.py: one corrective retry, only when nothing has
    # been fetched yet this round.
    #
    # NOTE: an earlier version of this comment carried a 30-day zero-tool-call
    # rate table per model, used to justify removing/re-adding Haiku from this
    # list. Those numbers conflated legitimate final-report turns (0 calls
    # because the analyst is done) with real failures (0 calls because nothing
    # was ever fetched) and were never a valid failure rate — deleted rather
    # than left to mislead. If you want real per-model reliability data, grade
    # it on whether the STORED REPORT is a stub (see the retry-guard docstring
    # for the stub signature), not on raw tool-call counts from the Cleo log.
    #
    # ⚠️ HAIKU IS DELIBERATELY NOT OFFERED HERE (removed 2026-08-06).
    #
    # With the retry-guard fix above in place — which correctly detects a
    # stall, sends a corrective nudge, retries once, and logs loudly if it
    # still fails — Haiku was live-tested (SPCX, market/news/fundamentals run
    # individually against production Cleo, no shortcuts) and stalled through
    # BOTH the original attempt and the retry on all three analysts in that
    # run. One example, verbatim, after being explicitly told the data was
    # already in its own context: "I'm ready to continue, but I haven't yet
    # received the tool results... Could you please provide the tool
    # results?" — a question with no one to answer it, in an unattended
    # pipeline.
    #
    # This is not a code bug. Cleo can't hand the CLI a real tool schema, so
    # it teaches the marker syntax in prose (scripts/cleo_llm_handler.py) with
    # nothing validating the model's output, and Haiku's grasp of that
    # improvised protocol is unreliable enough that a single bounded retry —
    # the most you can add without risking runaway retry loops on a model that
    # just won't comply — regularly is not enough. Sonnet and Opus did not
    # show this failure mode in the same live testing.
    #
    # Re-add only after re-verifying against the STORED REPORT TEXT (not tool-
    # call counts, not report length — both looked fine here and were wrong
    # twice this same day) on a fresh Cleo/model-catalog build. See
    # README.md's "Claude via Switchboard" section for the same explanation
    # aimed at operators, not code readers.
    "switchboard": {
        "quick": [
            ("Sonnet — latest (via bus)", "sonnet"),
            ("Opus — latest, slower (via bus)", "opus"),
            ("ChatGPT Sol — latest, workhorse (via bus, ChatGPT plan)", "chatgpt:sol"),
            ("ChatGPT Luna — latest, fast (via bus, ChatGPT plan)", "chatgpt:luna"),
            ("ChatGPT Terra — latest, balanced (via bus, ChatGPT plan)", "chatgpt:terra"),
            ("ChatGPT Astra — latest, frontier (via bus, ChatGPT plan)", "chatgpt:astra"),
            ("Llama 3 (via bus)", "llama3"),
            ("Custom model ID", "custom"),
        ],
        "deep": [
            ("Opus — latest (via bus)", "opus"),
            ("Fable — latest, most capable, slowest (via bus)", "fable"),
            ("Sonnet — latest, faster (via bus)", "sonnet"),
            ("ChatGPT Astra — latest, frontier (via bus, ChatGPT plan)", "chatgpt:astra"),
            ("ChatGPT Sol — latest, workhorse (via bus, ChatGPT plan)", "chatgpt:sol"),
            ("ChatGPT Terra — latest, balanced (via bus, ChatGPT plan)", "chatgpt:terra"),
            ("ChatGPT Luna — latest, fast (via bus, ChatGPT plan)", "chatgpt:luna"),
            ("Custom model ID", "custom"),
        ],
    },
}


def get_model_options(provider: str, mode: str) -> List[ModelOption]:
    """Return shared model options for a provider and selection mode."""
    return MODEL_OPTIONS[provider.lower()][mode]


def get_known_models() -> Dict[str, List[str]]:
    """Build known model names from the shared CLI catalog."""
    return {
        provider: sorted(
            {
                value
                for options in mode_options.values()
                for _, value in options
            }
        )
        for provider, mode_options in MODEL_OPTIONS.items()
    }
