"""Premium selling with defined risk: credit vertical spreads.

The edge this strategy harvests is the volatility risk premium: options on
equities and indexes have, on average, been priced for more movement than
the underlying then delivers. Selling a vertical (sell a closer OTM option,
buy a further one as insurance) collects part of that gap with a hard cap on
the loss (width - credit). It does not need a directional forecast.

What can go wrong is the tail: a fast selloff expands IV and moves price
through the short strike at once. Hence: small size by max loss, a loss stop
at a multiple of the credit, closing early at 50% profit, and managing out
around 21 DTE before gamma accelerates.

Pure functions shared by the live agent and the backtest.
"""
import math

from .options_math import bs_delta, bs_price

# Calibrated against Robinhood quotes on 2026-10-07 (Nov 20 2026 expiry):
#   SPY puts:  740P 16.9% / 720P 19.2% vs ATM 13.2%  -> ~+26% IV per sigma below spot
#   AAPL 310P +8%, TSLA 330P +3% per ~0.9 sigma      -> single stocks ~+7%/sigma
# Calls (strikes above spot) slope down more gently. Slopes apply by strike location
# (below/above spot), which keeps calls and puts at a strike consistent (parity).
INDEX_ETFS = {"SPY", "QQQ", "IWM", "DIA"}
SKEW = {"index": (0.26, 0.08), "stock": (0.07, 0.03)}  # (below-spot slope, above-spot slope)
SKEW_BOUNDS = (0.6, 2.5)


def asset_class(symbol):
    return "index" if (symbol or "").upper() in INDEX_ETFS else "stock"


def skewed_iv(atm_iv, spot, strike, years, kind, symbol=None):
    """IV at `strike` from the at-the-money IV with an equity-style skew."""
    if years <= 0 or atm_iv <= 0:
        return atm_iv
    z = math.log(spot / strike) / (atm_iv * math.sqrt(years))  # >0 for strikes below spot
    down, up = SKEW[asset_class(symbol)]
    lo, hi = SKEW_BOUNDS
    return atm_iv * min(hi, max(lo, 1 + (down if z > 0 else up) * z))


def leg_price(spot, strike, years, atm_iv, kind, symbol=None):
    return bs_price(spot, strike, years, skewed_iv(atm_iv, spot, strike, years, kind, symbol), kind)


def spread_value(spot, short_k, long_k, years, atm_iv, kind, symbol=None):
    """Cost to buy back a credit vertical (short leg minus long leg), per share."""
    return max(0.0, leg_price(spot, short_k, years, atm_iv, kind, symbol)
               - leg_price(spot, long_k, years, atm_iv, kind, symbol))


def strike_for_delta(spot, years, atm_iv, kind, target, step, symbol=None):
    """OTM strike (rounded to `step`) whose |delta| is closest to target."""
    sign = -1 if kind == "put" else 1
    best, k = None, round(spot / step) * step
    for _ in range(400):
        d = abs(bs_delta(spot, k, years, skewed_iv(atm_iv, spot, k, years, kind, symbol), kind))
        if best is None or abs(d - target) < abs(best[1] - target):
            best = (k, d)
        if d < target * 0.5:
            break
        k = round(k + sign * step, 4)
    return best[0]


def credit_spread(spot, years, atm_iv, kind, short_delta, width, step, symbol=None):
    """(short_strike, long_strike) for a credit vertical at the target delta."""
    short_k = strike_for_delta(spot, years, atm_iv, kind, short_delta, step, symbol)
    long_k = short_k - width if kind == "put" else short_k + width
    return short_k, long_k


def edge_vs_realized(credit, spot, short_k, long_k, years, rv, kind, symbol=None):
    """Credit received minus the spread's value priced at REALIZED vol (same
    skew shape): positive when the market is paying more for the risk than
    recent movement justifies. Per share."""
    return credit - spread_value(spot, short_k, long_k, years, rv, kind, symbol)


def half_spread(price, symbol=None):
    """Typical half bid/ask of one option leg (same calibration date):
    SPY 2.67/4.32/7.46/12.82 -> 0.01/0.015/0.015/0.02; AAPL 3.25/10.60 ->
    0.10/0.20; TSLA 4.93/19.60 -> 0.075/0.10."""
    if asset_class(symbol) == "index":
        return max(0.01, 0.0025 * price)
    return max(0.05, 0.015 * price)


def strike_step(spot):
    return 1.0 if spot >= 50 else 0.5 if spot >= 10 else 0.25


def rank_live(cfg, market, state, symbol, spot, rv, atm, today, equity, max_candidates=5):
    """Candidate credit verticals for `symbol` from the contracts quoted this run.
    Edge = credit (between natural and mid) minus the spread's value at realized
    vol, per dollar of max loss. Only candidates that fit the risk budget and
    credit/width floor are returned, best first."""
    from datetime import date as _date
    sides = {"put": ("put",), "call": ("call",), "both": ("put", "call")}[cfg.premium_side]
    by_exp = {}
    for oid, inst in market.instruments.items():
        q = market.quotes.get(oid)
        if inst["symbol"] != symbol or inst["type"] not in sides or not inst["tradable"]:
            continue
        if not q or not q.get("bid") or not q.get("ask") or q.get("delta") is None:
            continue
        days = (_date.fromisoformat(inst["expiration"]) - today).days
        if cfg.premium_min_dte <= days <= cfg.premium_max_dte:
            by_exp.setdefault((inst["expiration"], inst["type"]), []).append((oid, inst, q, days))
    rows = []
    open_risk = state.open_premium()
    for (exp, kind), legs in by_exp.items():
        for s_id, s_inst, sq, days in legs:
            delta = abs(sq["delta"])
            if not 0.10 <= delta <= cfg.premium_max_short_delta:
                continue
            for l_id, l_inst, lq, _ in legs:
                width = abs(s_inst["strike"] - l_inst["strike"])
                further = l_inst["strike"] < s_inst["strike"] if kind == "put" else l_inst["strike"] > s_inst["strike"]
                if not further or width > max(cfg.premium_max_width_pct * spot, strike_step(spot)) + 1e-9:
                    continue
                natural = sq["bid"] - lq["ask"]
                mid = (sq["bid"] + sq["ask"]) / 2 - (lq["bid"] + lq["ask"]) / 2
                limit = round(max(natural, mid - 0.25 * (mid - natural)), 2)
                if limit <= 0 or limit < cfg.premium_min_credit_pct * width:
                    continue
                max_loss = (width - limit) * 100
                qty = min(math.floor((equity or 0) * cfg.premium_risk_pct / max_loss),
                          math.floor(((equity or 0) * cfg.premium_max_total_risk_pct - open_risk) / max_loss))
                if qty < 1:
                    continue
                vol = rv or atm or 0.2
                edge = edge_vs_realized(limit, spot, s_inst["strike"], l_inst["strike"], days / 365, vol, kind, symbol)
                legs_cost = half_spread(sq["mark"] or 0, symbol) + half_spread(lq["mark"] or 0, symbol)
                rows.append({
                    "short_option_id": s_id, "long_option_id": l_id, "side": kind, "expiration": exp, "dte": days,
                    "short_strike": s_inst["strike"], "long_strike": l_inst["strike"], "width": width,
                    "short_delta": round(delta, 3), "natural_credit": round(natural, 2), "mid_credit": round(mid, 2),
                    "suggested_limit_credit": limit, "credit_pct_of_width": round(limit / width, 3),
                    "max_loss_per_spread": round(max_loss, 2), "max_quantity": qty,
                    "edge_vs_realized_per_spread": round(edge * 100, 2), "edge_per_dollar_risk": round(edge * 100 / max_loss, 3),
                    "cost_drag_warning": width < 0.005 * spot and legs_cost * 2 > 0.25 * limit,
                    "take_profit_at": round((1 - cfg.premium_take_profit) * limit, 2),
                    "loss_stop_at": round(limit * (1 + cfg.premium_stop_x), 2),
                })
    rows.sort(key=lambda r: (-r["edge_per_dollar_risk"], abs(r["short_delta"] - cfg.premium_short_delta)))
    return rows[:max_candidates]
