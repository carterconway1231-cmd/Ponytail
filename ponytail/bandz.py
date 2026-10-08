"""Python port of the "Bandz All-in-One" TradingView indicator's tradable ideas,
so they can be measured before anything trades on them.

Ported (faithful to the Pine logic, 5-minute chart preset):
  * CISD + STDV projections (layer 0: 1H liquidity, 5m confirmation, 4H
    competition window). A sweep of an unswept 1H high/low, a delivery run of
    >= 2 opposite candles defining the CISD level, a close back through the
    CISD within 8 bars, then a close through the prior swing within 8 more.
    Anchor 0 = that swing, anchor 1 = the sweep extreme; level(r) = A0 + r*(A1 - A0).
  * SMT divergence of one symbol against a correlated one (the Pine compares
    to ES futures; here SPY vs QQQ), period-over-period for 15m and 1H.
  * "1st TF FVG": the first fair value gap of each 4H period on the 5m chart
    (>= 55 ticks), edges tightened by body volume imbalances.

Timing convention (no lookahead): the Pine processes the *previous* bar
(`high[1]`, `close[1]`, ...) on each bar close. A signal produced while
processing bar p is therefore known at the close of bar p+1; anything
traded on it starts at the open of bar p+2.

Pure functions over lists of bars ({begins_at, open_price, ...}).
"""
import math
import random
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
TICK = 0.01

# Layer-0 preset (5m chart) from the Pine source.
NORMAL_LOOKBACK = 12
CANDIDATE_LIFETIME = 24
CISD_LIFETIME = 8
SWING_BREAK_BARS = 8
SWING_STRENGTH = 2
SWING_SEARCH_BARS = 30
DELIVERY_GAP = 1
MIN_DELIVERY = 2
DELIVERY_SCAN = 12
IMPORTANCE = 2.0
RATIOS = (0.0, 1.0, -1.0, -1.5, -2.0, -2.5, -3.5, -4.0)


class Series:
    """Parsed OHLC arrays plus session-aligned period keys."""

    def __init__(self, bars):
        self.t = [datetime.fromisoformat(b["begins_at"].replace("Z", "+00:00")) for b in bars]
        self.o = [float(b["open_price"]) for b in bars]
        self.h = [float(b["high_price"]) for b in bars]
        self.l = [float(b["low_price"]) for b in bars]
        self.c = [float(b["close_price"]) for b in bars]
        self.n = len(bars)
        local = [x.astimezone(ET) for x in self.t]
        self.day = [x.date() for x in local]
        self.mins = [(x.hour * 60 + x.minute) - (9 * 60 + 30) for x in local]  # minutes since 9:30 ET
        self.atr = _atr(self, 14)

    def period(self, i, minutes):
        """Session-aligned bucket (TradingView aligns intraday stock bars to 9:30)."""
        return (self.day[i], self.mins[i] // minutes)

    def index(self):
        return {t: i for i, t in enumerate(self.t)}


def _atr(s, length):
    out, prev = [], None
    for i in range(s.n):
        tr = s.h[i] - s.l[i] if i == 0 else max(s.h[i] - s.l[i], abs(s.h[i] - s.c[i - 1]), abs(s.l[i] - s.c[i - 1]))
        prev = tr if prev is None else (prev * (length - 1) + tr) / length  # Wilder RMA like ta.atr
        out.append(prev if i >= length - 1 else None)
    return out


# ---- CISD / STDV ---------------------------------------------------------------

def _delivery_run(s, cur, base, gap, bear):
    """bearishDeliveryRun (bear=True) / bullishDeliveryRun, offsets from bar `cur`."""
    def at(off, arr):
        k = cur - off
        return arr[k] if 0 <= k < s.n else None

    def is_dir(off):
        o, c = at(off, s.o), at(off, s.c)
        return o is not None and (c < o if bear else c > o)

    def inside(off):
        h1 = at(off + 1, s.h)
        return h1 is not None and at(off, s.h) <= h1 and at(off, s.l) >= at(off + 1, s.l)

    def member(off):
        return is_dir(off) or (inside(off) and (is_dir(off + 1) or (off > 0 and is_dir(off - 1))))

    first = next((base + g for g in range(gap + 1) if at(base + g, s.c) is not None and member(base + g)), None)
    cisd = defining = None
    genuine = 0
    if first is not None:
        for n in range(DELIVERY_SCAN):
            off = first + n
            if at(off, s.c) is None or not member(off):
                break
            if is_dir(off):
                genuine += 1
                body = at(off, s.o)
                if cisd is None or (body > cisd if bear else body < cisd):
                    cisd, defining = body, off
    return cisd, genuine, first, defining


def _pivot(s, cur, off, strength, high):
    arr = s.h if high else s.l
    if off < strength or cur - off - strength < 0 or cur - off + strength >= s.n:
        return False
    v = arr[cur - off]
    for n in range(1, strength + 1):
        a, b = arr[cur - off + n], arr[cur - off - n]
        if high and not (v > a and v >= b):
            return False
        if not high and not (v < a and v <= b):
            return False
    return True


def _prior_swing(s, cur, first_delivery, high):
    if first_delivery is None:
        return None, None
    start = max(first_delivery + 1, SWING_STRENGTH)
    for off in range(start, start + SWING_SEARCH_BARS):
        if cur - off < 0:
            break
        if _pivot(s, cur, off, SWING_STRENGTH, high):
            return (s.h if high else s.l)[cur - off], cur - off
    return None, None


def detect_cisd(s, liquidity_min=60, competition_min=240):
    """Run the Pine detectLayer over the whole series. Returns confirmed events:
    {dir, p (processed bar), signal_bar (=p+1), a0, a1, cisd, ref, score, ...}."""
    events = []
    cands = {1: [], -1: []}
    consumed = {1: set(), -1: set()}
    building = None  # [key, high, low]
    completed = []   # (key, high, low)
    for cur in range(1, s.n):
        p = cur - 1
        key = s.period(p, liquidity_min)
        if building is None:
            building = [key, s.h[p], s.l[p]]
        elif key != building[0]:
            completed.append(tuple(building))
            completed[:] = completed[-128:]
            building = [key, s.h[p], s.l[p]]
        else:
            building[1], building[2] = max(building[1], s.h[p]), min(building[2], s.l[p])

        refs = {1: {}, -1: {}}
        window = completed[-NORMAL_LOOKBACK:]
        for j, (k, hi, lo) in enumerate(window):
            newer = window[j + 1:]
            if all(x[1] < hi for x in newer):
                refs[-1][k] = hi
            if all(x[2] > lo for x in newer):
                refs[1][k] = lo
        for d in (1, -1):
            for cd in cands[d]:
                refs[d].setdefault(cd["ref_key"], cd["ref"])

        comp = s.period(p, competition_min)
        for d in (1, -1):
            bull = d == 1
            cisd, genuine, first, defining = _delivery_run(s, cur, 1, DELIVERY_GAP, bear=bull)
            swing, swing_i = _prior_swing(s, cur, first, high=bull)
            base_ok = (genuine >= MIN_DELIVERY and cisd is not None and defining is not None and swing is not None
                       and (swing > s.l[p] if bull else swing < s.h[p]))
            quality = (max(0.0, 1 - (first - 1) * 0.2) + max(0.0, 1 - abs(genuine - 2) * 0.2)) if base_ok else 0.0
            measure = s.l[p] if bull else s.h[p]
            prior = (s.l[p - 1] if bull else s.h[p - 1]) if p >= 1 else None
            for ref_key, ref in refs[d].items():
                if ref_key in consumed[d]:
                    continue
                thr = ref - TICK if bull else ref + TICK
                violated = measure < thr if bull else measure > thr
                fresh = ref_key < key and violated and prior is not None and (prior >= thr if bull else prior <= thr)
                existing = next((cd for cd in cands[d] if cd["ref_key"] == ref_key), None)
                deepen = existing is not None and violated and (s.l[p] < existing["extreme"] if bull
                                                                else s.h[p] > existing["extreme"])
                if not ((existing is None and fresh) or deepen):
                    continue
                comp_start = comp if existing is None else existing["comp"]
                swing_ok = swing_i is not None and s.period(swing_i, competition_min) >= comp_start
                valid = base_ok and swing_ok
                fields = {"extreme": s.l[p] if bull else s.h[p], "extreme_bar": p,
                          "cisd": cisd if valid else None, "swing": swing if valid else None,
                          "swing_bar": swing_i if valid else None, "stage": 0, "confirm_bar": None,
                          "confirm_close": None, "quality": quality}
                if existing is None:
                    cands[d].append({"ref_key": ref_key, "ref": ref, "start": p, "first_sweep": p, "comp": comp, **fields})
                    cands[d][:] = cands[d][-64:]
                else:
                    existing.update(fields)

        for d in (1, -1):
            bull = d == 1
            keep = []
            for cd in cands[d]:
                expired = p - cd["start"] > CANDIDATE_LIFETIME
                cisd_expired = cd["stage"] == 0 and p - cd["start"] > CISD_LIFETIME
                swing_expired = cd["stage"] == 1 and cd["confirm_bar"] is not None and p - cd["confirm_bar"] > SWING_BREAK_BARS
                if expired or cisd_expired or swing_expired:
                    continue
                if p > cd["extreme_bar"] and cd["cisd"] is not None and cd["swing"] is not None:
                    if cd["stage"] == 0 and (s.c[p] > cd["cisd"] if bull else s.c[p] < cd["cisd"]):
                        cd.update(stage=1, confirm_bar=p, confirm_close=s.c[p])
                    if cd["stage"] == 1 and (s.c[p] > cd["swing"] if bull else s.c[p] < cd["swing"]):
                        rng = (cd["swing"] - cd["extreme"]) * d
                        if rng > 0:
                            norm = s.atr[p] if s.atr[p] else max(TICK, rng)
                            depth = max(0.0, (cd["ref"] - cd["extreme"]) * d) / norm
                            disp = max(0.0, (cd["confirm_close"] - cd["cisd"]) * d) / norm
                            sdisp = max(0.0, ((s.h[p] if bull else s.l[p]) - cd["swing"]) * d) / norm
                            score = (IMPORTANCE * 100 + cd["quality"] * 15 + depth * 12 + disp * 10 + sdisp * 8
                                     + rng / norm * 5)
                            events.append({"dir": d, "p": p, "signal_bar": cur, "a0": cd["swing"],
                                           "a1": cd["extreme"], "cisd": cd["cisd"], "ref": cd["ref"],
                                           "sweep_bar": cd["extreme_bar"], "score": round(score, 1),
                                           "range": rng})
                            consumed[d].add(cd["ref_key"])
                        continue  # removed once the swing breaks
                keep.append(cd)
            cands[d] = keep
    return events


def level(a0, a1, ratio):
    return a0 + ratio * (a1 - a0)


# ---- Brackets, nulls ---------------------------------------------------------

def bracket(s, entry_bar, direction, stop, target):
    """Enter at the open of entry_bar; first touch of stop or target, else the
    session's last close. A bar touching both counts as the stop. Returns
    (R multiple, outcome) or None when there's no same-day entry."""
    if entry_bar >= s.n:
        return None
    day, entry = s.day[entry_bar], s.o[entry_bar]
    risk = (entry - stop) * direction
    if risk <= 0 or (target - entry) * direction <= 0:
        return None
    for i in range(entry_bar, s.n):
        if s.day[i] != day:
            return ((s.c[i - 1] - entry) * direction / risk, "eod")
        hit_stop = s.l[i] <= stop if direction == 1 else s.h[i] >= stop
        hit_tgt = s.h[i] >= target if direction == 1 else s.l[i] <= target
        if hit_stop:
            return (-1.0, "stop")
        if hit_tgt:
            return ((target - entry) * direction / risk, "target")
    return ((s.c[-1] - entry) * direction / risk, "eod")


def matched_null(s, entry_bar, direction, risk_frac, reward_frac, draws=60, rng=None, window=6):
    """Same bracket (as fractions of entry price), same direction, entered at
    random bars from other days within +/- `window` bars of the same time of day."""
    rng = rng or random.Random(entry_bar)
    tod = s.mins[entry_bar]
    pool = [i for i in range(s.n) if abs(s.mins[i] - tod) <= window * 5 and s.day[i] != s.day[entry_bar]]
    out = []
    for i in rng.sample(pool, min(draws, len(pool))):
        e = s.o[i]
        r = bracket(s, i, direction, e - direction * risk_frac * e, e + direction * reward_frac * e)
        if r:
            out.append(r[0])
    return sum(out) / len(out) if out else None


# ---- SMT ---------------------------------------------------------------------

def detect_smt(a, b, minutes):
    """Period-over-period SMT of `a` against `b` (aligned on timestamps).
    Bearish: within the current period one symbol trades above its prior
    period high while the other does not; bullish mirror on lows. One event
    per direction per period, at the first bar it appears (signal known at
    that bar's close). Returns [{dir, bar (index in a), anchor}]."""
    bi = b.index()
    events = []
    prev = cur = None
    for i in range(a.n):
        j = bi.get(a.t[i])
        if j is None:
            continue
        key = a.period(i, minutes)
        if cur is None or key != cur["key"]:
            prev = cur
            cur = {"key": key, "ah": a.h[i], "al": a.l[i], "bh": b.h[j], "bl": b.l[j],
                   "fired": set()}
        else:
            cur["ah"], cur["al"] = max(cur["ah"], a.h[i]), min(cur["al"], a.l[i])
            cur["bh"], cur["bl"] = max(cur["bh"], b.h[j]), min(cur["bl"], b.l[j])
        if prev is None or prev["key"][0] != key[0]:
            continue  # first period of the day: no same-session reference
        a_took_h, b_took_h = cur["ah"] > prev["ah"], cur["bh"] > prev["bh"]
        a_took_l, b_took_l = cur["al"] < prev["al"], cur["bl"] < prev["bl"]
        if a_took_h != b_took_h and -1 not in cur["fired"]:
            cur["fired"].add(-1)
            events.append({"dir": -1, "bar": i, "anchor": max(cur["ah"], prev["ah"])})
        if a_took_l != b_took_l and 1 not in cur["fired"]:
            cur["fired"].add(1)
            events.append({"dir": 1, "bar": i, "anchor": min(cur["al"], prev["al"])})
    return events


# ---- First FVG per period ------------------------------------------------------

def detect_first_fvg(s, parent_min=240, min_ticks=55):
    """First 3-bar fair value gap in each parent period (both later bars
    inside the period), with the Pine's body volume-imbalance edge tightening.
    Known at the close of the third bar. Returns [{dir, bar, top, bottom}]."""
    events, seen = [], set()
    for i in range(2, s.n):
        key = s.period(i, parent_min)
        if key in seen or s.period(i - 1, parent_min) != key:
            continue
        bull = s.l[i] > s.h[i - 2] and (s.l[i] - s.h[i - 2]) / TICK >= min_ticks
        bear = s.h[i] < s.l[i - 2] and (s.l[i - 2] - s.h[i]) / TICK >= min_ticks
        if not (bull or bear):
            continue
        b1t, b1b = max(s.o[i - 2], s.c[i - 2]), min(s.o[i - 2], s.c[i - 2])
        b2t, b2b = max(s.o[i - 1], s.c[i - 1]), min(s.o[i - 1], s.c[i - 1])
        b3t, b3b = max(s.o[i], s.c[i]), min(s.o[i], s.c[i])
        if bull:
            top, bottom = s.l[i], s.h[i - 2]
            if b3b + TICK >= b2t:
                top = b3b
            if b2b + TICK >= b1t:
                bottom = b1t
        else:
            top, bottom = s.l[i - 2], s.h[i]
            if b2t - TICK <= b1b:
                top = b1b
            if b3t - TICK <= b2b:
                bottom = b3t
        seen.add(key)
        events.append({"dir": 1 if bull else -1, "bar": i, "top": top, "bottom": bottom})
    return events


def forward_return(s, bar, direction, horizon):
    """Signed return from the next bar's open to the close `horizon` bars later
    (same day only), in basis points."""
    e = bar + 1
    if e >= s.n or s.day[e] != s.day[bar]:
        return None
    x = min(e + horizon - 1, s.n - 1)
    while s.day[x] != s.day[e]:
        x -= 1
    return (s.c[x] - s.o[e]) / s.o[e] * direction * 1e4


def baseline_return(s, bar, horizon, window=6):
    """Mean unsigned-direction drift for the same time of day on other days (bp)."""
    tod = s.mins[bar]
    vals = [forward_return(s, i, 1, horizon) for i in range(s.n)
            if abs(s.mins[i] - tod) <= window * 5 and s.day[i] != s.day[bar]]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def tstat(xs):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 3:
        return n, None, None
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return n, m, (m / (sd / math.sqrt(n)) if sd else None)
