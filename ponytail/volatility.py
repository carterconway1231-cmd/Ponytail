"""Volatility awareness: is option premium cheap or expensive right now?

Buying options when implied volatility is rich is the classic way to be
right on direction and still lose (vol crush, fast theta). Two gauges:

  * IV rank: today's at-the-money IV vs its own range over the stored
    history (needs IV_RANK_MIN_DAYS of daily observations, which accrue as
    the agent quotes contracts each day).
  * IV / realized vol: available from day one; >1.4 means the market is
    charging well above the volatility the stock actually delivers.

Regime -> structure: cheap/normal -> long call/put; expensive -> debit
vertical spread (the short leg sells back some of the rich premium and
cuts vega/theta), or skip if spreads aren't allowed.
"""
import math
import statistics
from datetime import date

IV_RANK_MIN_DAYS = 20
HISTORY_DAYS = 300


def realized_vol(daily_bars, n=20):
    closes = [float(b["close_price"]) for b in daily_bars[-(n + 1):]]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    return statistics.pstdev(rets) * math.sqrt(252) if len(rets) >= 5 else None


def atm_iv(symbol, market, today):
    """Median IV of near-the-money contracts (|delta| 0.35-0.65, 7-90 DTE)
    quoted this run for `symbol`, or None."""
    ivs = []
    for oid, q in market.quotes.items():
        inst = market.instruments.get(oid)
        if not inst or inst["symbol"] != symbol or not q.get("iv") or q.get("delta") is None:
            continue
        dte = (date.fromisoformat(inst["expiration"]) - today).days
        if 0.35 <= abs(q["delta"]) <= 0.65 and 7 <= dte <= 90:
            ivs.append(q["iv"])
    return statistics.median(ivs) if ivs else None


class VolBook:
    def __init__(self, data, cfg):
        self.cfg = cfg
        self.d = data  # {symbol: {date: iv}}

    def record(self, symbol, day, iv):
        hist = self.d.setdefault(symbol, {})
        hist[day] = round(iv, 4)
        for old in sorted(hist)[:-HISTORY_DAYS]:
            del hist[old]

    def iv_rank(self, symbol, iv):
        hist = list((self.d.get(symbol) or {}).values())
        if len(hist) < IV_RANK_MIN_DAYS:
            return None
        lo, hi = min(hist + [iv]), max(hist + [iv])
        return 100 * (iv - lo) / (hi - lo) if hi > lo else 50.0

    def assess(self, symbol, iv, rv):
        """Return the vol regime and the structure it calls for."""
        if iv is None:
            return {"regime": "unknown", "structure": "long",
                    "note": "no near-the-money IV quoted yet; quote ATM contracts to assess"}
        rank = self.iv_rank(symbol, iv)
        ratio = iv / rv if rv else None
        cfg = self.cfg
        if (rank is not None and rank >= cfg.iv_rank_expensive) or (rank is None and ratio and ratio >= cfg.iv_rv_expensive):
            regime = "expensive"
        elif (rank is not None and rank <= cfg.iv_rank_cheap) or (rank is None and ratio and ratio <= 1.1):
            regime = "cheap"
        else:
            regime = "normal"
        structure = "long" if regime != "expensive" else ("debit_spread" if cfg.allow_spreads else "skip")
        return {"regime": regime, "structure": structure, "atm_iv": round(iv, 3),
                "realized_vol_20d": None if rv is None else round(rv, 3),
                "iv_rv_ratio": None if ratio is None else round(ratio, 2),
                "iv_rank": None if rank is None else round(rank, 1),
                "iv_history_days": len(self.d.get(symbol) or {})}
