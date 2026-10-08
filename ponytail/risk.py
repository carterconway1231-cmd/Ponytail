"""Hard guardrails. Pure functions over (config, state, market cache) so they
are unit-testable without a broker or a model.

Policy:
  * Opens: single-leg, buy-to-open, limit, long calls/puts only, sized and
    filtered by Config, only in the direction today's signal votes, and only
    while every existing position is protected by a resting stop and no loss
    circuit breaker has tripped.
  * Protective stops: sell-to-close stop orders for the full held quantity at
    or above the required stop (protect.required_stop). Never looser.
  * Closes: sell-to-close limit of a position the agent holds. Exits are never
    blocked by portfolio limits — getting out must always be possible.
"""
from datetime import date, timedelta

from . import protect
from .state import MULTIPLIER

DIRECTION_TO_TYPE = {"BUY": "call", "SELL": "put"}
STOP_TYPES = {"stop_market", "stop_limit"}


def dte(expiration, today):
    return (date.fromisoformat(expiration) - today).days


def unrealized_loss(state, market):
    """Sum of current paper losses on open positions (gains are ignored)."""
    total = 0.0
    for oid, p in state.positions.items():
        mark = (market.quotes.get(oid) or {}).get("mark")
        if mark:
            total += min(0.0, (mark - p["entry_price"]) * p["quantity"] * MULTIPLIER)
    return total


def circuit_breakers(cfg, state, market, today):
    """Portfolio-level reasons to stop opening new risk. Exits are unaffected."""
    tripped = []
    day = today.isoformat()
    day_loss = state.realized_pnl_on(day) + unrealized_loss(state, market)
    if day_loss <= -cfg.max_daily_loss:
        tripped.append(f"daily loss limit: {day_loss:.2f} (realized today + open losses) <= -{cfg.max_daily_loss:g}")
    week = state.realized_since((today - timedelta(days=6)).isoformat())
    if week <= -cfg.max_weekly_loss:
        tripped.append(f"weekly loss limit: {week:.2f} realized over 7 days <= -{cfg.max_weekly_loss:g}")
    streak, last_day = state.losing_streak()
    if streak >= cfg.max_consecutive_losses and last_day == day:
        tripped.append(f"{streak} consecutive losing trades; no new entries until tomorrow")
    naked = protect.unprotected(state, market, today)
    if naked:
        tripped.append(f"positions without an active protective stop: {naked}; place their stops first")
    return tripped


def check_open(cfg, state, market, signals, option_id, quantity, limit_price, today, now=None):
    """Return a list of violated rules (empty means approved)."""
    problems = []
    inst = market.instruments.get(option_id)
    quote = market.quotes.get(option_id)
    if inst is None:
        return ["contract not seen via get_option_instruments this run"]
    if quote is None:
        return ["contract not quoted via get_option_quotes this run"]

    problems += circuit_breakers(cfg, state, market, today)

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
    last_loss = state.last_loss_on(symbol)
    if last_loss and protect.days_between(last_loss, today) < cfg.loss_cooldown_days:
        problems.append(f"{symbol} had a losing exit on {last_loss}; cooling down {cfg.loss_cooldown_days} days")

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
    return problems


def _check_stop(cfg, pos, market, order, quantity, today):
    """Validate a protective stop order. Returns an error string or None."""
    inst, quote = market.instruments.get(pos["option_id"]), market.quotes.get(pos["option_id"])
    if not protect.is_confirmed(pos, market):
        return "position fill not confirmed yet; fetch get_option_positions (nonzero=true) first"
    if protect.stop_is_active(pos, today):
        return f"a stop is already active (order {pos['stop']['order_id']}); cancel it before replacing"
    if order.get("type") != cfg.stop_order_type:
        return f"protective stops must be type {cfg.stop_order_type}"
    if quantity != pos["quantity"]:
        return f"stop must cover the full position ({pos['quantity']:g} contracts)"
    try:
        stop_price = float(order["stop_price"])
    except (KeyError, TypeError, ValueError):
        return "stop_price is required"
    required = protect.required_stop(cfg, pos, inst)
    if stop_price < required - 1e-9:
        return f"stop {stop_price} is looser than required {required}; protective stops can only be at or above it"
    if quote and quote.get("bid") and stop_price >= quote["bid"]:
        return (f"bid {quote['bid']} is already at/below the stop {stop_price}: the stop level is breached, "
                "close the position now with a sell-to-close limit order instead")
    if cfg.stop_order_type == "stop_limit":
        try:
            limit = float(order["price"])
        except (KeyError, TypeError, ValueError):
            return "stop_limit requires price"
        if limit > stop_price or limit < stop_price * (1 - cfg.stop_limit_buffer_pct) - 0.05:
            return f"stop_limit price must be within {cfg.stop_limit_buffer_pct:.0%} below the stop"
    elif order.get("price") not in (None, ""):
        return "stop_market must not carry a price"
    if (order.get("time_in_force") or "gfd") != ("gtc" if cfg.stop_order_type == "stop_limit" else "gfd"):
        return "stop_market must be gfd; stop_limit must be gtc"
    return None


def check_order(cfg, state, market, plans, order, today):
    """Gate a place_option_order call. Returns (ok, reason, parsed)."""
    if str(order.get("account_number")) != cfg.account_number:
        return False, "orders may only target the configured agent account", None
    legs = order.get("legs") or []
    if len(legs) != 1:
        return False, "only single-leg orders are allowed", None
    leg = legs[0]
    if int(leg.get("ratio_quantity") or 1) != 1:
        return False, "ratio_quantity must be 1", None
    if (order.get("market_hours") or "regular_hours") != "regular_hours":
        return False, "only regular-hours orders are allowed", None
    try:
        quantity = float(order["quantity"])
    except (KeyError, TypeError, ValueError):
        return False, "quantity must be numeric", None

    option_id, side, effect = leg.get("option_id"), leg.get("side"), leg.get("position_effect")
    otype = order.get("type") or "limit"
    parsed = {"option_id": option_id, "quantity": quantity, "effect": effect, "type": otype}

    if otype in STOP_TYPES:
        if side != "sell" or effect != "close":
            return False, "stop orders are only allowed as sell-to-close protection", None
        pos = state.positions.get(option_id)
        if pos is None:
            return False, "agent holds no position in this contract", None
        err = _check_stop(cfg, {**pos, "option_id": option_id}, market, order, quantity, today)
        if err:
            return False, err, None
        parsed.update(effect="stop", stop_price=float(order["stop_price"]),
                      price=float(order["price"]) if otype == "stop_limit" else None)
        return True, "protective stop", parsed

    if otype != "limit" or order.get("price") in (None, ""):
        return False, "entries and exits must be limit orders; stops must be stop_market/stop_limit", None
    try:
        parsed["price"] = price = float(order["price"])
    except (TypeError, ValueError):
        return False, "price must be numeric", None

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
        if pos.get("pending_close"):
            return False, f"a close order is already working ({pos['pending_close']['order_id']})", None
        if pos["mode"] == "live" and protect.stop_is_active(pos, today):
            return False, (f"contracts are reserved by protective stop {pos['stop']['order_id']}; "
                           "cancel_option_order it first, then close"), None
        return True, "closing a held position", parsed

    return False, "position_effect must be 'open' or 'close'", None


def review_exits(cfg, state, market, signals, today):
    """Rule-based exit and stop-maintenance decisions for every held position."""
    out = []
    for option_id, pos in state.positions.items():
        quote = market.quotes.get(option_id)
        inst = market.instruments.get(option_id)
        row = {"option_id": option_id, "symbol": pos["symbol"], "type": pos["type"], "strike": pos["strike"],
               "expiration": pos["expiration"], "quantity": pos["quantity"], "entry_price": pos["entry_price"],
               "high_water_mark": pos.get("hwm"), "mode": pos["mode"]}
        if not protect.is_confirmed({**pos, "option_id": option_id}, market):
            out.append({**row, "action": "WAIT_FILL", "reason": "opening order not confirmed filled; check get_option_orders"})
            continue
        if pos.get("pending_close"):
            out.append({**row, "action": "PENDING_CLOSE", "reason": f"close order {pos['pending_close']['order_id']} working"})
            continue
        if quote is None or not quote["mark"]:
            out.append({**row, "action": "NEED_QUOTE", "reason": "call get_option_quotes for this contract"})
            continue
        change = (quote["mark"] - pos["entry_price"]) / pos["entry_price"]
        days = dte(pos["expiration"], today)
        sig = signals.get(pos["symbol"])
        reversed_ = sig is not None and sig["decision"] == ("SELL" if pos["direction"] > 0 else "BUY")
        required = protect.required_stop(cfg, pos, inst)
        reason = None
        if change >= cfg.take_profit_pct:
            reason = f"take profit ({change:+.0%})"
        elif change <= -cfg.stop_loss_pct or (quote["bid"] or 0) <= required:
            reason = f"stop level breached ({change:+.0%}, bid {quote['bid']} vs stop {required})"
        elif days <= cfg.exit_dte:
            reason = f"{days} DTE <= exit threshold {cfg.exit_dte}"
        elif reversed_:
            reason = f"signal reversed to {sig['decision']}"
        row.update(mark=quote["mark"], bid=quote["bid"], ask=quote["ask"], change_pct=round(change * 100, 1),
                   dte=days, required_stop=required, active_stop=pos.get("stop") if protect.stop_is_active(pos, today) else None)
        active = row["active_stop"]
        cancel_first = active["order_id"] if active and pos["mode"] == "live" else None
        if reason:
            out.append({**row, "action": "CLOSE", "reason": reason, "cancel_stop_first": cancel_first,
                        "suggested_limit": round(quote["bid"] or quote["mark"], 2)})
        elif active is None:
            out.append({**row, "action": "PLACE_STOP", "reason": "position is unprotected",
                        "stop_order": protect.stop_order_args(cfg, option_id, pos, required, inst)})
        elif active["stop_price"] < required - 1e-9:
            out.append({**row, "action": "RAISE_STOP", "reason": f"trailing stop rises {active['stop_price']} -> {required}",
                        "cancel_stop_first": active["order_id"],
                        "stop_order": protect.stop_order_args(cfg, option_id, pos, required, inst)})
        else:
            out.append({**row, "action": "HOLD", "reason": f"protected by stop at {active['stop_price']}"})
    return out
