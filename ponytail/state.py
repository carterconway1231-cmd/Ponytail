"""Durable agent state: ensemble weights, open positions, closed-trade log.

Positions record the entry-time indicator votes so the weights can learn
from each trade's realized P&L when it closes.
"""
import json
import os
import tempfile
from datetime import datetime, timezone


MULTIPLIER = 100


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class State:
    def __init__(self, path, data=None):
        self.path = path
        self.data = data or {"positions": {}, "trade_log": []}
        self.data.setdefault("learner", {})
        self.data.pop("weights", None)  # v1 three-indicator weights, superseded by the learner
        self.learner = None  # attached by the session (needs config)

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
    def positions(self):
        return self.data["positions"]

    @property
    def trade_log(self):
        return self.data["trade_log"]

    def open_premium(self):
        return sum(p["entry_price"] * p["quantity"] * MULTIPLIER for p in self.positions.values())

    def realized_pnl_on(self, day):
        return sum(t["pnl"] for t in self.trade_log if t["closed_at"][:10] == day)

    def realized_since(self, day):
        return sum(t["pnl"] for t in self.trade_log if t["closed_at"][:10] >= day)

    def losing_streak(self):
        """Consecutive losing closes, most recent first, and when the last one closed."""
        n = 0
        for t in reversed(self.trade_log):
            if t["pnl"] >= 0:
                break
            n += 1
        return n, (self.trade_log[-1]["closed_at"][:10] if self.trade_log else None)

    def last_loss_on(self, symbol):
        for t in reversed(self.trade_log):
            if t["symbol"] == symbol and t["pnl"] < 0:
                return t["closed_at"][:10]
        return None

    def open_position(self, option_id, inst, quantity, price, signal, mode, order_id=None, short=None, extra=None):
        """price is the per-share debit: the option's price, or the spread's net debit."""
        self.positions[option_id] = {
            "symbol": inst["symbol"], "type": inst["type"], "strike": inst["strike"],
            "expiration": inst["expiration"], "quantity": quantity, "entry_price": price,
            "entry_signal": signal, "direction": 1 if inst["type"] == "call" else -1,
            "mode": mode, "opened_at": now_iso(), "open_order_id": order_id, "filled": mode == "paper",
            "hwm": price, "lwm": price, "stop": None, "kind": "spread" if short else "single",
            **({"short_option_id": short["option_id"], "short_strike": short["strike"],
                "width": abs(short["strike"] - inst["strike"])} if short else {}),
            **(extra or {}),
        }

    def close_position(self, option_id, quantity, exit_price, reason):
        pos = self.positions[option_id]
        quantity = min(quantity, pos["quantity"])
        pnl = (exit_price - pos["entry_price"]) * quantity * MULTIPLIER
        premium = pos["entry_price"] * quantity * MULTIPLIER
        if self.learner is not None:
            self.learner.learn_trade(pos.get("entry_signal"), pos["direction"], pnl, premium)
        sig = pos.get("entry_signal") or {}
        self.trade_log.append({
            "option_id": option_id, **{k: pos[k] for k in ("symbol", "type", "strike", "expiration", "mode")},
            "quantity": quantity, "entry_price": pos["entry_price"], "exit_price": exit_price,
            "pnl": round(pnl, 2), "r": round(pnl / premium, 3) if premium else None, "reason": reason,
            "entry_conviction": sig.get("conviction"), "entry_regime": sig.get("regime"),
            "agreeing_factors": sig.get("agree"), "kind": pos.get("kind", "single"),
            "mfe": round((pos.get("hwm", pos["entry_price"]) - pos["entry_price"]) / pos["entry_price"], 3),
            "mae": round((pos.get("lwm", pos["entry_price"]) - pos["entry_price"]) / pos["entry_price"], 3),
            "held_days": (datetime.now(timezone.utc) - datetime.fromisoformat(pos["opened_at"])).days,
            "slippage_pct": round((pos["entry_price"] - pos["entry_mid"]) / pos["entry_mid"], 4)
            if pos.get("entry_mid") else None, "vol_regime": pos.get("vol_regime"),
            "closed_at": now_iso(),
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
