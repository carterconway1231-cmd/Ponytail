"""Hard guardrails. Pure functions over (config, state, market cache) so they
are unit-testable without a broker or a model.

Policy:
  * Opens: single-leg, buy-to-open, limit, long calls/puts only, sized and
    filtered by Config, and only in the direction today's signal votes.
  * Closes: sell-to-close of a position the agent holds. Exits are never
    blocked by portfolio limits — getting out must always be possible.
"""
from datetime import date

from .state import MULTIPLIER

DIRECTION_TO_TYPE = {"BUY": "call", "SELL": "put"}


def dte(expiration, today):
    return (date.fromisoformat(expiration) - today).days


def check_open(cfg, state, market, signals, option_id, quantity, limit_price, today, now=None):
    """Return a list of violated rules (empty means approved)."""
    problems = []
    inst = market.instruments.get(option_id)
    quote = market.quotes.get(option_id)
    if inst is None:
        return ["contract not seen via get_option_instruments this run"]
    if quote is None:
        return ["contract not quoted via get_option_quotes this run"]

    symbol = inst["symbol"]
    signal = signals.get(symbol)
    if signal is None:
        problems.append(f"no compute_signals result for {symbol} this run")
    elif signal["decision"] == "HOLD":
        problems.append(f"{symbol} signal is HOLD (score {signal['score']:.2f})")
    elif DIRECTION_TO_TYPE[signal["decision"]] != inst["type"]:
        problems.append(f"{symbol} signal is {signal['decision']} but contract is a {inst['type']}")

    if symbol not in cfg.symbols:
        problems.append(f"{symbol} is not in the configured SYMBOLS universe")
    if not inst["tradable"]:
        problems.append("contract is not active/tradable")
    if any(p["symbol"] == symbol for p in state.positions.values()):
        problems.append(f"already holding a {symbol} position")

    days = dte(inst["expiration"], today)
    if not cfg.min_dte <= days <= cfg.max_dte:
        problems.append(f"DTE {days} outside [{cfg.min_dte}, {cfg.max_dte}]")

    if cfg.avoid_earnings:
        if symbol not in market.earnings:
            problems.append(f"earnings not checked via get_earnings_results for {symbol} this run")
        else:
            hits = [d for d in market.earnings[symbol] if today.isoformat() <= d <= inst["expiration"]]
            if hits:
                problems.append(f"earnings on {hits[0]} falls before expiration {inst['expiration']}")

    bid, ask, delta = quote["bid"], quote["ask"], quote["delta"]
    if not bid or not ask or bid <= 0 or ask <= 0:
        problems.append("no two-sided market")
    else:
        mid = (bid + ask) / 2
        spread = (ask - bid) / mid
        if spread > cfg.max_spread_pct:
            problems.append(f"spread {spread:.1%} > {cfg.max_spread_pct:.0%}")
        if not bid <= limit_price <= ask:
            problems.append(f"limit {limit_price} outside bid/ask [{bid}, {ask}]")
    if quote["open_interest"] < cfg.min_open_interest:
        problems.append(f"open interest {quote['open_interest']} < {cfg.min_open_interest}")
    if delta is None or not cfg.min_abs_delta <= abs(delta) <= cfg.max_abs_delta:
        problems.append(f"|delta| {delta} outside [{cfg.min_abs_delta}, {cfg.max_abs_delta}]")
    age = market.quote_age_minutes(option_id, now)
    if age is None or age > cfg.max_quote_age_min:
        problems.append(f"quote is stale ({age if age is None else round(age)} min old)")

    if quantity < 1 or int(quantity) != quantity:
        problems.append("quantity must be a positive whole number of contracts")
    cost = limit_price * quantity * MULTIPLIER
    if cost > cfg.max_premium_per_trade:
        problems.append(f"premium ${cost:.2f} > per-trade cap ${cfg.max_premium_per_trade:.2f}")
    if state.open_premium() + cost > cfg.max_total_premium:
        problems.append(f"total premium at risk would be ${state.open_premium() + cost:.2f} > ${cfg.max_total_premium:.2f}")
    if len(state.positions) >= cfg.max_open_positions:
        problems.append(f"already at max open positions ({cfg.max_open_positions})")
    day_pnl = state.realized_pnl_on(today.isoformat())
    if day_pnl <= -cfg.max_daily_loss:
        problems.append(f"daily loss limit hit (realized {day_pnl:.2f} today)")
    return problems


def check_order(cfg, state, plans, order):
    """Gate a place_option_order call. Returns (ok, reason, parsed)."""
    if str(order.get("account_number")) != cfg.account_number:
        return False, "orders may only target the configured agent account", None
    legs = order.get("legs") or []
    if len(legs) != 1:
        return False, "only single-leg orders are allowed", None
    leg = legs[0]
    if int(leg.get("ratio_quantity") or 1) != 1:
        return False, "ratio_quantity must be 1", None
    if (order.get("type") or "limit") != "limit" or order.get("price") in (None, ""):
        return False, "only limit orders with an explicit price are allowed", None
    if (order.get("market_hours") or "regular_hours") != "regular_hours":
        return False, "only regular-hours orders are allowed", None
    try:
        quantity = float(order["quantity"])
        price = float(order["price"])
    except (KeyError, TypeError, ValueError):
        return False, "quantity and price must be numeric", None

    option_id, side, effect = leg.get("option_id"), leg.get("side"), leg.get("position_effect")
    parsed = {"option_id": option_id, "quantity": quantity, "price": price, "effect": effect}

    if effect == "open":
        if side != "buy":
            return False, "opening orders must be buy-to-open (no short premium)", None
        plan = plans.get(option_id)
        if plan is None:
            return False, "no approved plan for this contract; call propose_option_trade first", None
        if quantity != plan["quantity"] or price > plan["limit_price"]:
            return False, (f"order (qty {quantity} @ {price}) does not match approved plan "
                           f"(qty {plan['quantity']} @ <= {plan['limit_price']})"), None
        return True, "matches approved plan", parsed

    if effect == "close":
        if side != "sell":
            return False, "closing orders must be sell-to-close of a long position", None
        pos = state.positions.get(option_id)
        if pos is None:
            return False, "agent holds no position in this contract", None
        if quantity > pos["quantity"]:
            return False, f"close quantity {quantity} exceeds held {pos['quantity']}", None
        return True, "closing a held position", parsed

    return False, "position_effect must be 'open' or 'close'", None


def review_exits(cfg, state, market, signals, today):
    """Rule-based exit decisions for every agent-held position."""
    out = []
    for option_id, pos in state.positions.items():
        quote = market.quotes.get(option_id)
        row = {"option_id": option_id, "symbol": pos["symbol"], "type": pos["type"], "strike": pos["strike"],
               "expiration": pos["expiration"], "quantity": pos["quantity"], "entry_price": pos["entry_price"]}
        if market.broker_positions is not None and pos["mode"] == "live" and option_id not in market.broker_positions:
            out.append({**row, "action": "DROP", "reason": "not held at broker (unfilled open or closed outside the agent)"})
            continue
        if quote is None or not quote["mark"]:
            out.append({**row, "action": "NEED_QUOTE", "reason": "call get_option_quotes for this contract"})
            continue
        change = (quote["mark"] - pos["entry_price"]) / pos["entry_price"]
        days = dte(pos["expiration"], today)
        sig = signals.get(pos["symbol"])
        reversed_ = sig is not None and sig["decision"] == ("SELL" if pos["direction"] > 0 else "BUY")
        reason = None
        if change >= cfg.take_profit_pct:
            reason = f"take profit ({change:+.0%})"
        elif change <= -cfg.stop_loss_pct:
            reason = f"stop loss ({change:+.0%})"
        elif days <= cfg.exit_dte:
            reason = f"{days} DTE <= exit threshold {cfg.exit_dte}"
        elif reversed_:
            reason = f"signal reversed to {sig['decision']}"
        row.update(mark=quote["mark"], bid=quote["bid"], ask=quote["ask"], change_pct=round(change * 100, 1), dte=days)
        if reason:
            out.append({**row, "action": "CLOSE", "reason": reason,
                        "suggested_limit": round(quote["bid"] or quote["mark"], 2)})
        else:
            out.append({**row, "action": "HOLD", "reason": "no exit rule triggered"})
    return out
