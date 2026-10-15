"""Hard limits, enforced in code after Claude decides. Claude cannot exceed them.

All caps are in USD and apply across every chain together. The native coin's USD price
converts each trade; without a price the trade is refused.
"""
import os
import time
from dataclasses import replace


class RiskManager:
    def __init__(self, settings, state):
        self.s, self.state = settings, state

    def exposure_usd(self):
        """Cost basis still at risk in open positions (partial sells reduce it)."""
        total = 0.0
        for p in self.state.open_positions():
            initial = p.entry_sol / p.entry_price if p.entry_price else 0
            total += p.entry_usd * (p.tokens / initial) if initial else 0
        return total

    def unrealized_loss(self):
        """Worst case for open positions: marked at their stops. A negative USD number."""
        loss = 0.0
        for p in self.state.open_positions():
            initial = p.entry_sol / p.entry_price if p.entry_price else 0
            remaining = p.entry_usd * (p.tokens / initial) if initial else 0
            loss -= remaining * p.stop_loss_pct / 100
        return loss

    def halted(self):
        """Reason new entries are blocked right now, or None."""
        s = self.s
        if os.path.exists(s.kill_switch_path):
            return f"kill switch file {s.kill_switch_path} present"
        if self.state.realized_pnl_today() + self.unrealized_loss() <= -s.max_daily_loss_usd:
            return f"daily loss limit ${s.max_daily_loss_usd:.0f} hit"
        if self.state.spent_today() >= s.max_daily_spend_usd:
            return f"daily spend cap ${s.max_daily_spend_usd:.0f} reached"
        if len(self.state.open_positions()) >= s.max_open_positions:
            return f"{s.max_open_positions} positions already open"
        if self.state.entries_since(time.time() - 3600) >= s.max_entries_per_hour:
            return f"{s.max_entries_per_hour} entries in the last hour"
        return None

    def approve(self, decision, launch, native_usd, wallet_native=None):
        """Return (clamped_decision, None) or (None, rejection_reason).

        native_usd: USD price of the chain's native coin. wallet_native: balance in that coin
        (None in paper mode)."""
        s = self.s
        if decision.action != "BUY":
            return None, "skipped"
        if decision.confidence < s.min_confidence:
            return None, f"confidence {decision.confidence:.2f} < {s.min_confidence}"
        if not native_usd:
            return None, f"no {launch.chain} price; can't size the trade"
        reason = self.halted()
        if reason:
            return None, reason
        if self.state.holding(launch.chain, launch.mint):
            return None, "already holding this token"
        last = self.state.creator_last_entry(launch.creator)
        if last and time.time() - last < s.creator_cooldown_hours * 3600:
            return None, "already traded this creator recently"

        room = min(s.max_usd_per_trade,
                   s.max_total_exposure_usd - self.exposure_usd(),
                   s.max_daily_spend_usd - self.state.spent_today())
        if wallet_native is not None:
            room = min(room, wallet_native * native_usd - s.min_wallet_reserve_usd)
        size_usd = min(max(decision.size_usd, 0.0), room)
        if size_usd < 1.0:
            return None, "no room left under exposure/spend/wallet caps"

        return replace(
            decision,
            size_usd=round(size_usd, 2),
            size_native=size_usd / native_usd,
            # Claude may tighten stops/holds but never loosen them past the configured bounds.
            stop_loss_pct=min(decision.stop_loss_pct or s.stop_loss_pct, s.stop_loss_pct),
            take_profit_pct=decision.take_profit_pct if decision.take_profit_pct > 0 else s.take_profit_pct,
            max_hold_minutes=min(decision.max_hold_minutes or s.max_hold_minutes, s.max_hold_minutes),
        ), None
