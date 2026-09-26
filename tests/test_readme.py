"""Guard rails that keep README.md honest against the live code + deployment.

The README is authored in an external generator and re-exported, which has
repeatedly reintroduced stale claims — old image names, a fake DATABASE_URL,
the wrong test paths, a 5-tab count, a removed "Messages Log" panel. These
tests fail CI the moment any of those reappear, so a bad export can't ship
silently.

When the architecture GENUINELY changes (e.g. an image is renamed, a tab is
added), update the README **and** the matching assertion here in the same PR —
don't just delete the check.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_README = Path(__file__).resolve().parent.parent / "README.md"


@pytest.fixture(scope="module")
def readme() -> str:
    return _README.read_text(encoding="utf-8")


# (stale substring that must NOT appear, why it's wrong / what's correct now)
_FORBIDDEN = [
    ("tradingagents-switchboard", "deploy moved to the tradingagents / tradingagents-web images"),
    ("DATABASE_URL", "the app reads TRADINGAGENTS_WEB_DB (file web.db), not a DATABASE_URL"),
    ("web/requirements.txt", "deps live in root requirements.txt / pyproject, not web/requirements.txt"),
    ("tradingagents/tests", "tests live in tests/ at the repo root"),
    ("web/tests", "tests live in tests/ at the repo root"),
    ("4-tab", "don't state a tab COUNT — it differs per tier and goes stale; list the tabs instead"),
    ("Messages Log", "the tool-calls panel was removed; it is Live Reasoning now"),
    (
        "30% below the peak",
        "the arm-at-+50% / trail-30%-below-peak options model was retired; "
        "stops are per-account policies on paper_accounts now",
    ),
    (
        "options_trail_arm_pct",
        "that global config key was deleted along with options_trailing_stop / "
        "options_trail_give_back; stop behavior is per-account policy",
    ),
    ("every Saturday", "the weekly Saturday S&P scan was replaced by daily allocation from the shared research"),
    ("default 07:30 ET", "account allocation times default to 09:00 ET off the shared 00:00 research"),
]


@pytest.mark.unit
@pytest.mark.parametrize("needle, why", _FORBIDDEN)
def test_readme_has_no_stale_claims(readme: str, needle: str, why: str) -> None:
    assert needle not in readme, f"README contains stale claim {needle!r} — {why}"


@pytest.mark.unit
def test_readme_assets_all_exist(readme: str) -> None:
    """Every image the README references must actually exist in the repo."""
    root = _README.parent
    refs = set(re.findall(r"\((assets/[^)\s]+)\)", readme))
    refs |= set(re.findall(r'src="(assets/[^"]+)"', readme))
    missing = sorted(r for r in refs if not (root / r).exists())
    assert not missing, f"README references missing assets: {missing}"


@pytest.mark.unit
def test_readme_names_the_deployed_images(readme: str) -> None:
    """The canonical deploy images must be named (catches a generator dropping them)."""
    assert "ghcr.io/jemplayer82/tradingagents" in readme
    assert "tradingagents-web" in readme


@pytest.mark.unit
def test_readme_has_tier_identity_block(readme: str) -> None:
    """scripts/make_tier.py rewrites this block per tier so each generated
    branch says which tier it is. Losing the sentinels would silently ship
    master's identity line on every tier branch."""
    assert "<!-- TIER-IDENTITY BEGIN -->" in readme
    assert "<!-- TIER-IDENTITY END -->" in readme
