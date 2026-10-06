# Options basket strategy (iron condors, verticals, butterflies) — rule spec

**Status:** draft v2, revised after an adversarial review · **Author:** Landon + Claude · **Implementation:** not started

## Context

Landon followed an Options Alpha-style method years ago: many small, defined-risk,
mostly premium-selling positions (iron condors, vertical credit spreads, a few
butterflies) spread across many underlyings, with mechanical entries and exits. The
"collective alpha" is the combined P&L of a broad basket, not any single trade. The
barriers were discipline and capital, both of which paper trading and automation remove.

This document turns that method into explicit, testable rules. Everything is in **% of
account equity** so it scales to any account size. Numbers marked *(tunable)* are starting
points from the general method, not validated results. They must be backtested or
paper-traded before anyone trusts them.

**Decisions by Landon**
- Basket max loss **~25%** of equity (moderate band).
- Candidate pool is the desk's **S&P 500 universe**.
- Rules are expressed as percentages.
- The basket runs in **its own paper account**, separate from the existing options accounts.
- **As many positions as the account can support.** There is no fixed position-count cap.
  The count falls out of the risk budget (Section 3).
- **Fills happen only when the market touches the limit**, on both entries and exits. No
  midpoint fills (Section 5).

## What the repo does today (and why this is a different engine)

Verified against the code in review:

- `web/options_allocator.py` buys **long single-leg calls and puts only**, with caps of 5–12%
  of equity per position, 15–50% deployed, `MAX_OPEN_POSITIONS = 15` and `DTE_FLOOR = 3`.
- `options_positions` (`web/db.py`) is **one row per contract** with no group or leg column.
- `web/options_fills.py` buys at the ask and sells at the bid, instantly, with no limit-order
  concept. It cannot express "fill only if the market touches my price".
- `account_equity` (`web/options_engine.py`) counts every open row as an **asset** at its
  sell quote, and opens always debit cash (`web/db.py`). A short leg would inflate equity,
  and there is no way to record a credit.
- The sweeps (`settle_expired`, `refresh_positions`, intraday stops) select every account with
  `kind="options"`, and the stop policies treat a falling price as a loss, which is inverted
  for credit structures.
- Today's research screens the **top 150 movers** (`PRESCREEN_TOP`), the wrong pool for
  condors.
- **There is no IV rank, VIX feed, or entry IV history.** `normalize_schwab_chain` drops
  Schwab's per-contract `volatility`, the yfinance fallback has no greeks, and no chain
  snapshot table exists.

So this is a new engine, not a tweak to the allocator.

## 1. Universe and eligibility

Candidate pool: the S&P 500 list from `get_sp500_tickers()` plus SPY. A cheap pre-filter runs
before any option-chain call, because chains are expensive to fetch (Section 9).

| Filter | Rule (tunable) | Data source |
|---|---|---|
| Price | Underlying ≥ $20 | yfinance |
| Earnings | No earnings date between entry and expiry (hard exclude) | Alpha Vantage `EARNINGS_CALENDAR`, as `web/rules_engine.py` already uses |
| Ex-dividend | If the structure has a short call, no ex-dividend date before expiry (hard exclude) | yfinance dividends/calendar |
| Option liquidity | Short-strike open interest ≥ 500; each leg's bid-ask width ≤ 10% of mid (or ≤ $0.10) | Schwab chain |
| IV filter | **Launch proxy:** 252-day percentile of 20-day realized volatility ≥ 40, or current ATM IV ÷ 20-day realized vol ≥ 1.1 | yfinance prices; Schwab chain |
| Event risk | Exclude pending M&A, binary events, and names that hit a stop in the last 10 trading days | Manual list / own ledger |

**Data prerequisites.**
- **Schwab market data is mandatory.** Delta only exists on the Schwab path. With Schwab
  disabled or the chain on the yfinance fallback, the engine opens **no** new structures.
- True **IV rank** is a phase-2 upgrade. It needs `normalize_schwab_chain` to start persisting
  `volatility` and a daily chain-snapshot table to build history. Until then use the proxy
  above and label results as proxy-based.
- **VIX** comes from `^VIX` via yfinance.
- **Sector** has no table today. It exists only on scanned rows. Use yfinance sector as the
  source and cache it.

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

**Position count is derived, not capped.** The basket opens a new structure whenever a
candidate passes every rule and every limit below still has room. It keeps opening until a
limit binds or candidates run out. The binding limit is normally the total-risk budget.

| Limit | Rule (tunable) |
|---|---|
| Max loss per structure | ≤ **1%** of equity |
| Total basket max loss | ≤ **25%** of equity across everything open, so up to about 25 positions at full size |
| Per-underlying | ≤ 1 open structure per name |
| Per-sector | ≤ 25% of the 25% budget, i.e. **≤ 6.25% of equity** in one sector |
| Net delta | Beta-weighted to SPY, within ±0.15% of equity per 1% SPY move. Needs Schwab deltas |
| New entries per day | ≤ 3 until the book is built, then ≤ 3 per day ongoing, to avoid piling in on one day's volatility |
| Butterflies | ≤ 10% of the risk budget combined |
| Minimum tradable size | A structure is skipped if one contract's max loss exceeds the per-structure limit |

**Max loss formulas.**
- Credit structures: `(wing width − net credit) × 100 × contracts`.
- Butterflies: `net debit × 100 × contracts`.

**Minimum account size is a consequence, not a setting.** Example: a $5-wide spread with a
$1.50 credit risks $350 per contract. At a 1% limit that needs about $35,000 of equity to
open even one contract. On a smaller account the minimum-size rule blocks entries, and the
engine should report "account too small for current candidates" instead of oversizing.

**Why the cap matters.** In a selloff, correlations rise and the basket behaves like one
large short-volatility position. The total cap and the sector limit are the defense, not the
number of tickers.

## 4. Entry rules

| Item | Rule (tunable) |
|---|---|
| Expiration | 30–45 DTE, nearest monthly or weekly |
| Short strikes (condor, vertical) | ~16–20 delta |
| Wing width | $5 for names under $150, $10 above, or 3–5% of the underlying; same width on both sides |
| Minimum credit | Condor: combined credit ≥ **1/3** of wing width. Vertical: credit ≥ **1/5** of wing width |
| Butterfly | Center at the current price or a target level; wings ≈ expected 1-standard-deviation move; debit ≤ 20% of wing width |
| Skip day | If SPY is down more than 2% at the open, or VIX > 35, open nothing |

**Expected edge, stated honestly.** 16–20 delta means about an 80–84% chance a *single*
short strike finishes OTM at expiry. A condor has two, and the exit rules fire on a *touch*,
not on expiry. The managed win rate is therefore closer to **65–75%**. With the average win
about half the credit (rule 1) and the average loss about 1–1.5 credits (rules 3–4), the
breakeven win rate is roughly **67–75%**. The edge before costs is close to zero and is
likely negative after four-leg slippage. This strategy is **unproven here**. The
evaluation (Section 8) exists to find out.

## 5. Fill model (limit-touch, entries and exits)

No midpoint fills and no instant fills at the natural price. An order fills **only when the
quoted market reaches the limit price**, and it fills at that limit price.

**Orders**
- **Entry (credit structure):** limit credit = the structure's net mid at order time minus a
  conservative haircut *(tunable, 25% of the net bid-ask width)*. It fills if the net
  *natural* credit (sum of short-leg bids minus long-leg asks) is at or above the limit
  credit. Otherwise it stays unfilled.
- **Entry (butterfly debit):** limit debit = net mid plus the same haircut. It fills if the
  net natural debit (long asks minus short bids) is at or below the limit.
- **Exit at profit target:** a resting order to buy to close at the target debit (50% of the
  credit). It fills if the net natural close cost is at or below the target.
- **Exit at time stop / breach:** a limit to close at the net mid plus the haircut. It fills
  when the market touches it.
- **Loss stop:** triggers on the structure's mark (2× the credit). Once triggered, the close
  executes at the **natural price** (buy shorts at the ask, sell longs at the bid). A stop
  that waits for a limit touch could be skipped in a fast market. See open question 1.

**Unfilled orders.** An order that has not filled by the end of the trading day is cancelled
and re-evaluated on the next run from fresh quotes. Entries that fail to fill for 3
consecutive days are dropped. Exit orders are never dropped, only re-priced.

**Paper limitation.** The engine observes quotes once per scheduled run, not continuously, so
a price that touched the limit between runs is not seen. Fill rates will understate what a
live resting order would catch, and results must be labelled accordingly. Record both the
modeled net mid and the actual fill price per structure so the Section 8 slippage report is
real.

## 6. Exit and management rules

Evaluated every run, first match wins:

1. **Profit target:** close at ≥ 50% of max profit (credit structures).
2. **Time stop:** close at 21 DTE if still open. Rolling is out of scope for version 1.
3. **Loss stop:** close if the structure's mark reaches 2× the credit received (loss ≈ 1× credit).
4. **Breach:** close if the underlying trades through a short strike by more than 1%.
5. **Pre-expiry safety:** always close by 3 DTE. This matches the existing `DTE_FLOOR`.
6. **Butterflies:** close at 25–40% of max profit or at 7 DTE.

**Expiry and assignment.** The paper model assumes **European-style exercise with no early
assignment**, and results are labelled as such. The ex-dividend exclusion in Section 1
removes the largest early-assignment risk for short calls. Any leg still open at expiry
settles at **intrinsic value against the underlying's close**, reusing `intrinsic_value` and
`underlying_close_on_or_before` from `web/options_engine.py`. A short leg must never be left
open on the books because its bid is stale.

## 7. Portfolio-level circuit breakers

- **Drawdown pause:** if basket equity falls 8% from its peak, open no new positions. Resume
  when equity recovers to −4% from peak **or after 10 trading days**, whichever comes first.
  After a time-based resume, run at half the normal entry budget until equity is back above
  −4%. This avoids a permanent freeze once positions are closed and nothing can move equity.
- **Stress check at entry:** reject any new trade that would push the modeled loss under a
  **−10% one-day SPY gap** above **12% of equity**. The model uses current betas and
  put-side max loss only. This limit is lower than the 25% cap on purpose, so it can bind.
- **Data-quality halt:** if marks are stale for more than N consecutive runs (the repo
  already tracks `stale_count`), freeze new entries.

## 8. Evaluation (what "working" means)

Compare to a buy-and-hold SPY account over the same dates, and report all of:

- Total return and **max drawdown** (a short-premium basket usually wins on smoothness and
  loses on rallies, so return alone misleads).
- **Win rate against the breakeven win rate** from the actual average win and loss, not
  against the delta-implied number.
- Sharpe and Sortino, beta to SPY, and worst single day.
- P&L split by structure type, sector and entry IV proxy.
- Modeled net mid versus actual fill (slippage), and the share of orders that never filled.

**Minimum credible sample:** at least 100 closed trades across at least two volatility
regimes. At up to 3 entries a day with 30–45 DTE positions, expect roughly **4–7 months**
before any conclusion. Paper results understate slippage and cannot show assignment or
margin behavior.

## 9. Data and compute budget

- Today the engine pulls chains only for deep-dived movers, one side, `strike_count=20`, with
  a 7–60 DTE window and a 60-second timeout per call (`web/options_data.py`). A condor screen
  needs **both sides** and enough strikes to reach a 16-delta short plus a wing on
  high-priced names.
- Run the cheap pre-filter first (price, earnings, ex-dividend, realized-vol proxy, cached
  open interest). Then pull chains for only the **top ~60 names by the vol proxy** each day.
- Parameterize the chain fetch helper for 30–45 DTE, `contract_type="ALL"`, and a wider
  strike count.

## 10. Implementation notes (not part of the rules)

- **Separate account kind**, e.g. `options_basket`, with new tables `options_strategies` and
  `options_legs`. Do not reuse `options_positions` or `kind="options"`. Every consumer that
  selects `kind="options"` must be checked and must exclude the new kind: `settle_expired`,
  `refresh_positions`, the intraday stop sweep, `account_policy` stop types, the nightly
  learning sweeps, and the UI tabs.
- **Ledger kinds** for credit received, collateral hold, collateral release, and
  buy-to-close debit. Collateral per structure equals its max loss.
- **Equity** = cash + held collateral − mark-to-close of all open structures.
- **Fills:** a new limit-touch fill module. It does not extend `options_fills.py`'s instant
  natural fills.
- **Data:** persist Schwab `volatility` in `normalize_schwab_chain` and add a daily chain
  snapshot table so true IV rank can replace the proxy.
- **Do not reuse the movers pre-screen** as the candidate source. Use the filters in
  Section 1.
- **Keep the LLM out of the hard rules.** It can propose structure or direction only. Sizing,
  limits, exits and circuit breakers stay in code, as in the current allocator.
- **Tiers (per CLAUDE.md):** this is tier-4 work on `master`. New modules go in
  `TIER_ONLY_FILES[4]` in `scripts/make_tier.py` **and** in the forbidden-import lists for
  tiers 1–3. Any import from `portfolio_main.py` or `scheduler.py` must be guarded so the
  stripped tiers still import cleanly. The existing `rules_engine` import in
  `portfolio_main.py` `_startup()` is the unguarded pattern behind the current tier-3 CI
  failure on `master`. Do not copy it.

## Open questions

1. Loss stop fill: execute at the natural price once triggered (current default), or wait for
   a limit touch and risk skipping a fast move?
2. Entry haircut: is 25% of the net bid-ask width the right starting point for the limit, or
   should the limit be the net mid exactly?
3. Does a time-based drawdown resume after 10 days feel right, or should the pause require a
   manual restart?
4. Should rolling a tested side be added in version 2?
5. Reg T style max-loss collateral only, or also model portfolio margin?
