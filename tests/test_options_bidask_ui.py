"""Bid/ask UI rendering stays tied to the server's liquidation mark."""

import pytest

from tests.jsvm import run_js

pytestmark = pytest.mark.unit


def _run(script):
    return run_js(sources=["utils.js", "options.js"], script=script)


def test_bid_mark_and_information_do_not_use_mid_for_pnl():
    html = _run("""
        return optOpenPositionsHtml([{underlying:'A',put_call:'CALL',strike:1,
            contracts:1,entry_premium:5,cost_basis:500,current_premium:0,
            current_value:0,current_bid:0,current_ask:2,current_mid:1}]);
    """)
    assert "Mark (bid)" in html
    assert "bid $0.00 · ask $2.00 · mid $1.00 · spread $2.00 (200.0% of mid)" in html
    assert "-100.0%" in html
    assert "-80.0%" not in html
    assert "(stale)" not in html


@pytest.mark.parametrize("source", ["carried", "carried_bid", "intrinsic", "snapshot"])
def test_carried_bid_remains_mark_when_quote_is_missing(source):
    html = _run("const source = " + repr(source) + ";" + """
        return optOpenPositionsHtml([{underlying:'A',put_call:'CALL',strike:1,
            contracts:1,entry_premium:5,cost_basis:500,current_premium:3,
            current_value:300,current_bid:null,current_ask:6,current_mid:4,
            price_source:source,stale_count:source === 'snapshot' ? 1 : 0}]);
    """)
    assert "$3.00" in html and "(stale)" in html
    assert "bid — · ask $6.00 · mid $4.00 · spread —" in html
    assert "-40.0%" in html


def test_invalid_or_crossed_quote_info_is_unavailable():
    html = _run("""
        return [optQuoteInfoHtml({current_bid:NaN,current_ask:Infinity,current_mid:-1}),
                optQuoteInfoHtml({current_bid:5,current_ask:4,current_mid:4.5})];
    """)
    assert "bid — · ask — · mid — · spread —" in html[0]
    assert "spread —" in html[1]
    assert "NaN" not in "".join(html) and "Infinity" not in "".join(html)


def test_account_cutover_note_is_escaped_and_policy_visible():
    html = _run("""
        return optSummaryHtml({equity:100,cash:100,deployed:0,open_value:0,
            realized_pnl:0,return_pct:0,open_count:0,closed_count:0},
            {fill_model_cutover:'2026-10-03 <script>'});
    """)
    assert "fills before 2026-10-03 &lt;script&gt; were at mid; see re-score" in html
    assert "Entries fill at ask; sells and open marks use bid." in html
    assert "Fixed stops reference the entry-time bid" in html
    assert "trailing and staged stops track a bid peak starting at the entry-time bid" in html


def test_policy_is_visible_in_account_selector_and_modal():
    result = _run("""
        optAccounts = [{id:1,name:'Bull',bias:'bullish',aggressiveness:5}];
        activeOptAccountId = 1;
        updateOptAccountMeta();
        renderOptAccountsModal();
        return [document.getElementById('opt-account-meta').textContent,
                document.getElementById('opt-accounts-list').innerHTML];
    """)
    assert all("Entries fill at ask; sells and open marks use bid." in x for x in result)
