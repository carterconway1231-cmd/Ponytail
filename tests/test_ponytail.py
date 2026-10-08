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


def sig(decision, **kw):
    """A compute_signals result as the session stores it."""
    return {"decision": decision, "score": 0.5, "conviction": 0.5, "bucket": "medium",
            "agree": ["trend", "macd", "structure", "ote"], "oppose": ["rsi"], "p_win": 0.5,
            "expected_r": None, "bucket_trades": 0, "regime": "trend",
            "factors": {"trend": {"score": 1.0}, "macd": {"score": 0.6}, "rsi": {"score": -0.5},
                        "structure": {"score": 1.0}, "ote": {"score": 0.0}}, **kw}


@pytest.fixture
def cfg(tmp_path):
    return Config(account_number=ACCT, symbols=["SPY", "QQQ"], state_path=str(tmp_path / "state.json"))


@pytest.fixture
def session(cfg):
    s = TradingSession(cfg, today=TODAY)
    s.signals["SPY"] = sig("BUY")
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


# ---- market cache ---------------------------------------------------------

def test_ingest_parses_real_shapes():
    m = MarketCache()
    m.ingest("get_equity_historicals", {}, mcp({"data": {"results": [{"symbol": "SPY", "interval": "day", "bars": [
        {"close_price": "765.61"}, {"close_price": "764.20"},
        {"close_price": "779.09", "interpolated": True}]}]}}))
    assert [b["close_price"] for b in m.bars["SPY"]["day"]] == ["765.61", "764.20"]  # gap-fill dropped
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
    # Winning call: bullish factors were right, the bearish RSI read was wrong; OTE had no opinion.
    lr = session.learner
    assert lr.hit_rate("trend", "trend") > 0.5 and lr.hit_rate("structure", "trend") > 0.5
    assert lr.hit_rate("rsi", "trend") < 0.5 and lr.hit_rate("ote", "trend") == 0.5
    assert lr.d["trades_learned"] == 1

    # State persisted to disk.
    reloaded = json.load(open(session.cfg.state_path))
    assert reloaded["trade_log"][-1]["pnl"] == pytest.approx(85.0)


def test_live_mode_allows_and_books_on_success(cfg):
    s = TradingSession(dataclasses.replace(cfg, live_trading=True), today=TODAY)
    s.signals["SPY"] = sig("BUY")
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
    assert s.learner.d["trades_learned"] == 0  # no P&L known, nothing learned


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
    session.signals["QQQ"] = sig("BUY")
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
    # Losing call: bullish factors were wrong, the bearish RSI read was right.
    assert session.learner.hit_rate("trend", "trend") < 0.5 < session.learner.hit_rate("rsi", "trend")
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
    s.signals["SPY"] = sig("BUY")
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


# ---- factor engine ---------------------------------------------------------------

import os  # noqa: E402

from ponytail import factors as fx  # noqa: E402
from ponytail.learner import Learner  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def real_bars(kind):
    return [b for b in json.load(open(os.path.join(FIX, f"spy_{kind}.json"))) if not b.get("interpolated")]


def hbars(rows):
    """rows of (o, h, l, c, v) -> Robinhood-shaped bars."""
    return [{"open_price": o, "high_price": h, "low_price": l, "close_price": c, "volume": v,
             "begins_at": f"2026-09-{1 + i // 7:02d}T{14 + i % 7}:00:00Z"} for i, (o, h, l, c, v) in enumerate(rows)]


def drift(n, start=100.0, step=0.0, wiggle=0.3, v=1000):
    rows, p = [], start
    for i in range(n):
        o = p
        p = p + step + (wiggle if i % 2 else -wiggle)
        rows.append((o, max(o, p) + 0.2, min(o, p) - 0.2, p, v))
    return rows


def test_real_spy_data_produces_all_factors():
    a = fx.compute_factors(real_bars("day"), real_bars("hour"))
    assert set(a["factors"]) == set(fx.ALL_FACTORS)
    assert all(-1 <= f["score"] <= 1 and f["why"] for f in a["factors"].values())
    assert a["regime"] in ("trend", "range") and a["has_hourly"]


def test_daily_only_still_scores():
    a = fx.compute_factors(real_bars("day"))
    assert set(a["factors"]) == set(fx.DAILY_FACTORS) and not a["has_hourly"]


def test_fvg_retest_detected():
    rows = drift(40)
    base = rows[-1][3]
    # displacement up leaves a gap between bar[-3].high and bar[-1].low, then price returns into it
    rows += [(base, base + 0.3, base - 0.2, base + 0.2, 1000),
             (base + 0.2, base + 3.0, base + 0.2, base + 2.9, 5000),
             (base + 2.9, base + 3.5, base + 1.0, base + 3.3, 2000),
             (base + 3.3, base + 3.4, base + 1.1, base + 1.2, 1500)]
    s, why = fx.f_fvg(fx.bars_to_df(hbars(rows)))
    assert s == 1.0 and "bullish FVG" in why


def path(segments, start=100.0, v=1000):
    """Price path from (bars, step) legs with clean swing points."""
    rows, p = [], start
    for n, step in segments:
        for _ in range(n):
            o, p = p, p + step
            if step > 0:
                rows.append((o, p + 0.1, o - 0.05, p, v))
            else:
                rows.append((o, o + 0.05, p - 0.1, p, v))
    return rows


def test_liquidity_sweep_detected():
    rows = path([(6, 1), (3, -1), (4, 1), (2, -0.5)])
    swing_low = min(r[2] for r in rows[6:9])
    last = rows[-1][3]
    rows.append((last, last + 0.1, swing_low - 0.8, last + 0.2, 3000))  # wick under the lows, close back above
    s, why = fx.f_liquidity_sweep(fx.bars_to_df(hbars(rows)))
    assert s > 0 and "sell-side liquidity swept" in why


def test_structure_break_and_choch():
    up = path([(5, 1), (2, -1)] * 4 + [(5, 1)])          # higher highs: bullish BOS
    s, why = fx.f_structure(fx.bars_to_df(hbars(up)))
    assert s > 0 and "bullish BOS" in why
    down = up + path([(10, -1.5)], start=up[-1][3])        # breaks the last higher low: shift
    s, why = fx.f_structure(fx.bars_to_df(hbars(down)))
    assert s < 0 and "bearish CHoCH" in why


def test_order_flow_reads_closing_pressure():
    buying = [(100, 101, 99, 100.95, 1000)] * 25   # every bar closes at its high
    selling = [(100, 101, 99, 99.05, 1000)] * 25
    assert fx.f_order_flow(fx.bars_to_df(hbars(buying)))[0] > 0.8
    assert fx.f_order_flow(fx.bars_to_df(hbars(selling)))[0] < -0.8


def test_ote_zone():
    # Break of structure up, impulse leg 101.9 -> 111.1, then a ~70% pullback into the OTE zone.
    rows = path([(4, 1), (2, -1), (6, 1.5), (3, -2.1)])
    s, why = fx.f_ote(fx.bars_to_df(hbars(rows)))
    assert s == 1.0 and "OTE zone" in why, why
    shallow = path([(4, 1), (2, -1), (6, 1.5), (3, -0.6)])   # only ~20% back: not an entry yet
    s, why = fx.f_ote(fx.bars_to_df(hbars(shallow)))
    assert s == 0.0 and "not retraced enough" in why, why


# ---- learner -------------------------------------------------------------------------

def test_learner_upweights_winners_and_fades_losers(cfg):
    lr = Learner({}, cfg)
    assert lr.weight("ote", "trend") == pytest.approx(1.0)
    entry = {"factors": {"ote": {"score": 1.0}, "rsi": {"score": 1.0}}, "regime": "trend", "conviction": 0.5}
    for _ in range(8):
        lr.learn_trade(entry, direction=1, pnl=60, premium=150)          # bullish OTE calls keep winning
    assert lr.weight("ote", "trend") > 1.3
    entry2 = {"factors": {"rsi": {"score": 1.0}}, "regime": "trend", "conviction": 0.5}
    for _ in range(16):
        lr.learn_trade(entry2, direction=1, pnl=-60, premium=150)        # RSI-only calls keep losing
    assert lr.weight("rsi", "trend") < 0.8
    # Regime-specific: trend-regime evidence moves the range weight less.
    assert abs(lr.weight("ote", "range") - 1) < abs(lr.weight("ote", "trend") - 1)


def test_learner_shadow_labels_untraded_signals(cfg):
    lr = Learner({}, cfg)
    analysis = {"close": 100.0, "atr": 2.0, "regime": "range",
                "factors": {"fvg": {"score": 1.0}, "vwap": {"score": -1.0}}}
    lr.record_snapshot("SPY", "2026-09-01", analysis, {"decision": "BUY", "conviction": 0.5, "score": 0.5})
    later = [{"begins_at": f"2026-09-{d:02d}T00:00:00Z", "close_price": str(100 + d)} for d in range(2, 9)]
    assert lr.label_snapshots("SPY", later[:3]) == 0      # horizon not reached
    assert lr.label_snapshots("SPY", later) == 1          # +6 after 5 days = 3 ATR up
    assert lr.hit_rate("fvg", "range") > 0.5 > lr.hit_rate("vwap", "range")
    assert lr.d["snapshots"] == [] and lr.d["shadow_learned"] == 1


def test_decide_requires_confluence(cfg):
    lr = Learner({}, cfg)
    lone = {"trend": {"score": 1.0}, "macd": {"score": 0.0}, "rsi": {"score": 0.0}}
    d = lr.decide(lone, "trend")
    assert d["decision"] == "HOLD" and any("factors agree" in r for r in d["hold_reasons"])
    stacked = {k: {"score": 0.8} for k in ("trend", "macd", "structure", "ote", "fvg")}
    assert lr.decide(stacked, "trend")["decision"] == "BUY"
    conflicted = {**stacked, "vwap": {"score": -0.9}, "order_flow": {"score": -0.9}, "rsi": {"score": -0.9}}
    assert lr.decide(conflicted, "trend")["decision"] == "HOLD"


def test_learned_odds_gate_blocks_losing_setups(session):
    session.signals["SPY"].update(bucket_trades=12, p_win=0.40, expected_r=-0.15)
    problems = risk.check_open(session.cfg, session.state, session.market, session.signals, OID, 1, 1.55, TODAY)
    assert any("learned win rate" in p for p in problems) and any("expected return" in p for p in problems)


def test_compute_signals_end_to_end_on_real_bars(session):
    session.market.ingest("get_equity_historicals", {}, mcp({"data": {"results": [
        {"symbol": "SPY", "interval": "day", "bars": real_bars("day")},
        {"symbol": "SPY", "interval": "hour", "bars": real_bars("hour")}]}}))
    out = session.compute_signals("SPY")
    assert out["decision"] in ("BUY", "SELL", "HOLD") and out["hourly_factors"] == "included"
    assert len(out["factors"]) == len(fx.ALL_FACTORS)
    assert all("learned_weight" in f and f["why"] for f in out["factors"].values())
    assert session.learner.d["snapshots"][-1]["symbol"] == "SPY"
    assert session.learner.report()["factors"][0]["factor"] in fx.ALL_FACTORS


# ---- warm start & context compaction -----------------------------------------------

from ponytail import warmstart  # noqa: E402


def test_evidence_decays_with_half_life(cfg):
    lr = Learner({}, cfg, today=TODAY)
    st = lr.d["factors"]["fvg"]["global"]
    lr._add(st, 10.0, 0.0, TODAY - timedelta(days=90))
    assert lr._view(st)[0] == pytest.approx(5.0)          # one half-life later: half the evidence
    lr._add(st, 0.0, 4.0, TODAY - timedelta(days=200))   # out-of-order add never decays backwards
    assert st["asof"] == (TODAY - timedelta(days=90)).isoformat()


def test_warm_start_replays_without_lookahead(cfg, monkeypatch):
    seen = []
    real = warmstart.compute_factors

    def spy(daily, hourly=None):
        seen.append((daily[-1]["begins_at"][:10], hourly[-1]["begins_at"][:10] if hourly else None))
        return real(daily, hourly)

    monkeypatch.setattr(warmstart, "compute_factors", spy)
    lr = Learner({}, cfg, today=TODAY)
    out = warmstart.replay(lr, {"SPY": {"day": real_bars("day"), "hour": real_bars("hour")}})
    days = [b["begins_at"][:10] for b in real_bars("day")]
    assert out["days_replayed"] == len(days) - warmstart.MIN_HISTORY + 1 - cfg.shadow_horizon
    # Each replayed day only ever saw bars up to that day, on both timeframes.
    replayed = sorted(d for d in days[warmstart.MIN_HISTORY - 1:len(days) - cfg.shadow_horizon])
    assert [d for d, _ in seen] == replayed
    assert all(h is None or h == d for d, h in seen)
    assert out["days_with_hourly_factors"] > 0
    assert out["graded"] + out["flat_skipped"] == out["days_replayed"]
    assert lr.d["warm_start"]["samples"] == out["graded"] > 0
    assert set(out["historical_hit_rates"]) <= set(fx.ALL_FACTORS)
    # Incremental: replaying the same history again adds nothing.
    assert warmstart.replay(lr, {"SPY": {"day": real_bars("day")}})["days_replayed"] == 0


def test_warm_start_tool_and_kickoff(session):
    assert "Warm start needed for: SPY, QQQ" in session.kickoff()
    session.market.ingest("get_equity_historicals", {}, mcp({"data": {"results": [
        {"symbol": "SPY", "interval": "day", "bars": real_bars("day")}]}}))
    out = session.warm_start()
    assert out["graded"] > 0 and out["still_needed"] == ["QQQ"]
    assert "Warm start needed for: QQQ." in session.kickoff()
    assert json.load(open(session.cfg.state_path))["learner"]["warm_start"]["symbols"]["SPY"]


def test_large_responses_saved_to_file_are_followed(tmp_path):
    from ponytail.market import decode_tool_response
    f = tmp_path / "mcp-Robinhood-get_equity_historicals-1.txt"
    f.write_text(json.dumps({"data": {"results": [{"symbol": "SPY", "bars": []}]}}))
    stub = f"Error: result (780,071 characters) exceeds maximum allowed tokens. Output has been saved to {f}.\nFormat: ..."
    assert decode_tool_response(stub)["data"]["results"][0]["symbol"] == "SPY"


def test_bar_dumps_are_replaced_with_a_summary_for_the_model(session):
    resp = mcp({"data": {"results": [{"symbol": "SPY", "interval": "day", "bars": real_bars("day")}]}})
    out = run(session.post_tool_use({"tool_name": RH + "get_equity_historicals", "tool_input": {}, "tool_response": resp}, "t", None))
    text = out["hookSpecificOutput"]["updatedToolOutput"][0]["text"]
    assert "SPY day: 206 bars" in text and len(text) < 300
    assert len(session.market.bars["SPY"]["day"]) == 206   # full data still stored for the engine
