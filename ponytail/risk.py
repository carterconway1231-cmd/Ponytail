"""Hard guardrails. Pure functions over (config, state, market cache) so they
are unit-testable without a broker or a model.

Policy:
  * Opens: buy-to-open long calls/puts, or debit verticals (buy one, sell a
    further-OTM strike of the same expiration), as limit orders only, sized
    and filtered by Config, only in the direction today's signal votes, and
    only while every held single is protected by a resting stop and no loss
    circuit breaker has tripped. Never short premium on its own.
  * Protective stops: sell-to-close stop orders on singles for the full held
    quantity at or above the required stop (protect.required_stop). Debit
    spreads are defined-risk (max loss = debit) and Robinhood stop orders are
    single-leg only, so spreads are exited by the rule-based review instead.
  * Closes: limit closes of positions the agent holds. Exits are never
    blocked by portfolio limits: getting out must always be possible.
"""
from datetime import date, datetime, timedelta, timezone

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
        m = protect.position_mark(oid, p, market)
        if m:
            total += min(0.0, (m[0] - p["entry_price"]) * p["quantity"] * MULTIPLIER)
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


def _leg_quality(cfg, market, option_id, label, now):
    q = market.quotes.get(option_id)
    problems = []
    if not q["bid"] or not q["ask"] or q["bid"] <= 0 or q["ask"] <= 0:
        problems.append(f"{label}: no two-sided market")
    elif (q["ask"] - q["bid"]) / ((q["ask"] + q["bid"]) / 2) > cfg.max_spread_pct:
        problems.append(f"{label}: bid/ask spread {(q['ask'] - q['bid']) / ((q['ask'] + q['bid']) / 2):.1%} "
                        f"> {cfg.max_spread_pct:.0%}")
    if q["open_interest"] < cfg.min_open_interest:
        problems.append(f"{label}: open interest {q['open_interest']} < {cfg.min_open_interest}")
    age = market.quote_age_minutes(option_id, now)
    if age is None or age > cfg.max_quote_age_min:
        problems.append(f"{label}: quote is stale ({age if age is None else round(age)} min old)")
    return problems


def spread_debit_range(market, long_id, short_id):
    """(best-case debit at mids-crossed bid side, natural debit) per share."""
    lq, sq = market.quotes[long_id], market.quotes[short_id]
    return max(0.01, lq["bid"] - sq["ask"]), lq["ask"] - sq["bid"]


def check_open(cfg, state, market, signals, option_id, quantity, limit_price, today, now=None,
               short_option_id=None, universe=None):
    """Return a list of violated rules (empty means approved). limit_price is
    the per-share debit: the option price, or the spread's net debit."""
    problems = []
    inst = market.instruments.get(option_id)
    if inst is None:
        return ["contract not seen via get_option_instruments this run"]
    if market.quotes.get(option_id) is None:
        return ["contract not quoted via get_option_quotes this run"]
    short = None
    if short_option_id:
        short = market.instruments.get(short_option_id)
        if short is None or market.quotes.get(short_option_id) is None:
            return ["short leg not seen and quoted this run"]

    problems += circuit_breakers(cfg, state, market, today)

    symbol = inst["symbol"]
    signal = signals.get(symbol)
    if signal is None:
        problems.append(f"no compute_signals result for {symbol} this run")
    elif signal["decision"] == "HOLD":
        problems.append(f"{symbol} signal is HOLD (score {signal['score']:.2f})")
    elif DIRECTION_TO_TYPE[signal["decision"]] != inst["type"]:
        problems.append(f"{symbol} signal is {signal['decision']} but contract is a {inst['type']}")
    if signal is not None and signal.get("bucket_trades", 0) >= cfg.min_calibration_trades:
        # Enough history at this conviction level for the learned odds to veto.
        if signal["p_win"] < cfg.min_win_prob:
            problems.append(f"learned win rate {signal['p_win']:.0%} at {signal['bucket']} conviction < {cfg.min_win_prob:.0%}")
        if signal.get("expected_r") is not None and signal["expected_r"] <= 0:
            problems.append(f"learned expected return {signal['expected_r']:+.2f}R at {signal['bucket']} conviction is not positive")

    if symbol not in (universe or cfg.symbols):
        problems.append(f"{symbol} is not in today's universe (SYMBOLS + scanner discoveries)")
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

    delta = market.quotes[option_id]["delta"]
    if delta is None or not cfg.min_abs_delta <= abs(delta) <= cfg.max_abs_delta:
        problems.append(f"|delta| {delta} outside [{cfg.min_abs_delta}, {cfg.max_abs_delta}]")
    problems += _leg_quality(cfg, market, option_id, "long leg" if short else "contract", now)

    if short is None:
        q = market.quotes[option_id]
        if q["bid"] and q["ask"] and not q["bid"] <= limit_price <= q["ask"]:
            problems.append(f"limit {limit_price} outside bid/ask [{q['bid']}, {q['ask']}]")
    else:
        problems += _check_spread_shape(cfg, market, option_id, inst, short_option_id, short, limit_price, now)

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


def _check_spread_shape(cfg, market, long_id, inst, short_id, short, debit, now):
    problems = []
    if not cfg.allow_spreads:
        return ["spreads are disabled (ALLOW_SPREADS=false)"]
    if (short["symbol"], short["expiration"], short["type"]) != (inst["symbol"], inst["expiration"], inst["type"]):
        problems.append("short leg must be the same underlying, expiration and type as the long leg")
    further_otm = short["strike"] > inst["strike"] if inst["type"] == "call" else short["strike"] < inst["strike"]
    if not further_otm:
        problems.append("short leg must be further out of the money than the long leg (debit vertical)")
    if not short["tradable"]:
        problems.append("short leg is not active/tradable")
    problems += _leg_quality(cfg, market, short_id, "short leg", now)
    width = abs(short["strike"] - inst["strike"])
    lo, natural = spread_debit_range(market, long_id, short_id)
    if not lo - 1e-9 <= debit <= natural + 1e-9:
        problems.append(f"net debit {debit} outside the leg markets [{lo:.2f}, {natural:.2f}]")
    if width and debit > cfg.max_spread_debit_pct * width:
        problems.append(f"debit {debit} is more than {cfg.max_spread_debit_pct:.0%} of the {width:g} strike width "
                        "(reward/risk too poor)")
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


def _split_legs(legs):
    """Return (long_leg, short_leg_or_None) for 1-2 leg orders, or None if malformed."""
    if len(legs) == 1:
        return legs[0], None
    if len(legs) == 2:
        effects = {leg.get("position_effect") for leg in legs}
        if len(effects) != 1:
            return None
        effect = effects.pop()
        long_side = "buy" if effect == "open" else "sell"
        longs = [leg for leg in legs if leg.get("side") == long_side]
        shorts = [leg for leg in legs if leg.get("side") != long_side]
        if len(longs) == 1 and len(shorts) == 1:
            return longs[0], shorts[0]
    return None


def check_order(cfg, state, market, plans, order, today):
    """Gate a place_option_order call. Returns (ok, reason, parsed)."""
    if str(order.get("account_number")) != cfg.account_number:
        return False, "orders may only target the configured agent account", None
    legs = order.get("legs") or []
    split = _split_legs(legs)
    if split is None:
        return False, "only single options or 2-leg debit verticals are allowed", None
    leg, short_leg = split
    if any(int(x.get("ratio_quantity") or 1) != 1 for x in legs):
        return False, "ratio_quantity must be 1", None
    if (order.get("market_hours") or "regular_hours") != "regular_hours":
        return False, "only regular-hours orders are allowed", None
    try:
        quantity = float(order["quantity"])
    except (KeyError, TypeError, ValueError):
        return False, "quantity must be numeric", None

    option_id, side, effect = leg.get("option_id"), leg.get("side"), leg.get("position_effect")
    short_id = short_leg.get("option_id") if short_leg else None
    otype = order.get("type") or "limit"
    parsed = {"option_id": option_id, "short_option_id": short_id, "quantity": quantity, "effect": effect, "type": otype}

    if otype in STOP_TYPES:
        if short_leg or side != "sell" or effect != "close":
            return False, "stop orders are only allowed as single-leg sell-to-close protection", None
        pos = state.positions.get(option_id)
        if pos is None:
            return False, "agent holds no position in this contract", None
        if protect.is_spread(pos):
            return False, "spreads are defined-risk and exit by review; stop orders are single-leg only", None
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
    if short_leg and order.get("direction") != ("debit" if effect == "open" else "credit"):
        return False, "spread opens must be direction 'debit' and closes 'credit'", None

    if effect == "open":
        if side != "buy":
            return False, "opening orders must be buy-to-open (no short premium on its own)", None
        plan = plans.get(option_id)
        if plan is None:
            return False, "no approved plan for this contract; call propose_option_trade first", None
        if plan.get("short_option_id") != short_id:
            return False, "order legs do not match the approved plan's structure", None
        if quantity != plan["quantity"] or price > plan["limit_price"]:
            return False, (f"order (qty {quantity} @ {price}) does not match approved plan "
                           f"(qty {plan['quantity']} @ <= {plan['limit_price']})"), None
        return True, "matches approved plan", parsed

    if effect == "close":
        if side != "sell":
            return False, "closing orders must sell-to-close the long leg", None
        pos = state.positions.get(option_id)
        if pos is None:
            return False, "agent holds no position in this contract", None
        if (pos.get("short_option_id") if protect.is_spread(pos) else None) != short_id:
            return False, "close must include exactly the position's legs", None
        if quantity > pos["quantity"]:
            return False, f"close quantity {quantity} exceeds held {pos['quantity']}", None
        if pos.get("pending_close"):
            return False, f"a close order is already working ({pos['pending_close']['order_id']})", None
        if pos["mode"] == "live" and protect.stop_is_active(pos, today):
            return False, (f"contracts are reserved by protective stop {pos['stop']['order_id']}; "
                           "cancel_option_order it first, then close"), None
        return True, "closing a held position", parsed

    return False, "position_effect must be 'open' or 'close'", None


def close_order_args(cfg, option_id, pos, limit):
    legs = [{"option_id": option_id, "side": "sell", "position_effect": "close"}]
    args = {"quantity": f"{pos['quantity']:g}", "type": "limit", "price": f"{max(0.01, limit):.2f}", "time_in_force": "gfd"}
    if protect.is_spread(pos):
        legs.append({"option_id": pos["short_option_id"], "side": "buy", "position_effect": "close"})
        args["direction"] = "credit"
    return {"account_number": cfg.account_number, "legs": legs, **args}


def review_exits(cfg, state, market, signals, today, now=None):
    """Rule-based exit and stop-maintenance decisions for every held position."""
    out = []
    now = now or datetime.now(timezone.utc)
    for option_id, pos in state.positions.items():
        inst = market.instruments.get(option_id)
        spread = protect.is_spread(pos)
        row = {"option_id": option_id, "symbol": pos["symbol"], "type": pos["type"], "strike": pos["strike"],
               "kind": pos.get("kind", "single"), "expiration": pos["expiration"], "quantity": pos["quantity"],
               "entry_price": pos["entry_price"], "high_water_mark": pos.get("hwm"), "mode": pos["mode"]}
        if spread:
            row.update(short_option_id=pos["short_option_id"], short_strike=pos["short_strike"])
        if not protect.is_confirmed({**pos, "option_id": option_id}, market):
            out.append({**row, "action": "WAIT_FILL", "reason": "opening order not confirmed filled; check get_option_orders"})
            continue
        if pos.get("pending_close"):
            out.append({**row, "action": "PENDING_CLOSE", "reason": f"close order {pos['pending_close']['order_id']} working"})
            continue
        m = protect.position_mark(option_id, pos, market)
        if m is None:
            legs = "both legs" if spread else "this contract"
            out.append({**row, "action": "NEED_QUOTE", "reason": f"call get_option_quotes for {legs}"})
            continue
        mark, exit_bid, exit_ask = m
        change = (mark - pos["entry_price"]) / pos["entry_price"]
        days = dte(pos["expiration"], today)
        held = (now - datetime.fromisoformat(pos["opened_at"])).days
        sig = signals.get(pos["symbol"])
        reversed_ = sig is not None and sig["decision"] == ("SELL" if pos["direction"] > 0 else "BUY")
        required = None if spread else protect.required_stop(cfg, pos, inst)
        reason = None
        if change >= cfg.take_profit_pct:
            reason = f"take profit ({change:+.0%})"
        elif change <= -cfg.stop_loss_pct or (required is not None and (exit_bid or 0) <= required):
            reason = f"stop level breached ({change:+.0%})"
        elif days <= cfg.exit_dte:
            reason = f"{days} DTE <= exit threshold {cfg.exit_dte}"
        elif held >= cfg.time_stop_days and change < cfg.time_stop_min_gain:
            reason = f"time stop: {held} days held and only {change:+.0%} (theta is eating it)"
        elif reversed_:
            reason = f"signal reversed to {sig['decision']}"
        row.update(mark=round(mark, 3), exit_bid=round(exit_bid, 3), exit_ask=round(exit_ask, 3),
                   change_pct=round(change * 100, 1), dte=days, held_days=held, required_stop=required,
                   active_stop=pos.get("stop") if protect.stop_is_active(pos, today) else None)
        active = row["active_stop"]
        if reason:
            # Limit halfway between the natural exit price and the mark.
            limit = round(exit_bid + 0.5 * max(0.0, mark - exit_bid), 2) if spread else round(exit_bid or mark, 2)
            out.append({**row, "action": "CLOSE", "reason": reason,
                        "cancel_stop_first": active["order_id"] if active and pos["mode"] == "live" else None,
                        "close_order": close_order_args(cfg, option_id, pos, limit)})
        elif spread:
            out.append({**row, "action": "HOLD", "reason": f"defined-risk spread (max loss = {pos['entry_price']} debit)"})
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
