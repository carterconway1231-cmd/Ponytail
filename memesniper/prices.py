"""USD price of each chain's native coin (SOL, ETH, BNB), cached.

All risk caps are in USD so one set of limits covers every chain. If a price can't be
fetched the bot refuses to size trades on that chain rather than guess.
"""
import time

from .chains import CHAINS
from .net import get_json

TTL_S = 60


class NativePrices:
    def __init__(self, client, dexscreener_url, fixed=None):
        self.client, self.base = client, dexscreener_url.rstrip("/")
        self._cache = {}                 # chain -> (ts, usd)
        self._fixed = fixed or {}        # tests / offline overrides

    async def usd(self, chain):
        if chain in self._fixed:
            return self._fixed[chain]
        hit = self._cache.get(chain)
        if hit and time.time() - hit[0] < TTL_S:
            return hit[1]
        wrapped = CHAINS[chain].wrapped
        pairs = await get_json(self.client, f"{self.base}/tokens/v1/{chain}/{wrapped}")
        price = None
        if isinstance(pairs, list):
            # Pairs where the wrapped coin is the base token, deepest first.
            own = [p for p in pairs if (p.get("baseToken") or {}).get("address", "").lower() == wrapped.lower()
                   and p.get("priceUsd")]
            own.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True)
            if own:
                price = float(own[0]["priceUsd"])
        if price:
            self._cache[chain] = (time.time(), price)
            return price
        return hit[1] if hit and time.time() - hit[0] < 10 * TTL_S else None
