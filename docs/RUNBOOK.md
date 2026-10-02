# Staged options stop: deployment and forward paper comparison

Card #57. This build adds an opt-in stop; no deployment or account creation has
been performed. Landon approves deployment. `docs/staged-stop/preregistration-draft.md`
records the unsigned prospective assumptions; claude-quant reviews/signs the
experiment before enrollment. Historical grid results from 47 priced bull trades
are thin evidence and do not establish future profitability.

## Deploy after approval

1. Review the lane DK commit and test receipt. Merge the approved feature branch
   into tier-4 `master` through the normal review process. Do not edit generated
   tier branches. Have CI build the image for that approved commit.
2. Before redeploy, take and verify a consistent SQLite backup with the existing
   backup procedure (SQLite backup API or stopped writers). Do not copy only the
   main DB file while WAL writers are active. Record the image digest and backup.
3. On the approved deployment host, force-pull the freshly built image and
   redeploy the existing Portainer stack. Keep the persistent volume and existing
   secrets. API, portfolio and scheduler must all use the same approved image;
   the web frontend must include the matching options form assets. For the
   compose deployment path in README: `docker compose pull && docker compose up -d`.
4. Boot-time `init_db()` adds only `stage_trigger_pct REAL` and
   `stage_trail_pct REAL` to existing paper_accounts, plus `stop_level_hwm REAL`
   to options_positions, all with NULL defaults. There is no staged backfill, table rewrite or account modification. Verify the three
   columns and existing account policy snapshots before enabling the experiment.
5. Check the Options form offers staged trail, editing an existing account
   renders its unchanged policy, and the new account summary shows 20/20/10.
   Use an isolated staging account first to check save/round-trip behavior.

Rollback: return all services to the prior image. Added NULL columns can remain;
old code ignores them. If any staged account has been created, disable that
account's scan schedule before rolling back: old code cannot honor the new type.
Do not assume a code rollback preserves staged protection, and do not drop columns
or rewrite the production DB as part of rollback.

## Create `Bull staged 10%` after experiment approval

- Record bull's existing account ID, bias, aggressiveness, starting capital,
  schedule, stop settings, enrollment timestamp and deployed image/commit.
- In Options → account form, create a distinct account named `Bull staged 10%`.
  Copy bull's bias, aggressiveness and starting capital exactly. Use the same
  schedule/cadence (or leave both manual during setup and enroll them together).
- Select “Staged trailing % (change after gain trigger)”; set base trail **20**, gain
  trigger **20**, trail after trigger **10**. Save and verify the account summary.
- Do not edit bull/bear/small, copy old positions or backdate enrollment. Record
  the new ID and full API policy snapshot before its first scan.

For this policy: “Sells if the price falls 20% from its highest point; once the
position is up 20%, the trail becomes 10% below the highest point. The saved stop
level never falls.” Calls and puts use option premium, not underlying direction.
The saved highest stop level persists through pullbacks and account edits. NULL
on old rows initializes at the current peak and settings on the next evaluation;
historical levels are not reconstructed. Stale marks and the DTE floor retain
their existing behavior; exit reporting uses `trail_stop`.

## Edit a staged account with open positions

In Options, edit the account and save the base trail, gain trigger and trail
after trigger. The latter accepts any value from **1 to 99**, tighter or looser
than the base. Changes apply to every open position at its next evaluation.
A tighter setting raises the stop immediately at that check and can close a
position; a looser setting never lowers a stop already reached. A 40% stage
trail after a 20% base therefore preserves the prior stop until a higher peak
produces a higher candidate. The saved ratchet also survives switching the account
away from staged and back; other stop types ignore it. Gain-trigger edits use
the stored peak to determine the current stage. Daily and hourly checks share
the evaluator; mark updates save levels but do not execute stops.

For the prospective A/B, keep parameters fixed. Log discretionary edits and
end the fixed-policy interval before editing; agree a new interval first.

## Read the forward comparison

Freeze the interval, sample target and any decision criteria with claude-quant
before creating the account. View bull and the staged account over that same
interval in Options: starting capital, cash plus open-position value, realized
and unrealized P&L, drawdown, trade/stop counts and fees. Check scan logs for
missing/stale marks and scheduling differences. Pair matching contract/entry
trades when available; disclose unmatched trades separately. Independent agent
accounts can choose different entries and sizes, so an aggregate P&L difference
alone cannot attribute improvement to the stop. Keep parameters fixed throughout.

Replay caveat: fixtures use daily-close bid/ask midpoints and the replay books a
stop exit at the next valid session's bid. The shared desk evaluator signals at
the trigger observation itself. Intraday desk checks use scan-time marks, fill at
the level when the previous mark crossed it, or at the observed mark otherwise;
the allocator's daily backstop has no previous mark. The fixtures prove level and
trigger parity plus the replay's next-session exit convention; they do not prove
identical desk fill prices, intraday exits or P&L.
