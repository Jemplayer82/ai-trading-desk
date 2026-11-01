import os

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")

# Single source of truth for env-var → config-key overrides. To expose
# a new config key for environment-based override, add a row here — no
# entry-point script changes required. Coercion is driven by the type
# of the existing default, so users can keep writing plain strings in
# their .env file.
_ENV_OVERRIDES = {
    "TRADINGAGENTS_LLM_PROVIDER":         "llm_provider",
    "TRADINGAGENTS_DEEP_THINK_LLM":       "deep_think_llm",
    "TRADINGAGENTS_QUICK_THINK_LLM":      "quick_think_llm",
    "TRADINGAGENTS_LLM_BACKEND_URL":      "backend_url",
    "TRADINGAGENTS_OUTPUT_LANGUAGE":      "output_language",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS":    "max_debate_rounds",
    "TRADINGAGENTS_MAX_RISK_ROUNDS":      "max_risk_discuss_rounds",
    "TRADINGAGENTS_CHECKPOINT_ENABLED":   "checkpoint_enabled",
    "TRADINGAGENTS_BENCHMARK_TICKER":     "benchmark_ticker",
    "TRADINGAGENTS_REFLECTION_HOLDING_DAYS":       "reflection_holding_days",
    "TRADINGAGENTS_NOISE_ALPHA_THRESHOLD":         "noise_alpha_threshold",
    "TRADINGAGENTS_SWEEP_MAX_REFLECTIONS_PER_RUN": "sweep_max_reflections_per_run",
    "TRADINGAGENTS_SWEEP_CENSOR_AFTER_DAYS":       "sweep_censor_after_days",
    "TRADINGAGENTS_MEMORY_CONTEXT_MAX_AGE_DAYS":   "memory_context_max_age_days",
    "TRADINGAGENTS_MEMORY_CONTEXT_DECISION_MAX_CHARS": "memory_context_decision_max_chars",
    "TRADINGAGENTS_DEEP_DIVE_STORE_DECISIONS":     "deep_dive_store_decisions",
    "TRADINGAGENTS_RESEARCH_MANAGER_ROLE":         "research_manager_role",
    "TRADINGAGENTS_DEEP_DIVE_REUSE":                "deep_dive_reuse",
    "TRADINGAGENTS_DEEP_DIVE_REUSE_MAX_AGE_HOURS":  "deep_dive_reuse_max_age_hours",
    "TRADINGAGENTS_QUICK_SCAN_REUSE":               "quick_scan_reuse",
    "TRADINGAGENTS_MACRO_BRIEF_ENABLED":            "macro_brief_enabled",
    "TRADINGAGENTS_OPTIONS_LESSONS_MIN_CLOSED":    "options_lessons_min_closed",
    "TRADINGAGENTS_OPTIONS_REFLECT_MIN_NEW_CLOSED": "options_reflect_min_new_closed",
    "TRADINGAGENTS_OPTIONS_INTRADAY_STOP":         "options_intraday_stop",
}


def _coerce(value: str, reference):
    """Coerce env-var string to the type of the existing default value."""
    if isinstance(reference, bool):
        return value.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(reference, int) and not isinstance(reference, bool):
        return int(value)
    if isinstance(reference, float):
        return float(value)
    return value


def _apply_env_overrides(config: dict) -> dict:
    """Apply TRADINGAGENTS_* env vars to the config dict in-place."""
    for env_var, key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        config[key] = _coerce(raw, config.get(key))
    return config


DEFAULT_CONFIG = _apply_env_overrides({
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", os.path.join(_TRADINGAGENTS_HOME, "logs")),
    "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", os.path.join(_TRADINGAGENTS_HOME, "cache")),
    "memory_log_path": os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH", os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md")),
    # Optional cap on the number of resolved memory log entries. When set,
    # the oldest resolved entries are pruned once this limit is exceeded.
    # Pending entries are never pruned. None disables rotation entirely.
    # Capped by default so a nightly sweep + portfolio-scan decision volume
    # can't grow the log (and the injected context source) without bound.
    # 1000 (was 300): deep dives now store their decisions too (~50/weekday
    # from the options scan + ~50/Saturday from the S&P scan) — at 300 the
    # rotation window churned in under 6 trading days, evicting all
    # interactive/portfolio history. 1000 ≈ 4 weeks (~1-3 MB file).
    "memory_log_max_entries": 1000,
    # --- Outcome resolution / reflection (nightly sweep) ---
    # Forward-return window (trading days) a decision is graded over. The
    # maturity guard refuses to resolve an entry before this window has data.
    "reflection_holding_days": 5,
    # Fixed fallback noise band for |alpha|: below it, outcomes get a canned
    # NOISE reflection (no LLM call) and don't count as directional hits or
    # misses. Used when there isn't enough history for the vol-scaled band.
    "noise_alpha_threshold": 0.02,
    # Volatility-scaled noise band: N = clamp(sigma_5d * frac, min, max).
    # A fixed band mislabels noise as conviction on high-vol names (crypto).
    "noise_band_sigma_frac": 0.5,
    "noise_band_min": 0.015,
    "noise_band_max": 0.06,
    # LLM-reflection budget per sweep run — a large backlog drains over
    # several nights instead of one expensive burst. Canned NOISE/CENSORED
    # reflections are free and never capped.
    "sweep_max_reflections_per_run": 50,
    # Entries older than this (calendar days) whose price series ended early
    # resolve as CENSORED (probable delisting/halt) instead of pending forever
    # — otherwise the worst blowups silently vanish from the record.
    "sweep_censor_after_days": 30,
    # Resolved entries older than this are excluded from injected context so
    # a dead regime's lessons expire. None disables the cutoff.
    "memory_context_max_age_days": 180,
    # Cap on the DECISION body length (characters) embedded per past entry in
    # get_past_context -> _format_full. Only the long-form DECISION body is
    # truncated; the tag line and REFLECTION are never truncated. Keeps up to
    # 5 same-ticker rendered Portfolio Manager decisions (commonly 300-600+
    # chars each) from dominating the PM prompt.
    "memory_context_decision_max_chars": 400,
    # --- Options learning (web/options_learning.py) ---
    # Kill switch for deep dives storing their decisions into the memory log
    # (System C). Env-only rollback path: no redeploy needed to stop the flow.
    "deep_dive_store_decisions": True,
    # Same-day deep-dive reuse: when multiple scans (different paper accounts,
    # or equity + options) deep-dive the same ticker on the same trade_date
    # with an identical shared-stage config, run the analyst/debate/research-
    # manager stage once and rerun only the Portfolio Manager per account
    # (bias is the only thing it needs that differs per account — see
    # SwitchboardOrchestrator.rerun_decision). Kill switch: env-only rollback,
    # no redeploy needed. A reuse attempt can never fail a scan — any problem
    # with the cached state falls back to a full pipeline run.
    "deep_dive_reuse": True,
    # A donor analysis older than this is never reused, however same-day it
    # is — bounds intraday staleness (an ad-hoc equity scan reusing hours-old
    # analyst reports) and neutralizes the UTC trade_date rollover case (an
    # evening ~22:00 ET run is already dated "tomorrow" in UTC and would
    # otherwise look like a fresh same-day donor to the next morning's scan).
    "deep_dive_reuse_max_age_hours": 6,
    # Scan-level shared macro/global-news brief: web/spy_scanner.py's
    # run_deep_dives computes ONE macro-news summary per scan run (not once
    # per ticker) and injects it into every ticker's news analyst, instead
    # of each dive independently re-fetching/re-summarizing the same
    # ticker-independent macro news via get_global_news. Kill switch:
    # env-only rollback, no redeploy needed — with this off (or on any
    # fetch/summarize failure) a scan falls back to today's per-ticker
    # get_global_news tool-calling behavior. Interactive single-ticker runs
    # are unaffected either way — they never call run_deep_dives, so
    # config['macro_brief'] is never set for them regardless of this flag.
    "macro_brief_enabled": True,
    # Same-day quick-scan reuse: same idea as deep_dive_reuse but for the
    # cheap per-ticker pre-screen call. The bigger effect isn't the quick-LLM
    # savings — it's that reusing quick-scan signal/conviction across scans
    # makes every account converge on the same top-N tickers, which is what
    # drives deep_dive_reuse's hit rate toward 100%. entry_price is always
    # recomputed fresh regardless of this setting (never reused — allocators
    # depend on it and it isn't stored in spy_quick_results).
    "quick_scan_reuse": True,
    # Below this many closed positions the allocator gets NO track-record
    # block at all — a handful of trades is noise, and showing it invites
    # overfitting a tiny sample.
    "options_lessons_min_closed": 10,
    # Nightly batch reflection fires only when at least this many positions
    # closed since the last lessons row (lessons regenerate on new data only).
    "options_reflect_min_new_closed": 5,
    # Hourly refresh closes positions that breach the account's configured
    # stop between daily allocations, filled at the stop level when the mark
    # crossed it this interval (standing-stop emulation) or at the observed
    # quote on a gap. Master emergency off-switch for intraday stop
    # enforcement across all accounts.
    "options_intraday_stop": True,
    # Trailing (and every other) stop behavior is now a PER-ACCOUNT policy
    # stored on `paper_accounts` (stop_type / stop_value / stop_limit_offset)
    # and evaluated by web/account_policy.py, not a global knob — there is no
    # single fixed trailing arm/give-back for the whole deployment anymore.
    # The three global trailing-stop keys that used to live here were removed
    # for exactly this reason: dead config that looks live is a footgun
    # (someone flips a knob expecting it to do something, and nothing reads
    # it). See CHANGELOG.md for the retired key names.
    # Most recent closes included in the single nightly reflection call.
    "options_reflect_batch_max": 20,
    # Hard cap on the lessons block injected into the allocator prompt.
    "options_lessons_max_chars": 1200,
    # LLM settings
    "llm_provider": "openai",
    "deep_think_llm": "gpt-6.1-sol",
    "quick_think_llm": "gpt-6-luna",
    # Which LLM tier drives the Research Manager's debate synthesis.
    # "quick" (default) -- the deep model's route has a documented live
    # timeout failure for this call shape; set to "deep" (env override
    # TRADINGAGENTS_RESEARCH_MANAGER_ROLE) to roll back.
    "research_manager_role": "quick",
    # When None, each provider's client falls back to its own default endpoint
    # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
    # The CLI overrides this per provider when the user picks one. Keeping a
    # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
    # being forwarded to Gemini, producing malformed request URLs).
    "backend_url": None,
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # Checkpoint/resume: when True, LangGraph saves state after each node
    # so a crashed run can resume from the last successful step.
    "checkpoint_enabled": False,
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "English",
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    "analyst_concurrency_limit": 1,
    # News / data fetching parameters
    # Increase for longer lookback strategies or to broaden macro coverage;
    # decrease to reduce token usage in agent prompts.
    "news_article_limit": 20,             # max articles per ticker (ticker-news)
    "global_news_article_limit": 10,      # max articles for global/macro news
    "global_news_lookback_days": 7,       # macro news lookback window
    # Search queries used by get_global_news for macro headlines. Extend or
    # replace to broaden geographic / sector coverage.
    "global_news_queries": [
        "Federal Reserve interest rates inflation",
        "S&P 500 earnings GDP economic outlook",
        "geopolitical risk trade war sanctions",
        "ECB Bank of England BOJ central bank policy",
        "oil commodities supply chain energy",
    ],
    # Data vendor configuration
    # Category-level configuration (default for all tools in category)
    "data_vendors": {
        "core_stock_apis": "yfinance",       # Options: alpha_vantage, yfinance
        "technical_indicators": "yfinance",  # Options: alpha_vantage, yfinance
        "fundamental_data": "yfinance",      # Options: alpha_vantage, yfinance
        "news_data": "yfinance",             # Options: alpha_vantage, yfinance
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
    # Benchmark for alpha calculation in the reflection layer.
    # ``benchmark_ticker`` (when set) overrides the suffix map for all
    # tickers; leave it None to use ``benchmark_map`` for auto-detection
    # based on the ticker's exchange suffix. SPY remains the US default
    # so the reflection label keeps reading "Alpha vs SPY" for US tickers
    # while non-US tickers get their regional index automatically.
    "benchmark_ticker": None,
    "benchmark_map": {
        ".NS":  "^NSEI",    # NSE India (Nifty 50)
        ".BO":  "^BSESN",   # BSE India (Sensex)
        ".T":   "^N225",    # Tokyo (Nikkei 225)
        ".HK":  "^HSI",     # Hong Kong (Hang Seng)
        ".L":   "^FTSE",    # London (FTSE 100)
        ".TO":  "^GSPTSE",  # Toronto (TSX Composite)
        ".AX":  "^AXJO",    # Australia (ASX 200)
        "":     "SPY",      # default for US-listed tickers (no suffix)
    },
})
