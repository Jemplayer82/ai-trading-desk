"""LLM allocator: 50 deep-dive results → $100k paper portfolio (or daily rebalance).

Final phase of the S&P equity allocation (web/spy_routes._run_equity_allocation). One quick-LLM
call turns the enriched candidates into a JSON array of allocations; if the
call or its JSON parse fails, a deterministic equal-weight fallback runs so a
scan never finishes without a portfolio.

Allocation dict (persisted as spy_scans.portfolio_json; consumed by
spy_scanner.refresh_portfolio_prices and /api/spy-account/compare):
    ticker, action, allocation_pct, dollar_amount, entry_price, rationale
    peak_price, pending_stop_limit, stop_limit_price, current_price
        stop-state carried across a rebalance by carry_forward_state()
    shares, cost_basis              added here post-LLM (whole-share conversion)
    current_price, current_value    added later by refresh_portfolio_prices

`action` is "NEW" | "HOLD" | "ADDED" | "TRIMMED" | "EXITED". EXITED rows are
kept (shares=0) as a paper trail; live_positions() below is the canonical
filter for action != "EXITED", and downstream code that can import
spy_allocator should prefer it. It is not literally the only place the
comparison appears: web/scheduler.py inlines the same check because it ships
at the base tier where spy_allocator is removed, and a couple of tight
per-position loops in spy_scanner.py preserve the inline comparison by
design (verbatim refactor, avoids a cross-module call in a hot loop).

Two modes: fresh (week 1, $100k) vs rebalance (week 2+, capital = the previous
scan's refreshed value; kept positions retain their original entry_price so
P&L stays anchored to actual cost).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from tradingagents.default_config import DEFAULT_CONFIG

from .llm_helpers import llm_for

log = logging.getLogger(__name__)

STOP_EXIT_REASONS = frozenset({"stop_loss", "trail_stop", "stop_limit"})
CARRIED_STOP_STATE_KEYS = (
    "peak_price",
    "pending_stop_limit",
    "stop_limit_price",
    "current_price",
)
# These keys are owned by the portfolio scanner/refresher and must never be
# authored by the allocator's LLM or fallback output.
UNTRUSTED_LLM_KEYS = (
    *CARRIED_STOP_STATE_KEYS,
    "exit_reason",
    "exit_price",
    "exit_proceeds",
    "current_value",
)
RETAINED_ACTIONS = ("HOLD", "ADDED", "TRIMMED")


def live_positions(portfolio: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return live (non-EXITED) rows from a portfolio snapshot, preserving order.

    This is the canonical filter: every other module should call this helper
    rather than inline ``p.get("action") != "EXITED"`` checks, so EXITED stop-
    loss paper-trail rows are never accidentally treated as holdings.
    """
    return [p for p in (portfolio or []) if p.get("action") != "EXITED"]


def stopped_positions(portfolio: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return rows stamped with a stop-driven exit_reason.

    Weekly-rebalance EXITED rows carry no exit_reason and are intentionally
    excluded here; only mid-week stop exits (stop_loss, trail_stop,
    stop_limit) are selected.
    """
    return [p for p in (portfolio or []) if p.get("exit_reason") in STOP_EXIT_REASONS]


def _strip_untrusted_keys(allocations: list[dict[str, Any]] | None) -> None:
    """Remove system-owned stop/exit/current keys from LLM/fallback output.

    Mutates ``allocations`` in place. These keys must only be written by the
    portfolio scanner/refresher, never carried straight through from the
    allocator's JSON output.
    """
    for alloc in allocations or []:
        if not isinstance(alloc, dict):
            continue
        for key in UNTRUSTED_LLM_KEYS:
            alloc.pop(key, None)


def carry_forward_state(
    allocations: list[dict[str, Any]],
    previous_portfolio: list[dict[str, Any]] | None,
) -> None:
    """Carry forward entry_price and stop-state for retained positions.

    Mutates ``allocations`` in place. EXITED rows from the previous portfolio
    are never used as a source. For positions whose action is HOLD/ADDED/TRIMMED
    and that still have a live previous row, copy the original entry_price and
    any present stop-state keys (including ``current_price`` so the first post-
    rebalance mark-to-market has a valid previous mark to evaluate against the
    stop level). The previous row's value always wins for these keys, even if
    the incoming allocation dict already contained different values.
    The peak_price is then clamped to be at least the carried entry_price so a
    trailing stop can never ratchet below cost.
    """
    prev_map = {p["ticker"]: p for p in live_positions(previous_portfolio)}
    for alloc in allocations:
        if alloc.get("action") not in RETAINED_ACTIONS:
            continue
        prev = prev_map.get(alloc["ticker"])
        if not prev:
            continue

        if prev.get("entry_price"):
            alloc["entry_price"] = prev["entry_price"]

        for key in CARRIED_STOP_STATE_KEYS:
            if prev.get(key) is not None:
                alloc[key] = prev[key]

        if "peak_price" in alloc and "entry_price" in alloc:
            alloc["peak_price"] = max(alloc["peak_price"], alloc["entry_price"])


# ─── Fresh allocation (week 1 or first ever run) ──────────────────────────────

_FRESH_SYSTEM_TEMPLATE = """You are a quantitative portfolio manager. You have up to
${capital:,.0f} of paper capital and a shortlist of deep-dive analyses for the top S&P
500 candidates.
{bias_context}
Capital discipline (IMPORTANT):
- The candidate list is a SHORTLIST for review — NOT a mandate to buy all of them.
- You do NOT have to deploy the full ${capital:,.0f}. Hold the remainder as cash whenever
  there aren't enough genuinely compelling, high-conviction opportunities.
- Only deploy capital to names you would actually buy with real money. It is
  perfectly fine (and often correct) to invest well under ${capital:,.0f} in just a
  handful of positions and leave the rest in cash.

Rules:
- Total invested must be ≤ ${capital:,.0f} (anything not invested is cash).
- Minimum position: $500. Maximum position: ${max_pos:,.0f} ({max_pct}% of capital).
- At least {min_cash_pct}% of capital must remain as cash (uninvested buffer).
- Weight positions by signal strength and conviction score.
- Only BUY-rated tickers should receive meaningful allocation (>1%).
- HOLD tickers may receive small allocations (0.5–3%) as speculative, or none.
- SELL-rated and low-conviction tickers get $0 (just omit them).
- Set "action" to "NEW" for every position (this is a fresh portfolio).
- Return ONLY valid JSON — no prose, no markdown fences.

Output format (array of objects):
[
  {{"ticker": "NVDA", "action": "NEW", "allocation_pct": 8.5, "dollar_amount": 8500,
   "entry_price": 145.23, "rationale": "...one sentence..."}},
  ...
]
"""

_REBALANCE_SYSTEM_TEMPLATE = """You are a quantitative portfolio manager running a
weekly rebalance of a paper portfolio.
{bias_context}
You will receive:
1. The total capital available for this week (starting value).
2. Current holdings with this week's updated signals.
3. New high-conviction candidates not yet in the portfolio.

Capital discipline (IMPORTANT):
- The candidate list is a SHORTLIST for review — NOT a mandate to hold all of them.
- You do NOT have to stay fully invested. Total invested may be LESS than the
  starting capital; hold the remainder as cash when conviction is thin.
- Raise cash by trimming or exiting when there aren't enough compelling ideas.

Rebalancing rules:
- Total invested must be ≤ the starting capital (the rest is cash).
- Minimum position: $500. Maximum: {max_pct}% of starting capital.
- At least {min_cash_pct}% of capital must remain as cash (uninvested buffer).
- EXITED positions (SELL signal or dropped out of top candidates) free up capital.
- Kept positions maintain their original entry_price for P&L tracking.
- New positions use the current entry_price provided.
- Positions listed under STOPPED OUT are already closed and their capital is already reflected in the starting capital, so re-entering one is a deliberate NEW position it should justify in its rationale.
- Weight by conviction; BUY > HOLD in allocation size.
- Trim HOLD positions to make room for high-conviction BUYs.
- Return ONLY valid JSON — no prose, no markdown fences.

Action values:
  "NEW"     — new position added this week
  "HOLD"    — existing position kept at similar weight
  "ADDED"   — existing position, allocation increased
  "TRIMMED" — existing position, allocation decreased
  "EXITED"  — position closed (include with dollar_amount: 0 for the record)

Output format (array of objects, include EXITED positions with dollar_amount 0):
[
  {{"ticker": "NVDA", "action": "HOLD", "allocation_pct": 8.5, "dollar_amount": 8500,
   "entry_price": 145.23, "rationale": "...one sentence..."}},
  {{"ticker": "META", "action": "EXITED", "allocation_pct": 0, "dollar_amount": 0,
   "entry_price": 520.00, "rationale": "Signal flipped to SELL."}},
  ...
]
"""
_BIAS_CONTEXT = {
    "bullish": "\nMarket stance: BULLISH — prefer larger positions on high-conviction BUYs. "
               "Deploy more capital when signals are strong. In borderline Buy/Hold cases, lean Buy.\n",
    "bearish": "\nMarket stance: BEARISH — prefer smaller positions and higher cash buffers. "
               "Be selective; only deploy to the highest-conviction BUYs. In borderline Hold/Sell cases, lean Sell.\n",
    "neutral": "",
}

_DAILY_REBALANCE_ADDENDUM = (
    "\nDAILY CADENCE (overrides the weekly framing above): This is a DAILY check-in "
    "against a fresh research pass, not a weekly rebalance. Default every existing "
    "position to HOLD at its current size. EXIT only on a SELL/Underweight rating or "
    "a conviction collapse; open NEW positions only for BUY/Overweight candidates with "
    "conviction ≥ 8; never ADD/TRIM merely to re-weight. Holdings marked 'not re-rated "
    "today' have NO new signal — HOLD them. Turnover is a cost.\n"
)

CADENCES = ("weekly", "daily")
_BUY_SIGNALS = ("BUY", "OVERWEIGHT")
_SELL_SIGNALS = ("SELL", "UNDERWEIGHT")
_DAILY_NEW_MIN_CONVICTION = 8


def _position_limits(aggressiveness: int, capital: float) -> tuple[float, int, int]:
    """Return (max_position_dollars, max_pct, min_cash_pct) from aggressiveness 1–10."""
    if aggressiveness <= 3:
        max_pct, min_cash_pct = 7, 20
    elif aggressiveness <= 7:
        max_pct, min_cash_pct = 12, 10
    else:
        max_pct, min_cash_pct = 20, 5
    return capital * max_pct / 100, max_pct, min_cash_pct

FRESH_USER_HEADER = "Date: {trade_date}\nCandidates ({n} tickers):\n"

REBALANCE_USER_HEADER = (
    "Date: {trade_date}\n"
    "Starting capital: ${capital:,.0f}\n\n"
    "=== CURRENT HOLDINGS ({n_held} positions) ===\n"
)

REBALANCE_NEW_HEADER = "\n=== NEW CANDIDATES ({n_new} tickers not currently held) ===\n"

PER_TICKER_TEMPLATE = (
    "{ticker} | signal: {signal} | conviction: {conviction}/10 | "
    "entry_price: ${entry_price:.2f} | {excerpt}\n"
)

REBALANCE_STOPPED_HEADER = (
    "\n=== STOPPED OUT SINCE LAST REBALANCE ({n_stopped} positions — closed "
    "automatically by this account's stop policy, NOT by a prior allocation decision) ===\n"
)

STOPPED_TICKER_TEMPLATE = (
    "{ticker} | closed: {exit_reason} | entry_price: ${entry_price:.2f} | "
    "exit_price: ${exit_price:.2f}\n"
)


def build_rebalance_user_message(
    candidates: list[dict[str, Any]],
    previous_portfolio: list[dict[str, Any]] | None,
    trade_date: str,
    capital: float,
    cadence: str = "weekly",
) -> str:
    """Build the rebalance user message exactly as the legacy inline code did.

    Adds a STOPPED OUT section for mid-week stop exits so the LLM knows those
    tickers were already closed by this account's stop policy and are not
    current holdings.

    With ``cadence="daily"`` a holding missing from ``candidates`` was simply
    not re-rated by today's research pass, so it renders as HOLD (keeping its
    previous conviction) instead of the weekly "dropped out → SELL" framing.
    """
    live_prev = live_positions(previous_portfolio)
    prev_map = {p["ticker"]: p for p in live_prev}
    cand_map = {c["ticker"]: c for c in candidates}

    held = [c for c in candidates if c["ticker"] in prev_map]
    new_cands = [c for c in candidates if c["ticker"] not in prev_map]
    exits = [p for p in live_prev if p["ticker"] not in cand_map]

    user_msg = REBALANCE_USER_HEADER.format(
        trade_date=trade_date, capital=capital, n_held=len(held) + len(exits)
    )

    for prev in live_prev:
        cand = cand_map.get(prev["ticker"])
        if cand:
            sig = (cand.get("signal") or "—").upper()
            conv = cand.get("conviction") or 0
            excerpt = (cand.get("final_decision") or cand.get("reasoning") or "")[:200]
        elif cadence == "daily":
            sig = "HOLD"
            conv = int(prev.get("conviction") or 0)
            excerpt = "Not re-rated in today's research — no new signal; default HOLD."
        else:
            sig = "SELL"
            conv = 0
            excerpt = "No longer in top candidates — consider exiting."
        user_msg += PER_TICKER_TEMPLATE.format(
            ticker=prev["ticker"],
            signal=sig,
            conviction=conv,
            entry_price=prev.get("entry_price") or 0.0,
            excerpt=excerpt,
        )

    if new_cands:
        user_msg += REBALANCE_NEW_HEADER.format(n_new=len(new_cands))
        for c in new_cands:
            excerpt = (c.get("final_decision") or c.get("reasoning") or "")[:200]
            user_msg += PER_TICKER_TEMPLATE.format(
                ticker=c["ticker"],
                signal=(c.get("signal") or "—").upper(),
                conviction=c.get("conviction") or 0,
                entry_price=c.get("entry_price") or 0.0,
                excerpt=excerpt,
            )

    stopped = stopped_positions(previous_portfolio)
    if stopped:
        user_msg += REBALANCE_STOPPED_HEADER.format(n_stopped=len(stopped))
        for row in stopped:
            user_msg += STOPPED_TICKER_TEMPLATE.format(
                ticker=row["ticker"],
                exit_reason=row.get("exit_reason") or "",
                entry_price=float(row.get("entry_price") or 0),
                exit_price=float(row.get("exit_price") or 0),
            )

    return user_msg


def _llm(config: dict[str, Any]):
    return llm_for(config, deep=False, temperature=0.1)


# ─── Fallbacks ────────────────────────────────────────────────────────────────

def _fallback_fresh(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Equal-weight the BUYs (or top 20 by conviction if none) across $100k."""
    buys = [c for c in candidates if (c.get("signal") or "").upper() in _BUY_SIGNALS]
    if not buys:
        buys = sorted(candidates, key=lambda c: -(c.get("conviction") or 0))[:20]
    total = 100_000
    per = round(total / len(buys), 2)
    return [
        {
            "ticker": c["ticker"],
            "action": "NEW",
            # per/1000 == per / $100k * 100, i.e. pct of the fixed fresh capital.
            "allocation_pct": round(per / 1000, 2),
            "dollar_amount": per,
            "entry_price": c.get("entry_price", 0),
            "rationale": f"Fallback equal-weight (conviction {c.get('conviction','?')}/10).",
        }
        for c in buys
    ]


def _fallback_rebalance(
    candidates: list[dict[str, Any]],
    previous_portfolio: list[dict[str, Any]],
    starting_value: float,
) -> list[dict[str, Any]]:
    """Equal-weight BUY candidates using available capital."""
    new_tickers = {c["ticker"] for c in candidates}
    live_prev = live_positions(previous_portfolio)
    prev_map = {p["ticker"]: p for p in live_prev}
    result: list[dict[str, Any]] = []

    buys = [c for c in candidates if (c.get("signal") or "").upper() in _BUY_SIGNALS]
    if not buys:
        buys = sorted(candidates, key=lambda c: -(c.get("conviction") or 0))[:20]

    per = round(starting_value / max(len(buys), 1), 2)
    alloc_pct = round(per / starting_value * 100, 2) if starting_value else 0

    active_tickers = {c["ticker"] for c in buys}

    # Mark exits for live positions no longer active
    for prev in live_prev:
        if prev["ticker"] not in new_tickers or prev["ticker"] not in active_tickers:
            result.append({
                "ticker": prev["ticker"],
                "action": "EXITED",
                "allocation_pct": 0,
                "dollar_amount": 0,
                "entry_price": prev.get("entry_price", 0),
                "rationale": "Dropped from top candidates or SELL signal.",
            })

    # Allocate to buys
    for c in buys:
        prev = prev_map.get(c["ticker"])
        action = "HOLD" if prev else "NEW"
        entry = prev["entry_price"] if prev else c.get("entry_price", 0)
        result.append({
            "ticker": c["ticker"],
            "action": action,
            "allocation_pct": alloc_pct,
            "dollar_amount": per,
            "entry_price": entry,
            "rationale": f"Fallback equal-weight (conviction {c.get('conviction','?')}/10).",
        })

    return result


def _fallback_daily(
    candidates: list[dict[str, Any]],
    previous_portfolio: list[dict[str, Any]],
    capital: float,
) -> list[dict[str, Any]]:
    """Low-churn daily fallback: hold everything, act only on new signals.

    Live holdings are kept as HOLD at their previous size unless today's
    research rates them SELL/Underweight (→ EXITED). Non-held BUY/Overweight
    candidates with conviction ≥ 8 split the free cash (capital minus the
    held dollars) equally, highest conviction first.
    """
    cand_map = {c["ticker"]: c for c in candidates}
    live_prev = live_positions(previous_portfolio)
    held_tickers = {p["ticker"] for p in live_prev}
    result: list[dict[str, Any]] = []

    for prev in live_prev:
        cand = cand_map.get(prev["ticker"])
        signal = ((cand or {}).get("signal") or "").upper()
        if signal in _SELL_SIGNALS:
            result.append({
                "ticker": prev["ticker"],
                "action": "EXITED",
                "allocation_pct": 0,
                "dollar_amount": 0,
                "entry_price": prev.get("entry_price", 0),
                "rationale": f"Fallback: daily exit on {signal} rating.",
            })
            continue
        row = {
            "ticker": prev["ticker"],
            "action": "HOLD",
            "allocation_pct": prev.get("allocation_pct", 0),
            "dollar_amount": prev.get("dollar_amount", 0),
            "entry_price": prev.get("entry_price", 0),
            "rationale": "Fallback: daily hold (no actionable signal change).",
        }
        for key in ("shares", "cost_basis"):
            if key in prev:
                row[key] = prev[key]
        result.append(row)

    held_dollars = sum(
        float(r.get("dollar_amount") or 0) for r in result if r["action"] == "HOLD"
    )
    free_cash = capital - held_dollars

    buys = sorted(
        (
            c for c in candidates
            if c["ticker"] not in held_tickers
            and (c.get("signal") or "").upper() in _BUY_SIGNALS
            and (c.get("conviction") or 0) >= _DAILY_NEW_MIN_CONVICTION
        ),
        key=lambda c: -(c.get("conviction") or 0),
    )
    if buys and free_cash > 0:
        per = round(free_cash / len(buys), 2)
        alloc_pct = round(per / capital * 100, 2) if capital else 0
        for c in buys:
            result.append({
                "ticker": c["ticker"],
                "action": "NEW",
                "allocation_pct": alloc_pct,
                "dollar_amount": per,
                "entry_price": c.get("entry_price", 0),
                "rationale": f"Fallback: daily new position (conviction {c.get('conviction','?')}/10).",
            })

    return result


# ─── Public API ───────────────────────────────────────────────────────────────

def run(
    candidates: list[dict[str, Any]],
    trade_date: str,
    config: dict[str, Any],
    previous_portfolio: list[dict[str, Any]] | None = None,
    starting_value: float | None = None,
    aggressiveness: int = 5,
    bias: str = "neutral",
    cadence: str = "weekly",
) -> dict[str, Any]:
    """Return {allocations, total, cash, report_md, starting_value}.

    If previous_portfolio is provided (week 2+), performs a rebalance;
    otherwise allocates a fresh portfolio (week 1). aggressiveness (1–10)
    controls position sizing limits; bias (bullish/neutral/bearish) shifts
    the LLM prompt toward more or less aggressive stance.

    cadence is "weekly" (default, legacy behaviour byte-for-byte) or "daily".
    A daily rebalance appends a low-churn addendum to the system prompt,
    renders holdings absent from today's research as HOLD rather than SELL,
    falls back to _fallback_daily (hold unless SELL/Underweight; open only
    conviction ≥ 8 BUY/Overweight names), and titles the report "Daily
    allocation". A daily run with live holdings but no candidates still goes
    through the LLM/fallback so the holdings are kept, not wiped. Fresh mode is
    identical for both cadences. Any other cadence raises ValueError.
    """
    if cadence not in CADENCES:
        raise ValueError(f"cadence must be one of {CADENCES}, got {cadence!r}")

    if not candidates and (cadence == "weekly" or not live_positions(previous_portfolio)):
        return {"allocations": [], "total": 0, "report_md": "No candidates provided.",
                "starting_value": starting_value or 100_000}

    is_rebalance = bool(previous_portfolio)
    capital = starting_value if starting_value else 100_000.0
    max_pos, max_pct, min_cash_pct = _position_limits(aggressiveness, capital)
    bias_context = _BIAS_CONTEXT.get(bias, "")

    # ── Build the user message ────────────────────────────────────────────────
    if not is_rebalance:
        user_msg = FRESH_USER_HEADER.format(trade_date=trade_date, n=len(candidates))
        for c in candidates:
            excerpt = (c.get("final_decision") or c.get("reasoning") or "")[:300]
            user_msg += PER_TICKER_TEMPLATE.format(
                ticker=c["ticker"],
                signal=(c.get("signal") or "—").upper(),
                conviction=c.get("conviction") or 0,
                entry_price=c.get("entry_price") or 0.0,
                excerpt=excerpt,
            )
        system = _FRESH_SYSTEM_TEMPLATE.format(
            capital=capital, max_pos=max_pos, max_pct=max_pct,
            min_cash_pct=min_cash_pct, bias_context=bias_context,
        )
        fallback_fn = lambda: _fallback_fresh(candidates)
    else:
        user_msg = build_rebalance_user_message(
            candidates, previous_portfolio, trade_date, capital, cadence=cadence
        )
        system = _REBALANCE_SYSTEM_TEMPLATE.format(
            max_pct=max_pct, min_cash_pct=min_cash_pct, bias_context=bias_context,
        )
        if cadence == "daily":
            system += _DAILY_REBALANCE_ADDENDUM
        if cadence == "daily" and live_positions(previous_portfolio):
            fallback_fn = lambda: _fallback_daily(candidates, previous_portfolio or [], capital)
        else:
            fallback_fn = lambda: _fallback_rebalance(candidates, previous_portfolio or [], capital)

    # ── Call the LLM ─────────────────────────────────────────────────────────
    llm = _llm({**DEFAULT_CONFIG, **config})
    try:
        resp = llm.invoke([
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ])
        raw = resp.content if hasattr(resp, "content") else str(resp)
        raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw.strip())
        allocations: list[dict[str, Any]] = json.loads(raw)
    except Exception as exc:
        log.exception("Allocator LLM call failed: %s", exc)
        allocations = fallback_fn()

    # ── Strip system-owned keys that the LLM or fallback must never author ─────
    _strip_untrusted_keys(allocations)

    # ── Carry forward entry_price and stop-state for retained positions ───────
    if is_rebalance:
        carry_forward_state(allocations, previous_portfolio)

    # ── Convert dollar targets into WHOLE shares (real paper-trade fills) ─────
    # shares = floor(target $ / entry price); the rounding remainder stays cash.
    kept: list[dict[str, Any]] = []
    for a in allocations:
        if a.get("action") == "EXITED":
            a["shares"] = 0
            a["cost_basis"] = 0.0
            kept.append(a)
            continue
        ep = float(a.get("entry_price") or 0)
        target = float(a.get("dollar_amount") or 0)
        shares = int(target // ep) if ep > 0 else 0
        if shares <= 0:
            # Can't afford a single whole share — that money stays in cash.
            continue
        a["shares"] = shares
        a["cost_basis"] = round(shares * ep, 2)
        a["allocation_pct"] = round(a["cost_basis"] / capital * 100, 2) if capital else 0.0
        kept.append(a)
    allocations = kept

    total = sum(a.get("cost_basis", 0) for a in allocations if a.get("action") != "EXITED")
    cash = max(0.0, capital - total)
    cash_pct = (cash / capital * 100) if capital else 0.0

    # ── Build markdown report ─────────────────────────────────────────────────
    if is_rebalance and cadence == "daily":
        mode_label = "Daily allocation"
    else:
        mode_label = "Rebalance" if is_rebalance else "Initial Portfolio"
    bias_label = bias.capitalize() if bias != "neutral" else "Neutral"
    lines = [
        f"# S&P 500 Paper Portfolio — {trade_date} ({mode_label})",
        f"**Starting capital:** ${capital:,.0f}  ",
        f"**Aggressiveness:** {aggressiveness}/10 | **Bias:** {bias_label}  ",
        f"**Total deployed:** ${total:,.0f} ({(total / capital * 100) if capital else 0:.1f}%)  ",
        f"**Cash (uninvested):** ${cash:,.0f} ({cash_pct:.1f}%)  ",
        f"**Positions:** {len([a for a in allocations if a.get('action') != 'EXITED'])}",
        "",
    ]
    if is_rebalance:
        exited = [a for a in allocations if a.get("action") == "EXITED"]
        new = [a for a in allocations if a.get("action") == "NEW"]
        lines += [
            f"**New positions:** {len(new)}  ",
            f"**Exited positions:** {len(exited)}  ",
            "",
        ]

    lines += [
        "| Ticker | Action | Signal | Shares | Entry $ | Cost | % | Rationale |",
        "|--------|--------|--------|--------|---------|------|---|-----------|",
    ]
    for a in sorted(allocations, key=lambda x: -(x.get("cost_basis") or 0)):
        is_exit = a.get("action") == "EXITED"
        lines.append(
            f"| {a['ticker']} | {a.get('action','—')} | {a.get('signal','').upper() or '—'} "
            f"| {'—' if is_exit else a.get('shares', 0)} "
            f"| ${a.get('entry_price', 0):,.2f} "
            f"| {'—' if is_exit else '$' + format(a.get('cost_basis', 0), ',.0f')} "
            f"| {a.get('allocation_pct', 0):.1f}% "
            f"| {(a.get('rationale') or '')[:80]} |"
        )

    return {
        "allocations": allocations,
        "total": total,
        "cash": round(cash, 2),
        "report_md": "\n".join(lines),
        "starting_value": capital,
    }
