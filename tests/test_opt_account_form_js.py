"""Browser-side options paper-account form tests.

These tests exercise the account create/edit form helpers in
``web/static/options.js`` using the Node.js vm harness in ``tests/jsvm.py``.
"""

import pytest

from tests.jsvm import run_js

pytestmark = pytest.mark.unit

BOOTSTRAP = """
globalThis.__posts = [];
globalThis.alert = function () {};
globalThis.confirm = function () { return true; };
globalThis.renderScanQueue = function () {};
globalThis.fetch = function (url, options) {
    __posts.push([url, options]);
    return Promise.resolve({
        ok: true,
        status: 200,
        json: function () { return Promise.resolve({accounts: [], scans: []}); }
    });
};
"""


def _run(script):
    return run_js(
        sources=["utils.js", "options.js"],
        bootstrap=BOOTSTRAP,
        script=script,
    )


def test_saves_new_options_account_with_schedule_and_stop():
    result = _run(
        """
        return (async () => {
            document.getElementById('opt-new-name').value = 'Opt';
            document.getElementById('opt-new-capital').value = 50000;
            document.getElementById('opt-new-agg').value = 5;
            document.getElementById('opt-new-schedule').value = '10:00';
            document.getElementById('opt-new-stop-type').value = 'stop_limit';
            document.getElementById('opt-new-stop-value').value = '12';
            document.getElementById('opt-new-stop-offset').value = '3';
            await saveOptAccount();
            const posts = __posts.filter(p => p[0] === '/api/paper-accounts');
            const body = JSON.parse(posts[0][1].body);
            return { count: posts.length, body: body };
        })();
        """
    )
    assert result["count"] == 1
    body = result["body"]
    assert body["kind"] == "options"
    assert body["schedule_time"] == "10:00"
    assert body["stop_type"] == "stop_limit"
    assert body["stop_value"] == 12
    assert isinstance(body["stop_value"], float) or isinstance(body["stop_value"], int)
    assert body["stop_limit_offset"] == 3
    assert isinstance(body["stop_limit_offset"], float) or isinstance(body["stop_limit_offset"], int)


def test_blank_numerics_serialize_as_null_when_no_stop():
    result = _run(
        """
        return (async () => {
            document.getElementById('opt-new-name').value = 'Blank';
            document.getElementById('opt-new-capital').value = 100000;
            document.getElementById('opt-new-agg').value = 5;
            document.getElementById('opt-new-schedule').value = '';
            document.getElementById('opt-new-stop-type').value = 'none';
            document.getElementById('opt-new-stop-value').value = '';
            document.getElementById('opt-new-stop-offset').value = '';
            await saveOptAccount();
            const posts = __posts.filter(p => p[0] === '/api/paper-accounts');
            return JSON.parse(posts[0][1].body);
        })();
        """
    )
    assert result["stop_value"] is None
    assert result["stop_limit_offset"] is None


def test_blank_schedule_serializes_as_empty_string():
    result = _run(
        """
        return (async () => {
            document.getElementById('opt-new-name').value = 'NoSched';
            document.getElementById('opt-new-capital').value = 100000;
            document.getElementById('opt-new-agg').value = 5;
            document.getElementById('opt-new-schedule').value = '';
            document.getElementById('opt-new-stop-type').value = 'none';
            await saveOptAccount();
            const posts = __posts.filter(p => p[0] === '/api/paper-accounts');
            return JSON.parse(posts[0][1].body).schedule_time;
        })();
        """
    )
    assert result == ""


def test_populate_then_save_round_trip():
    result = _run(
        """
        return (async () => {
            populateOptAccountForm({
                name: 'OptAcct', starting_capital: 100000, aggressiveness: 5, bias: 'neutral',
                schedule_time: '07:45', stop_type: 'stop_limit', stop_value: 15, stop_limit_offset: 2
            });
            await saveOptAccount();
            const posts = __posts.filter(p => p[0] === '/api/paper-accounts');
            return JSON.parse(posts[0][1].body);
        })();
        """
    )
    assert result["schedule_time"] == "07:45"
    assert result["stop_type"] == "stop_limit"
    assert result["stop_value"] == 15
    assert result["stop_limit_offset"] == 2


def test_null_stop_value_renders_as_empty_input():
    result = _run(
        """
        populateOptAccountForm({
            name: 'X', starting_capital: 100000, aggressiveness: 5, bias: 'neutral',
            schedule_time: '', stop_type: 'none', stop_value: null, stop_limit_offset: null
        });
        return document.getElementById('opt-new-stop-value').value === '';
        """
    )
    assert result is True


def test_stop_field_visibility():
    result = _run(
        """
        function check(type) {
            document.getElementById('opt-new-stop-type').value = type;
            stopFieldVisibility('opt-new');
            return {
                valueHidden: document.getElementById('opt-new-stop-value-wrap').hidden,
                offsetHidden: document.getElementById('opt-new-stop-offset-wrap').hidden,
                label: document.getElementById('opt-new-stop-value-label').textContent
            };
        }
        return {
            none: check('none'),
            stop: check('stop'),
            stopLimit: check('stop_limit'),
            trailingDollar: check('trailing_dollar')
        };
        """
    )
    assert result["none"]["valueHidden"] is True
    assert result["none"]["offsetHidden"] is True

    assert result["stop"]["valueHidden"] is False
    assert result["stop"]["offsetHidden"] is True

    assert result["stopLimit"]["valueHidden"] is False
    assert result["stopLimit"]["offsetHidden"] is False

    assert result["trailingDollar"]["valueHidden"] is False
    assert result["trailingDollar"]["offsetHidden"] is True
    label = result["trailingDollar"]["label"]
    assert "$" in label or "Trail" in label


def test_reset_form_defaults():
    result = _run(
        """
        resetOptAccountForm();
        return {
            type: document.getElementById('opt-new-stop-type').value,
            value: document.getElementById('opt-new-stop-value').value,
            offset: document.getElementById('opt-new-stop-offset').value,
            schedule: document.getElementById('opt-new-schedule').value
        };
        """
    )
    assert result["type"] == "none"
    assert result["value"] == ""
    assert result["offset"] == ""
    assert result["schedule"] == "09:00"


def test_progress_waiting_for_research_note_and_defaults():
    result = _run(
        """
        return optProgressHtml({status: 'running_wait_research', quick_total: null, deep_total: null});
        """
    )
    assert "Waiting for today's shared research" in result
    assert "0/151" in result
    assert "0/51" in result
    assert "pre-screen" not in result.lower()


def test_progress_market_and_alloc_notes_still_render():
    result = _run(
        """
        return {
            market: optProgressHtml({status: 'running_wait_market'}),
            alloc: optProgressHtml({status: 'running_wait_alloc'}),
            done: optProgressHtml({status: 'complete'}),
        };
        """
    )
    assert "09:35 ET" in result["market"]
    assert "Waiting for today's shared research" not in result["market"]
    assert "allocation slot" in result["alloc"]
    assert result["done"] == ""


def test_blank_stop_value_blocks_submission():
    result = _run(
        """
        return (async () => {
            document.getElementById('opt-new-name').value = 'Bad';
            document.getElementById('opt-new-capital').value = 100000;
            document.getElementById('opt-new-agg').value = 5;
            document.getElementById('opt-new-stop-type').value = 'stop';
            document.getElementById('opt-new-stop-value').value = '';
            await saveOptAccount();
            return __posts.length;
        })();
        """
    )
    assert result == 0


def test_put_edit_includes_kind_and_correct_url():
    result = _run(
        """
        return (async () => {
            editingOptAccountId = 7;
            document.getElementById('opt-new-name').value = 'Edit';
            document.getElementById('opt-new-capital').value = 75000;
            document.getElementById('opt-new-agg').value = 6;
            document.getElementById('opt-new-schedule').value = '08:00';
            document.getElementById('opt-new-stop-type').value = 'trailing_pct';
            document.getElementById('opt-new-stop-value').value = '10';
            document.getElementById('opt-new-stop-offset').value = '';
            await saveOptAccount();
            const put = __posts.find(p => p[0] === '/api/paper-accounts/7');
            return { url: put[0], body: JSON.parse(put[1].body) };
        })();
        """
    )
    assert result["url"] == "/api/paper-accounts/7"
    assert result["body"]["kind"] == "options"
    assert result["body"]["schedule_time"] == "08:00"
    assert result["body"]["stop_type"] == "trailing_pct"
    assert result["body"]["stop_value"] == 10
    assert result["body"]["stop_limit_offset"] is None


def test_stop_summary_resolves_from_utils():
    result = _run(
        """
        return {
            none: stopSummary({ stop_type: 'none' }),
            stop: stopSummary({ stop_type: 'stop', stop_value: 60 }),
            stopLimit: stopSummary({ stop_type: 'stop_limit', stop_value: 60, stop_limit_offset: 5 }),
            trailingPct: stopSummary({ stop_type: 'trailing_pct', stop_value: 10 }),
            trailingDollar: stopSummary({ stop_type: 'trailing_dollar', stop_value: 2.5 }),
        };
        """
    )
    assert result["none"] == "no stop"
    assert result["stop"] == "stop 60%"
    assert result["stopLimit"] == "stop 60% / limit 5%"
    assert result["trailingPct"] == "trail 10%"
    assert result["trailingDollar"] == "trail $2.5"

# ── Open-positions totals footer (mirrors the S&P portfolio table) ──────────

_POSITIONS = """[
  {underlying: "AAPL", put_call: "CALL", strike: 200, expiration_date: "2026-10-16",
   contracts: 2, entry_premium: 5, cost_basis: 1000, current_premium: 6, current_value: 1200},
  {underlying: "MSFT", put_call: "PUT", strike: 400, expiration_date: "2026-10-23",
   contracts: 1, entry_premium: 8, cost_basis: 800, current_premium: null, current_value: null}
]"""


def _footer(script_args):
    out = _run(
        "const html = optOpenPositionsHtml(" + script_args + ");\n"
        "const i = html.indexOf('<tfoot>');\n"
        "return i < 0 ? null : html.slice(i, html.indexOf('</tfoot>') + 8)"
        ".replace(/<[^>]+>/g, '|').replace(/\\|+/g, '|');"
    )
    return out


def test_open_positions_footer_totals_cost_value_cash_and_account():
    summary = "{cash: 98000.4, equity: 100199.6, return_pct: 0.2}"
    text = _footer(_POSITIONS + ", " + summary)
    # Open book: cost 1,800; value 1,200 + 800 (no mark -> carried at cost) = 2,000; +11.1%
    assert "Open positions (2)|+11.1%|$1,800|$2,000|" in text
    assert "Cash|$98,000|" in text
    assert "ACCOUNT VALUE|+0.2%|$100,200|" in text


def test_open_positions_footer_without_summary_shows_only_the_open_book():
    text = _footer(_POSITIONS)
    assert "Open positions (2)" in text
    assert "Cash" not in text and "ACCOUNT VALUE" not in text


def test_no_open_positions_renders_no_footer():
    assert _footer("[], {cash: 1, equity: 1, return_pct: 0}") is None
