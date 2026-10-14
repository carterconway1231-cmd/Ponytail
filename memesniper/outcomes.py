"""Outcome tracker: what happened to every token after the bot decided on it.

For each tracked token we keep a reference quote (what max_usd_per_trade bought at decision
time) and re-quote selling those same tokens at 5m, 15m, 1h and 4h. The ratio
value_now / ref_native answers the questions that tuning depends on:
- do the safety checks reject tokens that went on to pump? (filter too strict)
- do Claude's BUYs beat its SKIPs? (is Claude adding value)
- are exits leaving money on the table, or holding losers too long?

Every Claude-reviewed token is tracked. Rejections are sampled (they are the vast majority)
so the bot doesn't hammer Jupiter's rate limit.
"""
import asyncio
import logging
import random
import re
import statistics
import time


log = logging.getLogger("memesniper")

HORIZONS = (("r5m", 300), ("r15m", 900), ("r1h", 3600), ("r4h", 14400))
GRACE_S = 900           # give up on a checkpoint this long after it was due
RUG_RATIO = 0.1         # worth <10% of entry => counted as rugged
WIN_RATIO = 2.0         # worth >=2x at any checkpoint => counted as a winner

FAIL_CATEGORIES = (
    ("mint authority", "mint authority"), ("freeze authority", "freeze authority"),
    ("token-2022", "token-2022 extension"), ("top-10", "holder concentration"),
    ("creator holds", "creator holding"), ("rugcheck", "rugcheck score"),
    ("no buy route", "no buy route"), ("no sell route", "no sell route"),
    ("round trip", "sell tax / depth"), ("unique buyers", "too few buyers"),
    ("already sold", "dev sold"), ("liquidity", "low liquidity"),
    ("blocked word", "blocked name"), ("rugged before", "creator rugged before"),
    ("serial launcher", "serial launcher"), ("wallet", "fresh creator wallet"),
)


def fail_category(reason):
    reason = (reason or "").lower()
    for key, label in FAIL_CATEGORIES:
        if key in reason:
            return label
    return re.sub(r"[\d.%$]+", "", reason)[:40].strip() or "other"


class OutcomeTracker:
    def __init__(self, settings, state, routers, prices, rng=random.random):
        """routers: chain -> Jupiter/KyberRouter; prices: NativePrices."""
        self.s, self.state, self.routers, self.prices, self.rng = settings, state, routers, prices, rng
        self._started = []          # timestamps of newly tracked tokens, for the hourly cap

    def wants(self, verdict):
        if not self.s.outcome_tracking:
            return False
        now = time.time()
        self._started = [t for t in self._started if now - t < 3600]
        if len(self._started) >= self.s.outcome_max_new_per_hour:
            return False
        return verdict != "REJECT" or self.rng() < self.s.outcome_sample_rejects

    async def track(self, launch, report, verdict, strategy="launch"):
        """Start tracking; call after the decision is journaled. Never raises."""
        try:
            if launch.chain not in self.routers or not self.wants(verdict):
                return False
            ref = report.ref_quote
            if ref is None:
                usd = await self.prices.usd(launch.chain)
                if not usd:
                    return False
                size = self.s.max_usd_per_trade / usd
                q = await self.routers[launch.chain].buy_quote(launch.mint, size, self.s.slippage_bps)
                if not q:
                    return False     # untradeable: nothing to measure against
                ref = (size, int(q["outAmount"]))
            reason = fail_category(report.hard_fails[0]) if report.hard_fails else ""
            self._started.append(time.time())
            self.state.start_outcome(launch, verdict, reason, ref[0], ref[1], strategy)
            return True
        except Exception as e:
            log.debug("outcome tracking failed for %s: %s", launch.mint, e)
            return False

    async def check_due(self, now=None, limit=40):
        """Fill every checkpoint that has come due. Returns the number of quotes taken."""
        now = now or time.time()
        quotes = 0
        for row in self.state.pending_outcomes():
            router = self.routers.get(row["chain"])
            if router is None:
                continue
            age = now - row["ts"]
            updates = {}
            for col, secs in HORIZONS:
                if row[col] is not None or age < secs:
                    continue
                if age > secs + GRACE_S:
                    updates[col] = -1.0          # missed (bot was down): marked, not guessed
                    continue
                if quotes >= limit:
                    break
                token = row["token"] or row["mint"]
                q = await router.sell_quote(token, int(row["ref_tokens"]), self.s.slippage_bps)
                quotes += 1
                # No route after we had one usually means the liquidity was pulled.
                back = int(q["outAmount"]) / 10 ** router.spec.native_decimals if q else 0.0
                updates[col] = back / row["ref_sol"]
            done = age > HORIZONS[-1][1] + GRACE_S or (
                all(row[c] is not None or c in updates for c, _ in HORIZONS))
            if updates or done:
                self.state.update_outcome(row["mint"], updates, done)
        return quotes

    async def run(self):
        while True:
            try:
                await self.check_due()
            except Exception:
                log.exception("outcome checker error")
            await asyncio.sleep(self.s.outcome_check_interval_s)


# --- reporting ------------------------------------------------------------------------

def _vals(rows, col):
    return [r[col] for r in rows if r[col] is not None and r[col] >= 0]


def group_stats(rows):
    """Summary of one group of outcome rows."""
    out = {"n": len(rows)}
    for col, _ in HORIZONS:
        v = _vals(rows, col)
        out[col] = {"n": len(v),
                    "median_x": round(statistics.median(v), 3) if v else None,
                    "mean_x": round(statistics.fmean(v), 3) if v else None}
    peaks, lasts = [], []
    for r in rows:
        v = [r[c] for c, _ in HORIZONS if r[c] is not None and r[c] >= 0]
        if v:
            peaks.append(max(v))
            lasts.append(v[-1])
    out["pct_hit_2x"] = round(100 * sum(p >= WIN_RATIO for p in peaks) / len(peaks), 1) if peaks else None
    out["pct_rugged"] = round(100 * sum(x <= RUG_RATIO for x in lasts) / len(lasts), 1) if lasts else None
    return out


def build_report(rows):
    by_verdict, by_reason, by_strategy, by_chain = {}, {}, {}, {}
    for r in rows:
        by_verdict.setdefault(r["verdict"], []).append(r)
        keys = r.keys()
        if r["verdict"] == "BUY":
            by_strategy.setdefault(r["strategy"] if "strategy" in keys else "launch", []).append(r)
            by_chain.setdefault(r["chain"] if "chain" in keys else "solana", []).append(r)
        if r["verdict"] == "REJECT":
            by_reason.setdefault(r["reason"] or "other", []).append(r)
    return {
        "tracked": len(rows),
        "by_verdict": {k: group_stats(v) for k, v in sorted(by_verdict.items())},
        "rejects_by_reason": {k: group_stats(v) for k, v in
                              sorted(by_reason.items(), key=lambda kv: -len(kv[1]))},
        "buys_by_strategy": {k: group_stats(v) for k, v in sorted(by_strategy.items())},
        "buys_by_chain": {k: group_stats(v) for k, v in sorted(by_chain.items())},
    }


def format_report(rep):
    """Human-readable table: one row per group, the 1h column carries most of the signal."""
    def line(name, g):
        def fx(col):
            m = g[col]["median_x"]
            return f"{m:>6.2f}x" if m is not None else "     - "
        hit = f"{g['pct_hit_2x']:>5.1f}%" if g["pct_hit_2x"] is not None else "    - "
        rug = f"{g['pct_rugged']:>5.1f}%" if g["pct_rugged"] is not None else "    - "
        return f"{name:<26}{g['n']:>6}  {fx('r5m')} {fx('r15m')} {fx('r1h')} {fx('r4h')}  {hit}  {rug}"

    head = f"{'group':<26}{'n':>6}  {'5m':>7} {'15m':>7} {'1h':>7} {'4h':>7}  {'>=2x':>6}  {'rug':>6}"
    out = [f"tracked tokens: {rep['tracked']}  (median value vs. entry; >=2x = peak at any checkpoint)",
           "", "BY DECISION", head]
    out += [line(k, g) for k, g in rep["by_verdict"].items()]
    for title, key in (("BUYS BY STRATEGY", "buys_by_strategy"), ("BUYS BY CHAIN", "buys_by_chain")):
        if rep.get(key):
            out += ["", title, head] + [line(k, g) for k, g in rep[key].items()]
    if rep["rejects_by_reason"]:
        out += ["", "REJECTIONS BY FIRST FAILED CHECK (a high >=2x rate means the check rejects winners)",
                head]
        out += [line(k, g) for k, g in rep["rejects_by_reason"].items()]
    return "\n".join(out)
