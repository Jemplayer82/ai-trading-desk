#!/usr/bin/env python3
"""Detect drift between our Claude model catalog and Anthropic's live docs.

From the Claude 4.6 generation on, model IDs are *pinned snapshots* rather than
evergreen pointers, so a stale entry in the catalog does not 404 — it quietly
keeps running an older model until somebody notices the outputs got worse. This
script is the "somebody".

It reads the model IDs out of Anthropic's public models-overview page and diffs
them against the ``claude-*`` entries in ``MODEL_OPTIONS``:

* **stale**  — in our catalog, absent from the docs. The model is retired or the
  ID was mistyped. This is the failure case (exit 1).
* **unlisted** — in the docs, absent from our catalog. Informational: not every
  released model belongs in the dropdown. Ones we have deliberately declined
  live in ``DELIBERATELY_EXCLUDED`` so the report stays quiet about them.

The docs page is public markdown — no API key, no Anthropic API call, and no
token spend. Standard library only, so CI needs nothing but a checkout.

Usage::

    python scripts/check_model_catalog.py           # report + exit 1 on stale
    python scripts/check_model_catalog.py --strict   # also exit 1 on unlisted
    python scripts/check_model_catalog.py --json     # machine-readable output

Exit codes: 0 clean, 1 drift, 2 could not reach the docs.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

_CATALOG_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "tradingagents" / "llm_clients" / "model_catalog.py"
)


def _load_catalog() -> dict:
    """Load model_catalog.py by path, bypassing the package ``__init__``.

    ``tradingagents.llm_clients.__init__`` imports the LangChain-backed client
    factory, so a plain import would drag the whole dependency tree in. The
    catalog module itself needs nothing beyond ``typing``, so loading it
    directly keeps this script runnable on a bare checkout with no venv — which
    is what lets the weekly workflow skip a dependency install entirely.
    """
    spec = importlib.util.spec_from_file_location("_model_catalog", _CATALOG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MODEL_OPTIONS


MODEL_OPTIONS = _load_catalog()

DOCS_URL = "https://platform.claude.com/docs/en/about-claude/models/overview.md"

# Providers whose menus are served by Anthropic model IDs. "switchboard" reaches
# Claude through Cleo's `claude -p` CLI, which takes the same IDs.
CLAUDE_PROVIDERS = ("anthropic", "switchboard")

# Released models we have consciously chosen not to offer. Keeping them here
# means the weekly run reports genuinely new models instead of the same three
# every time. Removing a name from this set makes it show up as "unlisted".
DELIBERATELY_EXCLUDED = {
    # Double Opus pricing; thinking is always on (an explicit thinking=disabled
    # is a 400), requires 30-day data retention, and can answer with
    # stop_reason="refusal" — none of which AnthropicClient handles yet.
    "claude-fable-5",
    # Project Glasswing only: invitation-gated, so an entry in the dropdown
    # would 404 for everyone who isn't in the program.
    "claude-mythos-5",
    "claude-mythos-preview",
    # Still served, but superseded within their own family — we offer the newer
    # one instead. Delete a line here if you ever want to re-offer that model;
    # when Anthropic finally retires it, it leaves the docs and this entry just
    # goes inert. Anything genuinely NEW is what you want the report to surface.
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-opus-4-5-20251101",
    "claude-sonnet-4-5",
    "claude-sonnet-4-5-20250929",
}

# Two-stage extraction. The loose pass grabs anything that looks like a Claude
# ID while refusing tokens glued to a prefix — that drops the Bedrock-style
# `anthropic.claude-opus-4-8` rows and the footnote-mangled variants next to
# them (`anthropic.claude-fable-53`). The strict pass then keeps only real ID
# shapes, discarding heading anchors such as
# `#claude-fable-5-and-claude-mythos-5`.
_CANDIDATE_RE = re.compile(
    r"(?<![\w.@-])(claude-(?:opus|sonnet|haiku|fable|mythos)-[a-z0-9-]*[a-z0-9])"
)
_STRICT_RE = re.compile(
    r"^claude-(?:opus|sonnet|haiku|fable|mythos)-(?:preview|\d+(?:-\d+)*)$"
)

# Wikipedia-style lesson from web/spy_tickers.py: docs hosts reject bare
# urllib user-agents.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ai-trading-desk-catalog-check/1.0; "
        "+https://github.com/Jemplayer82/ai-trading-desk)"
    )
}


def fetch_live_model_ids(url: str = DOCS_URL, timeout: int = 30) -> set[str]:
    """Return every Claude model ID advertised on Anthropic's overview page."""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        body = resp.read().decode("utf-8", errors="replace")

    ids = {m.group(1) for m in _CANDIDATE_RE.finditer(body)}
    live = {i for i in ids if _STRICT_RE.match(i)}
    if not live:
        raise RuntimeError(
            f"Parsed 0 model IDs from {url} — the page layout probably changed. "
            "Update _CANDIDATE_RE / _STRICT_RE before trusting this check."
        )
    return live


def catalog_model_ids() -> dict[str, set[str]]:
    """Claude IDs in our catalog, keyed by the provider that offers them.

    Only ``claude-*`` values are model IDs. The switchboard menu also carries
    the bare CLI family aliases (``opus``/``sonnet``/``fable``), which resolve
    at call time and by design never appear in Anthropic's ID list — checking
    them here would report permanent false drift. ``llama3`` and the ``custom``
    UI sentinel are skipped for the same reason.
    """
    out: dict[str, set[str]] = {}
    for provider in CLAUDE_PROVIDERS:
        values = {
            value
            for options in MODEL_OPTIONS.get(provider, {}).values()
            for _, value in options
            if value.startswith("claude-")
        }
        if values:
            out[provider] = values
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also fail when the docs list a model our catalog doesn't offer",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of prose")
    parser.add_argument("--url", default=DOCS_URL, help="override the docs URL")
    args = parser.parse_args()

    try:
        live = fetch_live_model_ids(args.url)
    except (urllib.error.URLError, RuntimeError, TimeoutError) as exc:
        print(f"could not read the model list from {args.url}: {exc}", file=sys.stderr)
        return 2

    by_provider = catalog_model_ids()
    ours = {model for models in by_provider.values() for model in models}

    stale = {
        model: sorted(p for p, models in by_provider.items() if model in models)
        for model in sorted(ours - live)
    }
    unlisted = sorted(live - ours - DELIBERATELY_EXCLUDED)

    if args.json:
        print(json.dumps({"stale": stale, "unlisted": unlisted, "live": sorted(live)}, indent=2))
    else:
        print(f"{len(live)} Claude models listed at {args.url}")
        print(f"{len(ours)} Claude models offered by our catalog\n")

        if stale:
            print("STALE - offered by us, no longer in Anthropic's docs:")
            for model, providers in stale.items():
                print(f"  {model}  (provider: {', '.join(providers)})")
            print("\n  Fix: edit MODEL_OPTIONS in tradingagents/llm_clients/model_catalog.py")
            print("  and bump its 'Last verified' date.\n")
        else:
            print("STALE: none - every model we offer is still listed.\n")

        if unlisted:
            print("UNLISTED - available from Anthropic, not offered by us:")
            for model in unlisted:
                print(f"  {model}")
            print(
                "\n  Add the ones you want to MODEL_OPTIONS, or add the rest to\n"
                "  DELIBERATELY_EXCLUDED in this script to silence them.\n"
            )
        else:
            print("UNLISTED: none.\n")

    if stale:
        return 1
    if unlisted and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
