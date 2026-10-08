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


def broker_order(order_id, state, processed_qty="1.00000", premium="155", typ="limit", trigger="immediate",
                 side="buy", effect="open", stop_price=None):
    return {"id": order_id, "state": state, "type": typ, "trigger": trigger, "quantity": "1.00000",
            "processed_quantity": processed_qty, "processed_premium": premium, "trade_value_multiplier": "100.0000",
            "stop_price": stop_price, "time_in_force": "gfd",
            "legs": [{"option_id": OID, "side": side, "position_effect": effect}]}


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
    session.state.trade_log.append({"symbol": "IWM", "pnl": -200.0, "closed_at": TODAY.isoformat() + "T15:00:00+00:00"})
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

    # Still working at the broker this run: not dropped even though positions don't list it yet.
    s.market.ingest("get_option_positions", {"account_number": ACCT, "nonzero": True}, mcp({"data": {"positions": []}}))
    assert s.review_positions()["positions"][0]["action"] == "WAIT_FILL"
    # Broker reports the open order cancelled unfilled -> dropped, nothing learned.
    s.market.ingest("get_option_orders", {"account_number": ACCT}, mcp({"data": {"orders": [
        broker_order("order-1", "cancelled", processed_qty="0", premium="0")]}}))
    s.review_positions()
    assert OID not in s.state.positions
    assert s.state.weights == {"sma": 1.0, "rsi": 1.0, "macd": 1.0}


def test_close_of_unheld_contract_denied(session):
    out = pre(session, RH + "place_option_order", order(effect="close", side="sell"))
    assert "holds no position" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_paper_mode_skips_broker_review(session):
    out = pre(session, RH + "review_option_order", order())
    assert "PAPER MODE" in out["hookSpecificOutput"]["permissionDecisionReason"]


# ---- stop losses & loss limits ---------------------------------------------------

from ponytail import protect  # noqa: E402

QQQ_ID = "11111111-2222-3333-4444-555555555555"


def stop(stop_price="1.01", tif="gfd", typ="stop_market", **kw):
    o = {"account_number": ACCT, "quantity": "1", "type": typ, "stop_price": stop_price, "time_in_force": tif,
         "legs": [{"option_id": OID, "side": "sell", "position_effect": "close"}]}
    o.update(kw)
    return o


def reason(out):
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def open_paper(session):
    assert session.propose_option_trade(OID, 1, 1.55, "t")["approved"]
    pre(session, RH + "place_option_order", order())
    return session.state.positions[OID]


def test_required_stop_and_trailing_ratchet(cfg):
    pos = {"entry_price": 1.55, "hwm": 1.55}
    assert protect.required_stop(cfg, pos) == 1.01            # 1.55 * 0.65 = 1.0075, rounded up to the tick
    pos["hwm"] = 2.00                                         # +29%: below the 30% trail activation
    assert protect.required_stop(cfg, pos) == 1.01
    pos["hwm"] = 2.10                                         # +35%: trail at 2.10 * 0.75
    assert protect.required_stop(cfg, pos) == 1.58           # now locks in a small gain


def test_new_entries_blocked_until_position_has_a_stop(session):
    open_paper(session)
    session.signals["QQQ"] = {"votes": {"sma": 1, "rsi": 0, "macd": 1}, "score": 0.67, "decision": "BUY", "rsi": 50}
    session.market.ingest("get_option_instruments", {}, instruments_resp(oid=QQQ_ID, symbol="QQQ"))
    session.market.ingest("get_option_quotes", {}, quotes_resp(oid=QQQ_ID))
    session.market.ingest("get_earnings_results", {"symbol": "QQQ"}, mcp({"data": {"results": []}}))
    res = session.propose_option_trade(QQQ_ID, 1, 1.55, "t")
    assert not res["approved"] and any("protective stop" in p for p in res["problems"])

    row = session.review_positions()["positions"][0]
    assert row["action"] == "PLACE_STOP"
    assert row["stop_order"]["stop_price"] == "1.01" and row["stop_order"]["type"] == "stop_market"
    out = pre(session, RH + "place_option_order", {**row["stop_order"], "account_number": ACCT})
    assert "protective stop_market at 1.01" in reason(out)
    assert session.propose_option_trade(QQQ_ID, 1, 1.55, "t")["approved"]


@pytest.mark.parametrize("bad, why", [
    (stop(stop_price="0.90"), "looser than required"),
    (stop(stop_price="1.60"), "already at/below the stop"),
    (stop(tif="gtc"), "must be gfd"),
    (stop(quantity="2"), "full position"),
    (stop(typ="stop_limit", price="0.90", tif="gtc"), "must be type stop_market"),
    (stop(legs=[{"option_id": OID, "side": "buy", "position_effect": "open"}]), "sell-to-close protection"),
])
def test_bad_stops_denied(session, bad, why):
    open_paper(session)
    assert why in reason(pre(session, RH + "place_option_order", bad))


def test_stop_cannot_be_cancelled_or_loosened_arbitrarily(session):
    open_paper(session)
    session.review_positions()
    pre(session, RH + "place_option_order", stop())
    stop_id = session.state.positions[OID]["stop"]["order_id"]
    session.review_positions()  # HOLD: protected, nothing to change
    assert "may only be cancelled" in reason(pre(session, RH + "cancel_option_order", {"account_number": ACCT, "order_id": stop_id}))
    assert "already active" in reason(pre(session, RH + "place_option_order", stop(stop_price="1.20")))


def test_paper_stop_triggers_and_books_loss(session):
    open_paper(session)
    session.review_positions()
    pre(session, RH + "place_option_order", stop())
    session.market.ingest("get_option_quotes", {}, quotes_resp(bid=0.95, ask=1.00))  # mark 0.975 < stop 1.01
    res = session.review_positions()
    assert res["broker_events"][0]["kind"] == "stopped_out"
    assert OID not in session.state.positions
    assert session.state.trade_log[-1]["pnl"] == pytest.approx(-60.0)   # (0.95 - 1.55) * 100
    assert session.state.weights == {"sma": 0.9, "rsi": 1.0, "macd": 0.9}
    # Cooldown: no immediate re-entry on the symbol that just lost.
    session.market.ingest("get_option_quotes", {}, quotes_resp())
    res = session.propose_option_trade(OID, 1, 1.55, "t")
    assert any("cooling down" in p for p in res["problems"])


def test_trailing_stop_raise_flow(session):
    open_paper(session)
    session.review_positions()
    pre(session, RH + "place_option_order", stop())
    session.market.ingest("get_option_quotes", {}, quotes_resp(bid=2.05, ask=2.15))  # mark 2.10, +35%
    row = session.review_positions()["positions"][0]
    assert row["action"] == "RAISE_STOP" and row["stop_order"]["stop_price"] == "1.58"
    old = session.state.positions[OID]["stop"]["order_id"]
    assert "PAPER MODE: stop" in reason(pre(session, RH + "cancel_option_order", {"account_number": ACCT, "order_id": old}))
    assert "1.58" in reason(pre(session, RH + "place_option_order", {**row["stop_order"], "account_number": ACCT}))
    assert session.state.positions[OID]["stop"]["stop_price"] == 1.58


def test_gfd_stop_lapses_next_day(session):
    open_paper(session)
    session.review_positions()
    pre(session, RH + "place_option_order", stop())
    session.today = TODAY + timedelta(days=1)
    assert protect.unprotected(session.state, session.market, session.today) == [OID]
    assert session.review_positions()["positions"][0]["action"] == "PLACE_STOP"


def test_breached_stop_level_forces_close(session):
    open_paper(session)
    session.market.ingest("get_option_quotes", {}, quotes_resp(bid=1.00, ask=1.06))  # bid at/below 1.01 stop
    row = session.review_positions()["positions"][0]
    assert row["action"] == "CLOSE" and "stop level breached" in row["reason"]


@pytest.mark.parametrize("trades, why", [
    ([(-310, 3)], "weekly loss limit"),
    ([(-10, 0), (-10, 0), (-10, 0)], "consecutive losing trades"),
])
def test_circuit_breakers(session, trades, why):
    for pnl, days_ago in trades:
        session.state.trade_log.append({"symbol": "IWM", "pnl": pnl,
                                        "closed_at": (TODAY - timedelta(days=days_ago)).isoformat() + "T15:00:00+00:00"})
    res = session.propose_option_trade(OID, 1, 1.55, "t")
    assert any(why in p for p in res["problems"]), res


def test_unrealized_losses_count_toward_daily_limit(session):
    open_paper(session)
    pre(session, RH + "place_option_order", stop())
    # Entry 3.15, now marked 1.55: $160 open loss trips the $150 daily limit for new entries.
    session.state.positions[OID]["entry_price"] = 3.15
    assert any("daily loss limit" in t for t in risk.circuit_breakers(session.cfg, session.state, session.market, TODAY))


def live_session(cfg):
    s = TradingSession(dataclasses.replace(cfg, live_trading=True), today=TODAY)
    s.signals["SPY"] = {"votes": {"sma": 1, "rsi": 0, "macd": 1}, "score": 0.67, "decision": "BUY", "rsi": 55}
    s.market.ingest("get_option_instruments", {}, instruments_resp())
    s.market.ingest("get_option_quotes", {}, quotes_resp())
    s.market.ingest("get_earnings_results", {"symbol": "SPY"}, mcp({"data": {"results": []}}))
    return s


def post(s, tool, args, payload):
    run(s.post_tool_use({"tool_name": RH + tool, "tool_input": args, "tool_response": mcp(payload)}, "t", None))


def test_live_stop_out_books_actual_fill_and_blocks_naked_close(cfg):
    s = live_session(cfg)
    s.propose_option_trade(OID, 1, 1.55, "t")
    post(s, "place_option_order", order(), {"data": {"id": "open-1", "state": "queued"}})
    # Broker confirms the fill at 1.52 (better than the 1.55 limit).
    s.market.ingest("get_option_positions", {"account_number": ACCT, "nonzero": True},
                    mcp({"data": {"positions": [{"option_id": OID, "quantity": "1", "type": "long"}]}}))
    s.market.ingest("get_option_orders", {"account_number": ACCT}, mcp({"data": {"orders": [
        broker_order("open-1", "filled", premium="152")]}}))
    row = s.review_positions()["positions"][0]
    assert s.state.positions[OID]["entry_price"] == pytest.approx(1.52)
    assert row["action"] == "PLACE_STOP" and row["stop_order"]["stop_price"] == "0.99"

    args = {**row["stop_order"], "account_number": ACCT}
    assert pre(s, RH + "place_option_order", args) == {}
    post(s, "place_option_order", args, {"data": {"id": "stop-1", "state": "confirmed"}})
    assert s.state.positions[OID]["stop"]["order_id"] == "stop-1"
    # A limit close can't be sent while the stop reserves the contracts.
    assert "cancel_option_order it first" in reason(pre(s, RH + "place_option_order", order(effect="close", side="sell", price="1.50")))

    # Overnight the stop fires at the broker; next run reconciles the real fill.
    s.market.ingest("get_option_orders", {"account_number": ACCT}, mcp({"data": {"orders": [
        broker_order("stop-1", "filled", premium="96", typ="market", trigger="stop", side="sell", effect="close", stop_price="0.99")]}}))
    s.market.broker_positions = {}
    ev = s.review_positions()["broker_events"]
    assert ev[0]["kind"] == "stopped_out" and OID not in s.state.positions
    assert s.state.trade_log[-1]["pnl"] == pytest.approx(-56.0)   # (0.96 - 1.52) * 100


def test_live_close_is_pending_until_filled(cfg):
    s = live_session(cfg)
    s.propose_option_trade(OID, 1, 1.55, "t")
    post(s, "place_option_order", order(), {"data": {"id": "open-1", "state": "queued"}})
    s.state.positions[OID]["filled"] = True
    close = order(effect="close", side="sell", price="2.40")
    assert pre(s, RH + "place_option_order", close) == {}
    post(s, "place_option_order", close, {"data": {"id": "close-1", "state": "queued"}})
    assert OID in s.state.positions and s.state.trade_log == []          # not booked yet
    s.market.broker_positions = {OID: 1}
    assert s.review_positions()["positions"][0]["action"] == "PENDING_CLOSE"
    s.market.ingest("get_option_orders", {"account_number": ACCT}, mcp({"data": {"orders": [
        broker_order("close-1", "filled", premium="241", side="sell", effect="close")]}}))
    s.review_positions()
    assert OID not in s.state.positions and s.state.trade_log[-1]["pnl"] == pytest.approx(86.0)
