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
| Direction | code (`signals.py`) | Weighted SMA20/50 crossover + RSI14 + MACD vote. BUY → long call, SELL → long put, HOLD → no trade. |
| Judgment | Claude | Vetoes signals on context (earnings, fundamentals, analyst consensus, nature of the move), picks the expiration/strike, sets the limit, manages exits. |
| Guardrails | code (`risk.py` + hooks) | Final say on every order. Claude cannot bypass them. |
| Learning | code (`state.py`) | When a position closes, indicators that voted with a winning bet gain weight; those that voted with a losing bet lose it. |

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

Schedule it during market hours. For example, in cron (times are in the host's timezone):

```
35 9,12,15 * * 1-5  cd /path/to/Ponytail && python -m ponytail >> agent.log 2>&1
```

Each run appends a record to `agent_state.json` under `runs`: signals, every guardrail decision, Claude's summary and the run's cost. Positions, the closed-trade log and the learned weights live in the same file.

## Known limitations

- **Live fill price.** Live fills are booked at the limit price. If an opening order never fills, the next run sees the contract missing from the broker positions and drops it without learning. The true average fill price isn't reconciled from `get_option_orders` yet.
- **Daily bars only.** Signals are recomputed on daily bars, so running more than once a day mainly helps exits react to intraday price.
- **Legacy bot.** `robinhood_trading_bot.py` is the original equity bot on `robin_stocks`. It's separate from the agent and kept for reference.

Options can lose 100% of the premium paid. You are responsible for every trade the agent places.
