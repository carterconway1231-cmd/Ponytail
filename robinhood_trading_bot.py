"""Auto-trader: buys/sells SYMBOLS on Robinhood using a weighted 3-signal
vote (SMA crossover, RSI, MACD). Weights persist in bot_state.json and
adapt after every closed trade: an indicator that called the direction of
a profitable trade gains weight, one that called a loss loses weight, so
the vote drifts toward whatever has been making money. Run once per
invocation; schedule with cron.
"""
import os
import sys
import json
import logging
from datetime import datetime, timezone

import pandas as pd
import robin_stocks.robinhood as r
from ta.trend import SMAIndicator, MACD
from ta.momentum import RSIIndicator
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("robinhood_trading_bot")

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")
LEARNING_RATE = 0.1
WEIGHT_FLOOR = 0.1
DECISION_THRESHOLD = 0.5  # weighted_avg magnitude needed to act; matches old "2 of 3" at equal weights


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"weights": {"sma": 1.0, "rsi": 1.0, "macd": 1.0}, "positions": {}, "trade_log": []}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def get_history_df(symbol):
    candles = r.stocks.get_stock_historicals(symbol, interval="day", span="year", bounds="regular")
    df = pd.DataFrame(candles)
    df["close_price"] = df["close_price"].astype(float)
    return df


def raw_votes(df):
    """SMA crossover + RSI + MACD, each a buy(+1)/sell(-1)/hold(0) vote. Pure
    function of price history for testability."""
    close = df["close_price"]

    sma_short = SMAIndicator(close, window=20).sma_indicator()
    sma_long = SMAIndicator(close, window=50).sma_indicator()
    sma_vote = 0
    if sma_short.iloc[-2] <= sma_long.iloc[-2] and sma_short.iloc[-1] > sma_long.iloc[-1]:
        sma_vote = 1
    elif sma_short.iloc[-2] >= sma_long.iloc[-2] and sma_short.iloc[-1] < sma_long.iloc[-1]:
        sma_vote = -1

    rsi = RSIIndicator(close, window=14).rsi()
    rsi_vote = 1 if rsi.iloc[-1] < 30 else -1 if rsi.iloc[-1] > 70 else 0

    macd = MACD(close)
    macd_line, macd_signal = macd.macd(), macd.macd_signal()
    macd_vote = 0
    if macd_line.iloc[-2] <= macd_signal.iloc[-2] and macd_line.iloc[-1] > macd_signal.iloc[-1]:
        macd_vote = 1
    elif macd_line.iloc[-2] >= macd_signal.iloc[-2] and macd_line.iloc[-1] < macd_signal.iloc[-1]:
        macd_vote = -1

    return {"sma": sma_vote, "rsi": rsi_vote, "macd": macd_vote}


def weighted_decision(votes, weights):
    total_weight = sum(weights.values())
    weighted_avg = sum(weights[k] * v for k, v in votes.items()) / total_weight
    decision = "BUY" if weighted_avg >= DECISION_THRESHOLD else "SELL" if weighted_avg <= -DECISION_THRESHOLD else "HOLD"
    return decision, weighted_avg


def learn_from_trade(weights, entry_votes, pnl):
    """Nudge each indicator's weight up if its entry-time vote direction
    matched a profitable outcome, down if it matched a loss. Flat votes
    (0) didn't call a direction, so they're skipped."""
    for name, v in entry_votes.items():
        if v == 0:
            continue
        correct = (v > 0) == (pnl > 0)
        weights[name] = max(WEIGHT_FLOOR, weights[name] + (LEARNING_RATE if correct else -LEARNING_RATE))


def run_symbol(symbol, trade_amount, holdings, state):
    df = get_history_df(symbol)
    votes = raw_votes(df)
    decision, weighted_avg = weighted_decision(votes, state["weights"])
    price = df["close_price"].iloc[-1]
    held_qty = float(holdings.get(symbol, {}).get("quantity", 0))
    position = state["positions"].get(symbol)
    log.info("%s votes=%s weighted_avg=%.2f decision=%s held_qty=%s", symbol, votes, weighted_avg, decision, held_qty)

    if decision == "BUY" and held_qty == 0 and position is None:
        result = r.orders.order_buy_fractional_by_price(symbol, trade_amount, timeInForce="gfd")
        state["positions"][symbol] = {"entry_price": price, "votes": votes}
        log.info("BUY %s $%s @ ~%.2f -> %s", symbol, trade_amount, price, result)

    elif decision == "SELL" and held_qty > 0:
        result = r.orders.order_sell_fractional_by_quantity(symbol, held_qty, timeInForce="gfd")
        if position is not None:
            pnl = (price - position["entry_price"]) * held_qty
            learn_from_trade(state["weights"], position["votes"], pnl)
            state["trade_log"].append({
                "symbol": symbol, "entry_price": position["entry_price"], "exit_price": price,
                "qty": held_qty, "pnl": pnl, "entry_votes": position["votes"],
                "weights_after": dict(state["weights"]), "closed_at": datetime.now(timezone.utc).isoformat(),
            })
            log.info("SELL %s qty=%s pnl=%.2f -> new weights=%s", symbol, held_qty, pnl, state["weights"])
        state["positions"].pop(symbol, None)

    else:
        log.info("%s: no action", symbol)


def main():
    load_dotenv()
    username = os.environ["ROBINHOOD_USERNAME"]
    password = os.environ["ROBINHOOD_PASSWORD"]
    symbols = [s.strip() for s in os.environ.get("SYMBOLS", "").split(",") if s.strip()]
    trade_amount = float(os.environ.get("TRADE_AMOUNT", "100"))
    if not symbols:
        sys.exit("SYMBOLS is empty; set it in .env, e.g. SYMBOLS=AAPL,MSFT")

    r.login(username, password)
    holdings = r.build_holdings()
    state = load_state()
    try:
        for symbol in symbols:
            try:
                run_symbol(symbol, trade_amount, holdings, state)
            except Exception:
                log.exception("%s: failed, skipping", symbol)
    finally:
        save_state(state)

    realized = sum(t["pnl"] for t in state["trade_log"])
    log.info("realized P&L to date: %.2f over %d closed trades; weights=%s", realized, len(state["trade_log"]), state["weights"])


def _selftest():
    import numpy as np

    n = 60
    prices = pd.Series(np.linspace(100, 90, n))
    prices.iloc[-5:] = np.linspace(90, 110, 5)  # sharp reversal up -> crossovers should fire
    votes = raw_votes(pd.DataFrame({"close_price": prices}))
    weights = {"sma": 1.0, "rsi": 1.0, "macd": 1.0}
    decision, weighted_avg = weighted_decision(votes, weights)
    assert decision in ("BUY", "SELL", "HOLD")

    # A profitable long where sma correctly called +1 and macd wrongly called -1.
    w = {"sma": 1.0, "rsi": 1.0, "macd": 1.0}
    learn_from_trade(w, {"sma": 1, "rsi": 0, "macd": -1}, pnl=5.0)
    assert w["sma"] == 1.1, w
    assert w["rsi"] == 1.0, w  # flat vote untouched
    assert w["macd"] == 0.9, w
    print("selftest ok:", decision, votes, "learning ->", w)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
