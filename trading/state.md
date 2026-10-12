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

## 2026-10-12 11:47 ET
- Stop check: AMD $591.20 vs entry $587.22, +$0.07, 3.0R cushion; flagged TRIM at 2R. Chose the "tighten" option instead: stop raised from $585.24 to breakeven $587.22. Trimming half of a $10 position would bank ~$0.03 and use a day trade (PDT count), for no risk benefit over a breakeven stop. RS vs SMH: AMD −2.8% vs SMH −1.8%, lag narrowed from the open; thesis intact.
- Reconciliation: equity $29.48 (cash $19.42 + AMD $10.06), $30 deposit pending · broker vs ledger: match (AMD 0.017029 sh).
- Market read (tape first): semis bouncing off lows (SMH −2.0% → −1.8%), tech −1.2%; comm +1.1%, energy +1.3%, financials +0.7% still lead; SPY flat. Same rotation as 10:47.
- Mean-reversion scan: MU −3.05% (gate 1 still FAIL, memory-specific news), AMD held, ORCL −2.4% (not clean-tier depth before 12:30). No new entry.
- Momentum scan: META only standout, gate 3 FAIL (no retest yet); CRM, MSFT, AAPL not standouts. No new entry.
- Decisions: hold AMD with breakeven stop; no new trade.
- Day: P&L +$0.07 of −$5.00 limit · entries 1/3 · open 1/2 · day trades (5 sessions) 0

## 2026-10-12 12:47 ET
- Stop check: AMD $591.83, +$0.08, +2.3R vs initial risk, stop at breakeven $587.22: HOLD. RS vs SMH: −2.7% vs −1.8%, steady.
- Fix: the 11:47 stop move made `check` measure R against the new breakeven stop (showed 1023R). `bot.py` now keeps `initial_stop` and measures R and cushion against it; added a `stop` command that only moves stops up, plus a test. AMD's initial stop set to $585.24.
- Reconciliation: equity $29.50 · broker vs ledger: match.
- Market read: same rotation as the morning (semis/tech down, energy/comm/staples up), SPY flat.
- Mean-reversion scan (looser tier open): ORCL −2.9% FAIL gate 4 (6% under 50-day, lower highs since September); gate 1 unclear, no news found. MU FAIL gate 1. AMD held.
- Momentum scan: META FAIL gate 3 (no retest). NKE standout (+1.2%, +1.7 vs XLY) FAIL gates 2/3 (bounce after the Oct 2 earnings gap-down, 3-month downtrend). CRM, AAPL, MSFT not standouts.
- Decisions: no new trade; hold AMD.
- Day: P&L +$0.08 of −$5.00 limit · entries 1/3 · open 1/2 · day trades (5 sessions) 0
