# Changelog

All notable changes to TradingAgents are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Breaking changes within the 0.x line are called out explicitly.

## [Unreleased]

### Added
- **ChatGPT via the switchboard.** New `scripts/codex_llm_handler.py` (deploy notes in
  `deploy/codex/`) answers `llm_request`s with `codex exec` on the host's ChatGPT
  sign-in, locked down (no shell/browser/apps, read-only sandbox, empty temp dir).
  The switchboard menus gain `chatgpt` (always the Codex CLI's current default
  model) in both roles; requests for `chatgpt*` route to `SWITCHBOARD_CHATGPT_AGENT`
  (default `codex`) while other models stay on Cleo, so quick and deep can use
  different subscriptions.

### Changed
- **Switchboard menus are always-latest only.** Pinned Claude snapshots were
  removed; the menus offer `sonnet`, `opus`, `fable` (deep only) and `chatgpt`.
  Pin a snapshot through Custom model ID.

### Changed — shared daily research

- **One shared research scan per trading day.** A single `kind='research'`
  scan runs once per NYSE trading day at `SCHEDULE_RESEARCH_TIME` (default
  `00:00` ET): the momentum/volume pre-screen ranks the whole S&P 500, the top
  150 movers + SPY get the quick scan, and the top 50 BUY/SELL names + SPY get
  the full deep dive. It feeds every options and S&P equity paper account. A
  failed or missing research run gets one automatic retry before 05:30 ET;
  otherwise accounts skip the day.
- **Per-account allocation rows.** Each account's scheduled allocation
  (default `09:00` ET) creates its own row that waits for the day's research
  (`running_wait_research`, deadline 10:30 ET), then for the 09:35 ET open, and
  all accounts allocate in parallel at live quotes (a per-account lock stops one
  account from allocating twice at once; options contract vetting is shared).
- **S&P equity accounts trade daily** with a low-churn allocator prompt; the
  weekly Saturday S&P scan is removed.
- **Bias-neutral research rating.** The Portfolio Manager rating is now
  bias-neutral; bull/bear bias is applied only in the allocators.
- **Schedule migration.** Scheduled `paper_accounts.schedule_time` values
  migrate to `09:00`; `NULL` stays manual-only.
- **New settings / env:** `SCHEDULE_RESEARCH_TIME`, `MARKET_HOLIDAYS_EXTRA`,
  `RESEARCH_DEADLINE_ET`, `RESEARCH_WAIT_MAX_MIN`, `RESEARCH_RETRY_CUTOFF_ET`,
  `RESEARCH_MAX_ATTEMPTS`, `SCAN_HEARTBEAT_SECONDS`.
- **Schema:** new column `spy_scans.research_scan_id` links each allocation
  row to the research row it consumed.
- **Stuck-scan reaper:** `STUCK_SCAN_STALL_MIN` default 60 → 120, and running
  scans now stamp a 2-minute heartbeat.
- **Market calendar:** new NYSE holiday calendar `web/market_calendar.py`
  (2026–27); jobs skip holidays and weekends.
- **Removed** the queue-slot hand-off between scans.
- **API:** `POST /api/spy-scan` now requires `account_id`; new
  `POST /api/research-scan`, `GET /api/research-scans/today` and
  `GET /api/research-scans`.

## [2.1.0] — 2026-08-14

Per-account automation scheduling and configurable paper stop-loss policies
for the S&P 500 and Options paper accounts, plus the Portfolio tab's nightly
Schwab scan time. Along the way, several real correctness bugs around
weekly equity rebalancing and stop-loss state were found and fixed.

### Added

- **Per-account scan scheduling.** Each paper account now carries its own
  `schedule_time` (`paper_accounts.schedule_time`, `HH:MM` 24-hour in the
  scheduler's timezone). The fixed `spy_scan`/`options_scan` cron jobs are
  retired in favor of one scheduled job per account, and a 60-second
  reconciler in `web/scheduler.py` picks up schedule edits and applies them
  live — no scheduler restart needed. Equity accounts' nightly cron time is
  now configurable via the new `SCHEDULE_NIGHTLY_SCAN_TIME` setting
  (Settings → Automation Schedule, default `22:00` ET); it still runs with
  defaults if left unset.

- **Per-account paper stop-loss policies.** A new shared stdlib-only module,
  `web/account_policy.py`, defines five stop types a paper account can be
  configured with — `none`, `stop`, `stop_limit`, `trailing_pct`,
  `trailing_dollar` — plus the shared crossed-vs-gap-through fill convention
  and the stop-limit arm/resting-fill state machine, so options and equity
  enforce stops through the exact same decision logic instead of two
  independently-drifting implementations. Enforced on both the options
  intraday-stop path (`web/options_allocator.py`) and the equity
  `refresh_portfolio_prices` path. Migration backfills existing **options**
  accounts to `stop` / 60 (matching the old fixed −60% floor) and existing
  **equity** accounts to `none` (equity had no stop enforcement before this).
  ⚠️ **Behavior change:** the old layered fixed −60% + arm/give-back options
  trailing model is retired outright — an account that relied on that
  trailing protection does **not** carry it forward automatically and must
  explicitly select `trailing_pct` or `trailing_dollar` as its `stop_type` to
  keep trailing-stop behavior. This remains simulation-only: no
  order-placement code exists anywhere in the stack, for either feature.

### Changed

- **Stopped-out tickers that reappear in candidates are now classified NEW instead of resurrected HOLD.** In `web/spy_allocator.py`, `run()` and `build_rebalance_user_message()` used to match candidates against a `prev_map` built from the raw previous portfolio, which still included `EXITED` rows. A ticker that was stopped out mid-week but appeared again in the current candidate list would therefore match as a `HOLD` at its stale pre-stop `entry_price`. The helper now builds `prev_map` from `live_positions(previous_portfolio)`, which drops `EXITED` rows; the ticker no longer matches, falls into `new_cands`, and is classified as `NEW` at the candidate's current price. This changes reported cost basis and realized P&L for that position going forward.

- **Stop-state now survives weekly equity rebalance via `carry_forward_state()`.** Previously, `web/spy_allocator.py` carried only `entry_price` (and later `current_price`) across a weekly rebalance for `HOLD`/`ADDED`/`TRIMMED` positions, while `peak_price`, `pending_stop_limit`, and `stop_limit_price` were reset each cycle. `carry_forward_state()` now also copies `peak_price`, `pending_stop_limit`, and `stop_limit_price` from the previous live row for every retained position (`CARRIED_STOP_STATE_KEYS`), and clamps `peak_price` to be at least the carried `entry_price`. Trailing stops now keep their ratchet high-water mark across rebalance weeks instead of resetting, and armed stop-limit orders stay armed instead of silently disarming.

- **Rebalance allocator prompt now surfaces positions stopped out mid-week.** `build_rebalance_user_message()` in `web/spy_allocator.py` now appends a `=== STOPPED OUT SINCE LAST REBALANCE ===` section (built with `REBALANCE_STOPPED_HEADER`, `STOPPED_TICKER_TEMPLATE`, and `stopped_positions()`) listing each stopped position's ticker, `exit_reason`, `entry_price`, and `exit_price`. The system prompt instructs the LLM that these positions are already closed and their capital is already reflected in starting capital, so re-entering one is a deliberate `NEW` position it should justify in its rationale. Previously the allocator never saw mid-week stop exits at all, which can change allocation decisions such as whether to re-enter a name that just stopped out.

### Fixed

- **Hourly equity price-refresh endpoint no longer treats empty portfolios as a total outage.** `POST /api/spy-scans/latest/refresh-prices` (called every weekday hour by `web/scheduler.py`) used to return HTTP 500 and fire an "all accounts failed" alert whenever every equity paper account simply had no portfolio yet — the normal state before the first weekly allocation or for an unallocated account. The root cause was that `web/spy_scanner.py::refresh_portfolio_prices` returned the same `{"error": ...}` shape both for genuine failures and for the benign "no portfolio yet" case, and the total-outage test `scans and all(isinstance(v, dict) and "error" in v for v in scans.values())` was duplicated in `web/spy_scanner.py::refresh_all_portfolio_prices` and in `web/spy_routes.py::refresh_spy_prices_latest`. The benign case now returns `{"skipped": "no portfolio yet"}` instead, and outage classification lives in a single helper, `web/spy_scanner.py::is_total_price_refresh_outage()`, used by both the fan-out alert path and the route's HTTP 500 decision. The helper returns True only when there is at least one account, none succeeded, and none of the non-successes were merely benign skips; a mix of one real error and one empty-portfolio account is correctly classified as a partial failure, not a total outage. `"scan not found"` still counts as a genuine error (not a skip): the fan-out selects scan ids from the database immediately beforehand, so a miss means the row vanished mid-run rather than representing a steady, benign state. A genuine total outage (every account's price refresh actually failing) still returns HTTP 500 and still fires the alert, unchanged. On the frontend, the S&P 500 tab no longer pops a spurious "Refresh failed: no portfolio yet" dialog when re-pricing a freshly-created scan, since that client-side check only fires on the `{"error": ...}` shape.

## [2.0.0] — 2026-08-09

Major version: this release is a full efficiency and reliability overhaul of
the deep-analysis and paper-trading pipeline — same-day dedup across
overlapping scans, prompt/context diet, wall-clock/architecture fixes,
reduced web/bus chatter, and parallel-analyst execution in the core
orchestrator, spanning 140+ commits. See the note at the end of the README
for the full rationale.

### Added

- **Trailing stop — winners ride, gains lock.** "Let it ride" now means what
  it should: no forced same-day profit-taking and no end-of-day exits, but a
  big win can't round-trip to the −60% floor. Every mark ratchets a
  `peak_premium` (seeded at entry); once the peak reaches entry × 1.5 the stop
  trails at peak × 0.7 (`exit_reason='trail_stop'`, "TRAIL" in the closed
  table). Enforced by both the hourly intraday pass (crossing-minute fills)
  and the daily allocator backstop. The allocator prompt now shows per-position
  **days held** and explicit WINNERS-RIDE guidance so the LLM stops cashing
  out green positions on day one. Knobs: `options_trailing_stop`,
  `options_trail_arm_pct` (0.50), `options_trail_give_back` (0.30), env
  `TRADINGAGENTS_OPTIONS_TRAILING_STOP` etc.

- **Intraday stop-loss enforcement (standing-stop emulation).** The −60%
  stop used to be checked only once a day at the 09:35 allocation, filled at
  that moment's price — a breach at 10:30 rode a full day past the stop. The
  hourly mark refresh now closes freshly-quoted positions that breach the
  stop: filled AT the stop level when the mark crossed it this interval (what
  a working stop order would have gotten), or at the observed quote when it
  gapped through (no pretending we caught a level the market never traded).
  The sale is BOOKED at the minute the level was crossed, not at the top of
  the hour the refresh noticed: the stop premium is mapped to an implied
  underlying level (linear between the two observed marks) and the underlying's
  1-minute bars are walked for the first adverse crossing — minute precision,
  because no intraday history exists for the option contract itself.
  `exit_underlying_source='backtracked'` marks these fills. Carried/intrinsic
  (stale) marks never trigger a stop. The daily allocator's forced-close
  remains as backstop. Kill switch: `TRADINGAGENTS_OPTIONS_INTRADAY_STOP=false`.

- **On-demand ticker recommendation.** New "[ Ticker Recommendation ]" panel on
  the Options tab (`POST /api/options-recommend`): type any ticker and get a
  specific contract recommendation with confidence — momentum quick-read, then
  BOTH the call and the put are vetted through the same Schwab-greeks/liquidity
  pipeline the daily scan uses, then one deep-LLM advisor call picks
  CALL / PUT / NO_TRADE with entry/target/stop premiums, horizon, thesis and
  risks. The advisor sees the ticker's graded decision history + global
  calibration and the account's own options lessons, and its directional call
  is stored in the memory log so the nightly sweep grades every recommendation.
  Hallucination-pinned (a rec can only reference an actually-vetted contract),
  deterministic fallback on LLM failure, advisory only — nothing is traded.

- **Options learning loop — the trader now learns from its own trades.** Two
  layers, each grading what it can actually measure:
  - *Layer 1 (System C):* scan deep dives (options weekdays + S&P Saturdays)
    now `store_decision` their directional calls into the memory log, so the
    nightly 23:30 outcome sweep grades them by forward alpha exactly like
    interactive analyses — previously the highest-volume decision path
    contributed nothing to calibration. Kill switch:
    `TRADINGAGENTS_DEEP_DIVE_STORE_DECISIONS=false`.
  - *Layer 2 (options ledger):* new `web/options_learning.py` grades every
    closed/settled position with a directional-vs-decay P&L attribution
    (`|entry_delta| × underlying move` vs the residual, honestly labelled
    "time/vol decay — theta+IV, not separable"), aggregates a track record
    (win rate by exit reason / DTE bucket / |delta| bucket, plus the
    right-direction-but-lost decay toll), and a nightly 20:15 ET
    `options_grade` job batch-reflects new closes in ONE quick-LLM call.
    The latest lessons + stats are injected into the allocator prompt as
    context (never rules — hard guardrails stay code-enforced). Gated behind
    minimum sample sizes (10 closed for stats, 5 new closes per reflection)
    with `[n<5 — ignore]` tags and a fixed small-sample caution.
  - Closes now capture the underlying spot (`exit_underlying`, source
    `live`/`eod_close`/`settlement`); missing spots backfill nightly.
  - SPY self-benchmark fix: a ticker whose alpha benchmark resolves to itself
    (SPY — deep-dived daily) now grades on absolute return instead of a
    degenerate always-zero alpha.
  - Memory-log writes are now serialized (thread lock + pid-unique temp file +
    read-retry) — deep dives write from a thread pool.
  - New CLI one-shot: `python -m web.scheduler --grade-options-now`.

- **Daily options paper trading.** New "Options" dashboard tab paper-trades
  long single-leg calls/puts on S&P 500 movers, $100k per options paper
  account. Every weekday (07:30 ET): momentum/volume pre-screen ranks the
  whole S&P 500, the top 150 movers + SPY get the cheap quick-LLM scan, the top
  50 directional names + SPY (BUY *and* SELL — puts need bearish candidates; SPY
  is deep-dived every run) get the full multi-agent deep dive, then after the
  open (09:35 ET gate for live
  quotes) a deterministic contract selector (~21 DTE, ~0.45 delta or near-ATM,
  liquidity gates against zero-bid/wide/illiquid quotes) feeds an LLM
  allocator that opens/holds/closes contracts under hard guardrails: force-
  close at DTE ≤ 3 or premium −60% (stop-loss), per-position and total-premium
  caps scaled by aggressiveness, max 15 open positions, and a deterministic
  conviction-ranked fallback when the LLM fails.
  - Positions and cash live in new normalized tables (`options_positions` +
    append-only `options_cash_ledger`) with transactional, idempotent
    open/close/settle helpers — real realized-P&L accounting, unlike the
    equity snapshot portfolio.
  - Expiry settlement models OCC auto-exercise (ITM ≥ $0.01 settles at
    intrinsic vs the last close on/before expiry; catch-up safe after
    downtime), swept nightly at 20:00 ET and opportunistically before every
    build/refresh.
  - Marks refresh hourly via Schwab bulk option quotes (new
    `getOptionChain`/`getOptionExpirationChain` wrappers and an option-safe
    `mark → mid → last` price extractor in
    `tradingagents/dataflows/schwab_mcp.py` — still zero order tools, paper
    only) with a yfinance chain fallback; missing quotes carry the last mark
    floored at intrinsic, never zero.
  - Options runs are `spy_scans` rows with a new `kind` column, so quick/deep
    progress bars, cooperative cancel, and the stuck-run reaper work
    unchanged; `paper_accounts.kind` separates equity and options accounts.
  - Three new scheduler jobs (scan / marks / settlement) with `--run-*-now`
    CLI flags, `/api/options*` routes + nginx location, and
    `web/static/options.js`.
  - Options runs participate in the scan-serialization queue: they enqueue
    behind a running portfolio/S&P scan and vice versa, and the dequeuer
    dispatches `kind='options'` rows to the options build (not the equity
    pipeline).

- **Same-day deep-dive and quick-scan reuse.** The 2 equity S&P scans + 3
  options-account builds that run most trading days independently re-ran the
  full multi-agent pipeline for the same ticker on the same day. A
  `config_fingerprint` (hash of every shared-stage LLM config field) now lets
  `web/spy_scanner.py` find an existing same-day analysis for a ticker with an
  identical fingerprint and re-run only the account-specific Portfolio
  Manager step (`SwitchboardOrchestrator.rerun_decision`) against it instead
  of the whole pipeline — this account's own `bias_context`/`past_context`
  always win, never a blind copy of the donor's decision. Quick-scan results
  (signal/conviction/reasoning) are reused the same way. Knobs:
  `deep_dive_reuse` / `quick_scan_reuse` (default on),
  `deep_dive_reuse_max_age_hours` (default 6). A reuse attempt that fails for
  any reason (corrupt donor state, empty rerun output, provider error) falls
  back to a full pipeline run rather than failing the dive; the `analyses` row
  is only created after a reuse rerun actually succeeds, so a failed attempt
  never leaves an orphaned "running" row for the stuck-run reaper to clean up.

- **Shared macro news brief for options deep dives.** Every options build's
  deep-dive phase used to have each ticker's own News Analyst independently
  fetch and summarize the same day's global news. `web/spy_scanner.py` now
  fetches `get_global_news` and summarizes it once per scan
  (`_compute_macro_brief`), and the News Analyst is handed the pre-fetched
  brief instead of calling `get_global_news` itself when one is available —
  wrapped in explicit `<start_of_global_news>`/`<end_of_global_news>`
  delimiters plus anti-injection instruction text, since the brief is
  untrusted third-party content flowing into an agent prompt. A vendor
  error/no-news string is never treated as a usable brief. Kill switch:
  `macro_brief_enabled` (default on).

- **Same-day market-data caches.** A shared `SameDayCache` primitive
  (`web/market_cache.py` — trade-date-scoped, TTL, lock-guarded, never caches
  an empty or badly-degraded result) now backs three separate caches so the
  day's 2 equity + 3 options builds stop re-fetching identical data: the
  S&P 500 movers pre-screen (`options-prescreen`, 4h TTL, requires ≥80%
  download completeness), the quick-scan bulk price-data download (15 min
  TTL, same 80%-completeness gate, counted by usable closes per ticker so a
  partial yfinance failure can't poison the cache), and the selected
  option-contract cache (2h TTL) — a same-day cache hit on the contract cache
  is never served blind: bid/ask are refreshed via one live quote and
  re-validated against the liquidity gates *and* a moneyness/delta drift
  check before being served, falling back to a full re-fetch if either check
  fails, so a contract that has drifted since it was first selected is never
  silently reused.

- **Same-day outcome-resolution price-history cache.** The nightly outcome
  sweep (`tradingagents/graph/outcome_resolution.py`) used to call
  `yf.Ticker(...).history()` once per pending memory-log entry — a ticker
  with 3 pending entries triggered 3 separate stock downloads and 3 separate
  benchmark downloads, with every ticker's SPY benchmark re-downloaded
  redundantly across the whole sweep. A lazy per-sweep `PriceHistoryCache`
  now fetches each distinct ticker/benchmark's price history at most once per
  sweep run and slices per-entry sub-windows out of it, plus an age-guard
  that skips a fetch entirely for entries that are guaranteed-immature by
  their trade date alone. `fetch_returns`'s public, fetch-it-yourself
  signature (used directly by `tradingagents/graph/trading_graph.py`) is
  unchanged. Cuts a full nightly sweep from ~400 yfinance calls toward
  ~60–100.

- **Nightly reflections gated by ticker relevance.** LLM reflections in the
  outcome sweep used to run for every matured, non-noise, non-censored
  pending entry regardless of whether anyone would ever read the lesson.
  `resolve_all_pending` now accepts an optional `relevant_tickers` set (the
  current paper-account holdings, `ALWAYS_DEEP` tickers, and anything
  recently deep-dived); a ticker outside that set gets a free canned
  `NOT-RELEVANT` reflection instead of an LLM call, the same free-path
  mechanism NOISE/CENSORED already use. Callers that don't pass the
  parameter (e.g. direct `trading_graph.py` callers) see no behavior change.
  The relevance computation (`web/scheduler.py::_sweep_relevant_tickers`)
  fails open to `None` (ungated) the moment *any* of its DB-backed sources
  errors, not just if all of them do — a partial DB outage must never
  silently and irreversibly canned-resolve real pending decisions.

- **Batched quick-scan LLM calls.** `run_quick_scan` used to make one LLM
  round-trip per ticker across up to ~500 tickers. On the deployed
  Switchboard/Cleo bus route each call is a full external process
  round-trip, so call count — not token volume — was the bottleneck. Tickers
  are now grouped into batches of ~20–25 and scored with one LLM call per
  batch (`TICKER|SIGNAL|CONVICTION|reason` lines in, parsed leniently
  per-line — a malformed or missing line degrades just that ticker to
  HOLD/conviction-5, distinct from the existing insufficient-data HOLD/1,
  and is never silently donated to another same-day scan via the reuse
  cache); a single leftover ticker still goes through the original
  `_quick_scan_one` per-ticker path. Cuts a full ~500-ticker scan from
  ~500 round-trips toward ~20–25.

- **Parallel analyst execution.** `SwitchboardOrchestrator` now honors the
  pre-existing `analyst_concurrency_limit` config knob, which was plumbed but
  previously read by nothing. When set above 1, selected analysts run on
  worker threads, each with its own isolated `messages` list; report merging
  happens only on the main thread as each future resolves. Node labeling is
  thread-local (`self._current_node` backed by `threading.local()`), so
  streaming token frames are always attributed to the analyst that produced
  them. The knob remains **default 1** in
  `tradingagents/default_config.py`; raising it in a deployed scan config is
  a separate operational decision.

### Changed

- **Research Manager and allocators moved off the deep model by default.**
  The Research Manager now runs on the quick model by default
  (`research_manager_role` = `"quick"`|`"deep"`, env
  `TRADINGAGENTS_RESEARCH_MANAGER_ROLE`) — instant rollback via the knob if
  synthesis quality regresses. The options/S&P/portfolio allocators
  (`web/options_allocator.py`, `web/spy_allocator.py`,
  `web/portfolio/aggregator.py`) were switched from the deep model to the
  quick model outright: this repo's own history has a documented live
  failure where the deep allocator blew Cleo's 150s call budget and silently
  fell back to an equal-weight allocation, and the deep model brought no
  benefit to a guardrailed, deterministic-fallback JSON-synthesis task the
  repo already documents the quick model as more reliable for.

- **Debate/research prompts stop re-embedding full analyst reports every
  round.** The bull/bear researchers and all three risk debators embedded
  the 4 full analyst reports (or all 4 risk-team raw reports) in their
  prompt on every round of debate, not just the first — the growing debate
  history already carries the same information, speaker-labelled. Reports
  now embed only on each side's first call
  (`investment_debate_state`/`risk_debate_state` `count == 0`); later
  rounds get history plus the synthesized `investment_plan`. Duplicate
  "last argument" prompt lines (redundant with what the embedded history
  already ends with) were also dropped. Memory-log DECISION context embedded
  in agent prompts is now truncated to `memory_context_decision_max_chars`
  (default 400 chars; REFLECTION text is untouched) — cuts ~2K tokens per
  Portfolio Manager call on recurring tickers. `get_YFin_data_online`
  downsamples returned price history (daily rows for the most recent ~60
  sessions, weekly beyond) instead of always returning full daily
  resolution.

- **Options scan queue no longer blocks other accounts during the
  market-open wait.** A build parked in the 07:30–09:35 ET market-open wait
  used to hold the scan-queue slot for the whole ~2h window, so accounts 2
  and 3 couldn't even start computing until account 1's full run (including
  its wait) finished. The wait status no longer counts as "busy"
  (`scan_queue._is_any_scan_running`), and a build proactively hands off the
  queue slot the instant it enters the wait, so queued accounts start
  computing pre-market instead of ~25–45 minutes late. The POST-wait
  allocation/order-placement phase is now serialized behind a module-level
  `_ALLOC_LOCK` (accounts still allocate one at a time, in the order they
  finish waiting) with a configurable hard timeout
  (`OPTIONS_ALLOC_TIMEOUT_SECONDS`, default 3600s) so one stalled allocator
  can't wedge the others forever. Kill switch:
  `OPTIONS_RELEASE_SLOT_DURING_WAIT` (default on) reverts both the busy-check
  change and the proactive hand-off together — the redeploy pre-flight guard
  (`scripts/redeploy.py`) and the frontend queue banner were both updated to
  treat a market-open-waiting scan as "still alive, don't deploy over it."

- **Nightly portfolio holdings scan and option-chain contract fetches
  parallelized.** The nightly per-holding scan (`web/portfolio_routes.py`)
  ran every holding's full `SwitchboardOrchestrator.run()` strictly
  sequentially; it now runs through the same `ThreadPoolExecutor` +
  `DynamicGate` concurrency pattern already used for options deep dives,
  cutting a 1.5–3h run toward ~20–40 minutes. `fetch_candidates`
  (`web/options_data.py`) now fetches each candidate's option chain
  concurrently (up to 6 workers) instead of one at a time, while preserving
  deterministic input-order results.

- **Bus poll cadence and browser chatter reduced.** The Agent Bus bridge's
  default server-side poll interval rose from 1.0s to 5.0s
  (`BUS_POLL_INTERVAL` env override still works) — cuts upstream polling
  from ~86,400 to ~17,000 iterations/day per open tab. `bus.js` now drops
  its WebSocket entirely while a tab is hidden/backgrounded (including a tab
  that loads already-hidden, e.g. session restore) and reopens it on
  return, so a background-restored tab costs the server nothing. The
  scan-activity banner (`pollScanActivity` in `web/static/portfolio.js`)
  switched from polling two full 50-row scan-history lists every 5s to the
  cheap `/api/portfolio/status` endpoint already used elsewhere on the page,
  and a market-open-waiting options build now renders correctly instead of
  being mislabeled as an S&P scan.

- **Deep-dive gate permits move from whole-dive to per-LLM-call.** Inside
  `SwitchboardOrchestrator(gate=...)`, every `llm.invoke()` now acquires
  exactly one `DynamicGate` permit for its own duration, so a dive waiting on
  a news or price fetch no longer squats on LLM capacity. The gate capacity
  itself (`max(1, TOTAL - active_singles)`) is unchanged, and `run_deep_dives`'
  worker pool width tracks it 1:1 by default — widening the pool beyond that
  is a **separate, explicit opt-in** (`DEEP_DIVE_POOL_MULTIPLIER`, default 1 =
  no widening), deliberately decoupled from the gating-position switch: the
  portfolio container has prior host-level OOM history under its 4g limit, so
  per-call gating (cheap) and pool widening (a real memory cost) don't move
  together just because one enables the other. A second `DynamicGate`
  (`DEEP_DIVE_TOOL_CONCURRENCY`, defaults to `budget`) separately bounds
  concurrent outbound tool-fetch calls (yfinance/Alpha Vantage/Reddit/
  Stocktwits/Schwab MCP), restoring the order-of-magnitude bound the old
  whole-dive permit used to provide there too.
  **`DEEP_DIVE_PER_CALL_GATING=0`** is the rollback switch for the gating
  position itself: it reverts to one gate permit per whole dive, because a
  whole-dive permit plus an inner per-call permit would deadlock.
  `SwitchboardOrchestrator(gate=...)` remains optional and defaults to
  `None`, so the CLI and single-ticker web runs are ungated and unchanged.

- ⚠️ `memory_log_max_entries` default raised 300 → 1000: deep-dive volume
  (~50 entries/weekday) churned the 300-entry rotation window in under 6
  trading days, evicting all interactive/portfolio history. Revert by setting
  the old value in `tradingagents/default_config.py` (or trim the log file).

### Fixed

- **Background scans silently did nothing on a fresh database.** `build_config`
  defaulted the *provider* to Ollama while `DEFAULT_CONFIG` supplied OpenAI
  *model* names, so with no saved preferences every background LLM call 404'd
  (`model "gpt-5.4-mini" not found`). Interactive runs were unaffected because
  the form always posts an explicit model — which is also the only thing that
  ever wrote preferences, so the bug was invisible on any instance that had run
  one analysis. `build_config` now resolves a provider-appropriate default from
  the provider's own catalog, after any explicit param and any
  `TRADINGAGENTS_*_THINK_LLM` operator override, and only when the configured
  name belongs to a different provider.
- **Four retired models were still offered in the model picker.** Ollama Cloud
  retires models and returns HTTP 410 for them; `kimi-k2:1t-cloud` (the *deep*
  default), `glm-4.6:cloud`, `deepseek-v3.1:671b-cloud` and
  `qwen3-coder:480b-cloud` had all gone dead. Catalog refreshed against the
  live model list, with the revalidation command documented alongside it.
- **A totally failed scan reported success.** Per-ticker errors degrade to
  HOLD/conviction-1 by design (one bad ticker must not sink 500), but nothing
  inspected the aggregate — so a dead backend was indistinguishable from a
  quiet market and the run completed green with an empty portfolio and no
  alert. Quick scans now fail the run when ≥50% error, deep dives when 100%
  fail (the stricter bar reflects that partial deep-dive failure is normal),
  and the alert quotes the underlying error. Missing price data carries no
  `error` key, so it never counts toward the rate.
- **`get_global_news` raised on `None` arguments instead of using config
  defaults.** Every other vendor path in `tradingagents/dataflows/` already
  substituted `DEFAULT_CONFIG` values when `look_back_days`/`limit` came in
  as `None`; the Alpha Vantage path was the one exception and raised
  `TypeError` instead, surfaced while building the shared macro news brief
  (above) since that path calls it directly rather than through an agent
  tool wrapper that always supplies explicit values.
- **The scan-activity banner never displayed.** `pollScanActivity`
  (`web/static/portfolio.js`) checked `Array.isArray(scans)` against the
  response of `/api/portfolio-scans` and `/api/spy-scans`, but both endpoints
  return a `{"scans": [...]}` envelope, not a bare array — so the check was
  always false and the "[ Scan in progress ]" banner never rendered,
  regardless of whether a scan was actually running. Fixed as part of the
  banner's move to the cheaper status endpoint, above.
- **Failed analyses could be traded on.** A crashed deep dive keeps the
  quick-scan `signal`/`conviction` in its result row, and neither the options
  contract vetter nor the equity allocator inspected `error` — so a partial
  deep-dive outage could open paper positions off analyses that never
  completed. Both paths now drop errored rows before allocation.
- **Zero-trade runs now explain themselves** in the allocator report, telling
  "nothing scored directional" apart from "dives failed" and "nothing passed
  contract vetting".
- **Deep-dive Overweight/Underweight calls were silently discarded before
  contract vetting.** The options vetter only recognized literal `BUY`/`SELL`,
  but a deep dive's own final rating is the system-wide 5-tier scale (`Buy`,
  `Overweight`, `Hold`, `Underweight`, `Sell` — `rating.py`), and only the two
  most extreme tiers matched. `Overweight`/`Underweight` — real, if less
  extreme, directional calls — never reached `fetch_contract`, and the skip
  path logged no note, so a scan with dozens of confident directional calls
  reported "no contract passed liquidity/delta/DTE vetting" despite never
  actually trying to vet most of them. This had been quietly starving the
  daily options build since launch (`Buy`/`Sell` alone landed 0-8 of ~40-50
  deep dives most days); it just took a run of days with zero extreme-tier
  ratings to make it fully visible. `Overweight` now maps to the CALL/BUY
  side, `Underweight` to PUT/SELL. The zero-candidate explainer also now
  distinguishes "every deep dive rated Hold" (nothing to vet) from "some
  rated directional but none survived vetting" (a real vetting failure).
- **Scan queue never dequeued.** `_dequeue_next_scan`'s `ORDER BY created_at`
  referenced a column absent from its UNION's result set — SQLite raised
  `OperationalError` on the first queued scan, so queued runs sat forever.
  Also closed the create→start race by counting `pending` scans as busy
  (back-to-back scan requests could previously both start).

## [1.2.1] — 2026-07-07

### Added

- **Login brute-force throttling.** Failed dashboard logins are recorded per
  username and per client IP (`login_attempts` table); 5 failures per username
  or 20 per IP within a 15-minute window answer 429 before any PBKDF2 work.
  Closes the High-severity item deferred in `SECURITY_AUDIT.md`.

### Removed

- Orphaned root `test.py` scratch script and the completed
  `tradingagents/llm_clients/TODO.md`.
- Unused dependencies `redis`, `backtrader`, `parsel`, `pytz`; `setuptools`
  demoted to `[build-system]` only. Slims the install and the Docker image.

### Changed

- Shipped refactor plans archived under `docs/history/`.
- ⚠️ **`memory_log_max_entries` now defaults to `300` (was `None` = unbounded).**
  Resolved decision-log entries beyond the cap are pruned oldest-first on the
  next write; pending entries are never pruned. This matters on upgrade: the
  new nightly outcome sweep resolves the backlog that previously sat pending
  forever, so the first sweep on a long-running deployment can cross the cap
  and drop old history in one pass. Set `memory_log_max_entries: None` in the
  config to keep the old unbounded behaviour (there is no env-var override for
  this key), and back up `~/.tradingagents/memory/trading_memory.md` first if
  that history matters to you.

## [1.2.0] — 2026-06-18

Version jumped 1.0.0 → 1.2.0 with the switchboard-orchestrator line; there was
no 1.1.x release.

### Added

- **Switchboard as universal LLM gateway.** Analysis LLM calls can route over
  the Agent Bus (`llm_request`/`llm_response`) to a pluggable backend; the
  `tradingagents-llm-router` compose service bridges to Ollama/OpenAI.
- **Cleo daemon** (`scripts/cleo_llm_handler.py`): bare-host bus responder that
  streams tokens from the `claude` CLI (no API key needed), with single-instance
  flock guard, stderr-drain + watchdog against subprocess hangs, and a systemd
  unit under `deploy/cleo/`.
- **Live token streaming** to the dashboard via the bus mirror.
- **Multi-account S&P 500 paper trading**, per-tab aggressiveness/bias
  controls, and a technical-indicator selector.
- **Portainer redeploy tooling** (`scripts/redeploy.py`,
  `scripts/render_stack_payload.py`) — repo compose is the source of truth for
  the deployed stack.
- `OLLAMA_MAX_CONCURRENCY` setting exposed in the UI registry.

### Changed

- **`SwitchboardOrchestrator` replaces the langgraph path for all web runs**
  (single-ticker, portfolio, S&P scans). The CLI still uses
  `TradingAgentsGraph`; langgraph removal is deferred until the CLI's
  streaming migration.
- Bus agent id renamed `langgraph-orchestrator` → `switchboard-orchestrator`.

## [1.0.0] — 2026-06-14

First release of the web-dashboard era (the repo's front-end line; core
framework changes continue to be listed per-feature below).

### Added

- **FastAPI + nginx dashboard**: Run Analysis (streamed over WebSocket),
  Portfolio Scan, S&P 500 Scanner, and Settings tabs; vanilla-JS SPA.
- **Dashboard auth**: PBKDF2 password login with server-side sessions,
  first-run setup flow, internal-token service auth.
- **Schwab integration**: OAuth (CSRF-protected), encrypted token store,
  live holdings with cost basis, optional MCP data source toggle.
- **Scheduler container**: nightly portfolio scan (Mon–Fri), newsletter
  email, token health checks, run-failure alerts (webhook + email).
- **Agent Bus** (mcp-switchboard) with the Live Reasoning panel.
- **Whole-share paper trading** for the S&P scanner with per-position P&L.
- **Security hardening** (rounds 1–3 in `SECURITY_AUDIT.md`): Fernet
  encryption at rest for credentials, XSS escapes at every render site,
  DOMPurify for LLM markdown, timing-safe token comparisons, SQL allow-lists.
- **CI gates**: ruff + pytest on every PR; README claims guarded by tests.

### Changed

- License corrected to **AGPL-3.0** (Fathom Consulting LLC).
- Deploys standardized on prebuilt `ghcr.io/jemplayer82/*` images; six-service
  docker-compose stack.

## [0.2.5] — 2026-05-11

### Added

- **Grounded Sentiment Analyst.** The renamed `sentiment_analyst` now reads
  real Yahoo News, StockTwits, and Reddit data before generating its report,
  replacing the prior flow that could fabricate social posts under prompt
  pressure. (#557, #607)
- **MiniMax provider** with the full M2.x catalog (M2.7 / M2.5 / M2.1 / M2
  plus highspeed variants, 204K context). Dual-region: Global
  (`MINIMAX_API_KEY`) and China (`MINIMAX_CN_API_KEY`).
- **Dual-region Qwen and GLM** with separate keys per region — international
  (`DASHSCOPE_API_KEY`, `ZHIPU_API_KEY`) and China (`DASHSCOPE_CN_API_KEY`,
  `ZHIPU_CN_API_KEY`), selectable via a secondary region prompt. (#758)
- **`TRADINGAGENTS_*` env-var configurability for `DEFAULT_CONFIG`.** Override
  `llm_provider`, deep/quick model IDs, `backend_url`, `output_language`,
  debate-round counts, checkpoint flag, and benchmark ticker via `.env` with
  type-aware coercion (string / int / bool). (#602)
- **Interactive API-key detection in the CLI.** When the selected provider's
  key is missing, the CLI prompts for it and persists the value to `.env`
  so the analysis run continues without restart.
- **Remote Ollama support.** `OLLAMA_BASE_URL` points the CLI and the
  programmatic client at a remote `ollama-serve`. The CLI surfaces the
  resolved endpoint and warns on common malformed inputs. Adds a
  `"Custom model ID"` option for models pulled via `ollama pull`. (#648, #768)
- **Configurable news-fetch parameters** in `DEFAULT_CONFIG` — per-ticker
  article limit, macro headline limit, lookback window, and macro search
  queries. (#606, #683)
- **Configurable alpha benchmark** for non-US tickers. Replaces hardcoded
  SPY with regional indices for `.NS` (^NSEI), `.T` (^N225), `.HK` (^HSI),
  `.L` (^FTSE), `.TO` (^GSPTSE), `.AX` (^AXJO), `.BO` (^BSESN); explicit
  `benchmark_ticker` override available. Eliminates FX drift dominating
  alpha for non-USD listings. (#628, #684)
- **Multi-language output covers every user-facing agent** — researchers,
  risk debators, research manager, and trader, ending the previous
  partial-localization reports. (#575)
- **Model catalog refresh.** OpenAI GPT-5.5 frontier, Anthropic Claude Opus
  4.7, Gemini 3.1 Flash-Lite GA, xAI Grok 4.20, Qwen 3.6 line. Versioned IDs
  only; auto-shifting aliases moved to the `"Custom model ID"` option.

### Changed

- **Sentiment Analyst** is now consistently named across the CLI dropdown,
  status panel, and final reports (previously the backend was renamed but
  the CLI still said "Social Analyst"). The `AnalystType.SOCIAL = "social"`
  wire value is kept for saved-config back-compat.

### Fixed

- **Structured output works on DeepSeek V4 / reasoner and MiniMax M2.x.**
  Those providers reject `tool_choice` per their tool-calling docs; the
  binding flow now skips it automatically via a capability table.
- **`pip install .` installations pick up the project `.env`** when running
  the CLI as a console script. (#747)
- **Reports save end-to-end** — streamed chunks were previously dropped from
  `complete_report.md`. (#719, #736)
- **Ticker prompt preserves exchange suffixes** (`.SH`, `.SZ`, `.SS`, `.HK`,
  `.T`, etc.) for A-share, HK, Tokyo, and other non-US flows. (#770)
- **Docker permission errors** no longer block first-run write to
  `~/.tradingagents/`. (#519, #627, #672, #771)
- **Config state no longer leaks between runs** when sub-dicts are mutated;
  `set_config` partial updates preserve sibling defaults. (#788)
- **`max_recur_limit` config actually applies** — previously read but not
  forwarded to the propagator. (#764)
- **Missing-API-key error** names the exact env var to set. (#680)
- **Quieter startup** — suppressed the noisy upstream
  `LangChainPendingDeprecationWarning` from langgraph-checkpoint; will be
  removed once that package ships its fix.

### Security

- **Ticker path-traversal validation** at every filesystem-path site (cache,
  checkpoint database, results) so a malicious ticker cannot escape its
  intended directory. (#618)

## [0.2.4] — 2026-04-25

### Added

- **Structured-output decision agents.** Research Manager, Trader, and Portfolio
  Manager now use `llm.with_structured_output(Schema)` on their primary call
  and return typed Pydantic instances. Each provider's native structured-output
  mode is used (`json_schema` for OpenAI / xAI, `response_schema` for Gemini,
  tool-use for Anthropic, function-calling for OpenAI-compatible providers).
  Render helpers preserve the existing markdown shape so memory log, CLI
  display, and saved reports keep working unchanged. (#434)
- **LangGraph checkpoint resume** — opt-in via `--checkpoint`. State is saved
  after each node so crashed or interrupted runs resume from the last
  successful step. Per-ticker SQLite databases under
  `~/.tradingagents/cache/checkpoints/`. `--clear-checkpoints` resets them. (#594)
- **Persistent decision log** replacing the per-agent BM25 memory. Decisions
  are stored automatically at the end of `propagate()`; the next same-ticker
  run resolves prior pending entries with realised return, alpha vs SPY, and
  a one-paragraph reflection. Override path with `TRADINGAGENTS_MEMORY_LOG_PATH`.
  Optional `memory_log_max_entries` config caps resolved entries; pending
  entries are never pruned. (#578, #563, #564, #579)
- **DeepSeek, Qwen (Alibaba DashScope), GLM (Zhipu), and Azure OpenAI**
  providers, plus dynamic OpenRouter model selection.
- **Docker support** — multi-stage build with separate dev and runtime images.
- **`scripts/smoke_structured_output.py`** — diagnostic that exercises the
  three structured-output agents against any provider so contributors can
  verify their setup with one command.
- **5-tier rating scale** (Buy / Overweight / Hold / Underweight / Sell) used
  consistently by Research Manager, Portfolio Manager, signal processor, and
  the memory log; Trader keeps 3-tier (Buy / Hold / Sell) since transaction
  direction is naturally ternary.
- **Pytest fixtures** — lazy LLM client imports plus placeholder API keys so
  the test suite runs cleanly without credentials. (#588)

### Changed

- **`backend_url` default is now `None`** rather than the OpenAI URL. Each
  provider client falls back to its native default. The previous default
  leaked the OpenAI URL into non-OpenAI clients (e.g. Gemini), producing
  malformed request URLs for Python users who switched providers without
  overriding `backend_url`. The CLI flow is unaffected.
- All file I/O passes explicit `encoding="utf-8"` so Windows users no longer
  hit `UnicodeEncodeError` with the cp1252 default. (#543, #550, #576)
- Cache and log directories moved to `~/.tradingagents/` to resolve Docker
  permission issues. (#519)
- `SignalProcessor` reads the rating from the Portfolio Manager's rendered
  markdown via a deterministic heuristic — no extra LLM call.
- OpenAI structured-output calls default to `method="function_calling"` to
  avoid noisy `PydanticSerializationUnexpectedValue` warnings emitted by
  langchain-openai's Responses-API parse path. Same typed result, no warnings.

### Fixed

- Empty memory no longer triggers fabricated past-lessons in agent prompts;
  the memory-log redesign makes this structurally impossible since only the
  Portfolio Manager consults memory and only when entries exist. (#572)
- Tool-call logging processes every chunk message, not just the last one, and
  memory score normalization handles empty score arrays. (#534, #531)

### Removed

- `FinancialSituationMemory` (the per-agent BM25 system) and the dead
  `reflect_and_remember()` plumbing; subsumed by the persistent decision log.
- Hardcoded Google endpoint that caused 404 when `langchain-google-genai`
  changed its API path. (#493, #496)

### Contributors

Thanks to everyone who shaped this release through code, design, and reports:

- [@claytonbrown](https://github.com/claytonbrown) — checkpoint resume (#594), test fixtures (#588), design feedback on cost tracking (#582) and structured validation (#583)
- [@Bcardo](https://github.com/Bcardo) — memory-log redesign (#579), empty-memory hallucination report (#572), encoding fix proposal (#570)
- [@voidborne-d](https://github.com/voidborne-d) — memory persistence design (#564), portfolio manager state fix (#503)
- [@mannubaveja007](https://github.com/mannubaveja007) — structured-output feature request (#434)
- [@kelder66](https://github.com/kelder66) — RAM-only memory issue (#563)
- [@Gujiassh](https://github.com/Gujiassh) — tool-call logging fix (#534), test stub PR (#533)
- [@iuyup](https://github.com/iuyup) — memory score normalization fix (#531)
- [@kaihg](https://github.com/kaihg) — Google base_url fix (#496)
- [@32ryh98yfe](https://github.com/32ryh98yfe) — Gemini 404 report (#493)
- [@uppb](https://github.com/uppb) — OpenRouter dynamic model selection (#482)
- [@guoz14](https://github.com/guoz14) — OpenRouter limited-model report (#337)
- [@samchenku](https://github.com/samchenku) — indicator name normalization (#490)
- [@JasonOA888](https://github.com/JasonOA888) — y_finance pandas import fix (#488)
- [@tiffanychum](https://github.com/tiffanychum) — stale import cleanup (#499)
- [@zaizou](https://github.com/zaizou) — Docker permission issue (#519)
- [@Stosman123](https://github.com/Stosman123), [@mauropuga](https://github.com/mauropuga), [@hotwind2015](https://github.com/hotwind2015) — Windows encoding bug reports (#543, #550, #576)
- [@nnishad](https://github.com/nnishad), [@atharvajoshi01](https://github.com/atharvajoshi01) — encoding fix proposals (#568, #549)

## [0.2.3] — 2026-03-29

### Added

- **Multi-language output** for analyst reports and final decisions, with a
  CLI selector. Internal agent debate stays in English for reasoning quality. (#472)
- **GPT-5.4 family models** in the default catalog, with deep/quick model split.
- **Unified model catalog** as a single source of truth for CLI options and
  provider validation.

### Changed

- `base_url` is forwarded to Google and Anthropic clients so corporate proxies
  work consistently across providers. (#427)
- Standardised the Google `api_key` parameter to the unified `api_key` form.

### Fixed

- Backtesting fetchers no longer leak look-ahead data when `curr_date` is in
  the middle of a fetched window. (#475)
- Invalid indicator names from the LLM are caught at the tool boundary instead
  of crashing the run. (#429)
- yfinance news fetchers respect the same exponential-backoff retry as price
  fetchers. (#445)

### Contributors

- [@ahmedk20](https://github.com/ahmedk20) — multi-language output (#472)
- [@CadeYu](https://github.com/CadeYu) — model catalog typing (#464)
- [@javierdejesusda](https://github.com/javierdejesusda) — unified Google API key parameter (#453)
- [@voidborne-d](https://github.com/voidborne-d) — yfinance news retry (#445)
- [@kostakost2](https://github.com/kostakost2) — look-ahead bias report (#475)
- [@lu-zhengda](https://github.com/lu-zhengda) — proxy/base_url support request (#427)
- [@VamsiKrishna2021](https://github.com/VamsiKrishna2021) — invalid indicator crash report (#429)

## [0.2.2] — 2026-03-22

### Added

- **Five-tier rating scale** (Buy / Overweight / Hold / Underweight / Sell)
  introduced for the Portfolio Manager.
- **Anthropic effort level** support for Claude models.
- **OpenAI Responses API** path for native OpenAI models.

### Changed

- `risk_manager` renamed to `portfolio_manager` to match the role description
  shown in the CLI display.
- Exchange-qualified tickers (e.g. `7203.T`, `BRK.B`) preserved across all
  agent prompts and tool calls.
- Process-level UTF-8 default attempted for cross-platform consistency
  (note: this approach did not actually take effect; replaced in v0.2.4 with
  explicit per-call `encoding="utf-8"` arguments).

### Fixed

- yfinance rate-limit errors are retried with exponential backoff. (#426)
- HTTP client SSL customisation is supported for environments that need
  custom certificate bundles. (#379)
- Report-section writes handle list-of-string content gracefully.

### Contributors

- [@CadeYu](https://github.com/CadeYu) — exchange-qualified ticker preservation (#413)
- [@yang1002378395-cmyk](https://github.com/yang1002378395-cmyk) — HTTP client SSL customisation (#379)

## [0.2.1] — 2026-03-15

### Security

- Patched `langchain-core` vulnerability (LangGrinch). (#335)
- Removed `chainlit` dependency affected by CVE-2026-22218.

### Added

- `pyproject.toml` build-system configuration; the project now installs via
  modern packaging tooling.

### Removed

- `setup.py` — dependencies consolidated to `pyproject.toml`.

### Fixed

- Risk manager reads the correct fundamental report source. (#341)
- All `open()` calls receive an explicit UTF-8 encoding (initial pass).
- `get_indicators` tool handles comma-separated indicator names from the LLM. (#368)
- `Propagation` initialises every debate-state field so risk debaters never
  see missing keys.
- Stock data parsing tolerates malformed CSVs and NaN values.
- Conditional debate logic respects the configured round count. (#361)

### Contributors

- [@RinZ27](https://github.com/RinZ27) — `langchain-core` security patch (#335)
- [@Ljx-007](https://github.com/Ljx-007) — risk manager fundamental-report fix (#341)
- [@makk9](https://github.com/makk9) — debate-rounds config issue (#361)

## [0.2.0] — 2026-02-04

This is the largest release since the initial public version. The framework
moved from single-provider to a multi-provider architecture and grew several
production-ready surfaces.

### Added

- **Multi-provider LLM support** (OpenAI, Google, Anthropic, xAI, OpenRouter,
  Ollama) via a factory pattern, with provider-specific thinking configurations.
- **Alpha Vantage** integration as a configurable primary data provider, with
  yfinance as a community-stability fallback.
- **Footer statistics** in the CLI: real-time tracking of LLM calls, tool
  calls, and token usage via LangChain callbacks.
- **Post-analysis report saving** — the framework writes per-section markdown
  files (analyst reports, debate transcripts, final decision) when a run
  completes.
- **Announcements panel** — fetches updates from `api.tauric.ai/v1/announcements`
  for the CLI welcome screen.
- **Tool fallbacks** so a single vendor outage does not stop the pipeline.

### Changed

- Risky / Safe risk debaters renamed to **Aggressive / Conservative** for
  consistency with the displayed agent labels.
- Default data vendor switched to balance reliability and quota across
  community deployments.
- Ollama and OpenRouter model lists updated; default endpoints clarified.

### Fixed

- Analyst status tracking and message deduplication in the live display.
- Infinite-loop guard in the agent loop; reflection and logging hardened.
- Various data-vendor implementation bugs and tool-signature mismatches.

### Contributors

This release is the first with substantial outside contributions; many community
PRs from late 2025 also landed here.

- [@luohy15](https://github.com/luohy15) — Alpha Vantage data-vendor integration (#235)
- [@EdwardoSunny](https://github.com/EdwardoSunny) — yfinance fetching optimisations (#245)
- [@Mirza-Samad-Ahmed-Baig](https://github.com/Mirza-Samad-Ahmed-Baig) — infinite-loop guard, reflection, and logging fixes (#89)
- [@ZeroAct](https://github.com/ZeroAct) — saved results path support (#29)
- [@Zhongyi-Lu](https://github.com/Zhongyi-Lu) — `.env` gitignore (#49)
- [@csoboy](https://github.com/csoboy) — local Ollama setup (#53)
- [@chauhang](https://github.com/chauhang) — initial Docker support attempt (#47, later reverted; the merged Docker support shipped in v0.2.4)

## [0.1.1] — 2025-06-07

### Removed

- Static site assets that had been bundled with v0.1.0; the public site now
  lives separately.

## [0.1.0] — 2025-06-05

### Added

- **Initial public release** of the TradingAgents multi-agent trading
  framework: market / sentiment / news / fundamentals analysts; bull and bear
  researchers; trader; aggressive, conservative, and neutral risk debaters;
  portfolio manager. LangGraph orchestration, yfinance data, per-agent
  BM25 memory, single-provider OpenAI integration, interactive CLI.

[0.2.4]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/TauricResearch/TradingAgents/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/TauricResearch/TradingAgents/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/TauricResearch/TradingAgents/releases/tag/v0.1.0
