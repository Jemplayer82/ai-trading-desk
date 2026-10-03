# Lane DF / card 65 validation

Worktree: `/home/landon/.codex/worktrees/desk-bidask-fills-20261003`
Branch: `feature/bid-ask-fills`, base `1c129b6`.

Commands (existing local desk environment; no pushes/deployments):

```
/home/landon/Projects/ai-trading-desk/.venv/bin/python -m pytest -q
/home/landon/Projects/quant/.venv/bin/ruff check web tests scripts/rescore_options_bidask.py scripts/smoke_options_bidask.py
node --check web/static/options.js
git diff --check
/home/landon/Projects/ai-trading-desk/.venv/bin/python scripts/smoke_options_bidask.py --json-export /home/landon/quant-status/desk-export/options_positions-20261002.json --output-json artifacts/bidask/smoke.json
/home/landon/Projects/ai-trading-desk/.venv/bin/python scripts/rescore_options_bidask.py --json-export /home/landon/quant-status/desk-export/options_positions-20261002.json --output-json artifacts/bidask/rescore.json --output-markdown artifacts/bidask/rescore.md
```

`full-tests.txt` contains full final suite output including warnings and the single skipped live-API test. `lint.txt` contains Ruff output. `smoke.json` contains only counts, exceptions, source hash and limitations; outcome values are suppressed. `rescore.md` and `rescore.json` contain the explicitly authorized read-only historical fill sensitivity estimate, its assumptions and excluded rows. Source JSON was not copied into the repository.

Coverage added: finite/nonnegative quote validation, valid zero sale, invalid/crossed entry exclusion, ask debit and bid liquidation equity, zero-value equity without cost-basis fallback, staged/fixed/trailing bid references, observed-bid stop fills, missing bid stale carry, migration idempotence and immutable closed numbers, quote audit columns, zero-entry-bid trailing stops, multi-row expiry liquidation, UI policy/cutover/quote strings, and re-score exclusions/fees/estimation.

Synthetic lifecycle tests retain the old stop-limit eligibility and saved-ratchet invariants, with requested fill/expiry assertions updated to the bid policy. No mutation tests were run. No live desk or DB was accessed. Full smoke's SQLite database is export-seeded, not an actual supplied production DB backup; missing historical exit quotes remain a limitation.
