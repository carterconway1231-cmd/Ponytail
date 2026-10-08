"""python -m ponytail [--paper] [--monitor] [--report]

  (no flags)  full trading cycle (signals, entries, exits) on AGENT_MODEL
  --monitor   protect/exit held positions only, on the cheap MONITOR_MODEL
  --paper     force paper mode even if LIVE_TRADING=true
  --report    print the performance scoreboard and go-live checklist; no Claude call

LIVE_TRADING=true is only honored once paper results pass the go-live
checklist (see performance.py), unless FORCE_LIVE=true.
"""
import asyncio
import dataclasses
import json
import logging
import sys

from dotenv import load_dotenv

from . import alerts, performance
from .agent import run_cycle
from .config import Config
from .state import State

log = logging.getLogger("ponytail")


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    if "--report" in sys.argv:
        state = State.load(cfg.state_path)
        print(json.dumps({"paper": performance.report(state, cfg.paper_capital, mode="paper"),
                          "live": performance.report(state, cfg.paper_capital, mode="live"),
                          "go_live": performance.go_live_check(cfg, state),
                          "exit_params": state.data.get("exit_params")}, indent=1))
        return
    if "--paper" in sys.argv:
        cfg = dataclasses.replace(cfg, live_trading=False)
    if cfg.live_trading and not cfg.force_live:
        gate = performance.go_live_check(cfg, State.load(cfg.state_path))
        if not gate["ready"]:
            failing = [f"{c['check']} {c['value']} (need {c['need']})" for c in gate["checks"] if not c["ok"]]
            msg = "LIVE_TRADING requested but paper results don't pass the go-live checklist; running PAPER. " \
                  + "; ".join(failing)
            log.warning(msg)
            alerts.send(cfg.alert_webhook_url, f"[ponytail] go-live blocked: {'; '.join(failing)}")
            cfg = dataclasses.replace(cfg, live_trading=False)
    _, result = asyncio.run(run_cycle(cfg, monitor="--monitor" in sys.argv))
    sys.exit(1 if result is None or result.is_error else 0)


if __name__ == "__main__":
    main()
