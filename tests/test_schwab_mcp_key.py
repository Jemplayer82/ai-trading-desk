"""CLEO_SCHWAB_MCP_TOKEN: sent as a Bearer header to Cleo's Schwab MCP door, never logged, optional."""
import json
import logging
import re
from pathlib import Path

import httpx
import pytest

from tradingagents.dataflows import schwab_mcp

KEY = "sekret-test-key-123"


class Recorder:
    def __init__(self, status=200, body=None):
        self.requests = []
        self.status = status
        self.body = body if body is not None else json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps([{"n": 1}])}]}})

    def transport(self):
        def handler(request):
            self.requests.append(request)
            return httpx.Response(self.status, text=self.body, request=request)
        return httpx.MockTransport(handler)


@pytest.fixture
def patched(monkeypatch):
    def install(rec):
        real = httpx.Client
        monkeypatch.setattr(schwab_mcp.httpx, "Client", lambda **kw: real(transport=rec.transport(), **kw))
    return install


def test_key_is_sent_as_bearer_header(monkeypatch, patched):
    monkeypatch.setenv("CLEO_SCHWAB_MCP_TOKEN", KEY)
    rec = Recorder(); patched(rec)
    assert schwab_mcp.call_tool("getAccounts", {}) == [{"n": 1}]
    assert rec.requests[0].headers["authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_no_header_when_unset_or_blank(monkeypatch, patched, value):
    monkeypatch.delenv("CLEO_SCHWAB_MCP_TOKEN", raising=False)
    if value is not None:
        monkeypatch.setenv("CLEO_SCHWAB_MCP_TOKEN", value)
    rec = Recorder(); patched(rec)
    schwab_mcp.call_tool("getAccounts", {})
    assert "authorization" not in rec.requests[0].headers


def test_key_is_trimmed_and_read_on_every_call(monkeypatch, patched):
    monkeypatch.setenv("CLEO_SCHWAB_MCP_TOKEN", f"  {KEY}\n")
    rec = Recorder(); patched(rec)
    schwab_mcp.call_tool("getAccounts", {})
    monkeypatch.setenv("CLEO_SCHWAB_MCP_TOKEN", "rotated-key")
    schwab_mcp.call_tool("getAccounts", {})
    assert rec.requests[0].headers["authorization"] == f"Bearer {KEY}"
    assert rec.requests[1].headers["authorization"] == "Bearer rotated-key"


@pytest.mark.parametrize("status", [401, 403, 500])
def test_key_never_appears_in_logs_on_refusal_or_error(monkeypatch, patched, caplog, status):
    monkeypatch.setenv("CLEO_SCHWAB_MCP_TOKEN", KEY)
    rec = Recorder(status=status, body="denied"); patched(rec)
    with caplog.at_level(logging.DEBUG):
        assert schwab_mcp.call_tool("getAccounts", {}) is None
    assert KEY not in caplog.text and "Bearer" not in caplog.text
    if status in (401, 403):
        assert "CLEO_SCHWAB_MCP_TOKEN" in caplog.text and str(status) in caplog.text


def test_key_never_logged_on_connection_error(monkeypatch, caplog):
    monkeypatch.setenv("CLEO_SCHWAB_MCP_TOKEN", KEY)

    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)
    real = httpx.Client
    monkeypatch.setattr(schwab_mcp.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(boom), **kw))
    with caplog.at_level(logging.DEBUG):
        assert schwab_mcp.call_tool("getAccounts", {}) is None
    assert KEY not in caplog.text


def test_compose_passes_the_key_to_every_service_that_has_the_mcp_url():
    text = Path(__file__).resolve().parents[1].joinpath("docker-compose.yml").read_text()
    services = re.split(r"\n  (?=[A-Za-z0-9_-]+:\n)", text)
    holders = [s for s in services if "SCHWAB_MCP_URL:" in s]
    assert len(holders) >= 4
    for service in holders:
        assert "CLEO_SCHWAB_MCP_TOKEN: ${CLEO_SCHWAB_MCP_TOKEN:-}" in service
    assert KEY not in text


def test_check_script_prints_no_secret_and_no_account_data(monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "check_schwab_mcp_key", Path(__file__).resolve().parents[1] / "scripts" / "check_schwab_mcp_key.py")
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    monkeypatch.setenv("CLEO_SCHWAB_MCP_TOKEN", KEY)
    monkeypatch.setenv("SCHWAB_MCP_URL", "http://192.168.7.50:23105/mcp")
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [
            {"type": "text", "text": json.dumps([{"accountNumber": "99999999", "balance": 123456.78}])}]}})
        return httpx.Response(200, text=body, request=request)
    assert check.run(transport=httpx.MockTransport(handler)) == 0
    out = capsys.readouterr().out
    assert KEY not in out and "99999999" not in out and "123456" not in out and "Bearer" not in out
    assert "key header: attached" in out and "1 account(s)" in out and seen == [f"Bearer {KEY}"]
    assert check.run(no_key=True, transport=httpx.MockTransport(lambda r: httpx.Response(401, text="no", request=r))) == 1
    assert "refused" in capsys.readouterr().out


@pytest.mark.parametrize("url,sent", [
    ("http://192.168.7.50:3105/mcp", True), ("http://192.168.1.19:3105/mcp", True),
    ("http://10.0.0.5:3105/mcp", True), ("http://localhost:3105/mcp", True), ("http://127.0.0.1:3105/mcp", True),
    ("http://mcp-schwab:3105/mcp", True),
    ("https://evil.example.com/mcp", False), ("http://8.8.8.8:3105/mcp", False),
    ("http://100.112.40.124:3105/mcp", True), ("not a url", False), ("", True),
])
def test_key_only_goes_to_lan_or_docker_hosts(monkeypatch, patched, url, sent):
    monkeypatch.setenv("CLEO_SCHWAB_MCP_TOKEN", KEY)
    monkeypatch.setenv("SCHWAB_MCP_URL", url)
    headers = schwab_mcp._auth_headers()
    assert ("Authorization" in headers) == (sent if url else sent)
