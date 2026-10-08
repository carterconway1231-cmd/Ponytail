"""Paper-trade driver: run one agent cycle step by step from saved Robinhood
responses, through the same hooks the autonomous agent uses.

For hosts where the Robinhood MCP is only reachable from an outer Claude
session (e.g. a claude.ai connector) and not from the SDK subprocess. Each
step is one process; the per-run market cache and approved plans persist in
RUN_DIR between steps, positions and learning in STATE_PATH as usual.

  python -m ponytail.drive start                      # new cycle (fresh cache, plans)
  python -m ponytail.drive ingest TOOL RESPONSE.json [INPUT_JSON]
  python -m ponytail.drive call METHOD [ARGS_JSON]    # rank_credit_spreads, propose_credit_spread,
                                                      # review_positions, portfolio_status, performance
  python -m ponytail.drive order ORDER_JSON           # place_option_order (paper-intercepted)

Paper only: refuses to run with LIVE_TRADING=true, so nothing here can reach
a real order.
"""
import asyncio
import json
import os
import pickle
import sys

from dotenv import load_dotenv

from .agent import RH, TradingSession
from .config import Config

RUN_DIR = os.environ.get("PONYTAIL_RUN_DIR", ".ponytail_run")
CACHE = os.path.join(RUN_DIR, "cycle.pkl")
CALLABLE = {"rank_credit_spreads", "propose_credit_spread", "review_positions", "portfolio_status",
            "performance", "rank_contracts", "propose_option_trade", "compute_signals"}


def _session(cfg, fresh=False):
    s = TradingSession(cfg)
    if not fresh and os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            saved = pickle.load(f)
        if saved["day"] == s.today.isoformat():
            for k in ("market", "plans", "signals", "cancelable", "exit_reasons", "events"):
                setattr(s, k, saved[k])
    return s


def _save(s):
    os.makedirs(RUN_DIR, exist_ok=True)
    with open(CACHE, "wb") as f:
        pickle.dump({"day": s.today.isoformat(), **{k: getattr(s, k) for k in (
            "market", "plans", "signals", "cancelable", "exit_reasons", "events")}}, f)
    s.state.save()


def main(argv=None):
    load_dotenv()
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        raise SystemExit(__doc__)
    cfg = Config.from_env()
    if cfg.live_trading:
        raise SystemExit("drive is paper-only; unset LIVE_TRADING")
    cmd, args = argv[0], argv[1:]
    s = _session(cfg, fresh=cmd == "start")
    if cmd == "start":
        out = {"mode": s.mode, "strategy": cfg.strategy, "today": s.today.isoformat(),
               "entry_window_open": s.entry_window_open(), "positions": len(s.state.positions)}
    elif cmd == "ingest":
        tool, path = args[0], args[1]
        tool_input = json.loads(args[2]) if len(args) > 2 else {}
        with open(path) as f:
            resp = f.read()
        asyncio.run(s.post_tool_use({"tool_name": RH + tool, "tool_input": tool_input, "tool_response": resp},
                                    "drive", None))
        m = s.market
        out = {"ingested": tool, "bars": {k: {i: len(v) for i, v in b.items()} for k, b in m.bars.items()},
               "instruments": len(m.instruments), "quotes": len(m.quotes), "equity": m.equity,
               "broker_positions": m.broker_positions}
    elif cmd == "call":
        method = args[0]
        if method not in CALLABLE:
            raise SystemExit(f"unknown method {method}; one of {sorted(CALLABLE)}")
        kwargs = json.loads(args[1]) if len(args) > 1 else {}
        out = getattr(s, method)(**kwargs)
    elif cmd == "order":
        order = json.loads(args[0])
        res = asyncio.run(s.pre_tool_use({"tool_name": RH + "place_option_order", "tool_input": order}, "drive", None))
        out = {"decision": (res.get("hookSpecificOutput") or {}).get("permissionDecisionReason", res)}
    else:
        raise SystemExit(__doc__)
    _save(s)
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
