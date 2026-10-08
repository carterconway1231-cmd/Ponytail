"""All knobs come from the environment (.env). Risk limits here are enforced
in code by the hooks, not by the prompt, so the model cannot talk its way
past them."""
import os
from dataclasses import dataclass, field


def _bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _float(name, default):
    return float(os.environ.get(name, default))


def _int(name, default):
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    account_number: str
    symbols: list = field(default_factory=list)
    live_trading: bool = False

    # Per-trade and portfolio limits (dollars of premium; 1 contract = price * 100)
    max_premium_per_trade: float = 200.0
    max_total_premium: float = 600.0
    max_open_positions: int = 3
    max_daily_loss: float = 150.0

    # Contract selection
    min_dte: int = 14
    max_dte: int = 60
    min_abs_delta: float = 0.30
    max_abs_delta: float = 0.70
    max_spread_pct: float = 0.10  # (ask - bid) / mid
    min_open_interest: int = 100
    max_quote_age_min: float = 30.0
    avoid_earnings: bool = True

    # Exit rules
    take_profit_pct: float = 0.50
    stop_loss_pct: float = 0.40
    exit_dte: int = 7

    # Agent runtime
    model: str = "claude-opus-5-5"
    effort: str = "high"
    max_turns: int = 80
    max_budget_usd: float = 3.0
    robinhood_mcp_url: str = ""
    robinhood_mcp_token: str = ""
    state_path: str = "agent_state.json"

    @classmethod
    def from_env(cls):
        account = os.environ.get("ROBINHOOD_ACCOUNT_NUMBER", "").strip()
        if not account:
            raise SystemExit("ROBINHOOD_ACCOUNT_NUMBER is required (your agent-enabled account)")
        symbols = [s.strip().upper() for s in os.environ.get("SYMBOLS", "SPY,QQQ").split(",") if s.strip()]
        return cls(
            account_number=account,
            symbols=symbols,
            live_trading=_bool("LIVE_TRADING", False),
            max_premium_per_trade=_float("MAX_PREMIUM_PER_TRADE", 200),
            max_total_premium=_float("MAX_TOTAL_PREMIUM", 600),
            max_open_positions=_int("MAX_OPEN_POSITIONS", 3),
            max_daily_loss=_float("MAX_DAILY_LOSS", 150),
            min_dte=_int("MIN_DTE", 14),
            max_dte=_int("MAX_DTE", 60),
            min_abs_delta=_float("MIN_ABS_DELTA", 0.30),
            max_abs_delta=_float("MAX_ABS_DELTA", 0.70),
            max_spread_pct=_float("MAX_SPREAD_PCT", 0.10),
            min_open_interest=_int("MIN_OPEN_INTEREST", 100),
            max_quote_age_min=_float("MAX_QUOTE_AGE_MIN", 30),
            avoid_earnings=_bool("AVOID_EARNINGS", True),
            take_profit_pct=_float("TAKE_PROFIT_PCT", 0.50),
            stop_loss_pct=_float("STOP_LOSS_PCT", 0.40),
            exit_dte=_int("EXIT_DTE", 7),
            model=os.environ.get("AGENT_MODEL", "claude-opus-5-5"),
            effort=os.environ.get("AGENT_EFFORT", "high"),
            max_turns=_int("AGENT_MAX_TURNS", 80),
            max_budget_usd=_float("AGENT_MAX_BUDGET_USD", 3.0),
            robinhood_mcp_url=os.environ.get("ROBINHOOD_MCP_URL", "").strip(),
            robinhood_mcp_token=os.environ.get("ROBINHOOD_MCP_TOKEN", "").strip(),
            state_path=os.environ.get("AGENT_STATE_PATH", "agent_state.json"),
        )
