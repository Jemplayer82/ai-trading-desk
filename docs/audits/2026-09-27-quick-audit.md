# Quick audit — a8a82eb (v2.1.0) .. f7bb3d7

Level: quick (gates + in-context 8-angle code review + scanner triage). Reviewer: Claude Fable 5.1, 2026-09-27.
Scope: shared daily research, parallel allocations, always-latest model menus, Codex (ChatGPT) handler, Cleo trim.

## Gates (`scripts/audit_gates.sh a8a82eb`)

| Gate | Result |
|---|---|
| pytest (1,659 tests) | OK |
| ruff | OK |
| make_tier lint + tier 3/2/1 strips | OK |
| committed-secret grep | OK |
| bandit (24 changed .py files) | 23 medium: 18× B608 f-string SQL, 3× B310 urllib, 2× B108 /tmp — all read; only B108 kept (low) |
| semgrep p/python + p/security-audit | 5 hits: subprocess/urllib with constant argv or fixed hosts, one logger line that logs a status body without secrets — no action |
| pip-audit (exported lock) | **151 advisories across 21 packages** — see finding 1 |

## Findings (7)

1. **uv.lock pins 151 published advisories** (starlette 14, python-multipart 12, urllib3 12, aiohttp 66, cryptography 6). Pre-existing, but the dashboard is internet-facing. Bump the lock.
2. **Codex handler drops the API error message** for `{"type":"error","error":{"message":…}}` events (desk sees "codex error"); a string-typed `turn.failed.error` would raise.
3. **Deployed Cleo has diverged from the repo copy** (host has parking + auth-expiry alerting; README's "git pull && restart" would regress it).
4. **Family-model lookup has no negative cache**: if `codex debug models` fails, every request re-runs the 30 s subprocess before falling back.
5. **Cancelled research runs count toward the 2-attempt retry cap.**
6. **`SCHEDULE_RESEARCH_TIME` outside 00:00–05:29 saves fine but is silently replaced by 00:00** at fire time; the UI reports success.
7. **Cleo/Codex single-instance lock at a predictable `/tmp` path** (bandit B108; low on a single-user host).

Not found: the three accounting/restart bugs fixed at close-out (b1b4563) stay fixed; per-account locks, shared contract vetting and the auth gate on the new `/api/research*` routes check out; model names from requests reach `codex -m` / `claude --model` as argv items, not shell, and clap rejects hyphen-leading values, so no flag injection.

## Not done at this level
Executed security probes (unauthenticated sweep of every route, injection payloads, hostile prompts against the handlers with a fake CLI), concurrency and time-calendar lenses, test-quality review, plan-conformance against the spec — these are the `deep` level of `dev-pipeline/audit.workflow.js`.

## Resolution

All seven findings fixed in commit 3626b29 (same day) and deployed: lock upgraded
(pip-audit now reports no known advisories), Codex error text and negative cache,
Cleo repo/host merge with the lock file moved out of /tmp, cancelled runs excluded
from the retry cap, and schedule settings validated at save time. Post-deploy
smoke on the new image: yfinance 1.7 pre-screen/live quotes/price bars OK, Claude
quick+deep via Cleo OK, ChatGPT via Codex OK, settings PUT rejects 22:00/05:30/abc.
