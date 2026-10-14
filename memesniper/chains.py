"""Chain registry. Every chain-specific constant lives here."""
from dataclasses import dataclass

EVM_NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"   # KyberSwap's placeholder for ETH/BNB


@dataclass(frozen=True)
class ChainSpec:
    name: str               # our id, also DexScreener's chainId
    family: str             # solana | evm
    native: str             # display symbol
    native_decimals: int
    wrapped: str            # wrapped native token, for USD pricing
    gecko_network: str      # GeckoTerminal network id
    evm_chain_id: int = 0
    kyber_chain: str = ""
    default_rpc: str = ""
    eip1559: bool = True
    explorer_tx: str = ""

    @property
    def is_evm(self):
        return self.family == "evm"


CHAINS = {
    "solana": ChainSpec("solana", "solana", "SOL", 9, "So11111111111111111111111111111111111111112",
                        "solana", default_rpc="https://api.mainnet-beta.solana.com",
                        explorer_tx="https://solscan.io/tx/"),
    "base": ChainSpec("base", "evm", "ETH", 18, "0x4200000000000000000000000000000000000006",
                      "base", 8453, "base", "https://mainnet.base.org",
                      explorer_tx="https://basescan.org/tx/"),
    "bsc": ChainSpec("bsc", "evm", "BNB", 18, "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
                     "bsc", 56, "bsc", "https://bsc-dataseed.bnbchain.org", eip1559=False,
                     explorer_tx="https://bscscan.com/tx/"),
    "ethereum": ChainSpec("ethereum", "evm", "ETH", 18, "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                          "eth", 1, "ethereum", "https://ethereum-rpc.publicnode.com",
                          explorer_tx="https://etherscan.io/tx/"),
}
GECKO_TO_CHAIN = {c.gecko_network: c.name for c in CHAINS.values()}


def spec(name):
    try:
        return CHAINS[name]
    except KeyError:
        raise SystemExit(f"unknown chain '{name}'; supported: {', '.join(CHAINS)}")


def token_key(chain, address):
    """Dedupe key. EVM addresses are case-insensitive; Solana mints are not."""
    return address if chain == "solana" else f"{chain}:{address.lower()}"


@dataclass
class ChainCtx:
    """Everything the engine needs to trade one chain."""
    spec: ChainSpec
    router: object              # Jupiter or KyberRouter: buy_quote / sell_quote / value_native
    executor: object            # paper or live executor for this chain
    rpc: object = None          # Solana: SolanaRpc; EVM: EvmRpc
    rugcheck: object = None     # Solana only
    goplus: object = None       # EVM only
    honeypot: object = None     # EVM only
