import json

import pytest

from trading import bot


def quotes_file(tmp_path, name, rows):
    """rows: symbol -> (last, prev_close)."""
    res = [{"quote": {"symbol": s, "last_trade_price": str(last), "adjusted_previous_close": str(prev),
                      "venue_last_trade_time": "2026-10-12T15:00:00Z", "state": "active"}}
           for s, (last, prev) in rows.items()]
    p = tmp_path / name
    p.write_text(json.dumps({"data": {"results": res}}))
    return str(p)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "LEDGER", str(tmp_path / "ledger.json"))
    monkeypatch.setattr(bot, "DASHBOARD", str(tmp_path / "dashboard.html"))
    return tmp_path


def run(*argv, capsys=None):
    bot.main(list(argv))
    return json.loads(capsys.readouterr().out) if capsys else None


def test_scan_separates_external_drops_from_company_news_and_finds_standouts(ledger, capsys):
    f = quotes_file(ledger, "q.json", {
        "SPY": (98.0, 100.0), "XLK": (97.0, 100.0), "SMH": (100.5, 100.0), "XLV": (100.0, 100.0),
        "AAPL": (96.5, 100.0),    # -3.5% with tech -3%: external
        "MSFT": (97.2, 100.0),    # -2.8%: external
        "UNH": (93.0, 100.0),     # -7% while health flat: company-specific
        "NVDA": (104.0, 100.0),   # +4% vs semis +0.5%: leader
        "AMD": (100.2, 100.0), "AVGO": (100.6, 100.0), "MU": (100.4, 100.0),
    })
    out = run("scan", f, capsys=capsys)
    mr = {r["symbol"]: r["cause_hint"] for r in out["mean_reversion"]}
    assert mr["AAPL"].startswith("external") and mr["MSFT"].startswith("external")
    assert mr["UNH"].startswith("likely company-specific")
    mom = {r["symbol"]: r for r in out["momentum"]}
    assert set(mom) == {"NVDA"} and mom["NVDA"]["standout"] and mom["NVDA"]["rs"] == 3.5
    assert "GOOGL" in out["missing"]
    assert any(c["name"] == "SPY" and c["chg"] == -2.0 for c in bot.load()["regime"]["chips"])


def test_trend_flags_a_real_downtrend_and_a_held_base():
    def bars(closes):
        return [{"close_price": c, "high_price": c + 0.5, "low_price": c - 0.5} for c in closes]
    down = bot.trend(bars([100 - i for i in range(30)]))
    assert down["downtrend"] and down["weekly_lower_highs_lows"] == "3/3" and not down["higher_low"]
    up = bot.trend(bars([100 + i * 0.5 for i in range(60)]))
    assert not up["downtrend"] and up["higher_low"] and up["sma10_rising"] and up["support_held"]


OPEN = ["--strategy", "momentum", "--tier", "standard", "--entry", "100", "--qty", "0.1",
        "--stop", "97", "--target", "106", "--horizon", "5", "--thesis", "leads semis on upgrade, held 10d base"]


def test_open_check_close_lifecycle_with_r_and_score(ledger, capsys):
    run("open", "NVDA", *OPEN, capsys=capsys)
    # cushion (99-97)/3 = 0.67R: hold, not yet fast cadence
    rows = run("check", quotes_file(ledger, "a.json", {"NVDA": (99.0, 100.0)}), capsys=capsys)["positions"]
    assert rows[0]["action"] == "HOLD" and rows[0]["cushion_r"] == 0.67 and not rows[0]["fast_cadence"]
    # 97.6 is 0.2R above the stop: exit before the stop prints
    rows = run("check", quotes_file(ledger, "b.json", {"NVDA": (97.6, 100.0)}), capsys=capsys)["positions"]
    assert rows[0]["action"] == "EXIT_NEAR_STOP" and rows[0]["fast_cadence"]
    t = run("close", "NVDA", "--exit", "97.6", "--reason", "near stop", "--setup", "30", "--execution", "30",
            "--outcome", "15", "--note", "base failed", capsys=capsys)
    assert t["pnl"] == -0.24 and t["r"] == -0.8 and t["score"]["total"] == 75
    st = run("status", capsys=capsys)
    assert st["open_positions"] == "0/2" and st["metrics"]["r_distribution"]["-1R..0"] == 1
    assert st["day_trades_last_5_sessions"] == 1 and st["metrics"]["organic"]["n"] == 1


def test_target_trim_and_horizon_actions(ledger, capsys):
    run("open", "NVDA", *OPEN, capsys=capsys)
    assert run("check", quotes_file(ledger, "t.json", {"NVDA": (106.5, 100.0)}),
               capsys=capsys)["positions"][0]["action"] == "EXIT_TARGET"
    led = bot.load()
    led["positions"][0]["target"] = 120
    bot.save(led)
    assert run("check", quotes_file(ledger, "r.json", {"NVDA": (106.5, 100.0)}),
               capsys=capsys)["positions"][0]["action"] == "TRIM"
    led = bot.load()
    led["positions"][0]["opened_day"] = "2026-01-02"
    bot.save(led)
    assert run("check", quotes_file(ledger, "h.json", {"NVDA": (101.0, 100.0)}),
               capsys=capsys)["positions"][0]["action"] == "EXIT_HORIZON"


def test_account_gates_refuse_entries(ledger, capsys):
    run("open", "NVDA", *OPEN, capsys=capsys)
    with pytest.raises(SystemExit, match="no adding / averaging down"):
        bot.main(["open", "NVDA", *OPEN])
    run("open", "AAPL", *OPEN, capsys=capsys)
    with pytest.raises(SystemExit, match="position slots in use"):
        bot.main(["open", "MSFT", *OPEN])
    with pytest.raises(SystemExit, match="stop must be below entry"):
        bot.main(["open", "MSFT", *OPEN[:-6], "--stop", "101", "--target", "106", "--horizon", "5", "--thesis", "x" * 12])


def test_daily_loss_limit_counts_unrealized_and_blocks_entries(ledger, capsys):
    led = bot.load()
    led["positions"].append({"symbol": "XOM", "entry": 100.0, "qty": 0.1, "stop": 90.0, "target": 120.0,
                             "horizon_days": 3, "opened_day": "2026-01-02", "strategy": "mean_reversion"})
    bot.save(led)
    out = run("check", quotes_file(ledger, "x.json", {"XOM": (40.0, 95.0)}), capsys=capsys)
    assert out["daily_pnl"] == -5.5   # (40 - 95 prev close) * 0.1: today's move only
    assert any("daily loss limit" in b for b in run("gate", "AAPL", capsys=capsys)["blockers"])


def test_drawdown_breaker_halves_size_and_pauses_entries(ledger, capsys):
    run("equity", "50", capsys=capsys)
    assert not run("equity", "41", capsys=capsys)["breaker"]["tripped"]
    out = run("equity", "39.9", capsys=capsys)
    assert out["breaker"]["tripped"] and out["drawdown"] == 0.202
    g = run("gate", "AAPL", capsys=capsys)
    assert g["size"] == 5.0 and any("circuit breaker" in b for b in g["blockers"])


def test_render_escapes_and_embeds_ledger(ledger, capsys):
    run("open", "NVDA", *OPEN[:-1], "</script><b>x</b>", capsys=capsys)
    run("equity", "29.42", capsys=capsys)
    run("render", capsys=capsys)
    page = (ledger / "dashboard.html").read_text()
    assert "</script><b>" not in page and "<\\/script>" in page
    assert "Agentic ••0222" in page and '"symbol": "NVDA"' in page
