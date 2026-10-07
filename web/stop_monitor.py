"""Two-second BID stop monitor for PAPER positions only.

No real brokerage order placement/modification path is implemented. The provider
has only get_quotes. Paper closing is the default; --dry-run is for verification.
SQLite BEGIN IMMEDIATE is the same cross-process writer lock used by the scan's
close path. Close, cash credit and trigger audit commit together.

Intentional difference from the hourly scan: this monitor skips a 0.00 bid (counted as
'invalid_bid') and never sells at zero, while web/options_fills.quote_bid treats a 0.00 bid with
a positive ask as a real no-bid market that the hourly path may fill at $0. The hourly scan stays
the fallback for those contracts.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import account_policy, db, market_calendar

log = logging.getLogger(__name__)
INTERVAL = 2.0
MIN_GAP = 1.0  # hard floor between two polls; main() paces the 2 s cadence


def iso_z(moment):
    """The desk's UTC timestamp form (…Z), same as every other options_positions timestamp."""
    return moment.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    timestamp: float
    age: float


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def quote_timestamp(value):
    """Schwab MCP normalizes timestamps to ISO; raw API uses epoch milliseconds."""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is not None:
                return parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    numeric = number(value)
    return numeric / 1000 if numeric is not None else None


def validate_quote(raw, now):
    """Data defects become counted exclusions, never numeric exceptions."""
    if not isinstance(raw, dict) or not isinstance(raw.get('quote'), dict):
        return None, 'missing_quote'
    q = raw['quote']
    bid, ask = (number(q.get(k)) for k in ('bidPrice', 'askPrice'))
    stamp = quote_timestamp(q.get('quoteTime'))
    if bid is None or bid <= 0:
        return None, 'invalid_bid'
    if ask is None or ask <= 0:
        return None, 'invalid_ask'
    if bid >= ask:
        return None, 'crossed_or_locked'
    if stamp is None or stamp <= 0:
        return None, 'invalid_quote_time'
    age = now.timestamp() - stamp
    if age > 20:
        return None, 'stale_quote'
    if age < -2:
        return None, 'future_quote'
    return Quote(bid, ask, stamp, age), None


def market_open(now):
    et = now.astimezone(ZoneInfo('America/New_York'))
    return market_calendar.is_trading_day(et.date()) and (9, 30) <= (et.hour, et.minute) < (16, 0)


class StopMonitor:
    def __init__(self, provider, *, dry_run=False, kill_file=None, heartbeat_file=None, notify=None):
        self.notify = notify
        self.provider = provider
        self.dry_run = dry_run
        self.kill_file = Path(kill_file) if kill_file else db.DB_PATH.parent / 'stop-monitor.kill'
        self.heartbeat_file = Path(heartbeat_file) if heartbeat_file else db.DB_PATH.parent / 'stop-monitor.heartbeat.json'
        self.failures = 0
        self.alerted = False
        self.last_call = None
        # Dry-run state remains in memory: no position, policy, ledger or audit writes.
        self.shadow = {}
        self.simulated_closed = set()
        if not dry_run:
            with db.connect() as conn:
                conn.executescript('''
                CREATE TABLE IF NOT EXISTS stop_monitor_audit (
                    position_id INTEGER PRIMARY KEY, trigger_time TEXT NOT NULL,
                    quote_time REAL NOT NULL, bid REAL NOT NULL, ask REAL NOT NULL,
                    latency REAL NOT NULL, policy TEXT NOT NULL, peak REAL NOT NULL,
                    level REAL NOT NULL, exit_reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS stop_monitor_alerts (
                    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, failures INTEGER NOT NULL, reason TEXT NOT NULL);
                ''')

    def _read(self):
        # Reads never open a writer connection or run migrations.
        with sqlite3.connect(f'{db.DB_PATH.resolve().as_uri()}?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            positions = [dict(r) for r in conn.execute("SELECT * FROM options_positions WHERE status='open'")]
            accounts = {r['id']: dict(r) for r in conn.execute("SELECT * FROM paper_accounts WHERE kind='options'")}
        return positions, accounts

    def _evaluate(self, pos, account, quote):
        policy = account_policy.StopPolicy.from_account(account)
        contracts, cost = number(pos.get('contracts')), number(pos.get('cost_basis'))
        if contracts is None or contracts <= 0 or not contracts.is_integer() or cost is None or cost < 0:
            return None, 'invalid_position'
        try:
            if int(pos['contracts']) != contracts:
                return None, 'invalid_position'
        except (ValueError, TypeError, OverflowError):
            return None, 'invalid_position'
        if not math.isfinite(quote.bid * 100 * contracts):
            return None, 'invalid_position'
        entry = number(pos.get('entry_bid'))
        if entry is None or entry <= 0:
            return None, 'missing_entry_bid'
        peak = number(pos.get('peak_premium'))
        if pos.get('peak_premium') is not None and peak is None:
            return None, 'invalid_peak'
        peak = max(entry, peak or entry, quote.bid)
        hwm = number(pos.get('stop_level_hwm'))
        if pos.get('stop_level_hwm') is not None and hwm is None:
            return None, 'invalid_level'
        if any(v is not None and number(v) is None for v in asdict(policy).values() if not isinstance(v, str)):
            return None, 'invalid_policy'
        result = account_policy.evaluate(policy, entry=entry, peak=peak, mark=quote.bid,
                                        armed=bool(pos.get('stop_triggered_at')), stop_level_hwm=hwm)
        if not math.isfinite(result.level):
            return None, 'invalid_level'
        return (policy, peak, result), None

    def _apply(self, pos, account, quote, now, latency, live_clock=False):
        if self.dry_run:
            if pos['id'] in self.simulated_closed:
                return 'already_closed'
            pos = dict(pos, **self.shadow.get(pos['id'], {}))
            evaluated, reason = self._evaluate(pos, account, quote)
            if reason:
                return reason
            policy, peak, outcome = evaluated
            self.shadow[pos['id']] = dict(peak_premium=peak,
                stop_level_hwm=max(number(pos.get('stop_level_hwm')) or 0, outcome.level) if policy.stop_type == 'trailing_staged' else pos.get('stop_level_hwm'),
                stop_triggered_at=pos.get('stop_triggered_at') or (iso_z(now) if outcome.action == 'arm' else None))
            if outcome.action == 'fill':
                self.simulated_closed.add(pos['id'])
                return 'would_close'
            return 'hold'
        with db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if self.kill_file.exists():
                return 'killed'
            if live_clock:
                now = datetime.now(timezone.utc)
                if now.timestamp() - quote.timestamp > 20:
                    return 'stale_quote'
                if not market_open(now):
                    return 'market_closed'
            row = conn.execute("SELECT * FROM options_positions WHERE id=? AND status='open'", (pos['id'],)).fetchone()
            if row is None:
                return 'already_closed'
            row = dict(row)
            account_row = conn.execute('SELECT * FROM paper_accounts WHERE id=?', (row['paper_account_id'],)).fetchone()
            evaluated, reason = self._evaluate(row, dict(account_row) if account_row else {}, quote)
            if reason:
                return reason
            policy, peak, outcome = evaluated
            conn.execute("UPDATE options_positions SET peak_premium=?, stop_level_hwm=?, stop_triggered_at=?, "
                         "current_premium=?, current_value=?, current_bid=?, current_ask=?, current_mid=?, "
                         "last_marked_at=?, price_source='realtime_bid', stale_count=0 WHERE id=? AND status='open'",
                         (peak, max(number(row.get('stop_level_hwm')) or 0, outcome.level) if policy.stop_type == 'trailing_staged' else row.get('stop_level_hwm'),
                          row.get('stop_triggered_at') or (iso_z(now) if outcome.action == 'arm' else None),
                          round(quote.bid, 4), round(quote.bid * 100 * int(row['contracts']), 2), quote.bid, quote.ask,
                          round((quote.bid + quote.ask) / 2, 4),
                          datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'), row['id']))
            if outcome.action == 'fill':
                ok = db.close_options_position(row['id'], quote.bid, outcome.exit_reason,
                    closed_at=iso_z(now), exit_bid=quote.bid, exit_ask=quote.ask, transaction=conn)
                if ok:
                    conn.execute('INSERT INTO stop_monitor_audit VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (row['id'], iso_z(now), quote.timestamp, quote.bid, quote.ask, latency,
                         json.dumps(asdict(policy), sort_keys=True), peak, outcome.level, outcome.exit_reason))
                conn.commit()
                if ok and self.notify:
                    self.notify('Paper option stop', f"{row['occ_symbol']}: {outcome.exit_reason}")
                return 'closed' if ok else 'already_closed'
            conn.commit()
            return 'hold'

    def poll(self, now=None, *, force_market=False):
        explicit_now = now is not None
        now = now or datetime.now(timezone.utc)
        report = dict(closed=0, would_close=0, rows_processed=0, exclusions={}, failed=False,
                      market_closed=False, killed=False, rate_limited=False, latency=[], quote_age=[])
        exclusions = Counter()
        try:
            if self.kill_file.exists():
                report['killed'] = True
                return report
            if not force_market and not market_open(now):
                report['market_closed'] = True
                return report
            positions, accounts = self._read()
            positions = [p for p in positions if account_policy.StopPolicy.from_account(accounts.get(p['paper_account_id'])).stop_type != 'none']
            symbols = sorted({p['occ_symbol'] for p in positions})
            quotes = {}
            for start in range(0, len(symbols), 100):
                tick = time.monotonic()
                if self.last_call is not None and start == 0 and tick - self.last_call < MIN_GAP:
                    report['rate_limited'] = True
                    return report
                if self.last_call is not None and start > 0 and tick - self.last_call < INTERVAL:
                    time.sleep(INTERVAL - (tick - self.last_call))
                if self.kill_file.exists():
                    report['killed'] = True
                    return report
                self.last_call = time.monotonic()
                try:
                    response = self.provider.get_quotes(symbols[start:start + 100])
                except Exception:
                    response = None
                elapsed = time.monotonic() - self.last_call
                report['latency'].append(elapsed)
                if not isinstance(response, dict):
                    exclusions['api_error'] += 1
                else:
                    quotes.update(response)
            # Production validates against receipt time, including network delay.
            received = now if explicit_now else datetime.now(timezone.utc)
            for pos in positions:
                report['rows_processed'] += 1
                quote, reason = validate_quote(quotes.get(pos['occ_symbol']), received)
                if reason:
                    exclusions[reason] += 1
                    continue
                report['quote_age'].append(quote.age)
                if self.kill_file.exists():
                    report['killed'] = True
                    break
                action = self._apply(pos, accounts.get(pos['paper_account_id']), quote, received, sum(report['latency']), live_clock=not explicit_now and not force_market)
                if action in ('closed', 'would_close'):
                    report[action] += 1
                elif action not in ('hold', 'already_closed'):
                    exclusions[action] += 1
            # Only a data-feed outage counts toward the alert: per-contract exclusions (a 0.00 bid on a
            # deep-OTM option, a stale quote after an early close) are normal and are reported, not alerted.
            # A response with no usable quote for ANY position (an error dict, every symbol null) is an outage too.
            outage = (bool(exclusions.get('api_error')) or bool(symbols and not quotes)
                      or bool(report['rows_processed'] and exclusions.get('missing_quote', 0) >= report['rows_processed']))
            report['failed'] = outage
            if outage:
                self.failures += 1
            else:
                if self.alerted and not self.dry_run:
                    with db.connect() as conn:
                        conn.execute('INSERT INTO stop_monitor_alerts(ts,failures,reason) VALUES (?,?,?)',
                                     (iso_z(received), 0, json.dumps({'recovered_after_failures': self.failures})))
                    if self.notify:
                        self.notify('Paper stop monitor recovered', 'Quotes are flowing again.')
                self.failures = 0
                self.alerted = False
            if self.failures >= 3 and not self.alerted and not self.dry_run:
                self.alerted = True
                with db.connect() as conn:
                    conn.execute('INSERT INTO stop_monitor_alerts(ts,failures,reason) VALUES (?,?,?)',
                                 (iso_z(received), self.failures, json.dumps(dict(exclusions), sort_keys=True)))
                log.error('Stop monitor failed three polls; hourly scan remains fallback')
                if self.notify:
                    self.notify('Paper stop monitor degraded', 'Three failed polls; hourly scan remains fallback.')
            report['exclusions'] = dict(exclusions)
            return report
        finally:
            self.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.heartbeat_file.with_suffix('.tmp')
            temporary.write_text(json.dumps(dict(timestamp=iso_z(now), consecutive_failures=self.failures, **report), sort_keys=True))
            temporary.replace(self.heartbeat_file)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--db', type=Path)
    parser.add_argument('--kill-file', type=Path)
    parser.add_argument('--heartbeat-file', type=Path)
    args = parser.parse_args()
    if args.db:
        db.DB_PATH = args.db
    if not db.DB_PATH.exists():
        parser.error('Desk DB must already exist; monitor does not create or migrate the desk')
    # One runner per shared desk DB across containers; never queue two providers.
    import fcntl

    from tradingagents.dataflows import schwab_mcp

    from . import alerts
    lock = (db.DB_PATH.parent / 'stop-monitor.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('Another stop-monitor runner already owns this DB')
    monitor = StopMonitor(schwab_mcp, dry_run=args.dry_run, kill_file=args.kill_file, heartbeat_file=args.heartbeat_file,
                          notify=None if args.dry_run else alerts.notify)
    while True:
        started = time.monotonic()
        report = monitor.poll()
        if report['killed']:
            break
        time.sleep(max(0, INTERVAL - (time.monotonic() - started)))


if __name__ == '__main__':
    main()
