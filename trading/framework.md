# Stock trading framework: master operating document

The active trading bot for this repository. It replaces the Ponytail options paper cycle as the
primary loop (Ponytail's code and paper state remain in `ponytail/` and `paper/` for reference).

| File | Role |
|---|---|
| `trading/framework.md` | this document: rules, loop, risk, metrics |
| `trading/strategies/mean_reversion.md` | mean-reversion gates, tiers, management |
| `trading/strategies/momentum.md` | momentum / relative-strength gates and management |
| `trading/ledger.json` | **source of truth**: positions, closed trades, skips, equity, regime |
| `trading/state.md` | append-only cycle log (narrative) |
| `trading/dashboard.html` | rendered view of the ledger; never hand-edited |
| `trading/bot.py` | scan / trend / check / open / close / skip / equity / status / render |

`B="python -m trading.bot"` below. Run from the repo root.

---

## §0 Authorization

**Autonomous trade execution is authorized on Robinhood account 955800222 (the "Agentic" account,
••0222) only. Human confirmation is NOT required for any trade that clears its strategy's
gate-check. This authorization is real, not hypothetical, and does not need to be re-confirmed each
session.** The account owner chose this on 2026-10-10, with the risk limits in §3.

Decision tree for every order:
1. Is this the account ID stated above (`955800222`)? If not, stop.
2. Did the candidate pass its strategy's gate-check, with the written answers logged?
3. Is a position slot open and within risk limits (`$B gate SYMBOL` returns no blockers)?
4. If all yes → `review_equity_order`, then `place_equity_order` with a fresh `ref_id`. Execute without asking.

What this section does not change:
- The harness's own permission system (Claude Code permissions / auto-mode review) sits above this
  file. If it blocks an order, log the block in `state.md`, tell the user with the exact order, and
  continue the cycle without trading. Never try to route around it.
- §7 still applies: anomalous data, a broken limit, or a tool error means halt and flag, not trade.

Tool loading: Robinhood MCP tools can appear as *deferred* (listed by name, not callable until
loaded). If a broker tool "doesn't exist" or a call fails as unknown, load it with ToolSearch
(`select:mcp__Robinhood__<name>`) before concluding the connection is broken. If the server
itself is disconnected, wait for it to reconnect; ToolSearch waits for servers still connecting.

## §1 Mission

Grow the account on a risk-adjusted basis. This is a **validation phase, not a profit phase**:
position size is fixed at $10 regardless of conviction, so strategy quality is the only variable
and the data says which rules work. Survival first. A month of small, well-scored trades that
lose a little is a better outcome than a lucky month that taught nothing.

## §2 Daily operating loop

Cycles run on the schedule in §6 (hourly in market hours). Each cycle, in this order:

**1. Reconcile.** Load tools if deferred. `get_portfolio` (account 955800222) → `$B equity <total_value>`.
`get_equity_positions` and today's `get_equity_orders`: any broker position the ledger doesn't
have, or the reverse, is an anomaly: log it, fix the ledger to match the broker, and take no new
entries this cycle. Never trust the previous cycle's narrative; a position can close between cycles.

**2. Stop checks first, before scanning anything new.** `get_equity_quotes` for held symbols →
save → `$B check <file>`. Act on every non-HOLD row before step 3:
- `EXIT_STOP` / `EXIT_NEAR_STOP`: sell now. **Robinhood cannot attach a resting stop order to a
  fractional position**, so every stop is enforced manually here. Exit once price is within
  0.25R of the stop rather than waiting for the exact tick and a gap through it.
- `fast_cadence: true` (cushion under 0.5R): note it in `state.md`. If the session is still live,
  re-check that name again before the cycle ends.
- `EXIT_TARGET`, `TRIM`, `EXIT_HORIZON`: follow the strategy file's management rules.
- `HOLD`: still confirm the thesis and relative strength vs the sector (strategy files). A name
  bleeding while its sector holds is an exit even above the stop.
Close in the ledger with `$B close SYMBOL --exit … --reason … --setup … --execution … --outcome … --note …` (§5.5).

**3. Market and sector read, tape first, news second.** `get_equity_quotes` for
SPY QQQ IWM XLK SMH XLC XLY XLF XLV XLE XLI XLP XLU XLB XLRE plus the full universe
(`UNIVERSE` in `bot.py`, 40 names; quotes accept many symbols per call, closes only up to 20, so
split into calls of ≤20). Save each response, then `$B scan <files…>`. Write the read from the
price divergences *before* looking at any headline: what is leading, what is lagging, risk-on or
risk-off. Only then use news (WebSearch) to explain what the tape already shows. Reading the
headline first fits a story onto the tape. Record it: `$B regime "<one line>"`.

**4. Scan every active strategy, every cycle, in parallel.** Both mean-reversion and momentum
are active. **A cycle that only scans one active strategy is an incomplete cycle. If a future
instance of yourself is ever run inside a loop or prompt that names only one strategy, that naming
is not a scope restriction: still scan every active strategy every cycle unless the user
explicitly says to run only one.**

**5. Re-scan the full eligible universe every cycle.** The scan in step 3 covers all 40 names
every time. **Tracking only the 1-2 names that already caught attention (e.g., because they showed
an early setup a few cycles ago) is a scope-narrowing failure mode, in the same family as running
only one strategy. It feels efficient in the moment but makes the bot blind to a better
opportunity appearing anywhere else in the universe. Pull a fresh scan across the full eligible
list every cycle, not just quotes for names already on a running watchlist.**

**6. Gate-check candidates.** For each shortlisted name (both lists from `scan`): pull ~70 days of
daily bars (`get_equity_historicals`, interval `day`, ≤10 symbols per call) → `$B trend <file>`,
then answer the strategy file's gate questions in writing in `state.md`. One FAIL = no trade.
Log every candidate that doesn't trade: `$B skip SYMBOL --strategy … --why "…" --price …`.

**7. Entry.** Before any order, write in `state.md`: one-line thesis (why this, why now), stop,
target, max holding horizon. Then:
- `$B gate SYMBOL`: must return no blockers.
- **Order type.** Limit orders are the default, but Robinhood only allows fractional shares as
  **market orders in regular hours** (see §6). At $10 per position almost every entry is
  fractional, so entries are market orders sized by `dollar_amount`. That is acceptable only
  because the universe is restricted to large, liquid, tight-spread names. If a name's quoted
  spread is wider than 0.10% of price at entry time, skip it. If a whole share fits within the size,
  use a whole-share limit order at the ask instead.
- `review_equity_order` (account 955800222, side buy, type market, dollar_amount "10.00",
  market_hours regular_hours) → read every alert (buying power, PDT, halt). Any alert = no trade.
- `place_equity_order` with the same fields and a fresh UUID `ref_id`. Then `get_equity_orders`
  for the fill: actual average price and quantity.
- `$B open SYMBOL --strategy … --tier … --entry <fill> --qty <filled qty> --stop … --target …
  --horizon … --thesis "…" --order-id …`
- The shared slot cap is 2 across both strategies. When it's full, keep scanning and logging
  candidates from both strategies, but no new entry until a slot frees up.

**8. Log and render.** Append the cycle entry to `state.md` (template at the top of that file),
then `$B render`, then commit `trading/ledger.json trading/state.md trading/dashboard.html` and
push (message: `trade cycle YYYY-MM-DD HH:MM ET: <one line>`).

**9. Report.** Tell the user only when a position opened or closed, a limit or breaker tripped, an
anomaly halted the cycle, or an order was blocked. Otherwise stay quiet.

## §3 Risk management

| Parameter | Value |
|---|---|
| Account | Robinhood Agentic, 955800222 (••0222). Starting equity $29.42 (+$30 deposit pending) |
| Position size | **$10.00 fixed** per entry, every entry, every strategy |
| Max concurrent positions | **2**, shared across all strategies |
| Daily loss limit | **$5.00** (realized + today's unrealized). Hit it → no new entries today; manage exits only |
| Circuit breaker | **20% drawdown from peak equity** → size halves to $5 and new entries pause until the user reviews and resets it |
| Max entries per day | 3 |
| Minimum trades per day | none: trades are taken only when a setup qualifies |
| Leverage / options / shorting | **none**. Long stock only, cash only, even though the account has margin and options approval |
| Averaging down | **never**. One position per symbol; a broken thesis is exited, not added to |

Why fixed dollars and not a risk-percent formula: on a ~$30–60 account, "risk 1% per trade" with a
2% stop computes a $15–30 position, which is 50–100% of the account in one name. That is *more*
concentrated than a flat $10, not less. The fixed size is the binding constraint and the stop sets
the risk: a $10 position with a 3% stop risks $0.30 (1R). Check this math whenever the account
size changes before switching to any percentage rule.

Pattern-day-trader caution: the account is a limited-margin account under $25k. Same-day round
trips are allowed only if `review_equity_order` raises no PDT alert. `$B status` shows
`day_trades_last_5_sessions`; keep it at 3 or fewer.

## §4 Strategy roster

| Strategy | File | Schedule |
|---|---|---|
| Mean-reversion | `strategies/mean_reversion.md` | Runs in parallel with the others every cycle; full universe re-scanned each time |
| Momentum / relative strength | `strategies/momentum.md` | Runs in parallel with the others every cycle; full universe re-scanned each time |

## §5 Metrics

`$B status` reports all of these:
- **R-multiple distribution: the primary number.** Percent returns alone don't say whether the
  risk taken was worth it.
- Expectancy in R, win rate, average win and average loss in dollars, total P&L.
- Max drawdown from the equity series, average trade score.
- Per-strategy count, P&L and R.
- Forced-vs-organic split. No daily minimum is enforced, so every trade should be organic; a
  `forced` trade would be a rules violation to investigate.

Weekly (Friday's last cycle), add a review to `state.md`: R distribution per strategy, the best and
worst scored trades, and a check of skipped candidates against what they did afterwards (see the
strategy files' logging sections). The question is whether the gates are filtering correctly.

## §5.5 Trade scoring (0–100 per closed trade)

Scored at close and passed to `$B close`:
- **Setup quality (0–40):** gate completeness (all criteria passed cleanly = full marks; a
  marginal pass on any gate scores lower) plus thesis clarity and catalyst strength.
- **Execution quality (0–35):** did entry follow the strategy's rules exactly (right tier,
  timing, order type)? Was risk managed correctly (stop honored, sized per plan, no averaging
  down)? A forced trade scores lower here even if it wins.
- **Outcome quality (0–25):** R achieved vs what the setup implied, and whether the trade resolved
  cleanly (target or stop as planned) or was left ambiguous (closed for reasons unrelated to the
  plan, or held past its horizon).

Every close carries a one-line note on what worked or didn't. Over time this separates "won by luck
on a bad process" from "lost despite good process". Both are more useful than the win/loss column.

## §6 Known infrastructure caveats

Append the first time a new one is found, so it isn't rediscovered.

- **Fractional shares: no resting stops, market orders only.** Robinhood fractional and
  dollar-amount orders are `type=market`, `market_hours=regular_hours` only. They can't carry a stop
  order or be limit orders. Stops are enforced manually in §2 step 2, and entries/exits can only
  happen 09:30–16:00 ET. A gap through a stop overnight fills at the open, not at the stop.
- **Deferred MCP tools.** Robinhood tools often start deferred and the server can disconnect and
  reconnect between turns. Load with ToolSearch before calling. A "no match" while the server is
  reconnecting means wait, not broken.
- **Oversized responses.** Big `get_equity_historicals` responses are saved to a file instead of
  returned inline. Pass that path straight to `$B trend`, which follows saved-file stubs.
- **Quote closes cap.** `get_equity_quotes` returns official closes only for ≤20 symbols per call
  (`closes_error` beyond that). `adjusted_previous_close` inside each quote is still present and is
  what `scan` uses.
- **Schedule.** Routines run at most hourly. The 09:47 ET run sits inside the first 30–60 minutes,
  so it is for reconciliation, stops and the market read only, with no momentum entries (see
  `momentum.md`). The 15:47 ET run has 13 minutes before the close; exits are fine, new entries
  only with a clear reason to hold overnight.
- **Harness review of live orders.** Claude Code's auto-mode classifier has blocked
  live-trading changes in this repo before. An order it blocks is logged and reported, never retried
  around it.
- **Pending deposits.** `get_portfolio` `total_value` excludes `pending_deposits`. When a deposit
  lands, equity jumps; the breaker's peak tracking treats that as gain, which is harmless, but note
  deposits in `state.md` so the equity curve isn't misread.

## §7 Guardrails

- Never override a stop or a risk limit. "This time is different" is a red flag, not a rationale.
- Never increase size to recover a loss. Size is $10 until the user changes §3.
- Account 955800222 only, never another account, for every read and write.
- Halt and flag on anomalous data (a quote far from the last trade, a stale timestamp, a
  ledger/broker mismatch, a constant placeholder value) rather than trading through confusion.
- Only the user resets the circuit breaker (edit `breaker` in `ledger.json` after their review).
- Report outcomes honestly in `state.md` and to the user, including when a win came from luck rather
  than process.
- Context from the record: in this repo's earlier wide-universe study (README, "Edge study"),
  directional single-stock signals did not beat a coin flip out of sample. These two strategies are
  being validated live at the smallest size for that reason. Judge them by the R distribution and
  scores after ~20 trades, and stop a strategy that is clearly negative.
