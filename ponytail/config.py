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


def _stop_type(value):
    value = value.strip().lower()
    if value not in ("stop_market", "stop_limit"):
        raise SystemExit("STOP_ORDER_TYPE must be stop_market or stop_limit")
    return value


@dataclass(frozen=True)
class Config:
    account_number: str
    symbols: list = field(default_factory=list)
    live_trading: bool = False

    # Per-trade and portfolio limits (dollars of premium; 1 contract = price * 100)
    max_premium_per_trade: float = 200.0
    max_total_premium: float = 600.0
    max_open_positions: int = 3
    max_daily_loss: float = 150.0      # realized today + current unrealized losses
    max_weekly_loss: float = 300.0     # realized over the trailing 7 days
    max_consecutive_losses: int = 3    # pause new entries for the day after this many losers
    loss_cooldown_days: int = 3        # no re-entry in a symbol this soon after a losing exit

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
    stop_loss_pct: float = 0.35        # protective stop at entry * (1 - this)
    trail_activate_pct: float = 0.30   # start trailing once up this much
    trail_pct: float = 0.25            # trailing stop distance below the high-water mark
    stop_order_type: str = "stop_market"  # or "stop_limit" (GTC, but can miss on a gap)
    stop_limit_buffer_pct: float = 0.15   # stop_limit: limit this far below the trigger
    exit_dte: int = 7

    # Signal engine & learning
    signal_threshold: float = 0.25     # |learned-weight score| needed for BUY/SELL
    min_confluence: int = 4            # factors that must agree (score >= 0.25 in the trade direction)
    min_win_prob: float = 0.52         # learned P(win) floor once a conviction bucket has evidence
    min_calibration_trades: int = 10   # trades in a bucket before P(win)/EV gates apply
    learn_half_life_days: float = 90.0  # evidence half-life; shorter adapts faster to regime change
    warm_start_weight: float = 0.3     # how much one replayed historical day teaches vs a real trade
    shadow_weight: float = 0.3         # how much an untraded signal's outcome teaches vs a real trade
    shadow_horizon: int = 5            # trading days until an untraded signal is graded

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
            max_weekly_loss=_float("MAX_WEEKLY_LOSS", 300),
            max_consecutive_losses=_int("MAX_CONSECUTIVE_LOSSES", 3),
            loss_cooldown_days=_int("LOSS_COOLDOWN_DAYS", 3),
            min_dte=_int("MIN_DTE", 14),
            max_dte=_int("MAX_DTE", 60),
            min_abs_delta=_float("MIN_ABS_DELTA", 0.30),
            max_abs_delta=_float("MAX_ABS_DELTA", 0.70),
            max_spread_pct=_float("MAX_SPREAD_PCT", 0.10),
            min_open_interest=_int("MIN_OPEN_INTEREST", 100),
            max_quote_age_min=_float("MAX_QUOTE_AGE_MIN", 30),
            avoid_earnings=_bool("AVOID_EARNINGS", True),
            take_profit_pct=_float("TAKE_PROFIT_PCT", 0.50),
            stop_loss_pct=_float("STOP_LOSS_PCT", 0.35),
            trail_activate_pct=_float("TRAIL_ACTIVATE_PCT", 0.30),
            trail_pct=_float("TRAIL_PCT", 0.25),
            stop_order_type=_stop_type(os.environ.get("STOP_ORDER_TYPE", "stop_market")),
            stop_limit_buffer_pct=_float("STOP_LIMIT_BUFFER_PCT", 0.15),
            exit_dte=_int("EXIT_DTE", 7),
            signal_threshold=_float("SIGNAL_THRESHOLD", 0.25),
            min_confluence=_int("MIN_CONFLUENCE", 4),
            min_win_prob=_float("MIN_WIN_PROB", 0.52),
            min_calibration_trades=_int("MIN_CALIBRATION_TRADES", 10),
            learn_half_life_days=_float("LEARN_HALF_LIFE_DAYS", 90),
            warm_start_weight=_float("WARM_START_WEIGHT", 0.3),
            shadow_weight=_float("SHADOW_WEIGHT", 0.3),
            shadow_horizon=_int("SHADOW_HORIZON", 5),
            model=os.environ.get("AGENT_MODEL", "claude-opus-5-5"),
            effort=os.environ.get("AGENT_EFFORT", "high"),
            max_turns=_int("AGENT_MAX_TURNS", 80),
            max_budget_usd=_float("AGENT_MAX_BUDGET_USD", 3.0),
            robinhood_mcp_url=os.environ.get("ROBINHOOD_MCP_URL", "").strip(),
            robinhood_mcp_token=os.environ.get("ROBINHOOD_MCP_TOKEN", "").strip(),
            state_path=os.environ.get("AGENT_STATE_PATH", "agent_state.json"),
        )
