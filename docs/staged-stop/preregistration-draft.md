# Prospective staged-stop A/B draft — unsigned, no account created

Card #57 / lane DK. Opt-in options policy `trailing_staged`, base 20%, trigger
20% peak gain over entry, tighter trail 10%. Existing accounts are unchanged.
This draft requires claude-quant review before a forward experiment is signed.

## ASSUMPTIONS

- Omitted or NULL staged parameters on an opt-in save normalize to 20/10;
  blank strings are invalid. Other stop types discard both stage parameters.
- Trigger is a finite positive percent with no upper cap. Tight trail is finite,
  at least 5%, no greater than base. Base is positive and less than 100%.
- Staged policy validation needs kind=options; runtime parsing rejects equity
  or missing-kind staged records. Existing policy parsing stays unchanged.
- Exact trigger comparisons tolerate only binary float noise (relative/absolute
  epsilon 1e-12). Stop/fill rounding and crossing stay exactly as existing trails.
- Replay fixtures select three desk stop exits in export order, including at
  least one switched exit. Only quotes up through that exit are copied.
- Forward comparison is descriptive. Enrollment duration/sample size and any
  confirmatory decision thresholds must be signed before account creation;
  no implementation choice supplies evidence of efficacy.

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
