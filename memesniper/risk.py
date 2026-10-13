"""Hard limits, enforced in code after Claude decides. Claude cannot exceed them."""
import os
import time
from dataclasses import replace


class RiskManager:
    def __init__(self, settings, state):
        self.s, self.state = settings, state

    def halted(self):
        """Reason new entries are blocked right now, or None."""
        s = self.s
        if os.path.exists(s.kill_switch_path):
            return f"kill switch file {s.kill_switch_path} present"
        if self.state.realized_pnl_today() + self.unrealized_loss() <= -s.max_daily_loss_sol:
            return f"daily loss limit {s.max_daily_loss_sol} SOL hit"
        if self.state.spent_today() >= s.max_daily_spend_sol:
            return f"daily spend cap {s.max_daily_spend_sol} SOL reached"
        if len(self.state.open_positions()) >= s.max_open_positions:
            return f"{s.max_open_positions} positions already open"
        if self.state.entries_since(time.time() - 3600) >= s.max_entries_per_hour:
            return f"{s.max_entries_per_hour} entries in the last hour"
        return None

    def unrealized_loss(self):
        """Worst case for open positions: mark them at their stop. Negative number."""
        return -sum(p.tokens * p.entry_price * p.stop_loss_pct / 100
                    for p in self.state.open_positions())

    def approve(self, decision, launch, wallet_sol=None):
        """Return (clamped_decision, None) or (None, rejection_reason)."""
        s = self.s
        if decision.action != "BUY":
            return None, "claude skipped"
        if decision.confidence < s.min_confidence:
            return None, f"confidence {decision.confidence:.2f} < {s.min_confidence}"
        reason = self.halted()
        if reason:
            return None, reason
        last = self.state.creator_last_entry(launch.creator)
        if last and time.time() - last < s.creator_cooldown_hours * 3600:
            return None, "already traded this creator recently"

        exposure = sum(p.entry_sol for p in self.state.open_positions())
        room = min(s.max_sol_per_trade,
                   s.max_total_exposure_sol - exposure,
                   s.max_daily_spend_sol - self.state.spent_today())
        if wallet_sol is not None:
            room = min(room, wallet_sol - s.min_wallet_reserve_sol)
        size = min(max(decision.size_sol, 0.0), room)
        if size < 0.001:
            return None, "no room left under exposure/spend/wallet caps"

        return replace(
            decision,
            size_sol=round(size, 6),
            # Claude may tighten stops/holds but never loosen past the configured bounds.
            stop_loss_pct=min(decision.stop_loss_pct or s.stop_loss_pct, s.stop_loss_pct),
            take_profit_pct=decision.take_profit_pct if decision.take_profit_pct > 0 else s.take_profit_pct,
            max_hold_minutes=min(decision.max_hold_minutes or s.max_hold_minutes, s.max_hold_minutes),
        ), None
