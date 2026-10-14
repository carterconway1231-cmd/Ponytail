"""The trading loop, across chains and strategies.

launch:   new token -> observe (pump.fun) -> safety screen -> Claude -> risk clamp -> execute
watchlist: every screened token is re-scored each minute; a breakout or dip signal re-runs the
          safety screen, then the same Claude -> risk -> execute path
exits:    every open position is marked to a live sell quote; staged take-profits, momentum fade,
          trailing stop, stop loss and time stop decide when to sell
"""
import asyncio
import logging
import time

from . import exits
from .feeds import fetch_metadata
from .models import Decision, Position
from .notify import dexscreener_link
from .safety import screen

log = logging.getLogger("memesniper")

WRITE_OFF_AFTER_FAILURES = 24   # monitor ticks with no sell route before booking a total loss


class Sniper:
    def __init__(self, settings, *, state, risk, chains, dexscreener, agent, prices, hub=None,
                 http=None, notifier=None, creators=None, outcomes=None, watchlist=None):
        self.s = settings
        self.state, self.risk, self.chains = state, risk, chains     # chains: name -> ChainCtx
        self.dex, self.agent, self.prices = dexscreener, agent, prices
        self.hub, self.http, self.notifier = hub, http, notifier
        self.creators, self.outcomes, self.watchlist = creators, outcomes, watchlist
        self._obs_slots = asyncio.Semaphore(settings.max_concurrent_observations)
        self._sell_failures = {}
        self._tasks = set()

    # --- helpers -------------------------------------------------------------
    def _paper(self, chain):
        return self.chains[chain].executor.mode == "paper"

    def _label(self, chain):
        return "PAPER " if self._paper(chain) else ""

    def portfolio_summary(self):
        open_pos = self.state.open_positions()
        recent = []
        for r in self.state.closed_positions(10):
            pnl = (r["realized_usd"] - r["entry_usd"]) / r["entry_usd"] * 100 if r["entry_usd"] else 0
            recent.append({"symbol": r["symbol"], "chain": r["chain"], "strategy": r["strategy"],
                           "pnl_pct": round(pnl, 1),
                           "held_min": round((r["closed_at"] - r["opened_at"]) / 60, 1),
                           "exit": r["exit_reason"]})
        return {"open_positions": [{"symbol": p.symbol, "chain": p.chain, "strategy": p.strategy}
                                   for p in open_pos],
                "exposure_usd": round(self.risk.exposure_usd(), 2),
                "realized_pnl_today_usd": round(self.state.realized_pnl_today(), 2),
                "recent_closed_trades": recent}

    async def _screen(self, launch, observation=None):
        price = await self.prices.usd(launch.chain)
        trade_usd = self.s.max_usd_per_trade
        return await screen(launch, self.s, self.chains[launch.chain], self.dex, observation,
                            creators=self.creators, trade_native=trade_usd / price if price else None,
                            trade_usd=trade_usd)

    async def _halted(self):
        halt = self.risk.halted()
        if self.notifier:
            await self.notifier.halted(halt)
        return halt

    # --- entries -------------------------------------------------------------
    async def handle(self, launch):
        """A newly discovered token (from a launch feed)."""
        if launch.chain not in self.chains or not self.state.mark_seen(launch.key):
            return None
        self.state.record_launch(launch)
        if await self._halted():
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

        report = await self._screen(launch, observation)
        pair = report.metrics.get("pair") or {}
        launch.name = launch.name or pair.get("name", "")
        launch.symbol = launch.symbol or pair.get("symbol", "")
        for k, v in (pair.get("socials") or {}).items():
            launch.socials.setdefault(k, v)

        if not report.passed:
            log.debug("reject %s %s: %s", launch.symbol, launch.mint, "; ".join(report.hard_fails))
            self.state.record_decision(launch, report)
            self._track(launch, report, "REJECT", "launch")
            return None
        if self.watchlist is not None:
            self.watchlist.add(launch)
        if "launch" not in self.s.strategies:
            return None
        return await self._decide_and_buy(launch, report, "launch")

    async def handle_signal(self, launch, strategy, signal):
        """A watchlist token fired a breakout or dip signal."""
        if launch.chain not in self.chains or await self._halted():
            return None
        report = await self._screen(launch)       # conditions change: re-check before buying
        if not report.passed:
            self.state.record_decision(launch, report, strategy=strategy)
            self._track(launch, report, "REJECT", strategy)
            return None
        log.info("%s signal on %s (%s)", strategy, launch.symbol or launch.mint[:8], launch.chain)
        return await self._decide_and_buy(launch, report, strategy, signal)

    async def _decide_and_buy(self, launch, report, strategy, signal=None):
        s = self.s
        if strategy in s.claude_strategies:
            decision = await self.agent.decide(launch, report, self.portfolio_summary(),
                                               self.state.claude_spend_today(), strategy, signal)
        else:
            decision = Decision("BUY", confidence=1.0, size_usd=s.max_usd_per_trade,
                                take_profit_pct=s.take_profit_pct, stop_loss_pct=s.stop_loss_pct,
                                max_hold_minutes=s.max_hold_minutes,
                                reasoning=f"{strategy} signal (code rules only)")
        name = launch.symbol or launch.mint[:8]
        log.info("%s %s %s/%s (%.2f) %.1fs: %s", "claude" if strategy in s.claude_strategies else "rules",
                 decision.action, name, launch.chain, decision.confidence, decision.latency_s,
                 decision.reasoning)

        ctx = self.chains[launch.chain]
        native_usd = await self.prices.usd(launch.chain)
        wallet = await ctx.executor.wallet_native()
        approved, why = self.risk.approve(decision, launch, native_usd, wallet)
        if not approved:
            if decision.action == "BUY":
                decision.reasoning = f"[blocked: {why}] {decision.reasoning}"
            elif self.notifier and s.alert_on_skips:
                await self.notifier.send(f"Skipped {name}", decision.reasoning, event="skip")
            self.state.record_decision(launch, report, decision, strategy=strategy)
            self._track(launch, report, "BLOCKED" if decision.action == "BUY" else "SKIP", strategy)
            return None

        fill = await ctx.executor.buy(launch.mint, approved.size_native)
        if not fill or fill.tokens <= 0:
            approved.reasoning = f"[buy failed] {approved.reasoning}"
            self.state.record_decision(launch, report, approved, strategy=strategy)
            self._track(launch, report, "BLOCKED", strategy)
            return None
        pos = Position(
            mint=launch.mint, symbol=name, entry_sol=fill.sol,
            tokens=fill.tokens, entry_price=fill.sol / fill.tokens, opened_at=time.time(),
            take_profit_pct=approved.take_profit_pct, stop_loss_pct=approved.stop_loss_pct,
            trailing_stop_pct=s.trailing_stop_pct, max_hold_minutes=approved.max_hold_minutes,
            chain=launch.chain, strategy=strategy, entry_usd=fill.sol * native_usd)
        self.state.open_position(pos, launch.creator, ctx.executor.mode, fill.sig)
        self.state.record_decision(launch, report, approved, executed=True, strategy=strategy)
        self._track(launch, report, "BUY", strategy)
        native = ctx.spec.native
        log.info("BOUGHT %s on %s via %s: %.5f %s ($%.2f, %s) tp+%.0f%% sl-%.0f%% hold %.0fm",
                 pos.symbol, pos.chain, strategy, fill.sol, native, pos.entry_usd, ctx.executor.mode,
                 pos.take_profit_pct, pos.stop_loss_pct, pos.max_hold_minutes)
        if self.notifier:
            await self.notifier.send(
                f"{self._label(pos.chain)}BUY {pos.symbol} ${pos.entry_usd:.2f} on {pos.chain} ({strategy})",
                f"{approved.reasoning}\nconfidence {approved.confidence:.0%} | "
                f"TP +{pos.take_profit_pct:.0f}% / SL -{pos.stop_loss_pct:.0f}% / {pos.max_hold_minutes:.0f}m\n"
                f"{dexscreener_link(pos.chain, pos.mint)}",
                event="buy", data={"chain": pos.chain, "mint": pos.mint, "usd": pos.entry_usd,
                                   "sig": fill.sig, "strategy": strategy})
        return pos

    # --- exits ---------------------------------------------------------------
    async def _usd_rate(self, p):
        """USD per native coin now; falls back to the rate at entry."""
        price = await self.prices.usd(p.chain)
        return price or (p.entry_usd / p.entry_sol if p.entry_sol else 0.0)

    async def _alert_exit(self, p, title, reason, native_received):
        if not self.notifier:
            return
        pnl = p.realized_usd - p.entry_usd
        pct = pnl / p.entry_usd * 100 if p.entry_usd else 0
        sym = self.chains[p.chain].spec.native if p.chain in self.chains else ""
        await self.notifier.send(
            f"{self._label(p.chain) if p.chain in self.chains else ''}{title} {p.symbol} ({p.chain})",
            f"{reason}\n+{native_received:.5f} {sym} this sale; position P&L so far ${pnl:+.2f} ({pct:+.0f}%)\n"
            f"{dexscreener_link(p.chain, p.mint)}",
            urgent=title == "WRITE-OFF", event=title.lower().split()[0],
            data={"chain": p.chain, "mint": p.mint, "pnl_usd": pnl})

    async def reconcile(self, p):
        """Sync with the wallet when tokens left it outside the bot (e.g. sold in the FOMO app).

        Returns True if the position was closed. What you received for the external sale isn't
        visible to the bot, so it is estimated at the current sell quote.
        """
        ctx = self.chains[p.chain]
        held = await ctx.executor.held_tokens(p.mint)
        if held is None or held >= p.tokens * 0.99:
            return False
        missing = p.tokens - held
        est = await ctx.router.value_native(p.mint, missing, self.s.slippage_bps) or 0.0
        p.realized_sol += est
        p.realized_usd += est * await self._usd_rate(p)
        p.tokens = float(held)
        reason = "sold outside the bot (FOMO app?); proceeds estimated"
        if held <= 0:
            self.state.close_position(p, reason)
            log.info("CLOSED %s externally: est. +%.5f", p.symbol, est)
            await self._alert_exit(p, "CLOSED (manual)", reason, est)
            return True
        self.state.save_position(p)
        log.info("%s partly sold outside the bot: est. +%.5f", p.symbol, est)
        return False

    async def check_position(self, p, now=None):
        now = now or time.time()
        if p.chain not in self.chains:
            log.warning("%s is on %s, which is not enabled: not managed", p.symbol, p.chain)
            return None
        ctx = self.chains[p.chain]
        if await self.reconcile(p):
            return None
        value = await ctx.router.value_native(p.mint, p.tokens, self.s.slippage_bps)
        price = value / p.tokens if value is not None and p.tokens > 0 else None
        fade = (self.watchlist.fade(p.chain, p.mint)
                if self.watchlist is not None and self.s.exit_on_fade else None)
        action = exits.evaluate(p, price, now, self.s.ladder, fade)
        if action.fraction <= 0:
            self._sell_failures.pop(p.id, None)
            self.state.save_position(p)
            return None

        sell_tokens = p.tokens if action.fraction >= 1 else p.tokens * action.fraction
        fill = await ctx.executor.sell(p.mint, sell_tokens)
        if not fill:
            n = self._sell_failures[p.id] = self._sell_failures.get(p.id, 0) + 1
            if n >= WRITE_OFF_AFTER_FAILURES:
                log.warning("writing off %s after %d failed sells", p.symbol, n)
                self.state.close_position(p, f"written off: {action.reason}")
                await self._alert_exit(p, "WRITE-OFF", action.reason, 0.0)
            return None
        self._sell_failures.pop(p.id, None)
        p.tokens = max(p.tokens - fill.tokens, 0.0)
        p.realized_sol += fill.sol
        p.realized_usd += fill.sol * await self._usd_rate(p)
        if fill.sig and not fill.sig.startswith("paper"):
            self.state.add_exit_sig(p.id, fill.sig)
        if action.fraction < 1:
            p.tp_taken += 1
            self.state.save_position(p)
            log.info("PARTIAL %s %s: +%.5f", p.symbol, action.reason, fill.sol)
            await self._alert_exit(p, f"SOLD {action.fraction:.0%}", action.reason, fill.sol)
        else:
            self.state.close_position(p, action.reason)
            log.info("CLOSED %s %s: pnl $%+.2f", p.symbol, action.reason, p.realized_usd - p.entry_usd)
            await self._alert_exit(p, "CLOSED", action.reason, fill.sol)
        return action

    async def monitor(self):
        while True:
            for p in self.state.open_positions():
                try:
                    await self.check_position(p)
                except Exception:
                    log.exception("monitor error on %s", p.symbol)
            await asyncio.sleep(self.s.monitor_interval_s)

    def _track(self, launch, report, verdict, strategy):
        """Start outcome tracking in the background so it never delays a trade."""
        if self.outcomes is not None:
            self._spawn(self.outcomes.track(launch, report, verdict, strategy))

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

    async def on_signal(self, launch, strategy, signal):
        self._spawn(self.handle_signal(launch, strategy, signal))

    async def run(self, feeds):
        jobs = [self.monitor()] + [self.consume(f) for f in feeds]
        if self.outcomes is not None:
            jobs.append(self.outcomes.run())
        if self.watchlist is not None and {"breakout", "dip"} & set(self.s.strategies):
            jobs.append(self.watchlist.run(self.on_signal))
        if self.hub is not None:
            jobs.append(self.hub.run())
        await asyncio.gather(*jobs)
