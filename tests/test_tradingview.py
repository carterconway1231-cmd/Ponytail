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
