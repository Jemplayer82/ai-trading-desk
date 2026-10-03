#!/usr/bin/env python3
"""Estimate historical bid/ask fills from a JSON export or an explicit SQLite copy.

No application modules, network calls, or database writes. Contract counts,
selection and exit times remain recorded: this is a fill sensitivity estimate,
not a replay. Primary estimates apply the exit spread to every closed row.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ASSUMPTIONS = [
    'Contract counts, selected contracts and exit times stay recorded; no sizing or stop replay.',
    'Entry fill is recorded ask; exit midpoint is recorded exit_premium.',
    'Exit relative spread equals (entry_ask-entry_bid)/entry_midpoint; estimated bid is exit midpoint times (1-relative spread/2).',
    'Entry fees are cost_basis minus recorded entry premium times 100 times contracts; exit fees are recorded exit premium times 100 times contracts minus exit_value.',
    'Primary estimates apply the same exit spread haircut to expiry rows, as required by the design; recorded cash settlement proceeds are a separate sensitivity.',
    'Only valid closed legacy rows enter the paired comparison; excluded rows do not contribute either PNL total.',
    'Already bid_ask rows are excluded to avoid applying a spread haircut twice.',
]
CLOSED_STATUSES = {'closed', 'expired_itm', 'expired_worthless', 'settled'}
CENT = Decimal('0.01')


def number(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not result.is_finite():
        return None
    magnitude = result.copy_abs()
    # Bound pathological exponents before arithmetic, keeping data errors out
    # of Decimal overflow/underflow traps and JSON float infinities.
    if magnitude > Decimal('1e15') or (magnitude and magnitude < Decimal('1e-12')):
        return None
    return result


def money(value: Decimal) -> float:
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


def estimate_position(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Data problems are counted exclusions, never exceptions."""
    if row.get('status') == 'open':
        return None, 'open_position'
    if row.get('status') not in CLOSED_STATUSES:
        return None, 'unsupported_status'
    if row.get('fill_model') == 'bid_ask':
        return None, 'already_bid_ask'
    fields = ['contracts', 'entry_premium', 'entry_bid', 'entry_ask',
              'cost_basis', 'exit_premium', 'exit_value', 'realized_pnl']
    values = {key: number(row.get(key)) for key in fields}
    if any(value is None for value in values.values()):
        return None, 'missing_or_nonfinite_numeric'
    n, entry, bid, ask, basis, exit_mid, exit_value, recorded = (values[k] for k in fields)
    if n <= 0 or n != n.to_integral_value():
        return None, 'invalid_contract_count'
    if min(entry, bid, ask, basis, exit_mid, exit_value) < 0 or ask <= 0 or bid > ask:
        return None, 'invalid_price_or_quote'
    multiplier = n * 100
    entry_fees = basis - entry * multiplier
    exit_fees = exit_mid * multiplier - exit_value
    # Recorded cents rounding can differ by at most one cent.
    if entry_fees < -CENT or exit_fees < -CENT:
        return None, 'inconsistent_fee_basis'
    entry_fees, exit_fees = max(Decimal(0), entry_fees), max(Decimal(0), exit_fees)
    if abs(recorded - (exit_value - basis)) > CENT:
        return None, 'inconsistent_recorded_pnl'
    spread = (ask - bid) / ((ask + bid) / 2)
    cash_settlement = row.get('status') in {'expired_itm', 'expired_worthless', 'settled'} or (
        row.get('exit_reason') == 'expiry' and row.get('settlement_close') is not None)
    estimated_exit = exit_mid * (1 - spread / 2)
    estimated_proceeds = estimated_exit * multiplier - exit_fees
    estimated_basis = ask * multiplier + entry_fees
    pnl = estimated_proceeds - estimated_basis
    return {
        'id': row.get('id'), 'paper_account_id': row.get('paper_account_id'),
        'recorded_pnl': money(recorded), 'estimated_pnl': money(pnl),
        'change': money(pnl - recorded), 'estimated_entry_premium': float(ask),
        'estimated_exit_premium': float(estimated_exit), 'entry_relative_spread': float(spread),
        'entry_fees_preserved': money(entry_fees), 'exit_fees_preserved': money(exit_fees),
        'cash_settlement': cash_settlement,
        'cash_settlement_sensitivity_pnl': money(exit_value - estimated_basis) if cash_settlement else money(pnl),
    }, None


def rescore(rows: list[dict[str, Any]]) -> dict[str, Any]:
    exclusions: Counter[str] = Counter()
    per_account: dict[str, dict[str, Any]] = {}
    details = []
    for row in rows:
        if not isinstance(row, dict):
            exclusions['invalid_row'] += 1
            continue
        account = str(row.get('paper_account_id', 'unknown'))
        summary = per_account.setdefault(account, {'paper_account_id': row.get('paper_account_id'),
            'included': 0, 'excluded': 0, 'exclusions': Counter(), 'recorded_pnl': Decimal(0),
            'estimated_pnl': Decimal(0), 'cash_settlement_sensitivity_pnl': Decimal(0), 'cash_settlements': 0})
        estimate, reason = estimate_position(row)
        if reason:
            exclusions[reason] += 1
            summary['excluded'] += 1
            summary['exclusions'][reason] += 1
            continue
        details.append(estimate)
        summary['included'] += 1
        summary['cash_settlements'] += int(estimate['cash_settlement'])
        summary['recorded_pnl'] += Decimal(str(estimate['recorded_pnl']))
        summary['estimated_pnl'] += Decimal(str(estimate['estimated_pnl']))
        summary['cash_settlement_sensitivity_pnl'] += Decimal(str(estimate['cash_settlement_sensitivity_pnl']))
    accounts = []
    for key in sorted(per_account):
        summary = per_account[key]
        summary['change'] = money(summary['estimated_pnl'] - summary['recorded_pnl'])
        summary['recorded_pnl'] = money(summary['recorded_pnl'])
        summary['estimated_pnl'] = money(summary['estimated_pnl'])
        summary['cash_settlement_sensitivity_pnl'] = money(summary['cash_settlement_sensitivity_pnl'])
        summary['exclusions'] = dict(summary['exclusions'])
        accounts.append(summary)
    total = {key: sum(item[key] for item in accounts) for key in
             ['included', 'excluded', 'cash_settlements']}
    total['excluded'] += exclusions['invalid_row']
    for key in ['recorded_pnl', 'estimated_pnl', 'change', 'cash_settlement_sensitivity_pnl']:
        total[key] = money(sum((Decimal(str(item[key])) for item in accounts), Decimal(0)))
    return {'estimate_only': True, 'rows_processed': len(rows), 'assumptions': ASSUMPTIONS,
            'accounts': accounts, 'total': total, 'exclusions': dict(exclusions), 'positions': details}


def markdown(result: dict[str, Any]) -> str:
    lines = ['# Historical bid/ask fill sensitivity — ESTIMATE', '',
             'Paired valid rows only. Recorded history remains unchanged.', '',
             '| Account | Included | Excluded | Recorded PNL | Estimated PNL | Change |',
             '| --- | ---: | ---: | ---: | ---: | ---: |']
    for item in result['accounts'] + [dict(result['total'], paper_account_id='TOTAL')]:
        lines.append(f"| {item['paper_account_id']} | {item['included']} | {item['excluded']} | "
                     f"${item['recorded_pnl']:,.2f} | ${item['estimated_pnl']:,.2f} | ${item['change']:,.2f} |")
    lines += ['', 'Separate sensitivity retaining recorded cash expiry proceeds (entry still crosses ask): '
              f"total estimated PNL ${result['total']['cash_settlement_sensitivity_pnl']:,.2f}."]
    lines += ['', '## Exclusions', '', json.dumps(result['exclusions'], sort_keys=True), '',
              '## ASSUMPTIONS', ''] + ['- ' + value for value in ASSUMPTIONS]
    return '\n'.join(lines) + '\n'


def load_rows(json_export: Path | None = None, sqlite_copy: Path | None = None) -> list[dict[str, Any]]:
    if (json_export is None) == (sqlite_copy is None):
        raise ValueError('Specify exactly one JSON export or SQLite copy')
    if json_export is not None:
        rows = json.loads(json_export.read_text())
        if not isinstance(rows, list):
            raise ValueError('JSON export must be a list of position objects')
        return rows
    # Caller explicitly certifies a standalone, non-live COPY. immutable avoids
    # locks, journal recovery and writes, including accidental WAL side effects.
    uri = sqlite_copy.resolve().as_uri() + '?mode=ro&immutable=1'
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute('SELECT * FROM options_positions')]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--json-export', type=Path)
    source.add_argument('--sqlite-copy', type=Path, help='Standalone COPY only; never a live desk database')
    parser.add_argument('--output-json', type=Path, required=True)
    parser.add_argument('--output-markdown', type=Path, required=True)
    args = parser.parse_args()
    source_path = args.json_export or args.sqlite_copy
    paths = [source_path.resolve(), args.output_json.resolve(), args.output_markdown.resolve()]
    if len(set(paths)) != 3:
        parser.error('Input and output paths must all be distinct')
    result = rescore(load_rows(args.json_export, args.sqlite_copy))
    result['source'] = {'path': str(source_path.resolve()),
                        'sha256': hashlib.sha256(source_path.read_bytes()).hexdigest()}
    args.output_json.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    args.output_markdown.write_text(markdown(result))
    print(markdown(result), end='')


if __name__ == '__main__':
    main()
