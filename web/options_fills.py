"""Paper executions use executable quotes. Data failures return exclusions, never raises."""
from __future__ import annotations

import math
from dataclasses import replace
from numbers import Real


def price(value):
    """Finite nonnegative numeric quote; zero is executable, missing is not."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0 <= value <= 1e12 else None


def quote_bid(bid, ask):
    """Executable bid from a two-sided quote. A zero bid counts only when the same quote shows a
    positive ask (a real no-bid market); 0/0 is an empty or after-hours quote, i.e. missing, so
    a data gap can never sell a position at $0."""
    bid, ask = price(bid), price(ask)
    if bid is None:
        return None
    if bid == 0 and not (ask is not None and ask > 0):
        return None
    return bid


def buy_quote(bid, ask):
    bid, ask = price(bid), price(ask)
    if bid is None or ask is None or ask <= 0 or ask < bid:
        return None, 'invalid_entry_quote'
    return ask, None


def sell_quote(bid, last_bid=None):
    bid = price(bid)
    if bid is not None:
        return bid, None
    last = price(last_bid)
    return last, 'stale_bid' if last is not None else 'missing_bid'


def stop_entry(pos):
    # Legacy mid entries still have recorded entry bid; never use the ask as a stop reference.
    return price(pos.get('entry_bid')) or 0.0


def bid_stop(outcome, bid):
    """Keep stop trigger/limit decisions, execute fills at the decision-time bid."""
    if outcome.action == 'fill':
        return replace(outcome, fill_price=bid, crossed=False)
    return outcome
