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

from . import risk
from .market import MarketCache, decode_tool_response
from .signals import raw_votes, weighted_decision
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
LOCAL_TOOLS = {"compute_signals", "propose_option_trade", "review_positions", "portfolio_status"}

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
        self.market = MarketCache()
        self.signals = {}  # symbol -> {votes, score, decision, rsi}
        self.plans = {}    # option_id -> approved open plan
        self.events = []   # audit trail of guardrail decisions this run

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
        closes = self.market.closes.get(symbol)
        if not closes:
            return {"error": f"no daily bars for {symbol}; call get_equity_historicals with interval='day' first"}
        try:
            votes, last_rsi = raw_votes(closes)
        except ValueError as e:
            return {"error": f"{e}; request a longer start_time"}
        decision, score = weighted_decision(votes, self.state.weights)
        self.signals[symbol] = {"votes": votes, "score": round(score, 3), "decision": decision, "rsi": round(last_rsi, 1)}
        return {"symbol": symbol, "last_close": closes[-1], "bars": len(closes), "weights": self.state.weights,
                **self.signals[symbol], "implies": risk.DIRECTION_TO_TYPE.get(decision, "no trade")}

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
        rows = risk.review_exits(self.cfg, self.state, self.market, self.signals, self.today)
        for row in rows:
            if row["action"] == "DROP":
                self.state.drop_position(row["option_id"], row["reason"])
                self.audit("position_dropped", **row)
        self.state.save()
        return {"mode": self.mode, "positions": rows}

    def portfolio_status(self):
        day = self.today.isoformat()
        log_ = self.state.trade_log
        return {
            "mode": self.mode, "account_number": self.cfg.account_number, "today": day,
            "universe": self.cfg.symbols, "weights": self.state.weights,
            "open_positions": self.state.positions, "open_premium": round(self.state.open_premium(), 2),
            "realized_pnl_today": round(self.state.realized_pnl_on(day), 2),
            "realized_pnl_all_time": round(sum(t["pnl"] for t in log_), 2), "closed_trades": len(log_),
            "limits": {k: getattr(self.cfg, k) for k in (
                "max_premium_per_trade", "max_total_premium", "max_open_positions", "max_daily_loss",
                "min_dte", "max_dte", "min_abs_delta", "max_abs_delta", "max_spread_pct",
                "min_open_interest", "avoid_earnings", "take_profit_pct", "stop_loss_pct", "exit_dte")},
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

        if name == RH + "place_option_order":
            ok, reason, order = risk.check_order(self.cfg, self.state, self.plans, args)
            if not ok:
                self.audit("order_denied", reason=reason, order=args)
                return _deny(f"Guardrail: {reason}")
            if not self.cfg.live_trading:
                self._record_fill(order, mode="paper")
                return _deny(f"PAPER MODE: order not sent to Robinhood. Simulated fill recorded: "
                             f"{order['effect']} {order['quantity']:g} x {order['option_id']} @ {order['price']}. "
                             f"Treat it as filled and continue.")
            self.audit("order_allowed", order=order)
        return {}

    async def post_tool_use(self, input_data, tool_use_id, context):
        name = input_data["tool_name"]
        if not name.startswith(RH):
            return {}
        short = name[len(RH):]
        resp = input_data.get("tool_response")
        if short in RH_READ_TOOLS:
            self.market.ingest(short, input_data.get("tool_input"), resp)
        elif short == "place_option_order":
            payload = decode_tool_response(resp) or {}
            if payload.get("data") and not payload.get("error"):
                _, _, order = risk.check_order(self.cfg, self.state, self.plans, input_data["tool_input"])
                if order:
                    self._record_fill(order, mode="live", broker_response=payload["data"])
            else:
                self.audit("order_failed", response=str(resp)[:500])
        return {}

    def _record_fill(self, order, mode, broker_response=None):
        """Book the order at its limit price. For live opens this is the
        intended fill; review_positions drops it next run if it never filled."""
        oid = order["option_id"]
        if order["effect"] == "open":
            inst = self.market.instruments[oid]
            self.state.open_position(oid, inst, order["quantity"], order["price"], self.signals[inst["symbol"]], mode)
            self.plans.pop(oid, None)
            self.audit("opened", mode=mode, option_id=oid, contract=inst, quantity=order["quantity"],
                       price=order["price"], broker=broker_response)
        else:
            pnl = self.state.close_position(oid, order["quantity"], order["price"], reason=f"{mode} close")
            self.audit("closed", mode=mode, option_id=oid, quantity=order["quantity"], price=order["price"],
                       pnl=round(pnl, 2), weights=self.state.weights, broker=broker_response)
        self.state.save()

    # ---- wiring -------------------------------------------------------

    def local_server(self):
        s = self

        @tool("compute_signals", "Run the weighted SMA/RSI/MACD ensemble on the daily bars already fetched "
              "via get_equity_historicals. Returns BUY (long call), SELL (long put) or HOLD.", {"symbol": str})
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

        @tool("portfolio_status", "Mode (paper/live), risk limits, ensemble weights, agent positions and P&L.", {})
        async def portfolio_status(args):
            return _text(s.portfolio_status())

        return create_sdk_mcp_server("ponytail", tools=[compute_signals, propose_option_trade, review_positions, portfolio_status])

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
        start = (self.today - timedelta(days=150)).isoformat() + "T00:00:00Z"
        return (
            f"Run today's trading cycle. Date: {self.today.isoformat()}. Mode: {self.mode.upper()}.\n"
            f"Account: {self.cfg.account_number}. Universe: {', '.join(self.cfg.symbols)}.\n"
            f"For signals, request daily bars with interval='day' and start_time='{start}'."
        )


SYSTEM_PROMPT = """You are Ponytail, an autonomous options trading agent operating a small Robinhood account through the Robinhood MCP tools. Each run is one trading cycle. No human reviews your trades before they are placed, so be deliberate, and when in doubt, don't trade.

How decisions are split:
- Direction comes from code. compute_signals runs an adaptive SMA/RSI/MACD ensemble: BUY means a long call, SELL means a long put, HOLD means no new trade. You never trade against or without a signal.
- You are the judgment layer. For each BUY/SELL signal, decide whether the context supports it: upcoming earnings, analyst consensus, fundamentals, the size and nature of the recent move. Veto a signal whenever the context looks wrong, and say why. A veto costs nothing; a bad trade costs money.
- Guardrails are enforced in code. propose_option_trade and the order hooks check sizing, DTE, delta, spread, open interest, earnings, and portfolio and daily-loss limits against data Robinhood returned this run. If a rule rejects a trade, adjust within the rules (another strike, expiration or quantity) or skip it. Never try to work around a rule.

Cycle:
0. If the Robinhood tools are not directly available, load them with ToolSearch (e.g. "select:mcp__Robinhood__get_option_quotes,..." or a keyword search for "Robinhood"). ToolSearch waits for servers that are still connecting.
1. Call portfolio_status. Call get_option_positions with nonzero=true. Quote every contract the agent holds (get_option_quotes), then call review_positions.
2. For each CLOSE: review_option_order, then place_option_order (sell, position_effect close, type limit) at the suggested limit or between bid and mark. Exits take priority over new entries. For DROP rows, note them; the state is already updated.
3. Fetch daily bars for the whole universe in one get_equity_historicals call and run compute_signals for each symbol.
4. For each BUY/SELL signal you don't veto:
   a. get_earnings_results for the symbol (ETFs return none, which is fine).
   b. get_option_chains, then pick one expiration inside the DTE window, preferring the nearest to roughly 30-45 DTE.
   c. get_option_instruments for that expiration and type, then get_option_quotes for 5-10 strikes around the money. Aim for |delta| near 0.40-0.55.
   d. Size to the per-trade premium cap, usually 1 contract. Set the limit at or slightly above the mid, never above the ask.
   e. propose_option_trade. If approved, review_option_order and then place_option_order with exactly the approved option_id, quantity and price, type limit, time_in_force gfd.
5. End with a short report covering: mode, exits taken, signals per symbol, trades placed or vetoed with reasons, and the guardrail rejections that mattered.

Rules:
- Always pass the configured account_number.
- Only single-leg long calls and long puts. Never sell to open, never place market orders, never exercise.
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
