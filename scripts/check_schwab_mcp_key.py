#!/usr/bin/env python3
"""Check that the desk's Schwab MCP key works against a door, WITHOUT printing any secret.

Run inside a desk container (where CLEO_SCHWAB_MCP_TOKEN is set), e.g. against the test door:

    SCHWAB_MCP_URL=http://192.168.7.50:23105/mcp python scripts/check_schwab_mcp_key.py
    SCHWAB_MCP_URL=http://192.168.7.50:23105/mcp python scripts/check_schwab_mcp_key.py --no-key

It sends a read-only getAccounts call and prints only: whether a key header was attached, the HTTP status,
and how many accounts came back. It never prints the key, headers, account numbers or balances.
--no-key sends the same request without the header (a door that requires the key should answer 401).
"""
from __future__ import annotations

import argparse
import sys

import httpx

from tradingagents.dataflows import schwab_mcp


def run(no_key: bool = False, *, transport=None) -> int:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    attached = False
    if not no_key:
        extra = schwab_mcp._auth_headers()
        headers.update(extra)
        attached = bool(extra)
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "getAccounts", "arguments": {"fields": ""}}}
    print(f"url: {schwab_mcp.mcp_url()}")
    print(f"key header: {'attached' if attached else 'NOT attached'}")
    try:
        with httpx.Client(timeout=30.0, transport=transport) as client:
            response = client.post(schwab_mcp.mcp_url(), json=request, headers=headers)
    except Exception as exc:  # noqa: BLE001 - report the class only, never request details
        print(f"result: request failed ({type(exc).__name__})")
        return 2
    print(f"http status: {response.status_code}")
    if response.status_code in (401, 403):
        print("result: refused (key missing or rejected)")
        return 1
    if response.status_code != 200:
        print("result: unexpected status")
        return 2
    frame = schwab_mcp._parse_frame(response.text) or {}
    result = frame.get("result") or {}
    if frame.get("error") or result.get("isError"):
        print("result: tool error (door accepted the key; Schwab login or upstream problem)")
        return 3
    payload = None
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                import json
                payload = json.loads(block.get("text") or "")
            except ValueError:
                payload = None
            break
    count = len(payload) if isinstance(payload, list) else (1 if payload else 0)
    print(f"result: ok, {count} account(s) returned")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-key", action="store_true", help="send the request without the key header")
    return run(parser.parse_args().no_key)


if __name__ == "__main__":
    sys.exit(main())
