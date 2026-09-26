# Shared daily research design

**Status:** approved 2026-09-25 · **Author:** Landon + Claude (planning session) · **Implementation:** dev-pipeline (Claude plans/reviews, Kimi K2.7 codes)

## Context

Landon's paper-trading desk (`Jemplayer82/ai-trading-desk`, deployed as the Portainer stack on 192.168.7.50) runs two research pipelines that should be one:

- **Options** (3 accounts: bull 02:00, bear 02:30, small 03:30 ET). Each account runs the *whole* research itself: pre-screen 503 → top 150 movers + SPY → 3-minute quick LLM scan → full agent-graph deep dive on ~51 tickers (2–4 h at 8-wide on ollama cloud / kimi-k2.6) → wait for the 09:35 ET gate → vet contracts and allocate. The scan queue serializes the compute, so account 2 starts when account 1 reaches the wait and account 3 after that. Measured over the last four trading days: account 1 ready ~05:30 ET, bear ~07:00–08:00, small 10:00–11:20 ET, so the small account trades at 10:15–11:15. On 2 of those 4 days account 1's deep dive went 60 min without a progress write and the stuck-run reaper killed it (no trades that day; the whole queue shifted).
- **S&P 500 equity** (2 accounts: Bull/Bear). A separate weekly Saturday 00:00 ET scan (503 quick + 50 deep, 2–6 h, also reaped last Saturday) feeds an LLM allocator that builds a whole-share portfolio at the prior close, with no market wait.

Same-day research reuse already exists in code (fingerprinted quick results and deep-dive analyses), but equity and options never share it because Saturday never matches a weekday.

**Landon's decisions (2026-09-25):** "One big research for everything, then each one does its own thing."
1. S&P equity accounts trade **daily** off the shared research; the Saturday scan goes away.
2. Research universe stays **top 150 movers + SPY** (quick) and top 50 BUY/SELL + SPY (deep) — exactly today's options research.
3. **One shared research run at 00:00 ET** (Mon–Fri) feeds all five accounts.
4. Trades fill at the existing **09:35 ET gate with live quotes, for both kinds** (equity stops filling at the prior close).
5. Equity churn: **act only on new signals and stops**, otherwise hold.
6. Research failure: **retry once** automatically (if it fails before ~05:30 ET); if there is still no completed research at the open, every account skips the day (holds; stops still enforced) and the failure alert fires.
7. Equity stops: no change — the per-account stop setting already exists in the UI; Landon sets it himself.
8. Delivery: **push to master, then I redeploy via Portainer** (see Deploy).

## Measured facts the design relies on

| Phase (today) | Wall time |
|---|---|
| Pre-screen 503 (yfinance bulk) | ~25 s |
| Quick scan 151 = 8 LLM batches of 20 | ~3 min |
| Deep dive 51 tickers, pool 8 | 3.5–4 h (first dive lands ~27 min in; 25–40 min per dive) |
| Deep dive with same-day shared-stage reuse | 1.5–2.5 h |
| Post-open vetting + allocator, per account | ~1 min |

Starting the single research at 00:00 ET leaves ~9 h before the open for a ~4 h job plus one retry.

Production config: portfolio container `OLLAMA_MAX_CONCURRENCY=8`, `DEEP_DIVE_PER_CALL_GATING=1`, mem 4g; scheduler `SCHEDULER_TIMEZONE=America/New_York`; all accounts aggressiveness 9–10 (3 debate rounds, same deep-dive fingerprint bucket, so reuse works across all five).

## Design summary

**One research row, five allocation rows, per trading day.**

```
00:00 ET  research_scan (kind='research', no account)      3–4 h; retried once if it fails before 05:30
          pre-screen 503 → 150 movers + SPY → quick scan → deep dive top 50 BUY/SELL + SPY
          → status completed; its spy_quick_results + analyses rows ARE the research
09:00 ET  per-account allocation rows (kind='options' ×3, kind='equity' ×2)
          wait for today's research (running_wait_research, heartbeating; deadline 10:30)
          → copy the research's quick rows onto the account row (UI/reuse keep working)
          → wait_for_market_open 09:35 (running_wait_market)
          → each account under its own allocation lock, all accounts in parallel (running_alloc)
            [amended 2026-09-26 by Landon: "trades can be parallel"; options contract
            vetting is done once per research row and shared]:
              options: refresh_positions → fetch_candidates over the research's usable dives
                       (live spot) → lessons → options_allocator.run → open/close/hold
              equity:  refresh prior portfolio to market (stops) → long-only candidates
                       (Buy/Overweight/Hold or currently held) priced at LIVE quotes
                       → spy_allocator.run(cadence="daily", low-churn prompt) → complete
```

- **New tier-3 modules** (`options_engine.py`/`options_data.py` are deleted in tier-3 builds, and equity needs this now): `web/market_calendar.py` (ET clock, `MARKET_OPEN_ET`, NYSE holiday list 2026–27 + `MARKET_HOLIDAYS_EXTRA` env, `is_trading_day`), `web/research_engine.py` (pre-screen/targets/allocation lock/market wait moved verbatim out of `web/options_engine.py`, plus `run_research`, `wait_for_research`, `run_allocation`, `equity_candidates`, `start_allocation`), `web/research_routes.py` (`POST /api/research-scan`, `GET /api/research-scans/today`, queue runner `research`). `options_engine.py` becomes allocation-only and imports from `research_engine`.
- **Scheduler** (`web/scheduler.py`): static jobs `research_scan` (Mon–Fri at app setting `SCHEDULE_RESEARCH_TIME`, default 00:00, same pattern as the nightly-scan setting) and `research_retry` (every 15 min: re-kick a failed research once before 05:30 ET, or kick a missing one 15 min after its time); per-account jobs both `mon-fri`; trading-day guard on every job; `STUCK_SCAN_STALL_MIN` default 60 → 120; reaper labels for research/allocation rows.
- **Robustness**: heartbeat thread in `spy_scanner.run_quick_scan`/`run_deep_dives` stamps `updated_at` every 2 min; research runs through the single compute slot (behind the 22:00 Schwab nightly scan if still running); allocation rows bypass the compute queue (~1 min of work) and serialize only on the allocation lock; the dead `_release_scan_slot` hand-off and its tests are removed.
- **DB** (`web/db.py`): `spy_scans.research_scan_id` (+ allow-list, create/list/status), helpers `latest_research_scan`, `copy_spy_quick_results`, `list_deep_dived_results`; one-shot backfill: every non-NULL `paper_accounts.schedule_time` → `09:00`; new-account default `09:00` for both kinds.
- **Research config**: `bias="neutral"`, `aggressiveness = max over accounts` (10 → 3 debate rounds), so the retry reuses completed dives via the existing fingerprinted `find_reusable_analysis`.
- **Equity allocator** (`web/spy_allocator.py`): `cadence="daily"` appends a low-churn block (hold by default; exit only on Sell/Underweight or conviction collapse; new only on Buy/Overweight ≥ 8; no re-weighting); fallback treats Overweight as Buy.
- **UI**: research status line + "Run research" button and a research-time input on the S&P tab (TIER:3 block), status line on the Options tab; "Allocate now" buttons; new statuses rendered; captions/defaults updated. Settings tab gets `SCHEDULE_RESEARCH_TIME` via a new TIER:3 registry block in `web/credentials.py`.
- **Tier tooling**: `scripts/make_tier.py` manifest gains the new tier-3 modules/tests; canary patterns for tiers 1–2 gain `research_engine`/`research_routes`; tier-3 identity/README/CLAUDE.md tables updated. (The canary is AST-based: only unconditional top-level imports of a deleted module count.)
- **Behavior notes for Landon**: the PM rating is neutral (bias applied only in allocators); day-one equity rebalances from the last Saturday portfolio; weekday market holidays are skipped; nothing here touches stop settings (set per account in the UI).

## File-by-file design

### 0. Findings that shape the design

1. **The leak canary is AST-based, not a text grep.** `scripts/make_tier.py:396-445` (`_module_top_level_imports` / `_leak_canary`) flags only *unconditional module-level* imports of a deleted module. Comments, strings, function-local imports and `if features.enabled(...)`-gated imports are not leaks. (The comment at `web/scheduler.py:575-579` claiming a function-local import trips it is stale.) Tier-3 code may *mention* options modules but must never `import options_*` at module top level; the plan still keeps every options-only symbol out of tier-3 modules.
2. **Allocation rows bypass the compute queue; the research row goes through it.** Research is 3–4 h of compute and must respect the single-slot invariant against the 22:00 Schwab nightly scan (`portfolio_routes.start_scan:117-127`). If the nightly scan is still running at 00:00, research queues behind it (correct; see Risks).
3. **`_wait_for_market_open`'s queue-slot hand-off becomes dead code.** Once allocation rows never hold the slot, `_release_scan_slot`/`_slot_release_enabled` (`options_engine.py:117-148`) and the `ticks == 0` branch (`734-772`) have nothing to hand off. Remove them and their tests (`tests/test_options_market_wait.py::TestWaitReleasesTheQueueSlot`). Keep `scan_queue._wait_market_release_enabled` (`scan_queue.py:50-63`): `running_wait_market` must still read as not-busy.
4. **Bias no longer reaches the deep dive.** Today each account's dives (or PM reruns via `find_reusable_analysis`) are bias-specific; `_deep_dive_fingerprint` (`spy_scanner.py:880-923`) excludes bias so the PM rerun re-applies it. With one research run the Portfolio Manager rates every ticker with `bias="neutral"`; bull/bear bias applies only in the allocators (`spy_allocator._BIAS_CONTEXT:205-211`, `options_allocator.run(bias=...)`). Accepted consequence; say so in CHANGELOG.
5. **Research aggressiveness is one number.** Debate rounds come from aggressiveness (`web/runner.py:58-68`, fingerprinted at `spy_scanner.py:906-907`). Use `max(aggressiveness)` over all paper accounts (today 10 → 3 rounds), stored on the research row's `aggressiveness` column.
6. **"enriched" rows are rebuilt from the DB at allocation time.** `run_deep_dives._finish` (`spy_scanner.py:1137-1146`) upserts the deep rating + `analysis_id` onto `spy_quick_results`; `analyses.final_decision` holds the PM text (`db.py:123`). Failed dives never touch the quick row (`1214-1217`), so "usable" = quick rows whose `analysis_id` joins to an `analyses` row with `status='completed'`. `entry_price` is not stored anywhere — fine, entry prices come from live quotes at 09:35.
7. **`refresh_portfolio_prices` treats a completed scan with empty `portfolio_json` as an error** (`spy_scanner.py:1441-1442`). Research rows complete with `portfolio_json=[]`; harmless because `refresh_all_portfolio_prices` selects `kind='equity'` only (`1617`) — nothing may ever call `refresh_portfolio_prices` on a research id.
8. **Settings-registry / index.html TIER blocks cannot nest.** `SCHEDULE_NIGHTLY_SCAN_TIME` sits in a `# TIER:2` block (`credentials.py:125-144`); the research time gets its own `# TIER:3 BEGIN/END` block after `# TIER:2 END`. In `index.html` the research-time control goes in the S&P tab (already TIER:3), not next to the Portfolio tab's nightly control (TIER:2). (`make_tier.py:213-215`.)
9. **UTC vs ET dates.** At 00:00 and 09:00 ET the UTC date equals the ET date, so switching the new paths to the ET date changes nothing for cron runs and fixes manual kicks between 20:00 and 23:59 ET. Do it.

### 1. New tier-agnostic module `web/market_calendar.py` (ships at every tier; no manifest entry)

Pure stdlib. Owns `_ET` (`ZoneInfo("America/New_York")` with the same fallback as `options_data.py:28-33`), `now_et()`, `today_et()`; `MARKET_OPEN_ET = (9, 35)` (moved from `options_engine.py:57`); `NYSE_HOLIDAYS: frozenset[date]` for 2026–2027 (2026: 01-01, 01-19, 02-16, 04-03, 05-25, 06-19, 07-03, 09-07, 11-26, 12-25; 2027: 01-01, 01-18, 02-15, 03-26, 05-31, 06-18, 07-05, 09-06, 11-25, 12-24) with a docstring pointing at nyse.com/markets/hours-calendars and an env override `MARKET_HOLIDAYS_EXTRA` (comma-separated ISO dates, read call-time); `is_trading_day(d: date | None = None) -> bool` (weekday and not a holiday); `is_market_open_now(now=None)`. `web/options_data.py:84-89`: make `today_et`/`now_et` one-line delegates to `market_calendar` (keeps `settle_expired`/`is_settleable` and every caller/test untouched).

Tests `tests/test_market_calendar.py` (tier-agnostic): weekend false, each 2026 holiday false, a normal Tuesday true, env extra-date honoured, `today_et()` returns a `date`.

### 2. `web/db.py`

- `SCHEMA` `spy_scans` (`163-188`): add `research_scan_id INTEGER`; `_COLUMN_MIGRATIONS` (`347-390`): append `("spy_scans", "research_scan_id", "INTEGER")`; `_SPY_SCAN_UPDATABLE` (`1114-1119`): add `"research_scan_id"`; `create_spy_scan` (`1078-1093`): `research_scan_id: int | None = None` in the INSERT; `list_spy_scans` cols (`1362-1367`) and `get_spy_scan_status` (`1396-1401`): include `research_scan_id`; `find_stuck_spy_scans` SELECT (`1227`): include it (reaper labels).
- New helpers: `latest_research_scan(trade_date) -> dict | None` (newest `kind='research'` row for the date: id, status, counters, quick_fingerprint, aggressiveness); `count_research_attempts(trade_date) -> int`; `copy_spy_quick_results(src_scan_id, dst_scan_id) -> int` (`INSERT OR REPLACE INTO spy_quick_results (scan_id, ticker, signal, conviction, reasoning, analysis_id, error) SELECT ?, ticker, signal, conviction, reasoning, analysis_id, error FROM spy_quick_results WHERE scan_id = ?` — keeps `get_spy_scan` `1405-1421`, the signal-flip check `1506`, and `find_reusable_quick_results` `1311-1350` working unchanged for allocation rows); `list_deep_dived_results(scan_id) -> list[dict]` (`SELECT r.ticker, r.signal, r.conviction, r.reasoning, r.analysis_id, a.final_decision FROM spy_quick_results r JOIN analyses a ON a.id = r.analysis_id WHERE r.scan_id = ? AND r.error IS NULL AND a.status = 'completed'`).
- Backfill: change the legacy schedule backfill at `397-398` to `'09:00'` for both kinds, and append a one-shot backfill keyed on `(("spy_scans","research_scan_id"),)`: `UPDATE paper_accounts SET schedule_time = '09:00' WHERE schedule_time IS NOT NULL` (NULL = manual-only stays NULL); list it after the legacy entry.

Tests: extend `tests/test_paper_account_schema.py` (assertions at 138/142/174 → `"09:00"`; new: a DB with `schedule_time='02:30'` and no `research_scan_id` column boots to `'09:00'`, NULL stays NULL, a second `init_db()` does not re-fire). Extend `tests/test_options_lifecycle.py::test_kind_migration_on_pre_kind_db` to assert `research_scan_id` exists. `tests/test_research_engine.py`: `copy_spy_quick_results` / `list_deep_dived_results` (failed analysis excluded, error rows excluded).

### 3. `web/spy_scanner.py` (tier 3)

- **Heartbeat.** Add `class _Heartbeat` (daemon thread, `threading.Event`-driven like `llm_helpers._GateMonitor:177-218`, every `SCAN_HEARTBEAT_SECONDS` (env, default 120) calls a supplied `beat()` closure). In `run_deep_dives` wrap the pool block (`1219-1250`) with `with _Heartbeat(lambda: db.update_spy_scan(scan_id, deep_count=completed, deep_reused_count=reused)):` (`update_spy_scan` always stamps `updated_at`, `1128-1131`). Same in `run_quick_scan` around `829-870` with `quick_count=completed`. Existing tests that call these with fakes (`test_deep_dive_reuse.py:117`, `test_deep_dive_gating.py:165`, `test_macro_brief_scan.py:84`, `test_spy_scanner_store.py:56`) keep passing (thread writes counters to the tmp DB, stops on `__exit__`).
- **`fetch_live_prices(tickers) -> dict[str, float]`**: extract `refresh_portfolio_prices` lines `1447-1478` (Schwab bulk → yfinance fallback) into a module function; `refresh_portfolio_prices` calls it (keeps its `{"error": ...}` on exception). Used by both allocations for entry prices.
- `run_quick_scan`/`run_deep_dives` otherwise unchanged; research calls them exactly as `run_options_build` does today (`options_engine.py:869, 883`).

Tests `tests/test_spy_scanner_store.py`: `test_heartbeat_touches_updated_at_while_dives_in_flight` (monkeypatch `SCAN_HEARTBEAT_SECONDS` to 0.05, fake orchestrator sleeping 0.3 s, assert `updated_at` advanced ≥ 2 times); `test_fetch_live_prices_schwab_then_yfinance_fallback`.

### 4. New tier-3 module `web/research_engine.py`

Move verbatim from `options_engine.py` (which then imports them by name): `PRESCREEN_TOP`, `DEEP_TOP`, `ALWAYS_DEEP` (`54-56`), `_PRESCREEN_TTL_SECONDS`, `_PRESCREEN_MIN_COMPLETENESS`, `_PRESCREEN_CACHE` (`68-76`), `_mover_score` (`209-222`), `prescreen` (`225-275`), `select_deep_dive_targets` (`278-296`); `_ALLOC_LOCK`, `_ALLOC_POLL_SECONDS`, `_ALLOC_TIMEOUT_SECONDS`, `_parse_alloc_timeout_seconds`, `_allocation_slot` (`78-114, 162-204`) — both allocation kinds serialise on it; `_phase` (`151-159`; delete the identical copy at `spy_routes.py:477-489` and import); `wait_for_market_open(scan_id)` from `734-772` minus the `ticks == 0` hand-off, using `market_calendar.now_et()` and `is_trading_day(now.date())` (non-trading day → return immediately, preserving weekend-manual-run semantics).

New:
- `research_aggressiveness() -> int` = `max((a["aggressiveness"] for a in db.list_paper_accounts()), default=5)`.
- `run_research(scan_id, trade_date)`: lifted from `run_options_build` phases 1–2 (`855-890`): prefs → `build_config({**prefs, "aggressiveness": scan["aggressiveness"], "bias": "neutral"})` → `get_sp500_tickers()` → `prescreen(universe, PRESCREEN_TOP, trade_date=trade_date)` → append `ALWAYS_DEEP` → `spy_scanner.run_quick_scan` → `assert_quick_scan_healthy` → `select_deep_dive_targets` → `spy_scanner.run_deep_dives` → `assert_deep_dives_healthy` → `db.complete_spy_scan(scan_id, allocator_report=<markdown: counts by signal, reused count, failed dives>, portfolio_json=[], previous_scan_id=None, starting_value=None)`. Cancel checks between phases as today (`870, 889`).
- `wait_for_research(scan_id, trade_date) -> dict`: every 30 s `row = db.latest_research_scan(trade_date)`; `completed` → `db.update_spy_scan(scan_id, research_scan_id=row["id"])`, return `db.get_spy_scan(row["id"])`; `failed`/`cancelled`/None → keep waiting (a retry may create a newer row) until the deadline = `max(today RESEARCH_DEADLINE_ET (10,30), started_at + RESEARCH_WAIT_MAX_MIN (90))` (both env-overridable) → `raise RuntimeError("today's research is not complete (status=...) — allocation abandoned at 10:30 ET")`; every 6th tick `db.update_spy_scan(scan_id, status="running_wait_research")` (heartbeat, mirrors `770`); `db.is_spy_scan_cancelled` → `ScanCancelled`.
- `run_allocation(scan_id, trade_date, allocate: Callable[[AllocationContext], None])` — shared skeleton: (1) `research = wait_for_research(...)`; (2) `db.copy_spy_quick_results(research["id"], scan_id)` and `db.update_spy_scan(scan_id, quick_fingerprint=research["quick_fingerprint"], quick_count/quick_total/deep_count/deep_total/deep_reused_count=research values)`; (3) `db.update_spy_scan(scan_id, status="running_wait_market")`; `wait_for_market_open(scan_id)`; (4) `with _allocation_slot(scan_id): db.update_spy_scan(scan_id, status="running_alloc"); allocate(ctx)` where `ctx` carries `scan`, `account`, `research`, `quick_results` (`research["quick_results"]`), `usable` (`db.list_deep_dived_results(research["id"])`), `live_prices` (`spy_scanner.fetch_live_prices([r["ticker"] for r in usable] + held tickers)`), `config`, `aggressiveness`, `bias`.
- `equity_candidates(usable, held_tickers) -> list[dict]`: keep rows whose upper-cased signal ∈ {BUY, OVERWEIGHT, HOLD} or whose ticker is currently held (a held name rated Sell reaches the rebalance as SELL → EXITED); set `entry_price` from `live_prices`; drop rows with no live price (log).
- `start_allocation(account, today, kind, background_tasks, *, force=False) -> dict` (shared by both POST routes): (a) `not is_trading_day(today) and not force` → `HTTPException(409, "not a trading day; pass force")`; (b) idempotent per `(today, account, kind)` on `status NOT IN ('failed','cancelled')` like `options_routes.py:61-71`; (c) `research_routes.ensure_research_scan(today, background_tasks)` so a manual click on a day with no research kicks it; (d) `db.create_spy_scan(today, paper_account_id, aggressiveness, bias, status="running_wait_research", kind=kind, research_scan_id=<id if already completed else None>)`; (e) `background_tasks.add_task(runner, scan_id, today)`; return `{"scan_id", "account_id", "status": "running_wait_research", "new": True, "research": {...}}`. No `_SCAN_LOCK` — allocation rows are never "busy" until `running_alloc`.

Tests `tests/test_research_engine.py` (tier-3 manifest). Move here, re-pointed at `research_engine.*`: `TestPrescreenSameDayCache` (`test_options_lifecycle.py:912-1139`), `test_mover_score_direction_agnostic` (`463-472`), the four `select_deep_dive_targets` tests (`182-213`), and `TestAllocationSlot` + `TestMarketWaitStatusSplit` from `test_options_market_wait.py` (monkeypatch `research_engine.now_et` and `research_engine.time_mod.sleep`). New: `wait_for_research` completes → returns row and sets `research_scan_id`; failed then a newer completed row → follows the retry; deadline → RuntimeError; heartbeat writes `running_wait_research`; cancel → `ScanCancelled`; `run_allocation` copies quick rows, holds `_ALLOC_LOCK` inside `allocate`, releases after; `equity_candidates` filter matrix; `run_research` end-to-end with `run_quick_scan`/`run_deep_dives` faked → status completed, `portfolio_json == []`, report contains counts.

### 5. New tier-3 routes `web/research_routes.py` + queue/nginx plumbing

- `ensure_research_scan(today, background_tasks) -> dict`: idempotency (`kind='research' AND trade_date=? AND status NOT IN ('failed','cancelled')`) → return existing; else under `scan_queue._SCAN_LOCK` busy-check + create (copy `spy_routes.py:213-234`) with `aggressiveness=research_engine.research_aggressiveness()`, `bias="neutral"`, `kind="research"`, `paper_account_id=None`; queued if busy else `background_tasks.add_task(_run_research_thread, scan_id, today)`.
- `POST /api/research-scan` (body `{force?: bool}`; `today = market_calendar.today_et().isoformat()`; non-trading day → 409 unless force) → `ensure_research_scan`. `GET /api/research-scans/today` → `db.latest_research_scan(today)` or `{"scan": None}`; `GET /api/research-scans?limit=` → `db.list_spy_scans(kind="research")`. Detail/cancel reuse the generic `/api/spy-scans/{id}` and `/cancel` routes (`spy_routes.py:256-290`).
- `_run_research_thread(scan_id, trade_date)`: copy of `options_routes._run_options_scan_thread:32-47` calling `research_engine.run_research`, `alerts.notify_run_failed(kind="Research")`. Bottom: `scan_queue.register_runner("research", sys.modules[__name__], "_run_research_thread")`.
- `web/portfolio_main.py:54-56`: `from . import research_routes; app.include_router(research_routes.router)` inside the `sp500` gate. Line `107`: add `'running_wait_research'` to the waiting-statuses list (this also makes `scripts/redeploy.py:143-157` refuse to redeploy while allocations wait).
- `web/scan_queue.py:144-149`: `elif row["kind"] == "research": key = "research"`. Update the docstring at `50-63` (drop the reference to `options_engine._slot_release_enabled`).
- `web/nginx.conf`: new `# TIER:3 BEGIN … END` block `location /api/research { … }` copied from the `/api/spy` block (`65-78`, 43200 s timeouts) right after line 79. Run `python scripts/make_tier.py --lint`.
- `scripts/make_tier.py`: `TIER_ONLY_FILES[3]` += `web/research_engine.py`, `web/research_routes.py`, `tests/test_research_engine.py`, `tests/test_research_routes.py`, `tests/test_shared_research_e2e.py` (the e2e test is tier-4 — see §13; put it in `TIER_ONLY_FILES[4]`); `LEAK_CANARY_PATTERNS[1]` and `[2]` += `"research_engine", "research_routes"`; `TIER_IDENTITY[3]` → `"Tier 3 — Scanner: + the daily S&P 500 research and paper portfolio"` (update README table row 39 and CLAUDE.md's table in the same commit).

Tests `tests/test_research_routes.py` (TestClient on `portfolio_main.app` with `features` forced): idempotent per date; a failed row allows a new one; queued when busy; 409 on a Saturday without force; `GET /today`. `tests/test_scan_queue.py`: a queued `kind='research'` row dispatches to `research_routes._run_research_thread` (shape of `test_options_lifecycle.py:368-411`) and is failed cleanly when no runner is registered.

### 6. `web/spy_allocator.py` — daily cadence

- `run(..., cadence: str = "weekly")`. When `"daily"`, append `_DAILY_REBALANCE_ADDENDUM` to `_REBALANCE_SYSTEM_TEMPLATE` (`163-204`, weekly template byte-identical): "This is a DAILY check-in against a fresh research pass, not a weekly rebalance. Default every existing position to HOLD at its current size. EXIT only on a SELL/Underweight rating or a conviction collapse; open NEW positions only for BUY/Overweight candidates with conviction ≥ 8; never ADD/TRIM merely to re-weight. Turnover is a cost." Fresh mode unchanged. Report title (`498`) "Daily allocation" when daily.
- `_fallback_fresh`/`_fallback_rebalance` (`324-391`): widen the BUY test to `in ("BUY", "OVERWEIGHT")` (the deep rating is 5-tier, `options_data.py:73-81`).

Tests `tests/test_spy_allocator.py`: `test_daily_cadence_adds_low_churn_block` (LLM mocked; addendum present for daily, absent for weekly); `test_fallback_treats_overweight_as_buy`.

### 7. Allocation workers

**`web/options_engine.py` (tier 4).** Replace `run_options_build` (`824-1052`) with `run_options_allocation(scan_id, trade_date)`: top (`827-853`) unchanged (account lookup, stop policy, deposit-on-first-build, `settle_expired`); then `research_engine.run_allocation(scan_id, trade_date, _allocate)` where `_allocate(ctx)` is phases 3–4 verbatim (`902-1052`) with `enriched`/`usable` replaced by `ctx.usable` (each row given `entry_price = ctx.live_prices.get(ticker)` as the chain `spot_hint`), `quick_results` by `ctx.quick_results` (for `fresh_signals`, `923-927`), and `_zero_candidate_reason` re-signatured to `(quick_results, n_targets, usable, candidates)` with `n_targets = research["deep_total"]` (update `tests/test_options_scan_health.py`). Delete `_release_scan_slot`, `_slot_release_enabled`, `_wait_for_market_open`, `_allocation_slot`, `prescreen`, `select_deep_dive_targets`, the constants and the prescreen cache (imported from `research_engine`); `MARKET_OPEN_ET` from `market_calendar`; rewrite the module docstring (`1-22`).
`web/options_routes.py`: `_run_options_scan_thread` (`32-47`) calls `run_options_allocation`; `_start_options_scan_for_account` (`50-93`) becomes `research_engine.start_allocation(account, today, "options", background_tasks, force=body.get("force"))`; `start_options_scan` line `104` uses `market_calendar.today_et()`.

**`web/spy_routes.py` (tier 3).** Delete `_run_spy_scan` (`492-611`) and `_phase`. New `_run_equity_allocation(scan_id, trade_date)`: prev = `db.get_latest_completed_spy_scan(exclude_id=scan_id, paper_account_id=account_id)` (kind `equity` default — includes the last Saturday scan, so day one rebalances from it); if prev: `spy_scanner.refresh_portfolio_prices(prev["id"])` first (live marks + stop enforcement), then `previous_portfolio`/`starting_value` exactly as `512-543`; `research_engine.run_allocation(scan_id, trade_date, _allocate)` with `_allocate(ctx)`: `candidates = research_engine.equity_candidates(ctx.usable, held)`; `spy_allocator.run(candidates, trade_date, ctx.config, previous_portfolio=…, starting_value=…, aggressiveness=…, bias=…, cadence="daily")`; `db.complete_spy_scan(...)` (`595-601`); `spy_scanner.refresh_portfolio_prices(scan_id)` (`607-611`). `_run_spy_scan_thread` (`458-474`) keeps its name (runner registration at `616` and patched tests stay valid) and calls `_run_equity_allocation`. `start_spy_scan` (`170-234`): `account_id` required (400 otherwise; the NULL-account global scan is gone); `today = market_calendar.today_et().isoformat()`; body → `research_engine.start_allocation(account, today, "equity", background_tasks, force=...)`; remove the `_SCAN_LOCK` block. `_DEFAULT_SCHEDULE_TIME` (`48`) → `{"equity": "09:00", "options": "09:00"}`.

Tests: rewrite `tests/test_options_market_wait.py::TestRunOptionsBuildHoldsAllocationLock` as `TestRunOptionsAllocation…` (seed a completed research row with one quick row + one completed `analyses` row via `db.create_analysis`/`complete_analysis`; monkeypatch `research_engine.now_et` to a Saturday 10:00 so both waits return immediately, `spy_scanner.fetch_live_prices`, `options_engine.refresh_positions`, `options_allocator.run`; keep the three assertions: lock held during refresh, policy passed, status completed). `tests/test_paper_account_routes.py`: `POST /api/spy-scan` without `account_id` → 400; Saturday without force → 409; with a completed research row today the created row has `research_scan_id` set and status `running_wait_research`; lines 118/123 → `"09:00"`.

### 8. `web/scheduler.py`

- `STUCK_SCAN_STALL_MIN` default `"60"` → `"120"` (`55`).
- Settings-driven times: `RESEARCH_TIME_SETTING = "SCHEDULE_RESEARCH_TIME"`, `DEFAULT_RESEARCH_TIME = (0, 0)`; refactor `nightly_scan_time` (`85-110`) into `_setting_time(setting, default)` with `nightly_scan_time()` and `research_time()` wrappers (existing tests at `test_schedule_reconciler.py:69-86` keep passing).
- `job_research_scan()`: skip with a log line if `not market_calendar.is_trading_day(market_calendar.today_et())`; POST `f"{PORTFOLIO_URL}/api/research-scan"` (timeout 60, `_internal_headers()`); ≥400 → `alerts.notify("⚠️ Daily research rejected (...)")`; exception → alert.
- `job_research_retry()`: trading day only; `now = market_calendar.now_et()`; `rows = db.list_spy_scans(kind="research", limit=10)` filtered to today; if no row and `now ≥ research_time + 15 min` → POST; elif newest row `failed` and `len(rows) < RESEARCH_MAX_ATTEMPTS (2)` and `now < RESEARCH_RETRY_CUTOFF_ET (5,30)` → POST (the endpoint creates a fresh row because failed rows don't satisfy idempotency; the retry reuses every completed dive via `find_reusable_analysis`, 6 h window). Else no-op. Never raises.
- `_ACCOUNT_JOBS` (`453-457`): both `"mon-fri"`. `job_spy_scan_account` (`284-307`) and `job_options_scan_account` (`338-364`): early-return on non-trading days; wording "Daily S&P 500 allocation". Delete `job_spy_scan` (`264-281`); `--run-spy-scan-now` now calls `job_research_scan`; add `--run-research-now`.
- `job_reconcile_schedules` (`541-570`): generalise into a loop over `_SETTING_JOBS = (("nightly_scan", nightly_scan_time, job_nightly_scan, "schwab"), ("research_scan", research_time, job_research_scan, "sp500"))`.
- `register_jobs` (`865-871`): inside the `sp500` gate add `research_scan` (CronTrigger mon-fri at `research_time()`) and `research_retry` (IntervalTrigger minutes=15).
- `job_reap_stuck_runs` (`799-804`): `kind_label` map `{"options": "Options scan", "research": "Research", "equity": "S&P 500 scan"}`, with " (allocation)" appended when the row has `research_scan_id`.
- Docstring schedule table (`11-22`) and startup log lines (`993-1004`).

Tests: `tests/test_scheduler_jobs.py`: tier-3 set += `research_scan`, `research_retry`; `test_tier4_registers_all_eleven_jobs` → thirteen; research-time analogues of the three nightly tests (`79-108`). `tests/test_schedule_reconciler.py`: line `100` → `day_of_week='mon-fri'`; add `test_research_time_default/reads_db/malformed`, `test_reconcile_updates_research_scan_time` (clone of `364-389`), `test_job_research_scan_posts_to_portfolio_url`, `test_job_research_scan_skips_holiday` (monkeypatch `market_calendar.today_et`), `test_job_research_retry_*` (no row → POST after grace; failed+1 attempt+before cutoff → POST; 2 attempts → no POST; after cutoff → no POST; completed → no POST).

### 9. `web/credentials.py`

After `# TIER:2 END` (`144`) add a `# TIER:3 BEGIN … END` block with `{"key": "SCHEDULE_RESEARCH_TIME", "label": "Daily shared research start (ET, HH:MM)", "group": "Automation Schedule", "secret": False, "type": "text", "placeholder": "00:00 — Mon-Fri; the one research pass every paper account allocates from"}`. It renders in the Settings tab (`list_settings_meta`, `178-220`) and saves through `PUT /api/settings/{key}` (`main.py:239-248`). Test: `tests/test_research_schedule_settings.py` cloning `tests/test_schedule_settings.py` with the TIER:3 assertion.

### 10. Frontend (vanilla JS, no build)

- `web/static/portfolio.js:55-61`: `SCAN_TYPE_TAG.research = "rsch"`; `scanTypeKey`: `kind === "research"` → `"research"`. `spy.js:284` `only: ["spy", "research"]`; `options.js:399` `only: ["options", "research"]`. Grep `app.js` for `scanTypeKey`/`SCAN_TYPE_TAG` and mirror.
- `web/static/spy.js`: default `schedule_time: "09:00"` (`153`); `triggerSpyScan` (`766-789`) requires `activePaperAccountId` (message like `options.js:801-804`), button "Allocate now"; progress (`535-545`): totals default 151/51, gate notes for `running_wait_research` ("Waiting for today's shared research…") / `running_wait_market` / `running_wait_alloc` / `running_alloc` (copy `options.js:576-582`). New `loadResearchStatus()` (GET `/api/research-scans/today`, renders into `#spy-research-status` and, if present, `#opt-research-status`: "Today's research #id · status · quick q/151 · deep d/51 (r reused)" plus a "Run research" button → POST `/api/research-scan`) called from `loadSpyHistory` and the 15 s interval (`903-909`). `loadResearchTime()/saveResearchTime()` cloned from `portfolio.js:719-758` with key `SCHEDULE_RESEARCH_TIME`, default `"00:00"`, elements `#spy-research-time`, `#spy-research-time-status`.
- `web/static/options.js`: default `"09:00"` (`140`); `optProgressHtml` (`570-595`): totals 151/51, add the `running_wait_research` note; call `loadResearchStatus()` from `loadOptionsHistory`.
- `web/static/index.html` (inside existing TIER:3 / TIER:4 blocks): caption `343` → "Trades every weekday from the shared 00:00 ET research (top 150 movers + SPY quick-scanned, top 50 + SPY deep-dived). Each account allocates at its own time; fills use live quotes after 09:35 ET."; S&P header (`344-356`): `<span id="spy-research-status">` row and a `Research time (ET)` `<input type="time" id="spy-research-time">` + status span; `385` label "Allocation time (ET)"; `391` helper "A blank allocation time means the account only trades when you click Allocate. Trades fill after the 09:35 ET open from that morning's shared research …"; `419` caption "…Auto-allocates Mon–Fri at each account's time (default 09:00 ET) from the shared 00:00 research; fills after 09:35 ET."; `474` label, `479` helper (drop "a scan time before the open just queues the build"); `<span id="opt-research-status">` in the options header (`426-432`).

Tests: `tests/test_spy_account_form_js.py:237` and `tests/test_opt_account_form_js.py:188` → `"09:00"`; `tests/test_scan_activity_js.py` — a research item renders the `rsch` tag; `tests/test_account_form_markup.py` — `spy-research-time`/`spy-research-status` inside TIER:3 and `opt-research-status` inside TIER:4; new `tests/test_spy_research_js.py` (tier-3 manifest) cloning `tests/test_portfolio_schedule_js.py` for `loadResearchTime/saveResearchTime` and `loadResearchStatus`.

### 11. Docs

`CHANGELOG.md` `[Unreleased]` (`9`): "Changed — shared daily research…", the bias consequence (§0.4), the schedule_time migration to 09:00, the new setting, `research_scan_id`, `STUCK_SCAN_STALL_MIN` 120, holiday calendar. `README.md`: `39`, `97`, `116`, `169` (Saturday), `289-297` (weekly → daily allocation from shared research), `303-304` (research 00:00 / allocation 09:00 / 09:35 fills); `tests/test_readme.py::_FORBIDDEN` add `("every Saturday", …)` and `("default 07:30 ET", …)`. `ARCHITECTURE.md:47` scheduler row and `162` ("weekly JSON snapshot" → "daily"). `CLAUDE.md` tier table row 3. `web/scheduler.py` docstring. `scripts/make_tier.py` `TIER_IDENTITY[3]`.

### 12. Ordered steps (suite green after each)

1. `market_calendar.py` + tests; `options_data` delegates.
2. `db.py` column/allow-list/helpers/backfills, 09:00 default flip in the backfill + `test_paper_account_schema.py`.
3. `spy_scanner` heartbeat + `fetch_live_prices`; scheduler stall default 120.
4. `research_engine.py` with the moved code; `options_engine` imports; move tests to `test_research_engine.py`; manifest + canary. Behaviour unchanged.
5. `run_research`, `research_routes`, queue dispatch, nginx, `portfolio_main` waiting list; tests.
6. `spy_allocator` cadence + fallback fix; tests.
7. Allocation workers + route changes (options and equity), `today_et`, trading-day gate; rewrite the affected tests.
8. Scheduler jobs/settings/reconciler/retry; `credentials` registry; tests.
9. Route + JS defaults to 09:00; `_DEFAULT_SCHEDULE_TIME`; tests.
10. UI work; JS tests.
11. Docs/CHANGELOG/README/ARCHITECTURE/CLAUDE.md/identity.
12. Verification (§13).

### 13. Verification

- Local: `uv run --extra web python -m pytest -q`; `ruff check .`; `python scripts/make_tier.py --lint`; `python scripts/make_tier.py --tier 3 --check`, `--tier 2 --check`, `--tier 1 --check` (CI runs the same three strips).
- Mocked end-to-end `tests/test_shared_research_e2e.py` (tier-4 manifest): `fastapi.testclient.TestClient(portfolio_main.app)` with `FEATURES=schwab,sp500,options`, `DB_PATH` in tmp, monkeypatches for `spy_scanner._llm_quick`/`_invoke_with_retry`, `spy_scanner.SwitchboardOrchestrator` (fake returning `Rating: Buy`), `research_engine.yf.download`, `spy_scanner.yf.download`, `spy_scanner.fetch_live_prices`, `options_data.fetch_contract`, `options_engine.refresh_positions`, `research_engine.now_et` (a Tuesday 09:40). Create 2 equity + 3 options accounts; `POST /api/research-scan` → run the background task synchronously; assert research completed with 151 quick rows and 51 `analysis_id`s; then `POST /api/spy-scan {account_id}` ×2 and `/api/options-scan` ×3; assert each row: `research_scan_id` set, quick rows copied, status `completed`, equity `portfolio_json` non-empty with `entry_price == live price`, options ledger has opens; `/api/portfolio/status` shows waiting rows during the wait phase (with `now_et` at 09:10).
- Production (after redeploy): on a weekday evening `docker exec tradingagents-tradingagents-scheduler-1 python -m web.scheduler --run-research-now`; watch `docker logs -f tradingagents-tradingagents-portfolio-1 | grep -E '\[(spy|research) '` and `GET /api/research-scans/today` (`running_quick` in ~30 s, `running_deep` by ~4 min, `completed` in 3–4 h; `updated_at` must move at least every 2 min during deep dives). Confirm the reaper logs nothing. Next trading day: at 09:00 ET `GET /api/portfolio/status` → five rows waiting with `running_wait_research`/`running_wait_market`; at 09:35–09:41 they flip to `running_alloc` one at a time; `GET /api/spy-scans?kind=equity&limit=2` and `?kind=options&limit=3` show `completed` rows with `research_scan_id`; ledger `opened_at` ≥ 13:35Z; Settings shows `SCHEDULE_RESEARCH_TIME`; edit it and confirm the scheduler log "updated research_scan to HH:MM" within 60 s.

### 14. Risks

- **22:00 Schwab nightly scan overlapping 00:00 research.** Research queues behind it (single slot). If the nightly scan runs > 2 h, research starts late; the 10:30 allocation deadline alerts. Operator option: move `SCHEDULE_NIGHTLY_SCAN_TIME` earlier.
- **Reaper vs long LLM calls.** With the heartbeat thread the 120-min stall is unreachable while the process is alive; a true OOM kill still trips it (correct).
- **Holiday list is hand-maintained.** `MARKET_HOLIDAYS_EXTRA` covers additions, not removals; revisit annually.
- **Research retry re-runs the quick scan** (`find_reusable_quick_results` needs a completed donor) — 3 min, accepted.
- **Legacy queued rows** at upgrade time would be dispatched as allocations (harmless: they wait for research); deploy with an empty queue (the redeploy pre-flight enforces no running/waiting scan).
- **History filters** in `spy.js`/`options.js` list only completed/failed/cancelled; waiting allocation rows show in the Queue panel's `waiting` list — the research status line must make that obvious on day one.
