"""Creator history: serial launchers and repeat ruggers are the strongest single red flag.

Three sources, each optional and fail-soft:
- local:   every launch this bot has seen, plus outcomes it tracked for the creator's earlier
           tokens (free, but starts empty and fills as the bot runs)
- pump.fun: the creator's past coins from pump.fun's public frontend API (unofficial: may
           change or rate-limit; set MEMESNIPER_PUMPFUN_API_URL="" to disable)
- RPC:     wallet age and activity (a brand-new wallet is a throwaway)
"""
import time

from .net import get_json

DAY = 86_400


def summarize_pump_coins(coins, exclude_mint, now=None):
    """pump.fun coin list -> launch counts and how many graduated to a DEX."""
    now = now or time.time()
    prior = [c for c in coins or [] if c.get("mint") != exclude_mint]
    created = [(c.get("created_timestamp") or 0) / 1000 for c in prior]
    return {
        "pump_launches": len(prior),
        "pump_launches_24h": sum(1 for t in created if now - t < DAY),
        "pump_graduated": sum(1 for c in prior if c.get("complete")),
    }


def wallet_age(signatures, now=None):
    """getSignaturesForAddress page (newest first) -> (age_hours or None, tx_count, capped)."""
    if not signatures:
        return None, 0, False
    capped = len(signatures) >= 1000
    oldest = signatures[-1].get("blockTime")
    age = (now or time.time()) - oldest if oldest else None
    # A full page means older history exists, so the true age is at least this.
    return (age / 3600 if age is not None else None), len(signatures), capped


class CreatorProfiler:
    def __init__(self, settings, state, rpc, client):
        self.s, self.state, self.rpc, self.client = settings, state, rpc, client

    def local(self, creator, exclude_mint):
        return self.state.creator_stats(creator, exclude_mint)

    async def remote(self, creator, exclude_mint):
        out = {}
        if self.s.pumpfun_api_url:
            coins = await get_json(
                self.client, f"{self.s.pumpfun_api_url.rstrip('/')}/coins/user-created-coins/{creator}",
                params={"offset": "0", "limit": "50", "includeNsfw": "true"})
            if isinstance(coins, dict):
                coins = coins.get("coins")
            if isinstance(coins, list):
                out.update(summarize_pump_coins(coins, exclude_mint))
        try:
            sigs = await self.rpc.call("getSignaturesForAddress", [creator, {"limit": 1000}])
            age_h, n, capped = wallet_age(sigs)
            out.update(wallet_age_hours=round(age_h, 1) if age_h is not None else None,
                       wallet_tx_count=f"{n}+" if capped else n)
        except Exception:
            pass
        return out


def check_creator_local(report, local, settings):
    """Cheap DB-only checks, run before any network call."""
    report.metrics["creator"] = dict(local)
    if local["prior_rugs"] >= settings.creator_max_prior_rugs > 0:
        report.fail(f"creator rugged before: {local['prior_rugs']} earlier token(s) fell >90% within an hour")
    if local["launches_24h"] > settings.creator_max_launches_24h:
        report.fail(f"serial launcher: {local['launches_24h']} other launches in 24h seen by this bot")


def check_creator_remote(report, remote, settings):
    creator = report.metrics.setdefault("creator", {})
    creator.update(remote)
    n24 = remote.get("pump_launches_24h", 0)
    if n24 > settings.creator_max_launches_24h and not any("serial launcher" in f for f in report.hard_fails):
        report.fail(f"serial launcher: {n24} other pump.fun launches in 24h")
    n, grad = remote.get("pump_launches", 0), remote.get("pump_graduated", 0)
    if n >= 5 and grad == 0:
        report.warnings.append(f"creator launched {n} coins before, none graduated")
    age = remote.get("wallet_age_hours")
    if age is not None and remote.get("wallet_tx_count") != "1000+" and age < 24:
        report.warnings.append(f"creator wallet is only {age:.0f}h old")
        if settings.creator_min_wallet_age_hours and age < settings.creator_min_wallet_age_hours:
            report.fail(f"creator wallet {age:.0f}h old (min {settings.creator_min_wallet_age_hours:.0f}h)")
