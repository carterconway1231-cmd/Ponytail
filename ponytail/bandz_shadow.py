"""Forward test of the Bandz signals: log every signal on a completed trading
day with its bracket outcome, without trading it.

  python -m ponytail.bandz_shadow RESPONSE.json [--day YYYY-MM-DD] [--log paper/bandz_shadow.json]

RESPONSE.json is a get_equity_historicals response (or the file Claude Code
saved it to) for SPY and QQQ at interval 5minute, covering the target day
plus ~3 prior days of warm-up. The target day defaults to the last complete
day in the data. Entries are keyed, so re-running a day is idempotent.

These signals showed no edge in the historical study (README "Bandz
indicator study"); this log is the out-of-sample check, and nothing here
places orders.
"""
import json
import os
import sys

from . import bandz as bz
from .market import decode_tool_response

LOG = "paper/bandz_shadow.json"


def _series(payload):
    data = decode_tool_response(payload) or {}
    out = {}
    for r in (data.get("data") or {}).get("results", []):
        bars = [b for b in r.get("bars", []) if not b.get("interpolated")]
        if bars:
            out[r["symbol"].upper()] = bz.Series(bars)
    return out


def signals_for_day(series, day):
    rows = []

    def add(sym, kind, s, bar, d, stop, target, extra=None):
        res = bz.bracket(s, bar + 1, d, stop, target) if bar + 1 < s.n and s.day[bar + 1] == day else None
        fwd = bz.forward_return(s, bar, d, 12)
        rows.append({"key": f"{sym}|{kind}|{s.t[bar].isoformat()}", "day": str(day), "symbol": sym, "signal": kind,
                     "dir": d, "time_et": s.t[bar].astimezone(bz.ET).strftime("%H:%M"),
                     "r": None if res is None else round(res[0], 3), "outcome": None if res is None else res[1],
                     "fwd_12bars_bp": None if fwd is None else round(fwd, 2), **(extra or {})})

    for sym, other in (("SPY", "QQQ"), ("QQQ", "SPY")):
        s = series.get(sym)
        if s is None:
            continue
        for ev in bz.detect_cisd(s):
            if s.day[ev["signal_bar"]] == day:
                add(sym, "cisd_-2", s, ev["signal_bar"], ev["dir"], ev["a1"] - ev["dir"] * bz.TICK,
                    bz.level(ev["a0"], ev["a1"], -2.0), {"a0": ev["a0"], "a1": ev["a1"], "cisd": ev["cisd"]})
        if other in series:
            for ev in bz.detect_smt(s, series[other], 60):
                i, d = ev["bar"], ev["dir"]
                if s.day[i] == day and i + 1 < s.n:
                    stop = ev["anchor"] - d * bz.TICK * 2
                    e = s.o[i + 1]
                    if (e - stop) * d > 0:
                        add(sym, "smt_1h", s, i, d, stop, e + 2 * (e - stop))
        for ev in bz.detect_first_fvg(s):
            i, d = ev["bar"], ev["dir"]
            if s.day[i] == day and i + 1 < s.n:
                stop = (ev["bottom"] if d == 1 else ev["top"]) - d * bz.TICK
                e = s.o[i + 1]
                if (e - stop) * d > 0:
                    add(sym, "fvg_4h", s, i, d, stop, e + 2 * (e - stop))
    return rows


def summary(log):
    out = {}
    for row in log.values():
        g = out.setdefault(row["signal"], {"n": 0, "r": [], "fwd": []})
        g["n"] += 1
        if row["r"] is not None:
            g["r"].append(row["r"])
        if row["fwd_12bars_bp"] is not None:
            g["fwd"].append(row["fwd_12bars_bp"])
    return {k: {"signals": v["n"], "mean_R": round(sum(v["r"]) / len(v["r"]), 3) if v["r"] else None,
                "t_R": bz.tstat(v["r"])[2], "mean_fwd_bp": round(sum(v["fwd"]) / len(v["fwd"]), 2) if v["fwd"] else None}
            for k, v in out.items()}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        raise SystemExit(__doc__)
    log_path = argv[argv.index("--log") + 1] if "--log" in argv else LOG
    with open(argv[0]) as f:
        series = _series(f.read())
    if "SPY" not in series:
        raise SystemExit("no SPY 5minute bars in the response")
    days = sorted(set(series["SPY"].day))
    if "--day" in argv:
        from datetime import date
        day = date.fromisoformat(argv[argv.index("--day") + 1])
    else:
        complete = [d for d in days if series["SPY"].day.count(d) >= 78]
        day = complete[-1]
    log = json.load(open(log_path)) if os.path.exists(log_path) else {}
    rows = signals_for_day(series, day)
    for r in rows:
        log[r["key"]] = r
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "w") as f:
        json.dump(log, f, indent=1, sort_keys=True)
    print(json.dumps({"day": str(day), "logged": len(rows), "signals": rows, "cumulative": summary(log)}, indent=1))


if __name__ == "__main__":
    main()
