"""Browser-side scan-activity banner tests.

These tests exercise ``pollScanActivity()`` from ``web/static/portfolio.js``
using the Node.js vm harness in ``tests/jsvm.py``.
"""

import json
from pathlib import Path

import pytest

from tests.jsvm import run_js

ROOT = Path(__file__).resolve().parents[1]

# Fetch stub installed once before the tested sources are loaded.  Tests can
# reassign ``globalThis.__fetchPayload`` between calls to ``pollScanActivity()``
# in the same VM context.
BOOTSTRAP = """
globalThis.__fetchPayload = null;
globalThis.fetch = function (url) {
    __fetches.push(url);
    return Promise.resolve({
        ok: true,
        status: 200,
        json: function () { return Promise.resolve(globalThis.__fetchPayload); }
    });
};
"""

pytestmark = pytest.mark.unit


PORTFOLIO_PROGRESS = {
    "running": {
        "scan_type": "portfolio",
        "kind": "portfolio",
        "status": "running",
        "completed": 3,
        "total": 10,
    },
    "queued": [],
    "waiting": [],
}

SPY_PROGRESS = {
    "running": {
        "scan_type": "spy",
        "kind": "spy",
        "status": "running",
        "completed": 1,
        "total": 5,
    },
    "queued": [],
    "waiting": [],
}

OPTIONS_RUN = {
    "running": {
        "scan_type": "options_run",
        "kind": "options_run",
        "status": "running",
        "completed": 0,
        "total": 1,
    },
    "queued": [],
    "waiting": [],
}

PENDING_SCAN = {
    "running": {
        "scan_type": "portfolio",
        "kind": "portfolio",
        "status": "pending",
    },
    "queued": [],
    "waiting": [],
}

NO_RUNNING = {"running": None, "queued": [], "waiting": []}

WAITING_OPTIONS = {
    "running": None,
    "queued": [],
    "waiting": [
        {"scan_type": "options", "kind": "options", "status": "waiting"}
    ],
}


def _run_poll(payload, first_payload=None, failure=False):
    """Run ``pollScanActivity()`` and return the final banner state.

    If ``first_payload`` is provided, ``pollScanActivity()`` is called twice in
    the same VM context: first with ``first_payload`` to render a real banner,
    then with ``payload`` (or a failing fetch when ``failure`` is True) to
    exercise the clearing path.  The returned state reflects the banner after
    the final call.
    """
    first_json = "null" if first_payload is None else json.dumps(first_payload)
    payload_json = json.dumps(payload)

    failure_stub = (
        """
globalThis.fetch = function (url) {
    __fetches.push(url);
    return Promise.reject(new Error('fetch failed'));
};
"""
        if failure
        else ""
    )

    script = (
        "return (async () => {\n"
        f"    if ({first_json} !== null) {{\n"
        f"        globalThis.__fetchPayload = {first_json};\n"
        "        await pollScanActivity();\n"
        "    }\n"
        f"{failure_stub}"
        f"    globalThis.__fetchPayload = {payload_json};\n"
        "    await pollScanActivity();\n"
        "    const box = document.getElementById('scan-activity');\n"
        "    return { hidden: box.hidden, html: box.innerHTML, fetches: __fetches };\n"
        "})();"
    )

    return run_js(
        sources=["utils.js", "portfolio.js"],
        bootstrap=BOOTSTRAP,
        script=script,
    )


def test_renders_portfolio_progress():
    result = _run_poll(PORTFOLIO_PROGRESS)
    assert result["hidden"] is False
    assert result["html"] != ""
    assert "portfolio" in result["html"].lower()


def test_renders_spy_progress():
    result = _run_poll(SPY_PROGRESS)
    assert result["hidden"] is False
    assert result["html"] != ""
    assert "spy" in result["html"].lower()


def test_polls_only_the_status_endpoint():
    result = _run_poll(NO_RUNNING, first_payload=PORTFOLIO_PROGRESS)
    assert result["hidden"] is True
    assert result["html"] == ""
    assert result["fetches"] == ["/api/portfolio/status", "/api/portfolio/status"]


def test_options_run_does_not_render_spy_banner():
    result = _run_poll(OPTIONS_RUN, first_payload=PORTFOLIO_PROGRESS)
    assert result["hidden"] is True
    assert result["html"] == ""
    assert "spy" not in result["html"].lower()


def test_pending_scan_does_not_render():
    result = _run_poll(PENDING_SCAN, first_payload=PORTFOLIO_PROGRESS)
    assert result["hidden"] is True
    assert result["html"] == ""


def test_fetch_failure_hides_banner():
    result = _run_poll(NO_RUNNING, first_payload=PORTFOLIO_PROGRESS, failure=True)
    assert result["hidden"] is True
    assert result["html"] == ""


def test_waiting_options_scan_does_not_render_spy_banner():
    result = _run_poll(WAITING_OPTIONS)
    assert "spy" not in result["html"].lower()


RESEARCH_RUNNING = {
    "running": {
        "id": 42,
        "scan_type": "spy",
        "kind": "research",
        "status": "running_deep",
        "quick_count": 151,
        "quick_total": 151,
        "deep_count": 10,
        "deep_total": 51,
    },
    "queued": [],
    "waiting": [],
}

WAITING_EQUITY_ALLOCATION = {
    "running": None,
    "queued": [],
    "waiting": [
        {
            "id": 43,
            "scan_type": "spy",
            "kind": "equity",
            "status": "running_wait_research",
        }
    ],
}


def test_research_row_renders_daily_research_banner():
    result = _run_poll(RESEARCH_RUNNING)
    assert result["hidden"] is False
    html = result["html"]
    assert "Daily research" in html
    assert "#42" in html
    assert "10/51" in html
    assert "S&amp;P 500 scan" not in html
    assert "S&P 500 scan" not in html


def test_waiting_research_allocation_renders_spy_block():
    result = _run_poll(WAITING_EQUITY_ALLOCATION)
    assert result["hidden"] is False
    html = result["html"]
    assert "S&amp;P 500 scan" in html
    assert "Daily research" not in html


def test_queue_renders_research_item_with_rsch_tag():
    data = {
        "running": None,
        "waiting": [],
        "queued": [
            {
                "id": 7,
                "scan_type": "spy",
                "kind": "research",
                "status": "queued",
                "trade_date": "2026-09-25",
                "created_at": None,
            }
        ],
    }
    script = (
        "const ul = document.createElement('ul');\n"
        f"renderScanQueue(ul, {json.dumps(data)});\n"
        "return ul.children.map((li) => li.innerHTML);"
    )
    items = run_js(sources=["utils.js", "portfolio.js"], script=script)
    assert len(items) == 1
    assert "rsch #7" in items[0]


def test_scan_type_key_research_and_unchanged_keys():
    script = """
return {
    research: scanTypeKey({scan_type: 'spy', kind: 'research'}),
    options: scanTypeKey({scan_type: 'spy', kind: 'options'}),
    equity: scanTypeKey({scan_type: 'spy', kind: 'equity'}),
    portfolio: scanTypeKey({scan_type: 'portfolio'}),
    tag: SCAN_TYPE_TAG.research,
};
"""
    result = run_js(sources=["utils.js", "portfolio.js"], script=script)
    assert result == {
        "research": "research",
        "options": "options",
        "equity": "spy",
        "portfolio": "portfolio",
        "tag": "rsch",
    }


def test_zero_deep_total_renders_zero_not_placeholder():
    script = (
        "return scanActivityResearch({id: 1, quick_count: 151, quick_total: 151,"
        " deep_count: 0, deep_total: 0});"
    )
    html = run_js(sources=["utils.js", "portfolio.js"], script=script)
    assert "Deep 0/0" in html
    assert "0/51" not in html


def test_unknown_totals_use_151_51_placeholders():
    script = "return scanActivitySpy({status: 'running_wait_research'});"
    html = run_js(sources=["utils.js", "portfolio.js"], script=script)
    assert "Quick 0/151" in html
    assert "Deep 0/51" in html
