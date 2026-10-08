"""Factor engine: every factor scores the underlying from -1 (bearish) to +1
(bullish) and says why in one line. Pure functions of OHLCV bars.

Daily bars set the higher-timeframe bias; hourly bars drive the ICT and
order-flow reads used for entry timing.

  Classic       trend (EMA 20/50/200), macd, rsi, adx, volume_thrust
  Order flow*   order_flow (CVD from close location in range), vwap
  ICT           structure (BOS/CHoCH), liquidity_sweep, fvg, order_block,
                ote (62-79% retracement), premium_discount

* Robinhood exposes bars, not the tape, so order flow is estimated from where
  each bar closes within its range, weighted by volume. It is a proxy for
  aggressor flow, not a footprint.

None of these is assumed to work. learner.py measures each factor's hit rate
on this account's own trades and signals and weights it accordingly.
"""
import numpy as np
import pandas as pd

MIN_DAILY_BARS = 60
MIN_HOURLY_BARS = 40
SWING_N = 2  # pivot needs this many bars on each side


def bars_to_df(bars):
    df = pd.DataFrame(bars)
    df = df.rename(columns={"open_price": "o", "high_price": "h", "low_price": "l", "close_price": "c", "volume": "v"})
    for col in "ohlcv":
        df[col] = df[col].astype(float)
    return df.reset_index(drop=True)


def _clip(x, lo=-1.0, hi=1.0):
    return float(max(lo, min(hi, x)))


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr(df, n=14):
    prev = df.c.shift()
    tr = pd.concat([df.h - df.l, (df.h - prev).abs(), (df.l - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


def adx(df, n=14):
    up, down = df.h.diff(), -df.l.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    a = atr(df, n)
    plus_di = 100 * pd.Series(plus_dm).ewm(alpha=1 / n, adjust=False).mean() / a
    minus_di = 100 * pd.Series(minus_dm).ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean(), plus_di, minus_di


def swings(df, n=SWING_N):
    """Pivot highs/lows as (index, price). A pivot at i is only known at i + n."""
    highs, lows = [], []
    h, lo = df.h.values, df.l.values
    for i in range(n, len(df) - n):
        if h[i] >= h[i - n:i].max() and h[i] > h[i + 1:i + n + 1].max():
            highs.append((i, h[i]))
        if lo[i] <= lo[i - n:i].min() and lo[i] < lo[i + 1:i + n + 1].min():
            lows.append((i, lo[i]))
    return highs, lows


# ---- daily (higher timeframe) ----------------------------------------------

def f_trend(d):
    c = d.c
    parts = [0.4 if c.iloc[-1] > ema(c, 50).iloc[-1] else -0.4,
             0.3 if ema(c, 20).iloc[-1] > ema(c, 50).iloc[-1] else -0.3]
    note = "EMA20/50"
    if len(c) >= 200:
        parts.append(0.3 if c.iloc[-1] > ema(c, 200).iloc[-1] else -0.3)
        note += "/200"
    s = sum(parts) / (1.0 if len(parts) == 3 else 0.7)
    return _clip(s), f"price/{note} alignment {s:+.2f}"


def f_macd(d):
    line = ema(d.c, 12) - ema(d.c, 26)
    hist = line - ema(line, 9)
    a = atr(d).iloc[-1]
    s = _clip(hist.iloc[-1] / (0.15 * a)) if a else 0.0
    crossed = np.sign(hist.iloc[-1]) != np.sign(hist.iloc[-4]) if len(hist) > 4 else False
    if crossed:
        s = float(np.sign(hist.iloc[-1]))
    return s, f"histogram {hist.iloc[-1]:+.2f}{' (fresh cross)' if crossed else ''}"


def f_rsi(d):
    r = rsi(d.c).iloc[-1]
    s = 1.0 if r < 30 else 0.4 if r < 40 else -1.0 if r > 70 else -0.4 if r > 60 else 0.0
    return s, f"RSI14 {r:.0f} (mean-reversion read)"


def f_adx(d):
    a, pdi, mdi = adx(d)
    val = a.iloc[-1]
    if np.isnan(val) or val < 20:
        return 0.0, f"ADX {val:.0f}: no trend"
    s = float(np.sign(pdi.iloc[-1] - mdi.iloc[-1])) * min(1.0, val / 40)
    return s, f"ADX {val:.0f}, {'+DI' if s > 0 else '-DI'} leads"


def f_volume_thrust(d):
    rvol = d.v.iloc[-1] / d.v.iloc[-21:-1].mean()
    if rvol < 1.5:
        return 0.0, f"relative volume {rvol:.1f}x: no thrust"
    s = float(np.sign(d.c.iloc[-1] - d.o.iloc[-1])) * min(1.0, (rvol - 1) / 1.5)
    return s, f"{rvol:.1f}x volume on a {'up' if s > 0 else 'down'} day"


def f_premium_discount(d, lookback=20):
    hi, lo = d.h.iloc[-lookback:].max(), d.l.iloc[-lookback:].min()
    if hi <= lo:
        return 0.0, "flat range"
    pos = (d.c.iloc[-1] - lo) / (hi - lo)
    s = _clip((0.5 - pos) * 2)
    return s, f"{pos:.0%} of {lookback}-day dealing range ({'discount' if pos < 0.5 else 'premium'})"


# ---- hourly (entry timeframe): order flow --------------------------------------

def f_order_flow(hdf, window=20):
    w = hdf.iloc[-window:]
    rng = (w.h - w.l).replace(0, np.nan)
    delta = (w.v * ((w.c - w.l) - (w.h - w.c)) / rng).fillna(0)
    total = w.v.sum()
    if not total:
        return 0.0, "no volume"
    s = _clip(2 * delta.sum() / total)
    # Divergence: price pushes to a new extreme while cumulative delta fades.
    half = window // 2
    cvd = delta.cumsum()
    note = ""
    if w.h.iloc[half:].max() > w.h.iloc[:half].max() and cvd.iloc[-1] < cvd.iloc[half - 1]:
        s, note = _clip(s - 0.5), ", bearish CVD divergence"
    elif w.l.iloc[half:].min() < w.l.iloc[:half].min() and cvd.iloc[-1] > cvd.iloc[half - 1]:
        s, note = _clip(s + 0.5), ", bullish CVD divergence"
    return s, f"estimated net buying {delta.sum() / total:+.0%} of volume over {window}h{note}"


def f_vwap(hdf, window=35):
    w = hdf.iloc[-window:]
    tp = (w.h + w.l + w.c) / 3
    vwap = (tp * w.v).sum() / w.v.sum()
    a = atr(hdf).iloc[-1]
    dist = (w.c.iloc[-1] - vwap) / a if a else 0.0
    return _clip(dist), f"price {'above' if dist > 0 else 'below'} 5-session VWAP {vwap:.2f} by {dist:+.1f} ATR"


# ---- hourly: ICT ------------------------------------------------------------------

def market_structure(hdf):
    """Sequence of structure breaks: (bar index, +1 close above swing high / -1 below swing low)."""
    highs, lows = swings(hdf)
    by_confirm_h = {i + SWING_N: p for i, p in highs}
    by_confirm_l = {i + SWING_N: p for i, p in lows}
    last_hi = last_lo = None
    events = []
    for i, c in enumerate(hdf.c.values):
        if i in by_confirm_h:
            last_hi = by_confirm_h[i]
        if i in by_confirm_l:
            last_lo = by_confirm_l[i]
        if last_hi is not None and c > last_hi:
            events.append((i, 1))
            last_hi = None
        if last_lo is not None and c < last_lo:
            events.append((i, -1))
            last_lo = None
    return events


def f_structure(hdf):
    events = market_structure(hdf)
    if not events:
        return 0.0, "no structure break"
    i, direction = events[-1]
    choch = len(events) > 1 and events[-2][1] != direction
    age = len(hdf) - 1 - i
    s = direction * (0.7 if choch else 1.0) * (0.5 if age > 20 else 1.0)
    kind = "CHoCH (shift)" if choch else "BOS (continuation)"
    return float(s), f"{'bullish' if direction > 0 else 'bearish'} {kind} {age}h ago"


def f_liquidity_sweep(hdf, recent=5):
    highs, lows = swings(hdf)
    n = len(hdf)
    for k in range(n - 1, max(n - 1 - recent, 0), -1):
        prior_lows = [p for i, p in lows if i + SWING_N < k and i >= k - 40]
        prior_highs = [p for i, p in highs if i + SWING_N < k and i >= k - 40]
        bar = hdf.iloc[k]
        if prior_lows and bar.l < prior_lows[-1] < bar.c:
            return 1.0 * (1 - (n - 1 - k) / (recent + 1)), f"sell-side liquidity swept below {prior_lows[-1]:.2f} and reclaimed"
        if prior_highs and bar.h > prior_highs[-1] > bar.c:
            return -1.0 * (1 - (n - 1 - k) / (recent + 1)), f"buy-side liquidity swept above {prior_highs[-1]:.2f} and rejected"
    return 0.0, f"no sweep in last {recent}h"


def f_fvg(hdf, lookback=30):
    a = atr(hdf).iloc[-1]
    h, lo, c = hdf.h.values, hdf.l.values, hdf.c.values
    n, price = len(hdf), c[-1]
    for i in range(n - 1, max(n - lookback, 2), -1):
        if lo[i] > h[i - 2]:  # bullish gap [h[i-2], lo[i]]
            bottom, top = h[i - 2], lo[i]
            if lo[i + 1:].min(initial=np.inf) <= bottom:
                continue  # fully filled: no longer support
            if price <= top + 0.5 * a:
                return 1.0, f"retesting bullish FVG {bottom:.2f}-{top:.2f}"
            return 0.4, f"unfilled bullish FVG {bottom:.2f}-{top:.2f} below price"
        if h[i] < lo[i - 2]:  # bearish gap [h[i], lo[i-2]]
            bottom, top = h[i], lo[i - 2]
            if h[i + 1:].max(initial=-np.inf) >= top:
                continue
            if price >= bottom - 0.5 * a:
                return -1.0, f"retesting bearish FVG {bottom:.2f}-{top:.2f}"
            return -0.4, f"unfilled bearish FVG {bottom:.2f}-{top:.2f} above price"
    return 0.0, "no open fair value gap"


def f_order_block(hdf, lookback=40):
    a = atr(hdf)
    n = len(hdf)
    price = hdf.c.iloc[-1]
    for i in range(n - 1, max(n - lookback, 3), -1):
        bar = hdf.iloc[i]
        rng = bar.h - bar.l
        if rng < 1.5 * a.iloc[i] or abs(bar.c - bar.o) < 0.6 * rng:
            continue  # not a displacement candle
        bull = bar.c > bar.o
        for j in range(i - 1, max(i - 4, -1), -1):  # last opposite candle before the move
            ob = hdf.iloc[j]
            if (ob.c < ob.o) if bull else (ob.c > ob.o):
                after = hdf.iloc[i + 1:]
                if bull:
                    if (after.c < ob.l).any():
                        break  # mitigated through
                    near = ob.l <= price <= ob.h + 0.25 * a.iloc[-1]
                    return (1.0 if near else 0.3), f"bullish order block {ob.l:.2f}-{ob.h:.2f}{' being retested' if near else ''}"
                if (after.c > ob.h).any():
                    break
                near = ob.l - 0.25 * a.iloc[-1] <= price <= ob.h
                return (-1.0 if near else -0.3), f"bearish order block {ob.l:.2f}-{ob.h:.2f}{' being retested' if near else ''}"
        break  # only the most recent displacement counts
    return 0.0, "no valid order block"


def f_ote(hdf):
    """ICT optimal trade entry: price back in the 62-79% retracement of the
    latest impulse leg, in the direction of current structure."""
    events = market_structure(hdf)
    if not events:
        return 0.0, "no structure bias for OTE"
    bias = events[-1][1]
    highs, lows = swings(hdf)
    a, price = atr(hdf).iloc[-1], hdf.c.iloc[-1]
    if bias > 0 and highs and lows:
        hi_i, hi = highs[-1]
        prior = [(i, p) for i, p in lows if i < hi_i]
        if not prior:
            return 0.0, "no leg"
        lo = min(p for _, p in prior[-3:])
        leg = hi - lo
        r = (hi - price) / leg if leg > 0 else 0
    elif bias < 0 and highs and lows:
        lo_i, lo = lows[-1]
        prior = [(i, p) for i, p in highs if i < lo_i]
        if not prior:
            return 0.0, "no leg"
        hi = max(p for _, p in prior[-3:])
        leg = hi - lo
        r = (price - lo) / leg if leg > 0 else 0
    else:
        return 0.0, "no leg"
    if leg < a:
        return 0.0, "impulse leg too small for OTE"
    if 0.618 <= r <= 0.79:
        s, zone = 1.0, "in OTE zone"
    elif 0.5 <= r < 0.618:
        s, zone = 0.5, "in discount, above OTE" if bias > 0 else "in premium, below OTE"
    elif r > 1.0:
        s, zone = 0.0, "leg invalidated"
    else:
        s, zone = 0.0, "not retraced enough"
    return (bias * s) or 0.0, f"{r:.0%} retracement of {'bullish' if bias > 0 else 'bearish'} leg {lo:.2f}-{hi:.2f} ({zone})"


DAILY_FACTORS = {"trend": f_trend, "macd": f_macd, "rsi": f_rsi, "adx": f_adx,
                 "volume_thrust": f_volume_thrust, "premium_discount": f_premium_discount}
HOURLY_FACTORS = {"order_flow": f_order_flow, "vwap": f_vwap, "structure": f_structure,
                  "liquidity_sweep": f_liquidity_sweep, "fvg": f_fvg, "order_block": f_order_block, "ote": f_ote}
ALL_FACTORS = list(DAILY_FACTORS) + list(HOURLY_FACTORS)


def compute_factors(daily_bars, hourly_bars=None):
    d = bars_to_df(daily_bars)
    if len(d) < MIN_DAILY_BARS:
        raise ValueError(f"need at least {MIN_DAILY_BARS} daily bars, got {len(d)}")
    out = {}
    for name, fn in DAILY_FACTORS.items():
        s, why = fn(d)
        out[name] = {"score": round(s, 3), "why": why, "tf": "1D"}
    if hourly_bars and len(hourly_bars) >= MIN_HOURLY_BARS:
        h = bars_to_df(hourly_bars)
        for name, fn in HOURLY_FACTORS.items():
            s, why = fn(h)
            out[name] = {"score": round(s, 3), "why": why, "tf": "1h"}
    a_val = float(adx(d)[0].iloc[-1])
    return {
        "factors": out,
        "regime": "trend" if a_val >= 25 else "range",
        "adx": round(a_val, 1),
        "atr": float(atr(d).iloc[-1]),
        "close": float(d.c.iloc[-1]),
        "has_hourly": bool(hourly_bars and len(hourly_bars) >= MIN_HOURLY_BARS),
    }
