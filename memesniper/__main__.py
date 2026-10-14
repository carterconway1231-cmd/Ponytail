"""CLI: python -m memesniper {run,status,check,journal,stop,resume}"""
import argparse
import asyncio
import json
import logging
import os
import sys
import time

from .config import load_settings


def build(settings):
    from .agent import EntryAgent
    from .engine import Sniper
    from .executor import LiveExecutor, PaperExecutor
    from .feeds import DexScreenerFeed, PumpPortalHub
    from .jupiter import Jupiter
    from .net import SolanaRpc, make_client
    from .creator import CreatorProfiler
    from .notify import Notifier
    from .outcomes import OutcomeTracker
    from .risk import RiskManager
    from .safety import RugCheck
    from .state import State

    http = make_client()
    state = State(settings.state_path)
    rpc = SolanaRpc(http, settings.rpc_url)
    jup = Jupiter(http, settings.jupiter_url, settings.jupiter_api_key)
    dex = DexScreenerFeed(http, settings.dexscreener_url, settings.dexscreener_poll_s)
    hub = PumpPortalHub(settings.pumpportal_ws) if "pumpportal" in settings.sources else None
    executor = LiveExecutor(settings, jup, rpc) if settings.live else PaperExecutor(settings, jup)
    sniper = Sniper(settings, state=state, risk=RiskManager(settings, state), rpc=rpc, jupiter=jup,
                    rugcheck=RugCheck(http, settings.rugcheck_url), dexscreener=dex,
                    agent=EntryAgent(settings), executor=executor, hub=hub, http=http,
                    notifier=Notifier(settings, http),
                    creators=CreatorProfiler(settings, state, rpc, http),
                    outcomes=OutcomeTracker(settings, state, jup))
    feeds = [f for f in (hub, dex if "dexscreener" in settings.sources else None) if f]
    return sniper, feeds


def cmd_run(args):
    overrides = {"mode": "live"} if args.live else {}
    s = load_settings(**overrides)
    if s.live:
        if os.environ.get("MEMESNIPER_MODE") != "live" or not args.live:
            sys.exit("live trading needs BOTH MEMESNIPER_MODE=live and --live")
        print(f"LIVE MODE: up to {s.max_sol_per_trade} SOL/trade, {s.max_daily_spend_sol} SOL/day, "
              f"{s.max_daily_loss_sol} SOL daily loss limit. Ctrl-C now to abort.", file=sys.stderr)
        time.sleep(5)
    sniper, feeds = build(s)
    logging.getLogger("memesniper").info(
        "memesniper %s | sources=%s | model=%s effort=%s", s.mode, ",".join(s.sources), s.model, s.effort)
    try:
        asyncio.run(sniper.run(feeds))
    except KeyboardInterrupt:
        pass


def cmd_status(args):
    from .risk import RiskManager
    from .state import State, day_start
    s = load_settings()
    st = State(s.state_path)
    out = {
        "mode": s.mode,
        "halted": RiskManager(s, st).halted(),
        "open_positions": [p.to_dict() for p in st.open_positions()],
        "today": {"spent_sol": round(st.spent_today(), 4),
                  "realized_pnl_sol": round(st.realized_pnl_today(), 4),
                  "claude_usd": round(st.claude_spend_today(), 4),
                  "decisions": st.decision_stats(day_start())},
        "recent_closed": [{k: r[k] for k in ("symbol", "entry_sol", "realized_sol", "exit_reason")}
                          for r in st.closed_positions(10)],
    }
    print(json.dumps(out, indent=2, default=str))


def cmd_journal(args):
    from .state import State
    st = State(load_settings().state_path)
    rows = st.db.execute("SELECT ts, symbol, mint, action, confidence, size_sol, reasoning, "
                         "executed FROM decisions ORDER BY id DESC LIMIT ?", (args.n,)).fetchall()
    for r in rows:
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"]))
        print(f"{when} {r['action']:6} {r['symbol'] or r['mint'][:8]:12} c={r['confidence']:.2f} "
              f"size={r['size_sol']:.3f}{' EXEC' if r['executed'] else ''} | {r['reasoning'][:140]}")


def cmd_check(args):
    """Screen one mint (and optionally ask Claude) without trading."""
    from .models import Launch
    s = load_settings()

    async def go():
        sniper, _ = build(s)
        from .safety import screen
        launch = Launch(mint=args.mint, source="manual")
        report = await screen(launch, s, sniper.rpc, sniper.jup, sniper.rugcheck, sniper.dex)
        out = {"passed": report.passed, "hard_fails": report.hard_fails,
               "warnings": report.warnings, "metrics": report.metrics}
        if args.ask and report.passed:
            d = await sniper.agent.decide(launch, report, sniper.portfolio_summary())
            out["claude"] = d.__dict__
        print(json.dumps(out, indent=2, default=str))
    asyncio.run(go())


def cmd_outcomes(args):
    """What happened to tokens after each decision: the data for tuning filters and Claude."""
    from .outcomes import build_report, format_report
    from .state import State
    st = State(load_settings().state_path)
    since = time.time() - args.days * 86_400 if args.days else 0
    rep = build_report(st.outcome_rows(since))
    print(json.dumps(rep, indent=2) if args.json else format_report(rep))


def cmd_creator(args):
    """Creator history for a wallet: what the bot has seen locally plus pump.fun and wallet age."""
    from .creator import CreatorProfiler
    from .net import SolanaRpc, make_client
    from .state import State
    s = load_settings()
    st = State(s.state_path)

    async def go():
        http = make_client()
        prof = CreatorProfiler(s, st, SolanaRpc(http, s.rpc_url), http)
        out = {"local": prof.local(args.wallet, ""), "remote": await prof.remote(args.wallet, "")}
        print(json.dumps(out, indent=2))
    asyncio.run(go())


def cmd_test_alert(args):
    from .net import make_client
    from .notify import Notifier
    s = load_settings()

    async def go():
        n = Notifier(s, make_client())
        if not n.enabled:
            sys.exit("no alert channel set: MEMESNIPER_NTFY_TOPIC, MEMESNIPER_TELEGRAM_BOT_TOKEN"
                     "+MEMESNIPER_TELEGRAM_CHAT_ID, or MEMESNIPER_WEBHOOK_URL")
        await n.send("memesniper test", f"Alerts are working ({s.mode} mode).", event="test")
        print("test alert sent")
    asyncio.run(go())


def cmd_wallet(args):
    """Show which wallet the bot trades from, so you can match it to the FOMO app."""
    s = load_settings()
    if not s.private_key:
        sys.exit("no MEMESNIPER_PRIVATE_KEY set (paper mode needs none)")
    from solders.keypair import Keypair
    from .net import SolanaRpc, make_client
    pubkey = str(Keypair.from_base58_string(s.private_key).pubkey())
    print(f"address: {pubkey}")

    async def go():
        try:
            print(f"balance: {await SolanaRpc(make_client(), s.rpc_url).balance_sol(pubkey):.4f} SOL")
        except Exception as e:
            print(f"balance: unavailable ({e})")
    asyncio.run(go())


def cmd_stop(args):
    s = load_settings()
    open(s.kill_switch_path, "w").write(time.ctime())
    print(f"kill switch set ({s.kill_switch_path}): no new entries; exits keep running")


def cmd_resume(args):
    s = load_settings()
    if os.path.exists(s.kill_switch_path):
        os.remove(s.kill_switch_path)
    print("kill switch cleared")


def main(argv=None):
    p = argparse.ArgumentParser(prog="memesniper", description="Claude-gated Solana meme coin sniper")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="start sniping (paper unless --live)")
    r.add_argument("--live", action="store_true", help="real trades; also needs MEMESNIPER_MODE=live")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    j = sub.add_parser("journal", help="recent decisions")
    j.add_argument("-n", type=int, default=30)
    j.set_defaults(fn=cmd_journal)
    c = sub.add_parser("check", help="safety-screen one mint")
    c.add_argument("mint")
    c.add_argument("--ask", action="store_true", help="also ask Claude (costs one API call)")
    c.set_defaults(fn=cmd_check)
    o = sub.add_parser("outcomes", help="how tokens did after each decision (tuning data)")
    o.add_argument("--days", type=float, default=0, help="only the last N days (default: all)")
    o.add_argument("--json", action="store_true")
    o.set_defaults(fn=cmd_outcomes)
    cr = sub.add_parser("creator", help="creator history for a wallet")
    cr.add_argument("wallet")
    cr.set_defaults(fn=cmd_creator)
    sub.add_parser("test-alert", help="send a test phone alert").set_defaults(fn=cmd_test_alert)
    sub.add_parser("wallet", help="show the bot's wallet address and SOL balance").set_defaults(fn=cmd_wallet)
    sub.add_parser("stop", help="kill switch: halt new entries").set_defaults(fn=cmd_stop)
    sub.add_parser("resume", help="clear the kill switch").set_defaults(fn=cmd_resume)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "websockets", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    args.fn(args)


if __name__ == "__main__":
    main()
