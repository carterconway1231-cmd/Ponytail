"""Position sizing by edge: fractional Kelly on the learned win rate and
payoff, applied to account equity.

  kelly f* = p - (1 - p) / b      (b = average win R / average loss R)
  risk budget = equity * clip(KELLY_FRACTION * f*, 0, MAX_RISK_PCT)

Until a conviction level has MIN_CALIBRATION_TRADES of history, a flat
BASE_RISK_PCT is used. A setup with no edge (f* <= 0) sizes to zero.
"Risk" per contract is what a stop-out realistically costs: premium times
the stop distance with a gap allowance for singles (capped at the full
premium), and the full debit for spreads (their max loss).
"""
import math

from .state import MULTIPLIER


def risk_per_contract(cfg, price, is_spread):
    full = price * MULTIPLIER
    return full if is_spread else min(full, full * cfg.stop_loss_pct * cfg.stop_gap_allowance)


def risk_fraction(cfg, signal):
    if signal.get("bucket_trades", 0) < cfg.min_calibration_trades or not signal.get("avg_win_r") \
            or not signal.get("avg_loss_r"):
        return cfg.base_risk_pct, "base risk (no calibrated edge yet)"
    p, b = signal["p_win"], signal["avg_win_r"] / abs(signal["avg_loss_r"])
    kelly = p - (1 - p) / b
    frac = max(0.0, min(cfg.max_risk_pct, cfg.kelly_fraction * kelly))
    return frac, f"{cfg.kelly_fraction:g}x Kelly on p={p:.2f}, payoff {b:.2f} (full Kelly {kelly:+.1%})"


def max_contracts(cfg, equity, signal, price, is_spread):
    frac, why = risk_fraction(cfg, signal)
    budget = equity * frac
    per = risk_per_contract(cfg, price, is_spread)
    n = math.floor(budget / per) if per > 0 else 0
    return n, {"equity": round(equity, 2), "risk_pct": round(frac * 100, 2), "risk_budget": round(budget, 2),
               "risk_per_contract": round(per, 2), "basis": why}
