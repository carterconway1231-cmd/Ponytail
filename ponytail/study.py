"""Edge study: does anything here actually predict, and does it hold up out
of sample? Run it before trusting (or loosening) any setting.

  python -m ponytail.study BARS_DIR [--split YYYY-MM-DD] [--capital N]

1. Factor edge: for every factor, how often its direction matched the next
   SHADOW_HORIZON-day move, on the RAW move (what options pay on) and on the
   move beyond the market's drift (what the learner grades), separately for
   the two halves. Overlapping windows and correlated stocks inflate naive
   t-stats; z* deflates them ~3.9x. Treat |z*| < 2 as noise.
2. Settings: a grid over the entry settings, each scored on trades opened
   before the split only; the best in-sample setting is then reported on the
   unseen second half, together with how many settings held up at all.
"""
import dataclasses
import glob
import itertools
import json
import math
import os
import sys

from . import backtest as bt
from .factors import ALL_FACTORS

GRID = {"min_confluence": (3, 4, 5), "signal_threshold": (0.15, 0.25, 0.35), "min_contract_ev": (-0.10, 0.0, 0.10)}
DEFLATE = math.sqrt(5 * 3)  # overlapping 5-day windows x cross-stock correlation (conservative)


def factor_edge(events, split, horizon):
    def one(evs, excess):
        out = {}
        for f in ALL_FACTORS:
            moves = []
            for e in evs:
                v = e["factors"].get(f)
                if not e["outcome"] or not e["atr"] or not v or abs(v["score"]) < 0.25:
                    continue
                m = (e["outcome"][1] - e["close"] - (e["drift"] * horizon if excess else 0)) / e["atr"]
                moves.append(m if v["score"] > 0 else -m)
            if len(moves) < 30:
                out[f] = None
                continue
            n, mean = len(moves), sum(moves) / len(moves)
            sd = math.sqrt(sum((x - mean) ** 2 for x in moves) / n) or 1.0
            out[f] = {"n": n, "hit": round(sum(x > 0 for x in moves) / n, 3), "avg_atr": round(mean, 3),
                      "z_star": round(mean / (sd / math.sqrt(n)) / DEFLATE, 2)}
        return out

    halves = ([e for e in events if e["day"] < split], [e for e in events if e["day"] >= split])
    return {kind: {"first_half": one(halves[0], kind == "excess"), "second_half": one(halves[1], kind == "excess")}
            for kind in ("raw", "excess")}


def _stats(trades):
    if not trades:
        return {"n": 0, "pnl": 0.0}
    pnl = [t["pnl"] for t in trades]
    w, lo = sum(p for p in pnl if p > 0), -sum(p for p in pnl if p <= 0)
    return {"n": len(trades), "win": round(sum(p > 0 for p in pnl) / len(pnl), 2), "pnl": round(sum(pnl), 2),
            "profit_factor": round(w / lo, 2) if lo else None}


def settings_study(cfg, events, earnings, capital, split, horizon):
    rows = []
    for values in itertools.product(*GRID.values()):
        c = dataclasses.replace(cfg, **dict(zip(GRID, values)))
        trades, _ = bt.simulate(c, events, capital, horizon, earnings=earnings)
        rows.append({"settings": dict(zip(GRID, values)),
                     "in_sample": _stats([t for t in trades if t["opened"] < split]),
                     "out_of_sample": _stats([t for t in trades if t["opened"] >= split])})
    traded_is = [r for r in rows if r["in_sample"]["n"]]
    traded_oos = [r for r in rows if r["out_of_sample"]["n"]]
    eligible = [r for r in rows if r["in_sample"]["n"] >= 15]
    best = max(eligible, key=lambda r: r["in_sample"]["pnl"]) if eligible else None
    return {"profitable_in_sample": f"{sum(r['in_sample']['pnl'] > 0 for r in traded_is)}/{len(traded_is)}",
            "profitable_out_of_sample": f"{sum(r['out_of_sample']['pnl'] > 0 for r in traded_oos)}/{len(traded_oos)}",
            "chosen_on_first_half": best, "grid": rows}


def buy_and_hold(events, sym, a, b):
    ev = [e for e in events if e["sym"] == sym and a <= e["day"] <= b]
    return round((ev[-1]["close"] / ev[0]["close"] - 1) * 100, 1) if len(ev) > 1 else None


def main():
    from dotenv import load_dotenv

    from .config import Config

    load_dotenv()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    opt = lambda name, default: sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default  # noqa: E731
    split, capital = opt("--split", None), float(opt("--capital", "3000"))
    bars = {}
    for path in glob.glob(os.path.join(args[0], "*_*.json")):
        sym, kind = os.path.basename(path)[:-5].rsplit("_", 1)
        with open(path) as f:
            bars.setdefault(sym.upper(), {})[kind] = json.load(f)
    cfg = Config.from_env()
    horizon = cfg.shadow_horizon
    events = bt.precompute(bars, horizon)
    earnings = {s: b["earnings"] for s, b in bars.items() if b.get("earnings")}
    days = sorted({e["day"] for e in events})
    start = days[min(bt.TRADE_AFTER, len(days) - 1)]
    split = split or days[(days.index(start) + len(days)) // 2]
    syms = sorted({e["sym"] for e in events})
    cfg = dataclasses.replace(cfg, symbols=syms)
    out = {"period": f"{start}..{days[-1]}", "split": split, "symbols": syms,
           "spy_buy_and_hold_pct": {"first_half": buy_and_hold(events, "SPY", start, split),
                                    "second_half": buy_and_hold(events, "SPY", split, days[-1])},
           "factor_edge": factor_edge(events, split, horizon),
           "settings": settings_study(cfg, events, earnings, capital, split, horizon)}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
