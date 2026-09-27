# Running Cleo as a systemd service

Cleo (`scripts/cleo_llm_handler.py`) is the bridge that lets TradingAgents use a
**free local `claude -p` subscription session** as an LLM backend instead of the
paid Anthropic API. It registers on the internal switchboard bus as agent `cleo`,
receives `llm_request` DMs, drives the local Claude CLI, and streams replies back.

It runs as a **bare process on the host** (not in the Docker stack) because it
needs that host user's `claude login`. This unit gives it the three things a bare
process otherwise lacks: **auto-restart**, **log aggregation** (journald), and a
**single-instance guard** so a stray second copy can't split the request stream.

> ⚠️ Cleo must run as the user who ran `claude login`. Confirm first:
> ```sh
> sudo -u <that-user> claude -p "hi"
> ```
> If that doesn't print a reply, fix the CLI login before going further.

## Prerequisites

| Requirement | Notes |
|---|---|
| **Claude Code CLI** installed and logged in | `claude -p "hi"` must print a reply as the service user |
| **Python 3.9+** | Must be the interpreter in `ExecStart`; 3.12 recommended |
| **`httpx` package** | `pip install httpx` (or `pip3`, or `uv pip`) |
| **Network access to the switchboard** | `http://<host>:3109` — no TLS required for LAN; the bearer token is the gate |

## Install

```sh
# 1. Env file (secrets live here, mode 600 — never in git or the unit).
sudo install -D -m 600 deploy/cleo/cleo.env.example /etc/cleo/cleo.env
sudo $EDITOR /etc/cleo/cleo.env          # set SWITCHBOARD_URL + SWITCHBOARD_MCP_TOKEN

# 2. Edit the three CHANGEME lines in the unit (User, WorkingDirectory, ExecStart).
$EDITOR deploy/cleo/cleo.service

# 3. Verify the interpreter has httpx:
#    <interpreter> -c "import httpx"   # if it errors: pip install httpx

# 4. Install + start.
sudo cp deploy/cleo/cleo.service /etc/systemd/system/cleo.service
sudo systemctl daemon-reload
sudo systemctl enable --now cleo
```

## Verify

```sh
systemctl is-active cleo          # -> active
journalctl -u cleo -n 50 -f       # watch it register + handle requests
```

A healthy start logs `registered as 'cleo'`. A refused duplicate logs
`Another Cleo instance already holds … Exiting.` and stops — that's the
single-instance guard doing its job, not a bug.

## How Cleo stays available (keepalive)

Cleo holds its presence on the switchboard through a **long-poll loop**:

1. On startup it calls `register_agent` + `set_status` to announce itself.
2. It then calls `wait_for_message` with a 25-second timeout in a tight loop.
   Each poll refreshes its `last_seen` timestamp on the bus. The switchboard
   marks an agent offline after 60 seconds of silence, so the 25-second cadence
   keeps Cleo showing **online** continuously.
3. If a poll fails (network blip, switchboard restart), the loop catches the
   error, waits 2 seconds, then **re-registers and resumes polling** — no manual
   restart needed.

**What can take Cleo offline:**
- The process is killed / crashes (systemd restarts it automatically with `Restart=always`)
- The machine loses network to the switchboard for >60 seconds (auto-recovers once network returns)
- A second Cleo process starts and the flock guard kills it — the first one keeps running

**What does NOT affect availability:**
- The Docker stack restarting — Cleo is a bare host process, independent of the stack
- The switchboard container restarting — Cleo re-registers automatically on the next poll cycle

## Deploy a code update

> ⚠️ **There is no repo checkout on the WebServer.** This section used to say
> `cd /opt/TradingAgents && git pull`; that directory does not exist. The handler
> lives as a **bare file** at `/home/landon/cleo_llm_handler.py`, which is why the
> live copy silently drifted 99 lines ahead of the repo before anyone noticed.

Until the host has git credentials for this repo, deployment is a copy:

```sh
# From a machine with the repo, to the host:
scp scripts/cleo_llm_handler.py landon@192.168.7.50:/home/landon/cleo_llm_handler.py
ssh landon@192.168.7.50 'sudo systemctl restart cleo'
```

Always back up and verify the checksum matches what you intended to ship:

```sh
cp -a /home/landon/cleo_llm_handler.py /home/landon/cleo_llm_handler.py.bak-$(date +%Y%m%d)
md5sum /home/landon/cleo_llm_handler.py
```

The kernel releases the single-instance lock the moment the old process exits, so
the restart re-acquires it immediately — no crash loop.

**Worth fixing properly:** give the host a read-only deploy key or token and clone
the repo there, so deployment becomes `git pull && systemctl restart` and drift
cannot recur. That needs credentials set up by hand — it is not done yet.

## Knobs

All optional env vars are documented in [`cleo.env.example`](cleo.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `SWITCHBOARD_URL` | — | Switchboard base URL, no trailing `/mcp` (e.g. `http://host:3109`) |
| `SWITCHBOARD_MCP_TOKEN` | — | Bearer token — must match the stack's `SWITCHBOARD_MCP_TOKEN` |
| `SWITCHBOARD_AGENT_ID` | `cleo` | Bus agent name — change if you run multiple Cleo instances |
| `DEFAULT_MODEL` | `sonnet` (always-latest alias) | Fallback model when the request doesn't specify one |
| `CLEO_CALL_TIMEOUT_S` | `150` | Hard per-call ceiling in seconds — keep below the client's 180s so Cleo fails itself first |
| `CLAUDE_BIN` | `claude` | Full path to the `claude` binary if it isn't on `PATH` for the service user |
| `CLEO_LOCK_FILE` | `/tmp/cleo-<agent_id>.lock` | Single-instance flock path — override if `/tmp` is on a tmpfs that's shared across hosts |
| `CLEO_MAX_INFLIGHT` | `16` | Requests allowed in flight before Cleo replies `llm_error` instead of queueing. The executor queue is unbounded and each queued item holds a whole conversation, so a fan-out that outruns the workers would otherwise grow it without limit |
| `CLEO_REAP_GRACE_S` | `5` | Seconds allowed at each shutdown step (natural exit → SIGTERM → SIGKILL) when reaping the CLI's process group |

## 🧠 Memory behaviour

Every `claude -p` subprocess runs **inside cleo's cgroup**, so `systemctl status cleo`
reports the daemon *plus* all in-flight CLI calls. Measured on the WebServer:

| State | Cgroup memory |
|---|---|
| Idle daemon | ~18 MB |
| One call in flight | ~530 MB peak |
| Four concurrent calls | ~1.2 GB peak |

> ⚠️ That's why `MemoryMax` is set to **6G** and not something tight. A 1 GB ceiling
> looks reasonable next to the 18 MB idle figure and would OOM-kill Cleo on an
> ordinary four-way analyst fan-out. It's a runaway backstop, not a target.

Cleo reaps the CLI's whole **process group** (`start_new_session=True` + `killpg`),
escalating natural-exit → SIGTERM → SIGKILL. Previously it SIGKILLed the CLI alone
on *every* call; SIGKILL can't be caught, so Node never reaped its own children and
they reparented to PID 1 and accumulated. If you suspect that has regressed:

```sh
ps -eo pid,etimes,rss,args | grep [c]laude
```

Anything older than ~150 s (the per-call ceiling) is an orphan. The
[memwatch](../memwatch/README.md) sampler tracks this continuously as
`claude_stale` in `/var/log/memwatch/orphans.csv`.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `registered as 'cleo'` never appears in logs | `claude -p "hi"` fails as service user | Fix CLI login: `sudo -u <user> claude login` |
| `Another Cleo instance already holds…` then exits | Stale lock or second copy running | `systemctl status cleo` to confirm only one; `rm /tmp/cleo-cleo.lock` if stale |
| `Poll error: … retrying` loops forever | Wrong `SWITCHBOARD_URL` or bad token | Check URL is reachable from host: `curl http://<host>:3109/mcp` |
| Analysis hangs, no reply from Cleo | `claude -p` subprocess hung past timeout | Check `journalctl -u cleo -f`; watchdog kills it after `CLEO_CALL_TIMEOUT_S` |
| Cleo shows offline on the bus | Process not running | `systemctl start cleo`; check `journalctl -u cleo -n 20` for crash reason |

## Why bare instead of containerized?

Cleo drives the host's Claude Code subscription login (`~/.claude`).
Containerizing means mounting that credential dir read-write (for token refresh),
which is the one wrinkle that makes the host-process approach simpler. If you
later want it in the stack, the credential mount is the thing to validate first.

## Updating

The repo file `scripts/cleo_llm_handler.py` is the source of truth for the
deployed handler (it carries the inbox parking, auth-expiry alerting and the
lean `claude -p` flags). To update: copy it over `/home/landon/cleo_llm_handler.py`
**and** `/home/landon/codex-llm/cleo_llm_handler.py` (the Codex daemon imports it),
then `sudo systemctl restart cleo codex-llm`. The instance lock and the optional
debug dump live under `$XDG_RUNTIME_DIR` (or `~/.cache/cleo`), never `/tmp`.

