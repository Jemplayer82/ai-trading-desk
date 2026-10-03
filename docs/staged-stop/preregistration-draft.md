# Prospective staged-stop A/B draft — unsigned, no account created

Card #57 / lane DK. Opt-in options policy `trailing_staged`, base 20%, trigger
20% peak gain over entry, stage trail 10%. Existing accounts are unchanged.
This draft requires claude-quant review before a forward experiment is signed.

## ASSUMPTIONS

- Omitted or NULL staged parameters on an opt-in save normalize to 20/10;
  blank strings are invalid. Other stop types discard both stage parameters.
- Trigger is a finite positive percent with no upper cap. Stage trail is finite,
  between 1% and 99% inclusive (fractional values allowed), independent of base.
  Base is positive and less than 100%.
- Staged policy validation needs kind=options; runtime parsing rejects equity
  or missing-kind staged records. Existing policy parsing stays unchanged.
- Exact trigger comparisons tolerate only binary float noise (relative/absolute
  epsilon 1e-12). Stop/fill rounding and crossing stay exactly as existing trails.
- Replay fixtures select three desk stop exits in export order, including at
  least one switched exit. Only quotes up through the latest of the
  5/10/40 exits are copied.
- Forward comparison is descriptive. Enrollment duration/sample size and any
  confirmatory decision thresholds must be signed before account creation;
  no implementation choice supplies evidence of efficacy.
- A NULL saved stop level means no previous evaluation; initialize from the
  current peak and policy, without inventing historical pre-trigger observations.
  Save four-decimal levels like existing stops. Preserve the ratchet across edits
  and temporary switches away from staged; other stop types ignore it.
- Mark updates persist the ratchet even for carried marks, without executing a
  stop there. Hourly fills still require fresh quotes; DTE floor retains priority.
  Daily and hourly staged evaluations serialize their saved-level reads/writes.
- Mid-trade edits are supported mechanically; experiment enrollment still freezes
  parameters. Any discretionary edit must be logged and ends the fixed-policy
  comparison interval. Trigger edits re-evaluate stage membership from peak.

## Round 3 UI assumptions

The live explanation follows the existing policy bounds: base >0 and <100,
trigger >0 without an upper cap, and stage trail 1–99 inclusive. Fractional
values are displayed as numbers. Blank or invalid values show the placeholder;
input events never restore a cleared value. Defaults are populated on selecting
the staged policy or loading the form, as before. The shared dashboard sentence
also shows the placeholder for malformed persisted staged parameters.

## Round 2 amendment

Any after-trigger trail from 1 to 99 is allowed. Candidate levels use base before
the trigger and stage trail after it. The saved stop is the maximum of the
previous saved level and candidate, so changing any parameter never lowers it.
Defaults remain 20/20/10. Tightening can cause an immediate stop at the next
evaluation if its raised level is at or above the mark.

## Build attribution assumption

The handoff references a session attribution line but supplies no literal, and
none was returned to the worker during this build. The commit uses the worker's
identity: `Co-Authored-By: Codex <noreply@openai.com>`.

## Proposed enrollment and measurements

After deployment approval, create `Bull staged 10%` with bull's starting capital,
bias, aggressiveness and schedule. Keep bull unchanged. Record both policy
snapshots, account IDs, enrollment time and code version before the first scan.
Use the same quote feed and scanning cadence. Compare equity, realized and
unrealized P&L, drawdown, count/fees, entries, stop exits and unmatched trades over
one fixed interval. Separate entry-selection differences from exit differences;
independent accounts need not choose identical trades. Do not tune parameters
from interim results. Historical replay is daily-close/next-session bid;
the desk acts on its scan-time marks with its existing simulated fill rules.
