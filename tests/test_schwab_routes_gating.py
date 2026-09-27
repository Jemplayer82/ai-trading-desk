"""Tier-gating parity for the Schwab OAuth routes extracted to schwab_routes.py.

Locks in: web.main.app registers the 3 Schwab paths iff the "schwab" feature
is on for the active DEFAULT_TIER (4 on master, rewritten to 1/2/3 by
scripts/make_tier.py on a stripped tier tree); explicit TIER=1/TIER=2 always
behave as documented regardless of DEFAULT_TIER — EXCEPT TIER=2 needs
web/schwab_routes.py to physically exist, which it doesn't below tier 2 on a
stripped tree (forcing the env var can't resurrect a deleted file).
"""
from __future__ import annotations

import importlib

import pytest

import web.features as features
import web.main as main_module
from tests._route_paths import app_route_paths

_SCHWAB_PATHS = {
    "/api/auth/schwab",
    "/api/auth/schwab/callback",
    "/api/auth/schwab/status",
}


@pytest.fixture()
def app_paths(monkeypatch):
    def _load(**env):
        monkeypatch.delenv("TIER", raising=False)
        monkeypatch.delenv("FEATURES", raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        importlib.reload(features)
        importlib.reload(main_module)
        return app_route_paths(main_module.app)

    yield _load
    # Leave web.main.app back at the physical default tier for any test that
    # imports it later — a prior failed reload here (e.g. a since-fixed
    # ImportError mid-reload) otherwise leaves the module cached in a broken
    # state for whatever test runs next in the same pytest session.
    monkeypatch.delenv("TIER", raising=False)
    monkeypatch.delenv("FEATURES", raising=False)
    importlib.reload(features)
    importlib.reload(main_module)


class TestSchwabRouteGating:
    def test_default_tier_registers_schwab_paths_iff_enabled(self, app_paths):
        paths = app_paths()
        if features.enabled("schwab"):
            assert _SCHWAB_PATHS <= paths
        else:
            assert not (paths & _SCHWAB_PATHS)

    def test_tier_1_registers_no_schwab_paths(self, app_paths):
        # Safe at any physical tier: TIER=1 means enabled("schwab") is False,
        # so web.main never attempts the schwab_routes import at all.
        paths = app_paths(TIER="1")
        assert not (paths & _SCHWAB_PATHS)

    @pytest.mark.skipif(features.DEFAULT_TIER < 2,
                         reason="schwab_routes.py isn't physically present below tier 2 — "
                                "TIER=2 can't be faked via env once it's deleted")
    def test_tier_2_registers_schwab_paths(self, app_paths):
        paths = app_paths(TIER="2")
        assert _SCHWAB_PATHS <= paths
