"""Adaptive learner: decides how much each factor counts and how likely a
setup is to win, from this account's own results.

Factor weights (Bayesian, regime-aware)
  Each factor keeps a Beta posterior over "was its call right?", globally and
  separately for trending vs ranging markets. A regime posterior shrinks
  toward the global one until it has its own evidence, and every posterior
  starts at 50% with PRIOR_STRENGTH pseudo-observations, so a few lucky
  trades can't swing it. Weight = (hit_rate / 0.5)^2: a 50% factor counts 1x,
  70% counts ~2x, 30% fades to ~0.36x. Old evidence decays (LEARN_DECAY per
  update), so the model tracks regime change instead of averaging over all
  history.

What it learns from
  * Every closed trade (weight 1.0), with evidence scaled by the size of the
    win or loss relative to the premium risked.
  * Every signal, traded or not ("shadow" labels, weight SHADOW_WEIGHT):
    SHADOW_HORIZON trading days later, did the underlying move at least half
    an ATR in the factor's direction? That gives far more learning samples
    than trades alone, and also learns from vetoed setups.

Win probability and expected value
  Setups are bucketed by conviction (|weighted score|). Each bucket keeps a
  Beta posterior of win rate plus average win/loss returns, so the gate can
  say "setups like this have won 41% of the time with avg R -0.2: skip".
"""
from .factors import ALL_FACTORS

PRIOR_A = PRIOR_B = 5.0           # 50% prior worth 10 observations
PRIOR_STRENGTH = 10.0             # regime posterior's pull toward global
MIN_EVIDENCE_SCORE = 0.1          # factor must have had an opinion to be graded
BUCKETS = [(0.0, 0.40, "low"), (0.40, 0.55, "medium"), (0.55, 1.01, "high")]


def bucket_for(conviction):
    for lo, hi, name in BUCKETS:
        if lo <= conviction < hi:
            return name
    return "high"


def _empty():
    return {"A": 0.0, "B": 0.0}


class Learner:
    def __init__(self, data, cfg):
        self.cfg = cfg
        self.d = data
        self.d.setdefault("version", 2)
        self.d.setdefault("factors", {})
        for f in ALL_FACTORS:
            self.d["factors"].setdefault(f, {"global": _empty(), "trend": _empty(), "range": _empty()})
        self.d.setdefault("calibration", {})
        for _, _, b in BUCKETS:
            self.d["calibration"].setdefault(b, {"A": 0.0, "B": 0.0, "n_trades": 0, "win_r": 0.0, "loss_r": 0.0,
                                                 "wins": 0, "losses": 0})
        self.d.setdefault("snapshots", [])
        self.d.setdefault("trades_learned", 0)
        self.d.setdefault("shadow_learned", 0)

    # ---- weights & scoring ---------------------------------------------------

    def hit_rate(self, factor, regime):
        st = self.d["factors"][factor]
        g = st["global"]
        m_global = (PRIOR_A + g["A"]) / (PRIOR_A + PRIOR_B + g["A"] + g["B"])
        r = st.get(regime) or _empty()
        return (PRIOR_STRENGTH * m_global + r["A"]) / (PRIOR_STRENGTH + r["A"] + r["B"])

    def weight(self, factor, regime):
        return max(0.05, min(3.0, (self.hit_rate(factor, regime) / 0.5) ** 2))

    def score(self, factors, regime):
        """Learned-weight blend of factor scores plus confluence stats."""
        num = den = 0.0
        for name, f in factors.items():
            w = self.weight(name, regime)
            num += w * f["score"]
            den += w
        s = num / den if den else 0.0
        side = 1 if s >= 0 else -1
        agree = sorted(n for n, f in factors.items() if f["score"] * side >= 0.25)
        oppose = sorted(n for n, f in factors.items() if f["score"] * side <= -0.25)
        return s, agree, oppose

    def decide(self, factors, regime):
        cfg = self.cfg
        s, agree, oppose = self.score(factors, regime)
        reasons = []
        if abs(s) < cfg.signal_threshold:
            reasons.append(f"|score| {abs(s):.2f} < threshold {cfg.signal_threshold}")
        if len(agree) < cfg.min_confluence:
            reasons.append(f"only {len(agree)} factors agree (need {cfg.min_confluence})")
        if len(agree) < 2 * len(oppose):
            reasons.append(f"{len(oppose)} factors oppose vs {len(agree)} agreeing")
        decision = "HOLD" if reasons else ("BUY" if s > 0 else "SELL")
        conviction = abs(s)
        est = self.estimate(conviction)
        return {"decision": decision, "score": round(s, 3), "conviction": round(conviction, 3),
                "bucket": bucket_for(conviction), "agree": agree, "oppose": oppose,
                "hold_reasons": reasons, **est}

    def estimate(self, conviction):
        c = self.d["calibration"][bucket_for(conviction)]
        p = (PRIOR_A + c["A"]) / (PRIOR_A + PRIOR_B + c["A"] + c["B"])
        avg_win = c["win_r"] / c["wins"] if c["wins"] else None
        avg_loss = c["loss_r"] / c["losses"] if c["losses"] else None
        ev = p * avg_win + (1 - p) * avg_loss if avg_win is not None and avg_loss is not None else None
        return {"p_win": round(p, 3), "expected_r": None if ev is None else round(ev, 3),
                "bucket_trades": c["n_trades"]}

    # ---- learning ------------------------------------------------------------

    def _grade(self, factors, regime, outcome_sign, evidence_scale):
        """Update every factor that had an opinion. outcome_sign: +1 if the
        underlying/bet went up-direction-right, i.e. compare sign(score)."""
        gamma = self.cfg.learn_decay
        for name, f in factors.items():
            s = f["score"]
            if abs(s) < MIN_EVIDENCE_SCORE or name not in self.d["factors"]:
                continue
            correct = (s > 0) == (outcome_sign > 0)
            e = abs(s) * evidence_scale
            for key in ("global", regime):
                st = self.d["factors"][name].setdefault(key, _empty())
                st["A"] = st["A"] * gamma + (e if correct else 0.0)
                st["B"] = st["B"] * gamma + (0.0 if correct else e)

    def _calibrate(self, conviction, won, weight, r=None):
        c = self.d["calibration"][bucket_for(conviction)]
        gamma = self.cfg.learn_decay
        c["A"] = c["A"] * gamma + (weight if won else 0.0)
        c["B"] = c["B"] * gamma + (0.0 if won else weight)
        if r is not None:
            c["n_trades"] += 1
            if won:
                c["wins"] += 1
                c["win_r"] += r
            else:
                c["losses"] += 1
                c["loss_r"] += r

    def learn_trade(self, entry, direction, pnl, premium):
        """entry: the signal snapshot stored on the position at open."""
        if not entry or "factors" not in entry:
            return
        r = pnl / premium if premium else 0.0
        won = pnl > 0
        # The factor was "right" if it pointed the way that would have paid:
        # the bet's direction on a win, the opposite on a loss.
        outcome = direction if won else -direction
        scale = max(0.5, min(2.0, abs(r) / 0.3))
        self._grade(entry["factors"], entry.get("regime", "range"), outcome, scale)
        self._calibrate(entry.get("conviction", 0.0), won, 1.0, r)
        self.d["trades_learned"] += 1

    def record_snapshot(self, symbol, day, analysis, decision):
        snaps = [s for s in self.d["snapshots"] if not (s["symbol"] == symbol and s["date"] == day)]
        snaps.append({"symbol": symbol, "date": day, "close": analysis["close"], "atr": analysis["atr"],
                      "regime": analysis["regime"], "conviction": decision["conviction"],
                      "decision": decision["decision"], "score": decision["score"],
                      "factors": {k: {"score": v["score"]} for k, v in analysis["factors"].items()}})
        self.d["snapshots"] = snaps[-500:]

    def label_snapshots(self, symbol, daily_bars):
        """Grade snapshots for `symbol` whose horizon has passed. Returns count."""
        dated = [(b["begins_at"][:10], float(b["close_price"])) for b in daily_bars if b.get("begins_at")]
        keep, labeled = [], 0
        for s in self.d["snapshots"]:
            if s["symbol"] != symbol:
                keep.append(s)
                continue
            later = [c for d_, c in dated if d_ > s["date"]]
            if len(later) < self.cfg.shadow_horizon:
                keep.append(s)  # horizon not reached yet
                continue
            move = (later[self.cfg.shadow_horizon - 1] - s["close"]) / s["atr"] if s["atr"] else 0
            if abs(move) >= 0.5:
                self._grade(s["factors"], s["regime"], 1 if move > 0 else -1, self.cfg.shadow_weight)
                if s["decision"] in ("BUY", "SELL"):
                    won = (move > 0) == (s["decision"] == "BUY")
                    self._calibrate(s["conviction"], won, self.cfg.shadow_weight)
                self.d["shadow_learned"] += 1
                labeled += 1
        self.d["snapshots"] = keep
        return labeled

    # ---- reporting -------------------------------------------------------------

    def report(self):
        rows = []
        for name in ALL_FACTORS:
            st = self.d["factors"][name]
            n = st["global"]["A"] + st["global"]["B"]
            rows.append({"factor": name, "evidence": round(n, 1),
                         "hit_rate_trend": round(self.hit_rate(name, "trend"), 3),
                         "hit_rate_range": round(self.hit_rate(name, "range"), 3),
                         "weight_trend": round(self.weight(name, "trend"), 2),
                         "weight_range": round(self.weight(name, "range"), 2)})
        rows.sort(key=lambda r: -max(r["weight_trend"], r["weight_range"]))
        calib = {b: {**self.estimate((lo + hi) / 2 if hi < 1 else 0.7), "range": f"{lo:.2f}-{min(hi, 1):.2f}"}
                 for lo, hi, b in BUCKETS}
        return {"trades_learned": self.d["trades_learned"], "signals_learned": self.d["shadow_learned"],
                "pending_signal_labels": len(self.d["snapshots"]), "factors": rows, "calibration": calib}
