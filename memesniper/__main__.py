"""CLI: python -m memesniper {run,status,journal,outcomes,creator,check,wallet,test-alert,stop,resume}"""
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
    from .chains import ChainCtx, spec
    from .creator import CreatorProfiler
    from .engine import Sniper
    from .evm import EvmLiveExecutor, EvmPaperExecutor, EvmRpc, KyberRouter
    from .evm_safety import GoPlus, HoneypotIs
    from .executor import LiveExecutor, PaperExecutor
    from .feeds import DexScreenerFeed, GeckoTerminalFeed, PumpPortalHub
    from .jupiter import Jupiter
    from .net import SolanaRpc, make_client
    from .notify import Notifier
    from .outcomes import OutcomeTracker
    from .prices import NativePrices
    from .risk import RiskManager
    from .safety import RugCheck
    from .state import State
    from .watchlist import Watchlist

    http = make_client()
    state = State(settings.state_path)
    prices = NativePrices(http, settings.dexscreener_url)
    dex = DexScreenerFeed(http, settings.dexscreener_url, settings.dexscreener_poll_s, settings.chains)
    goplus, honeypot = GoPlus(http, settings.goplus_url), HoneypotIs(http, settings.honeypot_url)

    chains, sol_rpc = {}, SolanaRpc(http, settings.rpc_for("solana"))
    for name in settings.chains:
        cs = spec(name)
        if cs.is_evm:
            router = KyberRouter(http, cs, settings.kyberswap_url)
            rpc = EvmRpc(http, settings.rpc_for(name))
            ex = (EvmLiveExecutor(settings, router, rpc, settings.evm_private_key) if settings.live
                  else EvmPaperExecutor(settings, router, prices))
            chains[name] = ChainCtx(cs, router, ex, rpc=rpc, goplus=goplus, honeypot=honeypot)
        else:
            router = Jupiter(http, settings.jupiter_url, settings.jupiter_api_key)
            ex = LiveExecutor(settings, router, sol_rpc) if settings.live else PaperExecutor(settings, router)
            chains[name] = ChainCtx(cs, router, ex, rpc=sol_rpc,
                                    rugcheck=RugCheck(http, settings.rugcheck_url))

    hub = (PumpPortalHub(settings.pumpportal_ws)
           if "pumpportal" in settings.sources and "solana" in chains else None)
    sniper = Sniper(settings, state=state, risk=RiskManager(settings, state), chains=chains,
                    dexscreener=dex, agent=EntryAgent(settings), prices=prices, hub=hub, http=http,
                    notifier=Notifier(settings, http),
                    creators=CreatorProfiler(settings, state, sol_rpc, http),
                    outcomes=OutcomeTracker(settings, state, {c: x.router for c, x in chains.items()}, prices),
                    watchlist=Watchlist(settings, state, dex))
    feeds = [hub] if hub else []
    if "dexscreener" in settings.sources:
        feeds.append(dex)
    if "geckoterminal" in settings.sources:
        feeds.append(GeckoTerminalFeed(http, settings.geckoterminal_url, settings.geckoterminal_poll_s,
                                       settings.chains))
    return sniper, feeds


def cmd_run(args):
    overrides = {"mode": "live"} if args.live else {}
    s = load_settings(**overrides)
    if s.live:
        if os.environ.get("MEMESNIPER_MODE") != "live" or not args.live:
            sys.exit("live trading needs BOTH MEMESNIPER_MODE=live and --live")
        print(f"LIVE MODE on {', '.join(s.chains)}: up to ${s.max_usd_per_trade:.0f}/trade, "
              f"${s.max_daily_spend_usd:.0f}/day, ${s.max_daily_loss_usd:.0f} daily loss limit. "
              "Ctrl-C now to abort.", file=sys.stderr)
        time.sleep(5)
    sniper, feeds = build(s)
    logging.getLogger("memesniper").info(
        "memesniper %s | chains=%s | strategies=%s | sources=%s | model=%s effort=%s", s.mode,
        ",".join(s.chains), ",".join(s.strategies), ",".join(s.sources), s.model, s.effort)
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
        "today": {"spent_usd": round(st.spent_today(), 2),
                  "realized_pnl_usd": round(st.realized_pnl_today(), 2),
                  "claude_usd": round(st.claude_spend_today(), 4),
                  "decisions": st.decision_stats(day_start())},
        "recent_closed": [{k: r[k] for k in ("symbol", "chain", "strategy", "entry_usd", "realized_usd",
                                             "exit_reason")} for r in st.closed_positions(10)],
        "watchlist_size": len(st.watched()),
    }
    print(json.dumps(out, indent=2, default=str))


def cmd_journal(args):
    from .state import State
    st = State(load_settings().state_path)
    rows = st.db.execute("SELECT ts, symbol, mint, chain, strategy, action, confidence, size_usd, "
                         "reasoning, executed FROM decisions ORDER BY id DESC LIMIT ?", (args.n,)).fetchall()
    for r in rows:
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"]))
        print(f"{when} {r['chain'][:4]:4} {r['strategy'][:8]:8} {r['action']:6} "
              f"{r['symbol'] or r['mint'][:8]:12} c={r['confidence']:.2f} ${r['size_usd'] or 0:6.2f}"
              f"{' EXEC' if r['executed'] else ''} | {(r['reasoning'] or '')[:130]}")


def cmd_check(args):
    """Screen one mint (and optionally ask Claude) without trading."""
    from .models import Launch
    s = load_settings()

    async def go():
        sniper, _ = build(s)
        if args.chain not in sniper.chains:
            sys.exit(f"chain {args.chain} is not enabled (MEMESNIPER_CHAINS)")
        launch = Launch(mint=args.mint, source="manual", chain=args.chain)
        report = await sniper._screen(launch)
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
    """Show which wallets the bot trades from, so you can match them to the FOMO app."""
    from .chains import spec
    from .evm import EvmRpc
    from .net import SolanaRpc, make_client
    s = load_settings()
    if not s.private_key and not s.evm_private_key:
        sys.exit("no MEMESNIPER_PRIVATE_KEY or MEMESNIPER_EVM_PRIVATE_KEY set (paper mode needs none)")

    async def go():
        http = make_client()
        if s.private_key:
            from solders.keypair import Keypair
            pubkey = str(Keypair.from_base58_string(s.private_key).pubkey())
            print(f"solana address: {pubkey}")
            try:
                print(f"  balance: {await SolanaRpc(http, s.rpc_for('solana')).balance_sol(pubkey):.4f} SOL")
            except Exception as e:
                print(f"  balance: unavailable ({e})")
        if s.evm_private_key:
            from eth_account import Account
            addr = Account.from_key(s.evm_private_key).address
            print(f"evm address (Base, BSC, Ethereum): {addr}")
            for name in s.chains:
                cs = spec(name)
                if not cs.is_evm:
                    continue
                try:
                    bal = await EvmRpc(http, s.rpc_for(name)).balance(addr) / 10 ** cs.native_decimals
                    print(f"  {name}: {bal:.5f} {cs.native}")
                except Exception as e:
                    print(f"  {name}: unavailable ({e})")
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
    c.add_argument("mint", help="token address")
    c.add_argument("--chain", default="solana", help="solana | base | bsc | ethereum")
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
