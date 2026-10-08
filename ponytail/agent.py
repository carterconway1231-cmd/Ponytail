"""Hybrid options agent: a deterministic signal + risk engine wrapped around a
Claude agent loop that talks to the official Robinhood MCP server.

Division of labor:
  * Code (signals.py) decides direction: BUY -> long call, SELL -> long put.
  * Claude does the judgment work: vetoes on context (earnings, news-driven
    moves, fundamentals, analyst consensus), picks the contract, sets the
    limit price, and manages exits.
  * Code (risk.py, enforced in PreToolUse hooks) has the final say on every
    order. A denied tool call never reaches Robinhood.
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, HookMatcher, ResultMessage, TextBlock, ToolUseBlock,
    create_sdk_mcp_server, query, tool,
)

from . import protect, risk
from .market import MarketCache, decode_tool_response
from .factors import compute_factors
from .learner import Learner
from .state import State, now_iso

log = logging.getLogger("ponytail")

RH = "mcp__Robinhood__"
LOCAL = "mcp__ponytail__"

RH_READ_TOOLS = {
    "get_accounts", "get_portfolio", "get_equity_quotes", "get_equity_historicals",
    "get_equity_fundamentals", "get_equity_analyst_ratings", "get_equity_technical_indicators",
    "get_earnings_results", "get_option_chains", "get_option_instruments", "get_option_quotes",
    "get_option_positions", "get_option_orders", "search",
}
RH_WRITE_TOOLS = {"review_option_order", "place_option_order", "cancel_option_order"}
LOCAL_TOOLS = {"compute_signals", "propose_option_trade", "review_positions", "portfolio_status", "learning_report"}

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

    def __init__(self, cfg, today=None):
        self.cfg = cfg
        self.today = today or date.today()
        self.state = State.load(cfg.state_path)
        self.learner = self.state.learner = Learner(self.state.data["learner"], cfg)
        self.market = MarketCache()
        self.signals = {}  # symbol -> {votes, score, decision, rsi}
        self.plans = {}    # option_id -> approved open plan
        self.events = []   # audit trail of guardrail decisions this run
        self.cancelable = set()  # order ids review_positions cleared for cancellation

    @property
    def mode(self):
        return "live" if self.cfg.live_trading else "paper"

    def audit(self, kind, **fields):
        event = {"at": now_iso(), "kind": kind, **fields}
        self.events.append(event)
        log.info("%s %s", kind, json.dumps(fields, default=str))

    # ---- deterministic tools exposed to Claude -------------------------

    def compute_signals(self, symbol):
        symbol = symbol.upper()
        bars = self.market.bars.get(symbol, {})
        daily = bars.get("day")
        if not daily:
            return {"error": f"no daily bars for {symbol}; call get_equity_historicals with interval='day' first"}
        try:
            analysis = compute_factors(daily, bars.get("hour"))
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

    def propose_option_trade(self, option_id, quantity, limit_price, thesis):
        problems = risk.check_open(self.cfg, self.state, self.market, self.signals, option_id,
                                   quantity, limit_price, self.today)
        inst = self.market.instruments.get(option_id, {})
        if problems:
            self.audit("plan_rejected", option_id=option_id, symbol=inst.get("symbol"), problems=problems)
            return {"approved": False, "problems": problems}
        self.plans[option_id] = {"quantity": float(quantity), "limit_price": float(limit_price), "thesis": thesis}
        cost = limit_price * quantity * 100
        self.audit("plan_approved", option_id=option_id, contract=inst, quantity=quantity,
                   limit_price=limit_price, cost=cost, thesis=thesis)
        return {"approved": True, "contract": inst, "quantity": quantity, "limit_price": limit_price,
                "premium_at_risk": round(cost, 2), "next": "review_option_order, then place_option_order with these exact values"}

    def review_positions(self):
        events = protect.reconcile(self.cfg, self.state, self.market, self.today)
        events += protect.simulate_paper_stops(self.state, self.market, self.today)
        for e in events:
            self.audit(e["kind"], **{k: v for k, v in e.items() if k != "kind"})
        rows = risk.review_exits(self.cfg, self.state, self.market, self.signals, self.today)
        # Cancelling a live stop is only allowed to exit or to ratchet it higher.
        self.cancelable = {r["cancel_stop_first"] for r in rows if r.get("cancel_stop_first")}
        self.cancelable |= {p["open_order_id"] for p in self.state.positions.values()
                            if p["mode"] == "live" and p.get("open_order_id") and not p.get("filled")}
        self.state.save()
        return {"mode": self.mode, "broker_events": events, "positions": rows,
                "circuit_breakers": risk.circuit_breakers(self.cfg, self.state, self.market, self.today)}

    def portfolio_status(self):
        day = self.today.isoformat()
        log_ = self.state.trade_log
        return {
            "mode": self.mode, "account_number": self.cfg.account_number, "today": day,
            "universe": self.cfg.symbols,
            "learning": {k: self.learner.d[k] for k in ("trades_learned", "shadow_learned")},
            "open_positions": self.state.positions, "open_premium": round(self.state.open_premium(), 2),
            "realized_pnl_today": round(self.state.realized_pnl_on(day), 2),
            "realized_pnl_all_time": round(sum(t["pnl"] for t in log_), 2), "closed_trades": len(log_),
            "circuit_breakers": risk.circuit_breakers(self.cfg, self.state, self.market, self.today),
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
        if short in RH_READ_TOOLS:
            self.market.ingest(short, args, resp)
            return {}
        payload = decode_tool_response(resp) or {}
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
            for p in self.state.positions.values():
                if p.get("stop") and p["stop"]["order_id"] == args.get("order_id"):
                    p["stop"] = None
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
            self.state.open_position(oid, inst, order["quantity"], order["price"], self.signals[inst["symbol"]],
                                     mode, order_id=order_id)
            self.plans.pop(oid, None)
            self.audit("opened", mode=mode, option_id=oid, contract=inst, quantity=order["quantity"],
                       price=order["price"], order_id=order_id, broker=broker_response)
        elif mode == "live":
            # Not booked until reconcile() sees the broker fill; an unfilled
            # GFD close lapses and the position goes back to needing a stop.
            self.state.positions[oid]["pending_close"] = {
                "order_id": order_id, "price": order["price"], "quantity": order["quantity"],
                "placed_on": self.today.isoformat()}
            self.audit("close_submitted", option_id=oid, order_id=order_id, price=order["price"])
        else:
            pnl = self.state.close_position(oid, order["quantity"], order["price"], reason=f"{mode} close")
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
                  "option_id": {"type": "string"},
                  "quantity": {"type": "integer", "minimum": 1},
                  "limit_price": {"type": "number", "description": "Per-contract limit, between bid and ask"},
                  "thesis": {"type": "string", "description": "Why this trade, in two or three sentences"}},
               "required": ["option_id", "quantity", "limit_price", "thesis"]})
        async def propose_option_trade(args):
            return _text(s.propose_option_trade(args["option_id"], args["quantity"], float(args["limit_price"]), args["thesis"]))

        @tool("review_positions", "Apply the exit rules (take profit, stop loss, DTE, signal reversal) to every "
              "agent-held position. Quote held contracts and fetch nonzero positions first.", {})
        async def review_positions(args):
            return _text(s.review_positions())

        @tool("learning_report", "What the agent has learned: each factor's measured hit rate and current "
              "weight in trending vs ranging markets, and win rate / expected R by conviction level.", {})
        async def learning_report(args):
            return _text(s.learner.report())

        @tool("portfolio_status", "Mode (paper/live), risk limits, ensemble weights, agent positions and P&L.", {})
        async def portfolio_status(args):
            return _text(s.portfolio_status())

        return create_sdk_mcp_server("ponytail", tools=[compute_signals, propose_option_trade, review_positions, portfolio_status, learning_report])

    def options(self):
        servers = {"ponytail": self.local_server()}
        if self.cfg.robinhood_mcp_url:
            rh = {"type": "http", "url": self.cfg.robinhood_mcp_url}
            if self.cfg.robinhood_mcp_token:
                rh["headers"] = {"Authorization": f"Bearer {self.cfg.robinhood_mcp_token}"}
            servers["Robinhood"] = rh
        return ClaudeAgentOptions(
            model=self.cfg.model,
            effort=self.cfg.effort,
            system_prompt=SYSTEM_PROMPT,
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
            max_budget_usd=self.cfg.max_budget_usd,
        )

    def kickoff(self):
        day_start = (self.today - timedelta(days=420)).isoformat() + "T00:00:00Z"
        hour_start = (self.today - timedelta(days=30)).isoformat() + "T00:00:00Z"
        return (
            f"Run today's trading cycle. Date: {self.today.isoformat()}. Mode: {self.mode.upper()}.\n"
            f"Account: {self.cfg.account_number}. Universe: {', '.join(self.cfg.symbols)}.\n"
            f"For signals, fetch both timeframes for the whole universe: interval='day' with "
            f"start_time='{day_start}', and interval='hour' with start_time='{hour_start}'."
        )


SYSTEM_PROMPT = """You are Ponytail, an autonomous options trading agent operating a small Robinhood account through the Robinhood MCP tools. Each run is one trading cycle. No human reviews your trades before they are placed, so be deliberate, and when in doubt, don't trade.

How decisions are split:
- The setup comes from code. compute_signals scores 14 factors on the daily and hourly charts:
  - Classic: trend, MACD, RSI, ADX, volume thrust.
  - Order-flow estimates: CVD from where bars close in their range, and VWAP.
  - ICT: market structure (BOS/CHoCH), liquidity sweeps, fair value gaps, order blocks, OTE, premium/discount.
  It blends them with weights learned from this account's own results, and returns BUY (long call), SELL (long put) or HOLD. With each it gives the confluence (which factors agree and which oppose), the learned win probability and expected R for setups at that conviction, and a one-line reason per factor. You never trade against or without a BUY/SELL.
- You are the judgment layer. For each BUY/SELL, read the factor reasons as a chart narrative and ask whether they tell a coherent story. A strong case looks like a liquidity sweep into a discount OTE or order block with structure shifting your way and flow confirming. A weak one is mostly lagging trend factors with ICT and flow against it. Check the context too: upcoming earnings, analyst consensus, fundamentals, the nature of the move. Veto when the story is weak or the context is wrong, and say why. A veto costs nothing, and the system still learns from vetoed signals by grading them later.
- Use the learning. learning_report shows which factors have actually been right in trending vs ranging markets. Lean on factors with proven hit rates and be skeptical of setups that rest on ones that have been wrong.
- Guardrails are enforced in code. propose_option_trade and the order hooks check sizing, DTE, delta, spread, open interest, earnings, learned win rate and expected value, portfolio and loss limits, and stop protection, all against data Robinhood returned this run. If a rule rejects a trade, adjust within the rules (another strike, expiration or quantity) or skip it. Never try to work around a rule.

Cycle:
0. If the Robinhood tools are not directly available, load them with ToolSearch (e.g. "select:mcp__Robinhood__get_option_quotes,..." or a keyword search for "Robinhood"). ToolSearch waits for servers that are still connecting.
1. Protect what you hold. This comes before anything else, every run:
   a. Call portfolio_status, then get_option_positions (nonzero=true) and get_option_orders (created_at_gte = 7 days ago). These let the code see fills, stop-outs and expired stops.
   b. Quote every contract the agent holds (get_option_quotes), then call review_positions.
   c. For each CLOSE row: if cancel_stop_first is set, cancel_option_order that stop first. Then place_option_order (sell, position_effect close, type limit) at suggested_limit, or between bid and mark.
   d. For each PLACE_STOP row: place_option_order with exactly the stop_order arguments given (plus account_number).
   e. For each RAISE_STOP row: cancel_option_order the old stop (cancel_stop_first), then place the new stop_order.
   f. Never leave a held position without a resting stop. While any position is unprotected, the code rejects every new entry.
2. Fetch bars for the whole universe: one get_equity_historicals call with interval='day' and one with interval='hour' (start times are in the kickoff message). Then run compute_signals for each symbol, and call learning_report once to see which factors are currently earning their weight.
3. If review_positions or portfolio_status lists any circuit_breakers, open nothing new this run. Report why and finish.
4. For each BUY/SELL signal you don't veto:
   a. get_earnings_results for the symbol (ETFs return none, which is fine).
   b. get_option_chains, then pick one expiration inside the DTE window, preferring the nearest to roughly 30-45 DTE.
   c. get_option_instruments for that expiration and type, then get_option_quotes for 5-10 strikes around the money. Aim for |delta| near 0.40-0.55.
   d. Size to the per-trade premium cap, usually 1 contract. Set the limit at or slightly above the mid, never above the ask.
   e. propose_option_trade. If approved, review_option_order and then place_option_order with exactly the approved option_id, quantity and price, type limit, time_in_force gfd.
   f. Immediately protect the new position. Call get_option_positions (nonzero=true) to confirm the fill (live mode), then review_positions, then place the PLACE_STOP order it returns. If the open hasn't filled yet (WAIT_FILL), say so; the next run will place the stop.
5. End with a short report covering:
   - mode
   - stops placed, raised or triggered
   - exits
   - signals per symbol, with the top factors and the learned win probability
   - trades placed or vetoed, with reasons
   - circuit breakers and guardrail rejections that mattered

Rules:
- Always pass the configured account_number.
- Only single-leg long calls and long puts. Never sell to open, never place market orders, never exercise.
- Loss control beats opportunity. When unsure whether to hold or exit a losing position, exit. Never widen or remove a stop; the code only allows stops to stay put or move up.
- If review_option_order returns alerts that indicate a real problem (insufficient buying power, a halted contract), skip the trade.
- In PAPER mode, place_option_order is intercepted and returns a simulated fill. Treat that as a successful order.
- If a Robinhood tool errors, retry once at most, then move on. Don't loop.
"""


async def run_cycle(cfg, today=None):
    session = TradingSession(cfg, today)
    log.info("starting %s cycle for %s", session.mode, cfg.symbols)
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
        session.state.data.setdefault("runs", []).append({
            "at": datetime.now(timezone.utc).isoformat(), "mode": session.mode,
            "signals": session.signals, "events": session.events,
            "cost_usd": getattr(result, "total_cost_usd", None),
            "summary": getattr(result, "result", None),
        })
        session.state.data["runs"] = session.state.data["runs"][-50:]
        session.state.save()
    if result is not None:
        log.info("done: %s turns, $%.2f, error=%s", result.num_turns, result.total_cost_usd or 0, result.is_error)
    return session, result
