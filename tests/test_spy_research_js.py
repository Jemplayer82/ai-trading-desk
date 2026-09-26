"""Browser-side S&P tab shared-research tests.

These tests exercise ``loadResearchTime()`` / ``saveResearchTime()``,
``loadResearchStatus()`` / ``runResearchNow()`` and the Allocate-now gating in
``triggerSpyScan()`` from ``web/static/spy.js`` using the Node.js vm harness in
``tests/jsvm.py``.
"""

import json

import pytest

from tests.jsvm import run_js

pytestmark = pytest.mark.unit


BOOTSTRAP = """
globalThis.__calls = [];
globalThis.alert = function () {};
globalThis.confirm = function () { return true; };
globalThis.renderScanQueue = function () {};
globalThis.__settingsResponse = {registry: [], custom: []};
globalThis.__settingsReject = false;
globalThis.__todayResponse = {trade_date: '2026-09-29', scan: null, attempts: 0};
globalThis.__researchPostResponse = {scan_id: 7, status: 'pending', new: true, trade_date: '2026-09-29'};
function __ok(body) {
    return Promise.resolve({
        ok: true,
        status: 200,
        json: function () { return Promise.resolve(body); }
    });
}
globalThis.fetch = function (url, options) {
    __calls.push({ url: url, options: options || null });
    if (url === "/api/settings") {
        if (globalThis.__settingsReject) {
            return Promise.reject(new Error("settings outage"));
        }
        return __ok(globalThis.__settingsResponse);
    }
    if (url === "/api/settings/SCHEDULE_RESEARCH_TIME") {
        return __ok({status: "saved"});
    }
    if (url === "/api/research-scans/today") {
        return __ok(globalThis.__todayResponse);
    }
    if (url === "/api/research-scan") {
        return __ok(globalThis.__researchPostResponse);
    }
    return __ok({accounts: [], scans: []});
};
"""


def _run(script):
    return run_js(sources=["utils.js", "spy.js"], bootstrap=BOOTSTRAP, script=script)


def test_load_research_time_sets_saved_value():
    payload = {
        "registry": [{"key": "SCHEDULE_RESEARCH_TIME", "masked": "01:15", "has_value": True}],
        "custom": [],
    }
    result = _run(
        "globalThis.__settingsResponse = " + json.dumps(payload) + ";\n"
        "return (async () => {\n"
        "    await loadResearchTime();\n"
        "    return document.getElementById('spy-research-time').value;\n"
        "})();"
    )
    assert result == "01:15"


def test_load_research_time_defaults_when_key_absent():
    result = _run(
        "globalThis.__settingsResponse = {registry: [], custom: []};\n"
        "return (async () => {\n"
        "    await loadResearchTime();\n"
        "    return document.getElementById('spy-research-time').value;\n"
        "})();"
    )
    assert result == "00:00"


def test_load_research_time_survives_fetch_failure():
    result = _run(
        "globalThis.__settingsReject = true;\n"
        "return (async () => {\n"
        "    let threw = false;\n"
        "    try { await loadResearchTime(); } catch (e) { threw = true; }\n"
        "    return { threw: threw, calls: __calls.length };\n"
        "})();"
    )
    assert result["threw"] is False
    assert result["calls"] == 1


def test_save_research_time_puts_value_and_reports_success():
    result = _run(
        "return (async () => {\n"
        "    document.getElementById('spy-research-time').value = '01:30';\n"
        "    await saveResearchTime();\n"
        "    const call = __calls[0];\n"
        "    return {\n"
        "        calls: __calls.length,\n"
        "        url: call ? call.url : null,\n"
        "        method: call && call.options ? call.options.method : null,\n"
        "        body: call && call.options && call.options.body ? JSON.parse(call.options.body) : null,\n"
        "        status: document.getElementById('spy-research-time-status').textContent,\n"
        "    };\n"
        "})();"
    )
    assert result["calls"] == 1
    assert result["url"] == "/api/settings/SCHEDULE_RESEARCH_TIME"
    assert result["method"] == "PUT"
    assert result["body"] == {"value": "01:30"}
    assert "saved" in result["status"]


def test_save_research_time_rejects_bad_time_without_fetch():
    result = _run(
        "return (async () => {\n"
        "    document.getElementById('spy-research-time').value = '25:00';\n"
        "    await saveResearchTime();\n"
        "    return {\n"
        "        calls: __calls.length,\n"
        "        status: document.getElementById('spy-research-time-status').textContent,\n"
        "    };\n"
        "})();"
    )
    assert result["calls"] == 0
    assert "HH:MM" in result["status"]


def test_load_research_status_renders_running_scan():
    today = {
        "trade_date": "2026-09-29",
        "scan": {
            "id": 12,
            "status": "running_deep",
            "quick_count": 151,
            "quick_total": 151,
            "deep_count": 20,
            "deep_total": 51,
            "deep_reused_count": 3,
        },
        "attempts": 1,
    }
    result = _run(
        "globalThis.__todayResponse = " + json.dumps(today) + ";\n"
        "return (async () => {\n"
        "    await loadResearchStatus();\n"
        "    return document.getElementById('spy-research-status').innerHTML;\n"
        "})();"
    )
    for needle in ("#12", "running_deep", "151/151", "20/51", "3 reused"):
        assert needle in result


def test_load_research_status_without_scan_offers_run_button():
    result = _run(
        "globalThis.__todayResponse = {trade_date: '2026-09-29', scan: null, attempts: 0};\n"
        "return (async () => {\n"
        "    await loadResearchStatus();\n"
        "    return document.getElementById('spy-research-status').innerHTML;\n"
        "})();"
    )
    assert "No research yet today" in result
    assert "Run research" in result
    assert "runResearchNow()" in result


def test_run_research_now_posts_research_scan():
    result = _run(
        "return (async () => {\n"
        "    await runResearchNow();\n"
        "    const post = __calls.find((c) => c.url === '/api/research-scan');\n"
        "    return {\n"
        "        method: post && post.options ? post.options.method : null,\n"
        "        body: post && post.options ? post.options.body : null,\n"
        "        refreshed: __calls.some((c) => c.url === '/api/research-scans/today'),\n"
        "    };\n"
        "})();"
    )
    assert result["method"] == "POST"
    assert json.loads(result["body"]) == {}
    assert result["refreshed"] is True


def test_trigger_spy_scan_without_account_makes_no_fetch():
    result = _run(
        "return (async () => {\n"
        "    activePaperAccountId = null;\n"
        "    await triggerSpyScan();\n"
        "    return {\n"
        "        calls: __calls.length,\n"
        "        status: document.getElementById('spy-scan-status').textContent,\n"
        "    };\n"
        "})();"
    )
    assert result["calls"] == 0
    assert result["status"].startswith("Select or create an S&P paper account first")
