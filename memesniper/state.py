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
CREATE TABLE IF NOT EXISTS launches (mint TEXT PRIMARY KEY, creator TEXT, ts REAL, source TEXT);
CREATE INDEX IF NOT EXISTS idx_launch_creator ON launches(creator, ts);
CREATE TABLE IF NOT EXISTS outcomes (
    mint TEXT PRIMARY KEY, symbol TEXT, creator TEXT, source TEXT,
    verdict TEXT, reason TEXT, ts REAL, ref_sol REAL, ref_tokens REAL,
    r5m REAL, r15m REAL, r1h REAL, r4h REAL, done INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_out_creator ON outcomes(creator);
CREATE INDEX IF NOT EXISTS idx_out_done ON outcomes(done);
CREATE TABLE IF NOT EXISTS links (link TEXT, key TEXT, ts REAL, PRIMARY KEY (link, key));
CREATE TABLE IF NOT EXISTS watchlist (
    key TEXT PRIMARY KEY, chain TEXT, token TEXT, symbol TEXT, creator TEXT, source TEXT,
    added_at REAL, expires_at REAL, last_signal_at REAL DEFAULT 0
);
"""

# Columns added after the first release; existing databases get them on open.
MIGRATIONS = {
    "positions": {"chain": "TEXT DEFAULT 'solana'", "strategy": "TEXT DEFAULT 'launch'",
                  "entry_usd": "REAL DEFAULT 0", "realized_usd": "REAL DEFAULT 0"},
    "decisions": {"chain": "TEXT DEFAULT 'solana'", "strategy": "TEXT DEFAULT 'launch'",
                  "size_usd": "REAL DEFAULT 0"},
    "launches": {"chain": "TEXT DEFAULT 'solana'"},
    "outcomes": {"chain": "TEXT DEFAULT 'solana'", "strategy": "TEXT DEFAULT 'launch'", "token": "TEXT"},
}

POSITION_FIELDS = ("mint", "symbol", "entry_sol", "tokens", "entry_price", "opened_at",
                   "take_profit_pct", "stop_loss_pct", "trailing_stop_pct", "max_hold_minutes",
                   "high_water_price", "tp_taken", "realized_sol", "id", "chain", "strategy",
                   "entry_usd", "realized_usd")


def day_start(now=None):
    d = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self.db.commit()

    # --- dedupe --------------------------------------------------------------
    def mark_seen(self, key):
        """True if this token (launch.key) is new, and records it."""
        cur = self.db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (key, time.time()))
        self.db.commit()
        return cur.rowcount == 1

    # --- positions -----------------------------------------------------------
    def open_position(self, p: Position, creator, mode, sig=""):
        cur = self.db.execute(
            "INSERT INTO positions (mint, symbol, creator, mode, entry_sol, tokens, entry_price, "
            "opened_at, take_profit_pct, stop_loss_pct, trailing_stop_pct, max_hold_minutes, "
            "high_water_price, entry_sig, chain, strategy, entry_usd) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.mint, p.symbol, creator, mode, p.entry_sol, p.tokens, p.entry_price, p.opened_at,
             p.take_profit_pct, p.stop_loss_pct, p.trailing_stop_pct, p.max_hold_minutes,
             p.entry_price, sig, p.chain, p.strategy, p.entry_usd))
        self.db.commit()
        p.id = cur.lastrowid
        p.high_water_price = p.high_water_price or p.entry_price
        return p

    def open_positions(self):
        rows = self.db.execute("SELECT * FROM positions WHERE status='open'").fetchall()
        return [Position(**{k: r[k] for k in POSITION_FIELDS}) for r in rows]

    def save_position(self, p: Position):
        self.db.execute("UPDATE positions SET tokens=?, high_water_price=?, tp_taken=?, "
                        "realized_sol=?, realized_usd=? WHERE id=?",
                        (p.tokens, p.high_water_price, int(p.tp_taken), p.realized_sol,
                         p.realized_usd, p.id))
        self.db.commit()

    def add_exit_sig(self, pid, sig):
        row = self.db.execute("SELECT exit_sigs FROM positions WHERE id=?", (pid,)).fetchone()
        sigs = json.loads(row["exit_sigs"] or "[]") + [sig]
        self.db.execute("UPDATE positions SET exit_sigs=? WHERE id=?", (json.dumps(sigs), pid))
        self.db.commit()

    def close_position(self, p: Position, reason):
        self.db.execute("UPDATE positions SET status='closed', closed_at=?, exit_reason=?, "
                        "tokens=?, realized_sol=?, realized_usd=? WHERE id=?",
                        (time.time(), reason, p.tokens, p.realized_sol, p.realized_usd, p.id))
        self.db.commit()

    def closed_positions(self, limit=20):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT ?", (limit,))]

    # --- risk inputs ---------------------------------------------------------
    def spent_today(self):
        """USD spent on entries today, across chains."""
        return self.db.execute("SELECT COALESCE(SUM(entry_usd),0) FROM positions WHERE opened_at>=?",
                               (day_start(),)).fetchone()[0]

    def realized_pnl_today(self):
        """USD profit/loss of positions closed today."""
        return self.db.execute(
            "SELECT COALESCE(SUM(realized_usd - entry_usd),0) FROM positions "
            "WHERE status='closed' AND closed_at>=?", (day_start(),)).fetchone()[0]

    def holding(self, chain, mint):
        return self.db.execute("SELECT 1 FROM positions WHERE status='open' AND chain=? AND mint=?",
                               (chain, mint)).fetchone() is not None

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

    # --- creator history -----------------------------------------------------
    def record_launch(self, launch):
        if launch.creator:
            self.db.execute("INSERT OR IGNORE INTO launches (mint, creator, ts, source, chain) "
                            "VALUES (?,?,?,?,?)",
                            (launch.mint, launch.creator, launch.seen_at, launch.source, launch.chain))
            self.db.commit()

    def creator_stats(self, creator, exclude_mint, now=None):
        """What this bot has seen of a creator's other launches."""
        now = now or time.time()
        empty = {"launches_seen": 0, "launches_24h": 0, "prior_tracked": 0, "prior_rugs": 0,
                 "best_prior_x": None}
        if not creator:
            return empty
        seen, day = self.db.execute(
            "SELECT COUNT(*), SUM(ts >= ?) FROM launches WHERE creator=? AND mint!=?",
            (now - 86_400, creator, exclude_mint)).fetchone()
        rows = self.db.execute("SELECT r5m, r15m, r1h, r4h FROM outcomes WHERE creator=? "
                               "AND COALESCE(token, mint)!=? AND strategy='launch'",
                               (creator, exclude_mint)).fetchall()
        rugs, best = 0, None
        for r in rows:
            early = [x for x in (r["r5m"], r["r15m"], r["r1h"]) if x is not None and x >= 0]
            if early and min(early) <= 0.1:
                rugs += 1
            vals = [x for x in r if x is not None and x >= 0]
            if vals:
                best = max(best or 0, max(vals))
        return {"launches_seen": seen or 0, "launches_24h": day or 0, "prior_tracked": len(rows),
                "prior_rugs": rugs, "best_prior_x": round(best, 2) if best is not None else None}

    # --- outcomes --------------------------------------------------------------
    def start_outcome(self, launch, verdict, reason, ref_sol, ref_tokens, strategy="launch"):
        """One row per decision. `mint` is the row key (a token can be decided on at launch and
        again on later watchlist signals); `token` is the address that gets re-quoted."""
        now = time.time()
        key = launch.mint if strategy == "launch" else f"{strategy}:{launch.mint}:{int(now)}"
        self.db.execute("INSERT OR IGNORE INTO outcomes (mint, symbol, creator, source, verdict, reason, "
                        "ts, ref_sol, ref_tokens, chain, strategy, token) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (key, launch.symbol, launch.creator, launch.source, verdict, reason,
                         now, ref_sol, ref_tokens, launch.chain, strategy, launch.mint))
        self.db.commit()

    def pending_outcomes(self):
        return self.db.execute("SELECT * FROM outcomes WHERE done=0 ORDER BY ts").fetchall()

    def update_outcome(self, mint, updates, done):
        cols = [c for c in updates if c in ("r5m", "r15m", "r1h", "r4h")]
        sets = ", ".join([f"{c}=?" for c in cols] + ["done=?"])
        self.db.execute(f"UPDATE outcomes SET {sets} WHERE mint=?",
                        [updates[c] for c in cols] + [int(done), mint])
        self.db.commit()

    def outcome_rows(self, since=0):
        return self.db.execute("SELECT * FROM outcomes WHERE ts>=?", (since,)).fetchall()

    # --- decisions -----------------------------------------------------------
    def record_decision(self, launch, report, decision=None, executed=False, strategy="launch"):
        d = decision
        self.db.execute(
            "INSERT INTO decisions (ts, mint, symbol, source, passed_safety, hard_fails, warnings, "
            "metrics, action, confidence, size_sol, reasoning, model, cost_usd, latency_s, executed, "
            "chain, strategy, size_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), launch.mint, launch.symbol, launch.source, int(report.passed),
             json.dumps(report.hard_fails), json.dumps(report.warnings),
             json.dumps(report.metrics, default=str),
             d.action if d else "REJECT", d.confidence if d else 0, d.size_native if d else 0,
             d.reasoning if d else "; ".join(report.hard_fails), d.model if d else "",
             d.cost_usd if d else 0, d.latency_s if d else 0, int(executed),
             launch.chain, strategy, d.size_usd if d else 0))
        self.db.commit()

    # --- watchlist ---------------------------------------------------------------
    def watch(self, launch, hours, now=None):
        now = now or time.time()
        self.db.execute("INSERT OR IGNORE INTO watchlist (key, chain, token, symbol, creator, source, "
                        "added_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
                        (launch.key, launch.chain, launch.mint, launch.symbol, launch.creator,
                         launch.source, now, now + hours * 3600))
        self.db.commit()

    def watched(self, now=None):
        now = now or time.time()
        self.db.execute("DELETE FROM watchlist WHERE expires_at < ?", (now,))
        self.db.commit()
        return self.db.execute("SELECT * FROM watchlist ORDER BY added_at DESC").fetchall()

    def is_watched(self, key, now=None):
        return self.db.execute("SELECT 1 FROM watchlist WHERE key=? AND expires_at>=?",
                               (key, now or time.time())).fetchone() is not None

    def rejected_recently(self, chain, mint, hours=6.0):
        return self.db.execute("SELECT 1 FROM decisions WHERE chain=? AND mint=? AND action='REJECT' "
                               "AND ts>=?", (chain, mint, time.time() - hours * 3600)).fetchone() is not None

    # --- social links (copycat detection) ------------------------------------------
    def record_links(self, links, key):
        for link in links:
            self.db.execute("INSERT OR IGNORE INTO links VALUES (?,?,?)", (link, key, time.time()))
        self.db.commit()

    def link_reuse(self, links, key):
        """{link: number of OTHER tokens that used it}, only for links seen elsewhere."""
        out = {}
        for link in links:
            n = self.db.execute("SELECT COUNT(*) FROM links WHERE link=? AND key!=?", (link, key)).fetchone()[0]
            if n:
                out[link] = n
        return out

    def mark_signal(self, key, now=None):
        self.db.execute("UPDATE watchlist SET last_signal_at=? WHERE key=?", (now or time.time(), key))
        self.db.commit()

    def decision_stats(self, since):
        rows = self.db.execute("SELECT action, COUNT(*) n, SUM(cost_usd) c FROM decisions "
                               "WHERE ts>=? GROUP BY action", (since,)).fetchall()
        return {r["action"]: {"count": r["n"], "claude_usd": round(r["c"] or 0, 4)} for r in rows}
