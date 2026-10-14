import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from memesniper import exits
from memesniper.agent import EntryAgent
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
                    priority_fee_lamports=0, paper_extra_slippage_pct=0.0, min_unique_buyers=3)


@pytest.fixture
def state(settings):
    return State(settings.state_path)


# --- fakes ----------------------------------------------------------------------

class FakeJupiter:
    """Constant-product-free toy market: price in SOL per raw token, settable per mint."""
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

    async def value_sol(self, mint, tokens_raw, slippage_bps):
        q = await self.sell_quote(mint, tokens_raw, slippage_bps)
        return int(q["outAmount"]) / 1e9 if q else None


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
    async def pair_info(self, mint):
        return None


class FakeAgent:
    def __init__(self, decision):
        self.decision, self.calls = decision, 0

    async def decide(self, launch, report, portfolio, spent_today_usd=0.0):
        self.calls += 1
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


def make_sniper(settings, state, decision, jup=None, rpc=None, obs=None):
    jup = jup or FakeJupiter()
    agent = FakeAgent(decision)
    s = Sniper(settings, state=state, risk=RiskManager(settings, state), rpc=rpc or FakeRpc(),
               jupiter=jup, rugcheck=FakeRugcheck(), dexscreener=FakeDex(), agent=agent,
               executor=PaperExecutor(settings, jup), hub=FakeHub(obs or good_obs()))
    return s, agent, jup


BUY = Decision("BUY", confidence=0.8, size_sol=1.0, take_profit_pct=100, stop_loss_pct=50,
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
    ok = run(screen(l, settings, FakeRpc(), FakeJupiter(), FakeRugcheck(), FakeDex(), good_obs()))
    assert ok.passed, ok.hard_fails
    thin = Observation(seconds=30, buys=2, unique_buyers=1)
    bad = run(screen(l, settings, FakeRpc(), FakeJupiter(), FakeRugcheck(), FakeDex(), thin))
    assert not bad.passed and "unique buyers" in bad.hard_fails[0]
    assert "decimals" not in bad.metrics   # network checks skipped


# --- risk -----------------------------------------------------------------------

def test_risk_clamps_size_and_never_loosens_stop(settings, state):
    risk = RiskManager(settings, state)
    d, why = risk.approve(Decision("BUY", 0.9, 5.0, 300, 90, 600), pump_launch())
    assert why is None
    assert d.size_sol == settings.max_sol_per_trade
    assert d.stop_loss_pct == settings.stop_loss_pct
    assert d.max_hold_minutes == settings.max_hold_minutes
    assert d.take_profit_pct == 300


def test_risk_blocks_low_confidence_kill_switch_and_wallet_reserve(settings, state):
    risk = RiskManager(settings, state)
    assert risk.approve(Decision("BUY", 0.3, 0.01), pump_launch())[1].startswith("confidence")
    assert risk.approve(Decision("BUY", 0.9, 0.05), pump_launch(), wallet_sol=0.05)[1].startswith("no room")
    open(settings.kill_switch_path, "w").close()
    assert "kill switch" in risk.approve(Decision("BUY", 0.9, 0.01), pump_launch())[1]


def test_risk_enforces_daily_spend_and_creator_cooldown(settings, state):
    risk = RiskManager(settings, state)
    pos = Position(MINT, "FROG", 0.3, 1e5, 3e-6, time.time(), 100, 30, 25, 60)
    state.open_position(pos, CREATOR, "paper")
    assert "daily spend" in risk.halted()
    s2 = Settings(**{**settings.__dict__, "max_daily_spend_sol": 10, "max_daily_loss_sol": 10})
    d, why = RiskManager(s2, state).approve(Decision("BUY", 0.9, 0.01), pump_launch())
    assert why == "already traded this creator recently"


# --- exits ----------------------------------------------------------------------

def pos(**kw):
    base = dict(mint=MINT, symbol="FROG", entry_sol=0.05, tokens=50_000, entry_price=1e-6,
                opened_at=1000.0, take_profit_pct=100, stop_loss_pct=30, trailing_stop_pct=25,
                max_hold_minutes=60, high_water_price=1e-6)
    base.update(kw)
    return Position(**base)


def test_exit_rules_in_priority_order():
    assert exits.evaluate(pos(), 0.69e-6, 1010, 0.5).reason.startswith("stop loss")
    assert exits.evaluate(pos(), 1.1e-6, 1000 + 61 * 60, 0.5).reason.startswith("time stop")
    tp = exits.evaluate(pos(), 2.1e-6, 1010, 0.5)
    assert tp.fraction == 0.5 and tp.reason.startswith("take profit")
    assert exits.evaluate(pos(), None, 1010, 0.5).fraction == 1.0
    assert exits.evaluate(pos(), 1.2e-6, 1010, 0.5).fraction == 0     # trailing not armed yet


def test_trailing_stop_arms_after_run_up_and_fires_on_pullback():
    p = pos(tp_taken=True)
    assert exits.evaluate(p, 3e-6, 1010, 0.5).fraction == 0
    assert p.high_water_price == 3e-6
    out = exits.evaluate(p, 2.2e-6, 1020, 0.5)
    assert out.fraction == 1.0 and out.reason.startswith("trailing stop")


# --- engine end to end (paper) -------------------------------------------------

def test_paper_trade_lifecycle_buy_partial_take_profit_then_trailing_exit(settings, state):
    sniper, agent, jup = make_sniper(settings, state, BUY)
    p = run(sniper.handle(pump_launch()))
    assert p is not None and agent.calls == 1
    assert p.entry_sol == pytest.approx(settings.max_sol_per_trade + 5_000 / 1e9)  # + base fee
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


DECISION_JSON = json.dumps({"action": "BUY", "confidence": 0.72, "size_sol": 0.03,
                            "take_profit_pct": 80, "stop_loss_pct": 20, "max_hold_minutes": 30,
                            "reasoning": "15 distinct buyers, dev holding"})


def test_agent_parses_structured_decision_and_sends_schema(settings):
    client, msgs = fake_client(DECISION_JSON)
    agent = EntryAgent(settings, client=client)
    d = run(agent.decide(pump_launch(), SafetyReport(MINT, metrics={"top10_holder_pct": 3}), {}))
    assert (d.action, d.size_sol, d.stop_loss_pct) == ("BUY", 0.03, 20)
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
    monkeypatch.setenv("MEMESNIPER_MAX_SOL_PER_TRADE", "0.02")
    monkeypatch.setenv("MEMESNIPER_SOURCES", "dexscreener")
    s = load_settings()
    assert s.max_sol_per_trade == 0.02 and s.sources == ("dexscreener",) and s.mode == "paper"
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
    assert titles == ["PAPER BUY FROG 0.0500 SOL", "PAPER CLOSED FROG"]
    assert b"stop loss" in http.posts[1][1]["content"]


class WalletExecutor(PaperExecutor):
    """Paper fills, but with an on-chain balance you can change, like a shared FOMO wallet."""
    mode = "live"
    held = None

    async def held_tokens(self, mint):
        return self.held


def test_position_sold_in_fomo_app_is_closed_not_double_sold(settings, state):
    sniper, _, jup = make_sniper(settings, state, BUY)
    sniper.exec = WalletExecutor(settings, jup)
    p = run(sniper.handle(pump_launch()))
    sniper.exec.held = p.tokens                         # untouched: normal monitoring
    assert run(sniper.check_position(state.open_positions()[0])) is None
    assert state.open_positions()

    sniper.exec.held = p.tokens / 2                     # you sold half in FOMO
    run(sniper.check_position(state.open_positions()[0]))
    left = state.open_positions()[0]
    assert left.tokens == pytest.approx(p.tokens / 2) and left.realized_sol > 0

    sniper.exec.held = 0                                # then the rest
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
    r = run(screen(pump_launch(), settings, None, None, None, None, good_obs(), creators=fc))
    assert not r.passed and "rugged before" in r.hard_fails[0] and fc.remote_calls == 0

    fc = FakeCreators(CLEAN_CREATOR, {"pump_launches": 2, "pump_launches_24h": 0, "pump_graduated": 1})
    r = run(screen(pump_launch(), settings, FakeRpc(), FakeJupiter(), FakeRugcheck(), FakeDex(),
                   good_obs(), creators=fc))
    assert r.passed and r.metrics["creator"]["pump_graduated"] == 1 and r.ref_quote[0] == 0.05


# --- outcome tracking ---------------------------------------------------------------

def test_fail_categories():
    assert outcomes_mod.fail_category("top-10 wallets hold 40.1% (max 35%)") == "holder concentration"
    assert outcomes_mod.fail_category("only 3 unique buyers in 30s (min 8)") == "too few buyers"
    assert outcomes_mod.fail_category("no sell route on Jupiter (possible honeypot)") == "no sell route"
    assert outcomes_mod.fail_category("serial launcher: 5 other launches") == "serial launcher"


def test_tracker_samples_rejects_but_always_tracks_claude_decisions(settings, state):
    jup = FakeJupiter()
    t = outcomes_mod.OutcomeTracker(settings, state, jup, rng=lambda: 0.5)    # 0.5 > 0.1 sample rate
    rep = SafetyReport(MINT, passed=False, hard_fails=["only 2 unique buyers in 30s (min 3)"])
    assert run(t.track(pump_launch(), rep, "REJECT")) is False
    assert run(t.track(pump_launch(), rep, "SKIP")) is True                   # quotes its own ref
    row = state.pending_outcomes()[0]
    assert row["verdict"] == "SKIP" and row["reason"] == "too few buyers"
    assert row["ref_tokens"] == pytest.approx(0.05 / 1e-6)

    capped = outcomes_mod.OutcomeTracker(
        Settings(**{**settings.__dict__, "outcome_max_new_per_hour": 0}), state, jup)
    assert run(capped.track(pump_launch(), rep, "BUY")) is False


def test_checkpoints_fill_on_schedule_and_mark_rugs_and_gaps(settings, state):
    jup = FakeJupiter(sell_haircut=0.0)
    t = outcomes_mod.OutcomeTracker(settings, state, jup)
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
    sniper.outcomes = outcomes_mod.OutcomeTracker(settings, state, sniper.jup, rng=lambda: 0.0)

    async def go():
        await sniper.handle(pump_launch())
        thin = Launch(mint="thin", source="pumpportal", creator="someone")
        sniper.hub = FakeHub(Observation(seconds=30, unique_buyers=1))
        sniper.jup.price["thin"] = 1e-6
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
