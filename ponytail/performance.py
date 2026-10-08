"""Scorekeeping: is the agent actually making money, net of what it costs to
run, and better than just holding SPY?

The go-live gate turns these numbers into a hard requirement: LIVE_TRADING
is only honored once paper trading shows enough trades over enough days with
positive expectancy after AI costs, an acceptable profit factor, and a
tolerable drawdown (FORCE_LIVE=true overrides, deliberately).
"""
from datetime import date


def _equity_curve(trades, capital):
    eq, peak, max_dd = capital, capital, 0.0
    for t in sorted(trades, key=lambda t: t["closed_at"]):
        eq += t["pnl"]
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    return eq, max_dd


def ai_cost_since(state, since_iso):
    """Claude spend from a date on (kept per day forever; the run log is trimmed)."""
    return sum(c for d, c in state.data.get("ai_cost_by_day", {}).items() if d >= since_iso[:10])


def benchmark_return(spy_bars, start_iso):
    """SPY buy-and-hold return from the first trade's date to the latest bar."""
    if not spy_bars:
        return None
    start = [b for b in spy_bars if b["begins_at"][:10] >= start_iso[:10]]
    if not start:
        return None
    return float(spy_bars[-1]["close_price"]) / float(start[0]["close_price"]) - 1


def _group(trades, key):
    out = {}
    for t in trades:
        g = out.setdefault(str(t.get(key) or "unknown"), {"n": 0, "pnl": 0.0, "wins": 0})
        g["n"] += 1
        g["pnl"] = round(g["pnl"] + t["pnl"], 2)
        g["wins"] += t["pnl"] > 0
    return out


def report(state, capital, mode=None, spy_bars=None):
    trades = [t for t in state.trade_log if mode is None or t.get("mode") == mode]
    if not trades:
        return {"trades": 0, "note": "no closed trades yet"}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    first = min(t["closed_at"] for t in trades)
    ai_costs = ai_cost_since(state, first)
    final_eq, max_dd = _equity_curve(trades, capital)
    total = sum(pnls)
    rs = [t["r"] for t in trades if t.get("r") is not None]
    slips = [t["slippage_pct"] for t in trades if t.get("slippage_pct") is not None]
    held = [t["held_days"] for t in trades if t.get("held_days") is not None]
    bench = benchmark_return(spy_bars, first)
    strat = (total - ai_costs) / capital
    return {
        "trades": len(trades), "win_rate": round(len(wins) / len(trades), 3),
        "expectancy_per_trade": round(total / len(trades), 2),
        "expectancy_net_of_ai": round((total - ai_costs) / len(trades), 2),
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) else None,
        "total_pnl": round(total, 2), "ai_costs": round(ai_costs, 2), "net_pnl": round(total - ai_costs, 2),
        "max_drawdown": round(max_dd, 2), "max_drawdown_pct": round(max_dd / capital, 3),
        "return_pct": round(strat * 100, 2),
        "spy_buy_and_hold_pct": None if bench is None else round(bench * 100, 2),
        "beat_spy": None if bench is None else strat > bench,
        "avg_held_days": round(sum(held) / len(held), 1) if held else None,
        "avg_entry_slippage_pct": round(100 * sum(slips) / len(slips), 2) if slips else None,
        "by_structure": _group(trades, "kind"), "by_vol_regime": _group(trades, "vol_regime"),
        "by_exit_reason": _group(trades, "reason"),
        "first_close": first[:10],
    }


def go_live_check(cfg, state, today=None):
    today = today or date.today()
    paper = [t for t in state.trade_log if t.get("mode") == "paper"]
    rep = report(state, cfg.paper_capital, mode="paper")
    days = (today - date.fromisoformat(rep["first_close"])).days if paper else 0
    checks = [
        ("paper trades", len(paper), f">= {cfg.min_paper_trades}", len(paper) >= cfg.min_paper_trades),
        ("days of paper trading", days, f">= {cfg.min_paper_days}", days >= cfg.min_paper_days),
        ("expectancy net of AI costs", rep.get("expectancy_net_of_ai"), "> 0", (rep.get("expectancy_net_of_ai") or 0) > 0),
        ("profit factor", rep.get("profit_factor"), f">= {cfg.min_profit_factor}",
         rep.get("profit_factor") is not None and rep["profit_factor"] >= cfg.min_profit_factor),
        ("max drawdown", rep.get("max_drawdown_pct"), f"<= {cfg.max_drawdown_pct:.0%}",
         paper != [] and rep["max_drawdown_pct"] <= cfg.max_drawdown_pct),
    ]
    return {"ready": all(c[3] for c in checks),
            "checks": [{"check": n, "value": v, "need": need, "ok": ok} for n, v, need, ok in checks]}
