"""Plain data passed between the pipeline stages."""
import time
from dataclasses import dataclass, field, asdict


@dataclass
class Launch:
    """A newly seen token. Fields a source doesn't know stay at their defaults."""
    mint: str                           # token address (Solana mint or EVM contract)
    source: str                         # pumpportal | pumpportal-migration | dexscreener | geckoterminal | watchlist
    chain: str = "solana"
    pair: str = ""                      # pool/pair address when known
    name: str = ""
    symbol: str = ""
    creator: str = ""
    uri: str = ""
    pool: str = ""                      # pump | raydium | pumpswap | meteora ...
    market_cap_sol: float = 0.0
    initial_buy_sol: float = 0.0
    dev_initial_tokens: float = 0.0     # tokens the creator bought in the create tx
    bonding_curve: str = ""             # pump.fun curve account (excluded from holder stats)
    liquidity_usd: float = 0.0
    description: str = ""
    socials: dict = field(default_factory=dict)
    activity: dict = field(default_factory=dict)    # pool stats from GeckoTerminal, if any
    seen_at: float = field(default_factory=time.time)

    @property
    def key(self):
        from .chains import token_key
        return token_key(self.chain, self.mint)


@dataclass
class TradeTick:
    mint: str
    side: str                           # buy | sell
    trader: str
    sol: float
    tokens: float
    market_cap_sol: float = 0.0
    ts: float = field(default_factory=time.time)


@dataclass
class Observation:
    """What happened in the first observe_seconds after launch."""
    seconds: float = 0.0
    buys: int = 0
    sells: int = 0
    unique_buyers: int = 0
    buy_sol: float = 0.0
    sell_sol: float = 0.0
    dev_sold_pct: float = 0.0
    start_mcap_sol: float = 0.0
    end_mcap_sol: float = 0.0
    largest_buy_sol: float = 0.0

    @property
    def mcap_change_pct(self):
        if not self.start_mcap_sol:
            return 0.0
        return (self.end_mcap_sol / self.start_mcap_sol - 1) * 100


@dataclass
class SafetyReport:
    mint: str
    passed: bool = True
    hard_fails: list = field(default_factory=list)     # any -> reject, Claude not asked
    warnings: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    ref_quote: tuple = None     # (sol, tokens) from the round-trip buy quote, for outcome tracking

    def fail(self, reason):
        self.passed = False
        self.hard_fails.append(reason)


@dataclass
class Decision:
    action: str                         # BUY | SKIP
    confidence: float = 0.0
    size_usd: float = 0.0               # what Claude asked for
    size_native: float = 0.0            # set by RiskManager after clamping (SOL/ETH/BNB)
    take_profit_pct: float = 0.0
    stop_loss_pct: float = 0.0
    max_hold_minutes: float = 0.0
    reasoning: str = ""
    model: str = ""
    cost_usd: float = 0.0
    latency_s: float = 0.0


@dataclass
class Position:
    """Amounts named *_sol are in the chain's native coin (SOL, ETH or BNB)."""
    mint: str
    symbol: str
    entry_sol: float                    # native coin spent, fees included
    tokens: float                       # raw token units held
    entry_price: float                  # native coin per raw token unit
    opened_at: float
    take_profit_pct: float
    stop_loss_pct: float
    trailing_stop_pct: float
    max_hold_minutes: float
    high_water_price: float = 0.0
    tp_taken: int = 0                   # take-profit ladder rungs already sold
    realized_sol: float = 0.0           # native coin received from sells so far
    id: int = 0
    chain: str = "solana"
    strategy: str = "launch"
    entry_usd: float = 0.0
    realized_usd: float = 0.0

    def to_dict(self):
        return asdict(self)
