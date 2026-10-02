"""Hand-computed staged premium stops; no returns, network or live stores."""
import json
import random
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest

from web import account_policy as ap
from web import db

pytestmark = pytest.mark.unit


def policy(base=20, trigger=20, tight=10):
    return ap.validate_policy('trailing_staged', base, None, trigger, tight, kind='options')


@pytest.mark.parametrize('put_call', ['CALL', 'PUT'])
@pytest.mark.parametrize(('entry', 'peak', 'level'), [
    (10, 11.99, 9.592), (10, 12, 10.8), (10.3, 12.36, 11.124),
    (46.2, 55.44, 49.896), (10, 5, 8),
])
def test_hand_computed_premium_levels(put_call, entry, peak, level):
    # Premium-only policy is identical for either side of the option contract.
    assert put_call in ('CALL', 'PUT')
    out = ap.evaluate(policy(), entry=entry, peak=peak, mark=peak)
    assert out.level == level


def test_below_trigger_not_epsilon_band():
    assert ap.evaluate(policy(), entry=10, peak=12-1e-8, mark=12).level == 9.6


@pytest.mark.parametrize(('mark', 'previous', 'action', 'fill', 'crossed'), [
    (10.81, 12, 'hold', None, True),
    (10.8, 12, 'fill', 10.8, True),
    (7, 12, 'fill', 10.8, True),
    (7, None, 'fill', 7, False),
    (7, 10.8, 'fill', 7, False),
])
def test_at_under_gap_and_crossing(mark, previous, action, fill, crossed):
    out = ap.evaluate(policy(), entry=10, peak=12, mark=mark, prev_mark=previous)
    assert (out.action, out.fill_price, out.crossed) == (action, fill, crossed)
    assert out.limit_price is None
    assert out.exit_reason == ('trail_stop' if action == 'fill' else None)


def test_tightening_never_loosens():
    levels = [ap.evaluate(policy(), entry=10, peak=p, mark=p).level for p in (10, 11, 11.99, 12, 13, 15)]
    assert levels == [8, 8.8, 9.592, 10.8, 11.7, 13.5]
    assert levels == sorted(levels)


def test_equal_trails_byte_identity_on_1000_random_paths():
    rng = random.Random(20261002)
    staged = policy(tight=20)
    plain = ap.StopPolicy('trailing_pct', 20)
    for _ in range(1000):
        entry = rng.uniform(.05, 100)
        peak, previous, level = entry, None, None
        for _ in range(40):
            mark = entry * rng.uniform(.1, 3)
            peak = max(peak, mark)
            kwargs = dict(entry=entry, peak=peak, mark=mark, prev_mark=previous)
            assert json.dumps(asdict(ap.evaluate(staged, stop_level_hwm=level, **kwargs)), sort_keys=True) == json.dumps(
                asdict(ap.evaluate(plain, **kwargs)), sort_keys=True)
            level = ap.evaluate(staged, stop_level_hwm=level, **kwargs).level
            previous = mark


@pytest.mark.parametrize(('base', 'trigger', 'tight'), [
    (0, 20, 10), (100, 20, 10), (float('nan'), 20, 10), (float('inf'), 20, 10),
    (20, 0, 10), (20, -1, 10), (20, float('nan'), 10), (20, float('inf'), 10),
    (20, '', 10), (20, 20, ''), (20, 20, .99), (20, 20, 99.01),
    (20, 20, float('nan')), (20, 20, float('inf')), (20, 'abc', 10),
])
def test_validation_bounds(base, trigger, tight):
    with pytest.raises(ValueError):
        policy(base, trigger, tight)


def test_defaults_discard_and_runtime_kind():
    p = ap.validate_policy('trailing_staged', 20, 5, kind='options')
    assert p == ap.StopPolicy('trailing_staged', 20, None, 20, 10)
    assert policy(tight=5).stage_trail_pct == 5
    assert policy(tight=20).stage_trail_pct == 20
    assert policy(trigger=150).stage_trigger_pct == 150
    for kind in ('equity', None):
        with pytest.raises(ValueError, match='options accounts only'):
            ap.validate_policy('trailing_staged', 20, None, kind=kind)
        assert ap.StopPolicy.from_account(dict(stop_type='trailing_staged', stop_value=20, kind=kind)) == ap.NONE
    assert ap.StopPolicy.from_account(dict(stop_type='trailing_staged', stop_value=20, kind='options')) == p
    assert ap.StopPolicy.from_account(dict(stop_type='trailing_staged', stop_value=20, kind='options', stage_trail_pct=100)) == ap.NONE
    assert ap.validate_policy('trailing_pct', 20, None, 20, 10) == ap.StopPolicy('trailing_pct', 20)
    assert ap.describe_policy(p) == ('Sells if the price falls 20% from its highest point; once the position is '
                                     'up 20%, the trail becomes 10% below the highest point. The stop level never falls; edits apply to open positions at the next evaluation.')


def test_additive_migration_on_pre_staged_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'schema-copy.db')
    # Copy current schema minus the new nullable fields; no production DB access.
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.executescript(db.SCHEMA.replace(',\n    stage_trigger_pct REAL,\n    stage_trail_pct REAL', ''))
        conn.execute("INSERT INTO paper_accounts (name,created_at,kind,stop_type,stop_value) VALUES ('bull','2026-10-02','options','trailing_pct',20)")
        before = conn.execute('SELECT * FROM paper_accounts').fetchall()
    db.init_db()
    db.init_db()
    with sqlite3.connect(db.DB_PATH) as conn:
        columns = conn.execute('PRAGMA table_info(paper_accounts)').fetchall()
        rows = conn.execute('SELECT * FROM paper_accounts').fetchall()
        assert rows == [(*before[0], None, None)]
        assert [c[1] for c in columns][-2:] == ['stage_trigger_pct', 'stage_trail_pct']
        assert all(c[3] == 0 and c[4] is None for c in columns[-2:])
    aid = db.create_paper_account('staged', kind='options', stop_type='trailing_staged', stop_value=20,
                                 stage_trigger_pct=20, stage_trail_pct=10)
    assert db.get_paper_account(aid)['stage_trail_pct'] == 10
    assert db.list_paper_accounts(kind='options')[-1]['stage_trigger_pct'] == 20
    db.update_paper_account(aid, stage_trail_pct=5)
    assert db.get_paper_account(aid)['stage_trigger_pct'] == 20
    db.update_paper_account(aid, stage_trigger_pct=None, stage_trail_pct=None)
    assert db.get_paper_account(aid)['stage_trail_pct'] is None


@pytest.mark.parametrize('trail', [5, 10, 40])
def test_real_bull_replay_daily_close_cross_check(trail):
    data = json.loads((Path(__file__).parent / 'fixtures/staged-stop-bull.json').read_text())
    for trade in data['trades']:
        peak, previous = trade['entry_premium'], None
        triggered, level = None, None
        expected = trade['expected_by_trail'][str(trail)]
        for row in trade['quotes']:
            mid = (row['bid'] + row['ask']) / 2
            peak = max(peak, mid)
            out = ap.evaluate(policy(tight=trail), entry=trade['entry_premium'], peak=peak, mark=mid, prev_mark=previous, stop_level_hwm=level)
            level = out.level
            if out.action == 'fill':
                triggered = row['session']
                assert out.level == pytest.approx(expected['final_stop_level'], abs=0.00005)
                break
            previous = mid
        assert triggered == expected['trigger_session']
        # Replay fills at the next valid session bid, unlike immediate desk fills.
        next_valid = next(r['session'] for r in trade['quotes'] if r['session'] > triggered)
        assert next_valid == expected['exit_session']


@pytest.mark.parametrize('trail', [1, 1.5, 40, 99])
def test_any_stage_trail_inclusive(trail):
    assert policy(tight=trail).stage_trail_pct == trail
    assert 'tightens' not in ap.describe_policy(policy(tight=trail))


def test_looser_stage_ratchets_as_peak_rises_and_settings_change():
    level = None
    levels = []
    for peak in (10, 11.99, 12, 13, 16, 20):
        level = ap.evaluate(policy(tight=40), entry=10, peak=peak, mark=peak, stop_level_hwm=level).level
        levels.append(level)
    assert levels == [8, 9.592, 9.592, 9.592, 9.6, 12]
    tighter = ap.evaluate(policy(tight=5), entry=10, peak=20, mark=20, stop_level_hwm=level)
    assert tighter.level == 19
    looser = ap.evaluate(policy(base=40, trigger=200, tight=99), entry=10, peak=20, mark=18,
                         prev_mark=20, stop_level_hwm=tighter.level)
    assert (looser.level, looser.action, looser.fill_price) == (19, 'fill', 19)


@pytest.mark.parametrize('stop_type', ['stop', 'stop_limit', 'trailing_pct', 'trailing_dollar', 'none'])
def test_other_types_ignore_saved_ratchet(stop_type):
    p = ap.StopPolicy(stop_type, 20, 10)
    kwargs = dict(entry=10, peak=12, mark=11)
    assert ap.evaluate(p, **kwargs) == ap.evaluate(p, stop_level_hwm=100, **kwargs)


def test_ratchet_migration_preserves_old_open_row_null(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'pre-ratchet.db')
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.executescript(db.SCHEMA.replace('    stop_level_hwm REAL,\n', ''))
        conn.execute("INSERT INTO paper_accounts (id,name,created_at,kind) VALUES (1,'old','2026-10-02','options')")
        conn.execute("""INSERT INTO options_positions
            (paper_account_id,open_scan_id,occ_symbol,underlying,put_call,strike,expiration_date,
             contracts,entry_premium,cost_basis,opened_at,peak_premium)
            VALUES (1,1,'TEST','TEST','CALL',100,'2026-11-20',1,10,1000,'2026-10-02',12)""")
        before = conn.execute('SELECT * FROM options_positions').fetchone()
    db.init_db()
    db.init_db()
    with sqlite3.connect(db.DB_PATH) as conn:
        after = conn.execute('SELECT * FROM options_positions').fetchone()
        assert after == (*before, None)
        col = next(c for c in conn.execute('PRAGMA table_info(options_positions)') if c[1] == 'stop_level_hwm')
        assert col[2:5] == ('REAL', 0, None)
