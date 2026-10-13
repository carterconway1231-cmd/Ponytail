"""Exit rules. Pure function of position + current price, so it is easy to test.

Order matters: protective exits (stop, dead liquidity, time) beat profit-taking.
"""
from dataclasses import dataclass


@dataclass
class ExitAction:
    fraction: float          # of the CURRENT token balance to sell; 0 = hold
    reason: str = ""


HOLD = ExitAction(0.0)


def evaluate(p, price, now, tp_sell_fraction):
    """p: Position; price: SOL per raw token unit from a live sell quote (None = no route)."""
    if price is None:
        return ExitAction(1.0, "no sell route (liquidity pulled?)")
    if price > p.high_water_price:
        p.high_water_price = price
    pnl_pct = (price / p.entry_price - 1) * 100
    age_min = (now - p.opened_at) / 60

    if pnl_pct <= -p.stop_loss_pct:
        return ExitAction(1.0, f"stop loss {pnl_pct:.1f}%")
    if age_min >= p.max_hold_minutes:
        return ExitAction(1.0, f"time stop {age_min:.0f}m ({pnl_pct:+.1f}%)")
    if not p.tp_taken and pnl_pct >= p.take_profit_pct:
        return ExitAction(tp_sell_fraction, f"take profit {pnl_pct:+.1f}%")
    armed = p.high_water_price >= p.entry_price * (1 + p.trailing_stop_pct / 100)
    drawdown = (1 - price / p.high_water_price) * 100
    if armed and drawdown >= p.trailing_stop_pct:
        return ExitAction(1.0, f"trailing stop {drawdown:.1f}% off high ({pnl_pct:+.1f}%)")
    return HOLD
