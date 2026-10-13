"""SQLite journal: positions, every decision (including rejections), dedupe."""
import json
import sqlite3
import time
from datetime import datetime, timezone

from .models import Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT, symbol TEXT, creator TEXT, mode TEXT,
    status TEXT DEFAULT 'open',
    entry_sol REAL, tokens REAL, entry_price REAL, opened_at REAL,
    take_profit_pct REAL, stop_loss_pct REAL, trailing_stop_pct REAL, max_hold_minutes REAL,
    high_water_price REAL, tp_taken INTEGER DEFAULT 0, realized_sol REAL DEFAULT 0,
    closed_at REAL, exit_reason TEXT, entry_sig TEXT, exit_sigs TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, mint TEXT, symbol TEXT, source TEXT,
    passed_safety INTEGER, hard_fails TEXT, warnings TEXT, metrics TEXT,
    action TEXT, confidence REAL, size_sol REAL, reasoning TEXT,
    model TEXT, cost_usd REAL DEFAULT 0, latency_s REAL DEFAULT 0, executed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen (mint TEXT PRIMARY KEY, ts REAL);
CREATE INDEX IF NOT EXISTS idx_dec_ts ON decisions(ts);
"""

POSITION_FIELDS = ("mint", "symbol", "entry_sol", "tokens", "entry_price", "opened_at",
                   "take_profit_pct", "stop_loss_pct", "trailing_stop_pct", "max_hold_minutes",
                   "high_water_price", "tp_taken", "realized_sol", "id")


def day_start(now=None):
    d = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- dedupe --------------------------------------------------------------
    def mark_seen(self, mint):
        """True if this mint is new (and records it)."""
        cur = self.db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (mint, time.time()))
        self.db.commit()
        return cur.rowcount == 1

    # --- positions -----------------------------------------------------------
    def open_position(self, p: Position, creator, mode, sig=""):
        cur = self.db.execute(
            "INSERT INTO positions (mint, symbol, creator, mode, entry_sol, tokens, entry_price, "
            "opened_at, take_profit_pct, stop_loss_pct, trailing_stop_pct, max_hold_minutes, "
            "high_water_price, entry_sig) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.mint, p.symbol, creator, mode, p.entry_sol, p.tokens, p.entry_price, p.opened_at,
             p.take_profit_pct, p.stop_loss_pct, p.trailing_stop_pct, p.max_hold_minutes,
             p.entry_price, sig))
        self.db.commit()
        p.id = cur.lastrowid
        p.high_water_price = p.high_water_price or p.entry_price
        return p

    def open_positions(self):
        rows = self.db.execute("SELECT * FROM positions WHERE status='open'").fetchall()
        return [Position(**{k: r[k] for k in POSITION_FIELDS}) for r in rows]

    def save_position(self, p: Position):
        self.db.execute("UPDATE positions SET tokens=?, high_water_price=?, tp_taken=?, "
                        "realized_sol=? WHERE id=?",
                        (p.tokens, p.high_water_price, int(p.tp_taken), p.realized_sol, p.id))
        self.db.commit()

    def add_exit_sig(self, pid, sig):
        row = self.db.execute("SELECT exit_sigs FROM positions WHERE id=?", (pid,)).fetchone()
        sigs = json.loads(row["exit_sigs"] or "[]") + [sig]
        self.db.execute("UPDATE positions SET exit_sigs=? WHERE id=?", (json.dumps(sigs), pid))
        self.db.commit()

    def close_position(self, p: Position, reason):
        self.db.execute("UPDATE positions SET status='closed', closed_at=?, exit_reason=?, "
                        "tokens=?, realized_sol=? WHERE id=?",
                        (time.time(), reason, p.tokens, p.realized_sol, p.id))
        self.db.commit()

    def closed_positions(self, limit=20):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT ?", (limit,))]

    # --- risk inputs ---------------------------------------------------------
    def spent_today(self):
        return self.db.execute("SELECT COALESCE(SUM(entry_sol),0) FROM positions WHERE opened_at>=?",
                               (day_start(),)).fetchone()[0]

    def realized_pnl_today(self):
        return self.db.execute(
            "SELECT COALESCE(SUM(realized_sol - entry_sol),0) FROM positions "
            "WHERE status='closed' AND closed_at>=?", (day_start(),)).fetchone()[0]

    def entries_since(self, ts):
        return self.db.execute("SELECT COUNT(*) FROM positions WHERE opened_at>=?", (ts,)).fetchone()[0]

    def creator_last_entry(self, creator):
        if not creator:
            return None
        row = self.db.execute("SELECT MAX(opened_at) FROM positions WHERE creator=?",
                              (creator,)).fetchone()
        return row[0]

    def claude_spend_today(self):
        return self.db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM decisions WHERE ts>=?",
                               (day_start(),)).fetchone()[0]

    # --- decisions -----------------------------------------------------------
    def record_decision(self, launch, report, decision=None, executed=False):
        d = decision
        self.db.execute(
            "INSERT INTO decisions (ts, mint, symbol, source, passed_safety, hard_fails, warnings, "
            "metrics, action, confidence, size_sol, reasoning, model, cost_usd, latency_s, executed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), launch.mint, launch.symbol, launch.source, int(report.passed),
             json.dumps(report.hard_fails), json.dumps(report.warnings),
             json.dumps(report.metrics, default=str),
             d.action if d else "REJECT", d.confidence if d else 0, d.size_sol if d else 0,
             d.reasoning if d else "; ".join(report.hard_fails), d.model if d else "",
             d.cost_usd if d else 0, d.latency_s if d else 0, int(executed)))
        self.db.commit()

    def decision_stats(self, since):
        rows = self.db.execute("SELECT action, COUNT(*) n, SUM(cost_usd) c FROM decisions "
                               "WHERE ts>=? GROUP BY action", (since,)).fetchall()
        return {r["action"]: {"count": r["n"], "claude_usd": round(r["c"] or 0, 4)} for r in rows}
