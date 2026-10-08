"""Black-Scholes pricing for European options (a good approximation for the
short-dated American calls/puts on non-dividend-heavy underlyings traded
here), plus the scenario expected-value model used to pick contracts.
"""
import math

RISK_FREE = 0.04


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(spot, strike, years, vol, kind, r=RISK_FREE):
    """Option value per share. Degenerates to intrinsic at expiry or zero vol."""
    intrinsic = max(0.0, spot - strike) if kind == "call" else max(0.0, strike - spot)
    if years <= 0 or vol <= 0:
        return intrinsic
    sd = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (r + vol * vol / 2) * years) / sd
    d2 = d1 - sd
    if kind == "call":
        return spot * _ncdf(d1) - strike * math.exp(-r * years) * _ncdf(d2)
    return strike * math.exp(-r * years) * _ncdf(-d2) - spot * _ncdf(-d1)


def bs_delta(spot, strike, years, vol, kind, r=RISK_FREE):
    if years <= 0 or vol <= 0:
        itm = spot > strike if kind == "call" else spot < strike
        return (1.0 if itm else 0.0) * (1 if kind == "call" else -1)
    d1 = (math.log(spot / strike) + (r + vol * vol / 2) * years) / (vol * math.sqrt(years))
    return _ncdf(d1) if kind == "call" else _ncdf(d1) - 1


def theta_per_day(spot, strike, years, vol, kind):
    """Value lost per calendar day if nothing else changes."""
    return bs_price(spot, strike, years, vol, kind) - bs_price(spot, strike, max(0.0, years - 1 / 365), vol, kind)


def scenario_ev(legs, spot, move, p_up, hold_days, dte_days, cost):
    """Expected value of holding a position `hold_days` under a two-point
    forecast: the underlying moves +move with probability p_up, else -move.

    legs: [(strike, kind, iv, sign)] with sign +1 long, -1 short (per share).
    cost: net debit paid per share (including expected entry slippage).
    Returns (ev_per_share, value_up, value_down). IV is held constant, which
    ignores vol crush/expansion; the vol regime filter covers that separately.
    """
    years_left = max(0.0, (dte_days - hold_days) / 365)

    def value(s):
        return sum(sign * bs_price(s, k, years_left, iv, kind) for k, kind, iv, sign in legs)

    up, down = value(spot + move), value(max(0.01, spot - move))
    return p_up * up + (1 - p_up) * down - cost, up, down
