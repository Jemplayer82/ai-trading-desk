"""Option chain data + deterministic contract selection for the options paper trader.

Turns a directional signal on an underlying (BUY -> CALL, SELL -> PUT) into one
vetted, liquid, near-ATM contract, before any LLM sizing happens. Chain data
comes from the Schwab MCP server (greeks available) with a yfinance
Ticker.option_chain fallback (no greeks -> moneyness-based strike pick).

All thresholds are module constants so tests and the allocator reference one
source of truth. Prices used for entry/marking are always bid/ask mids —
lastPrice is never trusted (on illiquid contracts it can be a days-old trade).
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

import yfinance as yf

from tradingagents.dataflows import schwab_mcp

from . import market_cache, market_calendar

log = logging.getLogger(__name__)

_ET: tzinfo = market_calendar._ET

# ── Selection thresholds ─────────────────────────────────────────────────────
MIN_CONVICTION = 6          # options are levered; weak signals just donate theta
DTE_TARGET = 21
DTE_MIN, DTE_MAX = 10, 45           # preferred entry window
DTE_WIDE_MIN, DTE_WIDE_MAX = 7, 60  # single widening step before skipping
DELTA_TARGET = 0.45
DELTA_MIN, DELTA_MAX = 0.30, 0.65
MONEYNESS_BAND = 0.10       # yfinance fallback: strike within ±10% of spot
MIN_BID = 0.05
MIN_MID = 0.05
MIN_OPEN_INTEREST = 100     # the liquidity signal that survives weekends
MAX_SPREAD_ABS = 0.10       # spread ok if <= $0.10 absolute...
MAX_SPREAD_PCT = 0.20       # ...or <= 20% of mid
STRIKE_ATTEMPTS = 3         # preferred strike + 2 alternates before skipping

# Max worker threads for the per-ticker chain fetch in fetch_candidates.
# Bounded because each call can hit the Schwab MCP server plus a yfinance
# fallback, and concurrent same-day account builds now share that backend.
_CHAIN_FETCH_WORKERS = 6

# Same-day cache of the SELECTED contract — one that already passed the
# liquidity gates — keyed (underlying, side) within a trade_date scope.
# All three daily options accounts vet the same converged ticker set, so
# builds 2 and 3 would otherwise re-pull the identical full option
# chain. A hit is NEVER served blind: bid/ask are refreshed from one
# Schwab bulk-quotes call and re-run through passes_liquidity_gates
# before the contract is returned.
CONTRACT_CACHE_TTL_SECONDS = 2 * 3600
_CONTRACT_CACHE = market_cache.SameDayCache("options-contract",
                                            ttl_seconds=CONTRACT_CACHE_TTL_SECONDS)


def _contract_cache_enabled() -> bool:
    """Kill switch (env, read at call time). This cache sits directly
    upstream of paper-order placement, so it needs a no-code rollback."""
    value = os.environ.get("OPTIONS_CONTRACT_CACHE", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}

# Deep dives return the system-wide 5-tier rating (tradingagents/agents/utils
# /rating.py: Buy, Overweight, Hold, Underweight, Sell), not raw BUY/SELL/HOLD
# — the quick scanner's vocabulary. Overweight/Underweight are real directional
# calls, just not the most extreme tier; Hold and any unrecognized label carry
# no entry mapped below and fall through as "not tradeable" in the caller.
_DIRECTION_BY_SIGNAL = {
    "BUY": "BUY", "OVERWEIGHT": "BUY",
    "SELL": "SELL", "UNDERWEIGHT": "SELL",
}


def today_et() -> date:
    return market_calendar.today_et()


def now_et() -> datetime:
    return market_calendar.now_et()


def build_occ_symbol(underlying: str, expiration_date: str, put_call: str, strike: float) -> str:
    """Canonical Schwab-padded OCC symbol; round-trips with brokerages.parse_occ_symbol.

    Root space-padded to 6 chars + YYMMDD + C/P + strike*1000 zero-padded to 8.
    """
    exp = datetime.strptime(expiration_date, "%Y-%m-%d").date()
    cp = "C" if put_call.upper().startswith("C") else "P"
    return f"{underlying.upper():<6s}{exp.strftime('%y%m%d')}{cp}{int(round(strike * 1000)):08d}"


def _mid(bid: float | None, ask: float | None) -> float | None:
    if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)):
        return None
    if bid <= 0 or ask < bid:
        return None
    return round((float(bid) + float(ask)) / 2, 4)


def passes_liquidity_gates(c: dict[str, Any]) -> tuple[bool, str]:
    """Reject junk quotes (zero-bid, crossed, wide, illiquid, penny premium)."""
    bid, ask, mid = c.get("bid"), c.get("ask"), c.get("mid")
    if not isinstance(bid, (int, float)) or bid < MIN_BID:
        return False, "zero/low bid"
    if not isinstance(ask, (int, float)) or ask <= 0 or ask < bid:
        return False, "crossed/absent ask"
    if mid is None or mid < MIN_MID:
        return False, "penny premium"
    if (c.get("open_interest") or 0) < MIN_OPEN_INTEREST:
        return False, f"OI < {MIN_OPEN_INTEREST}"
    spread = ask - bid
    if spread > MAX_SPREAD_ABS and spread > MAX_SPREAD_PCT * mid:
        return False, "spread too wide"
    return True, ""


def _candidate(
    underlying: str,
    put_call: str,
    strike: float,
    expiration_date: str,
    bid: float | None,
    ask: float | None,
    delta: float | None,
    open_interest: int | None,
    underlying_price: float | None,
    source: str,
    ref_date: date,
) -> dict[str, Any] | None:
    try:
        exp = datetime.strptime(expiration_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    mid = _mid(bid, ask)
    spread = round(float(ask) - float(bid), 4) if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) else None
    # Schwab uses -999 as a greeks sentinel; treat out-of-range deltas as absent.
    if not isinstance(delta, (int, float)) or not (-1.0 <= delta <= 1.0) or delta == 0:
        delta = None
    return {
        "occ_symbol": build_occ_symbol(underlying, expiration_date, put_call, strike),
        "underlying": underlying.upper(),
        "put_call": "CALL" if put_call.upper().startswith("C") else "PUT",
        "strike": float(strike),
        "expiration_date": exp.isoformat(),
        "dte": (exp - ref_date).days,
        "bid": float(bid) if isinstance(bid, (int, float)) else None,
        "ask": float(ask) if isinstance(ask, (int, float)) else None,
        "mid": mid,
        "delta": float(delta) if delta is not None else None,
        "open_interest": int(open_interest or 0),
        "underlying_price": float(underlying_price) if isinstance(underlying_price, (int, float)) else None,
        "spread": spread,
        "spread_pct": round(spread / mid, 4) if spread is not None and mid else None,
        "source": source,
    }


def normalize_schwab_chain(
    payload: dict[str, Any],
    underlying: str,
    side: str,
    ref_date: date | None = None,
) -> list[dict[str, Any]]:
    """Flatten a Schwab getOptionChain payload into candidate dicts for one side.

    Map shape: {"callExpDateMap"/"putExpDateMap": {"YYYY-MM-DD:DTE":
    {"<strike>": [contract, ...]}}}. Strike keys are strings (often with a
    trailing .0) — always float() them.
    """
    ref_date = ref_date or today_et()
    key = "callExpDateMap" if side.upper().startswith("C") else "putExpDateMap"
    exp_map = payload.get(key) or {}
    spot = payload.get("underlyingPrice")
    if not isinstance(spot, (int, float)):
        uq = payload.get("underlying") or {}
        spot = uq.get("mark") or uq.get("last") or uq.get("close")
    out: list[dict[str, Any]] = []
    for exp_key, strikes in exp_map.items():
        exp_iso = str(exp_key).split(":", 1)[0]
        if not isinstance(strikes, dict):
            continue
        for strike_key, contracts in strikes.items():
            contract = contracts[0] if isinstance(contracts, list) and contracts else contracts
            if not isinstance(contract, dict):
                continue
            try:
                strike = float(contract.get("strikePrice") or strike_key)
            except (TypeError, ValueError):
                continue
            cand = _candidate(
                underlying=underlying,
                put_call=side,
                strike=strike,
                expiration_date=exp_iso,
                bid=contract.get("bid"),
                ask=contract.get("ask"),
                delta=contract.get("delta"),
                open_interest=contract.get("openInterest"),
                underlying_price=spot,
                source="schwab",
                ref_date=ref_date,
            )
            if cand:
                out.append(cand)
    return out


def normalize_yf_chain(
    underlying: str,
    expiration_date: str,
    frame: Any,
    side: str,
    spot: float | None,
    ref_date: date | None = None,
) -> list[dict[str, Any]]:
    """Flatten one side of a yfinance option_chain(exp) DataFrame into candidates.

    yfinance carries no greeks worth trusting, so delta stays None and strike
    selection downstream falls back to the moneyness band.
    """
    ref_date = ref_date or today_et()
    out: list[dict[str, Any]] = []
    if frame is None or getattr(frame, "empty", True):
        return out
    for row in frame.itertuples(index=False):
        try:
            strike = float(row.strike)
        except (TypeError, ValueError, AttributeError):
            continue
        bid = getattr(row, "bid", None)
        ask = getattr(row, "ask", None)
        oi = getattr(row, "openInterest", None)
        try:
            oi = int(oi) if oi is not None and oi == oi else 0  # NaN-safe
        except (TypeError, ValueError):
            oi = 0
        cand = _candidate(
            underlying=underlying,
            put_call=side,
            strike=strike,
            expiration_date=expiration_date,
            bid=float(bid) if isinstance(bid, (int, float)) and bid == bid else None,
            ask=float(ask) if isinstance(ask, (int, float)) and ask == ask else None,
            delta=None,
            open_interest=oi,
            underlying_price=spot,
            source="yfinance",
            ref_date=ref_date,
        )
        if cand:
            out.append(cand)
    return out


def pick_expiry(dtes: list[int]) -> int | None:
    """Choose the DTE nearest DTE_TARGET within the preferred window, widening
    once; None if nothing tradeable."""
    in_window = [d for d in dtes if DTE_MIN <= d <= DTE_MAX]
    if not in_window:
        in_window = [d for d in dtes if DTE_WIDE_MIN <= d <= DTE_WIDE_MAX]
    if not in_window:
        return None
    return min(in_window, key=lambda d: (abs(d - DTE_TARGET), d))


def select_contract(candidates: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[str]]:
    """Pick one contract from a single underlying's normalized candidates.

    Expiry first (nearest DTE_TARGET, window + one widening), then strike by
    |delta| nearest DELTA_TARGET within [DELTA_MIN, DELTA_MAX] when deltas
    exist, else nearest-to-spot within the moneyness band. The preferred strike
    plus up to STRIKE_ATTEMPTS-1 alternates are tried against the liquidity
    gates. Returns (contract, rejection_notes).
    """
    notes: list[str] = []
    if not candidates:
        return None, ["no chain data"]
    dte = pick_expiry(sorted({c["dte"] for c in candidates}))
    if dte is None:
        return None, [f"no expiry in {DTE_WIDE_MIN}-{DTE_WIDE_MAX} DTE"]
    pool = [c for c in candidates if c["dte"] == dte]

    with_delta = [c for c in pool if c.get("delta") is not None
                  and DELTA_MIN <= abs(c["delta"]) <= DELTA_MAX]
    if with_delta:
        ordered = sorted(with_delta, key=lambda c: abs(abs(c["delta"]) - DELTA_TARGET))
    else:
        spot = next((c["underlying_price"] for c in pool if c.get("underlying_price")), None)
        if not spot:
            return None, ["no underlying price for moneyness pick"]
        banded = [c for c in pool if abs(c["strike"] - spot) <= MONEYNESS_BAND * spot]
        if not banded:
            return None, [f"no strike within ±{MONEYNESS_BAND:.0%} of spot"]
        ordered = sorted(banded, key=lambda c: abs(c["strike"] - spot))

    for c in ordered[:STRIKE_ATTEMPTS]:
        ok, reason = passes_liquidity_gates(c)
        if ok:
            return c, notes
        notes.append(f"{c['occ_symbol']}: {reason}")
    return None, notes or ["all strikes failed liquidity gates"]


def _yf_spot(ticker: yf.Ticker, fallback: float | None) -> float | None:
    try:
        hist = ticker.history(period="1d")
        if hist is not None and not hist.empty:
            return float(hist["Close"].dropna().iloc[-1])
    except Exception:
        log.debug("yf spot fetch failed", exc_info=True)
    return fallback


def _fetch_contract_uncached(
    underlying: str,
    direction: str,
    spot_hint: float | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Fetch chain data for one underlying and pick a contract.

    direction: BUY -> CALL, SELL -> PUT. Schwab first (delta pick), yfinance
    fallback (moneyness pick). Returns (contract | None, notes).
    """
    side = "CALL" if str(direction).upper() == "BUY" else "PUT"
    ref = today_et()
    notes: list[str] = []

    if schwab_mcp.market_data_enabled():
        try:
            payload = schwab_mcp.get_option_chain(
                underlying,
                contract_type=side,
                strike_count=20,
                from_date=(ref + timedelta(days=DTE_WIDE_MIN)).isoformat(),
                to_date=(ref + timedelta(days=DTE_WIDE_MAX)).isoformat(),
            )
        except Exception:
            log.exception("[options] Schwab chain fetch failed for %s", underlying)
            payload = None
        if payload:
            cands = normalize_schwab_chain(payload, underlying, side, ref)
            contract, sel_notes = select_contract(cands)
            if contract:
                return contract, sel_notes
            notes += [f"schwab: {n}" for n in sel_notes]

    # yfinance fallback (also the primary path when Schwab is disabled).
    try:
        t = yf.Ticker(underlying)
        expiries = list(t.options or [])
        by_dte: dict[int, str] = {}
        for e in expiries:
            try:
                d = (datetime.strptime(e, "%Y-%m-%d").date() - ref).days
            except ValueError:
                continue
            if DTE_WIDE_MIN <= d <= DTE_WIDE_MAX:
                by_dte[d] = e
        dte = pick_expiry(sorted(by_dte))
        if dte is None:
            notes.append(f"yfinance: no expiry in {DTE_WIDE_MIN}-{DTE_WIDE_MAX} DTE")
            return None, notes
        exp_iso = by_dte[dte]
        chain = t.option_chain(exp_iso)
        frame = chain.calls if side == "CALL" else chain.puts
        spot = _yf_spot(t, spot_hint)
        cands = normalize_yf_chain(underlying, exp_iso, frame, side, spot, ref)
        contract, sel_notes = select_contract(cands)
        if contract:
            return contract, sel_notes
        notes += [f"yfinance: {n}" for n in sel_notes]
    except Exception as exc:
        log.warning("[options] yfinance chain fetch failed for %s: %s", underlying, exc)
        notes.append(f"yfinance: {exc}")
    return None, notes


def _revalidate_cached_contract(cached: dict[str, Any], spot_hint: float | None = None) -> tuple[dict[str, Any] | None, str]:
    """Refresh bid/ask for a cached contract and re-run the liquidity gates.

    Returns (validated_contract, "") on success, or (None, reason_note) when
    the cached contract can no longer be served safely and must be refetched.
    """
    occ = cached["occ_symbol"]

    try:
        quotes = schwab_mcp.get_quotes([occ])
    except Exception:
        log.debug("[options] re-validation quote refresh failed for %s", occ, exc_info=True)
        return None, f"{occ}: quote refresh failed — refetching"

    q = (quotes or {}).get(occ, {}).get("quote") or {}
    bid, ask = q.get("bidPrice"), q.get("askPrice")
    mid = _mid(bid, ask)
    if mid is None:
        return None, f"{occ}: no usable bid/ask — refetching"

    refreshed = dict(cached)
    refreshed["bid"] = float(bid)
    refreshed["ask"] = float(ask)
    refreshed["mid"] = mid
    refreshed["spread"] = round(float(ask) - float(bid), 4)
    refreshed["spread_pct"] = round(refreshed["spread"] / mid, 4) if mid else None

    # Refresh greeks/spot from the live quote. If the live quote does not
    # actually carry a usable value for a field, clear the stale cached value
    # so downstream code cannot mistake it for current live market data.
    raw_delta = q.get("delta")
    if isinstance(raw_delta, (int, float)) and -1.0 <= raw_delta <= 1.0 and raw_delta != 0:
        refreshed["delta"] = float(raw_delta)
    else:
        refreshed["delta"] = None

    raw_underlying = q.get("underlyingPrice")
    if isinstance(raw_underlying, (int, float)) and raw_underlying > 0:
        refreshed["underlying_price"] = float(raw_underlying)
    else:
        refreshed["underlying_price"] = None

    # Moneyness/delta sanity recheck BEFORE the liquidity gates. Use the
    # freshest available spot signal and reject contracts that have drifted
    # far ITM/OTM since they were originally selected.
    delta = refreshed.get("delta")
    if delta is not None:
        if not (DELTA_MIN <= abs(delta) <= DELTA_MAX):
            return None, f"{occ}: cached contract delta drifted out of profile — refetching"
    else:
        current_spot = None
        if isinstance(refreshed.get("underlying_price"), (int, float)) and refreshed["underlying_price"] > 0:
            current_spot = float(refreshed["underlying_price"])
        elif isinstance(spot_hint, (int, float)) and spot_hint > 0:
            current_spot = float(spot_hint)
        if current_spot is not None:
            moneyness = abs(refreshed["strike"] / current_spot - 1)
            if moneyness > MONEYNESS_BAND:
                return None, f"{occ}: cached contract strike drifted out of moneyness band — refetching"

    ok, reason = passes_liquidity_gates(refreshed)
    if ok:
        return refreshed, ""
    return None, f"{occ}: cached contract failed re-validation ({reason}) — refetching"


def fetch_contract(
    underlying: str,
    direction: str,
    spot_hint: float | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Fetch chain data for one underlying and pick a contract.

    direction: BUY -> CALL, SELL -> PUT. Schwab first (delta pick), yfinance
    fallback (moneyness pick). Returns (contract | None, notes).

    Same-day caching: a previously selected contract is reused only after a
    fresh bid/ask quote from Schwab is re-run through the liquidity gates.
    The per-ticker fetch_candidates worker pool (ThreadPoolExecutor with up
    to 6 workers) calls this concurrently; the cache lock is never held across the
    underlying chain fetch, so concurrent misses fetch independently. Tests that
    monkeypatch this function wholesale bypass the cache entirely, which is
    expected and supported.
    """
    if not _contract_cache_enabled():
        return _fetch_contract_uncached(underlying, direction, spot_hint)

    if not schwab_mcp.market_data_enabled():
        return _fetch_contract_uncached(underlying, direction, spot_hint)

    side = "CALL" if str(direction).upper() == "BUY" else "PUT"
    trade_date = today_et().isoformat()
    key = (underlying.upper(), side)

    prefix_notes: list[str] = []
    cached = _CONTRACT_CACHE.get(trade_date, key)
    if cached is not None:
        refreshed, note = _revalidate_cached_contract(cached, spot_hint)
        if refreshed is not None:
            return refreshed, []
        prefix_notes = [note]

    contract, notes = _fetch_contract_uncached(underlying, direction, spot_hint)
    if contract is not None:
        _CONTRACT_CACHE.put(trade_date, key, dict(contract))
    return contract, prefix_notes + notes


def _fetch_one(row: dict[str, Any], direction: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Per-row worker target; resolves fetch_contract by bare global name so
    monkeypatch replacements in tests take effect inside worker threads."""
    return fetch_contract(row["ticker"], direction, spot_hint=row.get("entry_price"))


def fetch_candidates(
    signals: list[dict[str, Any]],
    progress_cb: Any = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Map deep-dived directional signals to vetted contracts.

    signals: rows with ticker / signal (BUY|SELL|HOLD, or the 5-tier Buy|
    Overweight|Hold|Underweight|Sell a deep dive's own rating produces —
    see _DIRECTION_BY_SIGNAL) / conviction / reasoning / entry_price. Holds,
    unrecognized labels, and conviction < MIN_CONVICTION are skipped.
    Returns (candidates, rejection_notes) in the same order as the input rows;
    candidates order and notes order are deterministic regardless of the order
    in which worker threads complete their chain fetches.
    """
    # ── PHASE 1 — FILTER (input order, no network) ──────────────────────────
    eligible: list[tuple[int, dict[str, Any], str, int]] = []
    eligible_meta: dict[int, tuple[dict[str, Any], str, int]] = {}
    row_notes: list[list[str]] = [[] for _ in signals]
    for idx, row in enumerate(signals):
        sig_raw = (row.get("signal") or "").upper()
        direction = _DIRECTION_BY_SIGNAL.get(sig_raw)
        conviction = int(row.get("conviction") or 0)
        ticker = row.get("ticker")
        if not ticker or direction is None:
            continue
        if conviction < MIN_CONVICTION:
            row_notes[idx].append(f"{ticker}: conviction {conviction} < {MIN_CONVICTION}")
            continue
        eligible.append((idx, row, direction, conviction))
        eligible_meta[idx] = (row, direction, conviction)

    # ── PHASE 2 — FETCH concurrently (only if there is work to do) ────────────
    fetched: dict[int, tuple[dict[str, Any] | None, list[str]]] = {}
    if eligible:
        with ThreadPoolExecutor(max_workers=min(_CHAIN_FETCH_WORKERS, len(eligible))) as pool:
            futures = {pool.submit(_fetch_one, row, direction): idx
                       for idx, row, direction, _ in eligible}
            for fut in as_completed(futures):
                fetched[futures.pop(fut)] = fut.result()

    # ── PHASE 3 — ASSEMBLE in input order ───────────────────────────────────
    candidates: list[dict[str, Any]] = []
    notes: list[str] = []
    done = 0
    for idx in range(len(signals)):
        done += 1
        row = signals[idx]
        notes.extend(row_notes[idx])
        if idx in fetched:
            contract, c_notes = fetched[idx]
            notes += c_notes
            ticker = row.get("ticker")
            _, direction, conviction = eligible_meta[idx]
            if contract is None:
                notes.append(f"{ticker}: no tradeable contract")
            else:
                contract.update({
                    "ticker": ticker,
                    "signal": direction,
                    "conviction": conviction,
                    "rationale": (row.get("reasoning") or "")[:500],
                    "final_decision": (row.get("final_decision") or "")[:2000],
                })
                candidates.append(contract)
            if progress_cb:
                try:
                    progress_cb(done, len(signals))
                except Exception:  # noqa: BLE001 - progress must never sink a build
                    pass
    return candidates, notes