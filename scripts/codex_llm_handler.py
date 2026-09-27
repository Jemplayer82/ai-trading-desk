#!/usr/bin/env python3
"""Codex LLM handler — ChatGPT-subscription bus daemon for TradingAgents.

The ChatGPT twin of Cleo (scripts/cleo_llm_handler.py). It registers on the
mcp-switchboard bus under its own agent id (default ``codex``), serves
``llm_request`` DMs by running the OpenAI Codex CLI headless

    codex exec --json --ephemeral --sandbox read-only --disable <tools...> -

against the machine's ``codex login`` (Sign in with ChatGPT) session — no
OpenAI API key, no per-token billing; usage counts against the ChatGPT plan.

Everything protocol-related is reused from Cleo's handler (bus calls, the
inline ``<tool_call>`` marker protocol, backpressure, single-instance lock),
so only the model call differs:

* Codex has no system-prompt flag and no multi-turn stdin format, so the
  system prompt and the conversation are rendered into ONE prompt.
* Codex is a coding agent. Every tool-ish feature (shell, exec, browser,
  computer use, apps, plugins) is disabled and it runs in an empty temporary
  directory with a read-only sandbox, so a prompt injected through market
  news cannot make it run commands or read files.
* ``codex exec --json`` emits the reply only when the turn completes, so a
  reply is streamed back to the desk as a single chunk.

Model names: ``chatgpt:astra|sol|luna|terra`` pick a model FAMILY and always
run its newest member (resolved from ``codex debug models``, cached an hour).
``chatgpt`` alone is the CLI's built-in default model. ``chatgpt:<model-id>``
pins one (Custom model ID in the dashboard).

Required env vars (same as Cleo):
  SWITCHBOARD_URL, SWITCHBOARD_MCP_TOKEN

Optional:
  SWITCHBOARD_AGENT_ID    Agent name to register as (default: codex)
  CODEX_BIN               Path to the codex CLI (default: codex)
  CODEX_CALL_TIMEOUT_S    Per-call hard deadline (default: 170, below the
                          desk's 180 s client timeout)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cleo_llm_handler as base  # noqa: E402  (sibling module, path set above)

log = base.log

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CODEX_CALL_TIMEOUT_S = float(os.environ.get("CODEX_CALL_TIMEOUT_S", "170"))
DEFAULT_MODEL = "chatgpt"

# Model names meaning "the Codex CLI's current default model" (always latest).
LATEST_ALIASES = frozenset({"", "chatgpt", "gpt", "codex", "latest"})

# Codex features that would let the model act rather than just answer.
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "browser_use", "browser_use_external",
    "computer_use", "in_app_browser", "plugins", "remote_plugin", "sleep_tool",
    "tool_suggest", "skill_mcp_dependency_install",
)

# Replaces Codex's own ~4k-token coding-agent instructions. Together with the
# disabled features and web search off this cuts the fixed per-call overhead
# from ~12.2k to ~5.6k input tokens (measured 2026-09-26, codex-cli 0.157.1).
# The desk's real system prompt still travels in the user prompt.
BASE_INSTRUCTIONS = (
    "You are a model answering requests for an automated equity-research "
    "pipeline. Answer the request directly and concisely. You have no shell, "
    "files, browser or web access.\n"
)

_ROLE_LABEL = {"user": "USER", "assistant": "ASSISTANT", "system": "SYSTEM", "tool": "TOOL RESULT"}


# ChatGPT model families offered in the desk's menus. The desk sends
# "chatgpt:<family>"; the handler resolves it at call time to the newest model
# of that family the Codex CLI lists (e.g. "sol" -> "gpt-6-sol", and "terra" ->
# "gpt-5.6-terra" until a GPT-6 Terra ships), so the menus never pin a version.
FAMILIES = ("astra", "sol", "luna", "terra")
# Used only if `codex debug models` cannot be read (verified 2026-09-27).
FALLBACK_FAMILY_MODELS = {
    "astra": "gpt-6-astra", "sol": "gpt-6-sol",
    "luna": "gpt-6-luna", "terra": "gpt-5.6-terra",
}
MODEL_LIST_TTL_S = float(os.environ.get("CODEX_MODEL_LIST_TTL_S", "3600"))
# After a failed/empty model-list load, use the fallback table for this long
# before trying `codex debug models` again (no 30 s subprocess per request).
MODEL_LIST_RETRY_S = float(os.environ.get("CODEX_MODEL_LIST_RETRY_S", "600"))
_SLUG_RE = re.compile(r"^gpt-(\d+(?:\.\d+)*)-([a-z]+)$")
_model_cache: dict = {"at": 0.0, "families": {}}
_model_cache_lock = threading.Lock()


def latest_by_family(models: list) -> dict:
    """{family: newest listed slug} from `codex debug models` entries."""
    best: dict = {}
    for m in models or []:
        if not isinstance(m, dict) or m.get("visibility", "list") != "list":
            continue
        match = _SLUG_RE.match(str(m.get("slug") or ""))
        if not match:
            continue
        version = tuple(int(x) for x in match.group(1).split("."))
        family = match.group(2)
        if family not in best or version > best[family][0]:
            best[family] = (version, m["slug"])
    return {fam: slug for fam, (_, slug) in best.items()}


def _load_family_models() -> dict:
    out = subprocess.run(
        [CODEX_BIN, "debug", "models"], capture_output=True, text=True, timeout=30,
    )
    start = out.stdout.find("{")
    data = json.loads(out.stdout[start:]) if start >= 0 else {}
    return latest_by_family(data.get("models") or [])


def resolve_family(family: str) -> str:
    """Newest model slug for a family, refreshed at most once per TTL."""
    now = time.monotonic()
    with _model_cache_lock:
        ttl = MODEL_LIST_TTL_S if _model_cache["families"] else MODEL_LIST_RETRY_S
        if _model_cache["at"] == 0.0 or now - _model_cache["at"] >= ttl:
            try:
                families = _load_family_models()
            except Exception as exc:  # the CLI's model list is best-effort
                families = {}
                log.warning("codex model list unavailable (%s) — using fallbacks for %.0fs",
                            exc, MODEL_LIST_RETRY_S)
            if not families and _model_cache["families"]:
                families = _model_cache["families"]  # keep the last good list
            _model_cache.update(at=now, families=families)
        families = _model_cache["families"]
    return families.get(family) or FALLBACK_FAMILY_MODELS[family]


def model_arg(model: str | None) -> str | None:
    """The ``-m`` value for a requested model, or None for the CLI default."""
    m = (model or "").strip()
    if m.lower().startswith("chatgpt:"):
        m = m.split(":", 1)[1].strip()
    if m.lower() in LATEST_ALIASES:
        return None
    if m.lower() in FAMILIES:
        return resolve_family(m.lower())
    return m


def build_prompt(system: str, messages: list, tools: list) -> str:
    """Render system prompt + conversation into Codex's single prompt."""
    system = base._augment_system_with_tools(system, tools)
    id_to_name = base._build_tool_id_map(messages)
    parts: list[str] = []
    if system:
        parts.append("=== INSTRUCTIONS ===\n" + system.strip())
    parts.append("=== CONVERSATION ===")
    for m in base._merge_consecutive(messages):
        role = _ROLE_LABEL.get(m.get("role", "user"), "USER")
        parts.append(f"[{role}]\n" + base._flatten_content(m.get("content", ""), id_to_name).strip())
    parts.append(
        "=== END ===\n"
        "Write the ASSISTANT's reply to the last USER message. Answer directly in "
        "plain text; you have no shell, files or browser. If the instructions above "
        "describe tool_call markers, use those markers to request data."
    )
    return "\n\n".join(parts)


def build_command(model: str | None, instructions_file: str | None = None) -> list[str]:
    cmd = [
        CODEX_BIN, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
        "--ignore-rules", "--ignore-user-config", "--sandbox", "read-only",
        "-c", 'web_search="disabled"',
    ]
    if instructions_file:
        cmd += ["-c", f"model_instructions_file={json.dumps(instructions_file)}"]
    for feature in DISABLED_FEATURES:
        cmd += ["--disable", feature]
    m = model_arg(model)
    if m:
        cmd += ["-m", m]
    cmd.append("-")  # prompt on stdin
    return cmd


def _error_text(obj: dict, default: str) -> str:
    """Human-readable error from a codex ``error`` / ``turn.failed`` event.

    Observed shapes: top-level ``message``; ``error`` as a dict with
    ``message`` (API errors, e.g. ``{"type":"error","status":400,
    "error":{"type":"invalid_request_error","message":"..."}}``); ``error`` as
    a plain string. The HTTP status is kept when present so quota (429) and
    auth (401) failures are distinguishable in the desk's failure alert.
    """
    err = obj.get("error")
    if isinstance(err, dict):
        text = err.get("message") or err.get("type")
    elif err is not None:
        text = str(err)
    else:
        text = obj.get("message")
    text = (text or "").strip() or default
    status = obj.get("status")
    return f"{text} (HTTP {status})" if status else text


def parse_events(lines) -> tuple[str, str | None]:
    """(reply text, error or None) from ``codex exec --json`` output lines."""
    texts: list[str] = []
    error: str | None = None
    completed = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = obj.get("type", "")
        if etype == "item.completed":
            item = obj.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                texts.append(item["text"])
        elif etype == "turn.completed":
            completed = True
        elif etype == "turn.failed":
            error = _error_text(obj, "codex turn failed")
        elif etype == "error":
            error = _error_text(obj, "codex error")
    if error is None and not completed:
        error = "codex exited without completing the turn"
    return "\n\n".join(texts), error


def call_codex_streaming(model: str, system: str, messages: list, tools: list, max_tokens: int):
    """Run one ``codex exec`` turn; yield the same chunks as Cleo's backend.

      {"delta": "text"}                    — the reply (tool markers stripped)
      {"done": True, "tool_calls": [...]}  — completion signal
    """
    prompt = build_prompt(system, messages, tools)
    workdir = tempfile.mkdtemp(prefix="codex-llm-")
    instructions = os.path.join(workdir, "instructions.md")
    with open(instructions, "w", encoding="utf-8") as fh:
        fh.write(BASE_INSTRUCTIONS)
    proc = subprocess.Popen(
        build_command(model, instructions),
        cwd=workdir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )

    def _write_stdin() -> None:
        try:
            proc.stdin.write(prompt)
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    stderr_tail: deque[str] = deque(maxlen=50)

    def _drain_stderr() -> None:
        try:
            for line in proc.stderr:
                stderr_tail.append(line)
        except (BrokenPipeError, ValueError):
            pass

    done_evt = threading.Event()
    timed_out = threading.Event()

    def _watchdog() -> None:
        if not done_evt.wait(CODEX_CALL_TIMEOUT_S):
            timed_out.set()
            log.warning("codex CLI exceeded %.0fs deadline — killing", CODEX_CALL_TIMEOUT_S)
            base._signal_group(proc, base._SIG_ESCALATION[-1])

    threading.Thread(target=_write_stdin, daemon=True).start()
    threading.Thread(target=_drain_stderr, daemon=True).start()
    threading.Thread(target=_watchdog, daemon=True).start()

    try:
        text, error = parse_events(proc.stdout)
    finally:
        done_evt.set()
        base._reap(proc)
        shutil.rmtree(workdir, ignore_errors=True)

    if timed_out.is_set():
        raise RuntimeError(f"codex CLI timed out after {CODEX_CALL_TIMEOUT_S:.0f}s")
    if error is not None:
        tail = "".join(stderr_tail).strip()[-400:]
        raise RuntimeError(f"codex CLI error: {error}" + (f" | {tail}" if tail else ""))

    flt = base._ToolMarkerFilter()
    visible = flt.feed(text) + flt.flush()
    if visible:
        yield {"delta": visible}
    yield {"done": True, "tool_calls": base._parse_tool_calls(flt.full_text)}


def main() -> None:
    base.main(
        backend=call_codex_streaming,
        display_name="Codex (ChatGPT daemon)",
        default_agent_id="codex",
        default_model=DEFAULT_MODEL,
    )


if __name__ == "__main__":
    main()
