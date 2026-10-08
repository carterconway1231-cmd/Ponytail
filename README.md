# Ponytail

An autonomous options trading agent for Robinhood. A deterministic signal and risk
engine is wrapped around a Claude agent loop (Claude Agent SDK) that trades through
Robinhood's official MCP server.

```
            ┌──────────── one cycle (cron) ────────────┐
 Robinhood  │  Claude (judgment)        Code (rules)   │
 MCP tools ─┼─► reads bars, chains,  ──► compute_signals: SMA/RSI/MACD
            │   quotes, earnings,          vote -> BUY=call / SELL=put / HOLD
            │   ratings; vetoes bad   ──► propose_option_trade: risk checks
            │   setups; picks strike,      against data Robinhood returned
            │   expiry, limit price   ──► PreToolUse hook gates every order
            │                         ──► review_positions: TP / SL / DTE / reversal
            └──────────────────────────────────────────┘
                     closed trade P&L ──► indicator weights adapt
```

## How it decides

| Layer | Owner | What it does |
|---|---|---|
| Setup | code (`factors.py`) | Scores 14 factors on daily and hourly bars, each from -1 to +1 with a one-line reason. |
| Weighting & odds | code (`learner.py`) | Blends factors using weights learned from this account's results, requires confluence, and estimates win probability and expected R. Result: BUY → long call, SELL → long put, HOLD → no trade. |
| Judgment | Claude | Reads the factor reasons as a chart narrative and vetoes setups that don't hang together or have bad context (earnings, fundamentals). Picks the contract and limit, and manages exits. |
| Guardrails | code (`risk.py`, `protect.py` + hooks) | Final say on every order. Claude cannot bypass them. |

### Factors

| Group | Factor | Timeframe | Reads |
|---|---|---|---|
| Classic | `trend` | 1D | Price vs EMA 20/50/200 alignment |
| | `macd` | 1D | Histogram strength, fresh crosses |
| | `rsi` | 1D | RSI14 overbought/oversold (mean reversion) |
| | `adx` | 1D | Trend strength × DI direction; also sets the regime (trend if ADX ≥ 25, else range) |
| | `volume_thrust` | 1D | High relative-volume up or down days |
| Order flow (estimated) | `order_flow` | 1h | Cumulative volume delta estimated from where each bar closes in its range, plus CVD/price divergence |
| | `vwap` | 1h | Distance from 5-session VWAP in ATRs |
| ICT | `structure` | 1h | Break of structure (continuation) vs change of character (shift) |
| | `liquidity_sweep` | 1h | Wick through a prior swing high or low that closes back inside the range |
| | `fvg` | 1h | Unfilled fair value gaps, strongest on a retest |
| | `order_block` | 1h | Last opposite candle before a displacement move, unmitigated, being retested |
| | `ote` | 1h | Price in the 62–79% retracement of the latest impulse leg, with structure |
| | `premium_discount` | 1D | Position in the 20-day dealing range (buy in discount, sell in premium) |

Robinhood provides bars, not the tape, so "order flow" here is an estimate from price and volume, not a footprint chart.

A BUY or SELL requires all of the following:

- |learned-weight score| ≥ `SIGNAL_THRESHOLD`
- at least `MIN_CONFLUENCE` factors agree
- agreeing factors outnumber opposing ones 2:1
- once a conviction level has `MIN_CALIBRATION_TRADES` of history, a learned win rate ≥ `MIN_WIN_PROB` and a positive expected R

### How it learns

No indicator is assumed to work. ICT concepts in particular have little rigorous evidence behind them, so every factor starts at a neutral 50% and earns its weight from results:

- **After every closed trade**, each factor that had an opinion is graded. It's right if it pointed the way that paid. Bigger wins and losses (relative to premium) count more.
- **After every signal, traded or not.** Each day's per-symbol read is saved, and 5 trading days later it's graded on whether the stock moved at least half an ATR in the factor's direction. That gives roughly 5–10× more learning data than trades alone, and the agent learns from vetoed setups too.
- **By regime.** Each factor keeps separate track records for trending and ranging markets (by ADX). An ICT retracement entry may work in ranges and fail in trends, and the weights will reflect that.
- **Bayesian and decaying.** Hit rates are Beta posteriors with a 10-trade prior, so a lucky streak can't swing them. Weight = (hit rate / 50%)², so a 70% factor counts about 2× and a 30% factor about 0.36×. Evidence has a 90-day half-life (`LEARN_HALF_LIFE_DAYS`), so recent months dominate and the model adapts when markets change.
- **Calibrated odds.** Win rate and average win/loss in R are tracked per conviction level. Once there's enough history, the gate blocks setups that historically lose money.

### Warm start

On the first run for each symbol, the agent pre-trains the learner on history: 3 years of daily bars and about 6 months of hourly bars, which is as far back as Robinhood serves hourly data. It's a walk-forward replay. Each past day's factors are computed only from bars up to that day, then graded on the next 5 days, the same way untraded signals are graded live. Replayed days count `WARM_START_WEIGHT` each and decay with the same half-life, so the learner starts from evidence rather than a blank 50%. The replay is incremental, so new symbols are picked up automatically and nothing is double-counted.

Robinhood serves only about 6 months of hourly bars, so the hourly ICT and order-flow factors get far fewer warm-start samples than the daily factors. Treat their early weights as provisional.

You can also run it offline from saved bars (JSON files named `SPY_day.json`, `SPY_hour.json`, …):

```bash
python -m ponytail.warmstart path/to/bars/
```

Example from SPY and QQQ, Oct 2023 – Oct 2026 (1,254 replayed days, 960 graded):

| Factor | Trending markets | Ranging markets | Weight (trend / range) |
|---|---|---|---|
| trend | 53.4% | 60.0% | 1.39 / 1.11 |
| liquidity_sweep | 33.3% (15) | 67.9% (28) | 1.00 / 1.36 |
| fvg | 55.4% | 47.6% | 1.11 / 0.88 |
| order_flow | 51.9% | 51.6% | 0.99 / 1.02 |
| rsi | 46.1% | 42.8% | 0.77 / 0.88 |
| macd | 40.6% | 45.1% | 0.85 / 0.73 |
| vwap | 50.8% | 37.9% | 0.78 / 0.53 |

Counts in parentheses are graded samples. Weights reflect the last few months more than the raw 3-year hit rates.

Read results like these with care. The market rose through most of this window, which flatters bullish factors, and the ICT samples are small. That's exactly why the weights keep adapting after the warm start.

`learning_report` (an agent tool, also stored in `agent_state.json`) shows each factor's hit rate and weight by regime, plus the calibration table.

### Guardrails, enforced in the `PreToolUse` hook

- **Allowlist.** Only Robinhood read tools plus `review_option_order`, `place_option_order` and `cancel_option_order` are allowed. Equity and crypto orders, exercise, alerts and watchlist writes are blocked, and so are all file and shell tools.
- **Opens** must be single-leg, buy-to-open (no short premium) limit orders on the configured account. Each must match a plan that `propose_option_trade` approved this run, at that plan's exact quantity and a price no higher than the plan's.
- **Plan approval** uses only data captured from Robinhood responses this run, never numbers the model typed:
  - the signal agrees with the contract type
  - the symbol is in the `SYMBOLS` universe and isn't already held
  - DTE is inside the window and there are no earnings before expiry
  - delta, spread %, open interest and quote freshness are within limits
  - the limit price is between the bid and ask
  - the per-trade premium cap, total premium at risk, max open positions and the daily-loss kill switch all hold
- **Closes** must be sell-to-close of a position the agent holds. Exits are never blocked by portfolio limits.

### Stop losses and loss limits

Every position is protected by a **real stop order resting at Robinhood**, so a losing trade gets cut even when the agent isn't running.

| Protection | How it works |
|---|---|
| **Protective stop** | A sell-to-close stop order at `entry × (1 − STOP_LOSS_PCT)` (35% below entry by default), placed right after the open fills. The hook only accepts stops for the full position, at or above the required level, and below the current bid. |
| **Trailing stop** | Once the mark is up `TRAIL_ACTIVATE_PCT` (30%), the required stop rises to `high-water mark × (1 − TRAIL_PCT)` (25% below the peak). It ratchets up only: the code rejects any stop looser than required, and an active stop can be cancelled only to raise it or to exit. |
| **No naked positions** | While any held position lacks an active stop, every new entry is rejected. |
| **Backup exit rule** | If the bid is already at or below the stop level, `review_positions` orders an immediate limit close. This covers a stop that lapsed or a stop-limit that gapped through. |
| **Daily loss limit** | Realized losses today plus current open losses ≥ `MAX_DAILY_LOSS` → no new entries. |
| **Weekly loss limit** | Realized losses over the trailing 7 days ≥ `MAX_WEEKLY_LOSS` → no new entries. |
| **Losing streak** | `MAX_CONSECUTIVE_LOSSES` losers in a row → no new entries for the rest of the day. |
| **Cooldown** | No re-entry in a symbol for `LOSS_COOLDOWN_DAYS` after a losing exit. |
| **Premium caps** | No single trade can lose more than `MAX_PREMIUM_PER_TRADE`, and the whole book can't lose more than `MAX_TOTAL_PREMIUM`. |

Each run reconciles against Robinhood's order history:

- stops that filled at the broker are booked at their actual fill price, and the weights learn from the loss
- expired or cancelled stops are flagged for re-placement
- live opens are re-priced to their actual fill
- live closes aren't booked until the broker confirms the fill, so a close that doesn't fill can't silently leave you holding an unprotected position

**Choosing the stop type.** Robinhood only accepts `stop_market` as a day order. It guarantees an exit, which matches how you place stops yourself today, but it lapses at the close, so the first run each day re-places it. Schedule a run right after the open (9:31 ET). `stop_limit` can be good-till-cancelled and keeps protecting overnight, but if the price gaps below its limit it may not fill; the backup exit rule then closes the position on the next run.
- **Paper mode** (`LIVE_TRADING=false`, the default). Orders are intercepted before reaching Robinhood and booked as simulated fills at the limit price, so the whole loop, including learning, runs without money.

## Setup

1. **Robinhood account.** Give the agent-enabled ("Agentic") account options approval at Level 2 or higher (Robinhood app → upgrade options), then fund it. Until then, run in paper mode.
2. **Install:** `pip install -r requirements.txt`
3. **Connect Robinhood's MCP server** for the CLI the SDK drives. The server name must be `Robinhood`, because tools are matched as `mcp__Robinhood__*`.
   ```bash
   claude mcp add --scope user --transport http Robinhood https://agent.robinhood.com/mcp/trading
   claude   # then run /mcp and complete the Robinhood OAuth login once
   ```
   If you have a bearer token, you can set `ROBINHOOD_MCP_URL` and `ROBINHOOD_MCP_TOKEN` in `.env` instead.
4. **Claude auth.** Set `ANTHROPIC_API_KEY`, or be logged in with `claude`.
5. **Configure:** `cp .env.example .env`, then set `ROBINHOOD_ACCOUNT_NUMBER` and adjust the limits.

## Run

```bash
python -m pytest -q          # guardrail + signal tests, no network
python -m ponytail --paper   # one paper cycle (forces paper even if LIVE_TRADING=true)
python -m ponytail           # one cycle in the mode set by LIVE_TRADING
```

Schedule it during market hours, with the first run right after the open so day-only stops are re-placed quickly. For example, in cron (times are in the host's timezone, set to ET here):

```
31 9 * * 1-5        cd /path/to/Ponytail && python -m ponytail >> agent.log 2>&1
0 11,13,15 * * 1-5  cd /path/to/Ponytail && python -m ponytail >> agent.log 2>&1
```

More runs mean faster trailing-stop updates and backup exits.

Each run appends a record to `agent_state.json` under `runs`: signals, every guardrail decision, Claude's summary and the run's cost. Positions, the closed-trade log and the learned weights live in the same file.

## Known limitations

- **Gap risk.** Stops limit losses but can't guarantee a price. Options can gap well past a stop on news, and `stop_market` stops only exist from the agent's first run each day.
- **Daily bars only.** Signals are recomputed on daily bars, so running more than once a day mainly helps exits react to intraday price.
- **Legacy bot.** `robinhood_trading_bot.py` is the original equity bot on `robin_stocks`. It's separate from the agent and kept for reference.

Options can lose 100% of the premium paid. You are responsible for every trade the agent places.
