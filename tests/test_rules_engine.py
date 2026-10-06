"""Rules-only options account (web/rules_engine.py): the frozen strategy-lab rules, with fake
feeds (no network, no LLM)."""
from datetime import date, timedelta

import pytest

from web import db
from web import rules_engine as re_

pytestmark = pytest.mark.unit


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    re_.init_tables()
    return tmp_path / "web.db"


def _cand(strike, dte, bid, ask, delta, exp=None, ref=date(2026, 10, 6)):
    exp = exp or (ref + timedelta(days=dte)).isoformat()
    return {"occ_symbol": f"XYZ   {exp}C{strike}", "underlying": "XYZ", "put_call": "CALL", "strike": strike,
            "expiration_date": exp, "dte": dte, "bid": bid, "ask": ask, "delta": delta, "underlying_price": 100.0}


# ── pure rules ───────────────────────────────────────────────────────────────

def test_pead_signal_needs_beat_and_up_move():
    closes = {date(2026, 10, 1): 100.0, date(2026, 10, 2): 101.0, date(2026, 10, 5): 99.0}
    # report on 10/02 (either before open or after close): prior 10/01, reaction 10/05
    assert re_.pead_signal(date(2026, 10, 2), 5.0, closes) == (None, "stock did not rise")
    closes[date(2026, 10, 5)] = 100.5
    assert re_.pead_signal(date(2026, 10, 2), 5.0, closes) == (date(2026, 10, 5), "")
    assert re_.pead_signal(date(2026, 10, 2), 0.0, closes)[0] is None  # strictly > 0
    assert re_.pead_signal(date(2026, 10, 2), None, closes)[0] is None
    assert re_.pead_signal(date(2026, 10, 5), 5.0, closes) == (None, "reaction session not closed yet")


def test_congress_days_skip_weekend_and_holiday():
    # filed Friday 10/02/2026 -> signal Monday 10/05, entry Tuesday 10/06
    assert re_.congress_days(date(2026, 10, 2)) == (date(2026, 10, 5), date(2026, 10, 6))
    # filed Wednesday 11/25/2026 -> Thanksgiving 11/26 closed -> signal 11/27, entry 11/30
    assert re_.congress_days(date(2026, 11, 25)) == (date(2026, 11, 27), date(2026, 11, 30))


def test_pick_contract_expiry_spread_delta_and_earnings():
    entry = date(2026, 10, 6)
    cands = [
        _cand(100, 59, 5.00, 5.10, 0.52),   # expiry closest to 60; spread 2%
        _cand(105, 59, 3.00, 3.06, 0.41),
        _cand(95, 59, 7.00, 7.50, 0.63),    # spread 7% -> dropped
        _cand(100, 73, 6.00, 6.05, 0.55),   # other expiry, ignored
        _cand(100, 40, 4.00, 4.02, 0.50),   # under 45 days
    ]
    pick, why = re_.pick_contract(cands, entry, [])
    assert why == "" and pick["strike"] == 100 and pick["dte"] == 59
    # earnings between entry and expiry-7 blocks the trade
    pick, why = re_.pick_contract(cands, entry, [entry + timedelta(days=30)])
    assert pick is None and why == "earnings before exit"
    # an earnings date AFTER the planned exit is fine
    assert re_.pick_contract(cands, entry, [entry + timedelta(days=55)])[0] is not None
    # no fallback to another expiry when the chosen one has no tight spread
    wide = [_cand(100, 60, 5.0, 5.5, 0.5), _cand(100, 75, 5.0, 5.05, 0.5)]
    assert re_.pick_contract(wide, entry, []) == (None, "spread over 3%")
    # delta outside 0.40-0.60 -> no trade
    far = [_cand(100, 60, 5.0, 5.05, 0.68)]
    assert re_.pick_contract(far, entry, []) == (None, "no contract near delta 0.50")
    # zero bid / missing delta are not usable
    assert re_.pick_contract([_cand(100, 60, 0.0, 0.1, 0.5), _cand(100, 60, 1, 1.02, None)], entry, [])[0] is None


def test_costs_sizing_and_trail():
    assert re_.cost_per(5.10) == pytest.approx(510.65)
    assert re_.value_per(5.0) == pytest.approx(499.35)
    assert re_.value_per(0.0) == 0.0
    # 2% of 50k = $1,000 -> 1 contract at $510.65; capped by liquid money
    assert re_.size(50_000, 50_000, 510.65) == 1
    assert re_.size(50_000, 300, 210.65) == 1
    assert re_.size(50_000, 50_000, 1_200) == 0
    cost = 500.0
    armed, peak, sell = re_.exit_decision(False, None, 740.0, cost)
    assert (armed, sell) == (False, False)
    armed, peak, sell = re_.exit_decision(False, None, 760.0, cost)   # arms, no sell same day
    assert (armed, peak, sell) == (True, 760.0, False)
    armed, peak, sell = re_.exit_decision(True, 760.0, 900.0, cost)
    assert (peak, sell) == (900.0, False)
    armed, peak, sell = re_.exit_decision(True, 900.0, 811.0, cost)
    assert sell is False
    assert re_.exit_decision(True, 900.0, 810.0, cost)[2] is True      # 10% below the best


def test_switch_uses_yesterday_close_vs_200_day():
    days, d = [], date(2025, 6, 2)
    while len(days) < 201:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    closes = {x: 100.0 for x in days[:-1]}
    closes[days[-1]] = 90.0          # today's (partial) price must be ignored
    today = days[-1]
    assert re_.switch_on(closes, today) is False   # 100 is not ABOVE a 100 average
    closes[days[-2]] = 101.0
    assert re_.switch_on(closes, today) is True
    assert re_.switch_on({days[0]: 1.0}, today) is None


def test_tie_key_matches_backtest_format():
    import zlib
    assert re_.tie_key("AAPL", date(2024, 1, 23)) == zlib.crc32(b"AAPL|2024-01-23 00:00:00")


# ── daily run with fake feeds ────────────────────────────────────────────────

class FakeFeeds:
    def __init__(self, today):
        days, d = [], today - timedelta(days=400)
        while d < today:
            if d.weekday() < 5:
                days.append(d)
            d += timedelta(days=1)
        self.spy = {x: 100.0 + i * 0.1 for i, x in enumerate(days)}  # rising: switch on
        self.chains = {}
        self.q = {"SPY": {"last": 500.0}}
        self.calls = []

    def as_feeds(self):
        return re_.Feeds(sp500=lambda: [], av=lambda *a, **k: None,
                         daily_closes=lambda s: self.spy, call_chain=self.chain, quotes=self.quotes)

    def chain(self, t, ref):
        self.calls.append(t)
        return self.chains.get(t, [])

    def quotes(self, syms):
        return {s: self.q[s] for s in syms if s in self.q}


def _signal(aid, system, ticker, entry, rank=-1.0):
    with db.connect() as conn:
        conn.execute("INSERT INTO rules_signals (account_id, system, ticker, signal_date, entry_date, rank, status, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, 'pending', 'x')",
                     (aid, system, ticker, (entry - timedelta(days=1)).isoformat(), entry.isoformat(), rank))


def _rows(sql, *args):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def test_daily_run_opens_in_priority_order_and_respects_slots(tmp_db):
    today = date(2026, 10, 6)
    aid = re_.create_account("Rules", 50_000)
    f = FakeFeeds(today)
    for t in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"):
        f.chains[t] = [dict(_cand(100, 60, 4.0, 4.1, 0.5), occ_symbol=f"{t:<6}261205C00100000", underlying=t)]
    for t in ("AAA", "BBB", "CCC", "DDD", "EEE", "GGG"):
        _signal(aid, "congress", t, today)
    _signal(aid, "pead", "FFF", today, rank=-9.0)
    _signal(aid, "pead", "AAA", today, rank=-1.0)        # AAA: pead takes it, congress skipped
    _signal(aid, "congress", "ZZZ", today - timedelta(days=1))  # entry day passed -> missed
    out = re_.run_daily(f.as_feeds(), today)
    res = out["accounts"]["Rules"]
    assert out["switch_on"] is True
    sig = {(r["system"], r["ticker"]): (r["status"], r["reason"]) for r in _rows("SELECT * FROM rules_signals")}
    assert sig[("pead", "FFF")][0] == "opened" and sig[("pead", "AAA")][0] == "opened"
    assert sig[("congress", "AAA")] == ("skipped", "already holding this stock")
    assert sig[("congress", "ZZZ")][0] == "missed"
    opened_congress = [k for k, v in sig.items() if k[0] == "congress" and v[0] == "opened"]
    assert len(opened_congress) == 4                    # max 4 per signal type
    assert [k for k, v in sig.items() if v == ("skipped", "no open slot")]
    assert f.calls[:2] == ["FFF", "AAA"]                # pead first, biggest surprise first
    pos = _rows("SELECT * FROM rules_positions")
    assert len(pos) == 6 and all(p["contracts"] == 2 for p in pos)  # 2% of ~50k / $410.65
    assert pos[0]["cost_per"] == pytest.approx(410.65)
    acct = _rows("SELECT * FROM rules_accounts")[0]
    assert acct["cash"] == 0.0 and acct["spy_shares"] == pytest.approx((50_000 - 6 * 2 * 410.65) / 500.0)
    assert res["opened"] == 6
    # one run per day
    assert re_.run_daily(f.as_feeds(), today, force=True)["accounts"]["Rules"] == {"skipped": "already ran today"}


def test_daily_run_trail_time_exit_and_cash_switch(tmp_db):
    today = date(2026, 10, 6)
    aid = re_.create_account("Rules", 50_000)
    f = FakeFeeds(today)
    occ = "XYZ   261205C00100000"
    f.chains["XYZ"] = [dict(_cand(100, 60, 4.0, 4.1, 0.5), occ_symbol=occ, underlying="XYZ")]
    _signal(aid, "pead", "XYZ", today)
    re_.run_daily(f.as_feeds(), today)
    days = [today + timedelta(days=k) for k in (1, 2, 3)]
    # day 1: up 60% at the bid -> arms (no sell); day 2: new high; day 3: 10% below the high -> sell
    for d, bid in zip(days, (6.60, 8.00, 7.19), strict=True):
        f.q[occ] = {"bid": bid, "ask": bid + 0.05}
        re_.run_daily(f.as_feeds(), d)
    p = _rows("SELECT * FROM rules_positions")[0]
    assert p["status"] == "closed" and p["exit_reason"] == "trailing_stop"
    assert p["exit_value"] == pytest.approx(719 - 0.65)
    assert p["pnl"] == pytest.approx(2 * (718.35 - 410.65))
    # switch off: idle money moves to cash
    f.spy[max(f.spy)] = 1.0
    d = today + timedelta(days=6)
    re_.run_daily(f.as_feeds(), d)
    acct = _rows("SELECT * FROM rules_accounts")[0]
    assert acct["spy_shares"] == 0.0 and acct["cash"] > 0
    daily = _rows("SELECT * FROM rules_daily ORDER BY run_date")
    assert daily[-1]["switch_on"] == 0


def test_time_exit_uses_intrinsic_when_unquoted(tmp_db):
    today = date(2026, 10, 6)
    aid = re_.create_account("Rules", 50_000)
    f = FakeFeeds(today)
    occ = "XYZ   261205C00100000"
    f.chains["XYZ"] = [dict(_cand(100, 60, 4.0, 4.1, 0.5), occ_symbol=occ, underlying="XYZ")]
    _signal(aid, "pead", "XYZ", today)
    re_.run_daily(f.as_feeds(), today)
    planned = date.fromisoformat(_rows("SELECT planned_exit FROM rules_positions")[0]["planned_exit"])
    f.q["XYZ"] = {"last": 103.0}                         # option unquoted, stock at 103
    re_.run_daily(f.as_feeds(), re_.session_on_or_before(planned))
    p = _rows("SELECT * FROM rules_positions")[0]
    assert p["exit_reason"] == "time_exit" and p["exit_value"] == pytest.approx(300 - 0.65)


def test_prepare_writes_pead_and_congress_signals(tmp_db):
    today = date(2026, 10, 6)                             # Tuesday
    re_.create_account("Rules", 50_000)
    cal = "symbol,name,reportDate,fiscalDateEnding,estimate,currency\nAAA,A,2026-10-05,2026-09-30,1,USD\n" \
          "BBB,B,2026-12-01,2026-09-30,1,USD\nQQQQ,Q,2026-10-05,2026-09-30,1,USD\n"
    earnings = {"quarterlyEarnings": [{"reportedDate": "2026-10-05", "surprisePercentage": "4.2", "reportTime": "post-market"}]}
    congress = {"trades": [
        {"bioguide_id": "X1", "transaction_type": "BUY", "filed_date": "2026-10-05", "transaction_date": "2026-09-20"},
        {"bioguide_id": "X2", "transaction_type": "SELL", "filed_date": "2026-10-05", "transaction_date": "2026-09-21"},
        {"bioguide_id": "X3", "transaction_type": "BUY", "filed_date": "2025-01-05", "transaction_date": "2025-01-02"},
    ]}

    def av(function, **p):
        if function == "EARNINGS_CALENDAR":
            return cal
        if function == "EARNINGS":
            return earnings if p["symbol"] == "AAA" else {}
        return congress if p["symbol"] == "BBB" else {"trades": []}

    closes = {date(2026, 10, 2): 50.0, date(2026, 10, 5): 51.0, date(2026, 10, 6): 52.0}
    feeds = re_.Feeds(sp500=lambda: ["AAA", "BBB", "BRK-B"], av=av, daily_closes=lambda t: closes,
                      call_chain=lambda *a: [], quotes=lambda s: {})
    out = re_.prepare(feeds, today)
    assert out["pead"] == 1 and out["congress"] == 1 and not out["errors"]
    sig = {(r["system"], r["ticker"]): r for r in _rows("SELECT * FROM rules_signals")}
    # AAA reported 10/05 after the close: reaction 10/06 > prior 10/05 -> entry 10/07
    assert sig[("pead", "AAA")]["signal_date"] == "2026-10-06" and sig[("pead", "AAA")]["entry_date"] == "2026-10-07"
    assert sig[("pead", "AAA")]["rank"] == pytest.approx(-4.2) and sig[("pead", "AAA")]["status"] == "pending"
    # BBB filed 10/05 -> signal 10/06 -> entry 10/07; the SELL and the 2025 filing make no signal
    assert sig[("congress", "BBB")]["entry_date"] == "2026-10-07"
    assert len(sig) == 2
    # the forward calendar date is kept for the earnings-before-exit filter; non-S&P rows dropped
    assert {r["ticker"] for r in _rows("SELECT * FROM rules_calendar")} == {"AAA", "BBB"}
    # rerun is idempotent: nothing new
    out2 = re_.prepare(feeds, today)
    assert out2["congress"] == 0 and len(_rows("SELECT * FROM rules_signals")) == 2


def test_rules_tables_are_separate_from_ai_accounts(tmp_db):
    re_.create_account("Rules", 50_000)
    assert db.list_paper_accounts() == []                # invisible to every AI allocator path
