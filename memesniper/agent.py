"""Claude makes the final entry call on every candidate that survives the safety screen.

One structured-output request per candidate: no tool loop, because every second
spent deciding is paid for in entry price. Effort defaults to low for the same
reason. Claude's size and exits are proposals: RiskManager clamps them afterwards.
"""
import asyncio
import json
import logging
import time

import anthropic

from .models import Decision

log = logging.getLogger("memesniper")

# $ per million tokens (input, output) for the cost guard
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
}

SYSTEM_PROMPT = """You are the entry desk of an automated Solana meme coin sniper. For each \
candidate token you decide BUY or SKIP, and for a BUY you set size and exits.

Context you should hold:
- The overwhelming majority of new meme coins go to zero within hours. Rug pulls, \
coordinated bundles, wash trading and dev dumps are the norm. SKIP is the default; \
BUY needs evidence that real, organic demand is arriving faster than supply.
- Code has already hard-rejected tokens with live mint/freeze authority, dangerous \
token-2022 extensions, concentrated holders, unsellable routes or heavy sell tax. \
What reaches you passed those checks, but warnings still matter.
- Signals of organic demand: many distinct buyers rather than a few large ones, buy \
volume well above sell volume, steady market-cap growth rather than one spike, a \
creator who has not sold, a coherent name/narrative, real (not placeholder) socials.
- Signals of manipulation: a handful of wallets doing most of the buying, very large \
early single buys (bundles/snipers who will dump on you), the creator selling, \
copycat names of trending coins, generic or missing metadata, a market cap already \
far above launch with momentum fading.
- safety.metrics.creator is the creator's history: their other launches, how many \
graduated on pump.fun, how earlier tokens this bot tracked turned out, and the wallet's age. \
Many prior launches with no graduations, or a wallet created hours ago, mark a throwaway \
launcher. A creator whose earlier coins did well is a positive.
- Recent closed trades are included so you can see what has and has not been working. \
Learn from them, but do not overfit to one or two outcomes.

Sizing: size_sol must not exceed max_size_sol. Scale it with conviction; marginal \
setups that you still want deserve less than the max. Exits: take_profit_pct is the \
gain at which half the position is sold; stop_loss_pct is the loss that closes it; \
max_hold_minutes ends the trade regardless. Tighter stops and shorter holds suit \
fast pump-and-fade setups. For SKIP, set size_sol and the exit fields to 0.

confidence is your probability that this trade hits its take-profit before its stop. \
Keep reasoning to 1-3 sentences naming the deciding factors."""

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "SKIP"]},
        "confidence": {"type": "number"},
        "size_sol": {"type": "number"},
        "take_profit_pct": {"type": "number"},
        "stop_loss_pct": {"type": "number"},
        "max_hold_minutes": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "confidence", "size_sol", "take_profit_pct",
                 "stop_loss_pct", "max_hold_minutes", "reasoning"],
    "additionalProperties": False,
}


def build_candidate(launch, report, settings, portfolio):
    """The per-candidate JSON Claude sees. Kept deterministic (sorted keys)."""
    return {
        "token": {
            "mint": launch.mint, "name": launch.name, "symbol": launch.symbol,
            "source": launch.source, "pool": launch.pool,
            "age_seconds": round(time.time() - launch.seen_at, 1),
            "creator": launch.creator, "creator_initial_buy_sol": launch.initial_buy_sol,
            "market_cap_sol_at_launch": launch.market_cap_sol,
            "description": launch.description,
            "socials": {k: v for k, v in launch.socials.items() if not k.startswith("_")},
        },
        "safety": {"warnings": report.warnings, "metrics": report.metrics},
        "limits": {
            "max_size_sol": settings.max_sol_per_trade,
            "default_take_profit_pct": settings.take_profit_pct,
            "max_stop_loss_pct": settings.stop_loss_pct,
            "max_hold_minutes": settings.max_hold_minutes,
            "slippage_bps": settings.slippage_bps,
        },
        "portfolio": portfolio,
    }


class EntryAgent:
    def __init__(self, settings, client=None):
        self.s = settings
        self.client = client or anthropic.AsyncAnthropic(max_retries=1)
        self._calls = []          # timestamps, for the hourly cap

    def _rate_limited(self):
        now = time.time()
        self._calls = [t for t in self._calls if now - t < 3600]
        return len(self._calls) >= self.s.max_claude_calls_per_hour

    def cost(self, usage):
        pin, pout = PRICES.get(self.s.model, PRICES["claude-opus-5-5"])
        inp = (usage.input_tokens + (usage.cache_creation_input_tokens or 0) * 1.25
               + (usage.cache_read_input_tokens or 0) * 0.1)
        return (inp * pin + usage.output_tokens * pout) / 1e6

    async def decide(self, launch, report, portfolio, spent_today_usd=0.0):
        if self._rate_limited():
            return Decision("SKIP", reasoning="claude hourly call cap reached")
        if spent_today_usd >= self.s.max_claude_usd_per_day:
            return Decision("SKIP", reasoning="claude daily spend cap reached")
        self._calls.append(time.time())
        candidate = build_candidate(launch, report, self.s, portfolio)
        kwargs = dict(
            model=self.s.model,
            max_tokens=4000,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content":
                       "Candidate:\n" + json.dumps(candidate, sort_keys=True, default=str)}],
            output_config={"effort": self.s.effort,
                           "format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
        )
        if self.s.use_fallbacks:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        start = time.time()
        try:
            resp = await asyncio.wait_for(self.client.beta.messages.create(**kwargs),
                                          timeout=self.s.decision_timeout_s)
        except asyncio.TimeoutError:
            return Decision("SKIP", reasoning="claude timed out", latency_s=time.time() - start)
        except anthropic.RateLimitError:
            return Decision("SKIP", reasoning="claude rate limited", latency_s=time.time() - start)
        except anthropic.APIStatusError as e:
            log.warning("claude API error %s: %s", e.status_code, e.message)
            return Decision("SKIP", reasoning=f"claude error {e.status_code}")
        except anthropic.APIConnectionError:
            return Decision("SKIP", reasoning="claude unreachable")
        latency = time.time() - start
        cost = self.cost(resp.usage)
        if resp.stop_reason == "refusal":
            return Decision("SKIP", reasoning="claude declined", model=resp.model,
                            cost_usd=cost, latency_s=latency)
        if resp.stop_reason == "max_tokens":
            return Decision("SKIP", reasoning="claude output truncated", model=resp.model,
                            cost_usd=cost, latency_s=latency)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            d = json.loads(text)
        except ValueError:
            return Decision("SKIP", reasoning="unparseable decision", model=resp.model,
                            cost_usd=cost, latency_s=latency)
        return Decision(
            action=d["action"], confidence=float(d["confidence"]), size_sol=float(d["size_sol"]),
            take_profit_pct=float(d["take_profit_pct"]), stop_loss_pct=float(d["stop_loss_pct"]),
            max_hold_minutes=float(d["max_hold_minutes"]), reasoning=d["reasoning"][:1000],
            model=resp.model, cost_usd=cost, latency_s=latency)
