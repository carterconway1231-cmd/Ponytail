"""Gamma exposure (GEX) levels for the ICT OTE TradingView indicator.

Reads Robinhood option data (raw get_option_instruments and get_option_quotes
responses saved as JSON) and prints one line to paste into the indicator's
"GEX levels" box:

    SPY cw=790 pw=760 flip=772.40 lv=780,775,770 net=+1.25B d=2026-10-13

    cw    call wall: strike with the most call gamma (resistance / magnet)
    pw    put wall: strike with the most put gamma (support / magnet)
    flip  zero-gamma level: spot price where total dealer gamma changes sign
    lv    the next-largest net-gamma strikes
    net   total dealer gamma in $ per 1% move at the current spot

Convention (the standard simplification): dealers are long the calls and short
the puts that customers hold, so call gamma counts positive and put gamma
negative. Above the flip, dealers hedge against the move (price tends to pin
and mean-revert). Below it, they hedge with the move (ranges expand and trends
run). It is a model of positioning, not a certainty.

    python tradingview/gex.py SPY --spot 778.10 instruments*.json quotes*.json
"""
import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ponytail.options_math import bs_gamma  # noqa: E402

CONTRACT = 100
WALL_RANGE = 0.15   # walls and levels only within ±15% of spot
FLIP_RANGE = 0.10   # search for the flip within ±10% of spot
DAY_FRACTION = 0.3  # time value left on expiration day (about 2 trading hours)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load(paths):
    """Join instruments and quotes from saved MCP responses into contracts."""
    insts, quotes = {}, {}
    for p in paths:
        data = json.loads(Path(p).read_text())
        data = data.get("data", data)
        for inst in data.get("instruments", []):
            insts[inst["id"]] = inst
        for r in data.get("results", []):
            q = r.get("quote") or r
            if q.get("instrument_id"):
                quotes[q["instrument_id"]] = q
    contracts = []
    for iid, inst in insts.items():
        q = quotes.get(iid)
        if not q:
            continue
        contracts.append({
            "symbol": (inst.get("chain_symbol") or "").upper(),
            "type": inst.get("type"),
            "strike": _f(inst.get("strike_price")),
            "expiration": dt.date.fromisoformat(inst["expiration_date"]),
            "iv": _f(q.get("implied_volatility")),
            "gamma": _f(q.get("gamma")),
            "oi": int(q.get("open_interest") or 0),
        })
    return contracts


def _years(expiration, today):
    return (max((expiration - today).days, 0) + DAY_FRACTION) / 365


def _dollar_gamma(gamma, oi, spot):
    """Dealer $ gamma per 1% move for one contract line."""
    return gamma * oi * CONTRACT * spot * spot * 0.01


def total_gex(contracts, spot, today):
    """Net dealer gamma if the underlying were at `spot` (BS gamma from each IV)."""
    total = 0.0
    for c in contracts:
        if not c["iv"] or not c["oi"]:
            continue
        g = bs_gamma(spot, c["strike"], _years(c["expiration"], today), c["iv"])
        total += (1 if c["type"] == "call" else -1) * _dollar_gamma(g, c["oi"], spot)
    return total


def find_flip(contracts, spot, today, steps=200):
    """Zero-gamma crossing nearest to spot, or None if gamma never changes sign."""
    lo, hi = spot * (1 - FLIP_RANGE), spot * (1 + FLIP_RANGE)
    grid = [lo + (hi - lo) * i / steps for i in range(steps + 1)]
    vals = [total_gex(contracts, s, today) for s in grid]
    best = None
    for (s0, v0), (s1, v1) in zip(zip(grid, vals), zip(grid[1:], vals[1:])):
        if v0 == 0 or (v0 < 0) != (v1 < 0):
            x = s0 if v0 == v1 else s0 + (s1 - s0) * (-v0) / (v1 - v0)
            if best is None or abs(x - spot) < abs(best - spot):
                best = x
    return best


def compute(contracts, spot, today, n_levels=3):
    by_strike = defaultdict(lambda: {"call": 0.0, "put": 0.0})
    for c in contracts:
        if not c["oi"] or abs(c["strike"] - spot) > spot * WALL_RANGE:
            continue
        g = c["gamma"]
        if g is None:
            g = bs_gamma(spot, c["strike"], _years(c["expiration"], today), c["iv"] or 0)
        by_strike[c["strike"]][c["type"]] += _dollar_gamma(g, c["oi"], spot)
    if not by_strike:
        raise ValueError("no contracts with open interest near spot")
    call_wall = max(by_strike, key=lambda k: by_strike[k]["call"])
    put_wall = max(by_strike, key=lambda k: by_strike[k]["put"])
    net = {k: v["call"] - v["put"] for k, v in by_strike.items()}
    levels = [k for k in sorted(net, key=lambda k: abs(net[k]), reverse=True)
              if k not in (call_wall, put_wall)][:n_levels]
    return {
        "call_wall": call_wall,
        "put_wall": put_wall,
        "flip": find_flip(contracts, spot, today),
        "levels": sorted(levels, reverse=True),
        "net": total_gex(contracts, spot, today),
    }


def format_line(symbol, res, today):
    parts = [symbol.upper(), f"cw={res['call_wall']:g}", f"pw={res['put_wall']:g}"]
    if res["flip"] is not None:
        parts.append(f"flip={res['flip']:.2f}")
    if res["levels"]:
        parts.append("lv=" + ",".join(f"{k:g}" for k in res["levels"]))
    parts.append(f"net={res['net'] / 1e9:+.2f}B")
    parts.append(f"d={today.isoformat()}")
    return " ".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("symbol")
    ap.add_argument("files", nargs="+", help="saved get_option_instruments / get_option_quotes JSON")
    ap.add_argument("--spot", type=float, required=True, help="current underlying price")
    ap.add_argument("--date", type=dt.date.fromisoformat, default=dt.date.today(), help="as-of date (default today)")
    args = ap.parse_args(argv)
    contracts = [c for c in load(args.files) if c["symbol"] in ("", args.symbol.upper())]
    print(format_line(args.symbol, compute(contracts, args.spot, args.date), args.date))


if __name__ == "__main__":
    main()
