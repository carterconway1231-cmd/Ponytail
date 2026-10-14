"""Market activity for a candidate across time windows, from GeckoTerminal.

Unique buyers vs. unique sellers is the cleanest read on real demand: 200 trades from 8
wallets is wash trading, 200 trades from 150 wallets is a crowd. Volume, price change and
liquidity per window show whether a move is building or already fading.
"""
from datetime import datetime, timezone

from .chains import CHAINS
from .net import get_json

WINDOWS = ("m5", "m15", "m30", "h1", "h24")


def summarize_pool(pool, now=None):
    a = (pool or {}).get("attributes") or {}
    tx, vol, chg = a.get("transactions") or {}, a.get("volume_usd") or {}, a.get("price_change_percentage") or {}
    windows = {}
    for w in WINDOWS:
        t = tx.get(w) or {}
        if not t and w not in vol:
            continue
        windows[w] = {"buys": t.get("buys"), "sells": t.get("sells"), "buyers": t.get("buyers"),
                      "sellers": t.get("sellers"), "volume_usd": float(vol.get(w) or 0),
                      "price_change_pct": float(chg.get(w) or 0) if w in chg else None}
    age = None
    if a.get("pool_created_at"):
        try:
            created = datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00"))
            age = round(((now or datetime.now(timezone.utc)) - created).total_seconds() / 60, 1)
        except ValueError:
            pass
    h1 = windows.get("h1") or {}
    ratio = (h1["buyers"] / max(h1["sellers"], 1)) if h1.get("buyers") is not None and h1.get("sellers") is not None else None
    return {"pool": a.get("address"), "dex": (((pool.get("relationships") or {}).get("dex") or {})
                                                .get("data") or {}).get("id"),
            "liquidity_usd": float(a.get("reserve_in_usd") or 0), "fdv_usd": float(a.get("fdv_usd") or 0),
            "market_cap_usd": float(a.get("market_cap_usd") or 0) or None, "pool_age_minutes": age,
            "windows": windows, "buyer_seller_ratio_h1": round(ratio, 2) if ratio is not None else None}


class MarketData:
    HEADERS = {"Accept": "application/json;version=20230302"}

    def __init__(self, client, base_url):
        self.client, self.base = client, base_url.rstrip("/")

    async def activity(self, chain, token):
        net = CHAINS[chain].gecko_network
        data = await get_json(self.client, f"{self.base}/networks/{net}/tokens/{token}/pools",
                              params={"page": "1"}, headers=self.HEADERS)
        pools = (data or {}).get("data") or []
        if not pools:
            return None
        best = max(pools, key=lambda p: float(((p.get("attributes") or {}).get("reserve_in_usd")) or 0))
        return summarize_pool(best)


def check_market(report, market, settings):
    if not market:
        report.warnings.append("no GeckoTerminal market data yet")
        return
    report.metrics["market"] = market
    for w in ("m5", "h1"):
        win = market["windows"].get(w) or {}
        buyers, sellers = win.get("buyers"), win.get("sellers")
        if buyers is not None and sellers is not None and buyers >= 10 and sellers > 2 * buyers:
            report.warnings.append(f"{w}: {sellers} sellers vs {buyers} buyers")
        trades = (win.get("buys") or 0) + (win.get("sells") or 0)
        wallets = (buyers or 0) + (sellers or 0)
        if trades >= 100 and wallets and trades / wallets > settings.max_trades_per_wallet:
            report.warnings.append(f"{w}: {trades} trades from {wallets} wallets (possible wash trading)")
