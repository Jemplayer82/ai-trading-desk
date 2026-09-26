import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.unit


def _tier_blocks(html, tier):
    begin = f"<!-- TIER:{tier} BEGIN -->"
    end = f"<!-- TIER:{tier} END -->"
    ranges = []
    begin_idx = 0
    while True:
        begin_idx = html.find(begin, begin_idx)
        if begin_idx == -1:
            break
        end_idx = html.find(end, begin_idx)
        assert end_idx != -1, f"TIER:{tier} BEGIN without matching END"
        ranges.append((begin_idx, end_idx))
        begin_idx = end_idx + len(end)
    assert ranges, f"TIER:{tier} markers missing"
    return ranges


def _assert_id_in_tier_block(html, id_, tier):
    ranges = _tier_blocks(html, tier)
    id_idx = html.find(f'id="{id_}"')
    assert id_idx != -1, f"{id_} not found in index.html"
    assert any(begin_idx < id_idx < end_idx for begin_idx, end_idx in ranges), f"{id_} is not inside any TIER:{tier} block"


def test_portfolio_nightly_controls_within_tier2():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    if not (ROOT / "web/static/portfolio.js").exists():
        pytest.skip("portfolio.js not present at this tier")
    _assert_id_in_tier_block(html, "pf-nightly-caption", 2)
    _assert_id_in_tier_block(html, "pf-nightly-time", 2)


def test_spy_account_form_within_tier3():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    if not (ROOT / "web/static/spy.js").exists():
        pytest.skip("spy.js not present at this tier")
    ids = [
        "new-acct-schedule",
        "new-acct-stop-type",
        "new-acct-stop-value",
        "new-acct-stop-value-label",
        "new-acct-stop-value-wrap",
        "new-acct-stop-offset-wrap",
        "new-acct-stop-offset",
    ]
    for id_ in ids:
        _assert_id_in_tier_block(html, id_, 3)


def test_spy_research_controls_within_tier3():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    if not (ROOT / "web/static/spy.js").exists():
        pytest.skip("spy.js not present at this tier")
    for id_ in ("spy-research-status", "spy-research-time", "spy-research-time-status"):
        _assert_id_in_tier_block(html, id_, 3)


def test_options_account_form_within_tier4():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    if not (ROOT / "web/static/options.js").exists():
        pytest.skip("options.js not present at this tier")
    ids = [
        "opt-new-schedule",
        "opt-new-stop-type",
        "opt-new-stop-value",
        "opt-new-stop-value-label",
        "opt-new-stop-value-wrap",
        "opt-new-stop-offset-wrap",
        "opt-new-stop-offset",
    ]
    for id_ in ids:
        _assert_id_in_tier_block(html, id_, 4)


def test_options_markup_absent_when_options_tier_stripped():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    if not (ROOT / "web/static/options.js").exists():
        assert "opt-new-stop-type" not in html


def _stop_type_select_option_values(html, select_id):
    match = re.search(
        rf'<select[^>]*id="{re.escape(select_id)}"[^>]*>(.*?)</select>',
        html,
        re.DOTALL,
    )
    assert match is not None, f"{select_id} select not found in index.html"
    return re.findall(r'<option value="([^"]+)"', match.group(1))


def test_stop_type_option_values():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    expected = ["none", "stop", "stop_limit", "trailing_pct", "trailing_dollar"]
    select_ids = []
    if (ROOT / "web/static/spy.js").exists():
        select_ids.append("new-acct-stop-type")
    if (ROOT / "web/static/options.js").exists():
        select_ids.append("opt-new-stop-type")
    if not select_ids:
        pytest.skip("no account modals present at this tier")
    all_values = []
    for select_id in select_ids:
        all_values.extend(_stop_type_select_option_values(html, select_id))
    assert all_values, "stop-type options missing"
    assert all_values == expected * (len(all_values) // len(expected)), "unexpected stop-type option values"


def test_simulated_stop_wording_in_modals():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    if not (ROOT / "web/static/spy.js").exists() and not (ROOT / "web/static/options.js").exists():
        pytest.skip("no account modals present at this tier")
    found = False
    if (ROOT / "web/static/spy.js").exists():
        ranges = _tier_blocks(html, 3)
        for begin, end in ranges:
            if re.search(r"simulated", html[begin:end], re.IGNORECASE):
                found = True
                break
    if (ROOT / "web/static/options.js").exists():
        ranges = _tier_blocks(html, 4)
        for begin, end in ranges:
            if re.search(r"simulated", html[begin:end], re.IGNORECASE):
                found = True
                break
    assert found, "simulated stop wording missing"


def test_utils_defines_stop_field_visibility():
    utils = (ROOT / "web/static/utils.js").read_text(encoding="utf-8")
    assert "function stopFieldVisibility" in utils