"""Jupiter aggregator: quotes for pricing and safety checks, swap transactions for execution.

Jupiter routes pump.fun bonding curves, PumpSwap, Raydium and Meteora, so one
venue covers a token before and after it migrates.
"""
import base64
import logging

from .config import SOL_MINT, LAMPORTS_PER_SOL
from .net import get_json

log = logging.getLogger("memesniper")


class Jupiter:
    def __init__(self, client, base_url, api_key=""):
        self.client = client
        self.base = base_url.rstrip("/")
        self.headers = {"x-api-key": api_key} if api_key else None

    async def quote(self, input_mint, output_mint, amount_raw, slippage_bps):
        if amount_raw <= 0:
            return None
        return await get_json(self.client, f"{self.base}/quote", params={
            "inputMint": input_mint, "outputMint": output_mint,
            "amount": str(int(amount_raw)), "slippageBps": str(slippage_bps),
        }, headers=self.headers)

    async def buy_quote(self, mint, sol, slippage_bps):
        return await self.quote(SOL_MINT, mint, int(sol * LAMPORTS_PER_SOL), slippage_bps)

    async def sell_quote(self, mint, tokens_raw, slippage_bps):
        return await self.quote(mint, SOL_MINT, int(tokens_raw), slippage_bps)

    async def value_sol(self, mint, tokens_raw, slippage_bps):
        """What selling tokens_raw would return right now, in SOL (None if unroutable)."""
        q = await self.sell_quote(mint, tokens_raw, slippage_bps)
        return int(q["outAmount"]) / LAMPORTS_PER_SOL if q else None

    async def swap_tx(self, quote, user_pubkey, priority_fee_lamports):
        """Unsigned base64 VersionedTransaction for the given quote."""
        try:
            r = await self.client.post(f"{self.base}/swap", headers=self.headers, json={
                "quoteResponse": quote,
                "userPublicKey": user_pubkey,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": int(priority_fee_lamports),
            })
            r.raise_for_status()
            return r.json()["swapTransaction"]
        except Exception as e:
            log.warning("jupiter swap build failed: %s", e)
            return None


def sign_b64(b64_tx, keypair):
    """Sign a Jupiter-built transaction with our keypair; returns base64 for sendTransaction."""
    from solders.transaction import VersionedTransaction
    tx = VersionedTransaction.from_bytes(base64.b64decode(b64_tx))
    signed = VersionedTransaction(tx.message, [keypair])
    return base64.b64encode(bytes(signed)).decode()
