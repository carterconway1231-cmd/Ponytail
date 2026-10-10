# Cycle log (append-only)

Every cycle appends one entry at the bottom. Never edit or delete earlier entries. Pull live data
every cycle; never carry a position or price forward from an earlier entry. `trading/ledger.json`
is the source of truth for numbers, and this file holds the reasoning.

Entry template:

```
## YYYY-MM-DD HH:MM ET
- Stop check: <each open position: price, cushion in R, action> | none open
- Reconciliation: equity $X (peak $Y, drawdown Z%) · broker vs ledger: match | <mismatch and fix>
- Market read (tape first): <leaders/laggards, risk-on/off>; news: <what explains it>
- Mean-reversion scan: <n shortlisted> → <SYM: gate 1..4 PASS/FAIL with the answer> …
- Momentum scan: <n shortlisted> → <SYM: gate 1..4 PASS/FAIL with the answer> …
- Decisions: <entries with thesis/stop/target/horizon, exits with score and note> | no trade (<why>)
- Day: P&L $X of −$5.00 limit · entries N/3 · open N/2 · day trades (5 sessions) N
```

---

## 2026-10-10 (Saturday) setup
- Framework built from the user's setup answers: live on account 955800222 (••0222), $10 per
  entry, 2 positions max, $5 daily loss limit, 20% drawdown circuit breaker, mean-reversion and
  momentum active, no daily minimum, max 3 entries per day.
- Account: $29.42 cash, $30 deposit pending, no positions.
- Replaces the Ponytail options paper cycle as the primary loop; Ponytail's two paper spreads stay
  recorded in `paper/agent_state.json` but are no longer managed.
