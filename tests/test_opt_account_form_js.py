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
    # Average P&L over rows with a mark: AAPL 6/5-1 = +20.0% (MSFT has no mark).
    # Dollar-weighted book: value 1,200 + 800 (carried at cost) = 2,000 vs cost 1,800 = +11.1%.
    assert "Total — 2 open" in text
    assert "|avg |+20.0%| |(+11.1% on $)|$1,800|$2,000|" in text
    assert "Cash|$98,000|" in text
    assert "ACCOUNT VALUE|+0.2%|$100,200|" in text


def test_open_positions_footer_without_summary_shows_only_the_open_book():
    text = _footer(_POSITIONS)
    assert "Total — 2 open" in text
    assert "Cash" not in text and "ACCOUNT VALUE" not in text


def test_no_open_positions_renders_no_footer():
    assert _footer("[], {cash: 1, equity: 1, return_pct: 0}") is None


def test_average_pnl_is_the_mean_of_the_row_percentages():
    rows = """[
      {underlying: "A", put_call: "CALL", strike: 1, expiration_date: "2026-10-16", contracts: 1,
       entry_premium: 1, cost_basis: 100, current_premium: 2, current_value: 200},
      {underlying: "B", put_call: "CALL", strike: 1, expiration_date: "2026-10-16", contracts: 10,
       entry_premium: 10, cost_basis: 10000, current_premium: 5, current_value: 5000}
    ]"""
    text = _footer(rows)
    # Rows: +100% and -50% -> average +25.0%; dollars: 5,200 / 10,100 -> -48.5%.
    assert "|avg |+25.0%| |(-48.5% on $)|$10,100|$5,200|" in text


def test_staged_form_defaults_visibility_and_submit():
    result = _run("""
        return (async () => {
            resetOptAccountForm();
            document.getElementById('opt-new-name').value = 'Bull staged 10%';
            document.getElementById('opt-new-stop-type').value = 'trailing_staged';
            stopFieldVisibility('opt-new');
            const visible = ['stop-value', 'stage-trigger', 'stage-trail'].map(
                id => !document.getElementById('opt-new-' + id + '-wrap').hidden);
            const label = document.getElementById('opt-new-stop-value-label').textContent;
            const helpVisible = !document.getElementById('opt-new-stage-help').hidden;
            await saveOptAccount();
            return { visible, label, helpVisible, body: JSON.parse(__posts[0][1].body) };
        })();
    """)
    assert result['visible'] == [True, True, True]
    assert result['helpVisible'] is True
    assert result['label'] == 'Trail below peak (%)'
    body = result['body']
    assert (body['stop_type'], body['stop_value'], body['stage_trigger_pct'], body['stage_trail_pct']) == ('trailing_staged', 20, 20, 10)
    assert body['stop_limit_offset'] is None


def test_staged_form_edit_and_hide_clears_payload():
    result = _run("""
        return (async () => {
            populateOptAccountForm({name:'Stage',stop_type:'trailing_staged',stop_value:25,
                                    stage_trigger_pct:30,stage_trail_pct:15});
            const values = ['stop-value', 'stage-trigger', 'stage-trail'].map(
                id => document.getElementById('opt-new-' + id).value);
            const summary = stopSummary({stop_type:'trailing_staged',stop_value:25,
                                         stage_trigger_pct:30,stage_trail_pct:15});
            document.getElementById('opt-new-stop-type').value = 'trailing_pct';
            stopFieldVisibility('opt-new');
            const hidden = ['stage-trigger', 'stage-trail'].map(
                id => document.getElementById('opt-new-' + id + '-wrap').hidden);
            await saveOptAccount();
            return {values, summary, hidden, body:JSON.parse(__posts[0][1].body)};
        })();
    """)
    assert [float(x) for x in result['values']] == [25, 30, 15]
    assert result['summary'] == 'trail 25%; once up 30%, trail 15%'
    assert result['hidden'] == [True, True]
    assert result['body']['stage_trigger_pct'] is None
    assert result['body']['stage_trail_pct'] is None


@pytest.mark.parametrize(('trigger', 'tight', 'base'), [('0','10','20'), ('20','0','20'), ('20','100','20'), ('','10','20'), ('20','10','100')])
def test_invalid_staged_form_blocks_submit(trigger, tight, base):
    setup = (
        f"document.getElementById('opt-new-stop-value').value = {base!r};"
        f"document.getElementById('opt-new-stage-trigger').value = {trigger!r};"
        f"document.getElementById('opt-new-stage-trail').value = {tight!r};"
    )
    result = _run(setup + """
        return (async () => {
            document.getElementById('opt-new-name').value = 'Stage';
            document.getElementById('opt-new-stop-type').value = 'trailing_staged';
            await saveOptAccount();
            return __posts.length;
        })();
    """)
    assert result == 0


@pytest.mark.parametrize('trail', [1, 40, 99])
def test_staged_form_accepts_after_trigger_amount(trail):
    result = _run("""
        return (async () => {
            populateOptAccountForm({name:'Stage',stop_type:'trailing_staged',stop_value:20,
                                    stage_trigger_pct:20,stage_trail_pct:TRAIL});
            await saveOptAccount();
            return JSON.parse(__posts[0][1].body).stage_trail_pct;
        })();
    """.replace('TRAIL', str(trail)))
    assert result == trail
