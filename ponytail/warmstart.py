"""Warm start: pre-train the learner on history before (and alongside) live
trading, so factor weights start from evidence instead of a blank 50%.

Walk-forward replay, chronological across all symbols:
  for each trading day t: compute every factor from bars up to and
  including t only (no lookahead), then grade it against the underlying's
  move over the next SHADOW_HORIZON days, exactly like live untraded-signal
  learning. Days without hourly coverage still grade the daily factors.

Replay evidence counts WARM_START_WEIGHT per sample and decays with the same
half-life as live evidence, so older history matters less. It is
incremental: each symbol remembers the last day replayed, so rerunning only
adds new days and never double-counts.

Offline use, with bars saved as JSON lists of Robinhood bar objects:
  python -m ponytail.warmstart BARS_DIR      # files: SPY_day.json, SPY_hour.json, ...
"""
import glob
import json
import os
import sys
from datetime import date

from .factors import MIN_DAILY_BARS, MIN_HOURLY_BARS, compute_factors

DAILY_WINDOW = 300   # daily bars handed to the factor engine per replay day
HOURLY_WINDOW = 210  # ~30 sessions of hourly bars, as in live runs
MIN_HISTORY = 120    # first replay day needs this many daily bars behind it
CONTEXT_SYMBOLS = ("SPY", "VIX")
INDEX_SYMBOLS = {"VIX", "SPX", "NDX", "DJI", "RUT"}


def _real(bars):
    """Drop gap-fill bars. Index bars (VIX) saved straight from get_index_historicals
    use *_value fields; keep them too (factors.bars_to_df reads both)."""
    return [b for b in bars or [] if not b.get("interpolated") and (b.get("close_price") or b.get("close_value"))]


def replay(learner, bars_by_symbol, horizon=None, weight=None):
    """bars_by_symbol: {symbol: {"day": [...], "hour": [...]}}. Returns a summary."""
    cfg = learner.cfg
    horizon = horizon or cfg.shadow_horizon
    weight = cfg.warm_start_weight if weight is None else weight
    done = learner.d["warm_start"]["symbols"]

    events = []
    series = {}
    context = {k: _real((bars_by_symbol.get(k) or {}).get("day")) for k in CONTEXT_SYMBOLS}
    for sym, bars in bars_by_symbol.items():
        if sym in INDEX_SYMBOLS:
            continue  # context only (VIX has no volume and isn't tradable)
        daily = _real(bars.get("day"))
        hourly = _real(bars.get("hour"))
        if len(daily) < MIN_HISTORY + horizon:
            continue
        days = [b["begins_at"][:10] for b in daily]
        hour_days = [b["begins_at"][:10] for b in hourly]
        series[sym] = (daily, hourly, days, hour_days)
        last_done = done.get(sym, "")
        for t in range(MIN_HISTORY - 1, len(daily) - horizon):
            if days[t] > last_done:
                events.append((days[t], sym, t))
    events.sort()

    raw = {}  # undecayed tallies for the report: factor -> regime -> [right, wrong]
    graded = skipped_flat = 0
    hourly_days = 0
    for day, sym, t in events:
        daily, hourly, days, hour_days = series[sym]
        dslice = daily[max(0, t + 1 - DAILY_WINDOW):t + 1]
        # Hourly bars through the close of day t only; need coverage of day t itself.
        k = _upper(hour_days, day)
        hslice = hourly[max(0, k - HOURLY_WINDOW):k] if k and hour_days[k - 1] == day else None
        if hslice is not None and len(hslice) < MIN_HOURLY_BARS:
            hslice = None
        if len(dslice) < MIN_DAILY_BARS:
            continue
        analysis = compute_factors(dslice, hslice, context)
        hourly_days += bool(analysis["has_hourly"])
        outcome_day = days[t + horizon]
        outcome_close = float(daily[t + horizon]["close_price"])
        regime = analysis["regime"]
        decision = learner.decide(analysis["factors"], regime)
        if learner.grade_outcome(analysis["factors"], regime, decision["decision"], decision["conviction"],
                                 analysis["close"], outcome_close, analysis["atr"], analysis["drift_per_day"],
                                 weight, outcome_day):
            excess = outcome_close - analysis["close"] - analysis["drift_per_day"] * horizon
            if abs(excess) >= 0.5 * analysis["atr"]:
                for name, f in analysis["factors"].items():
                    if abs(f["score"]) >= 0.1:
                        tally = raw.setdefault(name, {}).setdefault(regime, [0, 0])
                        tally[0 if (f["score"] > 0) == (excess > 0) else 1] += 1
            graded += 1
        else:
            skipped_flat += 1  # no meaningful move beyond the trend: nothing to learn
        done[sym] = max(done.get(sym, ""), day)

    learner.d["warm_start"]["samples"] += graded
    learner.d["warm_start"]["at"] = date.today().isoformat()
    return {
        "days_replayed": len(events), "graded": graded, "flat_skipped": skipped_flat,
        "days_with_hourly_factors": hourly_days,
        "symbols": {s: done.get(s) for s in series},
        "historical_hit_rates": {
            name: {rg: {"right": r, "wrong": w, "hit_rate": round(r / (r + w), 3)} for rg, (r, w) in regs.items()}
            for name, regs in sorted(raw.items())},
    }


def _upper(sorted_days, day):
    """Index just past the last entry <= day (bisect_right on ISO dates)."""
    lo, hi = 0, len(sorted_days)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_days[mid] <= day:
            lo = mid + 1
        else:
            hi = mid
    return lo


def main():
    from dotenv import load_dotenv

    from .config import Config
    from .learner import Learner
    from .state import State

    load_dotenv()
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    cfg = Config.from_env()
    bars = {}
    for path in glob.glob(os.path.join(sys.argv[1], "*_*.json")):
        sym, interval = os.path.basename(path)[:-5].rsplit("_", 1)
        with open(path) as f:
            bars.setdefault(sym.upper(), {})[interval] = json.load(f)
    state = State.load(cfg.state_path)
    learner = state.learner = Learner(state.data["learner"], cfg)
    summary = replay(learner, bars)
    state.save()
    print(json.dumps({**summary, "learned_weights": learner.report()["factors"]}, indent=1))


if __name__ == "__main__":
    main()
