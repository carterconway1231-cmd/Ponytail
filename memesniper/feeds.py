"""Launch sources.

PumpPortal: one websocket for every new pump.fun token, migrations to PumpSwap/Raydium,
and per-token trade streams (used for the observation window). PumpPortal asks
clients to share ONE connection, so a single PumpPortalHub owns it and fans out.

DexScreener: polls newly profiled Solana tokens, which catches launches on other
launchpads and DEXs.
"""
import asyncio
import json
import logging
import time
from collections import defaultdict

from .models import Launch, TradeTick, Observation
from .net import get_json

log = logging.getLogger("memesniper")


def launch_from_pumpportal(msg):
    """Map a PumpPortal create/migration message to a Launch (None for other messages)."""
    if not isinstance(msg, dict) or "mint" not in msg:
        return None
    tx = msg.get("txType")
    if tx == "create":
        sol = float(msg.get("solAmount") or 0)
        return Launch(mint=msg["mint"], source="pumpportal", name=msg.get("name", ""),
                      symbol=msg.get("symbol", ""), creator=msg.get("traderPublicKey", ""),
                      uri=msg.get("uri", ""), pool=msg.get("pool", "pump"),
                      market_cap_sol=float(msg.get("marketCapSol") or 0), initial_buy_sol=sol,
                      dev_initial_tokens=float(msg.get("initialBuy") or 0),
                      bonding_curve=msg.get("bondingCurveKey", ""))
    if tx == "migrate":
        return Launch(mint=msg["mint"], source="pumpportal-migration",
                      pool=msg.get("pool", "pump-amm"))
    return None


def tick_from_pumpportal(msg):
    if not isinstance(msg, dict) or msg.get("txType") not in ("buy", "sell"):
        return None
    return TradeTick(mint=msg["mint"], side=msg["txType"], trader=msg.get("traderPublicKey", ""),
                     sol=float(msg.get("solAmount") or 0), tokens=float(msg.get("tokenAmount") or 0),
                     market_cap_sol=float(msg.get("marketCapSol") or 0))


def summarize_ticks(launch, ticks, seconds):
    """Turn the trades seen during the observation window into an Observation."""
    obs = Observation(seconds=seconds, start_mcap_sol=launch.market_cap_sol,
                      end_mcap_sol=launch.market_cap_sol)
    buyers = set()
    dev_tokens_in, dev_tokens_out = 0.0, 0.0
    for t in ticks:
        if t.side == "buy":
            obs.buys += 1
            obs.buy_sol += t.sol
            obs.largest_buy_sol = max(obs.largest_buy_sol, t.sol)
            if t.trader != launch.creator:
                buyers.add(t.trader)
            else:
                dev_tokens_in += t.tokens
        else:
            obs.sells += 1
            obs.sell_sol += t.sol
            if t.trader == launch.creator:
                dev_tokens_out += t.tokens
        if t.market_cap_sol:
            obs.end_mcap_sol = t.market_cap_sol
    dev_initial = launch.dev_initial_tokens + dev_tokens_in
    if dev_initial > 0:
        obs.dev_sold_pct = min(100.0, dev_tokens_out / dev_initial * 100)
    elif dev_tokens_out > 0:
        obs.dev_sold_pct = 100.0
    obs.unique_buyers = len(buyers)
    return obs


class PumpPortalHub:
    def __init__(self, url):
        self.url = url
        self.ws = None
        self.launches = asyncio.Queue(maxsize=5000)
        self._watchers = defaultdict(list)       # mint -> [list of ticks]
        self._send_lock = asyncio.Lock()

    async def run(self):
        import websockets
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=20, max_size=2**22) as ws:
                    self.ws = ws
                    backoff = 1
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    if self._watchers:
                        await ws.send(json.dumps({"method": "subscribeTokenTrade",
                                                  "keys": list(self._watchers)}))
                    log.info("pumpportal connected")
                    async for raw in ws:
                        self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("pumpportal disconnected (%s); retry in %ss", e, backoff)
            self.ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def _dispatch(self, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        tick = tick_from_pumpportal(msg)
        if tick:
            for sink in self._watchers.get(tick.mint, ()):
                sink.append(tick)
            return
        launch = launch_from_pumpportal(msg)
        if launch:
            try:
                self.launches.put_nowait(launch)
            except asyncio.QueueFull:
                pass  # we're behind; dropping is better than sniping stale launches

    async def _send(self, payload):
        if self.ws is None:
            return
        async with self._send_lock:
            try:
                await self.ws.send(json.dumps(payload))
            except Exception as e:
                log.debug("pumpportal send failed: %s", e)

    async def observe(self, launch, seconds):
        """Collect the token's trades for `seconds`, then unsubscribe and summarize."""
        sink = []
        self._watchers[launch.mint].append(sink)
        await self._send({"method": "subscribeTokenTrade", "keys": [launch.mint]})
        start = time.time()
        try:
            await asyncio.sleep(seconds)
        finally:
            sinks = self._watchers.get(launch.mint, [])
            if sink in sinks:
                sinks.remove(sink)
            if not sinks:
                self._watchers.pop(launch.mint, None)
                await self._send({"method": "unsubscribeTokenTrade", "keys": [launch.mint]})
        return summarize_ticks(launch, sink, time.time() - start)

    async def stream(self):
        while True:
            yield await self.launches.get()


class DexScreenerFeed:
    def __init__(self, client, base_url, poll_s):
        self.client, self.base, self.poll_s = client, base_url.rstrip("/"), poll_s
        self._seen = set()

    async def stream(self):
        while True:
            for launch in await self.poll():
                yield launch
            await asyncio.sleep(self.poll_s)

    async def poll(self):
        profiles = await get_json(self.client, f"{self.base}/token-profiles/latest/v1") or []
        fresh = [p for p in profiles
                 if p.get("chainId") == "solana" and p.get("tokenAddress") not in self._seen]
        out = []
        for p in fresh:
            mint = p["tokenAddress"]
            self._seen.add(mint)
            socials = {l.get("type") or l.get("label", "link"): l.get("url")
                       for l in p.get("links") or [] if l.get("url")}
            out.append(Launch(mint=mint, source="dexscreener",
                              description=(p.get("description") or "")[:500], socials=socials))
        if len(self._seen) > 50_000:
            self._seen = set(list(self._seen)[-10_000:])
        return out

    async def pair_info(self, mint):
        """Best Solana pair for a token: liquidity, volume, price change, age, socials."""
        data = await get_json(self.client, f"{self.base}/tokens/v1/solana/{mint}")
        pairs = data if isinstance(data, list) else (data or {}).get("pairs") or []
        if not pairs:
            return None
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        info = best.get("info") or {}
        return {
            "dex": best.get("dexId"),
            "name": (best.get("baseToken") or {}).get("name", ""),
            "symbol": (best.get("baseToken") or {}).get("symbol", ""),
            "liquidity_usd": (best.get("liquidity") or {}).get("usd") or 0.0,
            "fdv_usd": best.get("fdv") or 0.0,
            "volume_m5_usd": (best.get("volume") or {}).get("m5") or 0.0,
            "volume_h1_usd": (best.get("volume") or {}).get("h1") or 0.0,
            "price_change_m5_pct": (best.get("priceChange") or {}).get("m5") or 0.0,
            "price_change_h1_pct": (best.get("priceChange") or {}).get("h1") or 0.0,
            "txns_m5": (best.get("txns") or {}).get("m5") or {},
            "pair_created_at_ms": best.get("pairCreatedAt"),
            "websites": [w.get("url") for w in info.get("websites") or []],
            "socials": {s.get("type"): s.get("url") for s in info.get("socials") or []},
        }


async def fetch_metadata(client, uri):
    """pump.fun metadata JSON (description, twitter, telegram, website)."""
    if not uri:
        return {}
    if uri.startswith("ipfs://"):
        uri = "https://ipfs.io/ipfs/" + uri[len("ipfs://"):]
    data = await get_json(client, uri) or {}
    return {k: str(data[k])[:500] for k in ("description", "twitter", "telegram", "website")
            if data.get(k)}
