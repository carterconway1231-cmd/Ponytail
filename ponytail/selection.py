"""Contract selection by expected value instead of a fixed delta.

Forecast: over EV_HOLD_DAYS the underlying moves +/- one realized-vol move
(spot * daily_vol * sqrt(days)), up with probability p_up (the learned win
rate for this setup's conviction, flipped for bearish signals). Each
candidate (long option, or debit vertical) is priced in both scenarios with
Black-Scholes at its own quoted IV, minus a realistic entry cost (mid plus
ENTRY_SLIPPAGE_PCT of the way to the natural price). The ranking key is EV
per dollar of premium.

With no learned edge (p = 0.5) and IV above realized vol, long premium has
negative EV, which is the point: the gate only lets trades through when the
measured edge pays for the volatility premium and decay being bought.
"""
import math
from datetime import date

from .options_math import scenario_ev, theta_per_day

MAX_CANDIDATES = 5


def underlying_spot(market, symbol):
    bars = market.bars.get(symbol, {})
    for interval in ("5minute", "hour", "day"):  # freshest first
        if bars.get(interval):
            return float(bars[interval][-1]["close_price"])
    return None


def _mid(q):
    return (q["bid"] + q["ask"]) / 2


def _two_sided(q):
    return q and q.get("bid") and q.get("ask") and q["bid"] > 0 and q["ask"] > 0


def entry_cost(cfg, market, long_id, short_id=None):
    lq = market.quotes[long_id]
    if short_id is None:
        mid, natural = _mid(lq), lq["ask"]
    else:
        sq = market.quotes[short_id]
        mid, natural = _mid(lq) - _mid(sq), lq["ask"] - sq["bid"]
    return max(0.01, mid + cfg.entry_slippage_pct * (natural - mid)), mid


def evaluate(cfg, market, signal, spot, rv, long_id, short_id, today):
    inst = market.instruments[long_id]
    dte_days = (date.fromisoformat(inst["expiration"]) - today).days
    hold = min(cfg.ev_hold_days, max(1, dte_days - 1))
    daily_vol = (rv or market.quotes[long_id].get("iv") or 0.3) / math.sqrt(252)
    move = spot * daily_vol * math.sqrt(hold)
    p_up = signal["p_win"] if inst["type"] == "call" else 1 - signal["p_win"]
    lq = market.quotes[long_id]
    legs = [(inst["strike"], inst["type"], lq.get("iv") or rv or 0.3, 1)]
    if short_id:
        s_inst, sq = market.instruments[short_id], market.quotes[short_id]
        legs.append((s_inst["strike"], s_inst["type"], sq.get("iv") or rv or 0.3, -1))
    cost, mid = entry_cost(cfg, market, long_id, short_id)
    ev, up, down = scenario_ev(legs, spot, move, p_up, hold, dte_days, cost)
    years = dte_days / 365
    theta = sum(sign * theta_per_day(spot, k, years, iv, kind) for k, kind, iv, sign in legs)
    sign = 1 if inst["type"] == "call" else -1
    out = {
        "option_id": long_id, "short_option_id": short_id, "structure": "debit_spread" if short_id else "long",
        "expiration": inst["expiration"], "dte": dte_days, "strike": inst["strike"],
        "short_strike": market.instruments[short_id]["strike"] if short_id else None,
        "delta": lq.get("delta"), "mid": round(mid, 2), "expected_cost": round(cost, 2),
        "suggested_limit": round(cost, 2), "ev_per_share": round(ev, 3),
        "ev_per_dollar": round(ev / cost, 3) if cost else None,
        "value_if_right": round(up if sign > 0 else down, 2), "value_if_wrong": round(down if sign > 0 else up, 2),
        "theta_per_day": round(theta, 3), "breakeven": round(inst["strike"] + sign * cost, 2),
        "forecast": {"spot": round(spot, 2), "move": round(move, 2), "hold_days": hold, "p_right": signal["p_win"]},
    }
    if short_id:
        out["max_profit"] = round(abs(out["short_strike"] - inst["strike"]) - cost, 2)
    return out


def rank(cfg, market, signal, symbol, rv, today, structure):
    """Best candidates for `symbol` in the signal's direction, by EV per dollar."""
    spot = underlying_spot(market, symbol)
    if spot is None:
        return []
    kind = {"BUY": "call", "SELL": "put"}.get(signal["decision"])
    pool = []
    for oid, inst in market.instruments.items():
        q = market.quotes.get(oid)
        if inst["symbol"] != symbol or inst["type"] != kind or not inst["tradable"] or not _two_sided(q):
            continue
        dte_days = (date.fromisoformat(inst["expiration"]) - today).days
        if cfg.min_dte <= dte_days <= cfg.max_dte:
            pool.append((oid, inst, q))
    rows = []
    for oid, inst, q in pool:
        if q.get("delta") is None or not cfg.min_abs_delta <= abs(q["delta"]) <= cfg.max_abs_delta:
            continue
        if structure in ("long", "any"):
            rows.append(evaluate(cfg, market, signal, spot, rv, oid, None, today))
        if structure in ("debit_spread", "any") and cfg.allow_spreads:
            for sid, s_inst, sq in pool:
                further = s_inst["strike"] > inst["strike"] if kind == "call" else s_inst["strike"] < inst["strike"]
                if (s_inst["expiration"] == inst["expiration"] and further and sq.get("delta") is not None
                        and 0.10 <= abs(sq["delta"]) <= 0.40):
                    row = evaluate(cfg, market, signal, spot, rv, oid, sid, today)
                    width = abs(s_inst["strike"] - inst["strike"])
                    if row["expected_cost"] <= cfg.max_spread_debit_pct * width:
                        rows.append(row)
    rows.sort(key=lambda r: -(r["ev_per_dollar"] or -9))
    return rows[:MAX_CANDIDATES]
