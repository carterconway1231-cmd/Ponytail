"""Weighted SMA/RSI/MACD vote with weights that adapt to realized P&L.

Each indicator casts buy(+1)/sell(-1)/hold(0). The weighted average decides
BUY/SELL/HOLD. After a position closes, indicators whose entry-time vote
called the trade's direction correctly gain weight, wrong ones lose it, so
the ensemble drifts toward whatever has been making money.

For options the direction maps to the contract: BUY -> long call,
SELL -> long put. A short-premium strategy is never derived from a signal.
"""
import pandas as pd

LEARNING_RATE = 0.1
WEIGHT_FLOOR = 0.1
DECISION_THRESHOLD = 0.5  # |weighted_avg| needed to act; "2 of 3" at equal weights
MIN_BARS = 60  # SMA50 + a crossover lookback, with margin

DEFAULT_WEIGHTS = {"sma": 1.0, "rsi": 1.0, "macd": 1.0}


def _crossover_vote(fast, slow):
    if fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]:
        return 1
    if fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]:
        return -1
    return 0


def rsi(close, window=14):
    """Wilder's RSI (same smoothing as ta.momentum.RSIIndicator)."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    rs = gain / loss
    return 100 - 100 / (1 + rs)


def raw_votes(closes):
    """Pure function of a daily close series (oldest first)."""
    close = pd.Series(closes, dtype=float).reset_index(drop=True)
    if len(close) < MIN_BARS:
        raise ValueError(f"need at least {MIN_BARS} daily closes, got {len(close)}")

    sma_vote = _crossover_vote(close.rolling(20).mean(), close.rolling(50).mean())

    last_rsi = rsi(close).iloc[-1]
    rsi_vote = 1 if last_rsi < 30 else -1 if last_rsi > 70 else 0

    macd_line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_vote = _crossover_vote(macd_line, macd_signal)

    return {"sma": sma_vote, "rsi": rsi_vote, "macd": macd_vote}, float(last_rsi)


def weighted_decision(votes, weights):
    total_weight = sum(weights.values())
    weighted_avg = sum(weights[k] * v for k, v in votes.items()) / total_weight
    if weighted_avg >= DECISION_THRESHOLD:
        decision = "BUY"
    elif weighted_avg <= -DECISION_THRESHOLD:
        decision = "SELL"
    else:
        decision = "HOLD"
    return decision, weighted_avg


def learn_from_trade(weights, entry_votes, direction, pnl):
    """Nudge weights by whether each indicator's vote agreed with a trade
    that made (pnl > 0) or lost money. `direction` is +1 for a long call
    (bullish bet) and -1 for a long put (bearish bet); an indicator that
    voted with the bet is credited/blamed for its outcome, one that voted
    against it gets the opposite. Flat votes didn't call a direction."""
    for name, v in entry_votes.items():
        if v == 0 or pnl == 0:
            continue
        agreed_with_bet = (v > 0) == (direction > 0)
        correct = agreed_with_bet == (pnl > 0)
        weights[name] = max(WEIGHT_FLOOR, weights[name] + (LEARNING_RATE if correct else -LEARNING_RATE))
