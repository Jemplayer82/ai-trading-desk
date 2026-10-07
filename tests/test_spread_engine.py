"""Rules-only credit-spread accounts (web/spread_engine.py): scan rule, live-quote touch fills,
walking limits, take-profit, expiry settlement, sizing caps. Fake feeds only (no network, no LLM)."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from web import db
from web import spread_engine as se

pytestmark = pytest.mark.unit


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    se.init_tables()
    monkeypatch.setattr(se, "_alert", lambda *a, **k: None)
    return tmp_path / "web.db"


REF = date(2026, 10, 7)                    # Wednesday
FRI = "2026-10-30"                         # 23 days out, a Friday


def _contract(sym, side, k, bid, ask, iv=30.0, exp=FRI):
    return {"symbol": sym, "putCall": side, "strikePrice": k, "bid": bid, "ask": ask, "volatility": iv}


def _payload(spot, puts, calls, exp=FRI):
    dte = (date.fromisoformat(exp) - REF).days
    key = f"{exp}:{dte}"
    return {"underlyingPrice": spot,
            "putExpDateMap": {key: {str(c["strikePrice"]): [c] for c in puts}},
            "callExpDateMap": {key: {str(c["strikePrice"]): [c] for c in calls}}}


def _q(bid, ask, now):
    return {"quote": {"bidPrice": bid, "askPrice": ask, "quoteTime": now.isoformat().replace("+00:00", "Z")}}


def test_parse_chain_and_probability():
    spot, cs = se.parse_chain(_payload(100, [_contract("P95", "PUT", 95, 1.0, 1.1)], [_contract("C105", "CALL", 105, 1.0, 1.1)]), REF)
    assert spot == 100 and len(cs) == 2 and cs[0]["dte"] == 23 and cs[0]["friday"]
    assert cs[0]["iv"] == pytest.approx(0.30)
    assert se.p_above(100, 100, 0.3, 30) == pytest.approx(0.483, abs=0.01)   # slightly below 50% (lognormal drift)
    assert se.p_above(100, 80, 0.3, 30) > 0.99


def test_scan_rule_needs_both_probability_and_reward_risk():
    # A credit put spread whose mid credit is rich (1.5x reward/risk) while the short strike is OTM
    # with a high probability: passes. Same strikes with a poor credit: fails.
    puts = [_contract("P99", "PUT", 99, 0.70, 0.74, iv=10), _contract("P98", "PUT", 98, 0.08, 0.12, iv=10)]
    spot, cs = se.parse_chain(_payload(100, puts, []), REF)
    pick = se.scan(cs, spot, "vertical")
    assert pick is not None and pick["width"] == 1 and pick["mid"] == pytest.approx(0.62)
    assert pick["pmax"] >= 0.55 and pick["mid"] / (1 - pick["mid"]) >= 1.5
    poor = [_contract("P99", "PUT", 99, 0.25, 0.35, iv=10), _contract("P98", "PUT", 98, 0.15, 0.2, iv=10)]
    spot, cs = se.parse_chain(_payload(100, poor, []), REF)
    assert se.scan(cs, spot, "vertical") is None


def test_condor_scan_uses_both_sides():
    puts = [_contract("P99", "PUT", 99, 0.45, 0.5, iv=3), _contract("P98", "PUT", 98, 0.1, 0.15, iv=3)]
    calls = [_contract("C101", "CALL", 101, 0.45, 0.5, iv=3), _contract("C102", "CALL", 102, 0.1, 0.15, iv=3)]
    spot, cs = se.parse_chain(_payload(100, puts, calls), REF)
    pick = se.scan(cs, spot, "condor")
    assert pick is not None and len(pick["legs"]) == 4 and pick["mid"] == pytest.approx(0.7)
    assert [l["side"] for l in pick["legs"]] == ["short", "long", "short", "long"]


def test_prices_floor_and_intrinsic():
    now = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)
    legs = [{"side": "short", "symbol": "S", "put_call": "PUT", "strike": 99},
            {"side": "long", "symbol": "L", "put_call": "PUT", "strike": 98}]
    quotes = {"S": se.validate(_q(2.9, 3.1, now), now.timestamp()), "L": se.validate(_q(0.3, 0.5, now), now.timestamp())}
    assert se.open_natural(legs, quotes) == pytest.approx(2.4)
    assert se.close_natural(legs, quotes) == pytest.approx(2.8)
    assert se.mid_price(legs, quotes) == pytest.approx(2.6)
    assert se.floor_price(1.0) == 0.6 and se.floor_price(5.0) == 3.0
    assert se.walked_limit(0.61, 0.6) == 0.6 and se.walked_limit(0.6, 0.6) == 0.6
    assert se.intrinsic(legs, 97.0) == pytest.approx(1.0)            # capped by the long put
    assert se.intrinsic(legs, 100.0) == 0.0
    stale = _q(1, 2, now - timedelta(seconds=se.STALE_S + 10))
    assert se.validate(stale, now.timestamp()) is None
    assert se.validate(_q(2, 1, now), now.timestamp()) is None       # crossed
    assert se.validate(_q(0, 0.05, now), now.timestamp()) is not None  # no-bid wing is a real quote


class Feeds:
    def __init__(self):
        self.q, self.chains = {}, {}

    def as_feeds(self, universe=("XYZ",)):
        return se.Feeds(universe=lambda: list(universe), chain=lambda t, d, *a: self.chains.get(t),
                        raw_quotes=lambda syms: {s: self.q[s] for s in syms if s in self.q},
                        earnings_dates=lambda t: [])


def _setup_order(tmp_db, f, structure="vertical"):
    aid = se.create_account("Verticals", structure, 50_000)
    puts = [_contract("P99", "PUT", 99, 0.70, 0.74, iv=10), _contract("P98", "PUT", 98, 0.08, 0.12, iv=10)]
    f.chains["XYZ"] = _payload(100, puts, [])
    out = se.run_scan(f.as_feeds(), REF)
    assert out["accounts"]["Verticals"]["placed"] == 1
    (o,) = _rows("SELECT * FROM spread_orders")
    return aid, o


def _rows(sql):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql)]


def test_limit_fills_only_when_the_live_market_reaches_it(tmp_db):
    f = Feeds()
    aid, o = _setup_order(tmp_db, f)
    assert o["limit_price"] == 0.62 and o["floor_price"] == 0.6 and o["contracts"] == 12   # $500 cap / $40.60
    now = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)
    f.q = {"P99": _q(0.70, 0.74, now), "P98": _q(0.08, 0.12, now)}  # natural 0.58 < limit 0.62
    r = se.poll_once(f.as_feeds(), now)
    assert r["filled"] == 0 and _rows("SELECT status FROM spread_orders")[0]["status"] == "working"
    later = now + timedelta(seconds=5)
    f.q = {"P99": _q(0.74, 0.78, later), "P98": _q(0.10, 0.12, later)}  # natural 0.62 >= 0.62 -> fill AT the limit
    assert se.poll_once(f.as_feeds(), later)["filled"] == 1
    (o,) = _rows("SELECT * FROM spread_orders")
    (p,) = _rows("SELECT * FROM spread_positions")
    assert o["status"] == "filled" and o["fill_price"] == 0.62 and p["credit"] == 0.62
    assert p["tp_price"] == pytest.approx(0.37)                      # buy back at 60% of the credit
    (a,) = _rows("SELECT cash FROM spread_accounts")
    assert a["cash"] == pytest.approx(50_000 + p["contracts"] * (62 - 2 * 0.65))


def test_unfilled_limit_walks_toward_natural_but_not_below_floor(tmp_db):
    f = Feeds()
    aid, o = _setup_order(tmp_db, f)
    now = datetime.fromisoformat(o["last_walk_at"]) + timedelta(seconds=se.WALK_EVERY_S + 1)
    f.q = {"P99": _q(0.70, 0.74, now), "P98": _q(0.08, 0.12, now)}
    se.poll_once(f.as_feeds(), now)
    assert _rows("SELECT limit_price FROM spread_orders")[0]["limit_price"] == pytest.approx(0.61)


def test_take_profit_and_carry_drop_and_expiry(tmp_db):
    f = Feeds()
    aid, o = _setup_order(tmp_db, f)
    now = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)
    f.q = {"P99": _q(0.74, 0.78, now), "P98": _q(0.10, 0.12, now)}
    se.poll_once(f.as_feeds(), now)
    later = now + timedelta(days=1)
    f.q = {"P99": _q(0.30, 0.35, later), "P98": _q(0.0, 0.02, later)}  # close cost 0.35 <= 0.37 -> take profit
    assert se.poll_once(f.as_feeds(), later)["closed"] == 1
    (p,) = _rows("SELECT * FROM spread_positions")
    n = p["contracts"]
    assert p["close_reason"] == "profit_target" and p["close_price"] == pytest.approx(0.37)
    assert p["pnl"] == pytest.approx(n * ((0.62 - 0.37) * 100 - 4 * 0.65))
    # a second order that never fills is carried, then dropped after 3 trading days
    with db.connect() as conn:
        conn.execute("INSERT INTO spread_orders (account_id, ticker, structure, legs, expiration_date, width, contracts, initial_mid, "
                     "limit_price, floor_price, placed_at, last_walk_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (aid, "XYZ", "vertical", o["legs"], FRI, 1, 1, 0.62, 0.62, 0.6, now.isoformat(), now.isoformat()))
    for k in range(3):
        se.end_of_day(f.as_feeds(), REF + timedelta(days=k))
    assert se.end_of_day(f.as_feeds(), REF + timedelta(days=2)) == {"skipped": "end of day already ran today"}
    assert _rows("SELECT status FROM spread_orders ORDER BY id")[-1]["status"] == "dropped"


def test_expired_spread_settles_at_intrinsic(tmp_db):
    f = Feeds()
    aid, o = _setup_order(tmp_db, f)
    now = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)
    f.q = {"P99": _q(0.74, 0.78, now), "P98": _q(0.10, 0.12, now)}
    se.poll_once(f.as_feeds(), now)
    f.q = {"XYZ": {"quote": {"lastPrice": 98.7, "postMarketChange": 0.2}}, "SPY": {"quote": {"lastPrice": 600}}}
    se.end_of_day(f.as_feeds(), date.fromisoformat(FRI))
    (p,) = _rows("SELECT * FROM spread_positions")
    assert p["close_reason"] == "expiry" and p["close_price"] == pytest.approx(0.5)
    assert p["pnl"] == pytest.approx(p["contracts"] * ((0.62 - 0.5) * 100 - 2 * 0.65))   # no closing commission
    assert _rows("SELECT * FROM spread_daily")[0]["spy_price"] == 600


def test_sizing_caps(tmp_db):
    # capped at $500 of max loss per spread: a $5-wide spread at a $3 credit risks $200+fees -> 2 contracts
    assert se.size(50_000, 5, 3.0, 2) == 2
    assert se.size(50_000, 5, 1.0, 2) == 1          # $402.60 -> 1 contract
    assert se.size(50_000, 10, 6.0, 2) == 1
    assert se.size(5_000, 10, 1.0, 4) == 0          # too small: skip, never oversize


def test_no_llm_or_order_tools_imported():
    import inspect
    src = inspect.getsource(se)
    for banned in ("llm", "placeOrder", "replaceOrder", "anthropic", "openai"):
        assert banned not in src.replace("No LLM", "").replace("no LLM", "")


def test_too_wide_a_market_is_skipped():
    # same rich mid as the passing case, but the natural credit is negative: not a real price
    puts = [_contract("P99", "PUT", 99, 0.40, 1.04, iv=10), _contract("P98", "PUT", 98, 0.0, 0.22, iv=10)]
    spot, cs = se.parse_chain(_payload(100, puts, []), REF)
    assert [c["bid"] for c in cs if c["symbol"] == "P98"] == [0.0]  # a no-bid wing is a real contract
    assert se.scan(cs, spot, "vertical") is None
    puts = [_contract("P99", "PUT", 99, 0.40, 1.04, iv=10), _contract("P98", "PUT", 98, 0.01, 0.19, iv=10)]
    spot, cs = se.parse_chain(_payload(100, puts, []), REF)
    assert se.scan(cs, spot, "vertical") is None                  # mid 0.62 but natural 0.21: > 50% below


def test_earnings_calendar_failure_places_nothing(tmp_db):
    f = Feeds()
    se.create_account("Verticals", "vertical", 50_000)
    puts = [_contract("P99", "PUT", 99, 0.70, 0.74, iv=10), _contract("P98", "PUT", 98, 0.08, 0.12, iv=10)]
    f.chains["XYZ"] = _payload(100, puts, [])
    feeds = f.as_feeds()

    def broken(t):
        raise RuntimeError("earnings calendar unavailable today; no new spreads placed")
    feeds.earnings_dates = broken
    with pytest.raises(RuntimeError):
        se.run_scan(feeds, REF)
    assert _rows("SELECT * FROM spread_orders") == []
    assert _rows("SELECT status FROM spread_runs")[0]["status"] == "failed"


def test_regular_close_strips_after_hours():
    assert se.regular_close({"lastPrice": 101.0, "postMarketChange": 1.0}) == 100.0
    assert se.regular_close({"lastPrice": 101.0}) == 101.0
    assert se.regular_close({}) is None


def test_short_leg_needs_a_bid():
    puts = [_contract("P99", "PUT", 99, 0.0, 0.74, iv=10), _contract("P98", "PUT", 98, 0.0, 0.12, iv=10)]
    spot, cs = se.parse_chain(_payload(100, puts, []), REF)
    assert se.scan(cs, spot, "vertical") is None


def test_fill_needs_fresh_sold_legs(tmp_db):
    f = Feeds()
    aid, o = _setup_order(tmp_db, f)
    now = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)
    old = now - timedelta(seconds=se.SHORT_FRESH_S + 5)
    f.q = {"P99": _q(0.74, 0.78, old), "P98": _q(0.10, 0.12, now)}   # touch, but the sold leg's quote is old
    assert se.poll_once(f.as_feeds(), now)["filled"] == 0
    f.q = {"P99": _q(0.74, 0.78, now), "P98": _q(0.10, 0.12, old)}   # quiet bought wing is fine
    assert se.poll_once(f.as_feeds(), now)["filled"] == 1


def test_wings_capped_at_five_dollars():
    puts = [_contract("P99", "PUT", 99, 0.70, 0.74, iv=10), _contract("P92", "PUT", 92, 0.0, 0.02, iv=10)]
    spot, cs = se.parse_chain(_payload(100, puts, []), REF)
    assert se.scan(cs, spot, "vertical") is None      # only a $7-wide spread exists: too wide


# ── rich-day put spreads ──

def _rich_payload(spot=100.0, near_iv=40.0, far_iv=38.0, short_bid=0.90, short_ask=0.95, long_bid=0.30, long_ask=0.34):
    near, far = "2026-11-20", "2026-12-24"       # 44 and 78 days from REF
    def c(sym, side, k, bid, ask, iv):
        return {"symbol": sym, "putCall": side, "strikePrice": k, "bid": bid, "ask": ask, "volatility": iv}
    puts_near = {str(k): [c(f"P{k}", "PUT", k, b, a, near_iv)] for k, b, a in
                 ((100, 4.0, 4.1), (95, 2.0, 2.05), (90, short_bid, short_ask), (88, 0.6, 0.64), (85, long_bid, long_ask))}
    calls_near = {str(k): [c(f"C{k}", "CALL", k, 4.0, 4.1, near_iv)] for k in (100, 101)}
    puts_far = {"100": [c("PF100", "PUT", 100, 5.0, 5.1, far_iv)], "99": [c("PF99", "PUT", 99, 4.6, 4.7, far_iv)]}
    calls_far = {"100": [c("CF100", "CALL", 100, 5.0, 5.1, far_iv)], "101": [c("CF101", "CALL", 101, 4.6, 4.7, far_iv)]}
    return {"underlyingPrice": spot,
            "putExpDateMap": {f"{near}:44": puts_near, f"{far}:78": puts_far},
            "callExpDateMap": {f"{near}:44": calls_near, f"{far}:78": calls_far}}


def _calm_closes(vol=0.20):
    import math
    d, px, out = REF - timedelta(days=60), 100.0, {}
    step = vol / math.sqrt(252)
    k = 0
    while d < REF:
        if d.weekday() < 5:
            px *= math.exp(step if k % 2 else -step)
            out[d] = px
            k += 1
        d += timedelta(days=1)
    return out


def test_rich_put_scan_picks_075_sd_short_and_capped_wing():
    spot, cs = se.parse_chain(_rich_payload(), REF)
    pick = se.scan_rich_put(cs, spot, _calm_closes(0.20))     # IV 40% vs realized 20% -> IV/RV 2.0; far 38% < near: inverted
    assert pick is not None
    # SD = 100 x 0.40 x sqrt(44/365) = 13.9: short nearest 100 - 0.75 SD = 89.6 -> 90; wing min(0.5 SD, $5) = $5 -> 85
    assert [l["strike"] for l in pick["legs"]] == [90.0, 85.0] and pick["width"] == 5.0
    assert pick["mid"] == pytest.approx(0.605) and pick["natural"] == pytest.approx(0.56)
    assert pick["signal"]["ivrv"] >= 1.33 and pick["signal"]["term"] <= 0.012


def test_rich_put_skips_wide_markets():
    spot, cs = se.parse_chain(_rich_payload(short_bid=0.60, short_ask=1.20), REF)   # natural 0.26 vs mid 0.58: > 25% below
    assert se.scan_rich_put(cs, spot, _calm_closes(0.20)) is None


def test_rich_put_needs_rich_and_inverted():
    spot, cs = se.parse_chain(_rich_payload(near_iv=20.0, far_iv=19.0), REF)
    assert se.scan_rich_put(cs, spot, _calm_closes(0.20)) is None          # IV/RV 1.0: not rich
    spot, cs = se.parse_chain(_rich_payload(near_iv=40.0, far_iv=45.0), REF)
    assert se.scan_rich_put(cs, spot, _calm_closes(0.20)) is None          # term upward sloping: not inverted
    spot, cs = se.parse_chain(_rich_payload(), REF)
    assert se.scan_rich_put(cs, spot, {}) is None                          # no price history: no trade


def test_rich_put_floor_tp_and_full_budget(tmp_db):
    assert se.floor_for("rich_put", 0.60, 0.52, 3) == pytest.approx(0.58)  # 25% of the way from mid to natural
    assert se.floor_for("vertical", 0.60, 0.52, 1) == 0.6                  # reward/risk 1.5 rule
    assert se.STRUCTURES["rich_put"]["tp_keep"] == 0.50 and se.STRUCTURES["rich_put"]["risk_total"] == 1.00
    se.create_account("Rich puts", "rich_put", 20_000)
    with pytest.raises(ValueError):
        se.create_account("bad", "straddle", 20_000)


def test_rich_put_end_to_end_order_floor_and_tp(tmp_db):
    f = Feeds()
    se.create_account("Rich puts", "rich_put", 20_000)
    f.chains["XYZ"] = _rich_payload()
    feeds = f.as_feeds()
    feeds.daily_closes = lambda t: _calm_closes(0.20)
    out = se.run_scan(feeds, REF)
    assert out["accounts"]["Rich puts"]["placed"] == 1
    (o,) = _rows("SELECT * FROM spread_orders")
    assert o["structure"] == "rich_put" and o["limit_price"] == pytest.approx(0.6, abs=0.01)
    assert o["floor_price"] == pytest.approx(round(o["limit_price"] - 0.25 * (o["limit_price"] - 0.56), 2))
    assert json.loads(o["signal"])["ivrv"] >= 1.33 and o["contracts"] == 1      # $500 cap: one $5-wide spread
    now = datetime(2026, 10, 8, 15, tzinfo=timezone.utc)
    f.q = {"P90": _q(0.95, 0.97, now), "P85": _q(0.30, 0.32, now)}            # natural 0.63 >= limit -> fill
    assert se.poll_once(f.as_feeds(), now)["filled"] == 1
    (p,) = _rows("SELECT * FROM spread_positions")
    assert p["tp_price"] == pytest.approx(round(0.5 * p["credit"], 2))           # 50% kept


def test_recent_closes_parse_and_signal_column_migration(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    with sqlite3.connect(path) as c:      # the tables as created before the signal column existed
        c.executescript(se.SCHEMA.replace("    signal TEXT,\n", ""))
        assert "signal" not in {r[1] for r in c.execute("PRAGMA table_info(spread_orders)")}
    se._INIT["path"] = None
    se.init_tables()
    with db.connect() as conn:
        assert "signal" in {r["name"] for r in conn.execute("PRAGMA table_info(spread_orders)")}
