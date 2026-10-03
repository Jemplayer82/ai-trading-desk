<p align="center">
  <img src="assets/hero.svg" alt="TradingAgents — multi-agent LLM trading intelligence, streamed live and self-hosted" width="100%">
</p>

# `$ ai-trading-desk`

**A self-hosted multi-agent LLM trading dashboard** — a team of AI analyst agents researches, debates, and decides in real time, streamed live to your browser and deployed as containers you own.

*A [Fathom Works](https://github.com/jemplayer82) project.*

<div align="center">
  <img alt="License: AGPL-3.0" src="https://img.shields.io/badge/License-AGPL--3.0-6cd5e6?labelColor=030d14">
  <img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-Uvicorn-009688?logo=fastapi&logoColor=white">
  <img alt="Docker" src="https://img.shields.io/badge/Docker-ghcr.io-2496ED?logo=docker&logoColor=white">
  <img alt="Ollama" src="https://img.shields.io/badge/LLM-Ollama_Cloud-000000?logo=ollama&logoColor=white">
  <img alt="SQLite" src="https://img.shields.io/badge/SQLite-WAL-003B57?logo=sqlite&logoColor=white">
</div>

> [!WARNING]
> For research and educational purposes only. Trading performance varies with the chosen models, data quality, and market conditions. This is not financial, investment, or trading advice.

<!-- TIER-IDENTITY BEGIN -->
> **This branch: `master` — Tier 4 — Full: + daily options paper trading (the complete product).**
> Development happens here. The reduced tiers are published as generated branches — see the tier table below.
<!-- TIER-IDENTITY END -->

---

## `[ tiers ]`

This project ships at four cumulative tiers, so you can run only the parts you
want. Each tier is a branch and a matching pair of container image tags.

| Tier | Branch | Adds | Images |
|---|---|---|---|
| **1 — Base** | `tier-1-base` | Single-ticker AI analysis, live agent streaming, charts, Q&A, Agent Bus | `tradingagents:tier1` + `tradingagents-web:tier1` |
| **2 — Brokerage** | `tier-2-brokerage` | + Schwab account connection, portfolio scanning, morning newsletter | `:tier2` |
| **3 — Scanner** | `tier-3-scanner` | + the daily S&P 500 research and paper portfolio | `:tier3` |
| **4 — Full** | `master` | + daily options paper trading | `:latest` / `:tier4` |

Tiers are cumulative — each contains everything below it. `master` is the only
branch anyone develops on; the three tier branches are **generated artifacts**,
produced by `scripts/make_tier.py` and force-pushed by
`.github/workflows/tiers.yml`, so never commit to them directly. At runtime the
`TIER` environment variable selects which features a container mounts (see
`web/features.py`).

---

## `[ overview ]`

This project runs a team of specialized LLM agents that mirror the desks of a real trading firm — analysts, researchers, a trader, and a risk/portfolio manager — and surfaces the whole pipeline in a real-time web dashboard. Submit a ticker and watch each agent stream its reasoning, ending in a BUY / SELL / HOLD decision with full reports.

<!-- TIER:2 BEGIN -->
Connect a Schwab account to scan a live portfolio, with a nightly sweep of every holding and a morning briefing in your inbox.
<!-- TIER:2 END -->
<!-- TIER:3 BEGIN -->
Let the scheduler research the S&P 500 every trading day (a shared 00:00 ET pass), deep-dive the highest-conviction names, and rebalance a $100k paper portfolio on its own after the open.
<!-- TIER:3 END -->
<!-- TIER:4 BEGIN -->
Turn on the daily options paper trader, which hunts S&P 500 movers every weekday and trades long calls and puts under hard risk guardrails, grading its own closed trades to learn from them.
<!-- TIER:4 END -->

The project ships as container images and deploys as a Portainer edge stack, backed by FastAPI services and a SQLite database. The repository is **self-contained** — the underlying TradingAgents agent framework is vendored in directly, so everything needed to build and run the dashboard lives in this repo with no dependency on the upstream project.

---

## `[ agent team ]`

Each agent owns a narrow slice of the decision and hands its findings to the next stage.

**Analysts** — four agents each study one angle of a ticker:
- **Fundamentals** — financial statements, valuation, and balance-sheet health
- **Sentiment** — news headlines and social chatter distilled into a single mood read
- **News** — macro events and market-moving headlines
- **Technical** — price action and indicators (MACD, RSI, Bollinger Bands)

**Researchers** — a bull and a bear argue the analysts' findings in a structured debate, weighing upside against risk.

**Trader** — synthesizes every report into a concrete call: direction, timing, and size.

**Risk Management & Portfolio Manager** — stress-tests the trade against volatility and liquidity, then the Portfolio Manager approves, trims, or rejects it before it reaches the (paper) book.

---

## `[ features ]`

**Web Dashboard**
- Terminal-aesthetic UI with dark theme and color-coded signals
- Tabbed interface:
  - **Run Analysis** — submit a ticker, watch the agents work
  <!-- TIER:2 BEGIN -->
  - **Portfolio Scan** — your live Schwab holdings, analyzed
  <!-- TIER:2 END -->
  <!-- TIER:3 BEGIN -->
  - **S&P 500** — the daily market-wide research and paper portfolio
  <!-- TIER:3 END -->
  <!-- TIER:4 BEGIN -->
  - **Options** — the daily options paper trader
  <!-- TIER:4 END -->
  - **Settings** — provider keys, app settings, and users
- Real-time WebSocket streaming of agent progress and reports
- Interactive technical charts with RSI, MACD, Bollinger Bands overlays
- Per-analysis Q&A thread (multi-turn conversation without re-running)
- Live **Agent Bus** feed — watch analysts, researchers, and the risk team communicate in real time as the pipeline runs

**Automation**
- Background job scheduler (APScheduler with cron expressions) running the jobs for whichever features this tier enables
<!-- TIER:2 BEGIN -->
- Schwab OAuth integration for brokerage account scanning
- Selectable data source — Schwab MCP server or built-in collection method (toggle in settings)
- Automated nightly portfolio analysis of all holdings, emailed as a morning briefing
<!-- TIER:2 END -->
<!-- TIER:3 BEGIN -->
- Shared daily S&P 500 research at 00:00 ET (pre-screen all ~500 tickers, quick scan top 150 + SPY, deep-dive top 50 + SPY) feeding a daily $100k paper portfolio allocation
<!-- TIER:3 END -->
<!-- TIER:4 BEGIN -->
- Daily options paper trader (pre-screen the whole S&P 500 → quick scan top 150 + SPY → deep-dive top 50 directional + SPY → long calls/puts with hard risk guardrails, real cash/realized-P&L ledger)
<!-- TIER:4 END -->

**Provider & Credential Management**
- Ollama Cloud as the deployed default backend, with 14+ LLM providers supported (OpenAI, Anthropic, Google, xAI, DeepSeek, Qwen, GLM, MiniMax, OpenRouter, Azure, Ollama, Mistral, custom)
- Dashboard API key management — add/update/delete provider keys without `.env`
- Dynamic model selection with custom model name input
- Secure credential storage in SQLite (masked in UI)

**Deployment Architecture**
- Container stack from pre-built images: backends/CLI (`tradingagents`), nginx web tier (`tradingagents-web`), and the Agent Bus (`mcp-switchboard`)
- nginx serves the SPA and reverse-proxies the FastAPI backends
- Dedicated scheduler container (APScheduler) running this tier's cron jobs
- SQLite with WAL mode for concurrent access and persistence
- Deployed as a Portainer edge stack; images built and pushed to `ghcr.io` by GitHub Actions CI

<!-- TIER:2 BEGIN -->
**Companion: [schwab-mcp](https://github.com/Jemplayer82/schwab-mcp)**
- Containerized Node.js MCP server that gives the dashboard a direct, real-time connection to your Schwab brokerage account
- Enables the dashboard to query live quotes, account positions, orders, and transaction history straight from Schwab
- Optional — a settings checkbox switches between the MCP server and the built-in data collection method
<!-- TIER:2 END -->

---

## `[ screenshots ]`

### Run Analysis

![Run Analysis — live multi-agent streaming](assets/screenshot-agent-team.jpg)

*Submit a ticker and watch each agent — Market, Sentiment, News, Fundamentals, Research, Trader, Risk, and Portfolio Manager — stream its progress in real time, ending in a BUY/SELL/HOLD decision with full reports.*

### Q&A & Technical Chart

<img src="assets/screenshot-qa-chart.gif" alt="Multi-turn Q&A and interactive technical chart" width="100%">

*Ask follow-up questions grounded in the saved analysis (e.g. "what would a good entrance point be?") and explore the interactive price chart with indicators.*

### Agent Team

<img src="assets/screenshot-run-analysis.gif" alt="Full agent team completed with strategy report" width="100%">

*Every analyst, the Research and Risk teams, the Trader, and the Portfolio Manager complete in sequence, producing a final decision and a scaling / risk-management strategy.*

<!-- TIER:3 BEGIN -->
### S&P 500 Scanner

![S&P 500 scanner with $100k paper portfolio](assets/screenshot-spy-scanner.jpg)

*A shared 00:00 ET research pass pre-screens all ~500 tickers and deep-dives the top 50 by conviction; each paper account then allocates its $100k paper portfolio from that research at its own configurable time (default 09:00 ET, set in the account modal), filling at live quotes after 09:35 ET, with live performance tracking.*
<!-- TIER:3 END -->

---

## `[ quick start ]`

### Prerequisites

- Docker & Docker Compose
- An Ollama Cloud API key (or another supported provider's key)
- Python 3.12+ (for local CLI development)
- Portainer (optional — for edge stack deployment on a home lab / remote host)

### Docker Deployment

The full stack is defined in the repo's `docker-compose.yml`, from pre-built `ghcr.io` images built by GitHub Actions. Clone, configure, and bring it up:

```bash
$ git clone https://github.com/Jemplayer82/ai-trading-desk.git
$ cd ai-trading-desk
$ cp .env.example .env

# Edit .env — at minimum set:
#   OLLAMA_API_KEY=        your Ollama Cloud key (https://ollama.com)
#   TOKEN_ENCRYPTION_KEY=  python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
#   SWITCHBOARD_MCP_TOKEN= python -c "import secrets; print(secrets.token_urlsafe(32))"

$ docker compose up -d
```

Open `http://localhost:8080`. The `tradingagents-web` nginx container serves the dashboard and reverse-proxies the API backends. On a Portainer host, browse to `http://<host>:8080`.

> [!TIP]
> **Portainer edge stack:** set all secrets in the stack environment — never in the committed compose file. After each fresh CI build, force-pull `:latest` on the host before redeploying so cached images aren't reused.

### Interactive CLI

The `tradingagents` service runs the interactive CLI from the same image as the backends, accessed via the Portainer console or `docker attach`:

```bash
$ docker run -it \
    -e OLLAMA_API_KEY=your_key \
    -e OLLAMA_BASE_URL=https://ollama.com/v1 \
    ghcr.io/jemplayer82/tradingagents:latest
```

### Local Development

```bash
$ pip install -e .
$ pip install -r requirements.txt

$ export TRADINGAGENTS_WEB_DB=./web.db
$ export OLLAMA_API_KEY=your_key
$ export OLLAMA_BASE_URL=https://ollama.com/v1

# Analysis API
$ uvicorn web.main:app --reload --port 8000

# TIER:2 BEGIN
# Scan API (separate process)
$ uvicorn web.portfolio_main:app --reload --port 8001

# TIER:2 END
# Scheduler (this tier's cron jobs)
$ python -m web.scheduler
```

The nginx `tradingagents-web` tier is only needed in Docker; in local dev hit the API ports directly.

---

## `[ features in detail ]`

### Run Analysis Tab

Single-ticker deep analysis with real-time streaming:

1. **Input Form** — Ticker, date, language, LLM provider, deep/quick models, research depth, **aggressiveness**, **decision bias**, analyst selection
2. **Progress Panel** — Live status of each agent via WebSocket
3. **Reports** — Market, sentiment, news, fundamentals, research plan, trader plan, final decision
4. **Technical Chart** — Price candles + RSI + MACD with interactive overlays
5. **Q&A Thread** — Multi-turn follow-up questions without re-running the full analysis
6. **Live Reasoning** — each agent's streamed train-of-thought (tool calls included), shown beneath the reports

> **Aggressiveness vs. bias.** *Aggressiveness* (1–10) controls how much risk the run takes — it sets debate depth (1–3 → 1 round, 4–7 → 2, 8–10 → 3). *Decision bias* (bullish / neutral / bearish) nudges the stance the agents lean toward on borderline calls — a suggestion, not a hard limit. The two are **independent**: aggressiveness = *how much*, bias = *which way*. Both are available wherever an analysis or scan can be launched.

<!-- TIER:3 BEGIN -->
> **Sizing.** In the equity paper portfolio, aggressiveness also drives position sizing: ≤3 → max 7% per position / 20% cash · 4–7 → 12% / 10% · 8–10 → 20% / 5%.
<!-- TIER:3 END -->
<!-- TIER:4 BEGIN -->
> **Options sizing.** For options paper accounts: ≤3 → max 5% premium per position / 15% total at risk · 4–7 → 8% / 30% · 8–10 → 12% / 50%.
<!-- TIER:4 END -->

<!-- TIER:2 BEGIN -->
### Schwab Tab

Connect your Charles Schwab account via OAuth 2.0 to enable automated portfolio scanning. Tokens are stored securely and refresh automatically.

**Data source toggle.** A checkbox in settings lets you choose how account and market data is collected:

- **Schwab MCP server** *(checked)* — Routes all Schwab requests through the [schwab-mcp](https://github.com/Jemplayer82/schwab-mcp) server for live quotes, positions, orders, and transaction history.
- **Built-in method** *(unchecked)* — Uses the dashboard's own market-data retrieval (yfinance) and does not connect to Schwab at all.

When the MCP server is enabled, account positions and market data come directly from your Schwab brokerage account; the built-in method instead pulls public market data via yfinance.

### Portfolio Scan Tab

Run and review scans of your real Schwab holdings:

- **Run Scan Now** — kick off an on-demand scan with its own **aggressiveness** + **decision bias** (the nightly cron time is configurable in the Run Scan Now box (default 22:00 ET) and runs with defaults)
- Aggregated portfolio briefing
- Per-ticker analysis cards with signals and rationales
- Links to full detailed analyses
<!-- TIER:2 END -->

<!-- TIER:3 BEGIN -->
### S&P 500 Tab

Daily paper portfolio built from one shared research pass per NYSE trading day:

- **Research (00:00 ET, shared)** — momentum/volume pre-screen ranks all ~500 S&P 500 tickers; the top 150 + SPY get the quick LLM scan and the top 50 BUY/SELL names + SPY get the full multi-agent deep dive. One automatic retry runs before 05:30 ET; if the research still fails, accounts skip the day
- **Allocation (per account, default 09:00 ET)** — each account's allocation waits for that day's research, then for the 09:35 ET open, and builds or rebalances its $100k portfolio at live quotes with position sizing
- **Low churn** — the daily allocator keeps existing holdings by default and only trades on a real change in conviction

Each account's allocation time is configurable in the account modal, and the AI agent rebalances the paper portfolio — adding, trimming, or exiting positions as it sees fit. Results include an interactive allocation table with entry prices and performance tracking. Hourly price refreshes also enforce that account's configured stop policy (none / stop / stop-limit / trailing % / trailing $) against its paper positions, booking a simulated exit into the snapshot with its realized P&L — simulated only, the app never places a real order.
<!-- TIER:3 END -->

<!-- TIER:4 BEGIN -->
### Options Tab

Daily options paper trader — long single-leg calls and puts on S&P 500 movers, 100% simulated with $100k per options paper account. Every trading day:

- **Shared research (00:00 ET)** — one research pass per trading day, shared with the S&P tab: momentum/volume pre-screen ranks the **whole S&P 500**; the top **150 + SPY** get the quick LLM scan; the top **50 directional names + SPY** (BUY *and* SELL — big losers become put candidates; SPY is deep-dived every run) get the full multi-agent deep dive
- **Per-account allocation time (default 09:00 ET)** — each account allocates from that day's research at its own configurable time
- **09:35 ET gate** — allocation waits for the market open so entries fill at live quotes
- **Contract selection** — deterministic, pre-LLM: ~21 DTE (10–45 window), ~0.45 delta via Schwab chains (near-ATM fallback on yfinance), liquidity gates against zero-bid / crossed / wide / illiquid quotes
- **LLM allocator** decides open / hold / close daily under **hard guardrails**: force-close at DTE ≤ 3 unconditionally, while the stop is now the account's configured policy (none / stop / stop-limit / trailing % / trailing $), backfilled to a 60% stop for existing options accounts, per-position and total-premium caps by aggressiveness, max 15 open positions, deterministic fallback if the LLM fails
- **Opt-in staged trail (options only):** choose “Staged trailing % (change after gain trigger)” in the account form. Base trail, gain trigger and stage trail are configurable (defaults 20/20/10); once peak premium is up 20%, the stop sits 10% below the highest premium. The stage trail accepts 1–99%, tighter or looser than the base. Edits apply to open positions at the next evaluation; the saved highest stop level never falls. Existing accounts keep their policies. Deployment and the prospective paper comparison are in [the runbook](docs/RUNBOOK.md).
- **Hourly marks** (10:00–16:00 + 16:45 ET) enforce the account's own stop policy intraday like standing orders, booked at the minute the level was crossed: a level crossed during the interval fills **at** the level, a gap straight through fills at the observed quote, and a **stop-limit** that gaps below its limit price rests until a later refresh quotes back at or above it. Stale/carried marks never trigger, and `TRADINGAGENTS_OPTIONS_INTRADAY_STOP=false` disables the whole intraday pass regardless of any account's policy. A **20:00 ET expiry settlement** sweep models OCC auto-exercise (ITM ≥ $0.01 settles at intrinsic vs the last close on/before expiry)
- **20:15 ET learning pass** — deep-dive directional calls are graded nightly against the underlying's forward alpha (the shared memory log), and every closed position gets a directional-vs-decay P&L attribution; once enough closes accumulate, a nightly batch reflection distills "watch for …" lessons that are injected into the next allocator run as context (hard risk limits are never relaxed by lessons)

Unlike the S&P tab's daily snapshot, options positions live in a real ledger — cash and realized P&L are tracked per contract through opens, closes, and expiries, with open/closed position tables, a daily decision log, and the allocator's report on the tab. Paper only: no order endpoints exist anywhere in the stack.

The tab also has an on-demand **Ticker Recommendation** box: type any ticker and get a specific contract pick with confidence (1–10), entry/target/stop premiums, horizon, thesis and risks. It runs the same momentum quick-read and contract-vetting pipeline as the daily scan, vets **both** the call and the put, and the advisor is briefed with the system's graded decision history and its own options lessons — every recommendation is itself stored and graded by the nightly learning sweep. Advisory only; nothing is traded.
<!-- TIER:4 END -->

### Credentials Tab

Two distinct things live here — they are **not** duplicates:

- **LLM provider API keys** (OpenAI, Anthropic, Google, xAI, …) — your model-provider secrets, masked in the UI (last 4 visible).
- **Settings groups** (Ollama & Bus Routing, Market Data, …) — non-key configuration. *Ollama & Bus Routing* holds the Ollama base URL + key (Ollama has no entry in the provider-keys list, so this is its only home) and the switchboard routing hints — **not** provider keys.

No `.env` editing required; saved values apply immediately and override the `.env`/compose fallback.

---

## `[ agent bus ]`

The **Agent Bus** mirrors every inter-agent handoff from the multi-agent pipeline onto a dedicated [mcp-switchboard](https://github.com/Jemplayer82/mcp-switchboard) instance running inside the stack, then streams the messages to a live feed panel in the dashboard. A visitor can watch the analysts deliver reports, the bull and bear researchers debate, the risk team stress-test the trade, and the portfolio manager reach a final decision — in real time, as the run happens.

The pipeline orchestrator stays in charge. The bus is a **read-only mirror** — agent-graph code is untouched; every tap lives in the `web/` layer.

### How it works

```
  Multi-Agent Pipeline             Bus Mirror                     Dashboard
  ────────────────────   ──────────────────────────────────   ──────────────────

  4 Analysts ─────────→  report deltas  → result messages   →┐
  Bull / Bear debate ─→  state changes  → chat turns         →├─ switchboard :3107
  Research Manager ───→  handoffs       → instructions       →│   analysis-{id}
  Trader ─────────────→  handoffs       → instructions       →│        │
  Risk team ──────────→  state changes  → chat turns         →│        │ /api/bus WS
  Portfolio Manager ──→  final result   → FINAL decision     →┘        ↓
                                                              [ Agent Bus ]  ●
  The graph stays the orchestrator.                           Orchestrator   instruction
  The bus is a read-only mirror of handoffs.                  Market Analyst result
                                                              Bull / Bear    chat
                                                              Portfolio Mgr  result
```

### Enabling the Agent Bus

The `switchboard` service is already defined in `docker-compose.yml`. Generate a bearer token and set one environment variable — it starts on the next `docker compose up`:

```bash
$ python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Add the output to your `.env` (or Portainer stack environment):

```bash
SWITCHBOARD_MCP_TOKEN=<your-generated-token>
```

The `switchboard` container starts automatically, `tradingagents-web` connects to it at `http://switchboard:3107`, and the **[ Agent Bus ]** panel appears live on the Run Analysis tab.

### Agent Bus Environment Variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SWITCHBOARD_MCP_TOKEN` | Yes | — | Bearer token for the in-stack switchboard. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `BUS_MIRROR` | No | `analysis` | Set to `off` to disable all bus publishing without stopping the switchboard container |
| `SWITCHBOARD_URL` | Auto | `http://switchboard:3107` | Resolved by compose — only override if running the switchboard outside the stack |
| `SWITCHBOARD_TARGET_AGENT` | No | `llm-router` | Bus agent that answers LLM requests when `LLM_PROVIDER=switchboard` — `llm-router` (built-in → Ollama/OpenAI) or `cleo` (external Claude CLI daemon) |
| `SWITCHBOARD_CHATGPT_AGENT` | No | `codex` | Bus agent that answers the ChatGPT models (`chatgpt:astra` etc.) — the Codex daemon below. Routing is per model, so one analysis can mix Claude and ChatGPT roles |

The bus is also published on **host port `3109`** (`docker-compose.yml`) so off-stack agents can connect at `http://<host>:3109/mcp`.

### Connecting Claude (streaming daemon)

With `LLM_PROVIDER=switchboard`, every LLM call goes as an `llm_request` DM on the bus to whatever agent is registered under `SWITCHBOARD_TARGET_AGENT`:

- **`llm-router`** (default) — built-in service, dispatches to Ollama / OpenAI-compatible backends.
- **`cleo`** — the included `scripts/cleo_llm_handler.py` daemon; drives your **local `claude` CLI in headless streaming mode** and **streams tokens live** to the dashboard as Claude generates them. Uses your Claude Code subscription session — **no Anthropic API key, no per-token billing.**

> ⚠️ The `SWITCHBOARD_MCP_TOKEN` bearer is the **only** gate on the `3109` host port. Keep it strong; don't expose it to the public internet without TLS in front.

#### Quick setup

> **Prerequisite:** run this on a machine where `claude -p "hi"` already works — i.e. Claude Code is installed and logged in (`claude` on PATH, or set `CLAUDE_BIN`). The daemon reaches the switchboard over HTTP, so it can run anywhere that can hit `SWITCHBOARD_URL`.

```bash
# 1. Install the one runtime dep (httpx — usually already present with Claude Code)
pip install httpx

# 2a. Quick/dev start — run inline, ctrl-c to stop
SWITCHBOARD_URL=http://<host>:3109      \
SWITCHBOARD_MCP_TOKEN=<your-token>      \
python scripts/cleo_llm_handler.py

# 2b. Production — run as a systemd service (auto-restart, log aggregation)
#     See deploy/cleo/README.md for the full install walk-through.
#     Short version:
sudo install -D -m 600 deploy/cleo/cleo.env.example /etc/cleo/cleo.env
sudo $EDITOR /etc/cleo/cleo.env          # set SWITCHBOARD_URL + SWITCHBOARD_MCP_TOKEN
$EDITOR deploy/cleo/cleo.service         # fill User, WorkingDirectory, ExecStart
sudo cp deploy/cleo/cleo.service /etc/systemd/system/cleo.service
sudo systemctl daemon-reload && sudo systemctl enable --now cleo
journalctl -u cleo -f                    # confirm "registered as 'cleo'"
```

```
# 3. In the dashboard → Settings → Ollama & Bus Routing:
#    Switchboard — LLM handler agent:   cleo
#    (leave "backend provider" blank — Cleo ignores it)

# 4. In the Analysis form, pick provider "Switchboard (Bus LLM)" and a model
#    (Sonnet, Opus or Fable) then run as normal.
```

> 📋 **Cleo env knobs** (set in `/etc/cleo/cleo.env` or as shell env vars):
>
> | Variable | Default | Purpose |
> |---|---|---|
> | `SWITCHBOARD_URL` | — | Switchboard base URL, no trailing `/mcp` (e.g. `http://host:3109`) |
> | `SWITCHBOARD_MCP_TOKEN` | — | Bearer token — must match `SWITCHBOARD_MCP_TOKEN` in the stack |
> | `SWITCHBOARD_AGENT_ID` | `cleo` | Bus agent name to register as |
> | `DEFAULT_MODEL` | `sonnet` | Fallback model when the request doesn't specify one |
> | `CLEO_CALL_TIMEOUT_S` | `150` | Hard per-call deadline in seconds — keep below the client's 180s so Cleo fails itself first |
> | `CLAUDE_BIN` | `claude` | Full path to the `claude` binary if it isn't on `PATH` for the service user |

The daemon handles up to 8 concurrent requests so back-to-back analyst calls during a scan don't block each other. Each `claude -p` call skips the host user's interactive setup (settings, hooks, plugins, skills and auto-memory via `--setting-sources "" --disable-slash-commands`), keeping effort at `high`: about 460 fixed tokens per call instead of ~3,400, and none of your personal hooks fire for background work. A single-instance flock guard prevents a second accidental copy from splitting the request stream. Cleo runs from a copy of the script, so updating it is copy + restart: `scp scripts/cleo_llm_handler.py host:/home/landon/cleo_llm_handler.py && ssh host sudo systemctl restart cleo` (the Codex daemon imports the same file as a helper: copy it to `/home/landon/codex-llm/` too and restart `codex-llm`). `scripts/cleo_llm_handler.py` in this repo IS the production handler — never patch the host copy alone.

> ⚠️ **Why Claude Haiku isn't offered as a Switchboard model.** The `claude` CLI
> has no way to accept a real tool schema over this path, so Cleo teaches the
> model an inline text marker instead (`<tool_call name="...">`). Haiku's grasp
> of that improvised protocol is unreliable: live-tested against production
> Cleo, it repeatedly claimed to be "still waiting" for tool results it was
> already holding, sending analysts back a status update instead of a report —
> even after a corrective retry. Sonnet and Opus didn't show this. Haiku still
> works fine as a **direct Anthropic-API** model (real tool binding, a
> different code path); it's excluded only from the free Switchboard/Cleo
> route. See the `switchboard` catalog entry in
> `tradingagents/llm_clients/model_catalog.py` for the technical detail.

#### Model menus: always the latest

The Switchboard dropdowns list model **families**, never pinned versions, so the
desk moves to each new release on its own:

| Entry | Handler | Resolves to (2026-09-27) |
|---|---|---|
| Sonnet / Opus / Fable (deep only) | Cleo | `claude-sonnet-5` / `claude-opus-5-5` / `claude-fable-5-1` via the CLI's family aliases |
| ChatGPT Astra / Sol / Luna / Terra | Codex | `gpt-6-astra` / `gpt-6-sol` / `gpt-6-luna` / `gpt-5.6-terra`, the newest listed model per family |

To pin a specific version, type its ID into **Custom model ID** (a Claude ID, or
`chatgpt:<model-id>` for ChatGPT).

### Connecting ChatGPT (Codex daemon)

`scripts/codex_llm_handler.py` is Cleo's ChatGPT twin. It registers as `codex`
and answers the ChatGPT entries by running `codex exec` on the host's
**Sign in with ChatGPT** session, so usage counts against your ChatGPT plan: no
OpenAI API key and no per-token billing. It reuses Cleo's bus and tool-marker
protocol; tool calls work on every family.

Codex is a coding agent, so every call is locked down: shell, exec, browser,
computer use, apps and plugins are disabled, the sandbox is read-only, and it
runs in an empty temporary folder. Codex's own instructions are replaced by a
one-line file, which brings the fixed overhead down to about 5,600 tokens per
call. Replies arrive in one piece rather than streamed.

```bash
sudo npm install -g @openai/codex && codex login --device-auth
codex exec "Reply OK"                     # must print OK
# then follow deploy/codex/README.md (service unit + env file, agent id "codex")
```

#### Streaming protocol

When `stream: true` is in the `llm_request` payload (the default), the handler sends one `llm_stream_chunk` DM per text delta:

```json
{ "type": "llm_stream_chunk", "content": "{\"delta\": \"some text\", \"done\": false}" }
```

A final chunk signals completion and carries any tool calls:

```json
{ "type": "llm_stream_chunk", "content": "{\"delta\": \"\", \"done\": true, \"tool_calls\": []}" }
```

The dashboard handles these automatically — tokens appear in each agent's report tab as they arrive.

---

<!-- TIER:2 BEGIN -->
## `[ schwab mcp ]`

[**schwab-mcp**](https://github.com/Jemplayer82/schwab-mcp) is a companion containerized Node.js MCP server that gives this dashboard a direct, real-time connection to your Schwab brokerage account. Rather than relying on manual data exports or delayed feeds, the dashboard communicates with Schwab through schwab-mcp to pull live quotes, account positions, open orders, and transaction history — enabling the scans and OAuth token management to work seamlessly.

Using schwab-mcp is optional. A checkbox in the Schwab settings lets you switch between the MCP server and the dashboard's built-in data collection method.

| Tool | Description |
|------|-------------|
| `get_quotes` | Real-time quote data for one or more symbols |
| `get_accounts` | Account balances and positions |
| `get_orders` | Open and historical orders |
| `place_order` | Submit equity orders |
| `get_transactions` | Account transaction history |
| `get_market_hours` | Market open/close status |

```bash
$ docker run -p 3000:3000 \
    -e SCHWAB_CLIENT_ID=your_client_id \
    -e SCHWAB_CLIENT_SECRET=your_secret \
    ghcr.io/jemplayer82/schwab-mcp:latest
```

See [Jemplayer82/schwab-mcp](https://github.com/Jemplayer82/schwab-mcp) for full setup and MCP client configuration.
<!-- TIER:2 END -->

---

## `[ data sources ]`

Where market, indicator, and account data come from:

- **yfinance** — default price/OHLCV plus locally-computed technical indicators (via `stockstats`). Built-in, free, no key.
- **Alpha Vantage** — *optional* pre-calculated technical indicators (SMA/EMA/MACD/RSI/Bollinger/ATR). Set **Technical indicators source** to `alpha_vantage` and add `ALPHA_VANTAGE_API_KEY`.
<!-- TIER:2 BEGIN -->
- **Schwab MCP** — live holdings/positions plus OHLCV quotes, feeding the local indicator calculation when **Use Schwab for market data** is on. Wired via OAuth + [schwab-mcp](https://github.com/Jemplayer82/schwab-mcp).
- **Alpaca** — ⚠️ **not implemented.** Credential fields exist in Settings, but there is **no** Alpaca holdings/quote/trade integration in the codebase yet; saving keys does nothing functional. Schwab is the only wired brokerage.
<!-- TIER:2 END -->

**Technical indicators source** (Settings → Market Data) picks the indicator vendor:

- `yfinance` *(default)* — indicators computed locally with `stockstats`. No key needed.
- `alpha_vantage` — indicators come pre-calculated from Alpha Vantage (requires `ALPHA_VANTAGE_API_KEY`).

<!-- TIER:2 BEGIN -->
> Schwab is an **OHLCV source**, not a pre-calculated-indicator source — it feeds the local `stockstats` calculation rather than returning ready-made indicators.
<!-- TIER:2 END -->

---

## `[ architecture ]`

### Deployment Topology

Containers built from pre-built `ghcr.io` images, deployed as a Portainer edge stack. The `tradingagents-web` nginx tier is the only published port; everything else talks over the internal Docker network:

```
Portainer Edge Stack
│
├─ tradingagents-web         ghcr.io/jemplayer82/tradingagents-web   (nginx)
│    host 8080 → container 8000  ·  static SPA + reverse proxy
│    └─ /api/* → the FastAPI backends
│
├─ tradingagents-api         ghcr.io/jemplayer82/tradingagents       (FastAPI · web.main:app · :8000)
│    single-ticker analysis · chart data · Q&A · Agent Bus
│    └─ depends_on switchboard
│
# TIER:2 BEGIN
├─ tradingagents-portfolio   ghcr.io/jemplayer82/tradingagents       (FastAPI · web.portfolio_main:app · :8000)
│    scan backend  (isolated so long scans don't block ad-hoc analysis)
│    └─ depends_on tradingagents-api
│
# TIER:2 END
├─ tradingagents-scheduler   ghcr.io/jemplayer82/tradingagents       (APScheduler · web.scheduler)
│    this tier's cron jobs
│
├─ tradingagents-llm-router  ghcr.io/jemplayer82/tradingagents       (bus → LLM backend bridge)
│
├─ switchboard               ghcr.io/jemplayer82/mcp-switchboard     (Agent Bus · :3107 internal)
│    read-only mirror of inter-agent handoffs → streamed to the dashboard via /api/bus
│
└─ tradingagents             ghcr.io/jemplayer82/tradingagents       (interactive CLI · console attach)

Volumes:      tradingagents_data (SQLite · cache · tokens) · switchboard_data (bus DB)
LLM backend:  Ollama Cloud  (https://ollama.com/v1, OLLAMA_API_KEY)
Images built by GitHub Actions → pushed to ghcr.io
```

Every FastAPI and CLI role shares **one image** (`tradingagents`) with different entrypoints; nginx (`tradingagents-web`) and the bus (`mcp-switchboard`) are the other images. Secrets live in the Portainer stack environment and are never committed.

### Data Models

- **Preferences** — user settings (LLM provider, models, language, analysts, research depth)
- **Analyses** — single-ticker runs with reports and signals (BUY/SELL/HOLD)
- **Provider Credentials** — API keys (encrypted, masked in UI)
<!-- TIER:2 BEGIN -->
- **Portfolio Scans** — batch analysis of Schwab holdings
<!-- TIER:2 END -->
<!-- TIER:3 BEGIN -->
- **S&P 500 Scans** — multi-phase SPX analysis with portfolio allocations
<!-- TIER:3 END -->
<!-- TIER:4 BEGIN -->
- **Options Positions & Ledger** — per-contract paper positions, cash ledger, and graded lessons
<!-- TIER:4 END -->

---

## `[ configuration ]`

```bash
# Database (auto-creates on the tradingagents_data volume)
TRADINGAGENTS_WEB_DB=/home/appuser/.tradingagents/web.db

# LLM backend — Ollama Cloud (deployed default)
OLLAMA_API_KEY=your_key
OLLAMA_BASE_URL=https://ollama.com/v1

# TIER:2 BEGIN
# Schwab OAuth
SCHWAB_APP_KEY=your_app_key
SCHWAB_APP_SECRET=your_app_secret
SCHWAB_CALLBACK_URL=https://your-host/api/auth/schwab/callback
# TIER:2 END
TOKEN_ENCRYPTION_KEY=<base64-32-bytes>

# Agent Bus (switchboard container)
SWITCHBOARD_MCP_TOKEN=<generated>
BUS_MIRROR=analysis

# Scheduler (tradingagents-scheduler)
SCHEDULER_TIMEZONE=America/New_York
DASHBOARD_URL=https://your-dashboard-host

# TIER:2 BEGIN
# Morning newsletter + alert email
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASS=...
NEWSLETTER_FROM=...
NEWSLETTER_TO=...

# TIER:2 END
# TIER:4 BEGIN
# Options learning loop (optional overrides; sensible defaults built in)
TRADINGAGENTS_DEEP_DIVE_STORE_DECISIONS=true   # kill switch: scan deep dives feeding the memory log
TRADINGAGENTS_OPTIONS_LESSONS_MIN_CLOSED=10    # min closed positions before stats reach the allocator
TRADINGAGENTS_OPTIONS_REFLECT_MIN_NEW_CLOSED=5 # min NEW closes before a nightly reflection fires
# TIER:4 END
```

> [!IMPORTANT]
> Every secret above must be injected via the Portainer stack environment or a local `.env` that is git-ignored — never committed to the repo.

---

## `[ technical stack ]`

| Layer | Technology |
|-------|------------|
| Web Tier | nginx — serves the SPA and reverse-proxies the API backends |
| Backend | FastAPI + Uvicorn |
| Frontend | Vanilla JS + HTML5 (no build step) |
| Database | SQLite with WAL mode |
| Charting | lightweight-charts |
| Task Scheduling | APScheduler (dedicated `scheduler` container) |
| Markdown Rendering | marked.js |
| Agent Bus | mcp-switchboard (streamable-HTTP MCP) |
| Containers | Docker — deployed via Portainer edge stack |
| CI/CD | GitHub Actions matrix → `ghcr.io` images |
| LLM Backend | Ollama Cloud (default), LangChain multi-provider abstraction |
| Stock Data | yfinance + 5-year caching |
| Technical Indicators | stockstats |

---

## `[ project structure ]`

```
ai-trading-desk/
├── tradingagents/
│   ├── graph/              # Core multi-agent graph
│   ├── dataflows/          # Data fetching & indicators
│   └── tools/              # LLM tool definitions
├── web/
│   ├── main.py             # tradingagents-api — analysis + Agent Bus
│   ├── features.py         # tier feature gates (TIER / FEATURES env)
│   ├── scheduler.py        # tradingagents-scheduler — this tier's cron jobs
│   ├── db.py               # SQLite schema
│   ├── credentials.py      # API key management
│   ├── llm_helpers.py      # Multi-provider LLM abstraction
# TIER:2 BEGIN
│   ├── portfolio_main.py   # tradingagents-portfolio — scan API shell
│   ├── portfolio_routes.py # portfolio scan routes + worker
# TIER:2 END
# TIER:3 BEGIN
│   ├── spy_scanner.py      # S&P 500 3-phase scanner
│   ├── spy_allocator.py    # $100k portfolio builder
│   ├── research_engine.py  # shared daily S&P 500 research
# TIER:3 END
# TIER:4 BEGIN
│   ├── options_engine.py   # per-account options allocation
│   ├── options_learning.py # closed-trade grading + lessons
# TIER:4 END
│   ├── bus.py              # Switchboard MCP client + resilient publisher
│   ├── bus_mirror.py       # Mirror agent handoffs onto the Agent Bus
│   └── static/             # SPA files (served by nginx)
│       ├── index.html
│       ├── app.js
│       ├── bus.js          # Agent Bus WebSocket client + live feed panel
# TIER:2 BEGIN
│       ├── portfolio.js
# TIER:2 END
# TIER:3 BEGIN
│       ├── spy.js
# TIER:3 END
# TIER:4 BEGIN
│       ├── options.js
# TIER:4 END
│       ├── credentials.js
│       └── styles.css
├── scripts/make_tier.py    # generates the tier branches from master
├── Dockerfile              # backends + CLI → ghcr.io/jemplayer82/tradingagents
├── Dockerfile.web          # nginx tier   → ghcr.io/jemplayer82/tradingagents-web
└── docker-compose.yml      # the container stack
```

```bash
$ pytest tests/ -v
```

---

## `[ troubleshooting ]`

### Chart endpoint returns 500 error

Legacy cached OHLCV CSV files have an `index` column instead of `Date`. Code normalizes this automatically. If it persists, clear the cache:

```bash
$ rm -rf ~/.tradingagents/cache/*.csv
```

<!-- TIER:2 BEGIN -->
### Schwab scan doesn't start

OAuth token not saved or expired. Click "Connect to Schwab" in the Schwab tab and complete the OAuth flow.
<!-- TIER:2 END -->

<!-- TIER:3 BEGIN -->
### S&P 500 scan hangs

Check the portfolio backend logs and verify the Ollama Cloud key:

```bash
$ docker logs tradingagents-portfolio
```
<!-- TIER:3 END -->

### API key not taking effect

Restart the analysis backend:

```bash
$ docker restart tradingagents-api
```

### Agent Bus panel stays empty

`SWITCHBOARD_MCP_TOKEN` not set or the `switchboard` container didn't start:

```bash
$ docker logs switchboard
```

### Stale image after a fresh CI build

```bash
$ docker compose pull && docker compose up -d
```

### Port 8080 already in use

Change the host side of the `tradingagents-web` port mapping in `docker-compose.yml`:

```yaml
ports:
  - "9090:8000"
```

---

## 🤝 Contributing

Pull requests welcome. See [CONTRIBUTING.md](./CONTRIBUTING.md) for the CLA, PR guidelines, and code style requirements (`ruff` + `pytest` must pass).

Areas of focus: UI/UX improvements, new LLM providers, chart enhancements, performance optimizations, testing, documentation.

---

## ⚖️ License

Licensed under the [GNU Affero General Public License v3.0](./LICENSE) (AGPL-3.0) — © 2026 Fathom Works.

Commercial licensing available. For inquiries: [github.com/jemplayer82](https://github.com/jemplayer82)

---

#### `[ credits ]`

This project builds on the open-source [TradingAgents](https://github.com/TauricResearch/TradingAgents) multi-agent framework, vendored and extended here with a full web dashboard, brokerage integration, and container-based deployment.

Powered by FastAPI, LangChain, yfinance, stockstats, lightweight-charts, APScheduler, and many other open-source libraries.

---

#### `[ why v2.0 ]`

This release jumps from 1.2.1 straight to 2.0 because it isn't another
feature — it's a full efficiency and reliability pass across the whole deep-
analysis and paper-trading pipeline, roughly 140 commits deep. The 2 equity
scans and 3 options-account builds that overlap most trading days used to
independently re-run the same expensive multi-agent pipeline for the same
tickers; they now share and reuse same-day results. Debate and research
prompts stopped re-embedding full reports on every round. The Research
Manager and every allocator moved off a deep model that had a documented live
failure mode onto the quick model the codebase already trusted more for
guardrailed synthesis. Options accounts no longer sit blocked behind one
account's market-open wait. The nightly outcome sweep, the quick scanner, and
the Agent Bus itself all cut their round-trip counts by an order of magnitude
or more. And the core orchestrator gained real parallel-analyst execution and
per-call LLM gating, engineered carefully around this project's own
documented host-memory limits. None of it changes what the dashboard does —
only how much it costs in time, tokens, and reliability to do it. That's a
big enough body of work under the hood to earn a major version number, even
without a breaking API change to point to.

<img src="assets/fathom-footer-banner.svg" alt="Fathom Works — sound the depths before you set a course" width="100%">
