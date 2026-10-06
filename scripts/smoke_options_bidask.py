#!/usr/bin/env python3
"""Offline full numeric-path smoke on exported positions in disposable SQLite.

All monetary values are computed then discarded. Output contains only counts,
source hash and coverage/limitations. Historical exit quotes are unavailable:
entry bid/ask provide real quote inputs; zero and missing bids supplement them.
No live database is read; no provider calls or credentials are required.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import socket
import sys
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def smoke(export: Path) -> dict:
    rows = json.loads(export.read_text())
    if not isinstance(rows, list):
        raise ValueError('Expected a list of exported positions')
    counts = Counter()
    exclusions = Counter()
    exceptions = Counter()
    # Prevent accidental external calls in imported production code.
    def no_network(*args, **kwargs):
        raise RuntimeError('Network disabled for offline smoke')
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix='bidask-smoke-') as scratch, \
         patch.object(socket.socket, 'connect', no_network), \
         patch.object(socket.socket, 'connect_ex', no_network):
        from web import account_policy, db, options_allocator, options_engine, options_fills
        original_path = db.DB_PATH
        db.DB_PATH = Path(scratch) / 'export-seeded-copy.sqlite3'
        try:
            # Fresh scratch schema only. Never open the app's configured DB.
            with db.connect() as connection:
                connection.executescript(db.SCHEMA)
                db._run_column_migrations(connection)
                connection.execute("INSERT INTO paper_accounts (id,name,created_at,kind,stop_type,stop_value,stage_trigger_pct,stage_trail_pct) VALUES (1,'offline smoke','2026-01-01','options','trailing_staged',60,50,30)")
            scan = db.create_spy_scan('2026-01-01', paper_account_id=1, kind='options')
            policies = [account_policy.StopPolicy.from_account(dict(
                stop_type=kind, stop_value=60 if kind != 'trailing_dollar' else 1,
                stop_limit_offset=5, stage_trigger_pct=50, stage_trail_pct=30))
                for kind in account_policy.STOP_TYPES]
            for source in rows:
                counts['rows_processed'] += 1
                if not isinstance(source, dict):
                    exclusions['invalid_row'] += 1
                    continue
                try:
                    ask, reason = options_fills.buy_quote(source.get('entry_bid'), source.get('entry_ask'))
                    if ask is None:
                        exclusions[reason] += 1
                        continue
                    n = source.get('contracts')
                    if not isinstance(n, int) or n <= 0:
                        exclusions['invalid_contract_count'] += 1
                        continue
                    candidate = dict(source, paper_account_id=1)
                    allocator_candidate = dict(candidate, bid=source['entry_bid'], ask=ask,
                                               delta=source.get('entry_delta'), dte=30)
                    response = SimpleNamespace(content=json.dumps([dict(
                        occ_symbol=source['occ_symbol'], action='NEW', contracts=n)]))
                    offline_llm = SimpleNamespace(invoke=lambda _messages, response=response: response)
                    budget = ask * 100 * n * 100
                    with patch.object(options_allocator, 'llm_for', return_value=offline_llm):
                        allocation = options_allocator.run([allocator_candidate], [], '2026-01-01',
                            {}, equity=budget, cash=budget, policy=account_policy.NONE)
                    if not allocation['opens']:
                        exclusions['allocator_open_excluded'] += 1
                    elif allocation['opens'][0]['cost'] != round(ask * 100 * n, 2):
                        raise AssertionError('Allocator cost did not use ask')
                    counts['allocator_sizing_paths'] += 1
                    # Execute buy, persisted bid peak, mark, stop evaluation and
                    # every close reason on independent copies of each real row.
                    for exit_reason in ('llm_close', 'dte_floor', 'stop_loss', 'trail_stop', 'stop_limit', 'expiry'):
                        position_id = db.open_options_position(1, scan, candidate)
                        counts['entry_paths'] += 1
                        if not position_id:
                            exclusions['entry_rejected'] += 1
                            continue
                        bid = source['entry_bid']
                        db.mark_options_position(position_id, bid, bid * 100 * n, 'export_bid', ask=ask)
                        counts['mark_paths'] += 1
                        pos = db.get_options_position(position_id)
                        for policy in policies:
                            options_allocator.effective_stop_level(pos, policy, prev_mark=bid)
                            options_allocator.forced_closes([pos], policy)
                            counts['stop_paths'] += 1
                        # Missing quote carries a valid bid and increments stale.
                        carried, stale = options_fills.sell_quote(None, bid)
                        if stale:
                            exclusions['stale_bid_scenarios'] += 1
                        db.mark_options_position(position_id, carried, carried * 100 * n,
                                                 'carried_bid', reset_stale=False)
                        db.mark_options_position(position_id, None, 0, 'missing_bid')
                        counts['stale_mark_paths'] += 2
                        # Also execute a real zero liquidation mark and stops.
                        db.mark_options_position(position_id, 0, 0, 'zero_bid', ask=ask)
                        zero_pos = db.get_options_position(position_id)
                        for policy in policies:
                            options_allocator.effective_stop_level(zero_pos, policy, prev_mark=bid)
                            counts['zero_bid_stop_paths'] += 1
                        if not db.close_options_position(position_id, 0, exit_reason,
                                                         exit_bid=0, exit_ask=ask):
                            exclusions['close_rejected'] += 1
                        counts['exit_paths'] += 1
                    # Expiry scheduler must process many due rows at once.
                    db.open_options_position(1, scan, dict(candidate, expiration_date='2000-01-01'))
                except Exception as error:
                    exceptions[type(error).__name__] += 1
            try:
                with patch.object(options_engine, '_underlying_prices', return_value={}), \
                     patch.object(options_engine.schwab_mcp, 'market_data_enabled', return_value=False), \
                     patch.object(options_engine, '_yf_contract_price', return_value=None):
                    due = len(db.list_options_positions(1, status='open'))
                    summary = options_engine.refresh_positions(1)
                    counts['refresh_paths'] += 1
                    counts['expiry_scheduler_rows'] += due
                    if summary['settle']['due'] != due or db.list_options_positions(1, status='open'):
                        raise AssertionError('Expiry scheduler did not process all scratch rows')
                    options_engine.account_equity(1)
                    db.options_realized_pnl(1)
                    counts['equity_paths'] += 1
            except Exception as error:
                exceptions[type(error).__name__] += 1
        finally:
            db.DB_PATH = original_path
    return {'source_sha256': hashlib.sha256(export.read_bytes()).hexdigest(),
            'counts': dict(counts), 'exclusions': dict(exclusions),
            'exceptions': dict(exceptions), 'exception_count': sum(exceptions.values()),
            'outcome_values_suppressed': True,
            'limitations': ['Export-seeded disposable SQLite; never live DB.',
                'Entry bid/ask are real evidence; no historical exit quotes stored.',
                'Missing and zero quotes are supplemental edge scenarios.',
                'All stop policies and close reasons execute numeric persistence; no LLM/provider calls.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json-export', type=Path, required=True)
    parser.add_argument('--output-json', type=Path, required=True)
    args = parser.parse_args()
    if args.json_export.resolve() == args.output_json.resolve():
        parser.error('Output must differ from source')
    result = smoke(args.json_export)
    args.output_json.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    raise SystemExit(1 if result['exception_count'] else 0)


if __name__ == '__main__':
    main()
