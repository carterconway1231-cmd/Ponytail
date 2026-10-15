"""EVM chains (Base, BSC, Ethereum): KyberSwap routing, raw JSON-RPC, signing, executors.

KyberSwap's aggregator needs no API key and covers all three chains. Amounts are raw integer
units: wei for the native coin, the token's smallest unit for tokens.
"""
import asyncio
import itertools
import logging
import time

from .chains import EVM_NATIVE
from .executor import Fill
from .net import get_json

log = logging.getLogger("memesniper")

MAX_UINT = 2**256 - 1
_ids = itertools.count(1)


# --- ABI helpers (only the four ERC-20 calls we need) -------------------------------

def _word_addr(a):
    return a.lower().removeprefix("0x").rjust(64, "0")


def _word_uint(n):
    return format(int(n), "x").rjust(64, "0")


def balance_of_data(owner):
    return "0x70a08231" + _word_addr(owner)


def allowance_data(owner, spender):
    return "0xdd62ed3e" + _word_addr(owner) + _word_addr(spender)


def approve_data(spender, amount=MAX_UINT):
    return "0x095ea7b3" + _word_addr(spender) + _word_uint(amount)


def hex_int(x):
    """RPC quantities are 0x-hex; KyberSwap sends decimal strings. Accept both."""
    if isinstance(x, str):
        return int(x, 16) if x.lower().startswith("0x") else int(x or 0)
    return int(x or 0)


# --- RPC -----------------------------------------------------------------------------

class EvmRpcError(RuntimeError):
    pass


class EvmRpc:
    def __init__(self, client, url):
        self.client, self.url = client, url

    async def call(self, method, params=None):
        r = await self.client.post(self.url, json={"jsonrpc": "2.0", "id": next(_ids),
                                                   "method": method, "params": params or []})
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise EvmRpcError(f"{method}: {data['error']}")
        return data["result"]

    async def balance(self, address):
        return hex_int(await self.call("eth_getBalance", [address, "latest"]))

    async def token_balance(self, token, owner):
        return hex_int(await self.call("eth_call", [{"to": token, "data": balance_of_data(owner)}, "latest"]))

    async def allowance(self, token, owner, spender):
        return hex_int(await self.call("eth_call", [{"to": token, "data": allowance_data(owner, spender)},
                                                    "latest"]))

    async def receipt(self, tx_hash):
        return await self.call("eth_getTransactionReceipt", [tx_hash])


# --- KyberSwap router ----------------------------------------------------------------

class KyberRouter:
    """Same interface as Jupiter: buy_quote / sell_quote / value_native, plus build()."""

    def __init__(self, client, spec, base_url="https://aggregator-api.kyberswap.com"):
        self.client, self.spec = client, spec
        self.base = f"{base_url.rstrip('/')}/{spec.kyber_chain}/api/v1"
        self.headers = {"x-client-id": "memesniper"}

    async def quote(self, token_in, token_out, amount_raw):
        if amount_raw <= 0:
            return None
        res = await get_json(self.client, f"{self.base}/routes", params={
            "tokenIn": token_in, "tokenOut": token_out, "amountIn": str(int(amount_raw)),
            "gasInclude": "true"}, headers=self.headers)
        data = (res or {}).get("data") or {}
        rs = data.get("routeSummary")
        if not rs or not rs.get("amountOut") or int(rs["amountOut"]) <= 0:
            return None
        return {"outAmount": int(rs["amountOut"]), "gasUsd": float(rs.get("gasUsd") or 0),
                "routeSummary": rs, "routerAddress": data.get("routerAddress")}

    async def buy_quote(self, token, native_amount, slippage_bps):
        return await self.quote(EVM_NATIVE, token, int(native_amount * 10 ** self.spec.native_decimals))

    async def sell_quote(self, token, tokens_raw, slippage_bps):
        return await self.quote(token, EVM_NATIVE, int(tokens_raw))

    async def value_native(self, token, tokens_raw, slippage_bps):
        q = await self.sell_quote(token, tokens_raw, slippage_bps)
        return q["outAmount"] / 10 ** self.spec.native_decimals if q else None

    async def build(self, quote, sender, slippage_bps):
        try:
            r = await self.client.post(f"{self.base}/route/build", headers=self.headers, json={
                "routeSummary": quote["routeSummary"], "sender": sender, "recipient": sender,
                "slippageTolerance": int(slippage_bps), "deadline": int(time.time()) + 600,
                "source": "memesniper"})
            r.raise_for_status()
            data = r.json().get("data") or {}
            if not data.get("data"):
                return None
            return {"to": data.get("routerAddress") or quote["routerAddress"], "data": data["data"],
                    "value": hex_int(data.get("transactionValue") or 0), "gas": hex_int(data.get("gas") or 0)}
        except Exception as e:
            log.warning("kyber build failed: %s", e)
            return None


# --- executors -----------------------------------------------------------------------

class EvmPaperExecutor:
    """Fills against live KyberSwap quotes, minus a haircut and the quoted gas cost."""
    mode = "paper"

    def __init__(self, settings, router, prices):
        self.s, self.router, self.prices = settings, router, prices
        self.dec = router.spec.native_decimals

    async def _gas_native(self, q):
        usd = await self.prices.usd(self.router.spec.name)
        return q.get("gasUsd", 0) / usd if usd else 0.0

    async def wallet_native(self):
        return None

    async def held_tokens(self, token):
        return None

    async def buy(self, token, native):
        q = await self.router.buy_quote(token, native, self.s.slippage_bps)
        if not q:
            return None
        tokens = q["outAmount"] * (1 - self.s.paper_extra_slippage_pct / 100)
        return Fill(sol=native + await self._gas_native(q), tokens=tokens, sig=f"paper-{int(time.time()*1000)}")

    async def sell(self, token, tokens):
        q = await self.router.sell_quote(token, int(tokens), self.s.slippage_bps)
        if not q:
            return None
        native = q["outAmount"] / 10 ** self.dec * (1 - self.s.paper_extra_slippage_pct / 100)
        return Fill(sol=max(native - await self._gas_native(q), 0.0), tokens=tokens,
                    sig=f"paper-{int(time.time()*1000)}")


class EvmLiveExecutor:
    """Signs and sends KyberSwap transactions from a burner wallet."""
    mode = "live"

    def __init__(self, settings, router, rpc, private_key):
        from eth_account import Account
        self.s, self.router, self.rpc = settings, router, rpc
        self.spec = router.spec
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        self.dec = self.spec.native_decimals
        self._lock = asyncio.Lock()        # one tx at a time per chain keeps nonces simple

    async def wallet_native(self):
        return await self.rpc.balance(self.address) / 10 ** self.dec

    async def held_tokens(self, token):
        return await self.rpc.token_balance(token, self.address)

    async def _fees(self):
        if not self.spec.eip1559:
            return {"gasPrice": int(hex_int(await self.rpc.call("eth_gasPrice")) * 1.1)}
        block = await self.rpc.call("eth_getBlockByNumber", ["latest", False])
        base = hex_int(block.get("baseFeePerGas"))
        try:
            tip = hex_int(await self.rpc.call("eth_maxPriorityFeePerGas"))
        except EvmRpcError:
            tip = 10**9
        return {"maxFeePerGas": base * 2 + tip, "maxPriorityFeePerGas": tip, "type": 2}

    async def send(self, to, data, value=0, gas=0):
        """Sign, send and wait for the receipt. Returns the tx hash, or None if it failed."""
        async with self._lock:
            from eth_utils import to_checksum_address
            to = to_checksum_address(to)
            tx = {"to": to, "data": data, "value": int(value), "chainId": self.spec.evm_chain_id,
                  "nonce": hex_int(await self.rpc.call("eth_getTransactionCount", [self.address, "pending"]))}
            tx.update(await self._fees())
            if not gas:
                gas = hex_int(await self.rpc.call("eth_estimateGas", [{
                    "from": self.address, "to": to, "data": data, "value": hex(int(value))}]))
            tx["gas"] = int(gas * 1.25)
            signed = self.account.sign_transaction(tx)
            tx_hash = await self.rpc.call("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()
                                                                     .removeprefix("0x")])
        for _ in range(90):
            await asyncio.sleep(1)
            rec = await self.rpc.receipt(tx_hash)
            if rec:
                if hex_int(rec.get("status")) == 1:
                    return tx_hash
                log.warning("%s tx %s reverted", self.spec.name, tx_hash)
                return None
        log.warning("%s tx %s not mined in time", self.spec.name, tx_hash)
        return None

    async def buy(self, token, native):
        q = await self.router.buy_quote(token, native, self.s.slippage_bps)
        if not q:
            return None
        tx = await self.router.build(q, self.address, self.s.slippage_bps)
        if not tx:
            return None
        before_n, before_t = await self.rpc.balance(self.address), await self.held_tokens(token)
        sig = await self.send(tx["to"], tx["data"], tx["value"], tx["gas"])
        if not sig:
            return None
        got = await self.held_tokens(token) - before_t
        spent = (before_n - await self.rpc.balance(self.address)) / 10 ** self.dec
        return Fill(sol=spent, tokens=float(got), sig=sig)

    async def sell(self, token, tokens):
        amount = min(int(tokens), await self.held_tokens(token))
        if amount <= 0:
            return Fill(sol=0.0, tokens=tokens, sig="")
        q = await self.router.sell_quote(token, amount, self.s.slippage_bps)
        if not q:
            return None
        before_n = await self.rpc.balance(self.address)
        spender = q["routerAddress"]
        if spender and await self.rpc.allowance(token, self.address, spender) < amount:
            if not await self.send(token, approve_data(spender)):
                return None
        tx = await self.router.build(q, self.address, self.s.slippage_bps)
        if not tx:
            return None
        sig = await self.send(tx["to"], tx["data"], tx["value"], tx["gas"])
        if not sig:
            return None
        got = (await self.rpc.balance(self.address) - before_n) / 10 ** self.dec
        return Fill(sol=got, tokens=float(amount), sig=sig)
