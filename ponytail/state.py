"""Durable agent state: ensemble weights, open positions, closed-trade log.

Positions record the entry-time indicator votes so the weights can learn
from each trade's realized P&L when it closes.
"""
import json
import os
import tempfile
from datetime import datetime, timezone

from .signals import DEFAULT_WEIGHTS, learn_from_trade

MULTIPLIER = 100


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class State:
    def __init__(self, path, data=None):
        self.path = path
        self.data = data or {"weights": dict(DEFAULT_WEIGHTS), "positions": {}, "trade_log": []}

    @classmethod
    def load(cls, path):
        if os.path.exists(path):
            with open(path) as f:
                return cls(path, json.load(f))
        return cls(path)

    def save(self):
        # Atomic replace so a crash mid-write can't corrupt the trade log.
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    @property
    def weights(self):
        return self.data["weights"]

    @property
    def positions(self):
        return self.data["positions"]

    @property
    def trade_log(self):
        return self.data["trade_log"]

    def open_premium(self):
        return sum(p["entry_price"] * p["quantity"] * MULTIPLIER for p in self.positions.values())

    def realized_pnl_on(self, day):
        return sum(t["pnl"] for t in self.trade_log if t["closed_at"][:10] == day)

    def open_position(self, option_id, inst, quantity, price, signal, mode):
        self.positions[option_id] = {
            "symbol": inst["symbol"], "type": inst["type"], "strike": inst["strike"],
            "expiration": inst["expiration"], "quantity": quantity, "entry_price": price,
            "entry_votes": signal["votes"], "direction": 1 if inst["type"] == "call" else -1,
            "mode": mode, "opened_at": now_iso(),
        }

    def close_position(self, option_id, quantity, exit_price, reason):
        pos = self.positions[option_id]
        quantity = min(quantity, pos["quantity"])
        pnl = (exit_price - pos["entry_price"]) * quantity * MULTIPLIER
        learn_from_trade(self.weights, pos["entry_votes"], pos["direction"], pnl)
        self.trade_log.append({
            "option_id": option_id, **{k: pos[k] for k in ("symbol", "type", "strike", "expiration", "mode", "entry_votes")},
            "quantity": quantity, "entry_price": pos["entry_price"], "exit_price": exit_price,
            "pnl": round(pnl, 2), "reason": reason, "weights_after": dict(self.weights), "closed_at": now_iso(),
        })
        pos["quantity"] -= quantity
        if pos["quantity"] <= 0:
            del self.positions[option_id]
        return pnl

    def drop_position(self, option_id, reason):
        """Position vanished at the broker without an agent close (unfilled
        open, manual close, expiry). No P&L is known, so nothing is learned."""
        pos = self.positions.pop(option_id)
        self.data.setdefault("dropped", []).append({"option_id": option_id, **pos, "reason": reason, "at": now_iso()})
