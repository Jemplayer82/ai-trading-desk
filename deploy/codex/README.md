# Codex — ChatGPT handler for the switchboard

`scripts/codex_llm_handler.py` is Cleo's ChatGPT twin. It answers the desk's
`chatgpt` model (always the Codex CLI's current default model) and
`chatgpt:<model-id>` pins by running `codex exec` on the host's
**Sign in with ChatGPT** session. Usage counts against the ChatGPT plan; there
is no API key and no per-token bill.

The desk routes by model name: `chatgpt*` goes to the agent in
`SWITCHBOARD_CHATGPT_AGENT` (default `codex`), everything else to
`SWITCHBOARD_TARGET_AGENT` (Cleo). Pick ChatGPT for the quick role, the deep
role, or both in the dashboard.

## Install (on the Docker host, next to Cleo)

1. Codex CLI, signed in: `sudo npm install -g @openai/codex`, then
   `codex login --device-auth`. Check: `codex exec "Reply OK"`.
2. Code: copy `scripts/codex_llm_handler.py` **and** `scripts/cleo_llm_handler.py`
   (it imports Cleo's protocol helpers) into `/home/landon/codex-llm/`. This is
   separate from Cleo's own `/home/landon/cleo_llm_handler.py`, which is left alone.
3. Env: `/etc/codex-llm/codex-llm.env` (mode 600) with Cleo's
   `SWITCHBOARD_URL` and `SWITCHBOARD_MCP_TOKEN`, plus `SWITCHBOARD_AGENT_ID=codex`.
4. Service: install `codex-llm.service` into `/etc/systemd/system/`, then
   `systemctl daemon-reload && systemctl enable --now codex-llm`.

## Safety

Codex is a coding agent, so every call runs with shell/exec, browser, computer
use, apps and plugins disabled, a read-only sandbox, and an empty temporary
working directory. A prompt injected through market news cannot make it run
commands or read files.

## Cost

Each call carries ~12k tokens of Codex's own instructions on top of the desk's
prompt, and the reply arrives in one piece (no token streaming). The per-call
deadline is `CODEX_CALL_TIMEOUT_S` (default 170 s, below the desk's 180 s).
