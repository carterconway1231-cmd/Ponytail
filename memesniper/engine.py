"""The sniper loop: launch -> observe -> safety screen -> Claude -> risk clamp -> execute,
plus a monitor that marks open positions to live sell quotes and runs the exit rules."""
import asyncio
import logging
import time

from . import exits
from .feeds import fetch_metadata
from .models import Position
from .safety import screen

log = logging.getLogger("memesniper")

WRITE_OFF_AFTER_FAILURES = 24   # monitor ticks with no sell route before booking a total loss


class Sniper:
    def __init__(self, settings, *, state, risk, rpc, jupiter, rugcheck, dexscreener, agent,
                 executor, hub=None, http=None):
        self.s = settings
        self.state, self.risk, self.rpc, self.jup = state, risk, rpc, jupiter
        self.rugcheck, self.dex, self.agent, self.exec = rugcheck, dexscreener, agent, executor
        self.hub, self.http = hub, http
        self._obs_slots = asyncio.Semaphore(settings.max_concurrent_observations)
        self._sell_failures = {}
        self._tasks = set()

    # --- entries -------------------------------------------------------------
    def portfolio_summary(self):
        open_pos = self.state.open_positions()
        recent = []
        for r in self.state.closed_positions(10):
            pnl = (r["realized_sol"] - r["entry_sol"]) / r["entry_sol"] * 100 if r["entry_sol"] else 0
            recent.append({"symbol": r["symbol"], "pnl_pct": round(pnl, 1),
                           "held_min": round((r["closed_at"] - r["opened_at"]) / 60, 1),
                           "exit": r["exit_reason"]})
        return {"open_positions": len(open_pos),
                "exposure_sol": round(sum(p.tokens * p.entry_price for p in open_pos), 4),
                "realized_pnl_today_sol": round(self.state.realized_pnl_today(), 4),
                "recent_closed_trades": recent}

    async def handle(self, launch):
        if not self.state.mark_seen(launch.mint):
            return None
        if self.risk.halted():
            return None         # don't burn API calls on candidates we can't buy

        observation = None
        if launch.source == "pumpportal" and self.hub is not None:
            if self._obs_slots.locked():
                return None     # saturated: newer launches will come
            async with self._obs_slots:
                meta_task = asyncio.create_task(fetch_metadata(self.http, launch.uri)) if self.http else None
                observation = await self.hub.observe(launch, self.s.observe_seconds)
                if meta_task:
                    meta = await meta_task
                    launch.description = meta.pop("description", launch.description)
                    launch.socials.update(meta)

        report = await screen(launch, self.s, self.rpc, self.jup, self.rugcheck, self.dex, observation)
        pair = report.metrics.get("pair") or {}
        launch.name = launch.name or pair.get("name", "")
        launch.symbol = launch.symbol or pair.get("symbol", "")
        for k, v in (pair.get("socials") or {}).items():
            launch.socials.setdefault(k, v)

        if not report.passed:
            log.debug("reject %s %s: %s", launch.symbol, launch.mint, "; ".join(report.hard_fails))
            self.state.record_decision(launch, report)
            return None

        decision = await self.agent.decide(launch, report, self.portfolio_summary(),
                                           self.state.claude_spend_today())
        log.info("claude %s %s (%.2f) %.1fs: %s", decision.action, launch.symbol or launch.mint[:6],
                 decision.confidence, decision.latency_s, decision.reasoning)
        wallet = await self.exec.wallet_sol()
        approved, why = self.risk.approve(decision, launch, wallet)
        if not approved:
            if decision.action == "BUY":
                decision.reasoning = f"[blocked: {why}] {decision.reasoning}"
            self.state.record_decision(launch, report, decision)
            return None

        fill = await self.exec.buy(launch.mint, approved.size_sol)
        if not fill or fill.tokens <= 0:
            approved.reasoning = f"[buy failed] {approved.reasoning}"
            self.state.record_decision(launch, report, approved)
            return None
        pos = Position(
            mint=launch.mint, symbol=launch.symbol or launch.mint[:6], entry_sol=fill.sol,
            tokens=fill.tokens, entry_price=fill.sol / fill.tokens, opened_at=time.time(),
            take_profit_pct=approved.take_profit_pct, stop_loss_pct=approved.stop_loss_pct,
            trailing_stop_pct=self.s.trailing_stop_pct, max_hold_minutes=approved.max_hold_minutes)
        self.state.open_position(pos, launch.creator, self.exec.mode, fill.sig)
        self.state.record_decision(launch, report, approved, executed=True)
        log.info("BOUGHT %s %.4f SOL (%s) tp+%.0f%% sl-%.0f%% hold %.0fm", pos.symbol, fill.sol,
                 self.exec.mode, pos.take_profit_pct, pos.stop_loss_pct, pos.max_hold_minutes)
        return pos

    # --- exits ---------------------------------------------------------------
    async def check_position(self, p, now=None):
        now = now or time.time()
        value = await self.jup.value_sol(p.mint, p.tokens, self.s.slippage_bps)
        price = value / p.tokens if value is not None and p.tokens > 0 else None
        action = exits.evaluate(p, price, now, self.s.tp_sell_fraction)
        if action.fraction <= 0:
            self._sell_failures.pop(p.id, None)
            self.state.save_position(p)
            return None

        sell_tokens = p.tokens if action.fraction >= 1 else p.tokens * action.fraction
        fill = await self.exec.sell(p.mint, sell_tokens)
        if not fill:
            n = self._sell_failures[p.id] = self._sell_failures.get(p.id, 0) + 1
            if n >= WRITE_OFF_AFTER_FAILURES:
                log.warning("writing off %s after %d failed sells", p.symbol, n)
                self.state.close_position(p, f"written off: {action.reason}")
            return None
        self._sell_failures.pop(p.id, None)
        p.tokens = max(p.tokens - fill.tokens, 0.0)
        p.realized_sol += fill.sol
        if fill.sig and not fill.sig.startswith("paper"):
            self.state.add_exit_sig(p.id, fill.sig)
        if action.fraction < 1:
            p.tp_taken = True
            self.state.save_position(p)
            log.info("PARTIAL %s %s: +%.4f SOL", p.symbol, action.reason, fill.sol)
        else:
            self.state.close_position(p, action.reason)
            log.info("CLOSED %s %s: pnl %+.4f SOL", p.symbol, action.reason, p.realized_sol - p.entry_sol)
        return action

    async def monitor(self):
        while True:
            for p in self.state.open_positions():
                try:
                    await self.check_position(p)
                except Exception:
                    log.exception("monitor error on %s", p.symbol)
            await asyncio.sleep(self.s.monitor_interval_s)

    # --- run -----------------------------------------------------------------
    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._done)

    def _done(self, t):
        self._tasks.discard(t)
        if not t.cancelled() and t.exception():
            log.error("candidate task failed", exc_info=t.exception())

    async def consume(self, feed):
        async for launch in feed.stream():
            self._spawn(self.handle(launch))

    async def run(self, feeds):
        jobs = [self.monitor()] + [self.consume(f) for f in feeds]
        if self.hub is not None:
            jobs.append(self.hub.run())
        await asyncio.gather(*jobs)
