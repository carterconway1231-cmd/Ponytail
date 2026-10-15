"""Watchlist: tokens that passed the safety screen, re-scored every minute for watch_hours.

Three signals, all pure functions of the snapshot history so they can be tested and replayed:
- breakout_score: volume accelerating, buyers outnumbering sellers, price and liquidity rising.
  A high score means momentum is building. It does NOT mean the token will pump: most don't.
  The outcome tracker measures how often it is right.
- dip_signal: a token in a strong uptrend has pulled back 15-40% from its recent high, liquidity
  is intact, and buyers are stepping back in.
- fade_reason: momentum fading (sellers dominating, price dropping) or liquidity being pulled.
  Used to sell open positions while they are still in profit.
"""
import asyncio
import logging
import time
from collections import defaultdict, deque

from .models import Launch

log = logging.getLogger("memesniper")

MAX_SNAPS = 300          # 5h at one per minute


def _clamp(x):
    return max(0.0, min(1.0, x))


def breakout_score(snaps, settings):
    """0-1 momentum score from the latest snapshot vs. its history. Returns (score, parts)."""
    if len(snaps) < 3:
        return 0.0, {}
    now, first = snaps[-1], snaps[0]
    if now["liquidity_usd"] < settings.min_liquidity_usd or now["price_usd"] <= 0:
        return 0.0, {"skip": "liquidity too low"}
    if now["chg_h1"] > settings.breakout_max_chase_pct:
        return 0.0, {"skip": f"already up {now['chg_h1']:.0f}% in 1h"}
    hourly_avg_m5 = now["vol_h1"] / 12 if now["vol_h1"] else 0
    vol_accel = _clamp((now["vol_m5"] / hourly_avg_m5 - 1) / 3) if hourly_avg_m5 else 0.0
    txns = now["buys_m5"] + now["sells_m5"]
    buy_pressure = _clamp((now["buys_m5"] / txns - 0.5) / 0.25) if txns >= 10 else 0.0
    momentum = _clamp(now["chg_m5"] / 10) if now["chg_h1"] > 0 else 0.0
    prior_buys = [s["buys_m5"] for s in list(snaps)[:-1]]
    avg_buys = sum(prior_buys) / len(prior_buys) if prior_buys else 0
    txn_growth = _clamp((now["buys_m5"] / avg_buys - 1) / 2) if avg_buys else 0.0
    liq_growth = _clamp((now["liquidity_usd"] / first["liquidity_usd"] - 1) / 0.5) \
        if first["liquidity_usd"] else 0.0
    parts = {"volume_accel": round(vol_accel, 2), "buy_pressure": round(buy_pressure, 2),
             "momentum": round(momentum, 2), "buyer_growth": round(txn_growth, 2),
             "liquidity_growth": round(liq_growth, 2)}
    score = (0.30 * vol_accel + 0.25 * buy_pressure + 0.20 * momentum
             + 0.15 * txn_growth + 0.10 * liq_growth)
    return round(score, 3), parts


def dip_signal(snaps, settings):
    """Pullback inside an uptrend, with buyers returning. Returns (fire, details)."""
    if len(snaps) < 3:
        return False, {}
    now = snaps[-1]
    cutoff = now["ts"] - settings.dip_lookback_minutes * 60
    recent = [s for s in snaps if s["ts"] >= cutoff and s["price_usd"] > 0]
    if len(recent) < 3 or now["price_usd"] <= 0:
        return False, {}
    high = max(s["price_usd"] for s in recent)
    max_liq = max(s["liquidity_usd"] for s in recent)
    pullback = (1 - now["price_usd"] / high) * 100
    details = {"trend_h6_pct": now["chg_h6"], "pullback_pct": round(pullback, 1),
               "liquidity_vs_max": round(now["liquidity_usd"] / max_liq, 2) if max_liq else 0}
    fire = (now["chg_h6"] >= settings.dip_min_trend_pct
            and settings.dip_min_pullback_pct <= pullback <= settings.dip_max_pullback_pct
            and now["liquidity_usd"] >= 0.85 * max_liq
            and now["liquidity_usd"] >= settings.min_liquidity_usd
            and now["chg_m5"] >= 0 and now["buys_m5"] >= now["sells_m5"])
    return fire, details


def fade_reason(snaps):
    """Why to sell now even though no stop or target was hit, or None."""
    if len(snaps) < 2:
        return None
    now = snaps[-1]
    max_liq = max(s["liquidity_usd"] for s in snaps)
    if max_liq and now["liquidity_usd"] < 0.7 * max_liq:
        return f"liquidity pulled ({now['liquidity_usd'] / max_liq:.0%} of peak)"
    txns = now["buys_m5"] + now["sells_m5"]
    if txns >= 10 and now["sells_m5"] >= 2 * now["buys_m5"] and now["chg_m5"] <= -5:
        return f"momentum fading ({now['sells_m5']} sells vs {now['buys_m5']} buys in 5m)"
    return None


class Watchlist:
    def __init__(self, settings, state, dexscreener):
        self.s, self.state, self.dex = settings, state, dexscreener
        self.snaps = defaultdict(lambda: deque(maxlen=MAX_SNAPS))     # key -> snapshots

    def add(self, launch):
        self.state.watch(launch, self.s.watch_hours)

    def fade(self, chain, token):
        from .chains import token_key
        return fade_reason(self.snaps.get(token_key(chain, token)) or [])

    def latest(self, chain, token):
        from .chains import token_key
        s = self.snaps.get(token_key(chain, token))
        return s[-1] if s else None

    async def poll(self, now=None):
        """Refresh snapshots and return [(Launch, strategy, signal_details)] that fired."""
        now = now or time.time()
        rows = self.state.watched(now)[: self.s.watch_max_tokens]
        live_keys = {r["key"] for r in rows}
        for k in list(self.snaps):
            if k not in live_keys:
                del self.snaps[k]
        by_chain = defaultdict(list)
        for r in rows:
            by_chain[r["chain"]].append(r)

        fired = []
        for chain, entries in by_chain.items():
            snaps = await self.dex.snapshots(chain, [r["token"] for r in entries])
            for r in entries:
                snap = snaps.get(r["token"].lower())
                if not snap:
                    continue
                hist = self.snaps[r["key"]]
                hist.append(snap)
                if now - (r["last_signal_at"] or 0) < self.s.signal_cooldown_minutes * 60:
                    continue
                if self.state.holding(chain, r["token"]):
                    continue
                signal = self._signal(hist)
                if signal:
                    strategy, details = signal
                    self.state.mark_signal(r["key"], now)
                    launch = Launch(mint=r["token"], source="watchlist", chain=chain,
                                    symbol=r["symbol"] or "", name=r["symbol"] or "",
                                    creator=r["creator"] or "")
                    fired.append((launch, strategy, {**details, "snapshot": snap}))
        return fired

    def _signal(self, hist):
        if "breakout" in self.s.strategies:
            score, parts = breakout_score(hist, self.s)
            if score >= self.s.breakout_threshold:
                return "breakout", {"breakout_score": score, **parts}
        if "dip" in self.s.strategies:
            fire, details = dip_signal(hist, self.s)
            if fire:
                return "dip", details
        return None

    async def run(self, on_signal):
        while True:
            try:
                for launch, strategy, details in await self.poll():
                    await on_signal(launch, strategy, details)
            except Exception:
                log.exception("watchlist poll failed")
            await asyncio.sleep(self.s.watch_poll_s)
