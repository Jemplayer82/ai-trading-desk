"""Tests for scripts/codex_llm_handler.py — the ChatGPT (Codex CLI) bus handler.

The real call_codex_streaming is driven with a fake ``codex`` binary that
speaks just enough ``codex exec --json`` for each scenario, so subprocess
handling, event parsing and the watchdog are exercised, not assumed.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_HANDLER = _REPO / "scripts" / "codex_llm_handler.py"

_FAKE = r'''
import json, os, sys, time
prompt = sys.stdin.read()
open(os.environ["FAKE_PROMPT_FILE"], "w").write(prompt)
open(os.environ["FAKE_ARGS_FILE"], "w").write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}))
s = os.environ["FAKE_SCENARIO"]
def emit(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
emit({"type": "thread.started", "thread_id": "t"})
emit({"type": "turn.started"})
if s == "ok":
    emit({"type": "item.completed", "item": {"type": "reasoning", "text": "thinking"}})
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "Rating: Buy"}})
    emit({"type": "turn.completed", "usage": {}})
elif s == "tool":
    emit({"type": "item.completed", "item": {"type": "agent_message", "text":
          'Let me check.<tool_call name="get_stock_data">{"symbol": "AAPL"}</tool_call>'}})
    emit({"type": "turn.completed", "usage": {}})
elif s == "failed":
    emit({"type": "turn.failed", "error": {"message": "usage limit reached"}})
elif s == "early_exit":
    sys.stderr.write("boom on stderr\n"); sys.exit(2)
elif s == "hang":
    while True:
        time.sleep(0.5)
'''


@pytest.fixture
def codex():
    spec = importlib.util.spec_from_file_location("codex_llm_handler_under_test", _HANDLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake(tmp_path, codex, monkeypatch):
    path = tmp_path / "fake_codex.py"
    path.write_text(_FAKE, encoding="utf-8")
    files = {"prompt": tmp_path / "prompt.txt", "args": tmp_path / "args.json"}
    orig = subprocess.Popen

    def use(scenario):
        def _popen(cmd, **kwargs):
            env = dict(os.environ, FAKE_SCENARIO=scenario,
                       FAKE_PROMPT_FILE=str(files["prompt"]), FAKE_ARGS_FILE=str(files["args"]))
            return orig([sys.executable, str(path), *cmd[1:]], env=env, **kwargs)
        monkeypatch.setattr(codex.subprocess, "Popen", _popen)
        return files

    return use


def _run(codex, model="chatgpt", timeout=20):
    out = {"chunks": [], "error": None}

    def go():
        try:
            for ch in codex.call_codex_streaming(
                model, "You are an analyst.",
                [{"role": "user", "content": "Rate AAPL"},
                 {"role": "assistant", "content": "Working"},
                 {"role": "user", "content": "Final answer please"}],
                [], 8192,
            ):
                out["chunks"].append(ch)
        except Exception as exc:
            out["error"] = exc

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    return out["chunks"], out["error"], t.is_alive()


def test_success_yields_reply_then_done(codex, fake):
    files = fake("ok")
    chunks, err, hung = _run(codex)
    assert not hung and err is None
    assert chunks == [{"delta": "Rating: Buy"}, {"done": True, "tool_calls": []}]
    prompt = files["prompt"].read_text()
    assert "You are an analyst." in prompt
    assert prompt.index("[USER]\nRate AAPL") < prompt.index("[ASSISTANT]\nWorking") < prompt.index("[USER]\nFinal answer please")


def test_runs_locked_down_in_a_throwaway_dir(codex, fake):
    files = fake("ok")
    _run(codex)
    import json
    rec = json.loads(files["args"].read_text())
    argv = rec["argv"]
    assert argv[:2] == ["exec", "--json"]
    for flag in ("--ephemeral", "--ignore-rules", "--ignore-user-config", "--skip-git-repo-check"):
        assert flag in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
    assert {"shell_tool", "unified_exec", "browser_use", "computer_use", "plugins"} <= disabled
    assert "-m" not in argv and argv[-1] == "-"
    assert "codex-llm-" in rec["cwd"]
    assert not Path(rec["cwd"]).exists()  # removed after the call


def test_tool_call_marker_is_parsed_and_hidden(codex, fake):
    fake("tool")
    chunks, err, _ = _run(codex)
    assert err is None
    assert chunks[0] == {"delta": "Let me check."}
    (call,) = chunks[-1]["tool_calls"]
    assert call["name"] == "get_stock_data"


@pytest.mark.parametrize("scenario,needle", [
    ("failed", "usage limit reached"),
    ("early_exit", "without completing"),
])
def test_failures_raise_with_the_reason(codex, fake, scenario, needle):
    fake(scenario)
    chunks, err, hung = _run(codex)
    assert not hung
    assert chunks == []
    assert isinstance(err, RuntimeError) and needle in str(err)


def test_hung_codex_is_killed_by_the_watchdog(codex, fake, monkeypatch):
    fake("hang")
    monkeypatch.setattr(codex, "CODEX_CALL_TIMEOUT_S", 1.0)
    chunks, err, hung = _run(codex, timeout=15)
    assert not hung
    assert isinstance(err, RuntimeError) and "timed out" in str(err)


@pytest.mark.parametrize("model,expected", [
    ("chatgpt", None), ("", None), ("GPT", None), ("latest", None),
    ("chatgpt:gpt-6-astra", "gpt-6-astra"), ("gpt-6-astra", "gpt-6-astra"),
])
def test_model_arg(codex, model, expected):
    assert codex.model_arg(model) == expected
    cmd = codex.build_command(model)
    assert (cmd[cmd.index("-m") + 1] if "-m" in cmd else None) == expected


def test_main_registers_as_codex_with_chatgpt_default(codex, monkeypatch):
    seen = {}
    monkeypatch.setattr(codex.base, "main", lambda **kw: seen.update(kw))
    codex.main()
    assert seen["default_agent_id"] == "codex"
    assert seen["default_model"] == "chatgpt"
    assert seen["backend"] is codex.call_codex_streaming
