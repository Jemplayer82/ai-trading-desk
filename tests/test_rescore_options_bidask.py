import json
import sqlite3

import pytest

from scripts.rescore_options_bidask import estimate_position, load_rows, markdown, rescore


def position(**changes):
    row = dict(id=1, paper_account_id=1, status='closed', contracts=2,
               entry_premium=10, entry_bid=9, entry_ask=11, cost_basis=2002,
               exit_premium=12, exit_value=2397, realized_pnl=395)
    return dict(row, **changes)


def test_crosses_spread_preserves_fees_and_account_totals():
    result = rescore([position(), position(paper_account_id=2), position(status='open')])
    estimate = result['positions'][0]
    assert estimate['estimated_exit_premium'] == pytest.approx(10.8)
    assert estimate['entry_fees_preserved'] == 2
    assert estimate['exit_fees_preserved'] == 3
    assert estimate['estimated_pnl'] == -45
    assert result['total']['recorded_pnl'] == 790
    assert result['total']['estimated_pnl'] == -90
    assert result['exclusions'] == {'open_position': 1}
    assert 'ESTIMATE' in markdown(result)
    assert 'ASSUMPTIONS' in markdown(result)


@pytest.mark.parametrize('changes,reason', [
    ({'entry_ask': '1e999999'}, 'missing_or_nonfinite_numeric'),
    ({'entry_ask': '1e-999999'}, 'missing_or_nonfinite_numeric'),
    ({'entry_bid': None}, 'missing_or_nonfinite_numeric'),
    ({'entry_ask': float('nan')}, 'missing_or_nonfinite_numeric'),
    ({'exit_premium': float('inf')}, 'missing_or_nonfinite_numeric'),
    ({'entry_bid': 12}, 'invalid_price_or_quote'),
    ({'entry_bid': -1}, 'invalid_price_or_quote'),
    ({'entry_ask': 0}, 'invalid_price_or_quote'),
    ({'contracts': 1.5}, 'invalid_contract_count'),
    ({'contracts': 0}, 'invalid_contract_count'),
    ({'cost_basis': 1900}, 'inconsistent_fee_basis'),
    ({'realized_pnl': 400}, 'inconsistent_recorded_pnl'),
    ({'fill_model': 'bid_ask'}, 'already_bid_ask'),
    ({'status': 'unknown'}, 'unsupported_status'),
])
def test_bad_data_counted_without_exceptions(changes, reason):
    assert estimate_position(position(**changes)) == (None, reason)


@pytest.mark.parametrize('status', ['expired_itm', 'expired_worthless', 'settled'])
def test_cash_settlement_primary_haircut_and_separate_sensitivity(status):
    estimate, reason = estimate_position(position(status=status, exit_reason='expiry'))
    assert reason is None
    assert estimate['estimated_pnl'] == -45
    assert estimate['cash_settlement_sensitivity_pnl'] == 195
    assert estimate['estimated_exit_premium'] == pytest.approx(10.8)
    assert estimate['cash_settlement'] is True


def test_zero_entry_bid_and_worthless_exit_valid():
    estimate, reason = estimate_position(position(entry_bid=0, exit_premium=0,
                                                 exit_value=0, realized_pnl=-2002))
    assert reason is None
    assert estimate['estimated_exit_premium'] == 0
    assert estimate['estimated_pnl'] == -2202


def test_all_excluded_and_empty_inputs_serialize():
    result = rescore([position(status='open'), None])
    assert result['total']['excluded'] == 2
    assert result['total']['included'] == 0
    json.dumps(result, allow_nan=False)
    assert rescore([])['total']['estimated_pnl'] == 0


def test_json_and_sqlite_copy_readonly(tmp_path):
    export = tmp_path / 'positions.json'
    export.write_text(json.dumps([position()]))
    assert load_rows(json_export=export) == [position()]
    copy = tmp_path / 'copy.sqlite3'
    with sqlite3.connect(copy) as db:
        db.execute('CREATE TABLE options_positions (id INTEGER, status TEXT)')
        db.execute("INSERT INTO options_positions VALUES (1, 'closed')")
    before = copy.read_bytes()
    assert load_rows(sqlite_copy=copy) == [{'id': 1, 'status': 'closed'}]
    assert copy.read_bytes() == before
    assert not list(tmp_path.glob('*-wal'))
    with pytest.raises(ValueError):
        load_rows(json_export=export, sqlite_copy=copy)
