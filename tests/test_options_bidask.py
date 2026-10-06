"""Executable bid/ask regression checks, isolated SQLite only."""
import sqlite3
from datetime import datetime

import pytest

from web import account_policy as ap
from web import db, options_allocator, options_data, options_engine, options_fills

pytestmark = pytest.mark.unit


@pytest.fixture()
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'scratch.db')
    db.init_db()
    aid = db.create_paper_account('bull', kind='options', stop_type='trailing_staged',
                                 stop_value=20, stage_trigger_pct=20, stage_trail_pct=10)
    db.append_options_cash(aid, 'deposit', 10000)
    return aid


def position(account, **kwargs):
    data = dict(occ_symbol='TEST', underlying='AAPL', put_call='CALL', strike=100,
                expiration_date='2099-01-01', contracts=2, entry_premium=999,
                entry_bid=8, entry_ask=12)
    data.update(kwargs)
    return db.open_options_position(account, 1, data)


@pytest.mark.parametrize('bad', [None, -1, True, '4', float('nan'), float('inf'), 1e308, {}, []])
def test_bad_quotes_are_exclusions(bad):
    assert options_fills.buy_quote(bad, 10)[1] == 'invalid_entry_quote'
    assert options_fills.buy_quote(1, bad)[1] == 'invalid_entry_quote'
    assert options_fills.sell_quote(bad, 3) == (3, 'stale_bid')
    assert options_fills.sell_quote(bad, bad) == (None, 'missing_bid')


def test_zero_bid_is_executable():
    assert options_fills.sell_quote(0, 3) == (0, None)
    assert options_fills.buy_quote(0, 3) == (3, None)
    assert options_fills.buy_quote(5, 3)[0] is None


def test_ask_debit_bid_equity_stop_and_quote_audit(account, monkeypatch):
    monkeypatch.setattr(options_engine, "_underlying_prices", lambda *args: {})
    pid = position(account)
    row = db.get_options_position(pid)
    assert (row['entry_premium'], row['cost_basis'], row['current_bid'], row['peak_premium']) == (12, 2400, 8, 8)
    assert row['current_value'] == 1600
    assert row['fill_model'] == 'bid_ask'
    assert db.options_cash_balance(account) == 7600
    assert options_engine.account_equity(account)['equity'] == 9200
    policy = ap.StopPolicy.from_account(db.get_paper_account(account))
    assert options_allocator.effective_stop_level(row, policy).action == 'hold'
    db.mark_options_position(pid, 10, 999999, 'schwab', ask=14)
    row = db.get_options_position(pid)
    assert (row['peak_premium'], row['current_value'], row['current_mid']) == (10, 2000, 12)
    # +25% over entry bid tightens staged trail; crossing cannot fill at trigger 9.
    assert options_engine._apply_intraday_stops([row], {pid: (7, 'schwab')}, {account: policy}) == 1
    closed = db.get_options_position(pid)
    assert (closed['exit_premium'], closed['exit_bid'], closed['exit_value']) == (7, 7, 1400)
    assert closed['stop_level_hwm'] == 9


def test_missing_and_zero_refresh(account, monkeypatch):
    pid = position(account)
    monkeypatch.setattr(options_engine.schwab_mcp, 'market_data_enabled', lambda: True)
    monkeypatch.setattr(options_engine.schwab_mcp, 'get_quotes', lambda symbols: {'TEST': {'quote': {'mark': 999}}})
    monkeypatch.setattr(options_engine, '_yf_contract_price', lambda *args: None)
    summary = options_engine.refresh_positions(account)
    assert summary['marked'] == 1
    row = db.get_options_position(pid)
    assert (row['current_premium'], row['stale_count'], row['price_source']) == (8, 1, 'carried_bid')
    monkeypatch.setattr(options_engine.schwab_mcp, 'get_quotes', lambda symbols: {'TEST': {'quote': {'bidPrice': 0, 'askPrice': 1, 'mark': 999}}})
    # No underlying network needed for the stop metadata.
    monkeypatch.setattr(options_engine, '_underlying_prices', lambda *args: {})
    options_engine.refresh_positions(account)
    row = db.get_options_position(pid)
    assert (row['status'], row['exit_premium'], row['exit_bid'], row['exit_ask']) == ('closed', 0, 0, 1)
    assert row['stale_count'] == 0


def test_close_uses_bid_even_if_caller_supplies_mid(account):
    pid = position(account)
    db.mark_options_position(pid, 0, 999, 'schwab', ask=2)
    assert db.close_options_position(pid, 99, 'llm_close')
    row = db.get_options_position(pid)
    assert (row['exit_premium'], row['exit_bid'], row['exit_ask'], row['realized_pnl']) == (0, 0, 2, -2400)
    assert not db.close_options_position(pid, 99, 'llm_close')


def test_invalid_buy_does_not_debit(account):
    assert position(account, entry_bid=None) == 0
    assert position(account, entry_ask=float('nan')) == 0
    assert db.options_cash_balance(account) == 10000
    assert db.list_options_positions(account) == []


def test_migration_preserves_closed_numbers_and_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / 'legacy-copy.db'
    schema = db.SCHEMA
    for line in ('    fill_model TEXT NOT NULL DEFAULT \'mid_legacy\',\n', '    current_bid REAL,\n',
                 '    current_ask REAL,\n', '    current_mid REAL,\n', '    exit_bid REAL,\n', '    exit_ask REAL,\n'):
        schema = schema.replace(line, '')
    schema = schema.replace(',\n    fill_model TEXT NOT NULL DEFAULT \'bid_ask\',\n    fill_model_cutover TEXT', '')
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO paper_accounts (name, created_at, kind) VALUES ('bear','2026-01-01','options')")
        conn.execute("""INSERT INTO options_positions (paper_account_id,open_scan_id,occ_symbol,underlying,
             put_call,strike,expiration_date,contracts,entry_premium,cost_basis,status,opened_at,
             exit_premium,exit_value,realized_pnl,entry_bid,entry_ask)
             VALUES (1,1,'TEST','AAPL','CALL',100,'2026-01-01',2,10,2000,'closed','x',20,4000,2000,8,12)""")
    monkeypatch.setattr(db, 'DB_PATH', path)
    db.init_db()
    before = db.get_options_position(1)
    cutover = db.get_paper_account(1)['fill_model_cutover']
    db.init_db()
    assert db.get_options_position(1) == before
    assert (before['cost_basis'], before['exit_premium'], before['realized_pnl'], before['fill_model']) == (2000, 20, 2000, 'mid_legacy')
    assert db.get_paper_account(1)['fill_model_cutover'] == cutover
    assert db.get_paper_account(1)['fill_model'] == 'bid_ask'


def test_expiry_uses_last_bid_and_processes_every_row(account, monkeypatch):
    ids = [position(account, occ_symbol=f'TEST{i}', expiration_date='2026-01-01') for i in range(2)]
    db.mark_options_position(ids[1], 0, 0, 'schwab')
    monkeypatch.setattr(options_data, 'now_et', lambda: datetime(2026, 1, 2, 18, tzinfo=options_data._ET))
    monkeypatch.setattr(options_engine, 'underlying_close_on_or_before', lambda *args: pytest.fail('intrinsic lookup forbidden'))
    assert options_engine.settle_expired(account) == {'due': 2, 'settled_itm': 1, 'expired_worthless': 1, 'failed': 0}
    assert db.get_options_position(ids[0])['exit_premium'] == 8
    assert db.get_options_position(ids[1])['exit_premium'] == 0
    assert options_engine.settle_expired(account)['due'] == 0


def test_zero_entry_bid_can_gain_a_trailing_stop(account, monkeypatch):
    monkeypatch.setattr(options_engine, '_underlying_prices', lambda *args: {})
    pid = position(account, entry_bid=0, entry_ask=1)
    db.mark_options_position(pid, 2, 400, 'schwab', ask=3)
    pos = db.get_options_position(pid)
    policy = ap.StopPolicy.from_account(db.get_paper_account(account))
    assert pos['peak_premium'] == 2
    assert options_allocator.effective_stop_level(pos, policy).level == 1.8
    assert options_engine._apply_intraday_stops([pos], {pid: (1, 'schwab')}, {account: policy}) == 1
    assert db.get_options_position(pid)['exit_premium'] == 1


def test_zero_bid_open_value_never_falls_back_to_cost(account):
    db.update_paper_account(account, stop_type='none')
    pid = position(account)
    db.mark_options_position(pid, 0, 1234, 'schwab', ask=2)
    equity = options_engine.account_equity(account)
    assert equity == {'cash': 7600, 'open_value': 0, 'deployed': 2400, 'equity': 7600}
    summary = options_engine.account_summary(account)
    assert summary['fill_model'] == 'bid_ask'
    assert summary['fill_model_cutover'] is not None


def test_zero_zero_quote_is_missing_not_a_zero_sale():
    from web import options_fills
    assert options_fills.quote_bid(0, 0) is None          # empty / after-hours quote
    assert options_fills.quote_bid(0, None) is None
    assert options_fills.quote_bid(0, 0.05) == 0.0        # real no-bid market
    assert options_fills.quote_bid(1.2, 1.3) == 1.2


def test_zero_zero_and_carried_bids_never_trigger_a_stop(account, monkeypatch):
    pid = position(account)
    monkeypatch.setattr(options_engine.schwab_mcp, 'market_data_enabled', lambda: True)
    monkeypatch.setattr(options_engine, '_underlying_prices', lambda *args: {})
    monkeypatch.setattr(options_engine, '_yf_contract_price', lambda *args: None)
    # 0/0 quote = missing: the position is carried at its last bid, not sold at $0
    monkeypatch.setattr(options_engine.schwab_mcp, 'get_quotes',
                        lambda symbols: {'TEST': {'quote': {'bidPrice': 0, 'askPrice': 0}}})
    options_engine.refresh_positions(account)
    row = db.get_options_position(pid)
    assert (row['status'], row['price_source']) == ('open', 'carried_bid')
    # a carried bid far below the stop level still does not sell (only fresh quotes can)
    db.mark_options_position(pid, 5, 1000, 'carried_bid', reset_stale=False)
    db.update_paper_account(account, stop_type='stop', stop_value=10)
    options_engine.refresh_positions(account)
    assert db.get_options_position(pid)['status'] == 'open'
    # the same 5.00 bid from a fresh quote does sell
    monkeypatch.setattr(options_engine.schwab_mcp, 'get_quotes',
                        lambda symbols: {'TEST': {'quote': {'bidPrice': 5, 'askPrice': 5.5}}})
    options_engine.refresh_positions(account)
    assert db.get_options_position(pid)['status'] == 'closed'
