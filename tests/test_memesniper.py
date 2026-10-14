import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from memesniper import exits
from memesniper.agent import EntryAgent
from memesniper.chains import CHAINS, ChainCtx
from memesniper.config import Settings, load_settings
from memesniper.engine import Sniper
from memesniper.executor import PaperExecutor
from memesniper.feeds import launch_from_pumpportal, summarize_ticks, tick_from_pumpportal
from memesniper.models import Decision, Launch, Observation, Position, SafetyReport, TradeTick
from memesniper.risk import RiskManager
from memesniper.safety import TOKEN_2022, check_holders, check_mint, check_roundtrip, screen
from memesniper.state import State

MINT = "Mint111111111111111111111111111111111111111"
CREATOR = "Dev1111111111111111111111111111111111111111"
CURVE = "Curve11111111111111111111111111111111111111"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def settings(tmp_path):
    return Settings(state_path=str(tmp_path / "s.db"), kill_switch_path=str(tmp_path / "STOP"),
                    priority_fee_lamports=0, paper_extra_slippage_pct=0.0, min_unique_buyers=3,
                    chains=("solana",))


SOL_USD = 200.0          # so the default $10 trade is 0.05 SOL
LADDER = [(1.0, 0.5), (2.0, 0.5)]


@pytest.fixture
def state(settings):
    return State(settings.state_path)


# --- fakes ----------------------------------------------------------------------

class FakeJupiter:
    """Constant-product-free toy market: price in SOL per raw token, settable per mint."""
    spec = CHAINS["solana"]

    def __init__(self, price=1e-6, sell_haircut=0.03):
        self.price = {MINT: price}
        self.sell_haircut = sell_haircut
        self.routable = True

    async def buy_quote(self, mint, sol, slippage_bps):
        if not self.routable:
            return None
        return {"inAmount": str(int(sol * 1e9)), "outAmount": str(int(sol / self.price[mint])),
                "priceImpactPct": "0.01"}

    async def sell_quote(self, mint, tokens_raw, slippage_bps):
        if not self.routable:
            return None
        return {"outAmount": str(int(tokens_raw * self.price[mint] * (1 - self.sell_haircut) * 1e9))}

    async def value_native(self, mint, tokens_raw, slippage_bps):
        q = await self.sell_quote(mint, tokens_raw, slippage_bps)
        return int(q["outAmount"]) / 1e9 if q else None


class FakePrices:
    def __init__(self, prices=None):
        self.prices = prices or {"solana": SOL_USD, "base": 3000.0, "bsc": 600.0, "ethereum": 3000.0}

    async def usd(self, chain):
        return self.prices.get(chain)


class FakeRpc:
    def __init__(self, mint_authority=None, freeze_authority=None, holders=None):
        self.info = {"decimals": 6, "supply": str(1_000_000_000 * 10**6),
                     "mintAuthority": mint_authority, "freezeAuthority": freeze_authority,
                     "program": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}
        self.holders = holders if holders is not None else [
            {"address": CURVE, "owner": CURVE, "uiAmount": 800_000_000},
            {"address": "a1", "owner": "w1", "uiAmount": 20_000_000},
            {"address": "a2", "owner": CREATOR, "uiAmount": 10_000_000},
        ]

    async def mint_info(self, mint):
        return self.info

    async def largest_holders(self, mint):
        return self.holders


class FakeRugcheck:
    def __init__(self, score=100):
        self.score = score

    async def summary(self, mint):
        return {"score_normalised": self.score, "risks": []}


class FakeDex:
    def __init__(self, snaps=None):
        self.snaps = snaps or {}           # token_lower -> list of snapshots, served in order

    async def pair_info(self, mint, chain="solana"):
        return None

    async def snapshots(self, chain, tokens):
        return {t.lower(): self.snaps[t.lower()].pop(0) for t in tokens if self.snaps.get(t.lower())}


class FakeAgent:
    def __init__(self, decision):
        self.decision, self.calls = decision, 0

    async def decide(self, launch, report, portfolio, spent_today_usd=0.0, strategy="launch",
                     signal=None):
        self.calls += 1
        self.last = (strategy, signal)
        return self.decision


class FakeHub:
    def __init__(self, obs):
        self.obs = obs

    async def observe(self, launch, seconds):
        return self.obs


def good_obs():
    return Observation(seconds=30, buys=20, sells=3, unique_buyers=15, buy_sol=6, sell_sol=0.5,
                       start_mcap_sol=28, end_mcap_sol=45)


def pump_launch():
    return Launch(mint=MINT, source="pumpportal", name="Frog", symbol="FROG", creator=CREATOR,
                  bonding_curve=CURVE, market_cap_sol=28)


def sol_ctx(settings, jup, rpc=None):
    return ChainCtx(CHAINS["solana"], jup, PaperExecutor(settings, jup), rpc=rpc or FakeRpc(),
                    rugcheck=FakeRugcheck())


def make_sniper(settings, state, decision, jup=None, rpc=None, obs=None, dex=None):
    jup = jup or FakeJupiter()
    agent = FakeAgent(decision)
    s = Sniper(settings, state=state, risk=RiskManager(settings, state),
               chains={"solana": sol_ctx(settings, jup, rpc)}, dexscreener=dex or FakeDex(),
               agent=agent, prices=FakePrices(), hub=FakeHub(obs or good_obs()))
    return s, agent, jup


BUY = Decision("BUY", confidence=0.8, size_usd=100.0, take_profit_pct=100, stop_loss_pct=50,
               max_hold_minutes=999, reasoning="organic buyers")


# --- feeds ----------------------------------------------------------------------

def test_pumpportal_messages_parse_into_launches_and_ticks():
    create = {"txType": "create", "mint": MINT, "name": "Frog", "symbol": "FROG",
              "traderPublicKey": CREATOR, "solAmount": 1.5, "initialBuy": 5e7,
              "marketCapSol": 30, "bondingCurveKey": CURVE, "uri": "ipfs://x", "pool": "pump"}
    l = launch_from_pumpportal(create)
    assert (l.symbol, l.creator, l.bonding_curve, l.dev_initial_tokens) == ("FROG", CREATOR, CURVE, 5e7)
    assert launch_from_pumpportal({"txType": "migrate", "mint": MINT, "pool": "pump-amm"}).source \
        == "pumpportal-migration"
    assert launch_from_pumpportal({"message": "subscribed"}) is None
    t = tick_from_pumpportal({"txType": "sell", "mint": MINT, "traderPublicKey": "x",
                              "solAmount": 0.2, "tokenAmount": 1000})
    assert t.side == "sell" and t.sol == 0.2


def test_observation_counts_distinct_buyers_and_dev_selling():
    l = pump_launch()
    l.dev_initial_tokens = 1000
    ticks = [TradeTick(MINT, "buy", "a", 0.5, 10, 31), TradeTick(MINT, "buy", "a", 0.5, 10, 32),
             TradeTick(MINT, "buy", "b", 2.0, 10, 40), TradeTick(MINT, "sell", CREATOR, 1.0, 600, 35)]
    o = summarize_ticks(l, ticks, 30)
    assert (o.buys, o.sells, o.unique_buyers) == (3, 1, 2)
    assert o.dev_sold_pct == 60
    assert o.largest_buy_sol == 2.0 and o.end_mcap_sol == 35


# --- safety ---------------------------------------------------------------------

def test_live_mint_or_freeze_authority_is_a_hard_fail(settings):
    r = SafetyReport(MINT)
    check_mint(r, {"decimals": 6, "supply": "1", "mintAuthority": "X", "freezeAuthority": "Y"}, settings)
    assert not r.passed and len(r.hard_fails) == 2


def test_token2022_transfer_fee_is_a_hard_fail(settings):
    r = SafetyReport(MINT)
    check_mint(r, {"decimals": 6, "supply": "1", "program": TOKEN_2022,
                   "extensions": [{"extension": "transferFeeConfig"}, {"extension": "metadataPointer"}]},
               settings)
    assert not r.passed and "transferFeeConfig" in r.hard_fails[0]


def test_holder_check_ignores_bonding_curve_but_catches_whales(settings):
    r = SafetyReport(MINT)
    r.metrics.update(decimals=6, supply_raw=1_000_000_000 * 10**6)
    check_holders(r, FakeRpc().holders, pump_launch(), settings)
    assert r.passed and r.metrics["top10_holder_pct"] == 3.0 and r.metrics["creator_holding_pct"] == 1.0

    whale = FakeRpc().holders + [{"address": "a3", "owner": "w3", "uiAmount": 400_000_000}]
    r2 = SafetyReport(MINT)
    r2.metrics.update(decimals=6, supply_raw=1_000_000_000 * 10**6)
    check_holders(r2, whale, pump_launch(), settings)
    assert not r2.passed


def test_roundtrip_detects_honeypot_and_sell_tax(settings):
    r = SafetyReport(MINT)
    check_roundtrip(r, {"outAmount": "1000"}, None, 0.05, settings)
    assert "honeypot" in r.hard_fails[0]
    r = SafetyReport(MINT)
    check_roundtrip(r, {"outAmount": "1000"}, {"outAmount": str(int(0.03 * 1e9))}, 0.05, settings)
    assert not r.passed and r.metrics["roundtrip_loss_pct"] == 40


def test_screen_passes_clean_token_and_short_circuits_thin_launch(settings):
    l = pump_launch()
    ctx = sol_ctx(settings, FakeJupiter())
    ok = run(screen(l, settings, ctx, FakeDex(), good_obs(), trade_native=0.05, trade_usd=10))
    assert ok.passed, ok.hard_fails
    thin = Observation(seconds=30, buys=2, unique_buyers=1)
    bad = run(screen(l, settings, ctx, FakeDex(), thin, trade_native=0.05, trade_usd=10))
    assert not bad.passed and "unique buyers" in bad.hard_fails[0]
    assert "decimals" not in bad.metrics   # network checks skipped


# --- risk -----------------------------------------------------------------------

def test_risk_clamps_size_and_never_loosens_stop(settings, state):
    risk = RiskManager(settings, state)
    d, why = risk.approve(Decision("BUY", 0.9, 500.0, 0, 300, 90, 600), pump_launch(), SOL_USD)
    assert why is None
    assert d.size_usd == settings.max_usd_per_trade
    assert d.size_native == pytest.approx(settings.max_usd_per_trade / SOL_USD)
    assert d.stop_loss_pct == settings.stop_loss_pct
    assert d.max_hold_minutes == settings.max_hold_minutes
    assert d.take_profit_pct == 300


def test_risk_blocks_low_confidence_kill_switch_and_wallet_reserve(settings, state):
    risk = RiskManager(settings, state)
    assert risk.approve(Decision("BUY", 0.3, 5), pump_launch(), SOL_USD)[1].startswith("confidence")
    assert "no solana price" in risk.approve(Decision("BUY", 0.9, 5), pump_launch(), None)[1]
    # $6 wallet minus the $5 reserve leaves $1... a $4 wallet leaves nothing.
    assert risk.approve(Decision("BUY", 0.9, 5), pump_launch(), SOL_USD, 0.02)[1].startswith("no room")
    open(settings.kill_switch_path, "w").close()
    assert "kill switch" in risk.approve(Decision("BUY", 0.9, 5), pump_launch(), SOL_USD)[1]


def test_risk_enforces_daily_spend_and_creator_cooldown(settings, state):
    risk = RiskManager(settings, state)
    pos = Position(MINT, "FROG", 0.4, 1e5, 4e-6, time.time(), 100, 30, 25, 60, entry_usd=80.0)
    state.open_position(pos, CREATOR, "paper")
    assert "daily spend" in risk.halted()
    assert risk.exposure_usd() == pytest.approx(80.0)
    s2 = Settings(**{**settings.__dict__, "max_daily_spend_usd": 1000, "max_daily_loss_usd": 1000,
                     "max_total_exposure_usd": 1000})
    other = Launch(mint="other", source="pumpportal", creator=CREATOR)
    d, why = RiskManager(s2, state).approve(Decision("BUY", 0.9, 5), other, SOL_USD)
    assert why == "already traded this creator recently"
    d, why = RiskManager(s2, state).approve(Decision("BUY", 0.9, 5), pump_launch(), SOL_USD)
    assert why == "already holding this token"


# --- exits ----------------------------------------------------------------------

def pos(**kw):
    base = dict(mint=MINT, symbol="FROG", entry_sol=0.05, tokens=50_000, entry_price=1e-6,
                opened_at=1000.0, take_profit_pct=100, stop_loss_pct=30, trailing_stop_pct=25,
                max_hold_minutes=60, high_water_price=1e-6)
    base.update(kw)
    return Position(**base)


def test_exit_rules_in_priority_order():
    assert exits.evaluate(pos(), 0.69e-6, 1010, LADDER).reason.startswith("stop loss")
    assert exits.evaluate(pos(), 1.1e-6, 1000 + 61 * 60, LADDER).reason.startswith("time stop")
    tp = exits.evaluate(pos(), 2.1e-6, 1010, LADDER)
    assert tp.fraction == 0.5 and tp.reason.startswith("take profit 1/2")
    assert exits.evaluate(pos(), None, 1010, LADDER).fraction == 1.0
    assert exits.evaluate(pos(), 1.2e-6, 1010, LADDER).fraction == 0     # trailing not armed yet


def test_trailing_stop_arms_after_run_up_and_fires_on_pullback():
    p = pos(tp_taken=2)                                     # ladder done: only the trail is left
    assert exits.evaluate(p, 3e-6, 1010, LADDER).fraction == 0
    assert p.high_water_price == 3e-6
    out = exits.evaluate(p, 2.2e-6, 1020, LADDER)
    assert out.fraction == 1.0 and out.reason.startswith("trailing stop")


# --- engine end to end (paper) -------------------------------------------------

def test_paper_trade_lifecycle_buy_partial_take_profit_then_trailing_exit(settings, state):
    sniper, agent, jup = make_sniper(settings, state, BUY)
    p = run(sniper.handle(pump_launch()))
    assert p is not None and agent.calls == 1
    assert p.entry_sol == pytest.approx(settings.max_usd_per_trade / SOL_USD + 5_000 / 1e9)  # + base fee
    assert p.entry_usd == pytest.approx(p.entry_sol * SOL_USD)
    assert run(sniper.handle(pump_launch())) is None          # deduped

    jup.price[MINT] = 2.5e-6                                   # +142% after haircut
    act = run(sniper.check_position(state.open_positions()[0]))
    assert act.reason.startswith("take profit")
    left = state.open_positions()[0]
    assert left.tp_taken and left.tokens == pytest.approx(p.tokens / 2)

    jup.price[MINT] = 1.5e-6                                   # 40% off the high
    act = run(sniper.check_position(left))
    assert act.reason.startswith("trailing stop")
    assert state.open_positions() == []
    closed = state.closed_positions()[0]
    assert closed["realized_sol"] > closed["entry_sol"]
    assert state.realized_pnl_today() > 0


def test_rejected_candidates_never_reach_claude(settings, state):
    sniper, agent, _ = make_sniper(settings, state, BUY, rpc=FakeRpc(mint_authority="X"))
    assert run(sniper.handle(pump_launch())) is None
    assert agent.calls == 0
    row = state.db.execute("SELECT action, reasoning FROM decisions").fetchone()
    assert row["action"] == "REJECT" and "mint authority" in row["reasoning"]


def test_claude_skip_and_blocked_buys_are_journaled(settings, state):
    sniper, agent, _ = make_sniper(settings, state, Decision("SKIP", 0.2, reasoning="copycat"))
    assert run(sniper.handle(pump_launch())) is None
    assert state.db.execute("SELECT action FROM decisions").fetchone()[0] == "SKIP"


def test_unsellable_position_is_written_off(settings, state):
    sniper, _, jup = make_sniper(settings, state, BUY)
    run(sniper.handle(pump_launch()))
    jup.routable = False
    for _ in range(30):
        ps = state.open_positions()
        if not ps:
            break
        run(sniper.check_position(ps[0]))
    closed = state.closed_positions()[0]
    assert closed["exit_reason"].startswith("written off") and closed["realized_sol"] == 0


# --- Claude agent -----------------------------------------------------------------

class FakeMessages:
    def __init__(self, resp=None, delay=0.0):
        self.resp, self.delay, self.kwargs = resp, delay, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        await asyncio.sleep(self.delay)
        return self.resp


def fake_client(text=None, stop_reason="end_turn", delay=0.0):
    usage = SimpleNamespace(input_tokens=1500, output_tokens=200, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0)
    content = [SimpleNamespace(type="text", text=text)] if text else []
    resp = SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage, model="claude-opus-5-5")
    msgs = FakeMessages(resp, delay)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


DECISION_JSON = json.dumps({"action": "BUY", "confidence": 0.72, "size_usd": 6.0,
                            "take_profit_pct": 80, "stop_loss_pct": 20, "max_hold_minutes": 30,
                            "reasoning": "15 distinct buyers, dev holding"})


def test_agent_parses_structured_decision_and_sends_schema(settings):
    client, msgs = fake_client(DECISION_JSON)
    agent = EntryAgent(settings, client=client)
    d = run(agent.decide(pump_launch(), SafetyReport(MINT, metrics={"top10_holder_pct": 3}), {}))
    assert (d.action, d.size_usd, d.stop_loss_pct) == ("BUY", 6.0, 20)
    assert d.cost_usd == pytest.approx((1500 * 4 + 200 * 20) / 1e6)
    k = msgs.kwargs
    assert k["model"] == "claude-opus-5-5"
    assert k["output_config"]["format"]["type"] == "json_schema"
    assert k["output_config"]["effort"] == "low"
    assert k["fallbacks"] == "default" and k["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in k and "temperature" not in k


def test_agent_fails_closed(settings):
    client, _ = fake_client(stop_reason="refusal")
    assert run(EntryAgent(settings, client=client).decide(pump_launch(), SafetyReport(MINT), {})).action == "SKIP"
    client, _ = fake_client(DECISION_JSON, delay=0.2)
    slow = Settings(**{**settings.__dict__, "decision_timeout_s": 0.05})
    d = run(EntryAgent(slow, client=client).decide(pump_launch(), SafetyReport(MINT), {}))
    assert d.action == "SKIP" and "timed out" in d.reasoning
    client, msgs = fake_client(DECISION_JSON)
    capped = EntryAgent(Settings(**{**settings.__dict__, "max_claude_calls_per_hour": 1}), client=client)
    run(capped.decide(pump_launch(), SafetyReport(MINT), {}))
    assert "cap" in run(capped.decide(pump_launch(), SafetyReport(MINT), {})).reasoning
    assert "daily spend" in run(EntryAgent(settings, client=client).decide(
        pump_launch(), SafetyReport(MINT), {}, spent_today_usd=99)).reasoning


# --- config / CLI ---------------------------------------------------------------

def test_settings_from_env_and_live_requires_key(monkeypatch):
    monkeypatch.setenv("MEMESNIPER_MAX_USD_PER_TRADE", "25")
    monkeypatch.setenv("MEMESNIPER_SOURCES", "dexscreener")
    s = load_settings()
    assert s.max_usd_per_trade == 25 and s.sources == ("dexscreener",) and s.mode == "paper"
    assert s.chains == ("solana", "base", "bsc", "ethereum")
    monkeypatch.setenv("MEMESNIPER_MODE", "live")
    monkeypatch.delenv("MEMESNIPER_PRIVATE_KEY", raising=False)
    with pytest.raises(SystemExit):
        load_settings()


def test_cli_stop_resume_and_status(settings, monkeypatch, capsys):
    from memesniper.__main__ import main
    monkeypatch.setenv("MEMESNIPER_STATE_PATH", settings.state_path)
    monkeypatch.setenv("MEMESNIPER_KILL_SWITCH_PATH", settings.kill_switch_path)
    main(["stop"])
    main(["status"])
    out = capsys.readouterr().out
    status = json.loads(out[out.index("{"):])
    assert "kill switch" in status["halted"]
    main(["resume"])
    capsys.readouterr()
    main(["status"])
    assert json.loads(capsys.readouterr().out)["halted"] is None


# --- alerts and FOMO-wallet sync ---------------------------------------------------

class FakeHttp:
    def __init__(self):
        self.posts = []

    async def post(self, url, **kw):
        self.posts.append((url, kw))
        return SimpleNamespace(status_code=200)


def alert_settings(settings, **kw):
    base = dict(ntfy_topic="my-secret-topic", telegram_bot_token="T0K", telegram_chat_id="42",
                webhook_url="https://hooks.example/x")
    base.update(kw)
    return Settings(**{**settings.__dict__, **base})


def test_notifier_posts_to_every_configured_channel_and_dedupes_halts(settings):
    from memesniper.notify import Notifier
    http = FakeHttp()
    n = Notifier(alert_settings(settings), http)
    run(n.send("BUY FROG", "body", event="buy", data={"mint": MINT}))
    urls = [u for u, _ in http.posts]
    assert urls == ["https://ntfy.sh/my-secret-topic", "https://api.telegram.org/botT0K/sendMessage",
                    "https://hooks.example/x"]
    assert http.posts[0][1]["headers"]["Title"] == "BUY FROG"
    assert http.posts[2][1]["json"]["data"] == {"mint": MINT}
    http.posts.clear()
    run(n.halted("daily loss limit hit"))
    run(n.halted("daily loss limit hit"))
    run(n.halted(None))
    assert len(http.posts) == 3                         # one halt alert, across 3 channels
    assert not Notifier(settings, http).enabled


def test_buy_and_exit_send_alerts(settings, state):
    from memesniper.notify import Notifier
    http = FakeHttp()
    s = alert_settings(settings, telegram_bot_token="", webhook_url="")
    sniper, _, jup = make_sniper(s, state, BUY)
    sniper.notifier = Notifier(s, http)
    run(sniper.handle(pump_launch()))
    jup.price[MINT] = 0.5e-6
    run(sniper.check_position(state.open_positions()[0]))
    titles = [kw["headers"]["Title"] for _, kw in http.posts]
    assert titles == ["PAPER BUY FROG $10.00 on solana (launch)", "PAPER CLOSED FROG (solana)"]
    assert b"stop loss" in http.posts[1][1]["content"]


class WalletExecutor(PaperExecutor):
    """Paper fills, but with an on-chain balance you can change, like a shared FOMO wallet."""
    mode = "live"
    held = None

    async def held_tokens(self, mint):
        return self.held


def test_position_sold_in_fomo_app_is_closed_not_double_sold(settings, state):
    sniper, _, jup = make_sniper(settings, state, BUY)
    sniper.chains["solana"].executor = WalletExecutor(settings, jup)
    p = run(sniper.handle(pump_launch()))
    sniper.chains["solana"].executor.held = p.tokens                         # untouched: normal monitoring
    assert run(sniper.check_position(state.open_positions()[0])) is None
    assert state.open_positions()

    sniper.chains["solana"].executor.held = p.tokens / 2                     # you sold half in FOMO
    run(sniper.check_position(state.open_positions()[0]))
    left = state.open_positions()[0]
    assert left.tokens == pytest.approx(p.tokens / 2) and left.realized_sol > 0

    sniper.chains["solana"].executor.held = 0                                # then the rest
    run(sniper.check_position(left))
    assert state.open_positions() == []
    closed = state.closed_positions()[0]
    assert "outside the bot" in closed["exit_reason"]
    assert closed["realized_sol"] == pytest.approx(p.entry_sol * 0.97, rel=0.01)


# --- creator history ----------------------------------------------------------------

from memesniper import creator as creator_mod
from memesniper import outcomes as outcomes_mod


def test_pump_history_and_wallet_age_parsing():
    now = 1_000_000.0
    coins = [{"mint": MINT, "created_timestamp": now * 1000},                 # the token itself
             {"mint": "a", "created_timestamp": (now - 3600) * 1000, "complete": False},
             {"mint": "b", "created_timestamp": (now - 7200) * 1000, "complete": True},
             {"mint": "c", "created_timestamp": (now - 3 * 86400) * 1000}]
    assert creator_mod.summarize_pump_coins(coins, MINT, now) == {
        "pump_launches": 3, "pump_launches_24h": 2, "pump_graduated": 1}
    age, n, capped = creator_mod.wallet_age([{"blockTime": now - 100}, {"blockTime": now - 7200}], now)
    assert (age, n, capped) == (2.0, 2, False)
    assert creator_mod.wallet_age([], now) == (None, 0, False)


def test_creator_stats_count_launches_and_prior_rugs(settings, state):
    now = time.time()
    for i, ts in enumerate((now - 60, now - 3600, now - 3 * 86400)):
        state.record_launch(Launch(mint=f"old{i}", source="pumpportal", creator=CREATOR, seen_at=ts))
    state.record_launch(Launch(mint=MINT, source="pumpportal", creator=CREATOR, seen_at=now))
    state.start_outcome(Launch(mint="old0", source="pumpportal", creator=CREATOR), "REJECT", "", 0.05, 1e6)
    state.update_outcome("old0", {"r5m": 1.4, "r15m": 0.05}, False)          # rugged inside an hour
    stats = state.creator_stats(CREATOR, MINT)
    assert stats == {"launches_seen": 3, "launches_24h": 2, "prior_tracked": 1, "prior_rugs": 1,
                     "best_prior_x": 1.4}
    assert state.creator_stats("", MINT)["launches_seen"] == 0


def test_creator_checks_fail_repeat_ruggers_and_serial_launchers(settings):
    r = SafetyReport(MINT)
    creator_mod.check_creator_local(r, {"launches_seen": 9, "launches_24h": 5, "prior_tracked": 2,
                                        "prior_rugs": 1, "best_prior_x": 0.4}, settings)
    assert len(r.hard_fails) == 2 and r.metrics["creator"]["prior_rugs"] == 1

    r = SafetyReport(MINT)
    creator_mod.check_creator_remote(r, {"pump_launches": 7, "pump_launches_24h": 1, "pump_graduated": 0,
                                         "wallet_age_hours": 5.0, "wallet_tx_count": 12}, settings)
    assert r.passed and len(r.warnings) == 2               # never graduated + fresh wallet
    strict = Settings(**{**settings.__dict__, "creator_min_wallet_age_hours": 24})
    r = SafetyReport(MINT)
    creator_mod.check_creator_remote(r, {"wallet_age_hours": 5.0, "wallet_tx_count": 12}, strict)
    assert not r.passed
    r = SafetyReport(MINT)                                  # old, busy wallet: 1000+ txs, no warning
    creator_mod.check_creator_remote(r, {"wallet_age_hours": 5.0, "wallet_tx_count": "1000+"}, strict)
    assert r.passed and not r.warnings


class FakeCreators:
    def __init__(self, local, remote=None):
        self._local, self._remote, self.remote_calls = local, remote or {}, 0

    def local(self, creator, mint):
        return self._local

    async def remote(self, creator, mint):
        self.remote_calls += 1
        return self._remote


CLEAN_CREATOR = {"launches_seen": 0, "launches_24h": 0, "prior_tracked": 0, "prior_rugs": 0,
                 "best_prior_x": None}


def test_screen_rejects_known_rugger_before_any_network_call(settings):
    fc = FakeCreators({**CLEAN_CREATOR, "prior_rugs": 2})
    r = run(screen(pump_launch(), settings, None, None, good_obs(), creators=fc))
    assert not r.passed and "rugged before" in r.hard_fails[0] and fc.remote_calls == 0

    fc = FakeCreators(CLEAN_CREATOR, {"pump_launches": 2, "pump_launches_24h": 0, "pump_graduated": 1})
    r = run(screen(pump_launch(), settings, sol_ctx(settings, FakeJupiter()), FakeDex(),
                   good_obs(), creators=fc, trade_native=0.05, trade_usd=10))
    assert r.passed and r.metrics["creator"]["pump_graduated"] == 1 and r.ref_quote[0] == 0.05


# --- outcome tracking ---------------------------------------------------------------

def test_fail_categories():
    assert outcomes_mod.fail_category("top-10 wallets hold 40.1% (max 35%)") == "holder concentration"
    assert outcomes_mod.fail_category("only 3 unique buyers in 30s (min 8)") == "too few buyers"
    assert outcomes_mod.fail_category("no sell route on Jupiter (possible honeypot)") == "no sell route"
    assert outcomes_mod.fail_category("serial launcher: 5 other launches") == "serial launcher"


def test_tracker_samples_rejects_but_always_tracks_claude_decisions(settings, state):
    jup = FakeJupiter()
    t = outcomes_mod.OutcomeTracker(settings, state, {"solana": jup}, FakePrices(),
                                    rng=lambda: 0.5)                 # 0.5 > 0.1 sample rate
    rep = SafetyReport(MINT, passed=False, hard_fails=["only 2 unique buyers in 30s (min 3)"])
    assert run(t.track(pump_launch(), rep, "REJECT")) is False
    assert run(t.track(pump_launch(), rep, "SKIP")) is True                   # quotes its own ref
    row = state.pending_outcomes()[0]
    assert row["verdict"] == "SKIP" and row["reason"] == "too few buyers"
    assert row["ref_tokens"] == pytest.approx(0.05 / 1e-6)

    capped = outcomes_mod.OutcomeTracker(
        Settings(**{**settings.__dict__, "outcome_max_new_per_hour": 0}), state, {"solana": jup},
        FakePrices())
    assert run(capped.track(pump_launch(), rep, "BUY")) is False


def test_checkpoints_fill_on_schedule_and_mark_rugs_and_gaps(settings, state):
    jup = FakeJupiter(sell_haircut=0.0)
    t = outcomes_mod.OutcomeTracker(settings, state, {"solana": jup}, FakePrices())
    rep = SafetyReport(MINT, ref_quote=(0.05, 50_000))
    run(t.track(pump_launch(), rep, "BUY"))
    t0 = state.pending_outcomes()[0]["ts"]

    jup.price[MINT] = 3e-6                                  # 3x at 5 minutes
    assert run(t.check_due(now=t0 + 301)) == 1
    assert run(t.check_due(now=t0 + 302)) == 0              # nothing new due
    jup.routable = False                                    # liquidity pulled by 15m
    run(t.check_due(now=t0 + 901))
    row = state.pending_outcomes()[0]
    assert row["r5m"] == pytest.approx(3.0) and row["r15m"] == 0.0

    run(t.check_due(now=t0 + 14400 + outcomes_mod.GRACE_S + 1))   # bot was down: gaps marked -1
    assert state.pending_outcomes() == []
    done = state.outcome_rows()[0]
    assert (done["r1h"], done["r4h"], done["done"]) == (-1.0, -1.0, 1)


def test_report_shows_winners_and_rugs_by_group():
    def row(verdict, reason, *xs):
        return {"verdict": verdict, "reason": reason, **dict(zip(("r5m", "r15m", "r1h", "r4h"), xs))}
    rows = [row("BUY", "", 1.2, 2.5, 1.8, 1.1), row("BUY", "", 0.9, 0.5, 0.05, 0.0),
            row("SKIP", "", 1.0, 1.0, 1.0, None),
            row("REJECT", "too few buyers", 1.5, 3.0, 4.0, 2.0),
            row("REJECT", "too few buyers", 0.8, 0.2, -1.0, -1.0)]
    rep = outcomes_mod.build_report(rows)
    buy = rep["by_verdict"]["BUY"]
    assert buy["n"] == 2 and buy["pct_hit_2x"] == 50.0 and buy["pct_rugged"] == 50.0
    assert buy["r1h"]["median_x"] == pytest.approx(0.925)
    few = rep["rejects_by_reason"]["too few buyers"]
    assert few["pct_hit_2x"] == 50.0 and few["r1h"]["n"] == 1     # -1 gaps excluded
    text = outcomes_mod.format_report(rep)
    assert "REJECTIONS BY FIRST FAILED CHECK" in text and "too few buyers" in text


def test_engine_records_launches_and_tracks_every_decision(settings, state):
    sniper, _, _ = make_sniper(settings, state, BUY)
    sniper.creators = FakeCreators(CLEAN_CREATOR)
    router = sniper.chains["solana"].router
    sniper.outcomes = outcomes_mod.OutcomeTracker(settings, state, {"solana": router}, FakePrices(),
                                                  rng=lambda: 0.0)

    async def go():
        await sniper.handle(pump_launch())
        thin = Launch(mint="thin", source="pumpportal", creator="someone")
        sniper.hub = FakeHub(Observation(seconds=30, unique_buyers=1))
        router.price["thin"] = 1e-6
        await sniper.handle(thin)
        await asyncio.gather(*list(sniper._tasks))
    run(go())
    verdicts = {r["mint"]: r["verdict"] for r in state.outcome_rows()}
    assert verdicts == {MINT: "BUY", "thin": "REJECT"}
    assert state.db.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 2


def test_cli_outcomes_report(settings, state, monkeypatch, capsys):
    from memesniper.__main__ import main
    state.start_outcome(pump_launch(), "BUY", "", 0.05, 5e4)
    state.update_outcome(MINT, {"r5m": 2.2}, False)
    monkeypatch.setenv("MEMESNIPER_STATE_PATH", settings.state_path)
    main(["outcomes"])
    assert "BY DECISION" in capsys.readouterr().out
    main(["outcomes", "--json"])
    assert json.loads(capsys.readouterr().out)["by_verdict"]["BUY"]["pct_hit_2x"] == 100.0


# --- multi-chain: EVM routing, signing, safety -------------------------------------

from eth_account import Account

from memesniper import evm as evm_mod
from memesniper import evm_safety
from memesniper import watchlist as wl
from memesniper.feeds import DexScreenerFeed, launch_from_gecko, snapshot_from_pair
from memesniper.prices import NativePrices

TOKEN = "0x" + "ab" * 20
ROUTER_ADDR = "0x" + "cd" * 20


class Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeClient:
    """Routes GET/POST by URL substring to canned JSON; records calls."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _match(self, url):
        for key, val in self.routes.items():
            if key in url:
                return val(url) if callable(val) else val
        return Resp({}, 404)

    async def get(self, url, params=None, headers=None):
        self.calls.append(("GET", url, params))
        return self._match(url)

    async def post(self, url, json=None, headers=None, **kw):
        self.calls.append(("POST", url, json))
        return self._match(url)


def test_erc20_calldata():
    owner = "0x" + "11" * 20
    assert evm_mod.balance_of_data(owner) == "0x70a08231" + "0" * 24 + "11" * 20
    data = evm_mod.approve_data(ROUTER_ADDR, 5)
    assert data.startswith("0x095ea7b3") and data.endswith("0" * 63 + "5") and len(data) == 10 + 128


def test_kyber_quote_and_build():
    routes = {"/routes": Resp({"code": 0, "data": {"routeSummary": {"amountOut": "123000", "gasUsd": "0.4"},
                                                   "routerAddress": ROUTER_ADDR}}),
              "/route/build": Resp({"code": 0, "data": {"data": "0xdeadbeef", "routerAddress": ROUTER_ADDR,
                                                        "transactionValue": "1000", "gas": "200000"}})}
    client = FakeClient(routes)
    router = evm_mod.KyberRouter(client, CHAINS["base"])
    q = run(router.buy_quote(TOKEN, 0.002, 1500))
    assert q["outAmount"] == 123000 and q["gasUsd"] == 0.4
    params = client.calls[0][2]
    assert params["tokenIn"] == evm_mod.EVM_NATIVE and params["amountIn"] == str(2 * 10**15)
    assert "/base/api/v1/routes" in client.calls[0][1]
    tx = run(router.build(q, "0x" + "11" * 20, 1500))
    assert tx == {"to": ROUTER_ADDR, "data": "0xdeadbeef", "value": 1000, "gas": 200000}
    assert client.calls[-1][2]["slippageTolerance"] == 1500
    empty = evm_mod.KyberRouter(FakeClient({"/routes": Resp({"code": 4008, "data": None})}), CHAINS["bsc"])
    assert run(empty.sell_quote(TOKEN, 10, 1500)) is None


class FakeEvmRouter:
    """Toy EVM market: price in native coin per raw token."""
    def __init__(self, spec, price=1e-9, gas_usd=0.2):
        self.spec, self.price, self.gas_usd, self.routable = spec, {TOKEN: price}, gas_usd, True

    async def buy_quote(self, token, native, slippage_bps):
        if not self.routable:
            return None
        return {"outAmount": int(native / self.price[token]), "gasUsd": self.gas_usd,
                "routeSummary": {}, "routerAddress": ROUTER_ADDR}

    async def sell_quote(self, token, tokens_raw, slippage_bps):
        if not self.routable:
            return None
        return {"outAmount": int(tokens_raw * self.price[token] * 0.97 * 1e18), "gasUsd": self.gas_usd,
                "routeSummary": {}, "routerAddress": ROUTER_ADDR}

    async def value_native(self, token, tokens_raw, slippage_bps):
        q = await self.sell_quote(token, tokens_raw, slippage_bps)
        return q["outAmount"] / 1e18 if q else None

    async def build(self, quote, sender, slippage_bps):
        return {"to": ROUTER_ADDR, "data": "0xfeed", "value": 10**15, "gas": 150000}


def test_evm_paper_executor_charges_gas(settings):
    router = FakeEvmRouter(CHAINS["base"], gas_usd=0.3)
    ex = evm_mod.EvmPaperExecutor(settings, router, FakePrices())
    fill = run(ex.buy(TOKEN, 0.003))                     # $9 of ETH at $3000
    assert fill.tokens == pytest.approx(0.003 / 1e-9)
    assert fill.sol == pytest.approx(0.003 + 0.3 / 3000)
    sold = run(ex.sell(TOKEN, fill.tokens))
    assert sold.sol == pytest.approx(0.003 * 0.97 - 0.3 / 3000)


class FakeChainRpc:
    """Just enough of an EVM node for the live executor."""
    def __init__(self, allowance=0):
        self.native, self.tokens, self.allow = 10**18, 0, allowance
        self.sent, self.nonce = [], 7

    async def call(self, method, params=None):
        if method == "eth_getTransactionCount":
            return hex(self.nonce)
        if method == "eth_getBlockByNumber":
            return {"baseFeePerGas": hex(10**8)}
        if method == "eth_maxPriorityFeePerGas":
            return hex(10**7)
        if method == "eth_gasPrice":
            return hex(10**9)
        if method == "eth_estimateGas":
            return hex(60000)
        if method == "eth_sendRawTransaction":
            raw = params[0]
            self.sent.append(raw)
            self.nonce += 1
            if len(self.sent) == 1 or "095ea7b3" not in raw:        # a swap moved funds
                if self.tokens == 0:
                    self.native -= 10**15 + 10**13
                    self.tokens = 5 * 10**6
                else:
                    self.native += 9 * 10**14
                    self.tokens = 0
            else:
                self.allow = evm_mod.MAX_UINT
            return "0x" + "%064x" % len(self.sent)
        raise AssertionError(method)

    async def balance(self, address):
        return self.native

    async def token_balance(self, token, owner):
        return self.tokens

    async def allowance(self, token, owner, spender):
        return self.allow

    async def receipt(self, tx_hash):
        return {"status": "0x1"}


def test_evm_live_executor_signs_buys_approves_and_sells(settings, monkeypatch):
    monkeypatch.setattr(evm_mod.asyncio, "sleep", _no_sleep)
    acct = Account.create()
    rpc = FakeChainRpc()
    ex = evm_mod.EvmLiveExecutor(settings, FakeEvmRouter(CHAINS["base"]), rpc, acct.key.hex())
    assert ex.address == acct.address
    fill = run(ex.buy(TOKEN, 0.001))
    assert fill.tokens == 5 * 10**6 and fill.sol == pytest.approx((10**15 + 10**13) / 1e18)
    raw = rpc.sent[0]
    assert Account.recover_transaction(raw) == acct.address           # really signed by our key
    sold = run(ex.sell(TOKEN, fill.tokens))
    assert len(rpc.sent) == 3                                           # approve, then the swap
    assert "095ea7b3" in rpc.sent[1] and sold.sol == pytest.approx(9e14 / 1e18)


async def _no_sleep(_):
    return None


GOOD_GOPLUS = {"is_open_source": "1", "is_honeypot": "0", "buy_tax": "0.01", "sell_tax": "0.02",
               "holder_count": "820", "creator_address": "0x" + "99" * 20, "creator_percent": "0.03",
               "holders": [{"address": "pool", "is_contract": "1", "percent": "0.6"},
                           {"address": "a", "is_contract": "0", "percent": "0.05"},
                           {"address": "lock", "is_contract": "0", "is_locked": "1", "percent": "0.2"}],
               "lp_holders": [{"address": "0xdead", "is_locked": "1", "percent": "0.95"}]}


def test_goplus_checks(settings):
    r = SafetyReport(TOKEN)
    evm_safety.check_goplus(r, GOOD_GOPLUS, settings)
    assert r.passed and r.metrics["top10_holder_pct"] == 5.0 and r.metrics["lp_locked_pct"] == 95.0
    bad = {**GOOD_GOPLUS, "hidden_owner": "1", "sell_tax": "0.25", "is_proxy": "1",
           "lp_holders": []}
    r = SafetyReport(TOKEN)
    evm_safety.check_goplus(r, bad, settings)
    assert "hidden owner" in r.hard_fails and any("sell tax 25.0%" in f for f in r.hard_fails)
    assert "upgradeable proxy contract" in r.warnings and any("liquidity locked" in w for w in r.warnings)
    r = SafetyReport(TOKEN)
    evm_safety.check_goplus(r, None, settings)
    assert not r.passed


def test_honeypot_sim_and_gas_guard(settings):
    r = SafetyReport(TOKEN)
    evm_safety.check_honeypot_sim(r, {"honeypotResult": {"isHoneypot": True}}, settings)
    assert not r.passed
    r = SafetyReport(TOKEN)
    evm_safety.check_honeypot_sim(r, {"honeypotResult": {"isHoneypot": False},
                                      "simulationResult": {"sellTax": 3.5}}, settings)
    assert r.passed and r.metrics["simulated_sell_tax_pct"] == 3.5
    r = SafetyReport(TOKEN)
    evm_safety.check_gas(r, {"gasUsd": 2.5}, 10.0, settings)            # $5 gas on a $10 trade
    assert not r.passed and "gas $5.00" in r.hard_fails[0]


class FakeGoPlus:
    def __init__(self, data=GOOD_GOPLUS):
        self.data = data

    async def token(self, chain_id, address):
        return self.data


class FakeHoneypot:
    async def check(self, chain_id, address):
        return {"honeypotResult": {"isHoneypot": False}, "simulationResult": {"sellTax": 1.0}}


def evm_ctx(settings, chain="base", router=None, goplus=None):
    router = router or FakeEvmRouter(CHAINS[chain])
    return ChainCtx(CHAINS[chain], router, evm_mod.EvmPaperExecutor(settings, router, FakePrices()),
                    goplus=goplus or FakeGoPlus(), honeypot=FakeHoneypot())


def test_evm_screen_end_to_end(settings, state):
    from memesniper.creator import CreatorProfiler
    launch = Launch(mint=TOKEN, source="geckoterminal", chain="base",
                    activity={"buyers_h1": 40, "buys_h1": 90, "sells_h1": 30})
    prof = CreatorProfiler(settings, state, None, None)
    r = run(screen(launch, settings, evm_ctx(settings), FakeDex(), creators=prof,
                   trade_native=10 / 3000, trade_usd=10))
    assert r.passed, r.hard_fails
    assert launch.creator == GOOD_GOPLUS["creator_address"]             # learned from GoPlus
    assert r.metrics["pool_activity"]["buyers_h1"] == 40 and "roundtrip_loss_pct" in r.metrics
    thin = Launch(mint=TOKEN, source="geckoterminal", chain="base", activity={"buyers_h1": 1})
    assert not run(screen(thin, settings, evm_ctx(settings), FakeDex(), trade_native=0.003,
                          trade_usd=10)).passed


def test_engine_trades_base_and_solana_side_by_side(settings, state):
    s = Settings(**{**settings.__dict__, "chains": ("solana", "base")})
    sniper, agent, jup = make_sniper(s, state, BUY)
    sniper.chains["base"] = evm_ctx(s)
    base_launch = Launch(mint=TOKEN, source="geckoterminal", chain="base", symbol="BCAT",
                         activity={"buyers_h1": 50})
    p1 = run(sniper.handle(base_launch))
    p2 = run(sniper.handle(pump_launch()))
    assert p1.chain == "base" and p2.chain == "solana"
    assert p1.entry_usd == pytest.approx(10 + 0.2, rel=0.01)            # $10 + quoted gas
    assert run(sniper.handle(Launch(mint=TOKEN.upper().replace("0X", "0x"), source="dexscreener",
                                    chain="base"))) is None             # same token, other casing
    assert {p.chain for p in state.open_positions()} == {"base", "solana"}


# --- watchlist signals ---------------------------------------------------------------

def snap(ts, price=1.0, liq=50_000, vol_m5=2_000, vol_h1=12_000, buys=20, sells=20, m5=0.0, h1=0.0, h6=0.0):
    return {"ts": ts, "price_usd": price, "liquidity_usd": liq, "fdv_usd": 0, "vol_m5": vol_m5,
            "vol_h1": vol_h1, "buys_m5": buys, "sells_m5": sells, "chg_m5": m5, "chg_h1": h1, "chg_h6": h6}


def test_breakout_score(settings):
    flat = [snap(i * 60) for i in range(5)]
    assert wl.breakout_score(flat, settings)[0] < 0.2
    hot = flat + [snap(300, liq=70_000, vol_m5=8_000, vol_h1=14_000, buys=80, sells=20, m5=12, h1=40)]
    score, parts = wl.breakout_score(hot, settings)
    assert score >= settings.breakout_threshold and parts["buy_pressure"] == 1.0
    chased = flat + [snap(300, vol_m5=8_000, buys=80, sells=20, m5=12, h1=500)]
    assert wl.breakout_score(chased, settings) == (0.0, {"skip": "already up 500% in 1h"})
    assert wl.breakout_score(flat[:2], settings)[0] == 0.0


def test_dip_signal(settings):
    up = [snap(i * 600, price=p, h6=120) for i, p in enumerate((1.0, 1.3, 1.6, 2.0))]
    dip = up + [snap(2400, price=1.6, h6=110, m5=1, buys=30, sells=20)]       # -20% from high
    fire, d = wl.dip_signal(dip, settings)
    assert fire and d["pullback_pct"] == 20.0
    falling = up + [snap(2400, price=1.6, h6=110, m5=-3, buys=10, sells=40)]  # still being sold
    assert not wl.dip_signal(falling, settings)[0]
    crash = up + [snap(2400, price=1.0, h6=60, m5=1)]                         # -50%: trend broken
    assert not wl.dip_signal(crash, settings)[0]
    rug = up + [snap(2400, price=1.6, liq=20_000, h6=110, m5=1)]               # liquidity pulled
    assert not wl.dip_signal(rug, settings)[0]
    no_trend = [snap(i * 600, price=p, h6=10) for i, p in enumerate((1.0, 1.1, 1.2, 1.0))]
    assert not wl.dip_signal(no_trend, settings)[0]


def test_fade_reason():
    calm = [snap(0), snap(60)]
    assert wl.fade_reason(calm) is None
    assert "momentum fading" in wl.fade_reason([snap(0), snap(60, buys=5, sells=20, m5=-8)])
    assert "liquidity pulled" in wl.fade_reason([snap(0, liq=100_000), snap(60, liq=50_000)])


def test_watchlist_polls_fires_once_then_cools_down(settings, state):
    hot = [snap(i * 60) for i in range(4)] + [
        snap(240, liq=70_000, vol_m5=8_000, vol_h1=14_000, buys=80, sells=20, m5=12, h1=40),
        snap(300, liq=72_000, vol_m5=9_000, vol_h1=15_000, buys=90, sells=20, m5=10, h1=45)]
    dex = FakeDex({TOKEN.lower(): list(hot)})
    w = wl.Watchlist(settings, state, dex)
    w.add(Launch(mint=TOKEN, source="geckoterminal", chain="base", symbol="BCAT"))
    t0 = time.time()
    fired = [run(w.poll(now=t0 + i)) for i in range(6)]
    hits = [f for f in fired if f]
    assert len(hits) == 1                                    # cooldown stops the repeat
    launch, strategy, details = hits[0][0]
    assert (launch.chain, launch.mint, launch.source, strategy) == ("base", TOKEN, "watchlist", "breakout")
    assert details["breakout_score"] >= settings.breakout_threshold
    assert w.latest("base", TOKEN)["buys_m5"] == 90


def test_signal_buys_through_claude_with_strategy_context(settings, state):
    sniper, agent, _ = make_sniper(settings, state, BUY)
    p = run(sniper.handle_signal(pump_launch(), "dip", {"pullback_pct": 20}))
    assert p.strategy == "dip" and agent.last == ("dip", {"pullback_pct": 20})
    row = state.db.execute("SELECT strategy, executed FROM decisions").fetchone()
    assert (row["strategy"], row["executed"]) == ("dip", 1)


def test_rules_only_strategy_skips_claude(settings, state):
    s = Settings(**{**settings.__dict__, "claude_strategies": ("launch",)})
    sniper, agent, _ = make_sniper(s, state, BUY)
    p = run(sniper.handle_signal(pump_launch(), "breakout", {"breakout_score": 0.9}))
    assert p is not None and agent.calls == 0 and p.strategy == "breakout"


def test_screened_launches_join_the_watchlist(settings, state):
    sniper, _, _ = make_sniper(settings, state, Decision("SKIP", 0.1, reasoning="meh"))
    sniper.watchlist = wl.Watchlist(settings, state, FakeDex())
    run(sniper.handle(pump_launch()))
    assert [r["token"] for r in state.watched()] == [MINT]


# --- staged exits ----------------------------------------------------------------------

def test_ladder_sells_in_stages_then_trails():
    p = pos()
    first = exits.evaluate(p, 2.05e-6, 1010, LADDER)
    assert first.fraction == 0.5 and "1/2" in first.reason
    p.tp_taken = 1
    assert exits.evaluate(p, 2.5e-6, 1010, LADDER).fraction == 0          # between rungs
    second = exits.evaluate(p, 3.1e-6, 1010, LADDER)
    assert second.fraction == 0.5 and "2/2" in second.reason
    p.tp_taken = 2
    assert exits.evaluate(p, 2.2e-6, 1020, LADDER).reason.startswith("trailing stop")
    all_out = exits.evaluate(pos(), 2.05e-6, 1010, [(1.0, 1.0)])
    assert all_out.fraction == 1.0


def test_fade_exits_in_profit_and_liquidity_pull_exits_always():
    assert exits.evaluate(pos(), 1.3e-6, 1010, LADDER, fade="momentum fading").fraction == 1.0
    assert exits.evaluate(pos(), 0.9e-6, 1010, LADDER, fade="momentum fading").fraction == 0   # hold the loser
    pulled = exits.evaluate(pos(), 0.9e-6, 1010, LADDER, fade="liquidity pulled (60% of peak)")
    assert pulled.fraction == 1.0


# --- feeds, prices, migration ---------------------------------------------------------

def test_gecko_pool_and_dexscreener_snapshot_parsing():
    pool = {"attributes": {"address": "0xpool", "name": "BCAT / WETH", "reserve_in_usd": "45000",
                           "transactions": {"h1": {"buys": 90, "sells": 30, "buyers": 41, "sellers": 12}},
                           "volume_usd": {"h1": "52000"}, "price_change_percentage": {"h1": "35.5"}},
            "relationships": {"base_token": {"data": {"id": f"base_{TOKEN}"}}}}
    l = launch_from_gecko(pool, "base")
    assert (l.chain, l.mint, l.symbol, l.pair, l.liquidity_usd) == ("base", TOKEN, "BCAT", "0xpool", 45000.0)
    assert l.activity["buyers_h1"] == 41 and l.activity["price_change_h1_pct"] == 35.5
    assert launch_from_gecko(pool, None) is None
    pair = {"priceUsd": "0.0012", "liquidity": {"usd": 30000}, "volume": {"m5": 900, "h1": 7000},
            "txns": {"m5": {"buys": 14, "sells": 6}}, "priceChange": {"m5": 2.5, "h1": 12, "h6": 80}}
    sn = snapshot_from_pair(pair, now=5)
    assert (sn["price_usd"], sn["buys_m5"], sn["chg_h6"], sn["ts"]) == (0.0012, 14, 80.0, 5)


def test_dexscreener_profiles_filter_to_enabled_chains():
    profiles = [{"chainId": "base", "tokenAddress": TOKEN}, {"chainId": "tron", "tokenAddress": "T1"},
                {"chainId": "solana", "tokenAddress": MINT}]
    feed = DexScreenerFeed(FakeClient({"token-profiles": Resp(profiles)}), "https://x", 1,
                           chains=("base", "solana"))
    got = run(feed.poll())
    assert [(l.chain, l.mint) for l in got] == [("base", TOKEN), ("solana", MINT)]
    assert run(feed.poll()) == []


def test_native_prices_use_deepest_wrapped_pair():
    weth = CHAINS["base"].wrapped
    pairs = [{"baseToken": {"address": weth}, "priceUsd": "2990", "liquidity": {"usd": 1e5}},
             {"baseToken": {"address": weth.lower()}, "priceUsd": "3010", "liquidity": {"usd": 9e6}},
             {"baseToken": {"address": "0xother"}, "priceUsd": "1", "liquidity": {"usd": 1e9}}]
    prices = NativePrices(FakeClient({"/tokens/v1/base/": Resp(pairs)}), "https://x")
    assert run(prices.usd("base")) == 3010.0
    assert run(NativePrices(FakeClient({}), "https://x").usd("bsc")) is None


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, symbol TEXT, "
               "creator TEXT, mode TEXT, status TEXT DEFAULT 'open', entry_sol REAL, tokens REAL, "
               "entry_price REAL, opened_at REAL, take_profit_pct REAL, stop_loss_pct REAL, "
               "trailing_stop_pct REAL, max_hold_minutes REAL, high_water_price REAL, "
               "tp_taken INTEGER DEFAULT 0, realized_sol REAL DEFAULT 0, closed_at REAL, exit_reason TEXT, "
               "entry_sig TEXT, exit_sigs TEXT DEFAULT '[]')")
    db.execute("INSERT INTO positions (mint, symbol, entry_sol, tokens, entry_price, opened_at, "
               "take_profit_pct, stop_loss_pct, trailing_stop_pct, max_hold_minutes) "
               "VALUES ('m', 'OLD', 0.05, 1, 0.05, 1, 100, 30, 25, 60)")
    db.commit()
    db.close()
    st = State(path)
    (p,) = st.open_positions()
    assert (p.symbol, p.chain, p.strategy, p.entry_usd) == ("OLD", "solana", "launch", 0)


def test_signal_decisions_get_their_own_outcome_rows(settings, state):
    sniper, _, router = make_sniper(settings, state, Decision("SKIP", 0.2, reasoning="too early"))
    sniper.outcomes = outcomes_mod.OutcomeTracker(settings, state, {"solana": router}, FakePrices(),
                                                  rng=lambda: 0.0)

    async def go():
        await sniper.handle(pump_launch())                       # skipped at launch
        sniper.agent.decision = BUY
        await sniper.handle_signal(pump_launch(), "breakout", {"breakout_score": 0.8})
        await asyncio.gather(*list(sniper._tasks))
    run(go())
    rows = {(r["strategy"], r["verdict"]): r for r in state.outcome_rows()}
    assert set(rows) == {("launch", "SKIP"), ("breakout", "BUY")}
    assert rows[("breakout", "BUY")]["token"] == MINT
    t0 = rows[("breakout", "BUY")]["ts"]
    router.price[MINT] = 2e-6
    run(sniper.outcomes.check_due(now=t0 + 301))
    by = {r["strategy"]: r["r5m"] for r in state.outcome_rows()}
    assert by["breakout"] == pytest.approx(2 * 0.97)
    rep = outcomes_mod.build_report(state.outcome_rows())
    assert "breakout" in rep["buys_by_strategy"]
