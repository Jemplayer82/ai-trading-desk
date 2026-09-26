"""Browser-side S&P tab shared-research tests.

These tests exercise ``loadResearchTime()`` / ``saveResearchTime()``,
``loadResearchStatus()`` / ``runResearchNow()`` and the Allocate-now gating in
``triggerSpyScan()`` from ``web/static/spy.js`` (plus the matching
``triggerOptionsScan()`` in ``web/static/options.js``) using the Node.js vm harness in
``tests/jsvm.py``.
"""

import json
from pathlib import Path

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
        if (globalThis.__researchPostStatus) {
            const st = globalThis.__researchPostStatus;
            return Promise.resolve({
                ok: false,
                status: st,
                json: function () { return Promise.resolve(globalThis.__researchPostResponse); }
            });
        }
        return __ok(globalThis.__researchPostResponse);
    }
    if (url === "/api/spy-scan" && globalThis.__scanPostResponse) {
        const resp = globalThis.__scanPostResponse;
        return Promise.resolve({
            ok: resp.ok,
            status: resp.status,
            json: function () { return Promise.resolve(resp.body); }
        });
    }
    return __ok({accounts: [], scans: []});
};
"""

# options.js is tier-4-only (scripts/make_tier.py deletes it below tier 4) while
# this file ships at tier 3, so the options cases skip when it is absent.
_HAS_OPTIONS_JS = (Path(__file__).resolve().parent.parent / "web" / "static" / "options.js").exists()
needs_options_js = pytest.mark.skipif(
    not _HAS_OPTIONS_JS, reason="options.js isn't physically present below tier 4"
)

OPTIONS_BOOTSTRAP = BOOTSTRAP.replace('url === "/api/spy-scan"', 'url === "/api/options-scan"')


def _run(script):
    return run_js(sources=["utils.js", "spy.js"], bootstrap=BOOTSTRAP, script=script)


def _run_options(script):
    return run_js(sources=["utils.js", "options.js"], bootstrap=OPTIONS_BOOTSTRAP, script=script)


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


def test_run_research_now_409_detail_survives_status_refresh():
    result = _run(
        "globalThis.__researchPostStatus = 409;\n"
        "globalThis.__researchPostResponse = {detail: 'not a trading day <2026-09-26>'};\n"
        "globalThis.__todayResponse = {trade_date: '2026-09-26', scan: null, attempts: 0};\n"
        "return (async () => {\n"
        "    const msg = await runResearchNow();\n"
        "    return {\n"
        "        msg: msg,\n"
        "        html: document.getElementById('spy-research-status').innerHTML,\n"
        "        refreshed: __calls.some((c) => c.url === '/api/research-scans/today'),\n"
        "    };\n"
        "})();"
    )
    assert result["msg"] == "not a trading day <2026-09-26>"
    assert result["refreshed"] is True
    html = result["html"]
    assert "not a trading day &lt;2026-09-26&gt;" in html
    assert "<2026-09-26>" not in html
    assert "No research yet today" in html
    assert "runResearchNow()" in html


def test_run_research_now_5xx_without_detail_keeps_http_status():
    result = _run(
        "globalThis.__researchPostStatus = 503;\n"
        "globalThis.__researchPostResponse = {};\n"
        "return (async () => {\n"
        "    await runResearchNow();\n"
        "    return document.getElementById('spy-research-status').innerHTML;\n"
        "})();"
    )
    assert "Error: HTTP 503" in result
    assert "Run research" in result


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


NOT_TRADING_DETAIL = "2026-09-26 is not an NYSE trading day — allocation not queued"

# (runner, account global, POST url, status element id, "existing" wording)
TRIGGERS = {
    "spy": (_run, "activePaperAccountId", "/api/spy-scan", "spy-scan-status", "already running"),
    "options": (
        _run_options, "activeOptAccountId", "/api/options-scan", "options-scan-status", "already exists today"
    ),
}


def _trigger(kind, response):
    runner, account_var, post_url, status_id, _ = TRIGGERS[kind]
    fn = "triggerSpyScan" if kind == "spy" else "triggerOptionsScan"
    # The scan viewer render needs a real DOM; record which scan would open.
    loader = "loadSpyScan" if kind == "spy" else "loadOptionsScan"
    return runner(
        "globalThis.__scanPostResponse = " + json.dumps(response) + ";\n"
        "globalThis.__opened = [];\n"
        + loader + " = function (id) { __opened.push(id); };\n"
        "return (async () => {\n"
        "    " + account_var + " = 5;\n"
        "    await " + fn + "();\n"
        "    const posts = __calls.filter((c) => c.url === " + json.dumps(post_url) + ");\n"
        "    const post = posts[0];\n"
        "    return {\n"
        "        posts: posts.length,\n"
        "        method: post && post.options ? post.options.method : null,\n"
        "        body: post && post.options && post.options.body ? JSON.parse(post.options.body) : null,\n"
        "        status: document.getElementById(" + json.dumps(status_id) + ").textContent,\n"
        "        urls: __calls.map((c) => c.url),\n"
        "        opened: __opened,\n"
        "    };\n"
        "})();"
    )


@pytest.mark.parametrize(
    "kind",
    [pytest.param(k, marks=needs_options_js) if k == "options" else k for k in sorted(TRIGGERS)],
)
def test_allocate_now_409_shows_route_detail(kind):
    result = _trigger(kind, {"ok": False, "status": 409, "body": {"detail": NOT_TRADING_DETAIL}})
    assert result["posts"] == 1
    assert result["method"] == "POST"
    assert result["body"] == {"account_id": 5}
    assert result["status"] == NOT_TRADING_DETAIL
    # The error path returns before refreshing history or opening a scan.
    assert result["urls"] == [TRIGGERS[kind][2]]
    assert result["opened"] == []


@pytest.mark.parametrize(
    "kind",
    [pytest.param(k, marks=needs_options_js) if k == "options" else k for k in sorted(TRIGGERS)],
)
def test_allocate_now_non_ok_without_detail_shows_http_status(kind):
    result = _trigger(kind, {"ok": False, "status": 500, "body": {}})
    assert result["body"] == {"account_id": 5}
    assert result["status"].startswith("Error")
    assert "500" in result["status"]


@pytest.mark.parametrize(
    "kind",
    [pytest.param(k, marks=needs_options_js) if k == "options" else k for k in sorted(TRIGGERS)],
)
def test_allocate_now_new_allocation_reports_queued(kind):
    result = _trigger(
        kind, {"ok": True, "status": 200, "body": {"scan_id": 41, "status": "pending", "new": True}}
    )
    assert result["posts"] == 1
    assert result["method"] == "POST"
    assert result["body"] == {"account_id": 5}
    assert result["status"] == (
        "Allocation #41 queued — waits for today's research and the 09:35 ET open"
    )
    assert result["opened"] == [41]


@pytest.mark.parametrize(
    "kind",
    [pytest.param(k, marks=needs_options_js) if k == "options" else k for k in sorted(TRIGGERS)],
)
def test_allocate_now_existing_allocation_is_not_labelled_queued(kind):
    existing = TRIGGERS[kind][4]
    result = _trigger(
        kind, {"ok": True, "status": 200, "body": {"scan_id": 41, "status": "running", "new": False}}
    )
    assert result["body"] == {"account_id": 5}
    assert result["status"] == "Allocation #41 " + existing
    assert "queued" not in result["status"]


@needs_options_js
def test_trigger_options_scan_without_account_makes_no_fetch():
    result = _run_options(
        "return (async () => {\n"
        "    activeOptAccountId = null;\n"
        "    await triggerOptionsScan();\n"
        "    return {\n"
        "        calls: __calls.length,\n"
        "        status: document.getElementById('options-scan-status').textContent,\n"
        "    };\n"
        "})();"
    )
    assert result["calls"] == 0
    assert result["status"].startswith("Create an options paper account first")
