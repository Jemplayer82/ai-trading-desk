"""Enumerate every HTTP/WebSocket path an app serves.

FastAPI 0.141+ keeps ``include_router`` results as a nested ``_IncludedRouter``
entry in ``app.routes`` (with the real routes under ``original_router``)
instead of flattening them, so ``{r.path for r in app.routes}`` silently misses
every included route. Recurse into nested routers so tier-gating tests keep
seeing the whole surface.
"""
from __future__ import annotations


def app_route_paths(app) -> set[str]:
    out: set[str] = set()
    stack = list(getattr(app, "routes", []))
    while stack:
        r = stack.pop()
        inner = getattr(r, "original_router", None) or getattr(r, "router", None)
        if inner is not None and inner is not r and getattr(inner, "routes", None):
            stack.extend(inner.routes)
            continue
        path = getattr(r, "path", None)
        if path is not None:
            out.add(path)
    return out
