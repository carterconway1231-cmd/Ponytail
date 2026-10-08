"""Exit learning: pick take-profit / stop-loss levels from this account's
own trade outcomes instead of fixed guesses.

Each sample is a closed trade expressed in R (return on premium): either a
daily mark path (backtest trades) or just its max favorable / adverse
excursion and final result (live and paper trades). For every (TP, SL)
pair on a grid, replay the samples:
  path:     first day the path crosses +TP or -SL decides the exit
  MFE/MAE:  touched both -> assume the stop hit first (conservative),
            touched one -> that exit, neither -> the actual final result
Stops are charged a little worse than their level for gap/slippage.

The best pair is adopted only with MIN_EXIT_SAMPLES of evidence and only if
it beats the current setting by a margin, and the stop is bounded so
learning can never remove loss protection.
"""
from datetime import date

TP_GRID = (0.25, 0.35, 0.5, 0.75, 1.0)
SL_GRID = (0.2, 0.25, 0.3, 0.35, 0.45, 0.5)
STOP_SLIPPAGE = 1.1
MIN_IMPROVEMENT = 0.02  # R per trade
REAL_TRADE_WEIGHT = 2   # a real trade counts double a simulated one


def simulate(sample, tp, sl):
    path = sample.get("path")
    if path:
        for r in path:
            if r <= -sl:
                return -sl * STOP_SLIPPAGE
            if r >= tp:
                return tp
        return path[-1]
    mfe, mae, final = sample["mfe"], sample["mae"], sample["r"]
    if mae <= -sl:
        return -sl * STOP_SLIPPAGE  # includes the ambiguous both-touched case
    if mfe >= tp:
        return tp
    return final


def mean_r(samples, tp, sl):
    total = weight = 0.0
    for s in samples:
        w = s.get("weight", 1.0)
        total += w * simulate(s, tp, sl)
        weight += w
    return total / weight if weight else 0.0


def samples_from(state):
    out = [{"mfe": t["mfe"], "mae": t["mae"], "r": t["r"], "weight": REAL_TRADE_WEIGHT}
           for t in state.trade_log if t.get("mfe") is not None and t.get("r") is not None
           and t.get("kind", "single") == "single"]
    out += state.data.get("exit_samples", [])
    return out


def tune(cfg, state):
    """Re-fit exit levels; returns the stored params (or None if not enough data)."""
    samples = samples_from(state)
    n = sum(s.get("weight", 1.0) for s in samples)
    current = state.data.get("exit_params") or {"take_profit_pct": cfg.take_profit_pct,
                                                "stop_loss_pct": cfg.stop_loss_pct}
    if n < cfg.min_exit_samples:
        return state.data.get("exit_params")
    baseline = mean_r(samples, current["take_profit_pct"], current["stop_loss_pct"])
    best = max(((mean_r(samples, tp, sl), tp, sl) for tp in TP_GRID for sl in SL_GRID), key=lambda x: x[0])
    if best[0] - baseline >= MIN_IMPROVEMENT:
        current = {"take_profit_pct": best[1], "stop_loss_pct": best[2]}
    state.data["exit_params"] = {**current, "samples": round(n, 1), "mean_r": round(max(best[0], baseline), 3),
                                 "baseline_r": round(baseline, 3), "tuned_on": date.today().isoformat()}
    return state.data["exit_params"]
