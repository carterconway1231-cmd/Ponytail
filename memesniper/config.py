"""Every knob comes from the environment (MEMESNIPER_* variables, or a .env file).

The risk limits here are enforced in code by RiskManager. Claude proposes a size
and exits, and the code clamps them, so the model cannot spend past a cap.
"""
import os
from dataclasses import dataclass, fields

from .chains import spec

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


def _env(name, default):
    return os.environ.get(f"MEMESNIPER_{name}", default)


def _bool(name, default):
    return str(_env(name, default)).strip().lower() in ("1", "true", "yes", "on")


def _float(name, default):
    return float(_env(name, default))


def _int(name, default):
    return int(_env(name, default))


def _list(name, default=""):
    return [x.strip() for x in str(_env(name, default)).split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    # --- mode -------------------------------------------------------------
    mode: str = "paper"                 # paper | live
    state_path: str = "memesniper_state.db"
    kill_switch_path: str = "MEMESNIPER_STOP"   # touch this file to halt new entries

    # --- endpoints ----------------------------------------------------------
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    pumpportal_ws: str = "wss://pumpportal.fun/api/data"
    jupiter_url: str = "https://lite-api.jup.ag/swap/v1"
    jupiter_api_key: str = ""
    dexscreener_url: str = "https://api.dexscreener.com"
    rugcheck_url: str = "https://api.rugcheck.xyz/v1"
    sources: tuple = ("pumpportal", "dexscreener", "geckoterminal", "trending")
    trending_poll_s: float = 120.0      # trending pools + boosted tokens, every chain
    dexscreener_poll_s: float = 15.0
    geckoterminal_url: str = "https://api.geckoterminal.com/api/v2"
    geckoterminal_poll_s: float = 60.0
    kyberswap_url: str = "https://aggregator-api.kyberswap.com"
    goplus_url: str = "https://api.gopluslabs.io/api/v1"
    honeypot_url: str = "https://api.honeypot.is/v2"

    # --- chains ---------------------------------------------------------------
    chains: tuple = ("solana", "base", "bsc", "ethereum")
    base_rpc_url: str = ""              # empty = public RPC from chains.py
    bsc_rpc_url: str = ""
    ethereum_rpc_url: str = ""

    # --- wallets (live only) ------------------------------------------------
    private_key: str = ""               # Solana: base58 secret key of a BURNER wallet
    evm_private_key: str = ""           # Base/BSC/Ethereum: 0x hex key of a BURNER wallet
    priority_fee_lamports: int = 200_000
    min_wallet_reserve_usd: float = 5.0    # per chain: never spend below this (gas, rent)

    # --- observation window -------------------------------------------------
    # Watch a fresh pump.fun launch's trades for this long before deciding.
    observe_seconds: float = 30.0
    max_concurrent_observations: int = 25

    # --- hard pre-filters (rejected before Claude is asked) ---------------
    require_mint_revoked: bool = True
    require_freeze_revoked: bool = True
    max_top10_holder_pct: float = 35.0  # excludes bonding curve / AMM pool accounts
    max_creator_holding_pct: float = 10.0
    max_roundtrip_loss_pct: float = 12.0   # buy then immediate sell, via Jupiter quotes
    min_liquidity_usd: float = 5_000.0     # migrated/DEX pairs only
    max_rugcheck_score: int = 5_000        # rugcheck.xyz normalised risk score
    min_unique_buyers: int = 8          # within the observation window
    max_dev_sold_pct: float = 50.0      # creator has dumped more than this of their bag
    blocked_name_words: tuple = ()
    evm_max_tax_pct: float = 10.0       # buy or sell tax (GoPlus and simulated)
    evm_min_lp_locked_pct: float = 50.0 # below this: warning that liquidity can be pulled
    max_fee_pct: float = 8.0            # round-trip gas as % of the trade (Ethereum mostly)

    # --- credibility and market checks (run on every candidate before Claude) --
    cred_max_link_reuse: int = 2        # reject if its X/Telegram/website is on more other tokens
    cred_require_social: bool = False   # reject tokens with neither X nor Telegram
    cred_min_score: int = 0             # 0-100; 0 = Claude weighs it, no hard floor
    max_trades_per_wallet: float = 8.0  # above this in a window = likely wash trading (warning)

    # --- Claude -------------------------------------------------------------
    model: str = "claude-opus-5-5"
    effort: str = "low"                 # latency matters: every second costs entry price
    decision_timeout_s: float = 25.0
    min_confidence: float = 0.6
    max_claude_calls_per_hour: int = 60
    max_claude_usd_per_day: float = 5.0
    use_fallbacks: bool = True          # server-side refusal fallback

    # --- position sizing and risk caps (USD, across all chains) -------------
    max_usd_per_trade: float = 10.0
    max_open_positions: int = 4
    max_total_exposure_usd: float = 40.0
    max_daily_spend_usd: float = 80.0
    max_daily_loss_usd: float = 40.0
    max_entries_per_hour: int = 6
    creator_cooldown_hours: float = 24.0
    slippage_bps: int = 1500            # meme coins are thin; 15%
    paper_extra_slippage_pct: float = 2.0   # haircut on paper fills vs the quote

    # --- exits (defaults; Claude may tighten within these bounds) --------
    take_profit_pct: float = 100.0      # first take-profit rung (Claude may set its own)
    # Sell into strength in stages: "multiple:fraction" of the take-profit level, fraction of
    # the CURRENT balance. Default: half at +TP, half the rest at +2xTP, trail the remainder.
    tp_ladder: tuple = ("1:0.5", "2:0.5")
    exit_on_fade: bool = True           # sell when watchlist data shows momentum fading
    stop_loss_pct: float = 30.0
    trailing_stop_pct: float = 25.0     # from the high, armed once in profit
    max_hold_minutes: float = 60.0
    monitor_interval_s: float = 5.0

    # --- alerts (all optional) -------------------------------------------
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    webhook_url: str = ""
    alert_on_skips: bool = False        # also alert when Claude passes on a candidate

    # --- outcome tracking (what happened after each decision) ---------------
    outcome_tracking: bool = True
    outcome_sample_rejects: float = 0.1  # share of safety rejections to track (all Claude calls are)
    outcome_max_new_per_hour: int = 300
    outcome_check_interval_s: float = 30.0

    # --- creator history ------------------------------------------------------
    pumpfun_api_url: str = "https://frontend-api-v3.pump.fun"   # unofficial; "" disables
    creator_max_launches_24h: int = 3    # more other launches than this in 24h = serial launcher
    creator_max_prior_rugs: int = 1      # reject once this many earlier tokens rugged (0 = off)
    creator_min_wallet_age_hours: float = 0.0   # 0 = only warn about fresh wallets

    # --- strategies -----------------------------------------------------------
    strategies: tuple = ("launch", "breakout", "dip")
    claude_strategies: tuple = ("launch", "breakout", "dip")   # others run on code rules alone
    watch_hours: float = 24.0           # how long a screened token stays on the watchlist
    watch_poll_s: float = 60.0
    watch_max_tokens: int = 500
    signal_cooldown_minutes: float = 60.0
    breakout_threshold: float = 0.7     # 0-1 score: volume, buy pressure, momentum, growth
    breakout_max_chase_pct: float = 300.0   # skip if already up this much in an hour
    dip_min_trend_pct: float = 50.0     # 6h gain that counts as an uptrend
    dip_min_pullback_pct: float = 15.0  # pullback from the recent high to buy
    dip_max_pullback_pct: float = 40.0  # deeper than this = trend broken, not a dip
    dip_lookback_minutes: float = 120.0

    @property
    def live(self):
        return self.mode == "live"

    @property
    def ladder(self):
        """[(multiple of the take-profit level, fraction of current balance to sell)]"""
        rungs = sorted((float(m), float(f)) for m, f in (r.split(":") for r in self.tp_ladder))
        if any(not 0 < f <= 1 or m <= 0 for m, f in rungs):
            raise ValueError("bad ladder")
        return rungs

    def rpc_for(self, chain):
        return {"solana": self.rpc_url, "base": self.base_rpc_url, "bsc": self.bsc_rpc_url,
                "ethereum": self.ethereum_rpc_url}.get(chain) or spec(chain).default_rpc


def load_settings(**overrides):
    """Build Settings from MEMESNIPER_* env vars; keyword overrides win."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    d = Settings()
    values = {}
    for f in fields(Settings):
        cur = getattr(d, f.name)
        key = f.name.upper()
        if isinstance(cur, bool):
            values[f.name] = _bool(key, cur)
        elif isinstance(cur, int):
            values[f.name] = _int(key, cur)
        elif isinstance(cur, float):
            values[f.name] = _float(key, cur)
        elif isinstance(cur, tuple):
            values[f.name] = tuple(_list(key, ",".join(cur)))
        else:
            values[f.name] = str(_env(key, cur))
    values.update(overrides)
    s = Settings(**values)
    for c in s.chains:
        spec(c)
    try:
        s.ladder
    except ValueError:
        raise SystemExit("MEMESNIPER_TP_LADDER must look like 1:0.5,2:0.5")
    if s.mode not in ("paper", "live"):
        raise SystemExit("MEMESNIPER_MODE must be paper or live")
    if s.live and "solana" in s.chains and not s.private_key:
        raise SystemExit("live Solana trading needs MEMESNIPER_PRIVATE_KEY (use a burner wallet), "
                         "or drop solana from MEMESNIPER_CHAINS")
    if s.live and any(spec(c).is_evm for c in s.chains) and not s.evm_private_key:
        raise SystemExit("live Base/BSC/Ethereum trading needs MEMESNIPER_EVM_PRIVATE_KEY "
                         "(use a burner wallet), or drop them from MEMESNIPER_CHAINS")
    if s.effort not in ("low", "medium", "high", "xhigh", "max"):
        raise SystemExit("MEMESNIPER_EFFORT must be low|medium|high|xhigh|max")
    return s
