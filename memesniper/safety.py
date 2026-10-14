"""Deterministic rug screen. Anything in hard_fails rejects the token before Claude
is asked, which saves both latency and API spend on the ~95% of launches that are
obvious garbage. Warnings and metrics are handed to Claude as context.
"""
import asyncio
import logging

from .evm_safety import check_gas, check_goplus, check_honeypot_sim

log = logging.getLogger("memesniper")

TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
# Token-2022 extensions that let the issuer tax, freeze or claw back holders' tokens.
DANGEROUS_EXTENSIONS = {"transferFeeConfig", "permanentDelegate", "transferHook",
                        "nonTransferable", "defaultAccountState", "pausableConfig"}
# Owners of pool vaults: their balances are liquidity, not a whale.
POOL_AUTHORITIES = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM v4 authority
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # Raydium CPMM authority
    "WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh",   # Meteora DLMM / pools
}


def check_mint(report, mint_info, settings):
    if mint_info is None:
        report.fail("mint account not found / RPC unavailable")
        return
    report.metrics["decimals"] = mint_info.get("decimals")
    report.metrics["supply_raw"] = int(mint_info.get("supply") or 0)
    if settings.require_mint_revoked and mint_info.get("mintAuthority"):
        report.fail("mint authority not revoked (supply can be inflated)")
    if settings.require_freeze_revoked and mint_info.get("freezeAuthority"):
        report.fail("freeze authority not revoked (your tokens can be frozen)")
    if mint_info.get("program") == TOKEN_2022:
        exts = {e.get("extension") for e in mint_info.get("extensions") or []}
        bad = exts & DANGEROUS_EXTENSIONS
        if bad:
            report.fail(f"token-2022 extensions: {', '.join(sorted(bad))}")
        report.metrics["token2022_extensions"] = sorted(e for e in exts if e)


def check_holders(report, holders, launch, settings):
    supply = report.metrics.get("supply_raw") or 0
    decimals = report.metrics.get("decimals") or 0
    if not holders or not supply:
        report.warnings.append("holder distribution unavailable")
        return
    excluded = POOL_AUTHORITIES | {launch.bonding_curve}
    ui_supply = supply / 10 ** decimals
    real = [h for h in holders
            if h.get("owner") not in excluded and h.get("address") != launch.bonding_curve]
    pct = [float(h.get("uiAmount") or 0) / ui_supply * 100 for h in real]
    top10 = sum(sorted(pct, reverse=True)[:10])
    report.metrics["top10_holder_pct"] = round(top10, 2)
    report.metrics["top1_holder_pct"] = round(max(pct, default=0), 2)
    if top10 > settings.max_top10_holder_pct:
        report.fail(f"top-10 wallets hold {top10:.1f}% (max {settings.max_top10_holder_pct}%)")
    if launch.creator:
        creator_pct = sum(p for h, p in zip(real, pct) if h.get("owner") == launch.creator)
        report.metrics["creator_holding_pct"] = round(creator_pct, 2)
        if creator_pct > settings.max_creator_holding_pct:
            report.fail(f"creator holds {creator_pct:.1f}% (max {settings.max_creator_holding_pct}%)")


def check_rugcheck(report, summary, settings):
    if not summary:
        report.warnings.append("rugcheck unavailable")
        return
    score = summary.get("score_normalised", summary.get("score"))
    report.metrics["rugcheck_score"] = score
    risks = summary.get("risks") or []
    report.metrics["rugcheck_risks"] = [f"{r.get('level')}: {r.get('name')}" for r in risks][:10]
    if score is not None and score > settings.max_rugcheck_score:
        report.fail(f"rugcheck score {score} > {settings.max_rugcheck_score}")
    for r in risks:
        if r.get("level") == "danger":
            report.warnings.append(f"rugcheck danger: {r.get('name')}")


def check_roundtrip(report, buy_q, sell_q, size_native, settings, native_decimals=9):
    """A buy route plus a sell route back is the honeypot test: if we can't sell, we don't buy."""
    if not buy_q:
        report.fail("no buy route")
        return
    report.ref_quote = (size_native, int(buy_q["outAmount"]))
    if not sell_q:
        report.fail("no sell route (possible honeypot)")
        return
    back = int(sell_q["outAmount"]) / 10 ** native_decimals
    loss = (1 - back / size_native) * 100
    report.metrics["roundtrip_loss_pct"] = round(loss, 2)
    report.metrics["buy_price_impact_pct"] = round(float(buy_q.get("priceImpactPct") or 0) * 100, 2)
    if loss > settings.max_roundtrip_loss_pct:
        report.fail(f"round trip loses {loss:.1f}% (max {settings.max_roundtrip_loss_pct}%): "
                    "sell tax or no depth")


def check_observation(report, obs, settings):
    if obs is None:
        return
    report.metrics["observation"] = {
        "seconds": round(obs.seconds, 1), "buys": obs.buys, "sells": obs.sells,
        "unique_buyers": obs.unique_buyers, "buy_sol": round(obs.buy_sol, 3),
        "sell_sol": round(obs.sell_sol, 3), "largest_buy_sol": round(obs.largest_buy_sol, 3),
        "dev_sold_pct": round(obs.dev_sold_pct, 1),
        "mcap_change_pct": round(obs.mcap_change_pct, 1),
        "end_mcap_sol": round(obs.end_mcap_sol, 2),
    }
    if obs.unique_buyers < settings.min_unique_buyers:
        report.fail(f"only {obs.unique_buyers} unique buyers in {obs.seconds:.0f}s "
                    f"(min {settings.min_unique_buyers})")
    if obs.dev_sold_pct > settings.max_dev_sold_pct:
        report.fail(f"creator already sold {obs.dev_sold_pct:.0f}% of their bag")


def check_pair(report, pair, launch, settings):
    if pair is None:
        if launch.source != "pumpportal":
            report.warnings.append("no DexScreener pair yet")
        return
    report.metrics["pair"] = pair
    on_curve = launch.source == "pumpportal" or pair.get("dex") == "pumpfun"
    if not on_curve and pair["liquidity_usd"] < settings.min_liquidity_usd:
        report.fail(f"liquidity ${pair['liquidity_usd']:,.0f} < ${settings.min_liquidity_usd:,.0f}")


def check_name(report, launch, settings):
    text = f"{launch.name} {launch.symbol}".lower()
    for w in settings.blocked_name_words:
        if w.lower() in text:
            report.fail(f"name matches blocked word '{w}'")


def check_activity(report, launch, settings):
    """Pool trading activity from GeckoTerminal (non-pump.fun launches)."""
    act = launch.activity
    if not act:
        return
    report.metrics["pool_activity"] = act
    buyers = act.get("buyers_h1")
    if buyers is not None and buyers < settings.min_unique_buyers:
        report.fail(f"only {buyers} unique buyers in the last hour (min {settings.min_unique_buyers})")


async def screen(launch, settings, ctx, dexscreener, observation=None, creators=None,
                 trade_native=None, trade_usd=None):
    """Run every check concurrently and return a SafetyReport.

    ctx: the chain's ChainCtx (router, rpc, chain-specific checkers).
    trade_native / trade_usd: the size the round-trip test should use."""
    from .creator import check_creator_local, check_creator_remote
    from .models import SafetyReport
    report = SafetyReport(mint=launch.mint)
    check_name(report, launch, settings)
    check_observation(report, observation, settings)
    check_activity(report, launch, settings)
    if creators is not None and launch.creator:
        check_creator_local(report, creators.local(launch.creator, launch.mint), settings)
    if not report.passed:
        return report   # cheap fails first: skip the network calls
    if not trade_native:
        report.fail(f"no {launch.chain} price: can't size the safety round trip")
        return report

    async def safe(coro):
        try:
            return await coro
        except Exception as e:
            log.debug("safety call failed for %s: %s", launch.mint, e)
            return None

    async def nothing():
        return None

    router, spec = ctx.router, ctx.spec
    pair_task = safe(dexscreener.pair_info(launch.mint, launch.chain))
    buy_task = safe(router.buy_quote(launch.mint, trade_native, settings.slippage_bps))

    if spec.is_evm:
        goplus, sim, buy_q, pair = await asyncio.gather(
            safe(ctx.goplus.token(spec.evm_chain_id, launch.mint)),
            safe(ctx.honeypot.check(spec.evm_chain_id, launch.mint)), buy_task, pair_task)
        check_goplus(report, goplus, settings)
        check_honeypot_sim(report, sim, settings)
        check_gas(report, buy_q, trade_usd, settings)
        if not launch.creator and report.metrics.get("creator") and creators is not None:
            launch.creator = report.metrics["creator"]
            check_creator_local(report, creators.local(launch.creator, launch.mint), settings)
        remote = None
    else:
        creator_remote = (creators.remote(launch.creator, launch.mint)
                          if creators is not None and launch.creator else nothing())
        mint_info, holders, rug, buy_q, pair, remote = await asyncio.gather(
            safe(ctx.rpc.mint_info(launch.mint)), safe(ctx.rpc.largest_holders(launch.mint)),
            safe(ctx.rugcheck.summary(launch.mint)), buy_task, pair_task, safe(creator_remote))
        check_mint(report, mint_info, settings)
        check_holders(report, holders, launch, settings)
        check_rugcheck(report, rug, settings)

    sell_q = None
    if buy_q:
        sell_q = await safe(router.sell_quote(launch.mint, int(buy_q["outAmount"]), settings.slippage_bps))
    check_roundtrip(report, buy_q, sell_q, trade_native, settings, spec.native_decimals)
    check_pair(report, pair, launch, settings)
    if remote:
        check_creator_remote(report, remote, settings)
    return report


class RugCheck:
    def __init__(self, client, base_url):
        self.client, self.base = client, base_url.rstrip("/")

    async def summary(self, mint):
        from .net import get_json
        return await get_json(self.client, f"{self.base}/tokens/{mint}/report/summary")
