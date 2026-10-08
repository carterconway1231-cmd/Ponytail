"""Loss protection for open positions.

Every held position must carry a protective sell-to-close stop order resting
at Robinhood, so a losing position is cut even when the agent is not running:

  * Initial stop:  entry * (1 - STOP_LOSS_PCT)
  * Trailing stop: once the mark has run up TRAIL_ACTIVATE_PCT above entry,
    the stop ratchets to high_water_mark * (1 - TRAIL_PCT) and never moves
    down again.

stop_market orders are GFD-only at Robinhood, so they lapse at the close and
the first run each day re-places them. stop_limit orders can be GTC but may
not fill on a gap; the rule-based stop in review_exits backs them up.

reconcile() folds broker truth (option orders, positions) back into state:
actual fill prices, filled or cancelled stops, expired GFD stops.
"""
import math
from datetime import date

GFD_TYPES = {"stop_market"}
DEAD_STATES = {"cancelled", "rejected", "failed", "voided"}


def tick_size(inst, price):
    ticks = (inst or {}).get("ticks") or {}
    cutoff = ticks.get("cutoff") or 0
    tick = ticks.get("above") if price >= cutoff else ticks.get("below")
    return tick or 0.01


def round_up(price, tick):
    return round(math.ceil(round(price / tick, 6)) * tick, 2)


def round_down(price, tick):
    return round(math.floor(round(price / tick, 6)) * tick, 2)


def required_stop(cfg, pos, inst=None):
    """The minimum stop trigger this position must be protected at."""
    entry, hwm = pos["entry_price"], max(pos.get("hwm") or 0, pos["entry_price"])
    stop = entry * (1 - cfg.stop_loss_pct)
    if hwm >= entry * (1 + cfg.trail_activate_pct):
        stop = max(stop, hwm * (1 - cfg.trail_pct))
    return round_up(stop, tick_size(inst, stop))


def stop_order_args(cfg, option_id, pos, stop_price, inst=None):
    """Exact place_option_order arguments for the protective stop."""
    args = {
        "legs": [{"option_id": option_id, "side": "sell", "position_effect": "close"}],
        "quantity": f"{pos['quantity']:g}", "type": cfg.stop_order_type, "stop_price": f"{stop_price:.2f}",
    }
    if cfg.stop_order_type == "stop_limit":
        limit = round_down(stop_price * (1 - cfg.stop_limit_buffer_pct), tick_size(inst, stop_price))
        args.update(price=f"{max(limit, 0.01):.2f}", time_in_force="gtc")
    else:
        args.update(time_in_force="gfd")
    return args


def stop_is_active(pos, today):
    stop = pos.get("stop")
    if not stop:
        return False
    if stop["type"] in GFD_TYPES and stop["placed_on"] != today.isoformat():
        return False  # day order lapsed at the previous close
    return True


def is_confirmed(pos, market):
    """Paper positions fill instantly; live ones once the broker shows them."""
    if pos["mode"] == "paper" or pos.get("filled"):
        return True
    return market.broker_positions is not None and pos.get("option_id") in market.broker_positions


def is_spread(pos):
    return pos.get("kind") == "spread"


def position_mark(oid, pos, market):
    """(mark, exit_bid, exit_ask) per share/unit of the position, or None.
    For a debit spread: long leg minus short leg; exiting sells the long at
    its bid and buys the short at its ask."""
    q = market.quotes.get(oid)
    if not q or not q.get("mark"):
        return None
    if not is_spread(pos):
        return q["mark"], q.get("bid") or 0.0, q.get("ask") or 0.0
    s = market.quotes.get(pos["short_option_id"])
    if not s or s.get("mark") is None:
        return None
    return (q["mark"] - s["mark"], max(0.0, (q.get("bid") or 0) - (s.get("ask") or 0)),
            (q.get("ask") or 0) - (s.get("bid") or 0))


def unprotected(state, market, today):
    """Held singles without a resting stop. Debit spreads are exempt: their
    max loss is the debit paid, and Robinhood stops are single-leg only."""
    return [oid for oid, p in state.positions.items()
            if not is_spread(p) and is_confirmed({**p, "option_id": oid}, market) and not stop_is_active(p, today)]


def fill_price(order):
    qty = order.get("processed_quantity") or 0
    if qty <= 0 or not order.get("processed_premium"):
        return None
    return order["processed_premium"] / (qty * (order.get("multiplier") or 100))


def reconcile(cfg, state, market, today):
    """Apply broker truth to state. Returns a list of event dicts."""
    events = []
    for oid in list(state.positions):
        pos = state.positions[oid]

        # Live open order filled: replace the assumed limit price with the real fill.
        open_order = market.orders.get(pos.get("open_order_id") or "")
        if open_order and open_order["state"] == "filled" and not pos.get("filled"):
            px = fill_price(open_order)
            if px:
                pos["entry_price"] = pos["hwm"] = round(px, 4)
            pos["filled"] = True
            events.append({"kind": "open_filled", "option_id": oid, "price": px})

        stop = pos.get("stop")
        if stop and stop["mode"] == "live":
            order = market.orders.get(stop["order_id"])
            if order and order["state"] == "filled":
                px = fill_price(order) or stop["stop_price"]
                pnl = state.close_position(oid, order["processed_quantity"] or pos["quantity"], px, "stop loss filled at broker")
                events.append({"kind": "stopped_out", "option_id": oid, "price": px, "pnl": round(pnl, 2)})
                continue
            if order and order["state"] in DEAD_STATES:
                pos["stop"] = None
                events.append({"kind": "stop_gone", "option_id": oid, "state": order["state"]})
        if pos.get("stop") and not stop_is_active(pos, today):
            pos["stop"] = None
            events.append({"kind": "stop_expired", "option_id": oid})

        pending = pos.get("pending_close")
        if pending:
            order = market.orders.get(pending["order_id"] or "")
            if order and order["state"] == "filled":
                px = fill_price(order) or pending["price"]
                pnl = state.close_position(oid, order["processed_quantity"] or pending["quantity"], px, "live close filled")
                events.append({"kind": "closed", "option_id": oid, "price": px, "pnl": round(pnl, 2)})
                continue
            if (order and order["state"] in DEAD_STATES) or pending["placed_on"] != today.isoformat():
                pos["pending_close"] = None  # lapsed unfilled: still holding, needs protection again
                events.append({"kind": "close_lapsed", "option_id": oid})

        if pos["mode"] == "live" and market.broker_positions is not None and oid not in market.broker_positions:
            open_order = market.orders.get(pos.get("open_order_id") or "")
            never_filled = (open_order and open_order["state"] in DEAD_STATES) or pos["opened_at"][:10] != today.isoformat()
            if pos.get("filled") or never_filled:
                state.drop_position(oid, "closed outside the agent" if pos.get("filled") else "opening order never filled")
                events.append({"kind": "dropped", "option_id": oid, "filled": bool(pos.get("filled"))})
                continue

        # Track best/worst marks: the high-water mark drives the trailing stop,
        # and both feed exit learning (max favorable/adverse excursion).
        m = position_mark(oid, pos, market)
        if m:
            pos["hwm"] = max(pos.get("hwm") or pos["entry_price"], m[0])
            pos["lwm"] = min(pos.get("lwm") or pos["entry_price"], m[0])
    return events


def simulate_paper_stops(state, market, today):
    """Paper positions: trigger resting stops against the current quote."""
    events = []
    for oid in list(state.positions):
        pos = state.positions[oid]
        stop, quote = pos.get("stop"), market.quotes.get(oid)
        if is_spread(pos) or pos["mode"] != "paper" or not stop or not stop_is_active(pos, today) or not quote or not quote.get("mark"):
            continue
        if quote["mark"] > stop["stop_price"]:
            continue
        bid = quote.get("bid") or 0
        if stop["type"] == "stop_limit" and bid < stop["limit_price"]:
            events.append({"kind": "paper_stop_gapped", "option_id": oid, "bid": bid, "limit": stop["limit_price"]})
            continue  # unfilled, like a real stop-limit through a gap; rule-based exit takes over
        pnl = state.close_position(oid, pos["quantity"], bid, "paper stop triggered")
        events.append({"kind": "stopped_out", "mode": "paper", "option_id": oid, "price": bid, "pnl": round(pnl, 2)})
    return events


def days_between(a_iso, b):
    return (b - date.fromisoformat(a_iso[:10])).days
