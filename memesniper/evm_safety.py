"""EVM rug screen: GoPlus token security data plus honeypot.is sell simulation.

EVM scams differ from Solana's: hidden owner functions, adjustable taxes, blacklists,
trading pauses and proxy contracts that can be swapped after you buy. GoPlus flags those from
the contract; honeypot.is simulates an actual buy and sell to measure the real taxes.
"""
from .net import get_json

# GoPlus flags that alone mean the deployer can trap or drain holders.
FATAL_FLAGS = {
    "is_honeypot": "honeypot (GoPlus)",
    "cannot_sell_all": "cannot sell full balance",
    "cannot_buy": "buying disabled",
    "hidden_owner": "hidden owner",
    "can_take_back_ownership": "ownership can be reclaimed",
    "owner_change_balance": "owner can change balances",
    "selfdestruct": "contract can self-destruct",
    "is_blacklisted": "has a blacklist",
    "transfer_pausable": "trading can be paused",
    "slippage_modifiable": "tax can be changed",
    "personal_slippage_modifiable": "per-wallet tax can be set",
    "is_airdrop_scam": "airdrop scam",
}
RISKY_FLAGS = {
    "is_mintable": "supply is mintable",
    "is_proxy": "upgradeable proxy contract",
    "external_call": "makes external calls",
    "trading_cooldown": "trading cooldown",
    "is_anti_whale": "anti-whale limits",
}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class GoPlus:
    def __init__(self, client, base_url="https://api.gopluslabs.io/api/v1"):
        self.client, self.base = client, base_url.rstrip("/")

    async def token(self, chain_id, address):
        res = await get_json(self.client, f"{self.base}/token_security/{chain_id}",
                             params={"contract_addresses": address})
        result = (res or {}).get("result") or {}
        return result.get(address.lower()) or next(iter(result.values()), None)


class HoneypotIs:
    def __init__(self, client, base_url="https://api.honeypot.is/v2"):
        self.client, self.base = client, base_url.rstrip("/")

    async def check(self, chain_id, address):
        return await get_json(self.client, f"{self.base}/IsHoneypot",
                              params={"address": address, "chainID": str(chain_id)})


def check_goplus(report, data, settings):
    if not data:
        report.fail("no GoPlus security data (unknown contract risk)")
        return
    for flag, label in FATAL_FLAGS.items():
        if str(data.get(flag, "0")) == "1":
            report.fail(label)
    risky = [label for flag, label in RISKY_FLAGS.items() if str(data.get(flag, "0")) == "1"]
    if risky:
        report.warnings.extend(risky)
    if str(data.get("is_open_source", "1")) == "0":
        report.fail("contract source not verified")
    buy_tax, sell_tax = _f(data.get("buy_tax")), _f(data.get("sell_tax"))
    if sell_tax is not None and sell_tax * 100 > settings.evm_max_tax_pct:
        report.fail(f"sell tax {sell_tax * 100:.1f}% (max {settings.evm_max_tax_pct}%)")
    if buy_tax is not None and buy_tax * 100 > settings.evm_max_tax_pct:
        report.fail(f"buy tax {buy_tax * 100:.1f}% (max {settings.evm_max_tax_pct}%)")

    holders = data.get("holders") or []
    # Locked tokens and contracts (pools, lockers, burn) aren't a whale who can dump.
    wallets = [h for h in holders if str(h.get("is_contract")) != "1" and str(h.get("is_locked")) != "1"]
    top10 = sum(_f(h.get("percent")) or 0 for h in wallets[:10]) * 100
    creator_pct = (_f(data.get("creator_percent")) or 0) * 100
    lp_locked = sum((_f(h.get("percent")) or 0) for h in data.get("lp_holders") or []
                    if str(h.get("is_locked")) == "1") * 100
    report.metrics.update({
        "holder_count": int(_f(data.get("holder_count")) or 0),
        "top10_holder_pct": round(top10, 2), "creator_holding_pct": round(creator_pct, 2),
        "buy_tax_pct": round((buy_tax or 0) * 100, 2), "sell_tax_pct": round((sell_tax or 0) * 100, 2),
        "lp_locked_pct": round(lp_locked, 1), "owner": data.get("owner_address") or "",
        "creator": data.get("creator_address") or "",
    })
    if top10 > settings.max_top10_holder_pct:
        report.fail(f"top-10 wallets hold {top10:.1f}% (max {settings.max_top10_holder_pct}%)")
    if creator_pct > settings.max_creator_holding_pct:
        report.fail(f"creator holds {creator_pct:.1f}% (max {settings.max_creator_holding_pct}%)")
    if lp_locked < settings.evm_min_lp_locked_pct:
        report.warnings.append(f"only {lp_locked:.0f}% of liquidity locked or burned (rug-pull risk)")


def check_honeypot_sim(report, data, settings):
    if not data:
        report.warnings.append("honeypot.is simulation unavailable")
        return
    if (data.get("honeypotResult") or {}).get("isHoneypot"):
        report.fail("honeypot (sell simulation failed)")
        return
    sim = data.get("simulationResult") or {}
    sell_tax = _f(sim.get("sellTax"))
    if sell_tax is not None:
        report.metrics["simulated_sell_tax_pct"] = round(sell_tax, 2)
        if sell_tax > settings.evm_max_tax_pct:
            report.fail(f"simulated sell tax {sell_tax:.1f}% (max {settings.evm_max_tax_pct}%)")


def check_gas(report, buy_q, trade_usd, settings):
    """Gas for a buy and a sell must be a small share of the position (matters on Ethereum)."""
    if not buy_q or not trade_usd:
        return
    gas_usd = buy_q.get("gasUsd", 0) * 2
    pct = gas_usd / trade_usd * 100
    report.metrics["round_trip_gas_usd"] = round(gas_usd, 2)
    if pct > settings.max_fee_pct:
        report.fail(f"gas ${gas_usd:.2f} is {pct:.0f}% of a ${trade_usd:.0f} trade (max {settings.max_fee_pct}%)")
