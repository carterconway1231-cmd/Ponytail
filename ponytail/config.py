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


def _choice(name, default, options):
    value = os.environ.get(name, default).strip().lower()
    if value not in options:
        raise SystemExit(f"{name} must be one of {', '.join(options)}")
    return value


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

    # Strategy: "premium" sells defined-risk put credit spreads on index ETFs
    # (the edge is implied > realized vol); "directional" is the factor-signal
    # long-premium strategy (no measurable edge in the 14-stock study).
    strategy: str = "premium"
    premium_side: str = "put"           # put | call | both (both = iron condor as two verticals)
    premium_short_delta: float = 0.20
    premium_max_short_delta: float = 0.35
    premium_min_dte: int = 30
    premium_max_dte: int = 60
    premium_take_profit: float = 0.50   # buy back once 50% of the credit is captured
    premium_stop_x: float = 2.0         # buy back when the loss reaches 2x the credit
    premium_manage_dte: int = 21        # close at 21 DTE regardless
    premium_min_iv_rv: float = 1.0      # sell only when ATM IV >= realized vol x this
    premium_min_credit_pct: float = 0.10  # credit >= 10% of width
    premium_risk_pct: float = 0.10      # max loss per position as a fraction of equity
    premium_max_total_risk_pct: float = 0.40
    premium_max_width_pct: float = 0.01   # widest spread considered, as a fraction of spot
    premium_allow_stocks: bool = False  # single-stock spreads were cost-killed in the backtest

    # Volatility & structure
    allow_spreads: bool = True         # debit verticals when IV is expensive (needs options level 3)
    iv_rank_expensive: float = 60.0
    iv_rank_cheap: float = 30.0
    iv_rv_expensive: float = 1.4       # fallback gauge until IV rank has history
    max_spread_debit_pct: float = 0.60 # debit must be <= this fraction of the strike width

    # Contract selection by expected value
    min_contract_ev: float = 0.0       # scenario EV per $ of premium must exceed this
    ev_hold_days: int = 5              # holding horizon the EV model prices
    entry_slippage_pct: float = 0.25   # expected fill: mid + this fraction of half-spread

    # Position sizing (fraction of account equity at risk per trade)
    paper_capital: float = 3000.0      # paper-mode equity baseline (set to what you plan to fund)
    base_risk_pct: float = 0.03        # before the learner has evidence
    kelly_fraction: float = 0.25
    max_risk_pct: float = 0.06
    stop_gap_allowance: float = 1.3    # risk of a stopped single = premium * stop_loss_pct * this

    # Exit learning
    time_stop_days: int = 10           # close stalled trades after this many days...
    time_stop_min_gain: float = 0.10   # ...unless up at least this much
    adaptive_exits: bool = True
    min_exit_samples: int = 15

    # Universe discovery, events, execution
    discover: bool = True              # add scanner candidates to SYMBOLS each run
    max_discovered: int = 5
    events_path: str = "events.json"
    event_blackout_days: int = 1       # no new entries this many days before FOMC/CPI
    entry_window: tuple = ("09:45", "15:45")  # US/Eastern; no entries outside

    # Go-live gate (paper results required before LIVE_TRADING is honored)
    min_paper_trades: int = 30
    min_paper_days: int = 20
    min_profit_factor: float = 1.2
    max_drawdown_pct: float = 0.25
    force_live: bool = False

    # Cost control & alerts
    monitor_model: str = "claude-haiku-5-5"
    monitor_effort: str = "low"
    monitor_budget_usd: float = 0.30
    alert_webhook_url: str = ""

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
            strategy=_choice("STRATEGY", "premium", ("premium", "directional")),
            premium_side=_choice("PREMIUM_SIDE", "put", ("put", "call", "both")),
            premium_short_delta=_float("PREMIUM_SHORT_DELTA", 0.20),
            premium_max_short_delta=_float("PREMIUM_MAX_SHORT_DELTA", 0.35),
            premium_min_dte=_int("PREMIUM_MIN_DTE", 30),
            premium_max_dte=_int("PREMIUM_MAX_DTE", 60),
            premium_take_profit=_float("PREMIUM_TAKE_PROFIT", 0.50),
            premium_stop_x=_float("PREMIUM_STOP_X", 2.0),
            premium_manage_dte=_int("PREMIUM_MANAGE_DTE", 21),
            premium_min_iv_rv=_float("PREMIUM_MIN_IV_RV", 1.0),
            premium_min_credit_pct=_float("PREMIUM_MIN_CREDIT_PCT", 0.10),
            premium_risk_pct=_float("PREMIUM_RISK_PCT", 0.10),
            premium_max_total_risk_pct=_float("PREMIUM_MAX_TOTAL_RISK_PCT", 0.40),
            premium_max_width_pct=_float("PREMIUM_MAX_WIDTH_PCT", 0.01),
            premium_allow_stocks=_bool("PREMIUM_ALLOW_STOCKS", False),
            allow_spreads=_bool("ALLOW_SPREADS", True),
            iv_rank_expensive=_float("IV_RANK_EXPENSIVE", 60),
            iv_rank_cheap=_float("IV_RANK_CHEAP", 30),
            iv_rv_expensive=_float("IV_RV_EXPENSIVE", 1.4),
            max_spread_debit_pct=_float("MAX_SPREAD_DEBIT_PCT", 0.60),
            min_contract_ev=_float("MIN_CONTRACT_EV", 0.0),
            ev_hold_days=_int("EV_HOLD_DAYS", 5),
            entry_slippage_pct=_float("ENTRY_SLIPPAGE_PCT", 0.25),
            paper_capital=_float("PAPER_CAPITAL", 3000),
            base_risk_pct=_float("BASE_RISK_PCT", 0.03),
            kelly_fraction=_float("KELLY_FRACTION", 0.25),
            max_risk_pct=_float("MAX_RISK_PCT", 0.06),
            stop_gap_allowance=_float("STOP_GAP_ALLOWANCE", 1.3),
            time_stop_days=_int("TIME_STOP_DAYS", 10),
            time_stop_min_gain=_float("TIME_STOP_MIN_GAIN", 0.10),
            adaptive_exits=_bool("ADAPTIVE_EXITS", True),
            min_exit_samples=_int("MIN_EXIT_SAMPLES", 15),
            discover=_bool("DISCOVER", True),
            max_discovered=_int("MAX_DISCOVERED", 5),
            events_path=os.environ.get("EVENTS_PATH", "events.json"),
            event_blackout_days=_int("EVENT_BLACKOUT_DAYS", 1),
            entry_window=tuple(os.environ.get("ENTRY_WINDOW", "09:45-15:45").split("-")),
            min_paper_trades=_int("MIN_PAPER_TRADES", 30),
            min_paper_days=_int("MIN_PAPER_DAYS", 20),
            min_profit_factor=_float("MIN_PROFIT_FACTOR", 1.2),
            max_drawdown_pct=_float("MAX_DRAWDOWN_PCT", 0.25),
            force_live=_bool("FORCE_LIVE", False),
            monitor_model=os.environ.get("MONITOR_MODEL", "claude-haiku-5-5"),
            monitor_effort=os.environ.get("MONITOR_EFFORT", "low"),
            monitor_budget_usd=_float("MONITOR_BUDGET_USD", 0.30),
            alert_webhook_url=os.environ.get("ALERT_WEBHOOK_URL", "").strip(),
            model=os.environ.get("AGENT_MODEL", "claude-opus-5-5"),
            effort=os.environ.get("AGENT_EFFORT", "high"),
            max_turns=_int("AGENT_MAX_TURNS", 80),
            max_budget_usd=_float("AGENT_MAX_BUDGET_USD", 3.0),
            robinhood_mcp_url=os.environ.get("ROBINHOOD_MCP_URL", "").strip(),
            robinhood_mcp_token=os.environ.get("ROBINHOOD_MCP_TOKEN", "").strip(),
            state_path=os.environ.get("AGENT_STATE_PATH", "agent_state.json"),
        )
