"""Feature gates for tier-split deployments.

Cumulative tiers: 1 = single-ticker analysis only, 2 = +Schwab brokerage,
3 = +S&P 500 scanner, 4 = +options paper trading (default — the full
product, what master ships). A deployment declares its tier via the TIER
env var, or overrides individual features directly via FEATURES (comma
list, e.g. "schwab,sp500").

DEFAULT_TIER is rewritten to 1/2/3 by scripts/make_tier.py when generating
a tier branch; it stays 4 on master. This lets web/main.py, scheduler.py,
and the portfolio_main.py shell register routes/jobs conditionally and stay
byte-identical across every tier branch — only this constant changes.
"""
from __future__ import annotations

import os

DEFAULT_TIER = 2

_TIER_FEATURES: dict[int, frozenset[str]] = {
    1: frozenset(),
    2: frozenset({"schwab"}),
    3: frozenset({"schwab", "sp500"}),
    4: frozenset({"schwab", "sp500", "options"}),
}


def _tier() -> int:
    raw = os.environ.get("TIER")
    if raw:
        try:
            t = int(raw)
            if t in _TIER_FEATURES:
                return t
        except ValueError:
            pass
    return DEFAULT_TIER


def _explicit_features() -> frozenset[str] | None:
    raw = os.environ.get("FEATURES")
    if raw is None:
        return None
    return frozenset(f.strip() for f in raw.split(",") if f.strip())


def enabled(feature: str) -> bool:
    """True if `feature` ("schwab" | "sp500" | "options") is active.

    FEATURES env (comma list) wins if set; otherwise derived from TIER
    (falling back to DEFAULT_TIER). Evaluated call-time, not cached, so
    tests can monkeypatch the env mid-process.
    """
    explicit = _explicit_features()
    if explicit is not None:
        return feature in explicit
    return feature in _TIER_FEATURES[_tier()]
