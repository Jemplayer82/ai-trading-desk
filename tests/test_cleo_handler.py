"""Regression tests for scripts/cleo_llm_handler.py call_claude_streaming.

These guard the subprocess-handling fix: ``claude -p --verbose`` is chatty on
stderr, and the original code never drained it. Past ~64KB the OS pipe buffer
fills, the CLI blocks writing stderr, stops producing stdout, and the reader
hangs forever (the "works on one request, fails the next" bug). We now drain
stderr on a thread and enforce a per-call deadline with a watchdog kill.

The tests drive the REAL call_claude_streaming with a fake 'claude' binary that
reproduces each failure condition, so the protections are verified, not assumed.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_HANDLER = _REPO / "scripts" / "cleo_llm_handler.py"

# A fake 'claude' speaking just enough stream-json for the handler. The scenario
# is chosen via the FAKE_SCENARIO env var the test sets on the subprocess.
_FAKE = r'''
import sys, time, threading, os
def _drain():
    try:
        for _ in sys.stdin:
            pass
    except Exception:
        pass
threading.Thread(target=_drain, daemon=True).start()
scenario = os.environ["FAKE_SCENARIO"]
if scenario == "stderr_flood":
    # ~300KB of stderr BEFORE any stdout. Old code deadlocks at the 64KB buffer.
    blob = "x" * 1000
    for _ in range(300):
        sys.stderr.write(blob + "\n")
    sys.stderr.flush()
    sys.stdout.write('{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"hello world"}}}\n')
    sys.stdout.write('{"type":"result","is_error":false,"result":"ok"}\n')
    sys.stdout.flush()
    sys.exit(0)
elif scenario == "hang":
    # Never emit a result, never exit; the watchdog must kill us.
    while True:
        time.sleep(0.5)
elif scenario in ("grandchild_ok", "grandchild_hang"):
    # Spawn a long-lived child, exactly as the real claude CLI does, and record
    # its pid so the test can check whether it outlived us.
    import subprocess as _sp
    kid = _sp.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    open(os.environ["FAKE_KID_FILE"], "w").write(str(kid.pid))
    if scenario == "grandchild_ok":
        # The happy path: emit a clean result, then linger. The handler breaks
        # out of its read loop on the result event while we are still running,
        # which is precisely when the old code SIGKILLed us and stranded the kid.
        sys.stdout.write('{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}}\n')
        sys.stdout.write('{"type":"result","is_error":false,"result":"ok"}\n')
        sys.stdout.flush()
    time.sleep(300)
'''


@pytest.fixture
def cleo():
    spec = importlib.util.spec_from_file_location("cleo_llm_handler_under_test", _HANDLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_claude(tmp_path):
    path = tmp_path / "fake_claude.py"
    path.write_text(_FAKE, encoding="utf-8")
    return path


def _patch_popen(cleo, fake_claude, scenario, monkeypatch, kid_file=None):
    orig = subprocess.Popen

    def _popen(_cmd, **kwargs):
        env = dict(os.environ, FAKE_SCENARIO=scenario)
        if kid_file is not None:
            env["FAKE_KID_FILE"] = str(kid_file)
        return orig([sys.executable, str(fake_claude)], env=env, **kwargs)

    monkeypatch.setattr(cleo.subprocess, "Popen", _popen)


def _alive(pid: int) -> bool:
    """True only if the pid exists AND is not a zombie.

    A killed grandchild lingers as a zombie until PID 1 reaps it, and os.kill(pid, 0)
    still succeeds for a zombie — so checking only for existence would report a
    reaped process as alive and make this test useless.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    """Poll until the pid is gone. Reparenting to PID 1 and reaping isn't instant."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return not _alive(pid)


def _consume(cleo, timeout):
    """Run call_claude_streaming on a thread; return (chunks, error, hung)."""
    out: dict = {"chunks": [], "error": None}

    def _go():
        try:
            for ch in cleo.call_claude_streaming("m", "sys", [{"role": "user", "content": "hi"}], [], 8192):
                out["chunks"].append(ch)
        except Exception as exc:
            out["error"] = exc

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    t.join(timeout)
    return out["chunks"], out["error"], t.is_alive()


def test_stderr_flood_does_not_deadlock(cleo, fake_claude, monkeypatch):
    """A subprocess that floods stderr past the pipe buffer must still complete."""
    _patch_popen(cleo, fake_claude, "stderr_flood", monkeypatch)
    chunks, error, hung = _consume(cleo, timeout=20)

    assert not hung, "call_claude_streaming hung on a stderr flood (deadlock)"
    assert error is None, f"unexpected error: {error}"
    deltas = "".join(c["delta"] for c in chunks if "delta" in c)
    assert "hello world" in deltas
    assert any(c.get("done") for c in chunks), "never yielded the done chunk"


def test_hung_subprocess_is_killed_by_watchdog(cleo, fake_claude, monkeypatch):
    """A subprocess that never responds must be killed and surface a timeout."""
    monkeypatch.setattr(cleo, "CLAUDE_CALL_TIMEOUT_S", 2.0)
    _patch_popen(cleo, fake_claude, "hang", monkeypatch)
    chunks, error, hung = _consume(cleo, timeout=15)

    assert not hung, "watchdog did not kill the hung subprocess"
    assert error is not None and "timed out" in str(error), f"expected a timeout error, got {error}"


# The grandchild tests are the reason this fix exists, so they assert on real
# process state rather than on which signal we happened to send. POSIX-only:
# process groups, killpg and /proc all are.
_posix_only = pytest.mark.skipif(
    not hasattr(os, "killpg") or not os.path.isdir("/proc"),
    reason="process-group reaping is POSIX-only",
)


@_posix_only
def test_grandchild_dies_on_the_happy_path(cleo, fake_claude, monkeypatch, tmp_path):
    """A normal, successful call must not strand the CLI's children.

    This is the leak. The read loop breaks the moment it sees the result event,
    while the CLI is still alive, and the old code SIGKILLed it right there — on
    every single call. SIGKILL can't be caught, so Node never reaped its own
    children and they reparented to PID 1 and stayed for the life of the box.
    """
    kid_file = tmp_path / "kid.pid"
    monkeypatch.setattr(cleo, "REAP_GRACE_S", 2.0)
    _patch_popen(cleo, fake_claude, "grandchild_ok", monkeypatch, kid_file=kid_file)

    chunks, error, hung = _consume(cleo, timeout=30)
    assert not hung and error is None, f"call failed: {error}"
    assert any(c.get("done") for c in chunks), "never yielded the done chunk"

    kid = int(kid_file.read_text())
    assert _wait_dead(kid), (
        f"grandchild {kid} outlived the call — orphaned processes are leaking"
    )


@_posix_only
def test_grandchild_dies_when_the_watchdog_fires(cleo, fake_claude, monkeypatch, tmp_path):
    """A timed-out call must take the whole process group down, not just the CLI."""
    kid_file = tmp_path / "kid.pid"
    monkeypatch.setattr(cleo, "CLAUDE_CALL_TIMEOUT_S", 2.0)
    monkeypatch.setattr(cleo, "REAP_GRACE_S", 2.0)
    _patch_popen(cleo, fake_claude, "grandchild_hang", monkeypatch, kid_file=kid_file)

    _chunks, error, hung = _consume(cleo, timeout=30)
    assert not hung, "watchdog did not kill the hung subprocess"
    assert error is not None and "timed out" in str(error)

    kid = int(kid_file.read_text())
    assert _wait_dead(kid), f"grandchild {kid} survived the watchdog kill"


def test_dispatch_always_releases_its_inflight_slot(cleo):
    """A failing request must give its capacity back, or Cleo wedges permanently.

    The semaphore is the backpressure; if a crash could leak a slot, a run of
    errors would silently reduce capacity to zero and stop Cleo answering at all.
    """
    sem = threading.BoundedSemaphore(1)
    assert sem.acquire(blocking=False)

    # handle_request raises (no such bus), _dispatch swallows it, slot comes back.
    cleo._dispatch({"id": 1, "from": "x", "content": "not json"},
                   "http://127.0.0.1:1", "tok", "cleo", sem)

    assert sem.acquire(blocking=False), "_dispatch leaked its in-flight slot on error"


# ---------------------------------------------------------------------------
# Tool-marker dialect tolerance (the "blank market report" bug, 2026-08-06)
# ---------------------------------------------------------------------------
#
# There is no tool schema on this path, so nothing validates the model's marker
# syntax. Models emit near-misses. The old strict pattern matched exactly one
# dialect, and because _ToolMarkerFilter suppresses everything after the bare
# `<tool_call` prefix, a near-miss lost BOTH the tool call and the text — the
# run finished with a 0-character report and no error to trace.

_DIALECTS = [
    ('canonical', '<tool_call name="get_stock_data">{"ticker":"SPY"}</tool_call>'),
    ('single_quotes', "<tool_call name='get_stock_data'>{\"ticker\":\"SPY\"}</tool_call>"),
    ('envelope', '<tool_call>{"name":"get_stock_data","arguments":{"ticker":"SPY"}}</tool_call>'),
    ('id_before_name', '<tool_call id="1" name="get_stock_data">{"ticker":"SPY"}</tool_call>'),
    ('newlines', '<tool_call name="get_stock_data">\n{"ticker":"SPY"}\n</tool_call>'),
    ('code_fence', '<tool_call name="get_stock_data">```json\n{"ticker":"SPY"}\n```</tool_call>'),
    ('unquoted_attr', '<tool_call name=get_stock_data>{"ticker":"SPY"}</tool_call>'),
    ('envelope_fenced',
     '<tool_call>```json\n{"name":"get_stock_data","arguments":{"ticker":"SPY"}}\n```</tool_call>'),
]


@pytest.mark.unit
@pytest.mark.parametrize("label,raw", _DIALECTS, ids=[d[0] for d in _DIALECTS])
def test_every_observed_dialect_yields_the_same_call(cleo, label, raw):
    calls = cleo._parse_tool_calls(raw)
    assert len(calls) == 1, f"{label}: dropped — this is a silently empty report"
    assert calls[0]["name"] == "get_stock_data"
    assert calls[0]["args"] == {"ticker": "SPY"}, f"{label}: args mangled"


@pytest.mark.unit
def test_argument_legitimately_called_name_is_not_mistaken_for_the_tool(cleo):
    """The envelope form looks for a "name" key — it must not hijack a real arg."""
    calls = cleo._parse_tool_calls(
        '<tool_call name="lookup">{"name":"Apple Inc"}</tool_call>'
    )
    assert calls[0]["name"] == "lookup"
    assert calls[0]["args"] == {"name": "Apple Inc"}


@pytest.mark.unit
def test_mixed_dialects_in_one_reply_all_parse(cleo):
    calls = cleo._parse_tool_calls(
        '<tool_call name="a">{"x":1}</tool_call>\n'
        '<tool_call>{"name":"b","args":{"y":2}}</tool_call>'
    )
    assert [(c["name"], c["args"]) for c in calls] == [("a", {"x": 1}), ("b", {"y": 2})]


@pytest.mark.unit
def test_unreadable_marker_is_logged_not_swallowed(cleo, caplog):
    """A marker we still can't parse must leave a trace. The filter has already
    eaten the text by this point, so without the log the run is undiagnosable."""
    with caplog.at_level(logging.ERROR):
        calls = cleo._parse_tool_calls("<tool_call>not json at all</tool_call>")
    assert calls == []
    assert any("tool_call" in r.message.lower() for r in caplog.records)


@pytest.mark.unit
def test_ordinary_prose_produces_no_calls_and_no_error_log(cleo, caplog):
    with caplog.at_level(logging.ERROR):
        assert cleo._parse_tool_calls("SPY closed at 512.30, up 0.4% on the session.") == []
    assert not caplog.records


def test_claude_command_skips_interactive_user_setup(cleo, monkeypatch):
    """Background calls must not load the user's hooks, plugins, skills or memory."""
    import json as _json
    seen = {}

    class _Stop(Exception):
        pass

    def _popen(cmd, **kwargs):
        seen["cmd"] = cmd
        raise _Stop()

    monkeypatch.setattr(cleo.subprocess, "Popen", _popen)
    with pytest.raises(_Stop):
        next(cleo.call_claude_streaming("sonnet", "sys", [{"role": "user", "content": "hi"}], [], 100))
    cmd = seen["cmd"]
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--disable-slash-commands" in cmd
    settings = _json.loads(cmd[cmd.index("--settings") + 1])
    assert settings == {"autoMemoryEnabled": False, "effortLevel": "high"}
    assert cmd[cmd.index("--tools") + 1] == ""
