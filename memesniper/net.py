"""Thin async HTTP + Solana JSON-RPC helpers.

httpx2 is the HTTP library the anthropic SDK already depends on, so no extra
dependency is needed.
"""
import itertools
import logging

import httpx2 as httpx

log = logging.getLogger("memesniper")

_ids = itertools.count(1)


def make_client(timeout=8.0):
    return httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "memesniper/0.1"})


async def get_json(client, url, params=None, headers=None):
    """GET JSON; returns None on any network/HTTP error (callers treat as unknown)."""
    try:
        r = await client.get(url, params=params, headers=headers)
        if r.status_code != 200:
            log.debug("GET %s -> %s", url, r.status_code)
            return None
        return r.json()
    except Exception as e:  # network errors must never crash the sniper loop
        log.debug("GET %s failed: %s", url, e)
        return None


async def get_text(client, url, limit=200_000):
    """GET a web page; returns (status, text) or (None, "") on network errors."""
    try:
        r = await client.get(url, follow_redirects=True, timeout=6.0)
        return r.status_code, (r.text or "")[:limit]
    except Exception as e:
        log.debug("GET %s failed: %s", url, e)
        return None, ""


class RpcError(RuntimeError):
    pass


class SolanaRpc:
    def __init__(self, client, url):
        self.client, self.url = client, url

    async def call(self, method, params=None):
        body = {"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params or []}
        r = await self.client.post(self.url, json=body)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RpcError(f"{method}: {data['error']}")
        return data["result"]

    async def mint_info(self, mint):
        """Parsed SPL mint account: decimals, supply, mintAuthority, freezeAuthority."""
        res = await self.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        value = (res or {}).get("value")
        if not value:
            return None
        parsed = value["data"]["parsed"]["info"]
        parsed["program"] = value.get("owner")
        return parsed

    async def largest_holders(self, mint):
        """Top-20 token accounts: [{address, uiAmount}], plus their owners."""
        res = await self.call("getTokenLargestAccounts", [mint])
        accounts = res.get("value", [])
        if not accounts:
            return []
        owners = await self.call("getMultipleAccounts",
                                 [[a["address"] for a in accounts], {"encoding": "jsonParsed"}])
        for a, acc in zip(accounts, owners.get("value", [])):
            try:
                a["owner"] = acc["data"]["parsed"]["info"]["owner"]
            except (TypeError, KeyError):
                a["owner"] = ""
        return accounts

    async def balance_sol(self, pubkey):
        res = await self.call("getBalance", [pubkey])
        return res["value"] / 1e9

    async def token_balance_raw(self, owner, mint):
        res = await self.call("getTokenAccountsByOwner",
                              [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        total = 0
        for acc in res.get("value", []):
            total += int(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        return total

    async def send_raw(self, b64_tx):
        return await self.call("sendTransaction", [b64_tx, {
            "encoding": "base64", "skipPreflight": True, "maxRetries": 3}])

    async def signature_status(self, sig):
        res = await self.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": False}])
        return (res.get("value") or [None])[0]
