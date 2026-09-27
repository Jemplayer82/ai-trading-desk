"""FastAPI app for the `tradingagents-api` container (`uvicorn web.main:app`).

Dashboard backend: single-ticker analysis (streamed over the /api/analyze
WebSocket), dashboard login/users, app settings + LLM credential management,
saved-analysis Q&A and charting, and the /api/bus Agent Bus bridge. Schwab
OAuth (web/schwab_routes.py) mounts here too, but only when the "schwab"
feature is on (see web/features.py) — the tier-1 app has no brokerage.

Routing contract (web/nginx.conf): nginx sends /api/spy*, /api/accounts and
/api/portfolio* to a SEPARATE app — web/portfolio_main.py, running in its own
container so multi-hour scans can't block ad-hoc analysis — and everything
else under /api/ here. An endpoint added to the wrong app works in a
single-process dev run but is a silent 404 in production.

Auth model (gate lives in web/auth_app.py and is fail-closed):
  - Browser requests carry a `ta_session` cookie -> server-side session row in
    SQLite. The HTTP middleware enforces this on every /api/ route outside a
    small public allowlist.
  - Inter-container calls (scheduler -> this app) send an X-Internal-Token
    header, compared with hmac.compare_digest — never `==`, which would leak
    the token through a timing side-channel.
  - The HTTP middleware does not see WebSocket scope, so /api/analyze and
    /api/bus re-implement the same gate inline; both accept either credential.

The SQLite DB (web/db.py) sits on a Docker volume shared with the portfolio
and scheduler containers, so sessions minted here are honored everywhere.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import auth_app, bus, db, features
from . import credentials as creds
from .llm_helpers import llm_for
from .providers import ANALYSTS, DEPTH_PRESETS, LANGUAGES, get_providers
from .runner import run_analysis_sync

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Custom app-setting keys must look like env vars.
_SETTING_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _total_budget() -> int:
    """Shared LLM concurrency budget (OLLAMA_MAX_CONCURRENCY env var, default 5, floor 1)."""
    try:
        return max(1, int(os.environ.get("OLLAMA_MAX_CONCURRENCY", "5")))
    except (TypeError, ValueError):
        return 5


# Process-local cap on concurrent single-ticker analyses in THIS (api) container,
# so one user opening many tabs can't alone exceed the shared Ollama budget. The
# scanner in the portfolio container yields to these via the llm_activity table.
_SINGLE_SLOTS = threading.Semaphore(_total_budget())

app = FastAPI(title="TradingAgents Web")

# Gate every /api/ route behind a login session (allowlist + internal-token
# bypass live in auth_app). Registered before route handlers run.
app.middleware("http")(auth_app.auth_middleware)

if features.enabled("schwab"):
    from .schwab_routes import router as schwab_router
    app.include_router(schwab_router)


@app.on_event("startup")
def _startup() -> None:
    """Per-container boot: create/migrate the shared DB, then project DB-stored
    credentials and settings into this process's env so downstream code that
    resolves keys via env vars sees them. Purges are best-effort housekeeping
    and must never block startup."""
    db.init_db()
    creds.apply_to_env()
    creds.apply_settings_to_env()
    try:
        db.purge_expired_sessions()
    except Exception:
        log.exception("session purge failed")
    try:
        db.purge_stale_login_attempts(auth_app.LOCKOUT_WINDOW_MINUTES)
    except Exception:
        log.exception("login-attempt purge failed")
    try:
        db.purge_stale_activity()
    except Exception:
        log.exception("activity purge failed")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/providers")
def providers() -> dict[str, Any]:
    return {
        "providers": get_providers(),
        "analysts": ANALYSTS,
        "languages": LANGUAGES,
        "depth_presets": DEPTH_PRESETS,
    }


@app.get("/api/preferences")
def get_prefs() -> dict[str, Any]:
    return db.get_preferences()


@app.post("/api/preferences")
async def save_prefs(payload: dict[str, Any]) -> dict[str, str]:
    db.save_preferences(payload)
    return {"status": "saved"}


# ---------- authentication (dashboard login) ----------

@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    """Public. Reports auth state and whether first-run setup is needed."""
    if db.count_users() == 0:
        return {"authenticated": False, "setup_required": True}
    username = auth_app.current_username(request)
    return {"authenticated": bool(username), "username": username, "setup_required": False}


@app.post("/api/auth/setup")
def auth_setup(payload: dict[str, Any], response: Response) -> dict[str, Any]:
    """Public, but only works while no users exist. Creates the first admin."""
    if db.count_users() > 0:
        raise HTTPException(status_code=403, detail="setup already complete")
    username = ((payload or {}).get("username") or "").strip()
    password = (payload or {}).get("password") or ""
    if not username or len(password) < 8:
        raise HTTPException(status_code=400, detail="username required, password >= 8 chars")
    db.create_user(username, auth_app.hash_password(password))
    token, _ = auth_app.new_session(username)
    auth_app.set_session_cookie(response, token)
    return {"status": "created", "username": username}


@app.post("/api/auth/login")
def auth_login(payload: dict[str, Any], request: Request, response: Response) -> dict[str, Any]:
    username = ((payload or {}).get("username") or "").strip()
    password = (payload or {}).get("password") or ""
    ip = auth_app.client_ip(request)
    if auth_app.is_login_locked(username, ip):
        # Same message whether the username exists or not — the lockout must
        # not become a user-enumeration oracle.
        raise HTTPException(status_code=429, detail="too many failed attempts; try again later")
    user = db.get_user(username)
    if not user or not auth_app.verify_password(password, user["password_hash"]):
        db.record_failed_login(username, ip)
        raise HTTPException(status_code=401, detail="invalid username or password")
    db.clear_failed_logins(username)
    token, _ = auth_app.new_session(username)
    auth_app.set_session_cookie(response, token)
    return {"status": "ok", "username": username}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response) -> dict[str, str]:
    token = request.cookies.get(auth_app.COOKIE_NAME)
    if token:
        db.delete_session(token)
    auth_app.clear_session_cookie(response)
    return {"status": "logged_out"}


@app.get("/api/auth/users")
def auth_list_users() -> dict[str, Any]:
    return {"users": db.list_users()}


@app.post("/api/auth/users")
def auth_add_user(payload: dict[str, Any]) -> dict[str, Any]:
    username = ((payload or {}).get("username") or "").strip()
    password = (payload or {}).get("password") or ""
    if not username or len(password) < 8:
        raise HTTPException(status_code=400, detail="username required, password >= 8 chars")
    try:
        db.create_user(username, auth_app.hash_password(password))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="username already exists") from None
    return {"status": "created", "username": username}


@app.post("/api/auth/password")
def auth_change_password(payload: dict[str, Any], request: Request) -> dict[str, str]:
    username = auth_app.current_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="not logged in")
    current = (payload or {}).get("current_password") or ""
    new = (payload or {}).get("new_password") or ""
    user = db.get_user(username)
    if not user or not auth_app.verify_password(current, user["password_hash"]):
        raise HTTPException(status_code=403, detail="current password incorrect")
    if len(new) < 8:
        raise HTTPException(status_code=400, detail="new password must be >= 8 chars")
    db.set_user_password(username, auth_app.hash_password(new))
    return {"status": "changed"}


# ---------- app settings (env-style config managed from the UI) ----------

@app.get("/api/settings")
def list_settings_endpoint() -> dict[str, Any]:
    """Curated registry + custom settings, masked. Behind auth."""
    return creds.list_settings_meta()


@app.put("/api/settings/{key}")
def save_setting_endpoint(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    key = key.strip().upper()
    if not _SETTING_KEY_RE.match(key):
        raise HTTPException(status_code=400, detail="key must match ^[A-Z][A-Z0-9_]*$")
    value = (payload or {}).get("value")
    if value is None or str(value) == "":
        raise HTTPException(status_code=400, detail="missing 'value' in body")
    problem = creds.validate_setting_value(key, str(value).strip())
    if problem:
        raise HTTPException(status_code=400, detail=f"{key}: {problem}")
    db.set_app_setting(key, str(value))
    creds.apply_settings_to_env()
    return {"status": "saved", "key": key, "masked": creds.mask_setting(key, str(value))}


@app.delete("/api/settings/{key}")
def delete_setting_endpoint(key: str) -> dict[str, str]:
    key = key.strip().upper()
    if not db.delete_app_setting(key):
        raise HTTPException(status_code=404, detail="no setting stored for that key")
    # Unset from this process's env so the cleared value stops taking effect
    # immediately (other containers drop it at their next restart/refresh).
    os.environ.pop(key, None)
    return {"status": "cleared", "key": key}


# ---------- LLM provider API-key management ----------

@app.get("/api/credentials")
def list_credentials_endpoint() -> dict[str, Any]:
    """Per-provider credential metadata. Keys are masked — never raw."""
    return {"credentials": creds.list_meta()}


@app.put("/api/credentials/{provider}")
def save_credential_endpoint(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Save an API key (and optional base URL) for `provider`.

    Body: {"api_key": str, "base_url": str?}
    """
    api_key = ((payload or {}).get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="missing 'api_key' in body")
    base_url = (payload or {}).get("base_url") or None
    if base_url:
        base_url = str(base_url).strip() or None
    db.set_credential(provider.lower(), api_key, base_url)
    creds.apply_to_env()
    return {"status": "saved", "provider": provider, "masked": creds.mask_key(api_key)}


@app.delete("/api/credentials/{provider}")
def delete_credential_endpoint(provider: str) -> dict[str, str]:
    """Clear the DB-stored credential for `provider`.

    Note: env vars set externally (.env, docker-compose) are NOT touched.
    """
    if not db.delete_credential(provider.lower()):
        raise HTTPException(status_code=404, detail="no DB credential set for that provider")
    return {"status": "cleared", "provider": provider}


# ---------- saved analyses (history) ----------

@app.get("/api/analyses")
def list_analyses() -> dict[str, Any]:
    return {"analyses": db.list_analyses()}


@app.get("/api/analyses/{analysis_id}")
def get_analysis(analysis_id: int) -> dict[str, Any]:
    row = db.get_analysis(analysis_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return row


@app.delete("/api/analyses/{analysis_id}")
def delete_analysis_endpoint(analysis_id: int) -> dict[str, Any]:
    if not db.delete_analysis(analysis_id):
        raise HTTPException(status_code=404, detail="not found")
    return {"status": "deleted", "id": analysis_id}


@app.delete("/api/analyses")
def delete_all_analyses_endpoint() -> dict[str, Any]:
    return {"status": "deleted", "count": db.delete_all_analyses()}


# ---------- ticker company-info lookup (cached) ----------

@app.get("/api/ticker-info/{ticker}")
def ticker_info(ticker: str) -> dict[str, Any]:
    """Company name + website for a ticker, resolved via yfinance and cached.

    Falls back to the Yahoo Finance quote page when no corporate website is
    available, so the link is always usable.
    """
    t = (ticker or "").strip().upper()
    if not t:
        raise HTTPException(status_code=400, detail="missing ticker")
    # Validate before the value reaches yfinance / the Schwab MCP / a URL or the
    # cache path. safe_ticker_component enforces a strict charset and rejects
    # path-traversal-style input.
    try:
        from tradingagents.dataflows.utils import safe_ticker_component
        safe_ticker_component(t)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid ticker") from None
    yahoo = f"https://finance.yahoo.com/quote/{t}"

    cached = db.get_ticker_info(t)
    if cached:
        return {
            "ticker": t,
            "name": cached.get("name") or t,
            "website": cached.get("website") or yahoo,
        }

    name = t
    website = ""

    # Prefer the company name from a real-time Schwab quote (reference.description);
    # it's fast and avoids a yfinance .info call. yfinance still supplies the website.
    try:
        from tradingagents.dataflows import schwab_mcp
        quotes = schwab_mcp.get_quotes([t]) if schwab_mcp.market_data_enabled() else None
        desc = ((quotes or {}).get(t, {}).get("reference") or {}).get("description")
        if isinstance(desc, str) and desc.strip():
            name = desc.strip().title()
    except Exception:
        log.warning("schwab ticker name lookup failed for %s", t)

    try:
        import yfinance as yf  # lazy — keeps app startup light
        info = yf.Ticker(t).info or {}
        if name == t:
            name = info.get("longName") or info.get("shortName") or t
        site = info.get("website") or ""
        if isinstance(site, str) and site.startswith(("http://", "https://")):
            website = site
    except Exception:
        log.warning("ticker-info lookup failed for %s", t)

    final_website = website or yahoo
    # Only cache once we actually resolved something, so a transient yfinance
    # failure retries next time instead of sticking a poor result forever.
    if name != t or website:
        try:
            db.set_ticker_info(t, name, final_website)
        except Exception:
            pass
    return {"ticker": t, "name": name, "website": final_website}


@app.get("/api/ticker-search")
def ticker_search(q: str = "") -> list[dict[str, str]]:
    """Resolve a company name (or partial symbol) to candidate tickers.

    Powers the ticker-box type-ahead: the user types "Apple" (or "AAP") and
    picks AAPL. Backed by Yahoo Finance's public search endpoint (no API key).
    Returns up to ~8 {symbol, name, exchange} candidates, equities/ETFs only,
    each symbol passing safe_ticker_component(). Fails soft to [] so a Yahoo
    hiccup never breaks the box — the user can always type a raw symbol instead.
    """
    query = (q or "").strip()
    if len(query) < 2:
        return []
    try:
        import httpx  # lazy — already a dependency (web/bus.py, schwab_mcp)

        from tradingagents.dataflows.utils import safe_ticker_component
        resp = httpx.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": query, "quotesCount": 8, "newsCount": 0},
            headers={"User-Agent": "Mozilla/5.0"},  # Yahoo 429s requests with no UA
            timeout=4.0,
        )
        resp.raise_for_status()
        quotes = resp.json().get("quotes") or []
    except Exception:
        log.warning("ticker-search failed for %r", query)
        return []

    out: list[dict[str, str]] = []
    for item in quotes:
        if item.get("quoteType") not in ("EQUITY", "ETF"):
            continue
        sym = (item.get("symbol") or "").strip().upper()
        if not sym:
            continue
        try:
            safe_ticker_component(sym)  # same charset gate as a typed ticker
        except ValueError:
            continue
        name = item.get("longname") or item.get("shortname") or sym
        exch = item.get("exchDisp") or item.get("exchange") or ""
        out.append({"symbol": sym, "name": str(name), "exchange": str(exch)})
    return out


# ---------- Q&A and chart for saved analyses ----------

@app.post("/api/analyses/{analysis_id}/ask")
async def ask_about_analysis(analysis_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Multi-turn Q&A over a saved analysis.

    Body: {"question": str, "history": [{"role": "user"|"assistant", "content": str}, ...]}
    """
    row = db.get_analysis(analysis_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")

    question = ((payload or {}).get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="missing 'question' in body")

    history = (payload or {}).get("history") or []
    if not isinstance(history, list):
        raise HTTPException(status_code=400, detail="'history' must be a list")

    sections = [
        ("Ticker", row.get("ticker", "")),
        ("Trade Date", row.get("trade_date", "")),
        ("Final Decision", row.get("final_decision", "")),
        ("Processed Signal", row.get("processed_signal", "")),
        ("Trader Plan", row.get("trader_plan") or row.get("trader_investment_plan") or ""),
        ("Investment Plan", row.get("investment_plan", "")),
        ("Market Report", row.get("market_report", "")),
        ("Sentiment Report", row.get("sentiment_report", "")),
        ("News Report", row.get("news_report", "")),
        ("Fundamentals Report", row.get("fundamentals_report", "")),
    ]
    context = "\n\n".join(f"## {name}\n{val}" for name, val in sections if val)

    system = (
        "You are a financial analyst answering follow-up questions about a saved stock analysis. "
        "Use ONLY the information provided in the analysis below. If the analysis does not address "
        "the question, say so directly rather than speculating. Be concise. When possible cite "
        "which section you're drawing from (e.g., 'per the Market Report' or 'per the Trader Plan')."
        f"\n\n=== ANALYSIS ===\n{context}\n=== END ANALYSIS ==="
    )

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for turn in history:
        if (
            isinstance(turn, dict)
            and turn.get("role") in ("user", "assistant")
            and turn.get("content")
        ):
            messages.append({"role": turn["role"], "content": str(turn["content"])})
    messages.append({"role": "user", "content": question})

    prefs = db.get_preferences() or {}
    config: dict[str, Any] = {
        "llm_provider": prefs.get("provider") or "ollama",
        # This call always passes deep=False (see llm_for below) — only the
        # quick-role provider matters here.
        "quick_llm_provider": prefs.get("quick_provider") or prefs.get("provider") or "ollama",
        "deep_think_llm": prefs.get("deep_model"),
        "quick_think_llm": prefs.get("quick_model"),
    }

    try:
        llm_client = llm_for(config, deep=False, temperature=0.3)
        response = await asyncio.to_thread(llm_client.invoke, messages)
        answer = response.content if hasattr(response, "content") else str(response)
    except Exception as exc:
        log.exception("Q&A LLM call failed for analysis %s", analysis_id)
        raise HTTPException(status_code=500, detail=f"LLM error: {exc}") from exc

    return {"answer": answer}


@app.get("/api/analyses/{analysis_id}/chart-data")
async def get_analysis_chart_data(
    analysis_id: int, lookback_days: int = 180
) -> dict[str, Any]:
    """Point-in-time OHLCV + indicator series for charting a saved analysis."""
    row = db.get_analysis(analysis_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    ticker = row.get("ticker")
    trade_date = row.get("trade_date")
    if not ticker or not trade_date:
        raise HTTPException(status_code=400, detail="analysis missing ticker or trade_date")

    try:
        result = await asyncio.to_thread(_build_chart_data, ticker, trade_date, lookback_days)
    except Exception as exc:
        log.exception("chart data failed for %s on %s", ticker, trade_date)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


def _build_chart_data(ticker: str, trade_date: str, lookback_days: int) -> dict[str, Any]:
    """Candles + indicator series for the lookback window ending at trade_date.

    Point-in-time: OHLCV is loaded as of trade_date (not today), so the chart
    matches what the agents saw when the analysis ran. NaN indicator values
    (warm-up periods, e.g. the first 200 bars of sma_200) are dropped
    per-series rather than zero-filled.
    """
    import pandas as pd
    from stockstats import wrap

    from tradingagents.dataflows.stockstats_utils import load_ohlcv

    df = load_ohlcv(ticker, trade_date)
    if df.empty:
        raise RuntimeError(f"no OHLCV data for {ticker} on or before {trade_date}")

    end_dt = pd.to_datetime(trade_date)
    start_dt = end_dt - pd.Timedelta(days=lookback_days)
    df = df[df["Date"] >= start_dt].copy()
    if df.empty:
        raise RuntimeError(
            f"no OHLCV data in {lookback_days}d lookback window for {ticker}"
        )

    df = df.sort_values("Date").reset_index(drop=True)
    ss = wrap(df.copy())

    indicator_map = {
        "rsi_14": "rsi_14",
        "macd": "macd",
        "macds": "macds",
        "macdh": "macdh",
        "sma_50": "close_50_sma",
        "sma_200": "close_200_sma",
        "ema_10": "close_10_ema",
        "boll": "boll",
        "boll_ub": "boll_ub",
        "boll_lb": "boll_lb",
    }
    # Trigger stockstats to compute each indicator (lazy columns)
    for ss_col in indicator_map.values():
        try:
            _ = ss[ss_col]
        except Exception:
            pass

    dates = df["Date"].dt.strftime("%Y-%m-%d").tolist()
    candles = [
        {
            "time": dates[i],
            "open": float(df["Open"].iloc[i]),
            "high": float(df["High"].iloc[i]),
            "low": float(df["Low"].iloc[i]),
            "close": float(df["Close"].iloc[i]),
        }
        for i in range(len(df))
    ]

    def _series(col_name: str) -> list[dict[str, Any]]:
        if col_name not in ss.columns:
            return []
        series = ss[col_name]
        out: list[dict[str, Any]] = []
        for i in range(len(series)):
            v = series.iloc[i] if hasattr(series, "iloc") else series[i]
            try:
                if pd.isna(v):
                    continue
                out.append({"time": dates[i], "value": float(v)})
            except (TypeError, ValueError):
                continue
        return out

    indicators = {friendly: _series(ss_col) for friendly, ss_col in indicator_map.items()}

    return {
        "ticker": ticker,
        "trade_date": trade_date,
        "lookback_days": lookback_days,
        "candles": candles,
        "indicators": indicators,
    }


# ---------- single-ticker analysis WebSocket (existing) ----------

@app.websocket("/api/analyze")
async def analyze(ws: WebSocket) -> None:
    """Run one single-ticker analysis, streaming progress frames to the client.

    Protocol: the client sends a single JSON params object ({"ticker",
    "provider", ...}); the server replies {"type": "started",
    "analysis_id": N}, then forwards every frame the runner emits
    (web/runner.py) until its end-of-stream sentinel. The runner persists
    results to the analyses table as it goes, so a dropped socket loses the
    live view but not the analysis — it's still in history when done.

    ``trade_date`` is always today (UTC), computed server-side below and
    overwritten unconditionally — not accepted from the client. Analyzing a
    past date gave inconsistent, partly-broken results here (some tools
    filter correctly, some ignore the date, news breaks for non-recent
    dates) and nothing on this path uses it for dedupe or checkpoint/resume,
    so there was never a real use for a client-chosen value.
    """
    # The http auth middleware doesn't see websocket scope, so gate here:
    # require a valid session cookie (or the internal-token header). The token
    # check must stay hmac.compare_digest — a plain `==` leaks the internal
    # token through a timing side-channel.
    token = ws.cookies.get(auth_app.COOKIE_NAME)
    internal = ws.headers.get("x-internal-token")
    expected = os.environ.get("INTERNAL_API_TOKEN")
    authed = bool(token and db.get_session(token)) or bool(
        internal and expected and hmac.compare_digest(internal, expected)
    )
    if not authed:
        await ws.close(code=4401)
        return
    await ws.accept()
    try:
        params = await ws.receive_json()
    except WebSocketDisconnect:
        return

    # Always analyze as of "today" server-side — same pattern as
    # portfolio_main.py's start_scan/start_spy_scan. Overwritten
    # unconditionally so this holds for any caller, not just the browser
    # form (which no longer sends trade_date at all).
    params["trade_date"] = datetime.utcnow().date().isoformat()

    required = ("ticker",)
    if any(not params.get(k) for k in required):
        await ws.send_json({"type": "error", "message": "missing ticker"})
        await ws.close()
        return

    try:
        prefs_payload = {k: v for k, v in params.items() if k != "trade_date"}
        db.save_preferences(prefs_payload)
    except Exception:
        pass

    try:
        analysis_id = db.create_analysis(params)
    except Exception as exc:
        await ws.send_json({"type": "error", "message": f"db init failed: {exc}"})
        await ws.close()
        return

    await ws.send_json({"type": "started", "analysis_id": analysis_id})

    # Register this run in the cross-container LLM activity registry so the
    # S&P 500 scanner yields worker slots to it (single-ticker priority).
    ticker_label = str(params.get("ticker", "")).strip().upper()
    activity_id = db.register_activity("single", ticker_label)

    def _worker() -> None:
        # Process-local cap: block until a slot frees if this api container is
        # already at the shared budget. The scanner sees us via llm_activity
        # the moment we registered above, so it has already begun yielding.
        with _SINGLE_SLOTS:
            run_analysis_sync(params, analysis_id, frames)

    # Bridge from the worker thread into this async loop; runner pushes None
    # as the end-of-stream sentinel. (Defined after _worker on purpose —
    # closures resolve names at call time, and _worker only runs below.)
    frames: queue.Queue = queue.Queue()
    producer = asyncio.create_task(asyncio.to_thread(_worker))

    loop = asyncio.get_running_loop()
    last_hb = 0.0
    try:
        while True:
            frame = await loop.run_in_executor(None, frames.get)
            if frame is None:
                break
            # Heartbeat the activity row at most every 10s so a long run keeps
            # its slot reservation fresh (stale rows are reclaimed after 120s).
            now = time.monotonic()
            if now - last_hb >= 10.0:
                last_hb = now
                try:
                    db.heartbeat_activity(activity_id)
                except Exception:
                    pass
            try:
                await ws.send_json(frame)
            except (WebSocketDisconnect, RuntimeError):
                break
    finally:
        try:
            await producer
        except Exception:
            pass
        try:
            db.clear_activity(activity_id)
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


# ---------- bus WebSocket bridge (/api/bus) ----------

# Poll cadence for the bus bridge. The server wakes once per interval for every
# live /api/bus WebSocket, so this unset default is the per-connection idle
# cost of an open dashboard tab. 5 s is the new floor; BUS_POLL_INTERVAL still
# overrides it. In tests set BUS_POLL_INTERVAL="0.05" to keep suites fast.
def _bus_poll_interval() -> float:
    try:
        return float(os.environ.get("BUS_POLL_INTERVAL", "5.0"))
    except (TypeError, ValueError):
        return 5.0


def _pick_latest_analysis_channel(channels: list[dict]) -> str | None:
    """Return the channel_id of the analysis-* channel with the highest numeric suffix.

    channels is the list returned by list_channels(): each entry has an 'id' key
    (the channel_id string) plus 'name' and 'member_count'.

    Returns None if no analysis-* channel exists.
    """
    best_id: str | None = None
    best_num: int = -1
    for ch in channels:
        cid = ch.get("id") or ""
        if not cid.startswith("analysis-"):
            continue
        suffix = cid[len("analysis-"):]
        try:
            num = int(suffix)
        except ValueError:
            continue
        if num > best_num:
            best_num = num
            best_id = cid
    return best_id


def _to_frame(row: dict, channel: str) -> dict:
    """Map a raw bus message row to the /api/bus wire frame.

    Field naming note: the frame envelope uses "type" for the frame kind
    ("bus_message").  The bus message's own type field (chat/result/instruction)
    is carried as "msg_type" to avoid clobbering the envelope key.
    """
    return {
        "type": "bus_message",
        "id": row.get("id"),
        "agent": row.get("from"),          # bus field is 'from' (Python reserved)
        "channel": channel,
        "msg_type": row.get("type"),       # chat | result | instruction | status ...
        "content": row.get("content"),
        "ts": row.get("created_at"),
        "thread_id": row.get("thread_id"),  # may be None — that's fine
    }


@app.websocket("/api/bus")
async def bus_ws(ws: WebSocket) -> None:
    """Stream switchboard bus messages to the dashboard.

    Auth gate is identical to /api/analyze: valid session cookie OR
    X-Internal-Token header matching INTERNAL_API_TOKEN env var (code 4401 on
    failure).

    Channel resolution order:
      1. ?channel= query param
      2. list_channels() → newest analysis-* by numeric suffix
      3. Poll until one appears (still serving pings)

    Live poll sends bus_message frames every BUS_POLL_INTERVAL seconds (default 5s).
    Client may send {"channel": "analysis-N"} text frames to switch channel.
    Bus failures send bus_status ok:false once per outage; recovery sends
    bus_status ok:true.  Bus failures never close the socket.
    """
    # ---- auth gate (identical to /api/analyze) ----
    token = ws.cookies.get(auth_app.COOKIE_NAME)
    internal = ws.headers.get("x-internal-token")
    expected = os.environ.get("INTERNAL_API_TOKEN")
    authed = bool(token and db.get_session(token)) or bool(
        internal and expected and hmac.compare_digest(internal, expected)
    )
    if not authed:
        await ws.close(code=4401)
        return

    await ws.accept()

    client = bus.get_reader()
    if client is None:
        await ws.send_json({"type": "bus_status", "ok": False, "reason": "bus not configured"})
        # Keep open — frontend shows a grey dot; just idle with periodic pings.
        try:
            while True:
                await asyncio.sleep(25)
                try:
                    await ws.send_json({"type": "ping"})
                except (WebSocketDisconnect, RuntimeError):
                    return
        except (WebSocketDisconnect, asyncio.CancelledError):
            return

    # ---- receive task: feed inbound client frames into a variable ----
    # We use a simple asyncio.Queue so the poll loop can non-blockingly check
    # for channel-switch requests without coupling to WebSocket.receive().
    incoming: asyncio.Queue[str | None] = asyncio.Queue()

    async def _reader_task() -> None:
        try:
            while True:
                data = await ws.receive_text()
                await incoming.put(data)
        except (WebSocketDisconnect, RuntimeError):
            await incoming.put(None)  # sentinel: client gone

    reader_task = asyncio.create_task(_reader_task())

    # ---- channel resolution from ?channel= query param ----
    channel: str | None = ws.query_params.get("channel") or None

    # ---- per-outage status tracking ----
    bus_ok: bool = True  # assume healthy until first failure
    waiting_announced: bool = False  # sent the "healthy but no channel yet" status?

    async def _send_bus_status(ok: bool, reason: str = "") -> None:
        nonlocal bus_ok
        frame: dict[str, Any] = {"type": "bus_status", "ok": ok}
        if not ok and reason:
            frame["reason"] = reason
        try:
            await ws.send_json(frame)
        except (WebSocketDisconnect, RuntimeError):
            pass
        bus_ok = ok

    cursor: int = 0
    last_ping = asyncio.get_running_loop().time()
    poll_interval = _bus_poll_interval()

    try:
        while True:
            # ---- channel discovery / wait loop ----
            if channel is None:
                # Try to pick a channel from the bus.
                try:
                    result = await asyncio.to_thread(client.list_channels)
                    ch_list = result.get("channels") or []
                    channel = _pick_latest_analysis_channel(ch_list)
                    if bus_ok is False:
                        await _send_bus_status(True)
                except Exception as exc:
                    if bus_ok:
                        await _send_bus_status(False, str(exc))
                    # No channel yet; wait with a ping then retry.
                    now = asyncio.get_running_loop().time()
                    if now - last_ping >= 25:
                        last_ping = now
                        try:
                            await ws.send_json({"type": "ping"})
                        except (WebSocketDisconnect, RuntimeError):
                            return
                    # Poll incoming for channel-switch before sleeping.
                    try:
                        msg = incoming.get_nowait()
                        if msg is None:
                            return
                        _maybe_channel = _parse_channel_switch(msg)
                        if _maybe_channel:
                            channel = _maybe_channel
                            cursor = 0
                            continue
                    except asyncio.QueueEmpty:
                        pass
                    await asyncio.sleep(5.0)
                    continue

                if channel is None:
                    # Bus is reachable but no analysis-* channel yet — announce
                    # once and idle until one appears.
                    if not waiting_announced:
                        try:
                            await ws.send_json({"type": "bus_status", "ok": True})
                        except (WebSocketDisconnect, RuntimeError):
                            return
                        waiting_announced = True
                    now = asyncio.get_running_loop().time()
                    if now - last_ping >= 25:
                        last_ping = now
                        try:
                            await ws.send_json({"type": "ping"})
                        except (WebSocketDisconnect, RuntimeError):
                            return
                    try:
                        msg = incoming.get_nowait()
                        if msg is None:
                            return
                        _maybe_channel = _parse_channel_switch(msg)
                        if _maybe_channel:
                            channel = _maybe_channel
                            cursor = 0
                            continue
                    except asyncio.QueueEmpty:
                        pass
                    await asyncio.sleep(5.0)
                    continue

            # ---- announce channel + backfill ----
            try:
                await ws.send_json({"type": "channel", "channel": channel, "name": channel})
            except (WebSocketDisconnect, RuntimeError):
                return

            # Backfill: history read with channel_id+since_id is cursor-free
            # (no read_cursors side-effect per bus.js getMessages).
            try:
                backfill = await asyncio.to_thread(
                    client.get_messages,
                    "dashboard-bridge",
                    channel_id=channel,
                    since_id=0,
                    limit=200,
                    peek=True,
                )
                if bus_ok is False:
                    await _send_bus_status(True)
                msgs = backfill.get("messages") or []
                frames = [_to_frame(r, channel) for r in msgs]
                try:
                    await ws.send_json({"type": "backfill", "messages": frames})
                except (WebSocketDisconnect, RuntimeError):
                    return
                if msgs:
                    cursor = max(r.get("id", 0) for r in msgs)
                else:
                    cursor = 0
            except Exception as exc:
                if bus_ok:
                    await _send_bus_status(False, str(exc))
                # Fall through to the live poll loop with cursor as-is: once the
                # bus recovers, history arrives as individual bus_message frames
                # (no backfill envelope) — acceptable degraded path.

            # ---- live poll loop ----
            while True:
                # Check for client text frames (channel-switch or disconnect).
                try:
                    raw = incoming.get_nowait()
                    if raw is None:
                        return  # client disconnected
                    new_channel = _parse_channel_switch(raw)
                    if new_channel and new_channel != channel:
                        channel = new_channel
                        cursor = 0
                        break  # re-announce + re-backfill
                except asyncio.QueueEmpty:
                    pass

                # Poll for new messages.
                try:
                    result = await asyncio.to_thread(
                        client.get_messages,
                        "dashboard-bridge",
                        channel_id=channel,
                        since_id=cursor,
                        limit=250,
                        peek=True,
                    )
                    if bus_ok is False:
                        await _send_bus_status(True)
                    new_msgs = result.get("messages") or []
                    for row in new_msgs:
                        frame = _to_frame(row, channel)
                        try:
                            await ws.send_json(frame)
                        except (WebSocketDisconnect, RuntimeError):
                            return
                        if row.get("id", 0) > cursor:
                            cursor = row["id"]
                except Exception as exc:
                    if bus_ok:
                        await _send_bus_status(False, str(exc))
                    # Back off during outage before retrying.
                    await asyncio.sleep(5.0)
                    continue

                # Periodic ping.
                now = asyncio.get_running_loop().time()
                if now - last_ping >= 25:
                    last_ping = now
                    try:
                        await ws.send_json({"type": "ping"})
                    except (WebSocketDisconnect, RuntimeError):
                        return

                await asyncio.sleep(poll_interval)

                # Drain incoming again in case a channel-switch/disconnect
                # frame arrives during the sleep window, so we act promptly
                # instead of waiting for the next full poll iteration.
                try:
                    raw = incoming.get_nowait()
                    if raw is None:
                        return  # client disconnected
                    new_channel = _parse_channel_switch(raw)
                    if new_channel and new_channel != channel:
                        channel = new_channel
                        cursor = 0
                        break  # re-announce + re-backfill
                except asyncio.QueueEmpty:
                    pass

    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):
            pass


def _parse_channel_switch(raw: str) -> str | None:
    """Parse a client text frame for a channel-switch request.

    Expected: {"channel": "analysis-N"}.  Invalid JSON → None.  Missing or
    non-string channel key → None.
    """
    try:
        data = json.loads(raw)
    except Exception:
        return None
    ch = data.get("channel") if isinstance(data, dict) else None
    return ch if isinstance(ch, str) and ch else None
