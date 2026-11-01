"""Static reference data the frontend uses to populate dropdowns.

Mirrors the choices in cli/utils.py so the web form offers the same
provider and model menu as the interactive CLI — with one override:
when OLLAMA_BASE_URL points to Ollama Cloud, we surface the cloud
model names (suffixed `-cloud`) instead of the local-Ollama tags.
"""

from __future__ import annotations

from typing import Any

from tradingagents.llm_clients.defaults import resolve_ollama_base_url
from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS


def _is_ollama_cloud(url: str | None) -> bool:
    return bool(url) and "ollama.com" in url


# Ollama Cloud catalog — names that actually resolve at https://ollama.com/v1.
# Order matters: the first entry is the default in the dropdown for a fresh
# session (no saved preferences yet) AND the fallback build_config uses when no
# model is saved (web/runner.py _catalog_default). We default to a deliberate
# mixed pair — gpt-oss for quick + deepseek for deep — to keep the analyst and
# decision-maker stages on different model lineages.
#
# ⚠️ Ollama Cloud RETIRES models, and a retired name returns HTTP 410, not 404.
# In July 2026 four entries here (kimi-k2:1t-cloud, glm-4.6:cloud,
# deepseek-v3.1:671b-cloud, qwen3-coder:480b-cloud) had gone dead while still
# being offered in the dropdown — including the deep default. Revalidate with:
#
#     curl -H "Authorization: Bearer $OLLAMA_API_KEY" https://ollama.com/v1/models
#
# Presence in that list is NOT the test, and treating it as one produces false
# alarms: as of 2026-08-06 `/v1/models` no longer lists any `-cloud` suffixed
# name, yet `gpt-oss:20b-cloud` and `gpt-oss:120b-cloud` both still answer
# chat.completions with HTTP 200. The only reliable check is a real call:
#
#     curl -s -o /dev/null -w '%{http_code}\n' https://ollama.com/v1/chat/completions \
#       -H "Authorization: Bearer $OLLAMA_API_KEY" -H 'Content-Type: application/json' \
#       -d '{"model":"<id>","messages":[{"role":"user","content":"hi"}],"max_tokens":3}'
#
# Entries are also verified to support tool calling, which the analysts require
# (they bind get_stock_data / get_indicators — a model without tool support
# fails the whole graph).
# Last verified: 2026-11-01 (every entry below answered a live chat call AND returned a tool call).
# qwen3.5:397b and deepseek-v4-flash were retired 09/25/2026 (HTTP 410) and are gone.
_OLLAMA_CLOUD_MODELS = {
    "quick": [
        ("DeepSeek V4.1 Flash — default", "deepseek-v4.1-flash"),
        ("GLM-5.3 Flash", "glm-5.3-flash"),
        ("MiniMax M3.1 Flash", "minimax-m3.1-flash"),
        ("GPT-OSS 20B", "gpt-oss:20b"),
        ("Nemotron 3 Nano 30B", "nemotron-3-nano:30b"),
        ("Custom model ID", "custom"),
    ],
    "deep": [
        ("DeepSeek V4 Pro — default", "deepseek-v4-pro"),
        ("Kimi K3", "kimi-k3"),
        ("GLM-5.3", "glm-5.3"),
        ("MiniMax M3", "minimax-m3"),
        ("Mistral Large 4", "mistral-large-4"),
        ("Nemotron 3 Ultra", "nemotron-3-ultra"),
        ("GPT-OSS 120B", "gpt-oss:120b"),
        ("Custom model ID", "custom"),
    ],
}


PROVIDERS = [
    {"key": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1"},
    {"key": "google", "label": "Google", "base_url": None},
    {"key": "anthropic", "label": "Anthropic", "base_url": "https://api.anthropic.com/"},
    {"key": "xai", "label": "xAI", "base_url": "https://api.x.ai/v1"},
    {"key": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com"},
    {"key": "qwen", "label": "Qwen (Global)", "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
    {"key": "qwen-cn", "label": "Qwen (China)", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"key": "glm", "label": "GLM (Z.AI)", "base_url": "https://api.z.ai/api/paas/v4/"},
    {"key": "glm-cn", "label": "GLM (BigModel)", "base_url": "https://open.bigmodel.cn/api/paas/v4/"},
    {"key": "minimax", "label": "MiniMax (Global)", "base_url": "https://api.minimax.io/v1"},
    {"key": "minimax-cn", "label": "MiniMax (China)", "base_url": "https://api.minimaxi.com/v1"},
    {"key": "ollama", "label": "Ollama", "base_url": None},
    {"key": "switchboard", "label": "Switchboard (Bus LLM)", "base_url": None},
]


def _ollama_models() -> dict[str, list[tuple[str, str]]]:
    url = resolve_ollama_base_url()
    if _is_ollama_cloud(url):
        return _OLLAMA_CLOUD_MODELS
    return MODEL_OPTIONS.get("ollama", {"quick": [], "deep": []})


def get_providers() -> list[dict[str, Any]]:
    """Provider entries plus their quick/deep model menus, shaped for the UI.

    Resolved at call time (per request), so changing OLLAMA_BASE_URL via the
    Settings UI flips the Ollama menu between local tags and the cloud
    catalog without a restart.
    """
    out = []
    for p in PROVIDERS:
        entry = dict(p)
        if entry["key"] == "ollama":
            entry["base_url"] = resolve_ollama_base_url()
            models = _ollama_models()
        else:
            models = MODEL_OPTIONS.get(entry["key"], {})
        entry["models"] = {
            "quick": [{"label": display, "value": value} for display, value in models.get("quick", [])],
            "deep": [{"label": display, "value": value} for display, value in models.get("deep", [])],
        }
        out.append(entry)
    return out


# Keys must match web/runner.py ALL_ANALYSTS (the AnalystType enum values).
ANALYSTS = [
    {"key": "market", "label": "Market Analyst"},
    {"key": "social", "label": "Sentiment Analyst"},
    {"key": "news", "label": "News Analyst"},
    {"key": "fundamentals", "label": "Fundamentals Analyst"},
]


LANGUAGES = [
    "English", "Chinese", "Japanese", "Korean", "Hindi",
    "Spanish", "Portuguese", "French", "German", "Arabic", "Russian",
]


DEPTH_PRESETS = [
    {"value": 1, "label": "Shallow — quick"},
    {"value": 3, "label": "Medium — balanced"},
    {"value": 5, "label": "Deep — comprehensive"},
]
