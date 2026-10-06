# Options basket strategy (iron condors, verticals, butterflies) — rule spec

**Status:** draft for review · **Author:** Landon + Claude · **Implementation:** not started

## Context

Landon followed an Options Alpha-style method years ago: many small, defined-risk,
mostly premium-selling positions (iron condors, vertical credit spreads, a few
butterflies) spread across many underlyings, with mechanical entries and exits. The
"collective alpha" is the combined P&L of a broad basket, not any single trade. The
barriers were discipline and capital, both of which paper trading and automation remove.

This document turns that method into explicit, testable rules. Everything is in **% of
account equity** so it scales to any account size. Numbers marked *(tunable)* are starting
points from the general method, not validated results. They should be backtested or
paper-traded before anyone trusts them.

Parameters chosen by Landon: basket max loss **~25%** of equity (moderate, 20–30% band);
candidate pool is the desk's **S&P 500 universe**; rules expressed as percentages.

## What the repo does today (and why this is a different engine)

- `web/options_engine.py` / `web/options_allocator.py` trade **long single-leg calls and
  puts**, chosen directionally by an LLM from shared daily research, with caps of 5–12% of
  equity per position and 15–50% deployed, `MAX_OPEN_POSITIONS = 15`, and a forced close at
  `DTE_FLOOR = 3`.
- `options_positions` (`web/db.py`) is **one row per contract**. There is no spread/leg
  grouping, no short-leg margin or max-loss accounting, and no assignment handling.
- Fills (`web/options_fills.py`) model buying at the ask and selling at the bid for a single
  contract.
- The daily research feeds on the **top 150 movers** (momentum/volume). That screen finds
  trending names, which is the opposite of what an iron condor wants.

So this is not a tweak to the allocator. It needs a multi-leg position model (see
"Implementation notes"). The rules below are engine-agnostic.

## 1. Universe and eligibility

Candidate pool: the S&P 500 list from `get_sp500_tickers()` plus SPY, filtered each day to
names that pass **all** of:

| Filter | Rule (tunable) |
|---|---|
| Option liquidity | Short-strike open interest ≥ 500; leg bid-ask width ≤ 10% of mid (or ≤ $0.10) |
| Price | Underlying ≥ $20 |
| Earnings | No earnings date between entry and expiry (hard exclude) |
| Implied volatility | IV rank (or percentile) ≥ 40 over the trailing year; fallback proxy if the data source lacks it |
| Event risk | Exclude pending M&A, FDA binary events, or names already hit by a stop in the last 10 trading days |

Position count and diversification limits (Section 3) are enforced after eligibility.

## 2. Structure selection

Each eligible name gets one structure, chosen by regime and, optionally, the desk's
directional signal from the shared research:

| View | Structure | Notes |
|---|---|---|
| Neutral / range-bound | **Iron condor** | Default and core of the basket |
| Mild bullish | **Bull put spread** | Short put credit spread |
| Mild bearish | **Bear call spread** | Short call credit spread |
| Expect pinning near a level | **Long butterfly** | Debit; satellite only |

Mix guidance (share of total risk budget): condors ≥ 50%, verticals ≤ 40%, butterflies ≤ 10%.

## 3. Sizing and portfolio limits

| Limit | Rule (tunable) |
|---|---|
| Max loss per position | ≤ 1.5% of equity (wing width × 100 × contracts − credit) |
| Total basket max loss | ≤ 25% of equity across all open positions |
| Max open positions | 15 (matches the existing cap) |
| Per-underlying | ≤ 1 open structure per name |
| Per-sector | ≤ 25% of the basket risk budget in one GICS sector |
| Net delta | Beta-weighted to SPY, within ±0.15% of equity per 1% SPY move; rebalance entries toward neutral |
| New entries per day | ≤ 3 (avoid piling in on one day's volatility spike) |
| Butterflies | ≤ 10% of the risk budget combined |

**Why the 25% cap matters:** in a selloff, correlations rise and the basket behaves like one
large short-volatility position. The cap and the per-sector limit are the defense, not the
number of tickers.

## 4. Entry rules

| Item | Rule (tunable) |
|---|---|
| Expiration | 30–45 DTE, nearest monthly or weekly |
| Short strikes (condor, vertical) | ~16–20 delta (≈ 80–84% probability of expiring OTM) |
| Wing width | $5 for names under $150, $10 above, or 3–5% of the underlying; same width on both sides |
| Minimum credit | ≥ 1/3 of wing width (condor: combined credit) |
| Butterfly | Center at the current price or a target level; wings ≈ expected 1-standard-deviation move; debit ≤ 20% of wing width |
| Fill | Limit at mid minus a conservative haircut (e.g. 25% of the bid-ask width), reusing the repo's bid/ask fill model for each leg |
| Skip day | If SPY is down more than 2% at the open, or VIX is above 35, open nothing |

## 5. Exit and management rules

Evaluated every run, first match wins:

1. **Profit target:** close at ≥ 50% of max profit (credit structures).
2. **Time stop:** close or roll at 21 DTE if still open.
3. **Loss stop:** close if the structure's mark reaches 2× the credit received (loss ≈ 1× credit).
4. **Breach:** close if the underlying trades through a short strike by more than 1% (condor/vertical).
5. **Pre-expiry safety:** always close by 3 DTE; never hold into expiry (assignment risk). This
   matches the existing `DTE_FLOOR`.
6. **Butterflies:** close at 25–40% of max profit or at 7 DTE.

Rolling is **out of scope** for version 1. Positions are closed, and the name is eligible
again only after the cooldown.

## 6. Portfolio-level circuit breakers

- **Drawdown pause:** if basket equity falls 8% from its peak, open no new positions until it
  recovers to −4%.
- **Stress check at entry:** reject any new trade that would push modeled loss under a −10%
  one-day SPY gap above the 25% total cap. The model uses current betas and fixed
  worst-case structure losses, not option Greeks.
- **Data-quality halt:** if marks are stale for more than N consecutive runs (the repo already
  tracks `stale_count`), freeze new entries.

## 7. Evaluation (what "working" means)

Compare to a buy-and-hold SPY account over the same dates, and report all of:

- Total return and **max drawdown** (a short-premium basket usually wins on smoothness and
  loses on rallies, so return alone misleads)
- Win rate **and** average win ÷ average loss (a high win rate with a 3× loss ratio can still lose)
- Sharpe and Sortino, beta to SPY, and worst single day
- P&L split by structure type, sector and entry IV rank
- Realized slippage versus the modeled fill

Minimum credible sample: at least 100 closed trades across at least two distinct volatility
regimes before drawing conclusions. Paper results understate slippage and cannot show
assignment or margin behavior.

## 8. Implementation notes (not part of the rules)

- Add a **multi-leg position model**, for example a `strategy_id` grouping rows in
  `options_positions` (or a new `options_strategies` table) so a structure opens, marks and
  closes atomically, with defined max loss and credit stored on the group.
- Cash accounting for credits: collateral = max loss per structure, held until close.
- The entry-time screen needs **IV rank history**. Check whether `options_data.py` or the
  data provider offers it; otherwise compute a proxy from stored chain snapshots.
- Do not reuse the movers pre-screen as the candidate source for condors. Use a
  liquidity-and-IV filter over the full universe, and optionally the research signal only to
  pick direction for verticals.
- Keep the LLM out of the hard rules: it can propose structure or direction, but sizing,
  limits, exits and circuit breakers stay in code, as in the current allocator.
- Gate behind a new account type so the existing long-option accounts are untouched.
- Per CLAUDE.md, this is tier-4 work on `master`. If it adds files to tier 4 only, update
  `scripts/make_tier.py`'s manifest and regenerate the tier branches as needed.

## Open questions

1. Is IV rank available from the current data providers, or must it be derived?
2. Should rolling a tested side be added in version 2?
3. Should the basket run as its own paper account, or alongside the existing options accounts?
4. Defined-risk collateral rules: model Reg T style (max loss) only, or also portfolio margin?
