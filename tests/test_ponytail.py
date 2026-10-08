import asyncio
import dataclasses
import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from ponytail import risk
from ponytail.agent import RH, TradingSession
from ponytail.config import Config
from ponytail.market import MarketCache
from ponytail.signals import learn_from_trade, raw_votes, weighted_decision

TODAY = date(2026, 10, 8)
ACCT = "955800222"
OID = "a1f83876-a9ea-4f17-b04e-4ce2428567ee"


def mcp(payload):
    """Shape a payload the way MCP tool responses reach PostToolUse hooks."""
    return [{"type": "text", "text": json.dumps(payload)}]


def fresh_ts():
    return (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat().replace("+00:00", "123Z")


def instruments_resp(expiration="2026-11-20", typ="call", oid=OID, symbol="SPY"):
    return mcp({"data": {"instruments": [{
        "id": oid, "chain_symbol": symbol, "expiration_date": expiration, "strike_price": "780.0000",
        "type": typ, "state": "active", "tradability": "tradable"}]}})


def quotes_resp(bid=1.50, ask=1.60, delta=0.48, oi=500, oid=OID):
    return mcp({"data": {"results": [{"quote": {
        "instrument_id": oid, "bid_price": f"{bid}", "ask_price": f"{ask}", "mark_price": f"{(bid + ask) / 2}",
        "delta": f"{delta}", "implied_volatility": "0.2", "open_interest": oi, "updated_at": fresh_ts()}}]}})


def rising_then_cross():
    """Long decline then a sharp rally: SMA20/50 and MACD cross up on the last bar region."""
    p = list(np.linspace(120, 90, 80)) + list(np.linspace(90, 130, 12))
    return p


@pytest.fixture
def cfg(tmp_path):
    return Config(account_number=ACCT, symbols=["SPY", "QQQ"], state_path=str(tmp_path / "state.json"))


@pytest.fixture
def session(cfg):
    s = TradingSession(cfg, today=TODAY)
    s.signals["SPY"] = {"votes": {"sma": 1, "rsi": 0, "macd": 1}, "score": 0.67, "decision": "BUY", "rsi": 55}
    s.market.ingest("get_option_instruments", {}, instruments_resp())
    s.market.ingest("get_option_quotes", {}, quotes_resp())
    s.market.ingest("get_earnings_results", {"symbol": "SPY"}, mcp({"data": {"results": []}}))
    return s


def run(coro):
    return asyncio.run(coro)


def pre(session, tool, args):
    return run(session.pre_tool_use({"tool_name": tool, "tool_input": args}, "t1", None))


def order(effect="open", side="buy", qty="1", price="1.55", **kw):
    o = {"account_number": ACCT, "quantity": qty, "price": price, "type": "limit",
         "legs": [{"option_id": OID, "side": side, "position_effect": effect}]}
    o.update(kw)
    return o


# ---- signals ----------------------------------------------------------

def test_raw_votes_and_decision():
    votes, last_rsi = raw_votes(rising_then_cross())
    assert set(votes) == {"sma", "rsi", "macd"} and 0 <= last_rsi <= 100
    decision, score = weighted_decision({"sma": 1, "rsi": 0, "macd": 1}, {"sma": 1, "rsi": 1, "macd": 1})
    assert decision == "BUY" and score == pytest.approx(2 / 3)


def test_raw_votes_needs_history():
    with pytest.raises(ValueError):
        raw_votes([100.0] * 30)


def test_learning_respects_put_direction():
    # A profitable long put (bearish bet): the indicator that voted -1 was right.
    w = {"sma": 1.0, "rsi": 1.0, "macd": 1.0}
    learn_from_trade(w, {"sma": -1, "rsi": 0, "macd": 1}, direction=-1, pnl=50)
    assert w == {"sma": 1.1, "rsi": 1.0, "macd": 0.9}
    # A losing long call: the indicator that voted +1 was wrong.
    learn_from_trade(w, {"sma": 1, "rsi": 0, "macd": -1}, direction=1, pnl=-20)
    assert w["sma"] == pytest.approx(1.0) and w["macd"] == pytest.approx(1.0)


# ---- market cache ---------------------------------------------------------

def test_ingest_parses_real_shapes():
    m = MarketCache()
    m.ingest("get_equity_historicals", {}, mcp({"data": {"results": [{"symbol": "SPY", "interval": "day", "bars": [
        {"close_price": "765.61"}, {"close_price": "764.20"},
        {"close_price": "779.09", "interpolated": True}]}]}}))
    assert m.closes["SPY"] == [765.61, 764.20]  # interpolated gap-fill dropped
    m.ingest("get_option_quotes", {}, quotes_resp())
    assert m.quotes[OID]["bid"] == 1.5 and m.quotes[OID]["open_interest"] == 500
    assert m.quote_age_minutes(OID) < 5  # nanosecond timestamps parse
    m.ingest("get_option_positions", {"account_number": ACCT, "nonzero": True},
             mcp({"data": {"positions": [{"option": f"https://x/options/instruments/{OID}/", "quantity": "1.0000", "type": "long"}]}}))
    assert m.broker_positions == {OID: 1.0}


# ---- risk -----------------------------------------------------------------

def test_open_approved_when_everything_checks_out(session):
    assert risk.check_open(session.cfg, session.state, session.market, session.signals, OID, 1, 1.55, TODAY) == []


@pytest.mark.parametrize("mutate, expected", [
    (lambda s: s.signals["SPY"].update(decision="SELL"), "but contract is a call"),
    (lambda s: s.signals["SPY"].update(decision="HOLD"), "signal is HOLD"),
    (lambda s: s.market.quotes[OID].update(bid=1.0), "spread"),
    (lambda s: s.market.quotes[OID].update(open_interest=5), "open interest"),
    (lambda s: s.market.quotes[OID].update(delta=0.9), "delta"),
    (lambda s: s.market.quotes[OID].update(updated_at="2026-10-01T00:00:00Z"), "stale"),
    (lambda s: s.market.earnings.update(SPY=["2026-11-01"]), "earnings on 2026-11-01"),
    (lambda s: s.market.earnings.pop("SPY"), "earnings not checked"),
    (lambda s: s.market.instruments[OID].update(expiration="2026-10-15"), "DTE 7"),
    (lambda s: s.signals.pop("SPY"), "no compute_signals"),
])
def test_open_rejections(session, mutate, expected):
    mutate(session)
    problems = risk.check_open(session.cfg, session.state, session.market, session.signals, OID, 1, 1.55, TODAY)
    assert any(expected in p for p in problems), problems


def test_open_rejects_oversize_and_limit_outside_market(session):
    problems = risk.check_open(session.cfg, session.state, session.market, session.signals, OID, 1, 1.70, TODAY)
    assert any("outside bid/ask" in p for p in problems)
    problems = risk.check_open(session.cfg, session.state, session.market, session.signals, OID, 2, 1.55, TODAY)
    assert any("per-trade cap" in p for p in problems)


def test_daily_loss_kill_switch(session):
    session.state.trade_log.append({"pnl": -200.0, "closed_at": TODAY.isoformat() + "T15:00:00+00:00"})
    problems = risk.check_open(session.cfg, session.state, session.market, session.signals, OID, 1, 1.55, TODAY)
    assert any("daily loss" in p for p in problems)


# ---- hooks: the actual enforcement ------------------------------------------------

def test_blocks_tools_outside_allowlist(session):
    out = pre(session, RH + "place_equity_order", {"account_number": ACCT})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    out = pre(session, RH + "exercise_option", {})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_unplanned_open_is_denied(session):
    out = pre(session, RH + "place_option_order", order())
    assert "no approved plan" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("bad, reason", [
    (order(side="sell"), "buy-to-open"),
    (order(type="market"), "limit"),
    (order(account_number="475262101"), "configured agent account"),
    (order(legs=[{"option_id": OID, "side": "buy", "position_effect": "open"}] * 2), "single-leg"),
])
def test_order_shape_rules(session, bad, reason):
    session.propose_option_trade(OID, 1, 1.55, "test")
    out = pre(session, RH + "place_option_order", bad)
    assert reason in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_paper_round_trip_learns_from_pnl(session):
    assert session.propose_option_trade(OID, 1, 1.55, "breakout")["approved"]
    # Can't sneak a higher price or bigger size past the plan.
    assert "does not match" in pre(session, RH + "place_option_order", order(price="1.60"))["hookSpecificOutput"]["permissionDecisionReason"]

    out = pre(session, RH + "place_option_order", order())
    assert "PAPER MODE" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert session.state.positions[OID]["entry_price"] == 1.55
    # Plan is consumed: a second identical open is denied.
    assert "no approved plan" in pre(session, RH + "place_option_order", order())["hookSpecificOutput"]["permissionDecisionReason"]

    # Price rallies past take-profit -> review says CLOSE.
    session.market.ingest("get_option_quotes", {}, quotes_resp(bid=2.35, ask=2.45))
    rows = session.review_positions()["positions"]
    assert rows[0]["action"] == "CLOSE" and "take profit" in rows[0]["reason"]

    out = pre(session, RH + "place_option_order", order(effect="close", side="sell", price="2.40"))
    assert "PAPER MODE" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert OID not in session.state.positions
    trade = session.state.trade_log[-1]
    assert trade["pnl"] == pytest.approx(85.0)
    assert session.state.weights == {"sma": 1.1, "rsi": 1.0, "macd": 1.1}

    # State persisted to disk.
    reloaded = json.load(open(session.cfg.state_path))
    assert reloaded["trade_log"][-1]["pnl"] == pytest.approx(85.0)


def test_live_mode_allows_and_books_on_success(cfg):
    s = TradingSession(dataclasses.replace(cfg, live_trading=True), today=TODAY)
    s.signals["SPY"] = {"votes": {"sma": 1, "rsi": 0, "macd": 1}, "score": 0.67, "decision": "BUY", "rsi": 55}
    s.market.ingest("get_option_instruments", {}, instruments_resp())
    s.market.ingest("get_option_quotes", {}, quotes_resp())
    s.market.ingest("get_earnings_results", {"symbol": "SPY"}, mcp({"data": {"results": []}}))
    assert s.propose_option_trade(OID, 1, 1.55, "x")["approved"]
    assert pre(s, RH + "place_option_order", order()) == {}
    run(s.post_tool_use({"tool_name": RH + "place_option_order", "tool_input": order(),
                         "tool_response": mcp({"data": {"id": "order-1", "state": "queued"}})}, "t1", None))
    assert s.state.positions[OID]["mode"] == "live"

    # Next run: broker says we never got filled -> position is dropped, nothing learned.
    s.market.ingest("get_option_positions", {"account_number": ACCT, "nonzero": True}, mcp({"data": {"positions": []}}))
    rows = s.review_positions()["positions"]
    assert rows[0]["action"] == "DROP" and OID not in s.state.positions
    assert s.state.weights == {"sma": 1.0, "rsi": 1.0, "macd": 1.0}


def test_close_of_unheld_contract_denied(session):
    out = pre(session, RH + "place_option_order", order(effect="close", side="sell"))
    assert "holds no position" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_paper_mode_skips_broker_review(session):
    out = pre(session, RH + "review_option_order", order())
    assert "PAPER MODE" in out["hookSpecificOutput"]["permissionDecisionReason"]
