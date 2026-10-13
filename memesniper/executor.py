"""Order execution. PaperExecutor fills against real Jupiter quotes with a haircut;
LiveExecutor signs and sends Jupiter swap transactions from a burner wallet."""
import asyncio
import logging
import time
from dataclasses import dataclass

from .config import LAMPORTS_PER_SOL
from .jupiter import sign_b64

log = logging.getLogger("memesniper")

BASE_FEE_LAMPORTS = 5_000


@dataclass
class Fill:
    sol: float          # SOL spent (buy) or received (sell), net of fees
    tokens: float       # raw token units bought or sold
    sig: str = ""


class PaperExecutor:
    mode = "paper"

    def __init__(self, settings, jupiter):
        self.s, self.jup = settings, jupiter

    def _fee_sol(self):
        return (self.s.priority_fee_lamports + BASE_FEE_LAMPORTS) / LAMPORTS_PER_SOL

    async def wallet_sol(self):
        return None     # paper has no wallet cap; risk caps still apply

    async def held_tokens(self, mint):
        return None     # paper positions can't be touched outside the bot

    async def buy(self, mint, sol):
        q = await self.jup.buy_quote(mint, sol, self.s.slippage_bps)
        if not q:
            return None
        tokens = int(q["outAmount"]) * (1 - self.s.paper_extra_slippage_pct / 100)
        return Fill(sol=sol + self._fee_sol(), tokens=tokens, sig=f"paper-{int(time.time()*1000)}")

    async def sell(self, mint, tokens):
        q = await self.jup.sell_quote(mint, int(tokens), self.s.slippage_bps)
        if not q:
            return None
        sol = int(q["outAmount"]) / LAMPORTS_PER_SOL * (1 - self.s.paper_extra_slippage_pct / 100)
        return Fill(sol=max(sol - self._fee_sol(), 0.0), tokens=tokens,
                    sig=f"paper-{int(time.time()*1000)}")


class LiveExecutor:
    mode = "live"

    def __init__(self, settings, jupiter, rpc):
        from solders.keypair import Keypair
        self.s, self.jup, self.rpc = settings, jupiter, rpc
        self.keypair = Keypair.from_base58_string(settings.private_key)
        self.pubkey = str(self.keypair.pubkey())

    async def wallet_sol(self):
        return await self.rpc.balance_sol(self.pubkey)

    async def held_tokens(self, mint):
        """On-chain balance. Differs from the bot's books if you sold in the FOMO app."""
        return await self.rpc.token_balance_raw(self.pubkey, mint)

    async def _send(self, quote):
        b64 = await self.jup.swap_tx(quote, self.pubkey, self.s.priority_fee_lamports)
        if not b64:
            return None
        sig = await self.rpc.send_raw(sign_b64(b64, self.keypair))
        for _ in range(60):     # ~30s
            await asyncio.sleep(0.5)
            st = await self.rpc.signature_status(sig)
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                if st.get("err"):
                    log.warning("tx %s failed on chain: %s", sig, st["err"])
                    return None
                return sig
        log.warning("tx %s not confirmed in time", sig)
        return None

    async def buy(self, mint, sol):
        q = await self.jup.buy_quote(mint, sol, self.s.slippage_bps)
        if not q:
            return None
        before_sol = await self.wallet_sol()
        before_tok = await self.rpc.token_balance_raw(self.pubkey, mint)
        sig = await self._send(q)
        if not sig:
            return None
        got = await self.rpc.token_balance_raw(self.pubkey, mint) - before_tok
        spent = before_sol - await self.wallet_sol()
        return Fill(sol=spent, tokens=float(got), sig=sig)

    async def sell(self, mint, tokens):
        held = await self.rpc.token_balance_raw(self.pubkey, mint)
        amount = min(int(tokens), held)
        if amount <= 0:
            return Fill(sol=0.0, tokens=tokens, sig="")     # nothing left on chain
        q = await self.jup.sell_quote(mint, amount, self.s.slippage_bps)
        if not q:
            return None
        before_sol = await self.wallet_sol()
        sig = await self._send(q)
        if not sig:
            return None
        return Fill(sol=await self.wallet_sol() - before_sol, tokens=float(amount), sig=sig)
