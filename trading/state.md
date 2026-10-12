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

## 2026-10-12 09:47 ET
- Cycle interrupted by a session worker restart before any action; no orders, no data recorded.

## 2026-10-12 10:47 ET
- Stop check: none open.
- Reconciliation: equity $29.42 (peak $29.42, drawdown 0%), $30 deposit still pending · broker vs ledger: match (no positions, no orders today).
- Market read (tape first): semis −2.0% and tech −1.3% lead lower while comm +1.0%, energy +1.0%, financials +0.5% and staples/utilities/REITs +0.5% lead; SPY flat, QQQ −0.6%: rotation, not broad risk-off. News: China's CXMT announced DRAM cell-design progress (memory names hit hardest), plus reports of AI compute being resold weighing on chips.
- Mean-reversion scan: 3 shortlisted.
  - AMD −3.2% (clean tier, 10:48): 1 PASS, drop is semis-sector flow, no AMD-specific news (−1.2 pts vs SMH); 2 PASS, Aug 4 beat ($1.66 vs $1.55), next earnings Nov 3 beyond horizon; 3 PASS, mean analyst target $654 (47 buy / 10 hold); 4 PASS, no weekly downtrend (0/3), above 50-day ($529), 10.6% off 20-day high. Spread 0.04%.
  - MU −4.3%: gate 1 FAIL, memory-specific competitive news (CXMT), not flow. Skipped.
  - ORCL −2.3%: below clean-tier depth (−3%); looser tier only after 12:30. Skipped.
- Momentum scan: 4 shortlisted. CRM FAIL gate 3 (3/3 weekly downtrend). META FAIL gate 3 (gap day, sma10 falling, no held base). AAPL and MSFT FAIL gate 1 (neither stands out from the other in XLK).
- Decisions: BUY AMD $10 market, filled 0.017029 sh @ $587.2245 (order 6accf3af). Thesis: sector-flow dislocation in a non-memory chip name. Stop $585.24 (day low $587.00 −0.3%); target $608.10 (prior close, 9.4R); horizon 3 sessions. Caveat logged: entry is at the session low with a stop 0.34% away, so noise can stop it out; a stop exit is expected to cost about $0.03.
- Day: P&L $0.00 of −$5.00 limit · entries 1/3 · open 1/2 · day trades (5 sessions) 0
