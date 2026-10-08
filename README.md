# Ponytail

An autonomous options trading agent for Robinhood. A deterministic signal, volatility,
pricing and risk engine is wrapped around a Claude agent loop (Claude Agent SDK) that
trades through Robinhood's official MCP server.

```
            ┌──────────────────── one cycle (cron) ─────────────────────┐
 Robinhood  │  Claude (judgment)            Code (rules & math)         │
 MCP tools ─┼─► fetches bars, VIX,   ──► compute_signals: 16 factors x    │
            │   scanner, chains,         learned weights -> BUY/SELL/HOLD│
            │   quotes, earnings   ──► rank_contracts: vol regime ->     │
            │   reads the story,         long vs debit spread, EV/$,     │
            │   vetoes weak setups,      Kelly-sized quantity            │
            │   picks a candidate  ──► propose_option_trade: every gate  │
            │                      ──► PreToolUse hook: final say on     │
            │                            each order                      │
            │                      ──► review_positions: stops, trailing,│
            │                            TP / time stop / DTE / reversal │
            └────────────────────────────────────────────────────────────┘
          every signal & closed trade ──► factor weights, odds and exits adapt
```

**The honest headline.** On SPY and QQQ from mid-2024 to Oct 2026 the backtest made
**zero trades with default settings**, while SPY returned +42.8%. Every way of loosening
the gates lost money (details under [Backtest](#backtest)). That's the system working:
it only trades when its measured edge pays for the option premium, and on index ETFs
in a strong bull market it never found one. Paper-trade it before anything else.

## How it decides

| Layer | Owner | What it does |
|---|---|---|
| Setup | code (`factors.py`) | Scores 16 factors on daily and hourly bars, each from −1 to +1 with a one-line reason. |
| Weighting & odds | code (`learner.py`) | Blends factors with weights learned from results, requires confluence, and estimates win probability and expected R. |
| Structure | code (`volatility.py`) | Cheap or normal IV → long call/put. Expensive IV → debit vertical spread. |
| Contract & price | code (`selection.py`) | Ranks candidates by expected value per dollar after premium, decay and slippage. |
| Size | code (`sizing.py`) | Quarter-Kelly on the learned edge, capped as a percent of equity. |
| Judgment | Claude | Reads the factor reasons as a chart story, vetoes weak setups or bad context (earnings, fundamentals), and chooses among ranked candidates. |
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
| Market context | `market_trend` | 1D | SPY vs its EMA50 and 20-day return |
| | `vix` | 1D | VIX vs its 20-day average; high-but-falling VIX reads bullish |
| | `relative_strength` | 1D | 20-day return vs SPY |

Robinhood provides bars, not the tape, so "order flow" here is an estimate from price and volume, not a footprint chart. Context bars are trimmed to each symbol's date, so replays never see the future.

A BUY or SELL requires all of the following:

- |learned-weight score| ≥ `SIGNAL_THRESHOLD`
- at least `MIN_CONFLUENCE` factors agree
- agreeing factors outnumber opposing ones 2:1
- once a conviction level has `MIN_CALIBRATION_TRADES` of history, a learned win rate ≥ `MIN_WIN_PROB` and a positive expected R

### Volatility, structure and contract selection

Buying options when implied volatility is rich is the classic way to be right on direction and still lose. Each run, the agent records near-the-money IV per symbol from the quotes it pulls:

- **IV rank** compares today's IV with its own stored history. It needs 20 days of data.
- **IV / realized vol** works from day one. Above `IV_RV_EXPENSIVE` (1.4) is expensive.

When IV is expensive, a long option is rejected. The agent must use a debit vertical: buy one strike and sell a further-out one, same expiration. The short leg sells back some of the rich premium and cuts time decay. Spreads must cost no more than 60% of the strike width, and their max loss is the debit paid.

`rank_contracts` prices every quoted candidate under a two-scenario forecast. Over `EV_HOLD_DAYS`, the stock moves up or down by one realized-vol move, up with the learned win probability. Each leg is priced with Black-Scholes at its own IV, net of expected slippage. The result is expected value per dollar, breakeven, theta per day, value if right and wrong, and the Kelly-sized maximum quantity. `propose_option_trade` rejects anything below `MIN_CONTRACT_EV`. With no learned edge and IV above realized vol, long premium has negative EV, which is exactly what this gate is for.

### Position sizing

The risk per trade is a fraction of account equity:

- **Before calibration:** a flat `BASE_RISK_PCT` (3%) until a conviction level has `MIN_CALIBRATION_TRADES` of history.
- **After calibration:** quarter Kelly, `0.25 × (p − (1 − p) / b)`, using the learned win rate and win/loss ratio, capped at `MAX_RISK_PCT` (6%).
- **No edge:** if Kelly is zero or negative, the size is zero contracts.

Risk per contract is what a stop-out realistically costs. For a single option that's premium × stop distance × gap allowance; for a spread it's the full debit. In paper mode, equity is `PAPER_CAPITAL` plus realized P&L, so set it to what you plan to fund.

### How it learns

No indicator is assumed to work. ICT concepts in particular have little rigorous evidence behind them, so every factor starts at a neutral 50% and earns its weight from results:

- **After every closed trade**, each factor that had an opinion is graded. It's right if it pointed the way that paid. Bigger wins and losses (relative to premium) count more.
- **After every signal, traded or not.** Each day's read is graded 5 trading days later. That gives roughly 5–10× more data than trades alone, and the agent learns from vetoed setups too.
- **Drift-adjusted.** Factors are graded on the move beyond the trailing 60-day trend, not raw direction. In a bull market every bullish read "wins" on raw direction; this asks whether a factor added information beyond the trend. Win-rate calibration still uses the raw move, because that's what options pay on.
- **By regime.** Each factor keeps separate track records for trending and ranging markets (by ADX).
- **Bayesian and decaying.** Hit rates are Beta posteriors with a 10-observation prior, so a lucky streak can't swing them. Weight = (hit rate / 50%)². Evidence has a 90-day half-life (`LEARN_HALF_LIFE_DAYS`).
- **Exits too.** Each trade's best and worst point is recorded. The tuner replays closed trades and backtest price paths across take-profit/stop pairs, and adopts a better pair only with `MIN_EXIT_SAMPLES` of evidence and a clear margin. The stop can never exceed 50%.

`learning_report` shows each factor's hit rate and weight by regime, the calibration table and the tuned exits.

### Warm start

On the first run for each symbol, the agent pre-trains the learner with a walk-forward replay: 3 years of daily bars and about 6 months of hourly bars, which is as far back as Robinhood serves hourly data. Each past day's factors use only bars up to that day. The replay is incremental and never double-counts. Hourly ICT and order-flow factors get far fewer samples, so treat their early weights as provisional.

```bash
python -m ponytail.warmstart path/to/bars/     # SPY_day.json, SPY_hour.json, VIX_day.json, ...
```

Example raw hit rates from SPY and QQQ, Oct 2023 – Oct 2026 (graded on raw direction, before drift adjustment):

| Factor | Trending markets | Ranging markets |
|---|---|---|
| trend | 53.4% | 60.0% |
| liquidity_sweep | 33.3% (15) | 67.9% (28) |
| fvg | 55.4% | 47.6% |
| order_flow | 51.9% | 51.6% |
| rsi | 46.1% | 42.8% |
| macd | 40.6% | 45.1% |
| vwap | 50.8% | 37.9% |

Counts in parentheses are graded samples. The market rose through most of this window, which flattered bullish factors. That's why grading is now drift-adjusted.

## Guardrails

These are enforced in the `PreToolUse` hook, using only data captured from Robinhood responses this run, never numbers the model typed.

- **Allowlist.** Only Robinhood read tools plus `review_option_order`, `place_option_order` and `cancel_option_order` are allowed. Equity and crypto orders, exercise, alerts and watchlist writes are blocked, and so are all file and shell tools.
- **Opens.** Buy-to-open long calls/puts, or 2-leg debit verticals, as limit orders on the configured account. Each must match a plan `propose_option_trade` approved this run: same legs, same quantity, price no higher.
- **Plan approval checks:**
  - the signal direction matches the contract type
  - the symbol is in today's universe and isn't already held, and isn't in a loss cooldown
  - DTE is in the window, with no earnings before expiry
  - delta, bid/ask spread, open interest and quote freshness are within limits, for both legs of a spread
  - the vol regime allows the structure, and EV per dollar ≥ `MIN_CONTRACT_EV`
  - the quantity fits the Kelly risk budget, and the premium caps hold
  - no FOMC or CPI release within `EVENT_BLACKOUT_DAYS` (dates in `events.json`)
  - the time is inside `ENTRY_WINDOW` (09:45–15:45 ET, avoiding the widest spreads at the open and close)
  - no circuit breaker has tripped
- **Closes.** Limit closes of positions the agent holds, both legs for spreads. Exits are never blocked by portfolio limits.

### Stop losses and loss limits

| Protection | How it works |
|---|---|
| **Protective stop** | A sell-to-close stop order resting at Robinhood at `entry × (1 − STOP_LOSS_PCT)` on every single option, placed right after the open fills. Spreads are defined-risk, and Robinhood stop orders are single-leg only, so spreads exit by review. |
| **Trailing stop** | Once the mark is up `TRAIL_ACTIVATE_PCT`, the stop rises to `high-water mark × (1 − TRAIL_PCT)`. It ratchets up only. |
| **No naked singles** | While any held single lacks an active stop, every new entry is rejected. |
| **Backup exit rule** | If the bid is already at or below the stop level, review orders an immediate limit close. |
| **Time stop** | Held `TIME_STOP_DAYS` without reaching `TIME_STOP_MIN_GAIN` → close, since theta is eating it. |
| **Daily / weekly loss limits** | Realized + open losses today ≥ `MAX_DAILY_LOSS`, or realized over 7 days ≥ `MAX_WEEKLY_LOSS` → no new entries. |
| **Losing streak** | `MAX_CONSECUTIVE_LOSSES` in a row → no new entries for the day. |
| **Cooldown** | No re-entry in a symbol for `LOSS_COOLDOWN_DAYS` after a loss. |

Each run reconciles against Robinhood's order history:

- broker stop fills, live opens and live closes are booked at their real fill prices
- expired GFD stops are flagged for re-placement
- an unfilled entry can be cancelled and repriced
- a close isn't booked until the broker confirms it

**Choosing the stop type.** Robinhood only accepts `stop_market` as a day order. It guarantees an exit but lapses at the close, so the first run each day re-places it; schedule that run right after the open (9:31 ET). `stop_limit` can be good-till-cancelled and protects overnight, but it may not fill if the price gaps through its limit.

## Universe and context

- **Core symbols** come from `SYMBOLS`.
- **Scanner discovery** (`DISCOVER`): each run, `preview_scan` looks for large caps (market cap over $10B, average volume over 2M) with unusual options activity: relative options volume above 1.5× and more than 20k contracts. The top `MAX_DISCOVERED` names, at $10 or more a share, join that day's universe. `preview_scan` saves nothing to your account.
- **VIX** is fetched through Robinhood's index tools for the `vix` factor and IV context.
- **Macro events.** Implied volatility inflates before FOMC and CPI releases and collapses after. `events.json` holds the dates; FOMC 2027 is tentative. Verify them against federalreserve.gov and bls.gov, and add CPI dates as BLS publishes them. `portfolio_status` warns when no CPI date is listed for the next 45 days.

## Scoreboard and go-live gate

`python -m ponytail --report` (or the `performance_report` tool) shows:

- win rate and expectancy per trade, also net of Claude costs
- profit factor and max drawdown
- return vs SPY buy-and-hold over the same period
- average entry slippage
- breakdowns by structure, vol regime and exit reason

Claude spend is kept per day, so "net of AI costs" stays accurate.

**`LIVE_TRADING=true` only takes effect once paper results pass the checklist:**

- ≥ `MIN_PAPER_TRADES` (30) trades
- ≥ `MIN_PAPER_DAYS` (20) days
- positive expectancy after AI costs
- profit factor ≥ `MIN_PROFIT_FACTOR` (1.2)
- drawdown ≤ `MAX_DRAWDOWN_PCT` (25%)

Otherwise the run falls back to paper and sends an alert. `FORCE_LIVE=true` overrides this, deliberately.

## Costs and alerts

- **Monitor runs** (`--monitor`) only protect and exit held positions. They run on `MONITOR_MODEL` (`claude-haiku-5-5`) at low effort with a $0.30 cap, and entry tools are blocked by the hook. Use them for intraday checks and keep full Opus runs for entries.
- **Compact responses.** Bulky bar, contract and order responses are replaced with short summaries before they reach Claude. The engine keeps the full data.
- **Alerts.** Set `ALERT_WEBHOOK_URL` (a Slack or Discord incoming webhook, or an ntfy.sh topic) for:
  - opens and closes
  - stop-outs
  - circuit breakers
  - failed runs
  - a blocked go-live

## Backtest

```bash
python -m ponytail.backtest path/to/bars/ [--ablate] [--save-exits] [--capital 3000]
```

This is a walk-forward replay with a fresh learner trained only on what was knowable each day. The first 60 days only train. After that, each symbol-day runs the live pipeline: factors, decision, vol regime, candidates (ATM/OTM longs and $1/$2/0.5%/1%/2%-wide debit spreads) priced with Black-Scholes, then the EV gate, premium cap, Kelly sizing and daily-close exits.

- **Funnel.** The output shows where would-be trades dropped out (HOLD, negative EV, risk budget, and so on).
- **`--ablate`** reruns without each factor to measure its contribution.
- **`--save-exits`** feeds simulated trade paths to the exit tuner.

**Assumptions:**

- IV is proxied by VIX scaled by relative realized vol and held constant per trade, so there's no IV crush.
- Fills cost 1.5% of the option price per side beyond mid.
- Exits happen at daily closes.

Results on SPY and QQQ (trading Jun 2024 – Oct 2026, $3,000 capital; SPY buy-and-hold +42.8%):

| Setting | Trades | Win rate | P&L | Profit factor |
|---|---|---|---|---|
| Defaults | 0 | – | $0 | – |
| `MIN_CONFLUENCE=3` | 0 | – | $0 | – |
| `MIN_CONTRACT_EV=-0.10` | 3 | 0% | −$88 | 0.00 |
| confluence 3, threshold 0.2, EV −20% | 11 | 36% | −$142 | 0.30 |
| no EV gate at all | 12 | 33% | −$160 | 0.24 |

With defaults, 88% of symbol-days were HOLD (too little confluence), and the rest failed the EV gate or the risk budget. The gates kept the account out of a strategy that lost money whenever it was allowed to trade. Directional long premium on index ETFs is a hard way to beat simply holding them. Widen the universe (the scanner, single stocks with more dispersion) and backtest again before trusting any setting.

## Setup

1. **Robinhood account.** Give the agent-enabled ("Agentic") account options approval: Level 2 for long calls/puts, Level 3 for spreads. Then fund it. Until then, run in paper mode.
2. **Install:** `pip install -r requirements.txt`
3. **Connect Robinhood's MCP server** for the CLI the SDK drives. The server name must be `Robinhood`, because tools are matched as `mcp__Robinhood__*`.
   ```bash
   claude mcp add --scope user --transport http Robinhood https://agent.robinhood.com/mcp/trading
   claude   # then run /mcp and complete the Robinhood OAuth login once
   ```
   If you have a bearer token, you can set `ROBINHOOD_MCP_URL` and `ROBINHOOD_MCP_TOKEN` in `.env` instead.
4. **Claude auth.** Set `ANTHROPIC_API_KEY`, or be logged in with `claude`.
5. **Configure:** `cp .env.example .env`, then set `ROBINHOOD_ACCOUNT_NUMBER` and `PAPER_CAPITAL`, and review the limits.

## Run

```bash
python -m pytest -q              # tests (offline)
python -m ponytail --paper       # one full paper cycle
python -m ponytail --monitor     # cheap protect/exit-only run
python -m ponytail --report      # scoreboard + go-live checklist, no Claude call
```

An example cron schedule, with times in ET:

```
31 9 * * 1-5         cd /path/to/Ponytail && python -m ponytail >> agent.log 2>&1            # full: re-place day stops, entries
0 11,14 * * 1-5      cd /path/to/Ponytail && python -m ponytail --monitor >> agent.log 2>&1  # cheap: trail stops, exits
30 15 * * 1-5        cd /path/to/Ponytail && python -m ponytail >> agent.log 2>&1            # full: late entries/exits
```

`agent_state.json` holds positions, the closed-trade log, learner state, IV history, tuned exits, per-day AI costs, and the last 50 runs' signals and guardrail decisions.

## Known limitations

- **No proven edge yet.** See [Backtest](#backtest). Paper-trade until the go-live checklist passes.
- **Gap risk.** Stops limit losses but can't guarantee a price, and `stop_market` stops only exist from the first run each day.
- **The EV model is simple.** It uses two scenarios with constant IV, so it ignores skew, vol crush and early exercise.
- **Hourly history is short.** Robinhood serves about 6 months, so the hourly factors have small samples.
- **Legacy bot.** `robinhood_trading_bot.py` is the original equity bot on `robin_stocks`, kept for reference.

Options can lose 100% of the premium paid. You are responsible for every trade the agent places.
