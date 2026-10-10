"""Ledger, scanner and dashboard for the stock framework (trading/framework.md).

The cycle itself is run by Claude following framework.md; this file does the
arithmetic and record-keeping so it is the same every cycle:

  python -m trading.bot scan QUOTES.json [QUOTES.json ...]   # regime read + MR / momentum shortlist
  python -m trading.bot trend BARS.json                      # daily-bar structure for shortlisted names
  python -m trading.bot check QUOTES.json                    # stop / target / horizon check on open positions
  python -m trading.bot open SYMBOL --strategy S --tier T --entry P --qty Q --stop P --target P
                             --horizon DAYS --thesis "..." [--order-id ID] [--forced]
  python -m trading.bot close SYMBOL --exit P --reason R --setup 0-40 --execution 0-35 --outcome 0-25 --note "..."
  python -m trading.bot skip SYMBOL --strategy S --why "..." [--price P]
  python -m trading.bot equity VALUE                         # account value from get_portfolio
  python -m trading.bot regime "one-line read"
  python -m trading.bot status                               # limits, breaker, metrics
  python -m trading.bot render                               # rewrite dashboard.html from the ledger

ledger.json is the source of truth; state.md is the narrative log and
dashboard.html a rendered view. Every input file is a Robinhood MCP response
saved verbatim (or the file Claude Code saved an oversized one to).
"""
import argparse
import html
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ponytail.market import decode_tool_response

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.environ.get("TRADING_LEDGER", os.path.join(HERE, "ledger.json"))
DASHBOARD = os.environ.get("TRADING_DASHBOARD", os.path.join(HERE, "dashboard.html"))

CONFIG = {
    "account": "955800222",
    "account_label": "Agentic ••0222",
    "position_size": 10.0,       # fixed dollars per entry
    "max_positions": 2,          # shared across all strategies
    "daily_loss_limit": 5.0,     # dollars, realized + today's unrealized
    "breaker_drawdown": 0.20,    # from peak equity: halve size, pause entries until reviewed
    "max_trades_per_day": 3,     # entries; also keeps day trades under the PDT count
    "min_trades_per_day": 0,
}

INDICES = ["SPY", "QQQ", "IWM"]
SECTORS = {"XLK": "Tech", "SMH": "Semis", "XLC": "Comm", "XLY": "Discretionary", "XLF": "Financials",
           "XLV": "Health", "XLE": "Energy", "XLI": "Industrials", "XLP": "Staples", "XLU": "Utilities",
           "XLB": "Materials", "XLRE": "Real estate"}
UNIVERSE = {
    "AAPL": "XLK", "MSFT": "XLK", "ORCL": "XLK", "CRM": "XLK", "ADBE": "XLK",
    "NVDA": "SMH", "AVGO": "SMH", "AMD": "SMH", "MU": "SMH",
    "GOOGL": "XLC", "META": "XLC", "NFLX": "XLC",
    "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "MCD": "XLY", "NKE": "XLY",
    "JPM": "XLF", "BAC": "XLF", "GS": "XLF", "V": "XLF", "MA": "XLF",
    "UNH": "XLV", "LLY": "XLV", "JNJ": "XLV", "ABBV": "XLV", "MRK": "XLV",
    "XOM": "XLE", "CVX": "XLE",
    "CAT": "XLI", "GE": "XLI", "BA": "XLI", "HON": "XLI",
    "WMT": "XLP", "COST": "XLP", "PG": "XLP", "KO": "XLP",
    "NEE": "XLU", "LIN": "XLB", "PLD": "XLRE",
}

# scan thresholds (percent): see strategies/*.md for how each is used
MR_MIN_DROP = -2.0          # day change at or below this is a mean-reversion look
MR_EXTERNAL_SECTOR = -0.75  # sector (or SPY) down at least this much = plausibly external
MR_IDIOSYNCRATIC_GAP = -2.5 # stock this far below its own sector = probably company news
MOM_MIN_RS = 1.5            # stock minus sector, percent
NEAR_STOP_R = 0.25          # exit proactively inside this fraction of R from the stop
FAST_CADENCE_R = 0.5        # cushion under this many R = check every cycle you can


def now():
    return datetime.now(timezone.utc)


def today_et(ts=None):
    return (ts or now()).astimezone(ET).date().isoformat()


def load():
    if os.path.exists(LEDGER):
        with open(LEDGER) as f:
            led = json.load(f)
    else:
        led = {}
    for k, v in (("positions", []), ("trades", []), ("skips", []), ("equity", []), ("peak", 0.0),
                 ("breaker", {"tripped": False}), ("regime", {}), ("today_unrealized", 0.0)):
        led.setdefault(k, v)
    led["config"] = {**CONFIG, **led.get("config", {})}
    return led


def save(led):
    tmp = LEDGER + ".tmp"
    with open(tmp, "w") as f:
        json.dump(led, f, indent=1, sort_keys=True)
    os.replace(tmp, LEDGER)


def read_payload(path):
    with open(path) as f:
        data = decode_tool_response(f.read())
    if data is None:
        raise SystemExit(f"{path}: not a Robinhood tool response")
    return data


def quotes_from(paths):
    """symbol -> {last, prev, chg_pct, time} from get_equity_quotes responses."""
    out = {}
    for p in paths:
        for r in (read_payload(p).get("data") or {}).get("results", []):
            q = r.get("quote") or r
            sym = (q.get("symbol") or "").upper()
            last = _f(q.get("last_trade_price"))
            ext = _f(q.get("last_non_reg_trade_price"))
            if ext and (q.get("venue_last_non_reg_trade_time") or "") > (q.get("venue_last_trade_time") or ""):
                last = ext
            prev = _f(q.get("adjusted_previous_close")) or _f(q.get("previous_close"))
            if sym and last and prev:
                out[sym] = {"last": last, "prev": prev, "chg_pct": round((last / prev - 1) * 100, 2),
                            "time": q.get("venue_last_trade_time"), "state": q.get("state")}
    return out


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- scan

def scan(quotes):
    regime = [{"name": s, "label": s, "chg": quotes[s]["chg_pct"]} for s in INDICES if s in quotes]
    regime += [{"name": e, "label": SECTORS[e], "chg": quotes[e]["chg_pct"]} for e in SECTORS if e in quotes]
    spy = quotes.get("SPY", {}).get("chg_pct", 0.0)
    rows, missing = [], []
    for sym, etf in UNIVERSE.items():
        q = quotes.get(sym)
        if not q:
            missing.append(sym)
            continue
        sec = quotes.get(etf, {}).get("chg_pct")
        rs = None if sec is None else round(q["chg_pct"] - sec, 2)
        rows.append({"symbol": sym, "last": q["last"], "chg": q["chg_pct"], "sector": etf, "sector_chg": sec, "rs": rs})
    mr, mom = [], []
    for r in rows:
        if r["chg"] <= MR_MIN_DROP:
            external = min(r["sector_chg"] if r["sector_chg"] is not None else 0.0, spy) <= MR_EXTERNAL_SECTOR
            idio = r["rs"] is not None and r["rs"] <= MR_IDIOSYNCRATIC_GAP
            r2 = dict(r, cause_hint="external (sector/index weak)" if external and not idio
                      else "likely company-specific: check news, default FAIL" if idio
                      else "unclear: sector not weak, check news")
            mr.append(r2)
        if r["rs"] is not None and r["rs"] >= MOM_MIN_RS and r["chg"] > 0:
            peers = [x for x in rows if x["sector"] == r["sector"] and x["symbol"] != r["symbol"]]
            standout = all(x["chg"] < r["chg"] - 1.0 for x in peers) if peers else True
            mom.append(dict(r, standout=standout))
    mr.sort(key=lambda r: r["chg"])
    mom.sort(key=lambda r: -r["rs"])
    return {"spy": spy, "regime": regime, "mean_reversion": mr, "momentum": mom,
            "scanned": len(rows), "missing": missing}


# ---------------------------------------------------------------- trend

def bars_from(path):
    out = {}
    for r in (read_payload(path).get("data") or {}).get("results", []):
        bars = [b for b in r.get("bars", []) if not b.get("interpolated")]
        if bars:
            out[r["symbol"].upper()] = bars
    return out


def trend(bars):
    c = [float(b["close_price"]) for b in bars]
    h = [float(b["high_price"]) for b in bars]
    lo = [float(b["low_price"]) for b in bars]
    n = len(c)

    def sma(k, end=n):
        return round(sum(c[end - k:end]) / k, 2) if end >= k else None

    out = {"bars": n, "close": c[-1], "sma10": sma(10), "sma20": sma(20), "sma50": sma(50)}
    if n >= 15:
        out["sma10_rising"] = sma(10) > sma(10, n - 5)
    if n >= 20:
        hi20 = max(h[-20:])
        out["off_20d_high_pct"] = round((c[-1] / hi20 - 1) * 100, 2)
        # four weekly chunks: a real downtrend has lower highs AND lower lows week over week
        wk = [(max(h[i:i + 5]), min(lo[i:i + 5])) for i in range(n - 20, n, 5)]
        lower = sum(1 for a, b in zip(wk, wk[1:]) if b[0] < a[0] and b[1] < a[1])
        out["weekly_lower_highs_lows"] = f"{lower}/3"
        out["downtrend"] = lower >= 3
        # base: the last 5 sessions' low holds above the prior 5 sessions' low
        out["higher_low"] = min(lo[-5:]) > min(lo[-10:-5])
        out["support_held"] = c[-1] >= (sma(10) or 0) and min(lo[-3:]) >= min(lo[-10:-3])
    return out


# ---------------------------------------------------------------- positions

def check(led, quotes):
    rows, today = [], today_et()
    unreal_today = 0.0
    for p in led["positions"]:
        q = quotes.get(p["symbol"])
        if not q:
            rows.append({"symbol": p["symbol"], "action": "NO_QUOTE", "why": "fetch a quote before deciding"})
            continue
        px, risk = q["last"], p["entry"] - p["stop"]
        p["mark"], p["prev_close"], p["marked_at"] = px, q["prev"], now().isoformat()
        base = p["entry"] if p["opened_day"] == today else q["prev"]
        unreal_today += (px - base) * p["qty"]
        r_now = (px - p["entry"]) / risk if risk > 0 else 0.0
        cushion_r = (px - p["stop"]) / risk if risk > 0 else 0.0
        held = (datetime.fromisoformat(today) - datetime.fromisoformat(p["opened_day"])).days
        if px <= p["stop"]:
            action, why = "EXIT_STOP", "at or through the stop"
        elif cushion_r <= NEAR_STOP_R:
            action, why = "EXIT_NEAR_STOP", f"within {NEAR_STOP_R}R of the stop: exit now rather than risk a gap through it"
        elif px >= p["target"]:
            action, why = "EXIT_TARGET", "target reached"
        elif r_now >= 2.0 and not p.get("trimmed"):
            action, why = "TRIM", "2R+ extension: trim or tighten"
        elif held > p["horizon_days"]:
            action, why = "EXIT_HORIZON", f"held {held}d > {p['horizon_days']}d horizon"
        else:
            action, why = "HOLD", "check thesis and relative strength vs sector"
        rows.append({"symbol": p["symbol"], "strategy": p["strategy"], "price": px, "entry": p["entry"],
                     "stop": p["stop"], "target": p["target"], "unrealized": round((px - p["entry"]) * p["qty"], 2),
                     "r_now": round(r_now, 2), "cushion_r": round(cushion_r, 2),
                     "fast_cadence": cushion_r < FAST_CADENCE_R, "held_days": held, "action": action, "why": why})
    led["today_unrealized"] = round(unreal_today, 2)
    led["today_unrealized_day"] = today
    return rows


def realized_today(led, day=None):
    day = day or today_et()
    return round(sum(t.get("pnl_today", t["pnl"]) for t in led["trades"] if t["closed_day"] == day), 2)


def daily_pnl(led):
    unreal = led["today_unrealized"] if led.get("today_unrealized_day") == today_et() else 0.0
    return round(realized_today(led) + unreal, 2)


def entries_today(led):
    day = today_et()
    return sum(1 for p in led["positions"] if p["opened_day"] == day) + \
        sum(1 for t in led["trades"] if t["opened_day"] == day)


def day_trades_5d(led):
    days = sorted({t["closed_day"] for t in led["trades"]})[-5:]
    return sum(1 for t in led["trades"] if t["opened_day"] == t["closed_day"] and t["closed_day"] in days)


def gate(led, symbol):
    """Account-level checks every entry must pass; strategy gates are in the strategy files."""
    cfg, fails = led["config"], []
    if led["breaker"].get("tripped"):
        fails.append("circuit breaker tripped: entries paused until the user reviews")
    if len(led["positions"]) >= cfg["max_positions"]:
        fails.append(f"all {cfg['max_positions']} position slots in use")
    if any(p["symbol"] == symbol for p in led["positions"]):
        fails.append(f"already holding {symbol}: no adding / averaging down")
    if daily_pnl(led) <= -cfg["daily_loss_limit"]:
        fails.append(f"daily loss limit ${cfg['daily_loss_limit']:.2f} reached")
    if entries_today(led) >= cfg["max_trades_per_day"]:
        fails.append(f"max {cfg['max_trades_per_day']} entries per day reached")
    return fails


def size(led):
    s = led["config"]["position_size"]
    return s / 2 if led["breaker"].get("tripped") else s


def open_position(led, a):
    sym = a.symbol.upper()
    fails = gate(led, sym)
    if a.stop >= a.entry:
        fails.append("stop must be below entry (long only)")
    if a.target <= a.entry:
        fails.append("target must be above entry")
    if not a.thesis or len(a.thesis) < 10:
        fails.append("write a one-line thesis (why this, why now)")
    if fails:
        raise SystemExit("REFUSED: " + "; ".join(fails))
    p = {"id": uuid.uuid4().hex[:8], "symbol": sym, "strategy": a.strategy, "tier": a.tier,
         "tag": "forced" if a.forced else "organic", "entry": a.entry, "qty": a.qty,
         "cost": round(a.entry * a.qty, 2), "stop": a.stop, "target": a.target, "horizon_days": a.horizon,
         "thesis": a.thesis, "order_id": a.order_id, "opened_at": now().isoformat(), "opened_day": today_et(),
         "mark": a.entry, "prev_close": None}
    led["positions"].append(p)
    return p


def close_position(led, a):
    sym = a.symbol.upper()
    p = next((p for p in led["positions"] if p["symbol"] == sym), None)
    if p is None:
        raise SystemExit(f"no open position in {sym}")
    for name, v, hi in (("setup", a.setup, 40), ("execution", a.execution, 35), ("outcome", a.outcome, 25)):
        if not 0 <= v <= hi:
            raise SystemExit(f"{name} score must be 0-{hi}")
    risk = p["entry"] - p["stop"]
    pnl = round((a.exit - p["entry"]) * p["qty"], 2)
    day = today_et()
    base = p["entry"] if p["opened_day"] == day or not p.get("prev_close") else p["prev_close"]
    t = {**p, "exit": a.exit, "reason": a.reason, "closed_at": now().isoformat(), "closed_day": day,
         "pnl": pnl, "pnl_today": round((a.exit - base) * p["qty"], 2),
         "r": round((a.exit - p["entry"]) / risk, 2) if risk > 0 else 0.0,
         "score": {"setup": a.setup, "execution": a.execution, "outcome": a.outcome,
                   "total": a.setup + a.execution + a.outcome},
         "note": a.note, "exit_order_id": a.order_id}
    led["positions"].remove(p)
    led["trades"].append(t)
    return t


def record_equity(led, value):
    led["equity"].append({"ts": now().isoformat(), "value": value})
    led["peak"] = max(led.get("peak") or 0.0, value)
    dd = 1 - value / led["peak"] if led["peak"] else 0.0
    if dd >= led["config"]["breaker_drawdown"] and not led["breaker"].get("tripped"):
        led["breaker"] = {"tripped": True, "at": now().isoformat(), "drawdown": round(dd, 4)}
    return round(dd, 4)


def metrics(led):
    tr = led["trades"]
    wins = [t for t in tr if t["pnl"] > 0]
    losses = [t for t in tr if t["pnl"] <= 0]
    rs = [t["r"] for t in tr]
    eq = [e["value"] for e in led["equity"]]
    peak, mdd = 0.0, 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak if peak else 0.0)
    buckets = {"<=-1R": 0, "-1R..0": 0, "0..1R": 0, "1R..2R": 0, ">=2R": 0}
    for r in rs:
        k = "<=-1R" if r <= -1 else "-1R..0" if r <= 0 else "0..1R" if r < 1 else "1R..2R" if r < 2 else ">=2R"
        buckets[k] += 1

    def split(tag):
        sub = [t for t in tr if t.get("tag") == tag]
        return {"n": len(sub), "pnl": round(sum(t["pnl"] for t in sub), 2),
                "avg_r": round(sum(t["r"] for t in sub) / len(sub), 2) if sub else None}

    by_strategy = {}
    for t in tr:
        s = by_strategy.setdefault(t["strategy"], {"n": 0, "pnl": 0.0, "r": 0.0})
        s["n"] += 1
        s["pnl"] = round(s["pnl"] + t["pnl"], 2)
        s["r"] = round(s["r"] + t["r"], 2)
    return {"trades": len(tr), "win_rate": round(len(wins) / len(tr), 3) if tr else None,
            "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else None,
            "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
            "total_pnl": round(sum(t["pnl"] for t in tr), 2), "r_distribution": buckets,
            "max_drawdown": round(mdd, 4),
            "avg_score": round(sum(t["score"]["total"] for t in tr) / len(tr), 1) if tr else None,
            "organic": split("organic"), "forced": split("forced"), "by_strategy": by_strategy}


def status(led):
    cfg = led["config"]
    eq = led["equity"][-1]["value"] if led["equity"] else None
    return {"account": cfg["account_label"], "equity": eq, "peak": led["peak"],
            "drawdown": round(1 - eq / led["peak"], 4) if eq and led["peak"] else None,
            "breaker": led["breaker"], "position_size_now": size(led),
            "open_positions": f"{len(led['positions'])}/{cfg['max_positions']}",
            "daily_pnl": daily_pnl(led), "daily_loss_limit": cfg["daily_loss_limit"],
            "entries_today": f"{entries_today(led)}/{cfg['max_trades_per_day']}",
            "day_trades_last_5_sessions": day_trades_5d(led),
            "entry_blockers": gate(led, "__none__"), "metrics": metrics(led)}


# ---------------------------------------------------------------- dashboard

def render(led):
    data = {"status": status(led), "positions": led["positions"], "trades": led["trades"][-15:][::-1],
            "equity": led["equity"], "regime": led["regime"],
            "generated": now().astimezone(ET).strftime("%Y-%m-%d %H:%M ET")}
    page = TEMPLATE.replace("__DATA__", json.dumps(data).replace("</", "<\\/")) \
                   .replace("__TITLE__", html.escape(led["config"]["account_label"]))
    with open(DASHBOARD, "w") as f:
        f.write(page)
    return DASHBOARD


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trading Dashboard</title>
<!-- Rendered by `python -m trading.bot render` from trading/ledger.json. Do not hand-edit:
     it shows data as of the last render, not real time. Reopen or refresh after each cycle. -->
<style>
:root{color-scheme:dark;--bg:#121211;--surface:#1a1a19;--line:#2a2a28;--text:#f0efec;--text2:#c3c2b7;
--muted:#8f8e86;--accent:#3987e5;--good:#0ca30c;--warn:#fab219;--bad:#d03b3b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 16px 48px}
header{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:baseline;justify-content:space-between;margin-bottom:24px}
h1{font-size:15px;font-weight:600;margin:0;color:var(--text2);letter-spacing:.02em}
.status{display:inline-flex;align-items:center;gap:8px;font-size:13px;color:var(--text2)}
.dot{width:9px;height:9px;border-radius:50%}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:12px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.big{font-size:30px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:4px}
.sub{font-size:13px;color:var(--text2);font-variant-numeric:tabular-nums}
.grid2{display:grid;grid-template-columns:1fr;gap:12px;margin-top:12px}
@media(min-width:860px){.grid2{grid-template-columns:3fr 2fr}}
h2{font-size:13px;font-weight:600;margin:0 0 12px;color:var(--text2)}
.chart{position:relative}.chart svg{display:block}
.tip{position:absolute;pointer-events:none;background:#262624;border:1px solid var(--line);border-radius:6px;
padding:6px 8px;font-size:12px;white-space:nowrap;display:none}
.pos{padding:12px 0;border-top:1px solid var(--line)}.pos:first-of-type{border-top:0;padding-top:0}
.row{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
.sym{font-weight:600}.meta{color:var(--muted);font-size:12px}
.bar{position:relative;height:6px;background:#2a2a28;border-radius:3px;margin:10px 0 4px}
.bar i{position:absolute;top:-3px;width:2px;height:12px;background:var(--text)}
.bar b{position:absolute;top:0;height:6px;border-radius:3px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:7px 8px;border-top:1px solid var(--line);font-size:13px}
th{color:var(--muted);font-weight:500;font-size:12px;border-top:0}
td.n,th.n{text-align:right}.scroll{overflow-x:auto}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.chip{border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:12px;font-variant-numeric:tabular-nums}
.up{color:var(--good)}.down{color:var(--bad)}.empty{color:var(--muted);font-size:13px}
.tag{font-size:11px;border:1px solid var(--line);border-radius:4px;padding:1px 5px;color:var(--text2)}
footer{margin-top:24px;color:var(--muted);font-size:12px}
</style></head><body><div class="wrap">
<header><h1>__TITLE__ · stock framework</h1><span class="status" id="status"></span></header>
<section class="tiles" id="tiles"></section>
<section class="card"><h2>Account equity</h2><div class="chart" id="chart"><div class="tip" id="tip"></div></div></section>
<div class="grid2">
<section class="card"><h2>Open positions</h2><div id="positions"></div></section>
<section class="card"><h2>Market read</h2><div id="regime"></div></section>
</div>
<section class="card" style="margin-top:12px"><h2>Recent closed trades</h2><div class="scroll" id="trades"></div></section>
<footer id="foot"></footer>
</div>
<script>
const D=__DATA__;const S=D.status;
const $=id=>document.getElementById(id);
const usd=v=>v==null?"—":(v<0?"−$":"$")+Math.abs(v).toFixed(2);
const pct=v=>v==null?"—":(v>0?"+":v<0?"−":"")+Math.abs(v).toFixed(2)+"%";
const sign=v=>v>0?"up":v<0?"down":"";
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const today=D.generated.slice(0,10);
const stopHit=D.trades.some(t=>t.closed_day===today&&/stop/i.test(t.reason||""));
let st=["var(--good)","● Flat / healthy"];
if(S.breaker&&S.breaker.tripped) st=["var(--bad)","■ Circuit breaker tripped — entries paused"];
else if(stopHit) st=["var(--bad)","■ Stop hit today"];
else if(D.positions.length) st=["var(--warn)","▲ Positions open — being watched"];
$("status").innerHTML=`<span class="dot" style="background:${st[0]}"></span>${st[1]}`;
const eq=S.equity, first=D.equity.length?D.equity[0].value:null;
const pnlPct=eq&&eq-S.daily_pnl?S.daily_pnl/(eq-S.daily_pnl)*100:null;
const M=S.metrics;
const tiles=[["Equity",usd(eq),first!=null?`since start ${usd(eq-first)}`:"no equity recorded yet"],
["Today's P&L",`<span class="${sign(S.daily_pnl)}">${usd(S.daily_pnl)}</span>`,`${pct(pnlPct)} · limit −$${S.daily_loss_limit.toFixed(2)}`],
["Open positions",S.open_positions,`entries today ${S.entries_today} · size $${S.position_size_now}`],
["Win rate",M.win_rate==null?"—":Math.round(M.win_rate*100)+"%",`${M.trades} closed · avg score ${M.avg_score??"—"}`]];
$("tiles").innerHTML=tiles.map(t=>`<div class="card"><div class="label">${t[0]}</div><div class="big">${t[1]}</div><div class="sub">${t[2]}</div></div>`).join("");
// equity line: single series, crosshair + tooltip; drawn at the container's real width
function drawEquity(){const pts=D.equity.map(e=>({t:new Date(e.ts),v:e.value}));const el=$("chart");
el.querySelectorAll("svg,p").forEach(n=>n.remove());
if(pts.length<2){el.insertAdjacentHTML("beforeend",`<p class="empty">The equity line appears after two cycles have recorded account value.</p>`);return}
const W=Math.max(280,el.clientWidth),H=200,P={l:56,r:12,t:12,b:24};const vs=pts.map(p=>p.v);let lo=Math.min(...vs),hi=Math.max(...vs);
if(hi-lo<1){lo-=.5;hi+=.5}const t0=pts[0].t,t1=pts[pts.length-1].t;
const x=i=>P.l+(W-P.l-P.r)*i/(pts.length-1),y=v=>P.t+(H-P.t-P.b)*(1-(v-lo)/(hi-lo));
const ticks=[lo,(lo+hi)/2,hi];
let s=`<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Account equity over time">`;
s+=ticks.map(v=>`<line x1="${P.l}" x2="${W-P.r}" y1="${y(v)}" y2="${y(v)}" stroke="#2a2a28"/><text x="${P.l-8}" y="${y(v)+4}" fill="#8f8e86" font-size="11" text-anchor="end">$${v.toFixed(2)}</text>`).join("");
s+=`<text x="${P.l}" y="${H-6}" fill="#8f8e86" font-size="11">${t0.toLocaleDateString()}</text><text x="${W-P.r}" y="${H-6}" fill="#8f8e86" font-size="11" text-anchor="end">${t1.toLocaleDateString()}</text>`;
s+=`<polyline fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" points="${pts.map((p,i)=>x(i)+","+y(p.v)).join(" ")}"/>`;
s+=`<line id="xh" y1="${P.t}" y2="${H-P.b}" stroke="#8f8e86" stroke-dasharray="3 3" visibility="hidden"/><circle id="xd" r="4" fill="var(--accent)" stroke="#1a1a19" stroke-width="2" visibility="hidden"/>`;
s+=`<rect x="${P.l}" y="0" width="${W-P.l-P.r}" height="${H}" fill="transparent"/></svg>`;
el.insertAdjacentHTML("afterbegin",s);const svg=el.querySelector("svg"),tip=$("tip");
svg.addEventListener("mousemove",e=>{const r=svg.getBoundingClientRect();const fx=e.clientX-r.left;
const i=Math.max(0,Math.min(pts.length-1,Math.round((fx-P.l)/(W-P.l-P.r)*(pts.length-1))));
const xh=$("xh"),xd=$("xd");xh.setAttribute("x1",x(i));xh.setAttribute("x2",x(i));xh.setAttribute("visibility","visible");
xd.setAttribute("cx",x(i));xd.setAttribute("cy",y(pts[i].v));xd.setAttribute("visibility","visible");
tip.style.display="block";tip.innerHTML=`${pts[i].t.toLocaleString()}<br><b>$${pts[i].v.toFixed(2)}</b>`;
tip.style.left=Math.min(x(i)+12,W-150)+"px";tip.style.top="8px"});
svg.addEventListener("mouseleave",()=>{tip.style.display="none";$("xh").setAttribute("visibility","hidden");$("xd").setAttribute("visibility","hidden")})}
drawEquity();let rt;addEventListener("resize",()=>{clearTimeout(rt);rt=setTimeout(drawEquity,150)});
// positions
$("positions").innerHTML=D.positions.length?D.positions.map(p=>{const px=p.mark??p.entry,u=(px-p.entry)*p.qty;
const span=p.target-p.stop,at=Math.max(0,Math.min(1,(px-p.stop)/span)),en=(p.entry-p.stop)/span;
return `<div class="pos"><div class="row"><span><span class="sym">${esc(p.symbol)}</span> <span class="tag">${esc(p.strategy)} · ${esc(p.tier)}</span></span>
<span class="${sign(u)}">${usd(u)}</span></div>
<div class="row meta"><span>entry ${usd(p.entry)} · now ${usd(px)} · ${(+p.qty).toFixed(4)} sh</span><span>opened ${esc(p.opened_day)} · horizon ${p.horizon_days}d</span></div>
<div class="bar" title="stop ${usd(p.stop)} → target ${usd(p.target)}"><b style="left:0;width:${en*100}%;background:#3a2222"></b><b style="left:${en*100}%;width:${(1-en)*100}%;background:#1f3324"></b><i style="left:calc(${at*100}% - 1px)"></i></div>
<div class="row meta"><span>stop ${usd(p.stop)} (${usd((px-p.stop)*p.qty)} away)</span><span>target ${usd(p.target)} (${usd((p.target-px)*p.qty)} away)</span></div>
<div class="meta" style="margin-top:6px">${esc(p.thesis)}</div></div>`}).join(""):`<p class="empty">No open positions.</p>`;
// regime
const R=D.regime||{};$("regime").innerHTML=(R.chips&&R.chips.length?`<div class="chips">${R.chips.map(c=>`<span class="chip"><span class="${sign(c.chg)}">${c.chg>0?"▲":c.chg<0?"▼":"■"}</span> ${esc(c.label)} ${pct(c.chg)}</span>`).join("")}</div>`:"")+
(R.read?`<p style="margin:0">${esc(R.read)}</p><p class="meta">${esc(R.ts||"")}</p>`:`<p class="empty">No market read recorded yet.</p>`);
// trades
$("trades").innerHTML=D.trades.length?`<table><thead><tr><th>Symbol</th><th>Strategy</th><th class="n">Entry</th><th class="n">Exit</th><th class="n">P&amp;L</th><th class="n">R</th><th class="n">Score</th><th>Tag</th><th>Closed</th></tr></thead><tbody>${D.trades.map(t=>`<tr><td class="sym">${esc(t.symbol)}</td><td>${esc(t.strategy)}</td><td class="n">${usd(t.entry)}</td><td class="n">${usd(t.exit)}</td><td class="n ${sign(t.pnl)}">${usd(t.pnl)}</td><td class="n">${t.r.toFixed(2)}</td><td class="n">${t.score.total}</td><td><span class="tag">${esc(t.tag)}</span></td><td>${esc(t.closed_day)}</td></tr>`).join("")}</tbody></table>`:`<p class="empty">No closed trades yet.</p>`;
$("foot").textContent=`Rendered ${D.generated} from trading/ledger.json. Static snapshot: refresh after the next cycle re-renders it.`;
</script></body></html>
"""


# ---------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(prog="trading.bot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("files", nargs="+")
    s = sub.add_parser("trend"); s.add_argument("file")
    s = sub.add_parser("check"); s.add_argument("files", nargs="+")
    s = sub.add_parser("open")
    s.add_argument("symbol"); s.add_argument("--strategy", required=True, choices=["mean_reversion", "momentum"])
    s.add_argument("--tier", required=True); s.add_argument("--entry", type=float, required=True)
    s.add_argument("--qty", type=float, required=True); s.add_argument("--stop", type=float, required=True)
    s.add_argument("--target", type=float, required=True); s.add_argument("--horizon", type=int, required=True)
    s.add_argument("--thesis", required=True); s.add_argument("--order-id"); s.add_argument("--forced", action="store_true")
    s = sub.add_parser("close")
    s.add_argument("symbol"); s.add_argument("--exit", type=float, required=True); s.add_argument("--reason", required=True)
    s.add_argument("--setup", type=int, required=True); s.add_argument("--execution", type=int, required=True)
    s.add_argument("--outcome", type=int, required=True); s.add_argument("--note", required=True); s.add_argument("--order-id")
    s = sub.add_parser("skip")
    s.add_argument("symbol"); s.add_argument("--strategy", required=True); s.add_argument("--why", required=True)
    s.add_argument("--price", type=float)
    s = sub.add_parser("equity"); s.add_argument("value", type=float)
    s = sub.add_parser("regime"); s.add_argument("read")
    sub.add_parser("status"); sub.add_parser("render"); sub.add_parser("gate").add_argument("symbol")
    a = ap.parse_args(argv)

    led = load()
    if a.cmd == "scan":
        out = scan(quotes_from(a.files))
        led["regime"] = {**led.get("regime", {}), "chips": out["regime"], "chips_ts": now().isoformat()}
        save(led)
    elif a.cmd == "trend":
        out = {sym: trend(b) for sym, b in bars_from(a.file).items()}
    elif a.cmd == "check":
        out = {"positions": check(led, quotes_from(a.files)), "daily_pnl": daily_pnl(led)}
        save(led)
    elif a.cmd == "gate":
        out = {"symbol": a.symbol.upper(), "blockers": gate(led, a.symbol.upper()), "size": size(led)}
    elif a.cmd == "open":
        out = open_position(led, a); save(led)
    elif a.cmd == "close":
        out = close_position(led, a); save(led)
    elif a.cmd == "skip":
        out = {"ts": now().isoformat(), "symbol": a.symbol.upper(), "strategy": a.strategy, "why": a.why, "price": a.price}
        led["skips"].append(out); led["skips"] = led["skips"][-500:]; save(led)
    elif a.cmd == "equity":
        out = {"drawdown": record_equity(led, a.value), "breaker": led["breaker"]}; save(led)
    elif a.cmd == "regime":
        led["regime"] = {**led.get("regime", {}), "read": a.read, "ts": now().astimezone(ET).strftime("%Y-%m-%d %H:%M ET")}
        save(led); out = led["regime"]
    elif a.cmd == "status":
        out = status(led)
    elif a.cmd == "render":
        out = {"dashboard": render(led)}
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    sys.exit(main())
