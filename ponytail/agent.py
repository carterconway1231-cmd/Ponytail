"""Hybrid options agent: a deterministic signal + risk engine wrapped around a
Claude agent loop that talks to the official Robinhood MCP server.

Division of labor:
  * Code decides direction (factors.py + learner.py), structure from the
    volatility regime (volatility.py), candidate contracts by expected value
    (selection.py) and size by edge (sizing.py).
  * Claude does the judgment work: reads the chart narrative, vetoes on
    context (earnings, fundamentals, news-driven moves), chooses among the
    ranked candidates, and manages exits.
  * Code (risk.py, enforced in PreToolUse hooks) has the final say on every
    order. A denied tool call never reaches Robinhood.
"""
import dataclasses
import json
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, HookMatcher, ResultMessage, TextBlock, ToolUseBlock,
    create_sdk_mcp_server, query, tool,
)

from . import alerts, events as macro, exits, performance, protect, risk, selection, sizing
from .factors import compute_factors
from .learner import Learner
from .market import MarketCache, decode_tool_response
from .state import State, now_iso
from .volatility import VolBook, atm_iv, realized_vol
from .warmstart import replay

ET = ZoneInfo("America/New_York")

log = logging.getLogger("ponytail")

RH = "mcp__Robinhood__"
LOCAL = "mcp__ponytail__"

RH_READ_TOOLS = {
    "get_accounts", "get_portfolio", "get_equity_quotes", "get_equity_historicals",
    "get_equity_fundamentals", "get_equity_analyst_ratings", "get_equity_technical_indicators",
    "get_earnings_results", "get_option_chains", "get_option_instruments", "get_option_quotes",
    "get_option_positions", "get_option_orders", "search",
    "preview_scan", "get_indexes", "get_index_historicals", "get_index_quotes",
}
RH_WRITE_TOOLS = {"review_option_order", "place_option_order", "cancel_option_order"}
LOCAL_TOOLS = {"compute_signals", "rank_contracts", "propose_option_trade", "review_positions", "portfolio_status",
               "learning_report", "warm_start", "performance_report"}

MONITOR_BLOCKED = {LOCAL + t for t in ("compute_signals", "rank_contracts", "propose_option_trade", "warm_start")}

ALLOWED_TOOLS = (
    {RH + t for t in RH_READ_TOOLS | RH_WRITE_TOOLS}
    | {LOCAL + t for t in LOCAL_TOOLS}
    | {"ToolSearch"}
)


def _text(obj):
    return {"content": [{"type": "text", "text": json.dumps(obj, indent=1, default=str)}]}


def _deny(reason):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}


class TradingSession:
    """Everything one agent run needs; hooks and tools close over it."""

    def __init__(self, cfg, today=None, monitor=False, clock=None):
        self.cfg = cfg
        self.today = today or date.today()
        self.monitor = monitor  # cheap position-management run: no new entries
        self.clock = clock or (lambda: datetime.now(ET))
        self.state = State.load(cfg.state_path)
        if cfg.adaptive_exits:
            learned = exits.tune(cfg, self.state)
            if learned:
                cfg = self.cfg = dataclasses.replace(cfg, take_profit_pct=learned["take_profit_pct"],
                                                     stop_loss_pct=learned["stop_loss_pct"])
        self.learner = self.state.learner = Learner(self.state.data["learner"], cfg, today=self.today)
        self.vols = VolBook(self.state.data.setdefault("iv_history", {}), cfg)
        self.macro_events = macro.load_events(cfg.events_path)
        self.market = MarketCache()
        self.signals = {}  # symbol -> {votes, score, decision, rsi}
        self.plans = {}    # option_id -> approved open plan
        self.events = []   # audit trail of guardrail decisions this run
        self.cancelable = set()  # order ids review_positions cleared for cancellation
        self.exit_reasons = {}   # option_id -> why review_positions said CLOSE (for the trade log)
        self.breaker_alerted = False

    @property
    def mode(self):
        return "live" if self.cfg.live_trading else "paper"

    @property
    def discovered(self):
        """Top scanner candidates (unusual options activity) outside the core universe."""
        if not self.cfg.discover or not self.market.scan:
            return []
        rows = [r for r in self.market.scan if r["symbol"] not in self.cfg.symbols
                and (r["last"] or 0) >= 10 and (r["options_volume"] or 0) >= 20000]
        rows.sort(key=lambda r: -(r["rel_options_volume"] or 0))
        return [r["symbol"] for r in rows[:self.cfg.max_discovered]]

    @property
    def universe(self):
        return list(self.cfg.symbols) + self.discovered

    def equity(self):
        if self.cfg.live_trading:
            return self.market.equity
        return self.cfg.paper_capital + sum(t["pnl"] for t in self.state.trade_log if t.get("mode") == "paper")

    def entry_window_open(self):
        now = self.clock().strftime("%H:%M")
        start, end = self.cfg.entry_window
        return start <= now <= end

    def vol_assessment(self, symbol):
        iv = atm_iv(symbol, self.market, self.today)
        if iv:
            self.vols.record(symbol, self.today.isoformat(), iv)
        daily = self.market.bars.get(symbol, {}).get("day") or []
        return self.vols.assess(symbol, iv, realized_vol(daily) if daily else None)

    def audit(self, kind, **fields):
        event = {"at": now_iso(), "kind": kind, **fields}
        self.events.append(event)
        log.info("%s %s", kind, json.dumps(fields, default=str))
        if kind in alerts.ALERT_KINDS:
            alerts.send(self.cfg.alert_webhook_url, alerts.format_event(kind, fields, self.mode))

    # ---- deterministic tools exposed to Claude -------------------------

    def compute_signals(self, symbol):
        symbol = symbol.upper()
        bars = self.market.bars.get(symbol, {})
        daily = bars.get("day")
        if not daily:
            return {"error": f"no daily bars for {symbol}; call get_equity_historicals with interval='day' first"}
        try:
            context = {k: self.market.bars.get(k, {}).get("day") for k in ("SPY", "VIX")}
            analysis = compute_factors(daily, bars.get("hour"), context)
        except ValueError as e:
            return {"error": f"{e}; request a longer start_time"}
        labeled = self.learner.label_snapshots(symbol, daily)
        decision = self.learner.decide(analysis["factors"], analysis["regime"])
        self.learner.record_snapshot(symbol, self.today.isoformat(), analysis, decision)
        self.signals[symbol] = {**decision, "regime": analysis["regime"],
                                "factors": {k: {"score": v["score"]} for k, v in analysis["factors"].items()}}
        self.state.save()
        weights = {k: round(self.learner.weight(k, analysis["regime"]), 2) for k in analysis["factors"]}
        return {
            "symbol": symbol, "close": analysis["close"], "regime": f"{analysis['regime']} (ADX {analysis['adx']})",
            **decision, "implies": risk.DIRECTION_TO_TYPE.get(decision["decision"], "no trade"),
            "hourly_factors": "included" if analysis["has_hourly"] else "MISSING: fetch interval='hour' bars",
            "factors": {k: {**v, "learned_weight": weights[k]} for k, v in
                        sorted(analysis["factors"].items(), key=lambda kv: -abs(kv[1]["score"]) * weights[kv[0]])},
            "past_signals_graded_now": labeled,
        }

    def rank_contracts(self, symbol, structure="auto"):
        symbol = symbol.upper()
        signal = self.signals.get(symbol)
        if not signal or signal["decision"] == "HOLD":
            return {"error": f"no BUY/SELL signal for {symbol} this run; run compute_signals first"}
        vol = self.vol_assessment(symbol)
        if structure == "auto":
            structure = vol["structure"]
        if structure == "skip":
            return {"vol": vol, "candidates": [], "note": "IV is expensive and spreads are disabled: skip this one"}
        daily = self.market.bars.get(symbol, {}).get("day") or []
        rows = selection.rank(self.cfg, self.market, signal, symbol, realized_vol(daily) if daily else None,
                              self.today, structure)
        equity = self.equity()
        for r in rows:
            n, info = sizing.max_contracts(self.cfg, equity or 0, signal, r["expected_cost"], bool(r["short_option_id"]))
            r["max_contracts"], r["sizing"] = n, info
        if not rows:
            return {"vol": vol, "candidates": [],
                    "note": "no candidates: quote more strikes/expirations (and further-OTM strikes for spreads)"}
        return {"vol": vol, "structure": structure, "candidates": rows,
                "note": "propose the best candidate whose story you believe, at or below suggested_limit"}

    def propose_option_trade(self, option_id, quantity, limit_price, thesis, short_option_id=None):
        cfg = self.cfg
        problems = risk.check_open(cfg, self.state, self.market, self.signals, option_id, quantity, limit_price,
                                   self.today, short_option_id=short_option_id, universe=self.universe)
        inst = self.market.instruments.get(option_id, {})
        plan_extra = {}
        if self.monitor:
            problems.append("monitor runs manage positions only; new entries happen on full runs")
        if not self.entry_window_open():
            problems.append(f"outside the entry window {'-'.join(cfg.entry_window)} ET (open/close auctions are "
                            "where spreads are widest)")
        for e in macro.blackout(self.macro_events, self.today, cfg.event_blackout_days):
            problems.append(f"macro blackout: {e['name']} on {e['date']}")
        signal = self.signals.get(inst.get("symbol"))
        if inst and signal and option_id in self.market.quotes and \
                (not short_option_id or short_option_id in self.market.quotes):
            vol = self.vol_assessment(inst["symbol"])
            if vol["structure"] == "skip":
                problems.append(f"IV is expensive ({vol}) and spreads are disabled")
            elif vol["structure"] == "debit_spread" and not short_option_id:
                problems.append(f"IV is expensive (rank {vol['iv_rank']}, IV/RV {vol['iv_rv_ratio']}): "
                                "use a debit spread to sell back the rich premium")
            spot = selection.underlying_spot(self.market, inst["symbol"])
            daily = self.market.bars.get(inst["symbol"], {}).get("day") or []
            if spot is None:
                problems.append("no underlying price: fetch bars first")
            else:
                ev = selection.evaluate(cfg, self.market, signal, spot, realized_vol(daily) if daily else None,
                                        option_id, short_option_id, self.today)
                ev_at_limit = (ev["ev_per_share"] + ev["expected_cost"] - limit_price) / limit_price
                if ev_at_limit < cfg.min_contract_ev:
                    problems.append(f"expected value {ev_at_limit:+.1%} per dollar at limit {limit_price} is below "
                                    f"{cfg.min_contract_ev:+.0%} (premium/decay outweigh the measured edge)")
                plan_extra.update(entry_mid=ev["mid"], ev_per_dollar=round(ev_at_limit, 3), vol_regime=vol["regime"])
            equity = self.equity()
            if equity is None:
                problems.append("account equity unknown: call get_portfolio first")
            else:
                n, info = sizing.max_contracts(cfg, equity, signal, limit_price, bool(short_option_id))
                if quantity > n:
                    problems.append(f"{quantity:g} contracts exceeds the {n} the risk budget allows ({info})")
        if problems:
            self.audit("plan_rejected", option_id=option_id, symbol=inst.get("symbol"), problems=problems)
            return {"approved": False, "problems": problems}
        self.plans[option_id] = {"quantity": float(quantity), "limit_price": float(limit_price), "thesis": thesis,
                                 "short_option_id": short_option_id, **plan_extra}
        cost = limit_price * quantity * 100
        self.audit("plan_approved", option_id=option_id, short_option_id=short_option_id, contract=inst,
                   quantity=quantity, limit_price=limit_price, cost=cost, thesis=thesis, **plan_extra)
        legs = [{"option_id": option_id, "side": "buy", "position_effect": "open"}]
        if short_option_id:
            legs.append({"option_id": short_option_id, "side": "sell", "position_effect": "open"})
        order = {"account_number": self.cfg.account_number, "legs": legs, "quantity": f"{quantity:g}",
                 "type": "limit", "price": f"{limit_price:.2f}", "time_in_force": "gfd",
                 **({"direction": "debit"} if short_option_id else {})}
        return {"approved": True, "contract": inst, "quantity": quantity, "limit_price": limit_price,
                "premium_at_risk": round(cost, 2), **plan_extra,
                "next": "review_option_order, then place_option_order with exactly these arguments", "order": order}

    def review_positions(self):
        events = protect.reconcile(self.cfg, self.state, self.market, self.today)
        events += protect.simulate_paper_stops(self.state, self.market, self.today)
        for e in events:
            self.audit(e["kind"], **{k: v for k, v in e.items() if k != "kind"})
        rows = risk.review_exits(self.cfg, self.state, self.market, self.signals, self.today)
        self.exit_reasons.update({r["option_id"]: r["reason"] for r in rows if r["action"] == "CLOSE"})
        # Cancelling a live stop is only allowed to exit or to ratchet it higher.
        self.cancelable = {r["cancel_stop_first"] for r in rows if r.get("cancel_stop_first")}
        self.cancelable |= {p["open_order_id"] for p in self.state.positions.values()
                            if p["mode"] == "live" and p.get("open_order_id") and not p.get("filled")}
        self.state.save()
        breakers = risk.circuit_breakers(self.cfg, self.state, self.market, self.today)
        if breakers and not self.breaker_alerted:
            self.breaker_alerted = True
            self.audit("circuit_breaker", tripped=breakers)
        return {"mode": self.mode, "broker_events": events, "positions": rows, "circuit_breakers": breakers}

    def needs_warm_start(self):
        return [sym for sym in self.cfg.symbols if sym not in self.learner.d["warm_start"]["symbols"]]

    def warm_start(self):
        todo = {sym: bars for sym, bars in self.market.bars.items() if "day" in bars}
        if not todo:
            return {"error": "no bars cached; fetch long history with get_equity_historicals first"}
        summary = replay(self.learner, todo)
        self.state.save()
        self.audit("warm_start", graded=summary["graded"], symbols=summary["symbols"])
        top = self.learner.report()["factors"]
        return {**summary, "still_needed": self.needs_warm_start(),
                "learned_weights": [{k: r[k] for k in ("factor", "hit_rate_trend", "hit_rate_range",
                                                       "weight_trend", "weight_range")} for r in top]}

    def performance(self):
        capital = self.cfg.paper_capital if not self.cfg.live_trading else (self.market.equity or self.cfg.paper_capital)
        spy = self.market.bars.get("SPY", {}).get("day")
        return {"report": performance.report(self.state, capital, mode=self.mode, spy_bars=spy),
                "go_live": performance.go_live_check(self.cfg, self.state, self.today)}

    def portfolio_status(self):
        day = self.today.isoformat()
        log_ = self.state.trade_log
        return {
            "mode": self.mode, "account_number": self.cfg.account_number, "today": day,
            "universe": self.cfg.symbols,
            "learning": {k: self.learner.d[k] for k in ("trades_learned", "shadow_learned")},
            "needs_warm_start": self.needs_warm_start(),
            "open_positions": self.state.positions, "open_premium": round(self.state.open_premium(), 2),
            "realized_pnl_today": round(self.state.realized_pnl_on(day), 2),
            "realized_pnl_all_time": round(sum(t["pnl"] for t in log_), 2), "closed_trades": len(log_),
            "circuit_breakers": risk.circuit_breakers(self.cfg, self.state, self.market, self.today),
            "equity": self.equity(), "universe": self.universe, "discover": self.cfg.discover,
            "discovered": self.discovered,
            "entry_window_open": self.entry_window_open(),
            "upcoming_events": macro.blackout(self.macro_events, self.today, 14),
            "event_calendar_warnings": macro.calendar_warnings(self.macro_events, self.today),
            "performance": self.performance(),
            "limits": {k: getattr(self.cfg, k) for k in (
                "max_premium_per_trade", "max_total_premium", "max_open_positions", "max_daily_loss",
                "max_weekly_loss", "max_consecutive_losses", "loss_cooldown_days",
                "min_dte", "max_dte", "min_abs_delta", "max_abs_delta", "max_spread_pct",
                "min_open_interest", "avoid_earnings", "take_profit_pct", "stop_loss_pct",
                "trail_activate_pct", "trail_pct", "stop_order_type", "exit_dte")},
        }

    # ---- hooks --------------------------------------------------------

    async def pre_tool_use(self, input_data, tool_use_id, context):
        name = input_data["tool_name"]
        if name not in ALLOWED_TOOLS:
            self.audit("tool_blocked", tool=name)
            return _deny(f"{name} is not permitted for this agent")
        args = input_data.get("tool_input") or {}
        if self.monitor and name in MONITOR_BLOCKED:
            return _deny("monitor run: manage existing positions only; signals and entries run on full cycles")

        if name in (RH + "review_option_order", RH + "cancel_option_order", RH + "get_option_positions",
                    RH + "get_option_orders", RH + "get_portfolio"):
            if str(args.get("account_number")) != self.cfg.account_number:
                return _deny(f"use account_number {self.cfg.account_number}")

        if name == RH + "review_option_order" and not self.cfg.live_trading:
            # Broker review checks real buying power/options level, which a paper
            # account may not have; the guardrail plan check already ran.
            return _deny("PAPER MODE: broker review skipped. Proceed to place_option_order.")

        if name == RH + "cancel_option_order":
            return self._gate_cancel(args.get("order_id"))

        if name == RH + "place_option_order":
            ok, reason, order = risk.check_order(self.cfg, self.state, self.market, self.plans, args, self.today)
            if not ok:
                self.audit("order_denied", reason=reason, order=args)
                return _deny(f"Guardrail: {reason}")
            if not self.cfg.live_trading:
                order_id = f"paper-{len(self.events)}-{order['option_id'][:8]}"
                self._record(order, mode="paper", order_id=order_id)
                what = (f"protective {order['type']} at {order['stop_price']}" if order["effect"] == "stop"
                        else f"fill: {order['effect']} {order['quantity']:g} @ {order['price']}")
                return _deny(f"PAPER MODE: order not sent to Robinhood. Simulated {what} recorded for "
                             f"{order['option_id']} (order_id {order_id}). Treat it as accepted and continue.")
            self.audit("order_allowed", order=order)
        return {}

    def _gate_cancel(self, order_id):
        stops = {p["stop"]["order_id"]: oid for oid, p in self.state.positions.items() if p.get("stop")}
        if order_id not in self.cancelable:
            if order_id in stops:
                return _deny("Guardrail: protective stops may only be cancelled to close the position or to raise "
                             "the stop, as listed by review_positions")
            return _deny("Guardrail: only the agent's own stale opening orders or listed stops may be cancelled")
        if not self.cfg.live_trading and order_id in stops:
            self.state.positions[stops[order_id]]["stop"] = None
            self.state.save()
            self.audit("stop_cancelled", mode="paper", order_id=order_id)
            return _deny(f"PAPER MODE: stop {order_id} cancelled. Continue.")
        return {}

    async def post_tool_use(self, input_data, tool_use_id, context):
        name = input_data["tool_name"]
        if not name.startswith(RH):
            return {}
        short = name[len(RH):]
        args = input_data.get("tool_input") or {}
        resp = input_data.get("tool_response")
        payload = decode_tool_response(resp) or {}
        if short in RH_READ_TOOLS:
            self.market.ingest(short, args, payload)
            summary = self.market.summarize(short, payload)
            if summary is None:
                return {}
            out = [{"type": "text", "text": summary}] if isinstance(resp, list) else summary
            return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": out}}
        data = payload.get("data")
        if not data or payload.get("error"):
            self.audit(f"{short}_failed", request=args, response=str(resp)[:500])
            return {}
        if short == "place_option_order":
            _, _, order = risk.check_order(self.cfg, self.state, self.market, self.plans, args, self.today)
            broker_order = data.get("order", data) if isinstance(data, dict) else {}
            if order:
                self._record(order, mode="live", order_id=broker_order.get("id"), broker_response=data)
        elif short == "cancel_option_order":
            cancelled = args.get("order_id")
            for oid, p in list(self.state.positions.items()):
                if p.get("stop") and p["stop"]["order_id"] == cancelled:
                    p["stop"] = None
                elif p.get("open_order_id") == cancelled and not p.get("filled"):
                    # Unfilled entry pulled (e.g. to reprice): nothing was bought.
                    self.state.drop_position(oid, "opening order cancelled before fill")
            self.state.save()
            self.audit("order_cancelled", order_id=args.get("order_id"))
        return {}

    def _record(self, order, mode, order_id=None, broker_response=None):
        """Book an accepted order. Opens/closes are booked at the limit price
        (paper fills, or the intended live fill that reconcile() corrects from
        the broker's processed_premium); stops are attached to the position."""
        oid = order["option_id"]
        if order["effect"] == "stop":
            self.state.positions[oid]["stop"] = {
                "order_id": order_id, "type": order["type"], "stop_price": order["stop_price"],
                "limit_price": order.get("price"), "placed_on": self.today.isoformat(), "mode": mode}
            self.audit("stop_placed", mode=mode, option_id=oid, stop_price=order["stop_price"], order_id=order_id)
        elif order["effect"] == "open":
            inst = self.market.instruments[oid]
            plan = self.plans.pop(oid, {})
            short = None
            if order.get("short_option_id"):
                short = {"option_id": order["short_option_id"], **self.market.instruments[order["short_option_id"]]}
            self.state.open_position(oid, inst, order["quantity"], order["price"], self.signals[inst["symbol"]],
                                     mode, order_id=order_id, short=short,
                                     extra={k: plan[k] for k in ("entry_mid", "vol_regime", "ev_per_dollar") if k in plan})
            self.audit("opened", mode=mode, option_id=oid, contract=inst, quantity=order["quantity"],
                       price=order["price"], order_id=order_id, broker=broker_response)
        elif mode == "live":
            # Not booked until reconcile() sees the broker fill; an unfilled
            # GFD close lapses and the position goes back to needing a stop.
            self.state.positions[oid]["pending_close"] = {
                "order_id": order_id, "price": order["price"], "quantity": order["quantity"],
                "placed_on": self.today.isoformat(), "reason": self.exit_reasons.get(oid, "agent close")}
            self.audit("close_submitted", option_id=oid, order_id=order_id, price=order["price"])
        else:
            pnl = self.state.close_position(oid, order["quantity"], order["price"],
                                            reason=self.exit_reasons.get(oid, "agent close"))
            self.audit("closed", mode=mode, option_id=oid, quantity=order["quantity"], price=order["price"],
                       pnl=round(pnl, 2), broker=broker_response)
        self.state.save()

    # ---- wiring -------------------------------------------------------

    def local_server(self):
        s = self

        @tool("compute_signals", "Score a symbol on 14 factors (trend, MACD, RSI, ADX, volume thrust, order-flow/CVD, "
              "VWAP, ICT market structure, liquidity sweeps, fair value gaps, order blocks, OTE, premium/discount) "
              "from daily + hourly bars already fetched via get_equity_historicals, blend them with learned "
              "weights, and return BUY (long call) / SELL (long put) / HOLD with confluence, learned win "
              "probability and per-factor reasons.", {"symbol": str})
        async def compute_signals(args):
            return _text(s.compute_signals(args["symbol"]))

        @tool("propose_option_trade",
              "Submit a long call/put for guardrail approval before ordering. The contract must have been "
              "fetched via get_option_instruments and quoted via get_option_quotes this run, and earnings "
              "checked via get_earnings_results. Returns approved or the list of violated rules.",
              {"type": "object", "properties": {
                  "option_id": {"type": "string", "description": "the long (bought) contract"},
                  "short_option_id": {"type": "string", "description": "for a debit spread: the further-OTM contract sold"},
                  "quantity": {"type": "integer", "minimum": 1},
                  "limit_price": {"type": "number", "description": "Per-contract limit (net debit for a spread)"},
                  "thesis": {"type": "string", "description": "Why this trade, in two or three sentences"}},
               "required": ["option_id", "quantity", "limit_price", "thesis"]})
        async def propose_option_trade(args):
            return _text(s.propose_option_trade(args["option_id"], args["quantity"], float(args["limit_price"]),
                                                args["thesis"], args.get("short_option_id") or None))

        @tool("rank_contracts",
              "Rank candidate contracts for a BUY/SELL signal by expected value per dollar, after checking the "
              "volatility regime (cheap IV: long call/put; expensive IV: debit spread). Uses the contracts "
              "fetched/quoted this run, so quote a spread of strikes and expirations first. Returns the vol "
              "assessment, top candidates with suggested limit, EV, breakeven, theta, and max contracts by risk budget.",
              {"type": "object", "properties": {
                  "symbol": {"type": "string"},
                  "structure": {"type": "string", "enum": ["auto", "long", "debit_spread", "any"],
                                "description": "auto follows the volatility regime"}},
               "required": ["symbol"]})
        async def rank_contracts(args):
            return _text(s.rank_contracts(args["symbol"], args.get("structure", "auto")))

        @tool("review_positions", "Apply the exit rules (take profit, stop loss, DTE, signal reversal) to every "
              "agent-held position. Quote held contracts and fetch nonzero positions first.", {})
        async def review_positions(args):
            return _text(s.review_positions())

        @tool("learning_report", "What the agent has learned: each factor's measured hit rate and current "
              "weight in trending vs ranging markets, win rate / expected R by conviction level, and the "
              "exit levels tuned from trade outcomes.", {})
        async def learning_report(args):
            return _text({**s.learner.report(), "exit_params": s.state.data.get("exit_params"),
                          "active_exits": {"take_profit_pct": s.cfg.take_profit_pct,
                                           "stop_loss_pct": s.cfg.stop_loss_pct}})

        @tool("warm_start", "Pre-train the factor learner by walk-forward replay of all long bar history fetched "
              "this run (no lookahead). Incremental: only days not replayed before are added.", {})
        async def warm_start(args):
            return _text(s.warm_start())

        @tool("performance_report", "Scoreboard: win rate, expectancy, profit factor, drawdown, return vs SPY "
              "buy-and-hold, results net of AI costs, breakdowns by structure / vol regime / exit reason, "
              "and the go-live checklist.", {})
        async def performance_report(args):
            return _text(s.performance())

        @tool("portfolio_status", "Mode (paper/live), risk limits, ensemble weights, agent positions and P&L.", {})
        async def portfolio_status(args):
            return _text(s.portfolio_status())

        return create_sdk_mcp_server("ponytail", tools=[compute_signals, rank_contracts, propose_option_trade, review_positions,
                                                       portfolio_status, learning_report, warm_start,
                                                       performance_report])

    def options(self):
        servers = {"ponytail": self.local_server()}
        if self.cfg.robinhood_mcp_url:
            rh = {"type": "http", "url": self.cfg.robinhood_mcp_url}
            if self.cfg.robinhood_mcp_token:
                rh["headers"] = {"Authorization": f"Bearer {self.cfg.robinhood_mcp_token}"}
            servers["Robinhood"] = rh
        return ClaudeAgentOptions(
            model=self.cfg.monitor_model if self.monitor else self.cfg.model,
            effort=self.cfg.monitor_effort if self.monitor else self.cfg.effort,
            system_prompt=SYSTEM_PROMPT + (MONITOR_PROMPT if self.monitor else ""),
            # No file/shell tools. ToolSearch only loads deferred MCP tool schemas
            # (and waits for still-connecting servers like the Robinhood connector).
            tools=["ToolSearch"],
            mcp_servers=servers,
            allowed_tools=sorted(ALLOWED_TOOLS),
            hooks={
                "PreToolUse": [HookMatcher(matcher=None, hooks=[self.pre_tool_use])],
                "PostToolUse": [HookMatcher(matcher=None, hooks=[self.post_tool_use])],
            },
            max_turns=self.cfg.max_turns,
            max_budget_usd=self.cfg.monitor_budget_usd if self.monitor else self.cfg.max_budget_usd,
        )

    def kickoff(self):
        if self.monitor:
            return (f"MONITOR run. Date: {self.today.isoformat()}. Mode: {self.mode.upper()}. "
                    f"Account: {self.cfg.account_number}. Do step 1 of the cycle only: protect and exit held "
                    f"positions, then report in three lines or fewer. If nothing is held, say so and stop.")
        ago = lambda days: (self.today - timedelta(days=days)).isoformat() + "T00:00:00Z"  # noqa: E731
        need = self.needs_warm_start()
        warm = (f"\nWarm start needed for: {', '.join(need)}. Before computing signals, fetch long history "
                f"for those symbols: interval='day' with start_time='{ago(1095)}', and interval='hour' with "
                f"start_time='{ago(180)}' (if the hourly request is rejected as too large, retry with "
                f"'{ago(90)}'). Then call warm_start once. That history also covers today's signals for "
                f"those symbols, so don't refetch them.") if need else ""
        return (
            f"Run today's trading cycle. Date: {self.today.isoformat()}. Mode: {self.mode.upper()}.\n"
            f"Account: {self.cfg.account_number}. Universe: {', '.join(self.cfg.symbols)}.\n"
            f"For signals, fetch both timeframes for the whole universe: interval='day' with "
            f"start_time='{ago(420)}', and interval='hour' with start_time='{ago(30)}'." + warm
        )


MONITOR_PROMPT = """

This is a MONITOR run on a small, cheap model: only protect and exit what is already held (step 1). Do not compute signals, rank contracts or open anything; those tools are disabled. Be brief."""

SYSTEM_PROMPT = """You are Ponytail, an autonomous options trading agent operating a small Robinhood account through the Robinhood MCP tools. Each run is one trading cycle. No human reviews your trades before they are placed, so be deliberate, and when in doubt, don't trade. Most days the right answer is no new trade.

How decisions are split:
- The setup comes from code. compute_signals scores 16 factors on the daily and hourly charts:
  - Classic: trend, MACD, RSI, ADX, volume thrust.
  - Order-flow estimates: CVD from where bars close in their range, and VWAP.
  - ICT: market structure (BOS/CHoCH), liquidity sweeps, fair value gaps, order blocks, OTE, premium/discount.
  - Market context: SPY trend, VIX, relative strength vs SPY.
  It blends them with weights learned from this account's results and returns BUY (bullish), SELL (bearish) or HOLD, with confluence, the learned win probability and expected R at that conviction, and a one-line reason per factor. You never trade against or without a BUY/SELL.
- Structure, contract and size come from code too. rank_contracts checks the volatility regime (cheap IV: long call/put; expensive IV: debit vertical spread) and ranks candidates by expected value per dollar after premium, decay and slippage, with the maximum contracts the risk budget allows.
- You are the judgment layer. For each BUY/SELL, read the factor reasons as a chart narrative and ask whether they tell a coherent story. A strong case looks like a liquidity sweep into a discount OTE or order block, with structure shifting your way, flow confirming and the market context supportive. A weak one is mostly lagging trend factors with ICT, flow or context against it. Check earnings, analyst consensus and the nature of the move. Veto when the story is weak or the context is wrong, and say why. A veto costs nothing, and vetoed signals still teach the learner.
- Guardrails are enforced in code. propose_option_trade and the order hooks check structure, EV, sizing, DTE, delta, bid/ask spreads, open interest, earnings and macro-event blackouts, the entry time window, learned odds, portfolio and loss limits, and stop protection, all against data Robinhood returned this run. If a rule rejects a trade, adjust within the rules (another candidate, expiration or quantity) or skip it. Never try to work around a rule.

Cycle:
0. If the Robinhood tools are not directly available, load them with ToolSearch (e.g. "select:mcp__Robinhood__get_option_quotes,..." or a keyword search for "Robinhood"). ToolSearch waits for servers that are still connecting.
1. Protect what you hold. This comes before anything else, every run:
   a. Call portfolio_status, then get_portfolio, get_option_positions (nonzero=true) and get_option_orders (created_at_gte = 7 days ago). These let the code see equity, fills, stop-outs and expired stops.
   b. Quote every contract the agent holds, both legs of spreads (get_option_quotes), then call review_positions.
   c. For each CLOSE row: if cancel_stop_first is set, cancel_option_order that stop first. Then place_option_order with exactly the close_order given.
   d. For each PLACE_STOP row: place_option_order with exactly the stop_order arguments given (plus account_number).
   e. For each RAISE_STOP row: cancel_option_order the old stop (cancel_stop_first), then place the new stop_order.
   f. For each WAIT_FILL row from an earlier run, the entry didn't fill. If the setup is still valid, cancel_option_order the open order and re-propose at a slightly better price; otherwise just cancel it.
   g. Never leave a held single without a resting stop. While any is unprotected, the code rejects every new entry. Spreads are defined-risk and need no stop.
2. Build today's universe and context:
   a. If the kickoff says a warm start is needed, do that first (long-history fetch, then warm_start) and mention the historical hit rates in your report.
   b. Unless portfolio_status shows DISCOVER is off, run preview_scan once with these filters:
      - FILTER_TYPE_MARKET_CAP > 10000000000
      - FILTER_TYPE_AVERAGE_VOLUME > 2000000 (interval 1d, length 30)
      - FILTER_TYPE_RELATIVE_OPTIONS_VOLUME > 1.5 (interval 1d, length 30)
      - FILTER_TYPE_TOTAL_OPTIONS_VOLUME > 20000 (interval 1d)
      The code adds the top unusual-options-activity names to the universe (portfolio_status lists them).
   c. Fetch bars for the whole universe, including SPY: one get_equity_historicals call with interval='day' and one with interval='hour' (start times are in the kickoff). Then get VIX: get_indexes with symbols='VIX', then get_index_historicals with interval='day' over the same daily range.
   d. Run compute_signals for each symbol, and call learning_report once to see which factors are currently earning their weight.
3. Stop here if review_positions or portfolio_status lists any circuit_breakers, if entry_window_open is false, or if upcoming_events shows a macro blackout. Report why and finish.
4. For each BUY/SELL signal you don't veto:
   a. get_earnings_results for the symbol (ETFs return none, which is fine).
   b. get_option_chains, then get_option_instruments for one or two expirations inside the DTE window (prefer about 30-45 DTE), for the signal's type.
   c. get_option_quotes for about 10-15 strikes from slightly in the money to well out of the money, so rank_contracts can price both long options and spreads.
   d. rank_contracts(symbol). Pick the best candidate whose story you believe, usually the top one. Use quantity <= max_contracts and limit <= suggested_limit.
   e. propose_option_trade (include short_option_id for a spread). If approved, review_option_order (skipped in paper mode), then place_option_order with exactly the returned order.
   f. Protect the new position immediately: in live mode confirm the fill with get_option_positions (nonzero=true), then review_positions and place any PLACE_STOP order it returns. If the open hasn't filled yet (WAIT_FILL), say so; the next run handles it.
5. End with a short report covering:
   - mode
   - stops placed, raised or triggered
   - exits
   - signals per symbol, with the top factors and the learned win probability
   - trades placed or vetoed, with reasons
   - circuit breakers, blackouts and guardrail rejections that mattered
   - the performance_report headline (expectancy and net P&L vs SPY) if any trades have closed

Rules:
- Always pass the configured account_number.
- Only long calls, long puts and debit vertical spreads. Never sell premium on its own, never place market orders, never exercise.
- Loss control beats opportunity. When unsure whether to hold or exit a losing position, exit. Never widen or remove a stop; the code only allows stops to stay put or move up.
- If review_option_order returns alerts that indicate a real problem (insufficient buying power, a halted contract, missing options level), skip the trade.
- In PAPER mode, place_option_order is intercepted and returns a simulated fill. Treat that as a successful order.
- If a Robinhood tool errors, retry once at most, then move on. Don't loop.
"""


async def run_cycle(cfg, today=None, monitor=False):
    session = TradingSession(cfg, today, monitor=monitor)
    log.info("starting %s %s cycle for %s", session.mode, "monitor" if monitor else "full", cfg.symbols)
    result = None
    try:
        async for msg in query(prompt=session.kickoff(), options=session.options()):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        log.info("agent: %s", block.text.strip())
                    elif isinstance(block, ToolUseBlock):
                        log.info("tool: %s %s", block.name, json.dumps(block.input)[:300])
            elif isinstance(msg, ResultMessage):
                result = msg
    finally:
        cost = getattr(result, "total_cost_usd", None) or 0.0
        day = datetime.now(timezone.utc).date().isoformat()
        by_day = session.state.data.setdefault("ai_cost_by_day", {})
        by_day[day] = round(by_day.get(day, 0.0) + cost, 4)
        session.state.data.setdefault("runs", []).append({
            "at": datetime.now(timezone.utc).isoformat(), "mode": session.mode,
            "kind": "monitor" if monitor else "full", "signals": session.signals, "events": session.events,
            "cost_usd": cost, "summary": getattr(result, "result", None),
        })
        if result is None or result.is_error:
            session.audit("run_failed", error=getattr(result, "result", None) or "no result")
        session.state.data["runs"] = session.state.data["runs"][-50:]
        session.state.save()
    if result is not None:
        log.info("done: %s turns, $%.2f, error=%s", result.num_turns, result.total_cost_usd or 0, result.is_error)
    return session, result
