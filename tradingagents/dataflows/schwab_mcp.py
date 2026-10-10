"""Minimal MCP/JSON-RPC client for the user's Schwab MCP server (streamable HTTP).

The app reaches Schwab through the Schwab MCP server (which holds the
authenticated Schwab session) instead of the app's own OAuth. The server is
*stateless*: a single POST of a ``tools/call`` returns one SSE frame whose
``result.content[0].text`` is the tool's JSON payload — no session id or
``initialize`` handshake is required (verified against schwab-mcp 1.0.0).

Market-data + account READS only. No order/trade tools are exposed here, which
keeps the "paper only — no real orders" guarantee enforced in code.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any

import httpx

log = logging.getLogger(__name__)

_DEFAULT_URL = "http://100.112.40.124:3105/mcp"
_id_lock = threading.Lock()
_req_id = 0


def _next_id() -> int:
    global _req_id
    with _id_lock:
        _req_id += 1
        return _req_id


def mcp_url() -> str:
    return (os.environ.get("SCHWAB_MCP_URL") or _DEFAULT_URL).strip() or _DEFAULT_URL


_KEY_ENV = "CLEO_SCHWAB_MCP_TOKEN"


def _lan_host(url: str) -> bool:
    """True only for private/loopback addresses and single-label docker service names.

    SCHWAB_MCP_URL can be edited in the settings UI, so the key must never be sent to an arbitrary host.
    """
    import ipaddress
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").strip().lower()
    except ValueError:
        return False
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # docker service name such as mcp-schwab: letters first, so decimal/hex IP spellings
        # (134744072, 0x08080808) that the OS resolver would turn into a public address are rejected
        return host == "localhost" or bool(re.fullmatch(r"[a-z][a-z0-9-]*", host))
    # private, loopback, link-local and the 100.64.0.0/10 Tailscale/CGNAT overlay (the app's default URL lives there)
    return ip.is_private or ip.is_loopback or ip.is_link_local or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"))


def _scrub(text: object) -> str:
    """Server-supplied error text with the key removed (a door that echoes a rejected header)."""
    out = str(text)
    key = (os.environ.get(_KEY_ENV) or "").strip()
    return out.replace(key, "[redacted]") if key else out


def _auth_headers() -> dict[str, str]:
    """Bearer key for Cleo's Schwab MCP door, read from the environment on every call.

    The value comes only from the stack environment variable CLEO_SCHWAB_MCP_TOKEN: never from code,
    never logged, never put in an error message. Unset or blank means no header (the pre-key behaviour,
    so a deploy of this code before the key is set changes nothing).
    """
    key = (os.environ.get(_KEY_ENV) or "").strip()
    if not key:
        return {}
    if not _lan_host(mcp_url()):
        log.warning("[schwab_mcp] %s is set but SCHWAB_MCP_URL is not a LAN/docker host: key NOT sent", _KEY_ENV)
        return {}
    return {"Authorization": f"Bearer {key}"}


def schwab_enabled() -> bool:
    """Master switch for ALL Schwab features (account + market data).

    When off (SCHWAB_ENABLED=0), the app makes zero Schwab calls and the UI
    hides every Schwab surface — single-ticker reports and the S&P 500 paper
    builder still run fully on yfinance. Default on.
    """
    return (os.environ.get("SCHWAB_ENABLED", "1").strip().lower()) not in ("0", "false", "no")


def market_data_enabled() -> bool:
    """Whether to route market-data (quotes/OHLCV) through Schwab.

    Requires the master Schwab switch on. Account-sync endpoints gate on
    schwab_enabled() directly. SCHWAB_MARKET_DATA is a finer sub-toggle.
    """
    if not schwab_enabled():
        return False
    return (os.environ.get("SCHWAB_MARKET_DATA", "1").strip().lower()) not in ("0", "false", "no", "")


def _parse_frame(text: str) -> dict[str, Any] | None:
    """Extract the JSON-RPC object from an SSE ('data: {...}') or plain-JSON body."""
    body = (text or "").strip()
    if not body:
        return None
    if body[0] == "{":
        try:
            return json.loads(body)
        except ValueError:
            return None
    obj: dict[str, Any] | None = None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
    return obj


def call_tool(name: str, arguments: dict[str, Any], timeout: float = 30.0) -> Any:
    """Call a Schwab MCP tool; return its parsed JSON payload, or None on any error."""
    req = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **_auth_headers(),
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(mcp_url(), json=req, headers=headers)
            resp.raise_for_status()
            frame = _parse_frame(resp.text)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            log.warning("[schwab_mcp] %s refused (HTTP %s): the Schwab MCP key (%s) is missing or rejected",
                        name, exc.response.status_code, _KEY_ENV)
        else:
            log.warning("[schwab_mcp] %s call failed: HTTP %s", name, exc.response.status_code)
        return None
    except Exception as exc:  # noqa: BLE001 — callers fall back to yfinance
        log.warning("[schwab_mcp] %s call failed: %s", name, exc)
        return None

    if not frame:
        log.warning("[schwab_mcp] %s: empty/unparseable response", name)
        return None
    if frame.get("error"):
        log.warning("[schwab_mcp] %s error: %s", name, _scrub(frame["error"]))
        return None

    result = frame.get("result") or {}
    if result.get("isError"):
        # The MCP tool ran but the upstream Schwab call failed — most commonly
        # the MCP server's Schwab session is unauthorized / its token expired
        # (re-auth at https://schwab.txferguson.net/auth). The human-readable
        # reason is in the text content. Surface it in logs and treat as "no
        # data" so callers fall back (market data → yfinance) or show
        # "not connected" rather than leaking the raw error string downstream.
        detail = ""
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                detail = block.get("text") or ""
                break
        log.warning("[schwab_mcp] %s tool error: %s", name, _scrub(detail) or "(no detail)")
        return None
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            txt = block.get("text") or ""
            try:
                return json.loads(txt)
            except ValueError:
                return txt
    if "structuredContent" in result:
        return result["structuredContent"]
    return None


# ── Typed wrappers (market data + account reads only) ────────────────────────

def get_quotes(symbols: list[str]) -> dict[str, Any] | None:
    """Real-time quotes keyed by symbol. getQuotes takes a comma-separated string."""
    syms = [s for s in (symbols or []) if s]
    if not syms:
        return None
    data = call_tool("getQuotes", {"symbols": ",".join(syms)})
    return data if isinstance(data, dict) else None


def get_price_history(
    symbol: str,
    period_type: str = "year",
    period: int = 5,
    frequency_type: str = "daily",
    frequency: int = 1,
) -> dict[str, Any] | None:
    """OHLCV candles for one symbol. Returns the Schwab priceHistory payload
    ({'candles': [{open,high,low,close,volume,datetime(ms)}], ...}) or None."""
    data = call_tool("getPriceHistory", {
        "symbol": symbol,
        "periodType": period_type,
        "period": period,
        "frequencyType": frequency_type,
        "frequency": frequency,
    })
    return data if isinstance(data, dict) else None


def get_account_numbers() -> list[dict[str, Any]] | None:
    data = call_tool("getAccountNumbers", {})
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("accounts") or data.get("accountNumbers")
    return None


def get_accounts(fields: str = "positions") -> list[dict[str, Any]] | None:
    data = call_tool("getAccounts", {"fields": fields})
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("accounts") or [data]
    return None


def quote_price(quote_obj: dict[str, Any]) -> float | None:
    """Pull the best current price from one symbol's quote object."""
    if not isinstance(quote_obj, dict):
        return None
    q = quote_obj.get("quote") or {}
    for key in ("lastPrice", "mark", "closePrice"):
        v = q.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


# ── Option chains (market data only — still no order/trade tools) ────────────

def get_option_chain(
    symbol: str,
    contract_type: str = "ALL",
    strike_count: int = 20,
    from_date: str | None = None,
    to_date: str | None = None,
    include_underlying_quote: bool = True,
) -> dict[str, Any] | None:
    """Option chain for one underlying via getOptionChain.

    Returns the Schwab chain payload ({'callExpDateMap'/'putExpDateMap':
    {"YYYY-MM-DD:DTE": {"<strike>": [contract]}}, 'underlyingPrice', ...}) or
    None. contract_type: CALL | PUT | ALL. strike_count bounds strikes around
    ATM; from_date/to_date (YYYY-MM-DD) bound expirations.
    """
    args: dict[str, Any] = {
        "symbol": symbol,
        "contractType": contract_type,
        "strikeCount": strike_count,
        "includeUnderlyingQuote": include_underlying_quote,
    }
    if from_date:
        args["fromDate"] = from_date
    if to_date:
        args["toDate"] = to_date
    data = call_tool("getOptionChain", args, timeout=60.0)
    return data if isinstance(data, dict) else None


def get_option_expirations(symbol: str) -> list[dict[str, Any]] | None:
    """Expiration list for one underlying via getOptionExpirationChain."""
    data = call_tool("getOptionExpirationChain", {"symbol": symbol})
    if isinstance(data, dict):
        exp = data.get("expirationList")
        return exp if isinstance(exp, list) else None
    if isinstance(data, list):
        return data
    return None


def option_quote_price(quote_obj: dict[str, Any]) -> float | None:
    """Best current price from one OPTION quote object.

    Order differs from quote_price() deliberately: for illiquid contracts
    lastPrice can be a days-old trade, so prefer mark, then a live mid, and
    only then last/close.
    """
    if not isinstance(quote_obj, dict):
        return None
    q = quote_obj.get("quote") or {}
    mark = q.get("mark")
    if isinstance(mark, (int, float)) and mark > 0:
        return float(mark)
    bid, ask = q.get("bidPrice"), q.get("askPrice")
    if (isinstance(bid, (int, float)) and isinstance(ask, (int, float))
            and bid > 0 and ask >= bid):
        return round((float(bid) + float(ask)) / 2, 4)
    for key in ("lastPrice", "closePrice"):
        v = q.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None
