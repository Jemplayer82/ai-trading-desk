#!/usr/bin/env bash
# Mechanical audit gates — the checks both audit levels run before any reviewer
# reads a line of code. Exit status is non-zero if any gate fails; every gate's
# own output is kept under $OUT so a reviewer can cite it.
#
#   scripts/audit_gates.sh [BASE_REF] [OUT_DIR]
#
# BASE_REF defaults to the last release tag (git describe); the diff BASE..HEAD is
# what bandit/semgrep focus on, while pytest/ruff/tier strips cover the whole tree.
set -u
cd "$(dirname "$0")/.."
BASE="${1:-$(git describe --tags --abbrev=0 2>/dev/null || echo HEAD~20)}"
OUT="${2:-.pipeline/audit/$(git rev-parse --short HEAD)}"
mkdir -p "$OUT"
status=0
run() {  # run <name> <cmd...>  — capture output, record pass/fail
  local name="$1"; shift
  printf '== %-14s ' "$name"
  if "$@" > "$OUT/$name.log" 2>&1; then echo "OK"; else echo "FAIL (see $OUT/$name.log)"; status=1; fi
}

changed_py=$(git diff --name-only "$BASE"..HEAD -- '*.py' | grep -v '^tests/' | tr '\n' ' ')
echo "base=$BASE head=$(git rev-parse --short HEAD) changed .py files=$(echo $changed_py | wc -w) out=$OUT"

run pytest        uv run --extra web python -m pytest -q
run ruff          uvx ruff check .
run tier-lint     uv run --extra web python scripts/make_tier.py --lint
for t in 3 2 1; do
  # nginx -t needs the docker socket; everything else in --check must pass.
  run "tier-$t"   bash -c "uv run --extra web python scripts/make_tier.py --tier $t --check 2>&1 | tee /dev/stderr | grep -E '^\s+FAIL' | grep -v 'nginx -t' && exit 1 || exit 0"
done
# Static security scanners over the changed application code (tests excluded).
if [ -n "$changed_py" ]; then
  run bandit      uvx bandit -q -ll -ii $changed_py
  run semgrep     uvx semgrep --config p/python --config p/security-audit --error --quiet $changed_py
else
  echo "== bandit/semgrep skipped: no changed .py files"
fi
# Known-vulnerable dependencies in the lock file.
run pip-audit     bash -c "uv export --extra web --format requirements-txt --no-hashes -q > '$OUT/requirements.txt' && uvx pip-audit -r '$OUT/requirements.txt' --progress-spinner off"
# Secrets accidentally committed in the range.
run secrets       bash -c "! git diff '$BASE'..HEAD | grep -E '^\+' | grep -E -i '(sk-[a-z0-9]{20,}|ptr_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY)'"

echo "gates: $([ $status = 0 ] && echo ALL PASSED || echo FAILURES) — logs in $OUT"
exit $status
