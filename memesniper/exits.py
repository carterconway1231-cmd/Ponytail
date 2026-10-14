"""Exit rules. Pure function of position + current price, so it is easy to test.

Order matters: protective exits (stop, dead liquidity, time) beat profit-taking.
"Selling the peak" is done in stages: a take-profit ladder sells into strength, a fading-momentum
signal sells while still in profit, and a trailing stop catches the rest on the way down.
"""
from dataclasses import dataclass


@dataclass
class ExitAction:
    fraction: float          # of the CURRENT token balance to sell; 0 = hold
    reason: str = ""


HOLD = ExitAction(0.0)


def evaluate(p, price, now, ladder, fade=None):
    """p: Position; price: native coin per raw token from a live sell quote (None = no route).
    ladder: [(multiple of p.take_profit_pct, fraction of balance)], ascending.
    fade: reason string when watchlist data shows momentum fading, else None."""
    if price is None:
        return ExitAction(1.0, "no sell route (liquidity pulled?)")
    if price > p.high_water_price:
        p.high_water_price = price
    pnl_pct = (price / p.entry_price - 1) * 100
    age_min = (now - p.opened_at) / 60

    if pnl_pct <= -p.stop_loss_pct:
        return ExitAction(1.0, f"stop loss {pnl_pct:.1f}%")
    if fade and "liquidity" in fade:
        return ExitAction(1.0, f"{fade} ({pnl_pct:+.1f}%)")
    if age_min >= p.max_hold_minutes:
        return ExitAction(1.0, f"time stop {age_min:.0f}m ({pnl_pct:+.1f}%)")
    if p.tp_taken < len(ladder):
        multiple, fraction = ladder[p.tp_taken]
        if pnl_pct >= p.take_profit_pct * multiple:
            last = p.tp_taken == len(ladder) - 1 and fraction >= 1
            return ExitAction(1.0 if last else fraction,
                              f"take profit {p.tp_taken + 1}/{len(ladder)} at {pnl_pct:+.1f}%")
    if fade and pnl_pct > 0:
        return ExitAction(1.0, f"{fade} ({pnl_pct:+.1f}%)")
    armed = p.high_water_price >= p.entry_price * (1 + p.trailing_stop_pct / 100)
    drawdown = (1 - price / p.high_water_price) * 100
    if armed and drawdown >= p.trailing_stop_pct:
        return ExitAction(1.0, f"trailing stop {drawdown:.1f}% off high ({pnl_pct:+.1f}%)")
    return HOLD
