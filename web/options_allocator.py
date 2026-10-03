"""LLM allocator for the daily options paper trader.

Turns vetted contract candidates + currently open positions into a decision
set: NEW (buy contracts), HOLD, CLOSE. One quick-LLM call, wrapped in hard code
guardrails on both sides:

  pre-LLM  — force-CLOSE any open position at DTE <= DTE_FLOOR or that has
             violated the account's configured stop policy (the LLM never
             gets to argue with these);
  post-LLM — per-position and total-premium caps scaled by aggressiveness,
             MAX_OPEN_POSITIONS, affordability (never spend past cash), and a
             deterministic conviction-ranked fallback if the call or its JSON
             parse fails, so a build never finishes without a decision set.

Two-layer learning (each layer grades what it can actually measure):
  Layer 1 (System C) — the deep dives that feed this store their directional
  calls in the memory log, graded nightly by forward alpha on the underlying.
  Layer 2 (options ledger) — closed positions are graded by P&L attribution
  (web/options_learning.py: directional vs time/vol-decay split), batch-
  reflected nightly, and the resulting lessons + track-record stats arrive
  here via ``lessons_context``. Lessons are context, never rules: the hard
  guardrails below are code-enforced and are never relaxed by lessons.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from tradingagents.default_config import DEFAULT_CONFIG

from . import account_policy, db, options_data, options_fills
from .llm_helpers import llm_for

log = logging.getLogger(__name__)

# ── Hard guardrails ──────────────────────────────────────────────────────────
DTE_FLOOR = 3          # force-close at or below this many days to expiry
MAX_OPEN_POSITIONS = 15

# aggressiveness tier -> (max % of equity per position, max % of equity deployed)
def position_caps(aggressiveness: int) -> tuple[float, float]:
    if aggressiveness <= 3:
        return 0.05, 0.15
    if aggressiveness <= 7:
        return 0.08, 0.30
    return 0.12, 0.50


_BIAS_CONTEXT = {
    "bullish": "\nMarket stance: BULLISH — lean into calls on high-conviction names; "
               "deploy closer to the premium budget when signals are strong.\n",
    "bearish": "\nMarket stance: BEARISH — favor puts and smaller position counts; "
               "keep plenty of dry powder.\n",
    "neutral": "",
}

_SYSTEM_TEMPLATE = """You are a disciplined options trader managing a paper account that
buys LONG single-leg calls and puts only (no spreads, no short options). Positions may
be held from a day to several weeks — wherever there is money to be made. Long premium
decays: every position must earn its theta.
{bias_context}{lessons_context}
You will receive the account state, currently OPEN positions (with fresh marks and
today's signal on the underlying where available), and NEW pre-vetted contract
candidates from today's scan.

Decide for each open position: HOLD or CLOSE. Decide which candidates to open (NEW)
and with how many contracts. You do not have to open anything; cash is a position.

Hard limits (enforced in code — exceeding them just gets clamped):
- Max premium per position: ${per_cap:,.0f} ({per_pct:.0f}% of equity).
- Max total premium at risk across all open positions: ${total_cap:,.0f} ({total_pct:.0f}% of equity).
- Max {max_positions} open positions. New buys cannot exceed available cash.
- Contracts are whole numbers, minimum 1.

Judgment guidance:
- Close losers whose thesis is broken; let winners run while the signal holds.
- WINNERS RIDE: do not take profit in the first day or two just because a
  position is green. {stop_policy_text} Close a winner early only on a thesis
  break, a signal flip, or DTE burning down toward the floor.
- A position whose underlying flipped signal (e.g. long calls, now SELL) is a strong close.
- Prefer fewer, higher-conviction positions over many small ones.

Return ONLY valid JSON — no prose, no markdown fences. Array of objects:
[
  {{"occ_symbol": "NVDA  260821C00190000", "ticker": "NVDA", "action": "NEW",
    "contracts": 2, "rationale": "...one sentence..."}},
  {{"occ_symbol": "AAPL  260807C00230000", "ticker": "AAPL", "action": "CLOSE",
    "rationale": "Signal flipped to SELL."}},
  {{"occ_symbol": "MSFT  260814P00420000", "ticker": "MSFT", "action": "HOLD",
    "rationale": "Thesis intact."}}
]
Every OPEN position must get a HOLD or CLOSE decision. Candidates you skip may simply
be omitted.
"""

_USER_HEADER = (
    "Date: {trade_date}\n"
    "Account equity: ${equity:,.0f} | cash: ${cash:,.0f} | "
    "open premium at risk: ${at_risk:,.0f} | realized P&L to date: ${realized:+,.0f}\n"
)

_FORCED_HEADER = "\n=== FORCED EXITS (already executed by risk rules — informational) ===\n"
_OPEN_HEADER = "\n=== OPEN POSITIONS ({n}) — decide HOLD or CLOSE for each ===\n"
_CAND_HEADER = "\n=== NEW CANDIDATES ({n}) — pre-vetted liquid contracts ===\n"


def _display(pos_or_cand: dict[str, Any]) -> str:
    """'AAPL 230C 2026-08-21' style label."""
    return "{u} {s:g}{cp} {e}".format(
        u=pos_or_cand.get("underlying") or pos_or_cand.get("ticker") or "?",
        s=options_fills.price(pos_or_cand.get("strike")) or 0,
        cp=(pos_or_cand.get("put_call") or "?")[0],
        e=pos_or_cand.get("expiration_date") or "?",
    )


def _dte(expiration_date: str) -> int:
    try:
        from datetime import datetime
        exp = datetime.strptime(expiration_date, "%Y-%m-%d").date()
        return (exp - options_data.today_et()).days
    except (TypeError, ValueError):
        return 0


def _mark(pos: dict[str, Any]) -> float:
    """Best available per-share mark for an open position.

    `>= 0`, not `> 0`: a contract really can be marked at 0.00 (deep OTM
    decaying to worthless), and that is the single most important case the
    stop-loss exists to catch. Treating a real 0.00 as "no data" fell through
    to entry_premium, which made mark == entry, so the -60% stop-loss test
    could never fire on a -100% position and P&L reported 0% instead.
    """
    bid, _reason = options_fills.sell_quote(pos.get("current_bid"), pos.get("entry_bid"))
    return bid if bid is not None else 0.0



def effective_stop_level(
    pos: dict[str, Any],
    policy: account_policy.StopPolicy,
    *,
    prev_mark: float | None = None,
) -> account_policy.StopOutcome:
    """Apply the account's stop policy to one open position.

    The ONE place an options stop level is computed. Callers: forced_closes
    (the daily allocator backstop — no interval notion, so prev_mark is None)
    and options_engine._apply_intraday_stops (the hourly refresh, which passes
    the pre-refresh bid). Every fill executes at the observed bid.
    Staged evaluations save the ratchet under a short DB writer transaction;
    other stop types remain pure. `armed` comes from stop_triggered_at (stop-limit
    resting fill).
    """
    if policy.stop_type == "trailing_staged" and pos.get("id") is not None:
        return options_fills.bid_stop(db.evaluate_staged_options_stop(pos, policy, prev_mark), _mark(pos))
    return options_fills.bid_stop(account_policy.evaluate(
        policy,
        entry=options_fills.stop_entry(pos),
        peak=options_fills.price(pos.get("peak_premium")) or 0,
        mark=_mark(pos),
        prev_mark=options_fills.price(prev_mark),
        armed=bool(pos.get("stop_triggered_at")),
        stop_level_hwm=options_fills.price(pos.get("stop_level_hwm")),
        allow_zero_entry=True,
    ), _mark(pos))


def forced_closes(
    open_positions: list[dict[str, Any]],
    policy: account_policy.StopPolicy,
) -> list[tuple[dict[str, Any], str, float]]:
    """(position, exit_reason, exit_premium) triples the risk rules close regardless of the LLM."""
    out: list[tuple[dict[str, Any], str, float]] = []
    for pos in open_positions:
        bid, _reason = options_fills.sell_quote(pos.get("current_bid"), pos.get("entry_bid"))
        if bid is None:
            continue
        # DTE floor is unconditional — never gated by stop_type.
        if _dte(pos.get("expiration_date") or "") <= DTE_FLOOR:
            out.append((pos, "dte_floor", _mark(pos)))
            continue
        outcome = effective_stop_level(pos, policy)
        # 'arm' outcomes are owned by the hourly refresh; the daily backstop
        # must never fill a stop-limit below its limit price.
        if outcome.action == "fill":
            out.append((pos, outcome.exit_reason, outcome.fill_price))
    return out


def _days_held(pos: dict[str, Any]) -> int:
    """Calendar days since opened_at (0 for same-day)."""
    try:
        opened = datetime.strptime(str(pos.get("opened_at"))[:10], "%Y-%m-%d").date()
        return max(0, (datetime.utcnow().date() - opened).days)
    except (TypeError, ValueError):
        return 0


def _pnl_pct(pos: dict[str, Any]) -> float:
    entry = float(pos.get("entry_premium") or 0)
    if entry <= 0:
        return 0.0
    return (_mark(pos) / entry - 1) * 100


def _fallback(
    candidates: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    per_cap: float,
    deployable: float,
) -> list[dict[str, Any]]:
    """Deterministic decisions when the LLM is unavailable: HOLD everything
    still open, open conviction-ranked candidates equal-dollar under the caps."""
    decisions: list[dict[str, Any]] = [
        {"occ_symbol": p["occ_symbol"], "ticker": p.get("underlying"),
         "action": "HOLD", "rationale": "Fallback: hold (allocator LLM unavailable)."}
        for p in open_positions
    ]
    ranked = sorted(candidates, key=lambda c: -(c.get("conviction") or 0))
    slots = max(1, min(len(ranked), 8))
    per_target = min(per_cap, deployable / slots) if slots else 0.0
    spent = 0.0
    for c in ranked:
        ask, _reason = options_fills.buy_quote(c.get("bid"), c.get("ask"))
        if ask is None:
            continue
        contracts = int(per_target // (ask * 100))
        if contracts < 1:
            continue
        cost = contracts * ask * 100
        if spent + cost > deployable:
            continue
        spent += cost
        decisions.append({
            "occ_symbol": c["occ_symbol"], "ticker": c.get("ticker"),
            "action": "NEW", "contracts": contracts,
            "rationale": f"Fallback equal-weight (conviction {c.get('conviction', '?')}/10).",
        })
    return decisions


def run(
    candidates: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    trade_date: str,
    config: dict[str, Any],
    equity: float,
    cash: float,
    realized_pnl: float = 0.0,
    aggressiveness: int = 5,
    bias: str = "neutral",
    fresh_signals: dict[str, dict[str, Any]] | None = None,
    lessons_context: str = "",
    *,
    policy: account_policy.StopPolicy,
) -> dict[str, Any]:
    """Decide closes/holds/opens for one options account.

    open_positions: db rows (already marked to market). fresh_signals: today's
    quick/deep signal per underlying ({ticker: {signal, conviction}}).
    lessons_context: optional track-record + lessons block from
    options_learning.format_track_record — "" keeps the prompt byte-identical
    to the lesson-less version.
    Returns {closes, holds, opens, report_md} where closes carry exit_reason,
    and opens carry the full candidate contract + contracts count.
    """
    fresh_signals = fresh_signals or {}
    exclusions = []
    valid_candidates = []
    for candidate in candidates:
        _ask, reason = options_fills.buy_quote(candidate.get("bid"), candidate.get("ask"))
        if reason:
            exclusions.append({"occ_symbol": candidate.get("occ_symbol"), "reason": reason})
        else:
            valid_candidates.append(candidate)
    candidates = valid_candidates
    for position in open_positions:
        bid, reason = options_fills.sell_quote(position.get("current_bid"), position.get("entry_bid"))
        if bid is None:
            exclusions.append({"occ_symbol": position.get("occ_symbol"), "reason": reason})
    # Mirror _BIAS_CONTEXT's newline convention so an empty block changes
    # nothing and a real one gets clean line separation.
    if lessons_context and not lessons_context.startswith("\n"):
        lessons_context = f"\n{lessons_context}\n"
    per_pct, total_pct = position_caps(aggressiveness)
    equity = max(0.0, float(equity))
    per_cap = per_pct * equity
    total_cap = total_pct * equity

    # ── Pre-LLM hard guardrails ──────────────────────────────────────────────
    forced = forced_closes(open_positions, policy)
    forced_ids = {id(p) for p, _r, _f in forced}
    remaining = [p for p in open_positions if id(p) not in forced_ids]

    closes: list[dict[str, Any]] = [
        {"position_id": p["id"], "occ_symbol": p["occ_symbol"],
         "ticker": p.get("underlying"), "exit_reason": reason,
         "exit_premium": fill,
         "rationale": ("DTE floor" if reason == "dte_floor"
                       else "Trailing stop: gave back too much from the peak premium"
                       if reason == "trail_stop"
                       else "Stop-limit: triggered and filled at or above the limit"
                       if reason == "stop_limit"
                       else f"Stop-loss: premium down {-_pnl_pct(p):.0f}% from entry")}
        for p, reason, fill in forced
    ]

    # Cash freed by forced closes is spendable today (closes apply before opens).
    est_cash = cash + sum(fill * 100 * int(p.get("contracts") or 0) for p, _r, fill in forced)
    held_cost = sum(float(p.get("cost_basis") or 0) for p in remaining)

    # ── Build the prompt ─────────────────────────────────────────────────────
    user = _USER_HEADER.format(
        trade_date=trade_date, equity=equity, cash=cash,
        at_risk=held_cost + sum(float(p.get("cost_basis") or 0) for p, _r, _f in forced),
        realized=realized_pnl,
    )
    if forced:
        user += _FORCED_HEADER
        for p, reason, _fill in forced:
            user += f"{_display(p)} | x{p.get('contracts')} | {reason} | P&L {_pnl_pct(p):+.0f}%\n"
    user += _OPEN_HEADER.format(n=len(remaining))
    if not remaining:
        user += "(none)\n"
    for p in remaining:
        fs = fresh_signals.get((p.get("underlying") or "").upper())
        sig_txt = f"today's signal: {fs['signal']} {fs.get('conviction', '?')}/10" if fs else "not scanned today"
        user += (
            f"{p['occ_symbol']} | {_display(p)} | x{p.get('contracts')} | "
            f"held {_days_held(p)}d | {_dte(p.get('expiration_date') or '')}d left | "
            f"entry ${float(p.get('entry_premium') or 0):.2f} | "
            f"mark ${_mark(p):.2f} | P&L {_pnl_pct(p):+.0f}% | {sig_txt}\n"
        )
    user += _CAND_HEADER.format(n=len(candidates))
    if not candidates:
        user += "(none)\n"
    for c in candidates:
        delta_txt = f"delta {abs(c['delta']):.2f}" if c.get("delta") is not None else "delta n/a"
        excerpt = (c.get("final_decision") or c.get("rationale") or "")[:200]
        user += (
            f"{c['occ_symbol']} | {_display(c)} | {c.get('dte')}d | {delta_txt} | "
            f"ask ${float(c.get('ask') or 0):.2f} (${float(c.get('ask') or 0) * 100:,.0f}/contract) | "
            f"OI {c.get('open_interest')} | {c.get('signal')} conviction {c.get('conviction')}/10 | {excerpt}\n"
        )

    system = _SYSTEM_TEMPLATE.format(
        bias_context=_BIAS_CONTEXT.get(bias, ""),
        lessons_context=lessons_context,
        per_cap=per_cap, per_pct=per_pct * 100,
        total_cap=total_cap, total_pct=total_pct * 100,
        max_positions=MAX_OPEN_POSITIONS,
        stop_policy_text=account_policy.describe_policy(policy) + " Entries execute at ask; sells and marks at bid. Fixed stops reference entry bid; trailing peaks begin at entry bid.",
    )

    # ── LLM call (deterministic fallback on any failure) ─────────────────────
    deployable = max(0.0, min(est_cash, total_cap - held_cost))
    try:
        llm = llm_for({**DEFAULT_CONFIG, **config}, deep=False, temperature=0.1)
        resp = llm.invoke([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        raw = resp.content if hasattr(resp, "content") else str(resp)
        raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw.strip())
        decisions = json.loads(raw)
        if not isinstance(decisions, list):
            raise ValueError("allocator returned non-list JSON")
    except Exception:
        log.exception("[options] allocator LLM failed — using deterministic fallback")
        decisions = _fallback(candidates, remaining, per_cap, deployable)

    # ── Post-parse enforcement ───────────────────────────────────────────────
    open_by_occ = {p["occ_symbol"]: p for p in remaining}
    cand_by_occ = {c["occ_symbol"]: c for c in candidates}
    holds: list[dict[str, Any]] = []
    opens: list[dict[str, Any]] = []
    decided_occ: set[str] = set()

    llm_closes: list[dict[str, Any]] = []
    llm_opens: list[dict[str, Any]] = []
    for d in decisions:
        if not isinstance(d, dict):
            continue
        occ = str(d.get("occ_symbol") or "")
        action = str(d.get("action") or "").upper()
        if occ in open_by_occ and occ not in decided_occ:
            decided_occ.add(occ)
            if action == "CLOSE":
                llm_closes.append(d)
            else:
                holds.append({"position_id": open_by_occ[occ]["id"], "occ_symbol": occ,
                              "rationale": (d.get("rationale") or "")[:300]})
        elif occ in cand_by_occ and action == "NEW" and occ not in decided_occ:
            decided_occ.add(occ)
            llm_opens.append(d)
        # Unknown symbols / hallucinated contracts are dropped silently.

    # Open positions the LLM ignored default to HOLD.
    for occ, p in open_by_occ.items():
        if occ not in decided_occ:
            holds.append({"position_id": p["id"], "occ_symbol": occ,
                          "rationale": "No decision returned — holding."})

    for d in llm_closes:
        p = open_by_occ[d["occ_symbol"]]
        bid, reason = options_fills.sell_quote(p.get("current_bid"), p.get("entry_bid"))
        if bid is None:
            holds.append({"position_id": p["id"], "occ_symbol": p["occ_symbol"], "rationale": "Missing bid: close excluded."})
            exclusions.append({"occ_symbol": p["occ_symbol"], "reason": reason})
            continue
        closes.append({
            "position_id": p["id"], "occ_symbol": p["occ_symbol"],
            "ticker": p.get("underlying"), "exit_reason": "llm_close",
            "exit_premium": _mark(p),
            "rationale": (d.get("rationale") or "")[:300],
        })
        est_cash += _mark(p) * 100 * int(p.get("contracts") or 0)
        held_cost -= float(p.get("cost_basis") or 0)

    # NEW fills: conviction-ranked, clamped to caps / cash / position count.
    deployable = max(0.0, min(est_cash, total_cap - held_cost))
    open_slots = MAX_OPEN_POSITIONS - len(holds)
    ranked_opens = sorted(
        llm_opens,
        key=lambda d: -(cand_by_occ[d["occ_symbol"]].get("conviction") or 0),
    )
    clamped_notes: list[str] = []
    for d in ranked_opens:
        if open_slots <= 0:
            clamped_notes.append(f"{d['occ_symbol']}: dropped (max {MAX_OPEN_POSITIONS} positions)")
            continue
        c = cand_by_occ[d["occ_symbol"]]
        ask, _reason = options_fills.buy_quote(c.get("bid"), c.get("ask"))
        if ask is None:
            continue
        per_contract = ask * 100
        try:
            want = max(1, int(d.get("contracts") or 1))
        except (TypeError, ValueError, OverflowError):
            exclusions.append({"occ_symbol": c.get("occ_symbol"), "reason": "invalid_contract_count"})
            continue
        cap_contracts = int(min(per_cap, deployable) // per_contract)
        contracts = min(want, cap_contracts)
        if contracts < 1:
            clamped_notes.append(f"{d['occ_symbol']}: skipped (1 contract exceeds caps/cash)")
            continue
        if contracts < want:
            clamped_notes.append(f"{d['occ_symbol']}: clamped {want} -> {contracts} contracts")
        cost = contracts * per_contract
        deployable -= cost
        est_cash -= cost
        open_slots -= 1
        opens.append({
            "contract": c, "contracts": contracts, "cost": round(cost, 2),
            "rationale": (d.get("rationale") or c.get("rationale") or "")[:300],
        })

    # ── Markdown report ──────────────────────────────────────────────────────
    lines = [
        f"# Options Paper Portfolio — {trade_date}",
        f"**Equity:** ${equity:,.0f} | **Cash:** ${cash:,.0f} | **Realized P&L:** ${realized_pnl:+,.0f}  ",
        f"**Aggressiveness:** {aggressiveness}/10 ({per_pct:.0%} per position, {total_pct:.0%} total premium) | **Bias:** {bias}  ",
        f"**Decisions:** {len(opens)} new / {len(holds)} hold / {len(closes)} close",
        "",
    ]
    # Friendly labels ('KLAC 174C 2026-08-21') so the report mirrors the
    # dashboard's decisions table instead of raw OCC symbols.
    _disp = {p["occ_symbol"]: _display(p) for p in open_positions}
    if closes:
        lines += ["## Closes", "| Contract | Reason | Exit bid | Rationale |", "|---|---|---|---|"]
        for cdec in closes:
            lines.append(f"| {_disp.get(cdec['occ_symbol'], cdec['occ_symbol'])} | {cdec['exit_reason']} "
                         f"| ${cdec.get('exit_premium') or 0:.2f} | {(cdec.get('rationale') or '')[:80]} |")
        lines.append("")
    if opens:
        lines += ["## New positions", "| Contract | Contracts | Ask | Cost | Conviction | Rationale |", "|---|---|---|---|---|---|"]
        for o in opens:
            c = o["contract"]
            lines.append(f"| {_display(c)} | {o['contracts']} | ${float(c.get('ask') or 0):.2f} "
                         f"| ${o['cost']:,.0f} | {c.get('conviction')}/10 | {(o.get('rationale') or '')[:80]} |")
        lines.append("")
    if holds:
        lines += ["## Holds", ", ".join(_disp.get(h["occ_symbol"], h["occ_symbol"]) for h in holds), ""]
    if clamped_notes:
        lines += ["## Cap clamps", *[f"- {n}" for n in clamped_notes], ""]

    return {"closes": closes, "holds": holds, "opens": opens,
            "report_md": "\n".join(lines), "exclusions": exclusions}
