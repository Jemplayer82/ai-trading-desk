"""Generate a tier deployment from master — the strip script tier branches
are built from.

ai-trading-desk ships at 4 cumulative feature tiers (see web/features.py):
  1 = single-ticker analysis only            -> branch tier-1-base
  2 = + Schwab brokerage                     -> branch tier-2-brokerage
  3 = + S&P 500 scanner                      -> branch tier-3-scanner
  4 = + options paper trading (= master, the full product — this script is a
      no-op at tier 4 except for the two harmless substitutions below)

Three mechanisms carry the split:
  1. Runtime feature gates (web/features.py DEFAULT_TIER) — this script
     rewrites that one constant; web/main.py, web/scheduler.py and
     web/portfolio_main.py's shell read it at import time and mount/register
     the right routes and cron jobs. No file changes needed for that part.
  2. Whole-file deletion + TIER:N marker-comment stripping, for content that
     can't self-gate: static HTML, nginx routing, compose services, one
     settings-registry ordering, and README feature sections.
  3. The README's TIER-IDENTITY block, rewritten per tier so each generated
     branch's landing page says which tier it is and that it's generated.

Tier branches (tier-1-base, tier-2-brokerage, tier-3-scanner) are GENERATED
ARTIFACTS, regenerated from master and force-pushed by
.github/workflows/tiers.yml. Never develop on one directly — a regen
overwrites it silently. All development happens on master; this script (and
the manifest below) is the single source of truth for what each tier
contains.

Usage:
    python scripts/make_tier.py --lint                  # markers balanced?
    python scripts/make_tier.py --tier 1 --check         # dry-run + verify
    python scripts/make_tier.py --tier 1 --apply         # mutate the tree
    python scripts/make_tier.py --tier 1 --apply --force # skip dirty-tree guard

--apply mutates the CURRENT working tree in place — it is meant to be run
against a disposable checkout (a fresh `git worktree`, or right before
force-pushing a tier branch), never against your master working copy.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Manifest — the single source of truth for what each tier contains.
# ---------------------------------------------------------------------------

# Files/directories that exist ONLY at the given tier and above. To build
# tier T, delete the union of every TIER_ONLY_FILES[k] where k > T.
TIER_ONLY_FILES: dict[int, list[str]] = {
    4: [
        "web/options_data.py",
        "web/options_allocator.py",
        "web/options_engine.py",
        "web/options_learning.py",
        "web/options_recommend.py",
        "web/options_routes.py",
        "web/static/options.js",
        "tests/test_options_allocator.py",
        "tests/test_options_data.py",
        "tests/test_options_learning.py",
        "tests/test_options_lifecycle.py",
        "tests/test_options_recommend.py",
        "tests/test_options_scan_health.py",
        "tests/test_options_market_wait.py",
        "tests/test_opt_account_form_js.py",
    ],
    3: [
        "web/spy_tickers.py",
        "web/spy_scanner.py",
        "web/spy_allocator.py",
        "web/spy_routes.py",
        "web/research_engine.py",
        "web/static/spy.js",
        "tests/test_spy_scanner_store.py",
        "tests/test_spy_scan_status_endpoint.py",
        "tests/test_quick_scan_batch.py",
        "tests/test_scan_health_guard.py",
        "tests/test_deep_dive_reuse.py",
        "tests/test_deep_dive_gating.py",
        "tests/test_macro_brief_scan.py",
        "tests/test_spy_allocator.py",
        "tests/test_paper_account_routes.py",
        "tests/test_equity_stops.py",
        "tests/test_rebalance_carryover.py",
        "tests/test_spy_account_form_js.py",
        "tests/test_research_engine.py",
    ],
    2: [
        "web/schwab_routes.py",
        "web/auth",  # directory: schwab.py, token_store.py, __init__.py
        "web/brokerages.py",
        "web/portfolio",  # directory: aggregator.py, __init__.py
        "web/newsletter.py",
        "web/templates",  # directory: newsletter.html
        "web/portfolio_main.py",
        "web/portfolio_routes.py",
        "web/scan_queue.py",
        "web/static/portfolio.js",
        "web/static/schwab-alert.js",
        "tests/test_brokerages.py",
        "tests/test_portfolio_progress.py",
        "tests/test_portfolio_aggregator.py",
        "tests/test_scan_activity_js.py",
        "tests/test_portfolio_schedule_js.py",
        "tests/test_clear_history_portfolio.py",
        "tests/test_scan_queue.py",
    ],
}

# Modules whose absence at a given tier must never be silently reachable from
# surviving code — the leak canary greps the post-strip tree for these.
LEAK_CANARY_PATTERNS: dict[int, list[str]] = {
    1: ["schwab_routes", "brokerages", "portfolio_main", "portfolio_routes",
        "scan_queue", "spy_scanner", "spy_routes", "spy_allocator",
        "spy_tickers", "options_engine", "options_routes", "options_data",
        "options_allocator", "options_recommend", "options_learning",
        "research_engine", "research_routes"],
    2: ["spy_scanner", "spy_routes", "spy_allocator", "spy_tickers",
        "options_engine", "options_routes", "options_data",
        "options_allocator", "options_recommend", "options_learning",
        "research_engine", "research_routes"],
    3: ["options_engine", "options_routes", "options_data",
        "options_allocator", "options_recommend", "options_learning"],
}

# Files carrying <!-- TIER:N BEGIN/END --> or # TIER:N BEGIN/END markers.
MARKED_FILES = [
    "web/static/index.html",
    "web/nginx.conf",
    "docker-compose.yml",
    "web/credentials.py",
    "README.md",
]

# Branch each tier is published to. Tier 4 is master by definition (it IS the
# full product), so it has no generated branch.
TIER_BRANCH = {
    1: "tier-1-base",
    2: "tier-2-brokerage",
    3: "tier-3-scanner",
    4: "master",
}

# One-line identity for each tier, rendered into README.md's TIER-IDENTITY
# block. Keep TIER_IDENTITY[4] in sync with what master's README literally
# contains — apply_tier asserts it, which is what keeps a tier-4 build
# byte-identical to master.
TIER_IDENTITY = {
    1: "Tier 1 — Base: single-ticker AI analysis",
    2: "Tier 2 — Brokerage: + Schwab account scanning",
    3: "Tier 3 — Scanner: + the weekly S&P 500 scanner",
    4: "Tier 4 — Full: + daily options paper trading (the complete product)",
}

# (file, regex, replacement template with {tier}) — applied after deletion +
# marker stripping. Deliberately NEVER touches pyproject.toml/uv.lock: every
# tier shares one dependency set, so a tier branch's `uv run` must resolve
# against the same lockfile as master.
SUBSTITUTIONS = [
    ("web/features.py", r"DEFAULT_TIER = \d+", "DEFAULT_TIER = {tier}"),
    ("docker-compose.yml", r"\$\{TIER:-\d+\}", "${{TIER:-{tier}}}"),
]

_MARKER_RE = re.compile(r"(?:<!--|#)\s*TIER:(\d+)\s+(BEGIN|END)\s*(?:-->)?\s*$")

# The README block rewritten per tier. Deliberately NOT a TIER:N marker (the
# sentinel has no digits, so _MARKER_RE never matches it and lint() ignores it).
_IDENTITY_RE = re.compile(
    r"(?s)(<!-- TIER-IDENTITY BEGIN -->\n).*?(<!-- TIER-IDENTITY END -->)"
)


def _readme_identity(tier: int) -> str:
    """The rendered TIER-IDENTITY block body for `tier`, sentinels excluded."""
    head = f"> **This branch: `{TIER_BRANCH[tier]}` — {TIER_IDENTITY[tier]}.**\n"
    if tier == 4:
        return head + (
            "> Development happens here. The reduced tiers are published as "
            "generated branches — see the tier table below.\n"
        )
    return head + (
        "> **GENERATED BRANCH — do not commit here.** Regenerated from `master` "
        "by `scripts/make_tier.py` (driven by `.github/workflows/tiers.yml`); "
        "the next regeneration force-pushes over this branch and your commit is "
        "gone. Develop on `master`.\n"
    )


# ---------------------------------------------------------------------------
# Marker stripping
# ---------------------------------------------------------------------------

def _strip_markers(text: str, tier: int, *, path: str) -> str:
    """Remove every TIER:N BEGIN..END block where N > tier. No nesting."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    active_tier: int | None = None  # tier of the block we're currently inside
    for i, line in enumerate(lines, start=1):
        m = _MARKER_RE.search(line)
        if m is None:
            # Keep content when we're outside any block, OR inside a block
            # we're keeping (active_tier <= tier). Only drop content whose
            # enclosing block's tier exceeds the target.
            if active_tier is None or active_tier <= tier:
                out.append(line)
            continue
        n, kind = int(m.group(1)), m.group(2)
        if kind == "BEGIN":
            if active_tier is not None:
                raise ValueError(f"{path}:{i}: nested TIER marker (already inside TIER:{active_tier})")
            active_tier = n
            if n <= tier:
                out.append(line)  # keep markers for N<=tier blocks (harmless, aids future diffs)
        else:  # END
            if active_tier is None:
                raise ValueError(f"{path}:{i}: TIER:{n} END with no matching BEGIN")
            if active_tier != n:
                raise ValueError(f"{path}:{i}: TIER:{n} END does not match open TIER:{active_tier} BEGIN")
            if n <= tier:
                out.append(line)
            active_tier = None
    if active_tier is not None:
        raise ValueError(f"{path}: unterminated TIER:{active_tier} BEGIN")
    return "".join(out)


def _lint_file(path: Path) -> list[str]:
    """Balance-check a single file's markers. Returns a list of error strings."""
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    stack: list[tuple[int, int]] = []  # (tier, line_no)
    for i, line in enumerate(text.splitlines(), start=1):
        m = _MARKER_RE.search(line)
        if m is None:
            continue
        n, kind = int(m.group(1)), m.group(2)
        if kind == "BEGIN":
            if stack:
                errors.append(f"{path}:{i}: nested TIER:{n} BEGIN inside TIER:{stack[-1][0]}")
            stack.append((n, i))
        else:
            if not stack:
                errors.append(f"{path}:{i}: TIER:{n} END with no open BEGIN")
            elif stack[-1][0] != n:
                errors.append(f"{path}:{i}: TIER:{n} END does not match TIER:{stack[-1][0]} BEGIN at line {stack[-1][1]}")
            else:
                stack.pop()
    for n, i in stack:
        errors.append(f"{path}:{i}: unterminated TIER:{n} BEGIN")
    return errors


def lint() -> int:
    """Verify every TIER:N marker is balanced, and only appears in MARKED_FILES."""
    errors: list[str] = []
    for rel in MARKED_FILES:
        path = ROOT / rel
        if not path.exists():
            errors.append(f"{rel}: listed in MARKED_FILES but does not exist")
            continue
        errors.extend(_lint_file(path))

    marked_set = {ROOT / f for f in MARKED_FILES}
    for path in ROOT.rglob("*"):
        if not path.is_file() or path in marked_set:
            continue
        if any(part in (".git", ".venv", "__pycache__", "node_modules", ".pipeline") for part in path.parts):
            continue
        if any(part.endswith(".egg-info") for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if _MARKER_RE.search(line):
                errors.append(f"{path.relative_to(ROOT)}:{i}: TIER marker outside MARKED_FILES")

    if errors:
        print("make_tier.py --lint: FAILED", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    print(f"make_tier.py --lint: OK ({len(MARKED_FILES)} marked files, all balanced)")
    return 0


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _files_to_delete(tier: int) -> list[str]:
    out: list[str] = []
    for k in (4, 3, 2):
        if k > tier:
            out.extend(TIER_ONLY_FILES[k])
    return out


def _git_dirty(root: Path) -> bool:
    proc = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True,
    )
    return bool(proc.stdout.strip())


def apply_tier(tier: int, *, root: Path = ROOT, force: bool = False) -> None:
    if not force and _git_dirty(root):
        sys.exit(
            "make_tier.py: working tree is dirty — refusing to delete files.\n"
            "Run against a disposable checkout (fresh `git worktree`), commit "
            "first, or pass --force if you really mean it."
        )

    for rel in _files_to_delete(tier):
        target = root / rel
        if target.is_dir():
            shutil.rmtree(target)
            print(f"deleted dir  {rel}")
        elif target.exists():
            target.unlink()
            print(f"deleted file {rel}")
        else:
            print(f"skip (already gone) {rel}")

    for rel in MARKED_FILES:
        path = root / rel
        if not path.exists():
            continue  # tolerate a marked file that was itself deleted above
        original = path.read_text(encoding="utf-8")
        stripped = _strip_markers(original, tier, path=rel)
        if stripped != original:
            path.write_text(stripped, encoding="utf-8")
            print(f"stripped markers > tier {tier} in {rel}")

    for rel, pattern, template in SUBSTITUTIONS:
        path = root / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        replacement = template.format(tier=tier)
        new_text, n = re.subn(pattern, replacement, text)
        if n:
            path.write_text(new_text, encoding="utf-8")
            print(f"substituted {n}x in {rel}: {pattern!r} -> tier {tier}")

    _rewrite_readme_identity(tier, root=root)


def _rewrite_readme_identity(tier: int, *, root: Path) -> None:
    """Rewrite README.md's TIER-IDENTITY block for `tier`.

    Also asserts that master's committed block matches _readme_identity(4)
    exactly. That drift guard is load-bearing: it's what makes a tier-4 build
    provably byte-identical to master (check_tier relies on it), and it fires
    in ci.yml's tier-check the moment the README and TIER_IDENTITY disagree —
    instead of silently shipping a wrong identity line on every tier branch.
    """
    readme = root / "README.md"
    if not readme.exists():
        return
    text = readme.read_text(encoding="utf-8")

    def _sub(target: int, s: str) -> tuple[str, int]:
        return _IDENTITY_RE.subn(
            lambda m: m.group(1) + _readme_identity(target) + m.group(2), s
        )

    as_tier4, n = _sub(4, text)
    if n != 1:
        raise ValueError(
            f"README.md: expected exactly 1 TIER-IDENTITY block, found {n}"
        )
    if as_tier4 != text:
        raise ValueError(
            "README.md's TIER-IDENTITY block has drifted from "
            "_readme_identity(4) — edit them together. (This assertion is what "
            "keeps a tier-4 build byte-identical to master.)"
        )

    new_text, _ = _sub(tier, text)
    if new_text != text:
        readme.write_text(new_text, encoding="utf-8")
        print(f"rewrote README TIER-IDENTITY block for tier {tier}")


# ---------------------------------------------------------------------------
# Check — verify a tier build is internally consistent
# ---------------------------------------------------------------------------

def _module_top_level_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """(lineno, module_name) for every Import/ImportFrom that is a DIRECT
    child of the module body — i.e. unconditional at import time.

    Deliberately does NOT descend into function/class bodies (lazy imports,
    fine — they only run if called) or `if`/`try` bodies (this codebase's own
    gating pattern is `if features.enabled("x"): from . import x_module`,
    which must not run at a tier where the branch is never taken). A leak is
    an import Python would actually execute merely by loading the file.
    """
    out: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` / `from .x import y` — module is None for
            # `from . import x`, so use the imported names themselves too.
            if node.module:
                out.append((node.lineno, node.module))
            for alias in node.names:
                out.append((node.lineno, alias.name))
    return out


def _leak_canary(root: Path, tier: int) -> list[str]:
    """Flag any surviving .py file that would CRASH AT IMPORT TIME because it
    unconditionally imports a module this tier just deleted. Comments,
    docstrings, and imports gated behind a function body or an
    `if features.enabled(...)` branch are not leaks — see
    _module_top_level_imports.
    """
    patterns = set(LEAK_CANARY_PATTERNS.get(tier, []))
    if not patterns:
        return []
    hits: list[str] = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
        except (UnicodeDecodeError, PermissionError, SyntaxError):
            continue
        for lineno, name in _module_top_level_imports(tree):
            leaf = name.rsplit(".", 1)[-1]
            if leaf in patterns:
                line = text.splitlines()[lineno - 1].strip()
                hits.append(f"{path.relative_to(root)}:{lineno}: {line[:100]}")
    return hits


def _compileall(root: Path) -> str | None:
    proc = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(root / "web"), str(root / "tests")],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return proc.stdout + proc.stderr
    return None


def _syntax_check_all(root: Path) -> list[str]:
    errors = []
    for path in list((root / "web").rglob("*.py")) + list((root / "tests").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path.relative_to(root)}: {exc}")
    return errors


def _compose_config_ok(root: Path) -> str | None:
    """`docker compose config -q` against a stubbed .env, no daemon required
    for syntax validation but docker compose still needs a daemon reachable
    to fully resolve — degrade to a warning if docker isn't available."""
    compose = root / "docker-compose.yml"
    stub_env = root / ".make_tier_check.env"
    required = re.findall(r"\$\{([A-Z0-9_]+):\?", compose.read_text(encoding="utf-8"))
    stub_env.write_text("\n".join(f"{k}=x" for k in sorted(set(required))) + "\n", encoding="utf-8")
    try:
        proc = subprocess.run(
            ["docker", "compose", "--env-file", str(stub_env), "-f", str(compose), "config", "-q"],
            capture_output=True, text=True, cwd=root,
        )
    except FileNotFoundError:
        return None  # docker not installed in this environment — skip, don't fail
    finally:
        stub_env.unlink(missing_ok=True)
    if proc.returncode != 0:
        return proc.stderr
    return None


def _nginx_syntax_ok(root: Path) -> str | None:
    """Real `nginx -t` via `docker run nginx:alpine`, degrading to a skip
    (None) when docker isn't available. Upstream hostnames
    (tradingagents-api etc.) don't resolve outside the compose network, so
    `nginx -t` would fail on DNS resolution rather than syntax — swap them
    for loopback IPs in a throwaway copy first, same trick used to validate
    this by hand during the tier-marker commit."""
    conf = root / "web" / "nginx.conf"
    if not conf.exists():
        return None
    text = conf.read_text(encoding="utf-8")
    text = re.sub(r"tradingagents-\w+:8000", "127.0.0.1:8000", text)
    tmp_conf = root / ".make_tier_check_nginx.conf"
    tmp_conf.write_text(text, encoding="utf-8")
    try:
        proc = subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{tmp_conf}:/etc/nginx/nginx.conf:ro",
             "nginx:alpine", "nginx", "-t"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return None
    finally:
        tmp_conf.unlink(missing_ok=True)
    if proc.returncode != 0:
        return proc.stdout + proc.stderr
    return None


def _pytest_collect_ok(root: Path) -> str | None:
    """`pytest --collect-only` using the REAL repo's already-installed venv
    (never `uv run` here — that would try to sync a fresh venv inside the
    temp dir, slow and pointless since every tier shares one dependency
    set). PYTHONPATH + cwd point at the tier build so `from web import ...`
    resolves there instead of the real repo."""
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        venv_python = ROOT / ".venv" / "bin" / "python"  # POSIX CI runners
    if not venv_python.exists():
        return None  # no venv available in this environment — skip, don't fail
    proc = subprocess.run(
        [str(venv_python), "-m", "pytest", "--collect-only", "-q"],
        cwd=root, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(root)},
    )
    if proc.returncode != 0:
        return proc.stdout[-4000:] + proc.stderr[-2000:]
    return None


def _copy_tracked_tree(root: Path, dest: Path) -> None:
    """Copy only git-TRACKED files from root into dest.

    Deliberately not shutil.copytree(root, ...): root's working directory
    carries .venv/, .git/, __pycache__/ and other dev-only cruft that has
    nothing to do with what a tier build contains, and diffing against it
    (see the tier-4 identity check below) would be comparing apples to a
    junk drawer. git ls-files is the actual definition of "what's in this
    repo".
    """
    proc = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True,
    )
    for rel in proc.stdout.decode("utf-8").split("\0"):
        if not rel:
            continue
        src = root / rel
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def check_tier(tier: int, *, root: Path = ROOT) -> int:
    """Build tier N into a temp copy of root's TRACKED files and verify it's
    internally consistent. Never mutates `root` itself."""
    import tempfile

    print(f"make_tier.py --check --tier {tier}: building into a temp copy ...")
    with tempfile.TemporaryDirectory(prefix=f"make_tier_{tier}_") as tmp:
        tmp_root = Path(tmp) / "repo"
        tmp_root.mkdir()
        _copy_tracked_tree(root, tmp_root)
        apply_tier(tier, root=tmp_root, force=True)

        ok = True

        if tier == 4:
            # Compare against a second, UNMODIFIED tracked-only copy — never
            # against `root` itself, which still has .venv/__pycache__/etc
            # that have nothing to do with the tier build. Run this BEFORE
            # compileall below, which would litter tmp_root with .pyc files
            # the baseline copy never gets and pollute the diff.
            baseline = Path(tmp) / "baseline"
            baseline.mkdir()
            _copy_tracked_tree(root, baseline)
            diff = subprocess.run(
                ["git", "diff", "--stat", "--no-index", str(baseline), str(tmp_root)],
                capture_output=True, text=True,
            )
            # A tier-4 build must equal master exactly — no deletions, no
            # marker stripping (every block is N<=4), no TIER-README.md.
            changed = [
                line for line in diff.stdout.splitlines()
                if line.strip() and "files changed" not in line
            ]
            if changed:
                ok = False
                print("  FAIL tier-4 must equal master exactly, but diff found:", file=sys.stderr)
                print(diff.stdout, file=sys.stderr)
            else:
                print("  OK   tier 4 output is byte-identical to master")

        errors = _syntax_check_all(tmp_root)
        if errors:
            ok = False
            print(f"  FAIL syntax: {len(errors)} file(s)", file=sys.stderr)
            for e in errors[:20]:
                print(f"    {e}", file=sys.stderr)
        else:
            print("  OK   syntax (ast.parse over web/ + tests/)")

        compile_err = _compileall(tmp_root)
        if compile_err:
            ok = False
            print("  FAIL compileall:", file=sys.stderr)
            print(compile_err, file=sys.stderr)
        else:
            print("  OK   compileall")

        leaks = _leak_canary(tmp_root, tier)
        if leaks:
            ok = False
            print(f"  FAIL leak canary: {len(leaks)} reference(s) to a deleted module", file=sys.stderr)
            for h in leaks[:20]:
                print(f"    {h}", file=sys.stderr)
        else:
            print("  OK   leak canary (no references to deleted modules)")

        compose_err = _compose_config_ok(tmp_root)
        if compose_err:
            ok = False
            print("  FAIL docker compose config:", file=sys.stderr)
            print(compose_err, file=sys.stderr)
        elif compose_err is None and shutil.which("docker") is None:
            print("  SKIP docker compose config (docker not available)")
        else:
            print("  OK   docker compose config")

        nginx_err = _nginx_syntax_ok(tmp_root)
        if nginx_err:
            ok = False
            print("  FAIL nginx -t:", file=sys.stderr)
            print(nginx_err, file=sys.stderr)
        elif nginx_err is None and shutil.which("docker") is None:
            print("  SKIP nginx -t (docker not available)")
        else:
            print("  OK   nginx -t")

        pytest_err = _pytest_collect_ok(tmp_root)
        venv_missing = not (ROOT / ".venv" / "Scripts" / "python.exe").exists() and \
            not (ROOT / ".venv" / "bin" / "python").exists()
        if pytest_err:
            ok = False
            print("  FAIL pytest --collect-only:", file=sys.stderr)
            print(pytest_err, file=sys.stderr)
        elif pytest_err is None and venv_missing:
            print("  SKIP pytest --collect-only (no .venv found)")
        else:
            print("  OK   pytest --collect-only")

        print(f"make_tier.py --check --tier {tier}: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1


# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tier", type=int, choices=(1, 2, 3, 4), help="target tier")
    p.add_argument("--apply", action="store_true", help="mutate the working tree in place")
    p.add_argument("--check", action="store_true", help="dry-run into a temp copy and verify")
    p.add_argument("--lint", action="store_true", help="verify TIER markers are balanced")
    p.add_argument("--force", action="store_true", help="skip the dirty-working-tree guard for --apply")
    args = p.parse_args()

    if args.lint:
        return lint()

    if args.tier is None:
        p.error("--tier is required unless --lint is given")

    if args.check:
        return check_tier(args.tier)
    if args.apply:
        apply_tier(args.tier, force=args.force)
        return 0

    p.error("pass --apply or --check (or --lint alone)")
    return 2  # unreachable, keeps type checkers happy


if __name__ == "__main__":
    raise SystemExit(main())
