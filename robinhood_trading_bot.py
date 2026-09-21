"""Auto-trader: buys/sells SYMBOLS on Robinhood using a 3-vote signal
(SMA crossover, RSI, MACD). Run once per invocation; schedule with cron.
"""
import os
import sys
import logging

import pandas as pd
import robin_stocks.robinhood as r
from ta.trend import SMAIndicator, MACD
from ta.momentum import RSIIndicator
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("robinhood_trading_bot")


def get_history_df(symbol):
    candles = r.stocks.get_stock_historicals(symbol, interval="day", span="year", bounds="regular")
    df = pd.DataFrame(candles)
    df["close_price"] = df["close_price"].astype(float)
    return df


def vote(df):
    """Combine SMA crossover + RSI + MACD into one buy(+1)/sell(-1)/hold(0) vote per indicator.
    Needs >=2 of 3 to agree to signal; else hold. Pure function of price history for testability.
    """
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

    votes = {"sma": sma_vote, "rsi": rsi_vote, "macd": macd_vote}
    total = sum(votes.values())
    decision = "BUY" if total >= 2 else "SELL" if total <= -2 else "HOLD"
    return decision, votes


def run_symbol(symbol, trade_amount, holdings):
    df = get_history_df(symbol)
    decision, votes = vote(df)
    held_qty = float(holdings.get(symbol, {}).get("quantity", 0))
    log.info("%s votes=%s decision=%s held_qty=%s", symbol, votes, decision, held_qty)

    if decision == "BUY" and held_qty == 0:
        result = r.orders.order_buy_fractional_by_price(symbol, trade_amount, timeInForce="gfd")
        log.info("BUY %s $%s -> %s", symbol, trade_amount, result)
    elif decision == "SELL" and held_qty > 0:
        result = r.orders.order_sell_fractional_by_quantity(symbol, held_qty, timeInForce="gfd")
        log.info("SELL %s qty=%s -> %s", symbol, held_qty, result)
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
    for symbol in symbols:
        try:
            run_symbol(symbol, trade_amount, holdings)
        except Exception:
            log.exception("%s: failed, skipping", symbol)


def _selftest():
    import numpy as np
    n = 60
    prices = pd.Series(np.linspace(100, 90, n))
    prices.iloc[-5:] = np.linspace(90, 110, 5)  # sharp reversal up -> crossovers should fire
    decision, votes = vote(pd.DataFrame({"close_price": prices}))
    assert votes["rsi"] in (-1, 0, 1)
    assert decision in ("BUY", "SELL", "HOLD")
    print("selftest ok:", decision, votes)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
