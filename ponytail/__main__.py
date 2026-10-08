"""python -m ponytail [--paper] — run one trading cycle (schedule with cron)."""
import asyncio
import dataclasses
import logging
import sys

from dotenv import load_dotenv

from .agent import run_cycle
from .config import Config


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    if "--paper" in sys.argv:
        cfg = dataclasses.replace(cfg, live_trading=False)
    _, result = asyncio.run(run_cycle(cfg))
    sys.exit(1 if result is None or result.is_error else 0)


if __name__ == "__main__":
    main()
