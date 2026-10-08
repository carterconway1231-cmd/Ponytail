"""Options-aware walk-forward backtest: does the whole strategy make money
after premium, decay and costs, and does it beat holding SPY?

Walk-forward, no lookahead: a fresh learner is trained only on outcomes that
were knowable on each replay day (untraded signals are graded when their
horizon passes; trades when they close). The first TRADE_AFTER days only
train. On every later day each symbol gets the same pipeline as live:

  factors -> learned-weight decision -> vol regime (IV/RV) -> candidate
  structures (ATM / OTM long, debit verticals of several widths) priced with
  Black-Scholes -> EV gate (learned p_win, realized-vol move, slippage) ->
  per-trade premium cap and Kelly sizing on running equity -> exits at each
  daily close: take profit, stop / trailing stop, time stop, DTE, reversal.

Modeling assumptions (read results with these in mind):
  * Historical option quotes aren't available, so IV is proxied by VIX scaled
    by the symbol's realized vol relative to SPY (or 1.15x realized vol
    without VIX). Positions are marked each day at that day's IV proxy, so
    vol spikes in selloffs and crush afterwards are reflected. For SPY the
    proxy is VIX itself (real data); for single stocks it assumes the same
    implied/realized ratio as the index, which is an approximation.
  * Fills cost a half bid/ask per leg calibrated from real quotes
    (premium.half_spread: ~0.25% of price for index ETFs, ~1.5% with a
    $0.05 floor for single stocks), ENTRY_SLIPPAGE_PCT of it on entries and
    all of it on exits. Exits are evaluated on daily closes, so intraday stop
    fills are approximated by the close. Skew is modeled (premium.skewed_iv).
  * Hourly factors only exist where hourly bars do (about the last 6 months).
  * Earnings: entries are blocked when a report falls before the simulated
    expiry, like the live gate. Robinhood returns ~2 years of report dates;
    earlier quarters are backfilled at 91-day steps and blocked with a
    +/-7-day margin for the date uncertainty.

  python -m ponytail.backtest BARS_DIR [--ablate] [--save-exits] [--capital N]
  (STRATEGY=premium, the default, replays credit spreads; STRATEGY=directional the factor strategy)
  BARS_DIR holds SYM_day.json / SYM_hour.json bar lists, VIX_day.json, and
  optionally SYM_earnings.json (a list of report dates).
"""
import json
import math
import sys
from datetime import date, timedelta

from .factors import ALL_FACTORS, MIN_DAILY_BARS, MIN_HOURLY_BARS, compute_factors
from .learner import Learner
from .options_math import scenario_ev
from .sizing import max_contracts
from .state import MULTIPLIER, State
from .warmstart import CONTEXT_SYMBOLS, DAILY_WINDOW, HOURLY_WINDOW, INDEX_SYMBOLS, MIN_HISTORY, _real, _upper

TARGET_DTE = 30
SPREAD_WIDTHS_PCT = (0.005, 0.01, 0.02)  # of spot: ~$4 / $8 / $16 wide on a $780 ETF
TRADE_AFTER = 60                         # replay days that only train the learner
VIX_TO_ATM = 0.88


def _rv(closes):
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) * math.sqrt(252)


def precompute(bars_by_symbol, horizon):
    """Factor analyses for every (day, symbol), computed once and reused by
    every simulation (ablation reruns only change the weighting)."""
    context = {k: _real((bars_by_symbol.get(k) or {}).get("day")) for k in CONTEXT_SYMBOLS}
    vix = {b["begins_at"][:10]: float(b.get("close_price") or b.get("close_value")) for b in context.get("VIX") or []}
    spy_closes = {b["begins_at"][:10]: float(b["close_price"]) for b in context.get("SPY") or []}
    spy_days = sorted(spy_closes)
    events = []
    for sym, bars in bars_by_symbol.items():
        if sym in INDEX_SYMBOLS:
            continue
        daily, hourly = _real(bars.get("day")), _real(bars.get("hour"))
        days = [b["begins_at"][:10] for b in daily]
        closes = [float(b["close_price"]) for b in daily]
        hour_days = [b["begins_at"][:10] for b in hourly]
        for t in range(MIN_HISTORY - 1, len(daily)):
            day = days[t]
            k = _upper(hour_days, day)
            hslice = hourly[max(0, k - HOURLY_WINDOW):k] if k and hour_days[k - 1] == day else None
            if hslice is not None and len(hslice) < MIN_HOURLY_BARS:
                hslice = None
            dslice = daily[max(0, t + 1 - DAILY_WINDOW):t + 1]
            if len(dslice) < MIN_DAILY_BARS:
                continue
            a = compute_factors(dslice, hslice, context)
            rv = _rv(closes[max(0, t - 20):t + 1]) or 0.2
            j = _upper(spy_days, day)
            spy_rv = _rv([spy_closes[d] for d in spy_days[max(0, j - 21):j]]) if j else None
            if day in vix and spy_rv:
                # VIX is a variance-swap level that includes skew; SPY ATM IV runs ~0.88x
                # of it (13.2% vs VIX 15.0 on 2026-10-06). Skew is modeled separately.
                iv = VIX_TO_ATM * vix[day] / 100 * min(2.5, max(0.7, rv / spy_rv))
            else:
                iv = rv * 1.15
            events.append({
                "day": day, "sym": sym, "close": closes[t], "atr": a["atr"], "drift": a["drift_per_day"],
                "regime": a["regime"], "factors": {n: {"score": f["score"]} for n, f in a["factors"].items()},
                "rv": rv, "iv": max(0.08, iv),
                "outcome": (days[t + horizon], closes[t + horizon]) if t + horizon < len(daily) else None,
            })
    events.sort(key=lambda e: (e["day"], e["sym"]))
    return events


EARNINGS_STEP_DAYS = 91
BACKFILL_MARGIN_DAYS = 7


def earnings_windows(dates, start):
    """[(first_blocked_day, last_blocked_day)] per report: exact dates block that
    day; backfilled quarters (before the earliest known report) block +/- a margin."""
    if not dates:
        return []
    known = sorted(date.fromisoformat(d) for d in dates)
    windows = [(d, d) for d in known]
    d = known[0] - timedelta(days=EARNINGS_STEP_DAYS)
    margin = timedelta(days=BACKFILL_MARGIN_DAYS)
    while d >= start - timedelta(days=EARNINGS_STEP_DAYS):
        windows.append((d - margin, d + margin))
        d -= timedelta(days=EARNINGS_STEP_DAYS)
    return windows


def _earnings_in(windows, first, last):
    return any(lo <= last and hi >= first for lo, hi in windows)


def _half_spread(price, symbol=None):
    from .premium import half_spread
    return half_spread(price, symbol)


def _value(legs, spot, years, iv, symbol=None):
    from .premium import leg_price
    return sum(sign * leg_price(spot, k, years, iv, kind, symbol) for k, kind, sign in legs)


def _candidates(cfg, spot, iv, rv, kind, structure):
    """(name, legs) candidates with a TARGET_DTE expiry."""
    sign = 1 if kind == "call" else -1
    atm = round(spot)
    out = []
    if structure in ("long", "any"):
        sd = spot * iv * math.sqrt(TARGET_DTE / 365)
        out.append(("long_atm", [(atm, kind, 1)]))
        out.append(("long_otm", [(round(spot + sign * 0.25 * sd), kind, 1)]))
    if structure in ("debit_spread", "any") and cfg.allow_spreads:
        widths = sorted({1, 2} | {max(1, round(spot * pct)) for pct in SPREAD_WIDTHS_PCT})
        for w in widths:  # narrow spreads keep risk per contract within a small account's budget
            out.append((f"spread_{w}", [(atm, kind, 1), (atm + sign * w, kind, -1)]))
    return out


def simulate(cfg, events, capital, horizon, mask=None, funnel=None, earnings=None):
    """Run the strategy over precomputed events. mask: factor names to drop.
    funnel (dict) counts where would-be trades drop out."""
    funnel = funnel if funnel is not None else {}
    start = date.fromisoformat(events[0]["day"])
    blackout = {sym: earnings_windows(dates, start) for sym, dates in (earnings or {}).items()}

    def drop(key):
        funnel[key] = funnel.get(key, 0) + 1
    first = date.fromisoformat(events[0]["day"])
    learner = Learner({}, cfg, today=first)
    pending, positions, trades = [], {}, []
    day_list = sorted({e["day"] for e in events})
    trade_from = day_list[min(TRADE_AFTER, len(day_list) - 1)]
    equity = capital
    by_day = {}
    for e in events:
        by_day.setdefault(e["day"], []).append(e)

    for day in day_list:
        today = date.fromisoformat(day)
        learner.today = today
        still = []
        for p in pending:  # grade untraded signals whose outcome is now known
            if p["outcome"][0] <= day:
                learner.grade_outcome(p["factors"], p["regime"], p["decision"], p["conviction"], p["close"],
                                      p["outcome"][1], p["atr"], p["drift"], cfg.shadow_weight, p["outcome"][0])
            else:
                still.append(p)
        pending = still

        for e in by_day[day]:
            sym, spot = e["sym"], e["close"]
            factors = {k: v for k, v in e["factors"].items() if not mask or k not in mask}
            decision = learner.decide(factors, e["regime"])
            if e["outcome"]:
                pending.append({**e, "factors": factors, "decision": decision["decision"],
                                "conviction": decision["conviction"]})

            pos = positions.get(sym)
            if pos:  # mark and apply exits at the close
                years = max(0.0, (pos["expiry"] - today).days / 365)
                value = _value(pos["legs"], spot, years, e["iv"], sym)  # today's IV: vol expansion/crush shows up
                r = (value - pos["entry"]) / pos["entry"]
                pos["path"].append(round(r, 4))
                pos["hwm"] = max(pos["hwm"], value)
                held = (today - pos["opened"]).days
                trail_from = pos["entry"] * (1 + cfg.trail_activate_pct)
                stop = pos["entry"] * (1 - cfg.stop_loss_pct)
                if pos["kind"] == "single" and pos["hwm"] >= trail_from:
                    stop = max(stop, pos["hwm"] * (1 - cfg.trail_pct))
                flipped = decision["decision"] == ("SELL" if pos["direction"] > 0 else "BUY")
                reason = ("take profit" if r >= cfg.take_profit_pct else
                          "stop" if value <= stop else
                          "dte exit" if (pos["expiry"] - today).days <= cfg.exit_dte else
                          "time stop" if held >= cfg.time_stop_days and r < cfg.time_stop_min_gain else
                          "signal reversed" if flipped else None)
                if reason:
                    exit_px = max(0.0, value - sum(_half_spread(_value([leg], spot, years, e["iv"], sym), sym)
                                                   for leg in pos["legs"]))
                    pnl = (exit_px - pos["entry"]) * MULTIPLIER * pos["qty"]
                    premium = pos["entry"] * MULTIPLIER * pos["qty"]
                    learner.learn_trade(pos["signal"], pos["direction"], pnl, premium, today)
                    equity += pnl
                    trades.append({"symbol": sym, "kind": pos["kind"], "structure": pos["name"], "mode": "backtest",
                                   "direction": "call" if pos["direction"] > 0 else "put",
                                   "pnl": round(pnl, 2), "r": round(pnl / premium, 3), "reason": reason,
                                   "path": pos["path"], "held_days": held, "vol_regime": pos["vol_regime"],
                                   "opened": pos["opened"].isoformat(), "closed_at": f"{day}T20:00:00+00:00"})
                    del positions[sym]
                continue  # no same-day re-entry after an exit or while holding

            if day < trade_from:
                continue
            drop("evaluated")
            if decision["decision"] == "HOLD":
                drop("hold: weak score or confluence")
                continue
            if len(positions) >= cfg.max_open_positions:
                drop("max open positions")
                continue
            if cfg.avoid_earnings and _earnings_in(blackout.get(sym, []), today, today + timedelta(days=TARGET_DTE)):
                drop("earnings before expiry")
                continue
            kind = "call" if decision["decision"] == "BUY" else "put"
            ratio = e["iv"] / e["rv"] if e["rv"] else 1.0
            vol_regime = "expensive" if ratio >= cfg.iv_rv_expensive else "cheap" if ratio <= 1.1 else "normal"
            structure = "debit_spread" if vol_regime == "expensive" else "any"
            expiry = today + timedelta(days=TARGET_DTE)
            hold = cfg.ev_hold_days
            move = spot * e["rv"] / math.sqrt(252) * math.sqrt(hold)
            p_up = decision["p_win"] if kind == "call" else 1 - decision["p_win"]
            best, why = None, "no candidate"
            for name, legs in _candidates(cfg, spot, e["iv"], e["rv"], kind, structure):
                mid = _value(legs, spot, TARGET_DTE / 365, e["iv"], sym)
                if mid <= 0.05:
                    continue
                half = sum(_half_spread(_value([(k, kd, 1)], spot, TARGET_DTE / 365, e["iv"], sym), sym)
                           for k, kd, _ in legs)
                cost = mid + cfg.entry_slippage_pct * half
                if len(legs) == 2 and cost > cfg.max_spread_debit_pct * abs(legs[1][0] - legs[0][0]):
                    why = "spread debit too large vs width"
                    continue
                ev, _, _ = scenario_ev([(k, kd, e["iv"], sg) for k, kd, sg in legs], spot, move, p_up, hold,
                                       TARGET_DTE, cost)
                evpd = ev / cost
                if evpd < cfg.min_contract_ev:
                    why = "negative expected value"
                    continue
                n, _ = max_contracts(cfg, equity, decision, cost, len(legs) == 2)
                if n < 1:
                    why = "risk budget < 1 contract"
                    continue
                n = min(n, math.floor(cfg.max_premium_per_trade / (cost * MULTIPLIER)))
                if n < 1:
                    why = "premium cap < 1 contract"
                    continue
                if best is None or evpd > best[0]:
                    best = (evpd, name, legs, cost, n)
            if not best:
                drop(why)
            else:
                drop("opened")
                _, name, legs, cost, n = best
                positions[sym] = {"name": name, "kind": "spread" if len(legs) == 2 else "single", "legs": legs,
                                  "iv": e["iv"], "entry": cost, "qty": n, "opened": today, "expiry": expiry,
                                  "hwm": cost, "path": [], "direction": 1 if kind == "call" else -1,
                                  "vol_regime": vol_regime,
                                  "signal": {"factors": factors, "regime": e["regime"],
                                             "conviction": decision["conviction"]}}
    return trades, learner


# ---- premium selling (credit verticals) -----------------------------------------

PREMIUM_DEFAULTS = {
    "structure": "put",      # put | call | condor
    "short_delta": 0.20,
    "dte": 45,
    "width_pct": 0.01,       # strike width as a fraction of spot (>= one strike step)
    "take_profit": 0.50,     # close when 50% of the credit is captured
    "stop_x": 2.0,           # close when the loss reaches 2x the credit
    "manage_dte": 21,        # close at 21 DTE regardless
    "min_iv_rv": 1.0,        # only sell when implied vol >= realized vol x this
    "min_credit_pct": 0.10,  # credit must be >= 10% of width
    "risk_pct": 0.10,        # max loss per position as a fraction of equity
    "max_total_risk_pct": 0.40,
}


def simulate_premium(cfg, events, capital, params=None, earnings=None, funnel=None):
    """Sell credit verticals; returns trades. Same walk-forward clock and cost
    model as the directional simulator, with skewed IV and daily IV marks."""
    from . import premium as pm
    p = {**PREMIUM_DEFAULTS, **(params or {})}
    funnel = funnel if funnel is not None else {}

    def drop(key):
        funnel[key] = funnel.get(key, 0) + 1

    start = date.fromisoformat(events[0]["day"])
    blackout = {sym: earnings_windows(dates, start) for sym, dates in (earnings or {}).items()}
    sides = {"put": ["put"], "call": ["call"], "condor": ["put", "call"]}[p["structure"]]
    day_list = sorted({e["day"] for e in events})
    trade_from = day_list[min(TRADE_AFTER, len(day_list) - 1)]
    equity, positions, trades = capital, {}, []

    for e in events:
        day, sym, spot, iv = e["day"], e["sym"], e["close"], e["iv"]
        today = date.fromisoformat(day)
        for side in sides:
            key = (sym, side)
            pos = positions.get(key)
            if pos:
                years = max(0.0, (pos["expiry"] - today).days / 365)
                value = pm.spread_value(spot, pos["short"], pos["long"], years, iv, side, sym)
                legs = [pm.leg_price(spot, k, years, iv, side, sym) for k in (pos["short"], pos["long"])]
                close_cost = min(pos["width"], value + sum(_half_spread(x, sym) for x in legs))
                loss = close_cost - pos["credit"]
                dte_left = (pos["expiry"] - today).days
                reason = ("take profit" if value <= (1 - p["take_profit"]) * pos["credit"] else
                          "loss stop" if loss >= p["stop_x"] * pos["credit"] else
                          "manage dte" if dte_left <= p["manage_dte"] else None)
                pos["path"].append(round(-loss / pos["credit"], 3))
                if reason:
                    pnl = (pos["credit"] - close_cost) * MULTIPLIER * pos["qty"]
                    equity += pnl
                    trades.append({"symbol": sym, "kind": "credit_spread", "structure": f"{side}_credit",
                                   "direction": side, "mode": "backtest", "pnl": round(pnl, 2),
                                   "r": round(pnl / (pos["max_loss"] * pos["qty"]), 3), "reason": reason,
                                   "path": pos["path"], "held_days": (today - pos["opened"]).days,
                                   "opened": pos["opened"].isoformat(), "closed_at": f"{day}T20:00:00+00:00",
                                   "credit": round(pos["credit"], 2), "width": pos["width"]})
                    del positions[key]
                continue
            if day < trade_from:
                continue
            drop("evaluated")
            if cfg.avoid_earnings and _earnings_in(blackout.get(sym, []), today, today + timedelta(days=p["dte"])):
                drop("earnings before expiry")
                continue
            if e["rv"] and iv < p["min_iv_rv"] * e["rv"]:
                drop("IV not rich vs realized")
                continue
            years = p["dte"] / 365
            step = pm.strike_step(spot)
            width = max(step, round(spot * p["width_pct"] / step) * step)
            short_k, long_k = pm.credit_spread(spot, years, iv, side, p["short_delta"], width, step, sym)
            mid = pm.spread_value(spot, short_k, long_k, years, iv, side, sym)
            legs = [pm.leg_price(spot, k, years, iv, side, sym) for k in (short_k, long_k)]
            credit = mid - cfg.entry_slippage_pct * sum(_half_spread(x, sym) for x in legs)
            if credit < p["min_credit_pct"] * width:
                drop("credit too small vs width")
                continue
            max_loss = (width - credit) * MULTIPLIER
            open_risk = sum(x["max_loss"] * x["qty"] for x in positions.values())
            qty = min(math.floor(equity * p["risk_pct"] / max_loss),
                      math.floor((equity * p["max_total_risk_pct"] - open_risk) / max_loss))
            if qty < 1:
                drop("risk budget < 1 spread")
                continue
            drop("opened")
            positions[key] = {"short": short_k, "long": long_k, "width": width, "credit": credit, "qty": qty,
                              "max_loss": max_loss, "opened": today, "expiry": today + timedelta(days=p["dte"]),
                              "path": []}
    return trades


def summarize(trades, capital, spy_bars, start_day):
    from .performance import report
    st = State("/dev/null")
    st.data["trade_log"] = trades
    rep = report(st, capital)
    rep.pop("by_exit_reason", None)
    window = [b for b in spy_bars or [] if b["begins_at"][:10] >= start_day]
    if window and rep.get("trades"):
        bench = float(window[-1]["close_price"]) / float(window[0]["close_price"]) - 1
        rep["spy_buy_and_hold_pct"] = round(bench * 100, 2)
        rep["beat_spy"] = rep["return_pct"] / 100 > bench
    rep["by_exit"] = {}
    for t in trades:
        g = rep["by_exit"].setdefault(t["reason"], {"n": 0, "pnl": 0.0})
        g["n"] += 1
        g["pnl"] = round(g["pnl"] + t["pnl"], 2)
    return rep


def premium_params(cfg):
    """The live STRATEGY=premium settings as simulate_premium parameters."""
    return {"structure": {"both": "condor"}.get(cfg.premium_side, cfg.premium_side),
            "short_delta": cfg.premium_short_delta, "dte": (cfg.premium_min_dte + cfg.premium_max_dte) // 2,
            "width_pct": cfg.premium_max_width_pct, "take_profit": cfg.premium_take_profit,
            "stop_x": cfg.premium_stop_x, "manage_dte": cfg.premium_manage_dte, "min_iv_rv": cfg.premium_min_iv_rv,
            "min_credit_pct": cfg.premium_min_credit_pct, "risk_pct": cfg.premium_risk_pct,
            "max_total_risk_pct": cfg.premium_max_total_risk_pct}


def run(cfg, bars_by_symbol, capital, ablate=False):
    horizon = cfg.shadow_horizon
    events = precompute(bars_by_symbol, horizon)
    earnings = {s: b["earnings"] for s, b in bars_by_symbol.items() if b.get("earnings")}
    if not events:
        raise SystemExit("not enough history to backtest")
    day_list = sorted({e["day"] for e in events})
    start_day = day_list[min(TRADE_AFTER, len(day_list) - 1)]
    spy = _real((bars_by_symbol.get("SPY") or {}).get("day"))
    funnel = {}
    if cfg.strategy == "premium":
        from .premium import asset_class
        events = [e for e in events if cfg.premium_allow_stocks or asset_class(e["sym"]) == "index"]
        trades = simulate_premium(cfg, events, capital, premium_params(cfg), earnings=earnings, funnel=funnel)
        return {"strategy": "premium", "period": f"{start_day}..{day_list[-1]}", "params": premium_params(cfg),
                "funnel": funnel, "symbols": sorted({e["sym"] for e in events}),
                "results": summarize(trades, capital, spy, start_day), "trades": trades}
    trades, learner = simulate(cfg, events, capital, horizon, funnel=funnel, earnings=earnings)
    out = {"period": f"{start_day}..{day_list[-1]}", "train_only_days": TRADE_AFTER, "funnel": funnel,
           "symbols": sorted({e["sym"] for e in events}), "results": summarize(trades, capital, spy, start_day),
           "learned": learner.report()["factors"], "trades": trades}
    if ablate:
        base = out["results"].get("total_pnl", 0.0)
        rows = []
        for f in ALL_FACTORS:
            t2, _ = simulate(cfg, events, capital, horizon, mask={f}, earnings=earnings)
            r2 = summarize(t2, capital, spy, start_day)
            rows.append({"factor": f, "pnl_without": r2.get("total_pnl", 0.0), "trades_without": r2.get("trades", 0),
                         "contribution": round(base - r2.get("total_pnl", 0.0), 2)})
        rows.sort(key=lambda r: -r["contribution"])
        out["ablation"] = rows
    return out


def main():
    import glob
    import os

    from dotenv import load_dotenv

    from .config import Config

    load_dotenv()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    cfg = Config.from_env()
    capital = cfg.paper_capital
    if "--capital" in sys.argv:
        capital = float(sys.argv[sys.argv.index("--capital") + 1])
        args = [a for a in args if a != sys.argv[sys.argv.index("--capital") + 1]]
    bars = {}
    for path in glob.glob(os.path.join(args[0], "*_*.json")):
        sym, interval = os.path.basename(path)[:-5].rsplit("_", 1)
        with open(path) as f:
            bars.setdefault(sym.upper(), {})[interval] = json.load(f)
    out = run(cfg, bars, capital, ablate="--ablate" in sys.argv)
    if "--save-exits" in sys.argv and cfg.strategy == "directional":
        state = State.load(cfg.state_path)
        state.data["exit_samples"] = [{"path": t["path"]} for t in out["trades"]
                                      if t["kind"] == "single" and t["path"]][-500:]
        state.save()
    trades = out.pop("trades")
    out["sample_trades"] = [{k: v for k, v in t.items() if k != "path"} for t in trades[-10:]]
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
