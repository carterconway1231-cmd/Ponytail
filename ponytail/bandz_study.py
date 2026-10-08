"""Edge study for the Bandz signals on 5-minute bars.

  python -m ponytail.bandz_study DIR   (DIR holds SPY_5minute.json, QQQ_5minute.json)

Every signal is traded the same way a human would read the chart, entered
at the open of the bar after the signal is known, and compared to a
matched null: the identical bracket (same direction, same stop/target
distance as a fraction of price) entered at random bars from other days
at the same time of day. The edge is the paired difference in R per
trade; t > ~2 in BOTH halves of the sample is the bar to clear.
"""
import json
import os
import random
import sys

from . import bandz as bz

COST_PER_SHARE = 0.02  # round-trip slippage + spread on SPY/QQQ shares


def _halves(s, rows):
    days = sorted(set(s.day))
    split = days[len(days) // 2]
    return ([r for r in rows if r["day"] < split], [r for r in rows if r["day"] >= split]), split


def _summ(rows, key="edge"):
    n, m, t = bz.tstat([r[key] for r in rows])
    return {"n": n, "mean": None if m is None else round(m, 3), "t": None if t is None else round(t, 2)}


def _report(s, rows):
    (a, b), split = _halves(s, rows)
    out = {"all": _summ(rows), "first_half": _summ(a), "second_half": _summ(b), "split": str(split)}
    if rows and "r" in rows[0]:
        out["mean_R_gross"] = round(sum(r["r"] for r in rows) / len(rows), 3)
        out["mean_R_net"] = round(sum(r["r_net"] for r in rows) / len(rows), 3)
        out["mean_R_null"] = round(sum(r["null"] for r in rows) / len(rows), 3)
        out["hit_rate"] = round(sum(r["outcome"] == "target" for r in rows) / len(rows), 3)
    return out


def _trade(s, bar, direction, stop, target, rng):
    entry_bar = bar + 1
    if entry_bar >= s.n or s.day[entry_bar] != s.day[bar]:
        return None
    res = bz.bracket(s, entry_bar, direction, stop, target)
    if res is None:
        return None
    e = s.o[entry_bar]
    risk, reward = (e - stop) * direction, (target - e) * direction
    null = bz.matched_null(s, entry_bar, direction, risk / e, reward / e, rng=rng)
    if null is None:
        return None
    r_net = res[0] - COST_PER_SHARE / risk
    return {"day": s.day[bar], "r": res[0], "r_net": r_net, "outcome": res[1], "null": null,
            "edge": r_net - null, "risk_bp": risk / e * 1e4}


def cisd_study(s, rng):
    events = bz.detect_cisd(s)
    out = {"events": len(events)}
    for ratio in (-1.0, -2.0, -2.5, -4.0):
        rows = []
        for ev in events:
            target = bz.level(ev["a0"], ev["a1"], ratio)
            r = _trade(s, ev["signal_bar"], ev["dir"], ev["a1"] - ev["dir"] * bz.TICK, target, rng)
            if r:
                rows.append(r)
        out[f"target_{ratio:g}"] = _report(s, rows)
    return out


def smt_study(a, b, minutes, rng):
    events = bz.detect_smt(a, b, minutes)
    rows, fwd = [], []
    for ev in events:
        i, d = ev["bar"], ev["dir"]
        stop = ev["anchor"] - d * bz.TICK * 2
        e = a.o[i + 1] if i + 1 < a.n else None
        if e is not None and (e - stop) * d > 0:
            r = _trade(a, i, d, stop, e + 2 * (e - stop), rng)
            if r:
                rows.append(r)
        f = bz.forward_return(a, i, d, 12)
        if f is not None:
            fwd.append({"day": a.day[i], "edge": f - d * bz.baseline_return(a, i, 12)})
    return {"events": len(events), "bracket_2R": _report(a, rows), "fwd_12bars_bp": _report(a, fwd)}


def fvg_study(s, rng):
    events = bz.detect_first_fvg(s)
    rows, fwd = [], []
    for ev in events:
        i, d = ev["bar"], ev["dir"]
        stop = (ev["bottom"] if d == 1 else ev["top"]) - d * bz.TICK
        e = s.o[i + 1] if i + 1 < s.n else None
        if e is not None and (e - stop) * d > 0:
            r = _trade(s, i, d, stop, e + 2 * (e - stop), rng)
            if r:
                rows.append(r)
        f = bz.forward_return(s, i, d, 12)
        if f is not None:
            fwd.append({"day": s.day[i], "edge": f - d * bz.baseline_return(s, i, 12)})
    return {"events": len(events), "bracket_2R": _report(s, rows), "fwd_12bars_bp": _report(s, fwd)}


def run(directory):
    rng = random.Random(7)
    series = {sym: bz.Series(json.load(open(os.path.join(directory, f"{sym}_5minute.json")))) for sym in ("SPY", "QQQ")}
    out = {"days": len(set(series["SPY"].day)), "from": str(series["SPY"].day[0]), "to": str(series["SPY"].day[-1])}
    for sym, other in (("SPY", "QQQ"), ("QQQ", "SPY")):
        s = series[sym]
        out[sym] = {"cisd_stdv": cisd_study(s, rng),
                    "smt_15m": smt_study(s, series[other], 15, rng),
                    "smt_1h": smt_study(s, series[other], 60, rng),
                    "first_fvg_4h": fvg_study(s, rng)}
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    print(json.dumps(run(sys.argv[1]), indent=1, default=str))
