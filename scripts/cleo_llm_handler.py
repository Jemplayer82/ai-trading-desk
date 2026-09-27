#!/usr/bin/env python3
"""Cleo LLM handler — Claude CLI-backed bus daemon for TradingAgents.

Registers on the mcp-switchboard bus, polls for llm_request DMs, drives the
local ``claude`` CLI in headless streaming mode

    claude -p --input-format stream-json --output-format stream-json \\
           --verbose --include-partial-messages

which uses the machine's Claude Code subscription session — NO Anthropic API
key, NO per-token billing. Replies are streamed back as ``llm_stream_chunk``
DMs so tokens appear live in the dashboard.

Run this on a machine where ``claude -p "hi"`` already works (i.e. Claude Code
is logged in). It reaches the switchboard purely over HTTP, so it can run
anywhere that can hit SWITCHBOARD_URL.

Required env vars:
  SWITCHBOARD_URL         e.g. http://172.21.0.3:3107 or http://192.168.7.50:3109
  SWITCHBOARD_MCP_TOKEN   Bearer token matching the stack's SWITCHBOARD_MCP_TOKEN

Optional:
  SWITCHBOARD_AGENT_ID    Agent name to register as (default: cleo)
  DEFAULT_MODEL           Fallback model if the request doesn't specify one
                          (default: sonnet — the CLI's always-latest Sonnet alias)
  CLAUDE_BIN              Path to claude CLI binary (default: claude)

Usage:
  SWITCHBOARD_URL=http://172.21.0.3:3107 \\
  SWITCHBOARD_MCP_TOKEN=<token> \\
  python3 scripts/cleo_llm_handler.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "sonnet")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

# Hard ceiling on a single claude -p call. The watchdog kills the subprocess past
# this, so a stuck CLI (auth wedge, no output, etc.) can never hang a worker
# forever. Keep it STRICTLY below the client-side timeout (switchboard_client
# defaults to 180s) so Cleo fails itself and emits an error before the requester
# gives up waiting.
CLAUDE_CALL_TIMEOUT_S = float(os.environ.get("CLEO_CALL_TIMEOUT_S", "150"))

# How long to let the CLI shut itself down at each escalation step (natural exit,
# then SIGTERM, then SIGKILL). Short, because this runs after the reply has
# already streamed — it only delays the final done-chunk.
REAP_GRACE_S = float(os.environ.get("CLEO_REAP_GRACE_S", "5"))

# Ceiling on requests in flight at once. ThreadPoolExecutor's queue is unbounded,
# and each queued item holds a full conversation payload (megabytes for an
# analyst run), so an unthrottled burst grows it without limit — which presents
# exactly like a memory leak. Rejecting fast beats queueing forever: the
# requester already gives up after its own 180s timeout.
MAX_INFLIGHT = int(os.environ.get("CLEO_MAX_INFLIGHT", "16"))

# Escalation ladder for shutting the CLI down. SIGKILL doesn't exist on Windows
# (a dev box running the tests), so fall back to SIGTERM there.
_SIG_ESCALATION = (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM))

# Holds the single-instance file lock for the life of the process. Kept at module
# scope so the fd is never garbage-collected (which would release the lock).
_INSTANCE_LOCK = None

# One pooled HTTP client for the whole process. The previous code called the
# module-level httpx.post(), which constructs and tears down an entire Client —
# and a fresh TCP connection — on every call. bus_call runs once per streamed
# text delta, so a single analyst report opened thousands of short-lived
# connections. httpx.Client is safe to share across threads.
_HTTP = httpx.Client(
    limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
)

# Text protocol for tool calling. The CLI can't accept Anthropic tool schemas
# (that's an API-only feature), so we teach the model an inline marker syntax in
# the system prompt and parse it back out of the generated text.
_TOOL_OPEN = "<tool_call"
_TOOL_CLOSE = "</tool_call>"

# Parsing is deliberately LOOSE, and it has to be.
#
# There is no tool schema on this path — the CLI can't take one, so the model is
# told the marker syntax in prose and has nothing validating its output. Models
# therefore emit near-misses: single quotes, reordered attributes, or the far
# more common `{"name": ..., "arguments": {...}}` envelope they saw everywhere
# in training. The old strict pattern (`name="..."` in that exact order, double
# quotes only) matched none of those.
#
# That combination was silently destructive. _ToolMarkerFilter suppresses
# everything from the bare prefix `<tool_call` onward, so a near-miss lost the
# tool call AND the text: the run finished with a 0-character report, no error
# anywhere. Measured 2026-08-06: 44% of Haiku and 33% of Opus 4.8 multi-tool
# replies produced zero tool calls, which is what "the market report comes back
# blank" actually was. It read as a model-quality problem and was a parser one.
#
# So: match the block loosely, then work out the tool name from whichever
# dialect showed up, and shout in the log if a marker appears that we still
# cannot read — this must never fail silently again.
_TOOL_BLOCK_RE = re.compile(r"<tool_call\b([^>]*)>([\s\S]*?)</tool_call>", re.I)
_ATTR_RE = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")

# Where the tool name and its arguments hide, across the dialects seen in the
# wild. Order matters: first hit wins.
_NAME_KEYS = ("name", "tool", "tool_name", "function")
_ARG_KEYS = ("arguments", "args", "input", "parameters", "params")


# ---------------------------------------------------------------------------
# Bus helpers
# ---------------------------------------------------------------------------

def _parse_sse(resp: httpx.Response) -> dict:
    ct = resp.headers.get("content-type", "")
    if ct.startswith("application/json"):
        return resp.json()
    last_data = None
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            last_data = line[5:].strip()
    if last_data is None:
        raise RuntimeError(f"No data: line in SSE body: {resp.text!r}")
    return json.loads(last_data)


def bus_call(url: str, token: str, tool: str, args: dict, timeout: float = 35.0) -> dict:
    resp = _HTTP.post(
        url.rstrip("/") + "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args}},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    rpc = _parse_sse(resp)
    if rpc.get("error"):
        err = rpc["error"]
        raise RuntimeError(f"Bus RPC error: {err.get('message') if isinstance(err, dict) else err}")
    result = rpc.get("result") or {}
    if result.get("isError"):
        content = result.get("content") or []
        raise RuntimeError(f"Bus tool error: {content[0].get('text', '') if content else result}")
    content = result.get("content") or []
    return json.loads(content[0].get("text", "{}")) if content else {}


# ---------------------------------------------------------------------------
# Message / tool conversion helpers
# ---------------------------------------------------------------------------

def _flatten_content(content, id_to_name: dict | None = None) -> str:
    """Collapse an Anthropic content value (str or block list) to plain text.

    tool_use / tool_result blocks are rendered as the inline text protocol so
    the CLI (which has no API tool awareness) still sees the full history.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "tool_use":
            args = block.get("input", {})
            call_id = block.get("id") or ""
            id_attr = f' id="{call_id}"' if call_id else ""
            parts.append(f'{_TOOL_OPEN} name="{block.get("name", "")}"{id_attr}>'
                         f'{json.dumps(args)}{_TOOL_CLOSE}')
        elif btype == "tool_result":
            inner = block.get("content", "")
            if isinstance(inner, list):
                inner = "".join(
                    b.get("text", "") if isinstance(b, dict) else str(b) for b in inner
                )
            call_id = block.get("tool_use_id") or ""
            name = (id_to_name or {}).get(call_id, "")
            bits = []
            if name:
                bits.append(f"for {name}")
            if call_id:
                bits.append(f"(id: {call_id})")
            label = "[Tool result" + ((" " + " ".join(bits)) if bits else "") + "]"
            parts.append(f"{label}\n{inner}")
    return "\n".join(p for p in parts if p)


def _merge_consecutive(messages: list) -> list:
    """Collapse runs of same-role messages into one turn.

    `claude -p --input-format stream-json` answers EVERY user event as its own
    turn. The app sends parallel tool results as one ToolMessage each, so 8
    concurrent get_indicators calls arrived as 8 consecutive user events — the
    CLI then produced 8 separate replies. _ToolMarkerFilter latches on the first
    <tool_call> it sees and suppresses all later text, so the report the model
    eventually wrote was discarded and only the early tool call survived, and
    the analyst loop span until it exhausted its turns with an empty report.

    Anthropic semantics put all parallel tool_results in a SINGLE user message;
    this restores that shape, so the model sees every result at once and replies
    exactly once.
    """
    merged: list = []
    for m in messages or []:
        role = m.get("role", "user")
        if merged and merged[-1].get("role", "user") == role:
            prev = merged[-1]
            pc, mc = prev.get("content"), m.get("content")
            if isinstance(pc, list) and isinstance(mc, list):
                prev["content"] = pc + mc
            else:
                prev["content"] = f"{_flatten_content(pc)}\n\n{_flatten_content(mc)}"
            continue
        merged.append(dict(m))
    return merged


def _build_tool_id_map(messages: list) -> dict:
    """Map every tool_use id -> tool name across the conversation.

    tool_result blocks carry only ``tool_use_id``, never the name. Without this
    the CLI saw a run of undifferentiated "[Tool result]" blobs and could not
    tell which of several parallel calls each one answered — so it re-requested
    data it already had, burned every allowed turn, and the analyst report came
    back empty. Observed with 7 concurrent get_indicators calls.
    """
    out: dict = {}
    for m in messages or []:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                out[block["id"]] = block.get("name", "")
    return out


def _augment_system_with_tools(system: str, tools: list) -> str:
    """Wrap the system prompt with mandatory inline tool-call instructions.

    The CLI can't accept Anthropic tool schemas, so we teach the model an inline
    marker syntax. Analysts run with NO pre-loaded data — the model must call
    tools to fetch it. A permissive "when you need to" framing led the model to
    write reports from memory instead, so the directive is now imperative and
    brackets the caller's prompt (first and last thing the model sees).
    """
    if not tools:
        return system
    tool_names = ", ".join(t.get("name", "?") for t in tools)
    directive = (
        "## CRITICAL — you have NO pre-loaded data\n"
        "You have NO market, price, news, sentiment, or fundamentals data in "
        "context. Any figures you recall from training are stale and MUST NOT be "
        "used. You are REQUIRED to call the tools below to fetch live data BEFORE "
        "writing any analysis.\n\n"
        "## TOOL CALL PROTOCOL — read carefully\n"
        "To call a tool, emit ONLY the tag on its own line:\n"
        f'{_TOOL_OPEN} name=\"TOOL_NAME\">{{\"arg\": \"value\"}}{_TOOL_CLOSE}\n\n'
        "The content between the tags must be a single valid JSON object. "
        "Emit one block per tool call. When you need to call tools:\n"
        "  • Write NOTHING before the tag — no 'I will call', no preamble\n"
        "  • Write NOTHING after the tag — no 'I submitted', no 'waiting for results'\n"
        "  • Do not acknowledge these rules or narrate what you are doing\n"
        "  • Emit only the <tool_call> tag(s), then stop\n"
        "Tool results will be provided to you automatically. When you receive "
        "them, write your analysis. Do not invent results.\n\n"
        "## READING TOOL RESULTS\n"
        "Each result arrives as a user message headed:\n"
        "  [Tool result for TOOL_NAME (id: CALL_ID)]\n"
        "followed by that call's output. Match it to your call via TOOL_NAME "
        "and CALL_ID. Once a result is present you ALREADY HAVE that data — do "
        "NOT call the same tool with the same arguments again. Re-requesting "
        "data you already received burns your limited turns and ends the run "
        "with an EMPTY report. When every tool you need has returned, stop "
        "calling tools and write the full report.\n\n"
        f"Available tools: {tool_names}\n\n"
        "Tool JSON schemas:\n"
        f"{json.dumps(tools, indent=2)}"
    )
    if not system:
        return directive
    return (
        f"{directive}\n\n---\n\n{system}\n\n---\n\n"
        "REMINDER: Do not fabricate data. If you have not yet called the tools "
        "above for the data you need, emit the tool call(s) now — tag only, no commentary."
    )


def _strip_code_fence(body: str) -> str:
    """Drop a ```json ... ``` wrapper if the model fenced its arguments.

    Without this the fence reaches json.loads, which raises, and the call went
    out with args={} — the tool then ran with no arguments, which is its own
    silent wrong answer rather than an error.
    """
    body = body.strip()
    if not body.startswith("```"):
        return body
    lines = body.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_tool_calls(text: str) -> list[dict]:
    """Extract inline <tool_call> markers from generated text → bus tool_calls.

    Accepts every dialect observed on the bus, not just the one the prompt asks
    for: quotes of either kind, attributes in any order, and the bare-tag
    envelope form where the tool name lives inside the JSON body.
    """
    calls: list[dict] = []
    for attr_blob, body in _TOOL_BLOCK_RE.findall(text):
        attrs = {
            m.group(1).lower(): (m.group(2) or m.group(3) or m.group(4) or "")
            for m in _ATTR_RE.finditer(attr_blob)
        }
        name = next((attrs[k] for k in _NAME_KEYS if attrs.get(k)), "")
        call_id = attrs.get("id", "")

        payload = _strip_code_fence(body)
        parsed: object = None
        if payload:
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                log.warning("tool_call body is not valid JSON: %.200s", payload)

        args: dict = {}
        if isinstance(parsed, dict):
            if name:
                # The attribute already named the tool, so the body is the
                # argument object — unless it nests one under a known key.
                nested = next(
                    (parsed[k] for k in _ARG_KEYS if isinstance(parsed.get(k), dict)),
                    None,
                )
                args = nested if nested is not None else parsed
            else:
                # Envelope form: {"name": "...", "arguments": {...}}
                name = next(
                    (parsed[k] for k in _NAME_KEYS if isinstance(parsed.get(k), str)),
                    "",
                )
                args = next(
                    (parsed[k] for k in _ARG_KEYS if isinstance(parsed.get(k), dict)),
                    {},
                )

        if not name:
            log.error(
                "DROPPED tool_call — no tool name in any known position: <tool_call%.120s>%.200s",
                attr_blob, body,
            )
            continue

        calls.append({
            "id": call_id or f"toolu_{uuid4().hex[:16]}",
            "name": name,
            "args": args,
        })

    if not calls and _TOOL_OPEN in text:
        # The filter has already swallowed this text, so without the log line
        # the run just ends with an empty report and no trace of why.
        log.error("tool_call marker present but nothing parsed: %.400s", text)
    return calls


class _ToolMarkerFilter:
    """Streams text but withholds anything inside or after <tool_call> blocks.

    Markers can be split across deltas, so a small holdback buffer prevents a
    partial ``<tool_call`` prefix from leaking to the dashboard before we know
    whether it's really the start of a tool block.

    Once a tool_call tag is encountered, everything after it is also suppressed.
    This stops post-call commentary ("I've submitted…", "waiting for results…")
    from reaching the dashboard even when the model ignores the prompt instruction
    to emit nothing after the tag.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_tool = False
        self._saw_tool = False   # latched True after first tool block; suppresses all further text
        self._raw: list[str] = []

    def feed(self, text: str) -> str:
        self._raw.append(text)
        if self._saw_tool:
            return ""  # tool block seen earlier — suppress everything after it
        self._buf += text
        out: list[str] = []
        while self._buf:
            if not self._in_tool:
                idx = self._buf.find(_TOOL_OPEN)
                if idx == -1:
                    safe = self._safe_len(self._buf, _TOOL_OPEN)
                    if safe:
                        out.append(self._buf[:safe])
                        self._buf = self._buf[safe:]
                    break
                if idx > 0:
                    out.append(self._buf[:idx])
                self._buf = self._buf[idx:]
                self._in_tool = True
            else:
                idx = self._buf.find(_TOOL_CLOSE)
                if idx == -1:
                    break  # still inside a tool block; hold everything back
                self._buf = self._buf[idx + len(_TOOL_CLOSE):]
                self._in_tool = False
                self._saw_tool = True  # latch — suppress all text from here on
                self._buf = ""         # discard any post-tag text already buffered
                break
        return "".join(out)

    @staticmethod
    def _safe_len(s: str, marker: str) -> int:
        """How much of s is safe to emit (not a partial-marker suffix)."""
        for k in range(min(len(marker) - 1, len(s)), 0, -1):
            if marker.startswith(s[-k:]):
                return len(s) - k
        return len(s)

    def flush(self) -> str:
        if not self._in_tool and not self._saw_tool and self._buf:
            out, self._buf = self._buf, ""
            return out
        return ""

    @property
    def full_text(self) -> str:
        return "".join(self._raw)


# ---------------------------------------------------------------------------
# Claude CLI invocation
# ---------------------------------------------------------------------------

def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the CLI's whole process group, not just the CLI itself.

    ``claude`` is a Node process that spawns children. Signalling only the direct
    child leaves those grandchildren running; they reparent to PID 1 and survive
    for the life of the box. Popen(start_new_session=True) puts the CLI in its
    own process group precisely so the whole tree can be taken down here.
    """
    # Never signal a PID we have already reaped. Popen.send_signal() has this
    # guard built in; raw killpg does not, and the watchdog can fire at the same
    # moment _reap() collects the child — at which point the PID may already
    # belong to something else. Signalling a recycled PID's whole group would
    # take out unrelated processes owned by this user.
    if proc.returncode is not None:
        return

    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass  # already gone, or we lost the race with a normal exit
    # Non-POSIX, or no group to signal. Fall back to the direct child so a
    # missing group never means "no signal at all".
    try:
        proc.send_signal(sig)
    except Exception:
        pass


def _reap(proc: subprocess.Popen) -> None:
    """Shut the CLI down gracefully, escalating only if it refuses to go.

    The read loop breaks the instant it sees the ``result`` event, at which point
    the CLI is still running and about to exit on its own. The previous code
    SIGKILLed it right there — on EVERY call, not just the rare timeout — and
    SIGKILL cannot be caught, so Node never got the chance to reap its own
    children. Over hundreds of calls a day that is a steady drip of orphans.

    So: wait for a natural exit first, then SIGTERM the group (catchable, lets
    Node clean up), and only then SIGKILL the group.
    """
    if proc.poll() is None:
        try:
            proc.wait(timeout=REAP_GRACE_S)
        except subprocess.TimeoutExpired:
            pass

    for sig in _SIG_ESCALATION:
        if proc.poll() is not None:
            break
        _signal_group(proc, sig)
        try:
            proc.wait(timeout=REAP_GRACE_S)
        except subprocess.TimeoutExpired:
            continue

    if proc.poll() is None:
        log.warning("claude CLI pid %s survived SIGKILL — likely orphaned", proc.pid)

    # Close our ends last. Doing it earlier would hand the CLI an EPIPE and make
    # it die abruptly, which is the very thing we are trying to avoid.
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def call_claude_streaming(model: str, system: str, messages: list, tools: list, max_tokens: int):
    """Drive ``claude -p`` in stream-json mode and yield text deltas + tool_calls.

    Uses the local Claude Code subscription session (free — no API key). Yields:
      {"delta": "text"}                    — streamed text (tool markers stripped)
      {"done": True, "tool_calls": [...]}  — completion signal
    """
    system = _augment_system_with_tools(system, tools)

    cmd = [
        CLAUDE_BIN, "-p",
        "--strict-mcp-config",  # no MCP servers; stops leaked docker MCP containers
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--no-session-persistence",
        "--tools", "",            # disable built-in Claude Code tools
        "--model", model,
    ]
    if system:
        cmd += ["--system-prompt", system]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        # Own process group, so _signal_group can take down the CLI's children
        # with it instead of leaving them to reparent to PID 1.
        start_new_session=True,
    )

    # Feed the conversation on a thread so a large history can't deadlock against
    # a full stdout pipe.
    id_to_name = _build_tool_id_map(messages)
    messages = _merge_consecutive(messages)

    if os.environ.get("CLEO_DEBUG_DUMP"):
        try:
            with open("/tmp/cleo_dump.txt", "a", encoding="utf-8") as _fh:
                _fh.write("\n\n########## REQUEST ##########\n")
                _fh.write(f"[id_to_name] {id_to_name}\n")
                _fh.write(f"[system len] {len(system)}\n")
                for _m in messages:
                    _r = _m.get("role", "user")
                    _t = _flatten_content(_m.get("content", ""), id_to_name)
                    _fh.write(f"\n--- {_r} ---\n{_t[:1200]}\n")
        except Exception:
            pass

    def _write_stdin() -> None:
        try:
            for m in messages:
                role = m.get("role", "user")
                evt_type = "assistant" if role == "assistant" else "user"
                event = {
                    "type": evt_type,
                    "message": {
                        "role": role,
                        "content": [{"type": "text",
                                     "text": _flatten_content(m.get("content", ""), id_to_name)}],
                    },
                }
                proc.stdin.write(json.dumps(event) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    # Drain stderr on a thread too. ``claude -p --verbose`` is chatty on stderr;
    # if nobody reads it, its ~64KB OS pipe buffer fills, the CLI blocks writing,
    # stops producing stdout, and the read loop below hangs forever (the original
    # "works once, fails the next" bug). Keep only the tail for error reporting.
    stderr_tail: deque[str] = deque(maxlen=50)

    def _drain_stderr() -> None:
        try:
            for line in proc.stderr:
                stderr_tail.append(line)
        except (BrokenPipeError, ValueError):
            pass

    # Watchdog: hard-kill the subprocess if it blows past the per-call deadline,
    # so a stuck claude (no output at all, auth wedge, network stall) can't hang
    # the worker indefinitely. Killing closes stdout, which ends the read loop.
    done_evt = threading.Event()
    timed_out = threading.Event()

    def _watchdog() -> None:
        if not done_evt.wait(CLAUDE_CALL_TIMEOUT_S):
            timed_out.set()
            log.warning("claude CLI exceeded %.0fs deadline — killing", CLAUDE_CALL_TIMEOUT_S)
            # Whole group: a wedged CLI is exactly the case where children are
            # most likely to be left behind.
            _signal_group(proc, _SIG_ESCALATION[-1])

    threading.Thread(target=_write_stdin, daemon=True).start()
    threading.Thread(target=_drain_stderr, daemon=True).start()
    threading.Thread(target=_watchdog, daemon=True).start()

    flt = _ToolMarkerFilter()
    error_result: str | None = None
    got_result = False

    try:
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            otype = obj.get("type", "")

            if otype == "stream_event":
                event = obj.get("event", {})
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        visible = flt.feed(delta.get("text", ""))
                        if visible:
                            yield {"delta": visible}

            elif otype == "result":
                got_result = True
                if obj.get("is_error"):
                    error_result = obj.get("result") or "claude CLI returned an error"
                break

        tail = flt.flush()
        if tail:
            yield {"delta": tail}
    finally:
        # Always stop the watchdog and reap the child — covers normal completion,
        # an exception, and an abandoned generator (requester went away).
        done_evt.set()
        _reap(proc)

    if error_result is not None:
        raise RuntimeError(f"claude CLI error: {error_result}")
    if got_result:
        # A clean result event means the call succeeded — don't misread the
        # post-result kill above as a failure via returncode.
        yield {"done": True, "tool_calls": _parse_tool_calls(flt.full_text)}
        return
    # No result event ⇒ the CLI died early. Explain why: deadline first, then exit.
    if timed_out.is_set():
        raise RuntimeError(f"claude CLI timed out after {CLAUDE_CALL_TIMEOUT_S:.0f}s")
    err = "".join(stderr_tail).strip()
    raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {err}")


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

def handle_request(msg: dict, url: str, token: str, agent_id: str,
                   backend=None, default_model: str | None = None) -> None:
    """Serve one llm_request. ``backend`` defaults to call_claude_streaming; the
    Codex handler (scripts/codex_llm_handler.py) passes its own generator with
    the same (model, system, messages, tools, max_tokens) signature."""
    backend = backend or call_claude_streaming
    req = json.loads(msg["content"])
    model = req.get("model") or default_model or DEFAULT_MODEL
    system = req.get("system") or ""
    messages = req.get("messages") or []
    tools = req.get("tools") or []
    try:
        max_tokens = int(req.get("max_tokens") or 8192)
    except (TypeError, ValueError):
        max_tokens = 8192
    sender = msg.get("from")
    msg_id = msg.get("id")
    thread_id = msg.get("thread_id") or str(uuid4())

    log.info("llm_request from=%s model=%s tools=%d", sender, model, len(tools))

    def send(msg_type: str, content: str) -> None:
        bus_call(url, token, "send_message", {
            "from": agent_id,
            "to": sender,
            "type": msg_type,
            "thread_id": thread_id,
            "reply_to": msg_id,
            "content": content,
        })

    tool_calls: list = []
    for chunk in backend(model, system, messages, tools, max_tokens):
        if "delta" in chunk:
            # Best-effort: a transient bus hiccup on one delta must not tear down
            # an otherwise-healthy stream (and orphan the live claude subprocess).
            try:
                send("llm_stream_chunk", json.dumps({"delta": chunk["delta"], "done": False}))
            except Exception as exc:
                log.warning("delta send failed (continuing stream): %s", exc)
        elif chunk.get("done"):
            tool_calls = chunk.get("tool_calls", [])

    send("llm_stream_chunk", json.dumps({"delta": "", "done": True, "tool_calls": tool_calls}))
    log.info("  → streamed reply to %s (%d tool_calls)", sender, len(tool_calls))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _acquire_single_instance_lock(agent_id: str) -> None:
    """Hard single-instance guard: at most one Cleo poller per host.

    Two processes polling the same agent_id silently split the request stream —
    each wait_for_message drains the shared inbox, so one streams while the other
    answers in one shot, corrupting replies. systemd already runs a single managed
    instance; this stops a SECOND manual start from racing it. We use an exclusive,
    non-blocking OS file lock: the kernel releases it the instant the holding
    process dies, so a systemd restart re-acquires cleanly with no crash loop.

    No-op where file locking isn't available (e.g. a Windows dev box).
    """
    global _INSTANCE_LOCK
    try:
        import fcntl
    except ImportError:
        return  # non-POSIX — skip the guard
    lock_path = os.environ.get("CLEO_LOCK_FILE", f"/tmp/cleo-{agent_id}.lock")
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error(
            "Another Cleo instance already holds %s — refusing to start a second "
            "poller for agent_id '%s' (a second poller would split the request "
            "stream and corrupt replies). Exiting.",
            lock_path, agent_id,
        )
        sys.exit(1)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    _INSTANCE_LOCK = lock_file  # keep the fd alive for the process lifetime


def main(backend=None, display_name: str = "Cleo (Claude daemon)",
         default_agent_id: str = "cleo", default_model: str | None = None) -> None:
    url = os.environ["SWITCHBOARD_URL"]
    token = os.environ["SWITCHBOARD_MCP_TOKEN"]  # pragma: allowlist secret
    agent_id = os.environ.get("SWITCHBOARD_AGENT_ID", default_agent_id)

    _acquire_single_instance_lock(agent_id)

    def bus(tool: str, args: dict) -> dict:
        return bus_call(url, token, tool, args)

    # Split-brain heads-up: if a SECOND process polls the same agent_id, the two
    # silently split the request stream (each wait_for_message drains the shared
    # inbox) — one streams while the other answers in one shot, corrupting
    # replies. Presence alone can't prove a duplicate (a just-restarted self
    # lingers ~60s, and a heartbeat script keeps presence warm), so this is an
    # advisory note, not a hard failure. The real tell is `llm_response` /
    # reply_to:null messages from this agent on the bus while a handler streams.
    try:
        agents = bus("list_agents", {}).get("agents", [])
        peer = next(
            (a for a in agents if a.get("id") == agent_id and a.get("online")),
            None,
        )
        if peer:
            log.info(
                "Note: '%s' already shows online (likely this restart's lingering "
                "presence or a heartbeat script). If replies look truncated, make "
                "sure only ONE handler polls this agent_id (or use a unique "
                "SWITCHBOARD_AGENT_ID).",
                agent_id,
            )
    except Exception as exc:
        log.debug("dup-registration check skipped: %s", exc)

    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="cleo")
    # Backpressure. Without this, every inbound request is submitted to an
    # unbounded queue holding full conversation payloads; a TradingAgents analyst
    # fan-out that outruns the workers grows it without limit.
    inflight = threading.BoundedSemaphore(MAX_INFLIGHT)

    # Registration is retried inside the loop so Cleo recovers automatically if the
    # switchboard restarts (registration is lost on switchboard restart; the poll
    # would keep failing until re-registration succeeds).
    registered = False

    while True:
        if not registered:
            try:
                bus("register_agent", {"agent_id": agent_id, "name": display_name})
                bus("set_status", {"agent_id": agent_id, "activity": "ready"})
                log.info("registered as '%s' — waiting for llm_request DMs", agent_id)
                registered = True
            except Exception as exc:
                log.warning("Registration failed: %s — retrying in 5s", exc)
                time.sleep(5)
                continue

        try:
            result = bus("wait_for_message", {"agent_id": agent_id, "timeout_seconds": 25})
        except Exception as exc:
            log.warning("Poll error: %s — re-registering in 2s", exc)
            registered = False  # switchboard may have restarted; force re-registration
            time.sleep(2)
            continue

        for msg in result.get("messages", []):
            if msg.get("type") != "llm_request":
                continue
            if not inflight.acquire(blocking=False):
                log.warning("at capacity (%d in flight) — rejecting message %s",
                            MAX_INFLIGHT, msg.get("id"))
                _send_error(msg, url, token, agent_id,
                            f"Cleo is at capacity ({MAX_INFLIGHT} requests in "
                            f"flight); try again shortly")
                continue
            executor.submit(_dispatch, msg, url, token, agent_id, inflight,
                            backend, default_model)


def _send_error(msg: dict, url: str, token: str, agent_id: str, error: str) -> None:
    """Best-effort llm_error reply. Never raises — the caller is a worker loop."""
    try:
        bus_call(url, token, "send_message", {
            "from": agent_id,
            "to": msg.get("from"),
            "type": "llm_error",
            "thread_id": msg.get("thread_id"),
            "reply_to": msg.get("id"),
            "content": json.dumps({"error": error}),
        })
    except Exception:
        pass


def _dispatch(msg: dict, url: str, token: str, agent_id: str,
              inflight: threading.BoundedSemaphore | None = None,
              backend=None, default_model: str | None = None) -> None:
    try:
        handle_request(msg, url, token, agent_id, backend=backend, default_model=default_model)
    except Exception as exc:
        log.exception("Error handling message %s", msg.get("id"))
        _send_error(msg, url, token, agent_id, str(exc))
    finally:
        if inflight is not None:
            inflight.release()


if __name__ == "__main__":
    main()
