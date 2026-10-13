import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_pine_scripts_are_built_from_src():
    res = subprocess.run([sys.executable, str(ROOT / "tradingview" / "build.py"), "--check"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stdout


def test_pine_scripts_start_with_version_and_share_core():
    core = (ROOT / "tradingview" / "src" / "core.pine").read_text().strip()
    for name in ("ICT_OTE_Indicator.pine", "ICT_OTE_Strategy.pine"):
        text = (ROOT / "tradingview" / name).read_text()
        assert text.startswith("//@version=6\n")
        assert core in text
        assert "\t" not in text  # Pine indentation must be spaces


# ─── GEX levels ─────────────────────────────────────────────────────
import datetime as dt
import json as _json

sys.path.insert(0, str(ROOT / "tradingview"))
import gex  # noqa: E402
from ponytail.options_math import bs_delta, bs_gamma  # noqa: E402

TODAY = dt.date(2026, 10, 13)


def _c(kind, strike, oi, iv=0.2, days=30):
    return {"symbol": "SPY", "type": kind, "strike": strike, "oi": oi, "iv": iv, "gamma": None,
            "expiration": TODAY + dt.timedelta(days=days)}


def test_bs_gamma_matches_delta_slope():
    h = 0.01
    num = (bs_delta(100 + h, 100, 0.1, 0.25, "call") - bs_delta(100 - h, 100, 0.1, 0.25, "call")) / (2 * h)
    assert abs(bs_gamma(100, 100, 0.1, 0.25) - num) < 1e-6


def test_walls_and_flip():
    contracts = [_c("call", 110, 50_000), _c("call", 105, 5_000),
                 _c("put", 90, 50_000), _c("put", 95, 5_000)]
    res = gex.compute(contracts, 100.0, TODAY)
    assert res["call_wall"] == 110 and res["put_wall"] == 90
    # symmetric book: gamma is put-heavy below 100, call-heavy above
    assert res["flip"] is not None and abs(res["flip"] - 100) < 1.0
    assert gex.total_gex(contracts, 106, TODAY) > 0 > gex.total_gex(contracts, 94, TODAY)


def test_no_flip_when_gamma_one_sided():
    res = gex.compute([_c("call", 100, 10_000), _c("call", 105, 10_000)], 100.0, TODAY)
    assert res["flip"] is None and res["net"] > 0
    assert "flip=" not in gex.format_line("SPY", res, TODAY)


def test_load_joins_raw_mcp_responses(tmp_path):
    inst = {"data": {"instruments": [
        {"id": "a", "chain_symbol": "SPY", "type": "call", "strike_price": "780.0000", "expiration_date": "2026-10-16"},
        {"id": "b", "chain_symbol": "SPY", "type": "put", "strike_price": "770.0000", "expiration_date": "2026-10-16"},
        {"id": "c", "chain_symbol": "SPY", "type": "put", "strike_price": "760.0000", "expiration_date": "2026-10-16"}]}}
    quotes = {"data": {"results": [
        {"quote": {"instrument_id": "a", "implied_volatility": "0.15", "gamma": "0.02", "open_interest": 9000}},
        {"quote": {"instrument_id": "b", "implied_volatility": "0.18", "gamma": "0.015", "open_interest": 12000}}]}}
    (tmp_path / "i.json").write_text(_json.dumps(inst))
    (tmp_path / "q.json").write_text(_json.dumps(quotes))
    contracts = gex.load([tmp_path / "i.json", tmp_path / "q.json"])
    assert sorted(c["strike"] for c in contracts) == [770.0, 780.0]  # c has no quote
    line = gex.format_line("SPY", gex.compute(contracts, 775.0, TODAY), TODAY)
    assert line.startswith("SPY cw=780 pw=770 ") and line.endswith("d=2026-10-13")
