# Strategy: options (defined risk, max loss 50% of account value, capped at $100)

Added 2026-10-12 at the user's request: debit spreads, single calls and SPY/QQQ put credit
spreads. Max loss per trade is **50% of account value, capped at $100** (`$B gate` → `option_max_loss`),
updated from the fixed $100 on 2026-10-12. Every structure is long-biased and defined-risk.
Options share the 2-position cap, the daily loss limit, the entry count and the circuit breaker
with the stock strategies. One position per underlying, stock or option, never both.

All option work is checked against buying power first. A trade whose max loss exceeds the
account's buying power is not taken, whatever the cap says. `$B open-option … --buying-power BP`
refuses it.

## 1. Expressing a stock signal with options

Only after a mean-reversion or momentum candidate has passed **every** gate in its own strategy
file (same tier rules, same written answers). The option is an alternative way to take that trade,
not a new reason to trade.

Choose in this order, taking the first that qualifies:

**a. Call debit spread (preferred)**
- Expiry: the first expiry at least 14 days past the strategy's horizon, within 14–60 DTE.
- Long leg: delta 0.45–0.65. Short leg: the strike at or just above the strategy's target.
- Debit ≤ 50% of the width (pays at least 1:1 if the target is reached).
- Max loss (debit × 100 × contracts) ≤ $100 and ≤ buying power.

**b. Long call** (only when no debit spread qualifies)
- 21–60 DTE, delta 0.45–0.65.
- Implied vol ≤ 1.2× the 20-day realized vol of the underlying (no overpaying for vol).
- Premium × 100 ≤ $100 and ≤ buying power.

**c. Otherwise** take the $10 stock trade as usual.

Underlying stop and target are the stock strategy's stop and target; the horizon is the stock
strategy's horizon.

## 2. SPY/QQQ put credit spreads (premium strategy)

Independent of the stock signals; scanned every cycle from 10:47 ET on. This is Ponytail's premium
strategy, the one that tested profitable in this repo's backtest (README "Premium strategy").
- Underlyings: SPY and QQQ only.
- Expiry 30–60 DTE (closest to 45).
- Short put near 0.20 delta, long put one strike width lower (normally $1 wide).
- Credit ≥ 20% of the width. Max loss ((width − credit) × 100 × contracts) ≤ $100 and ≤ buying power.
- Filter: implied vol at the short strike ≥ 20-day realized vol of the underlying (sell rich
  premium only). Skip entries when SPY is down > 2% on the day or VIX is up > 20%.
- One open credit spread per underlying.

## Liquidity (every leg, every structure)

- Open interest ≥ 100 and bid-ask spread ≤ 10% of mid (or ≤ $0.05 for options under $0.50).
- Limit orders only, at the mid for spreads (one combo order), at or below the ask for single
  calls. Review with `review_option_order`, then `place_option_order` with a fresh `ref_id`.
- If an order hasn't filled by the next cycle, cancel it (`cancel_option_order`) and re-evaluate;
  never chase more than one tick.

## Exits (`$B check` applies these; mark each option position every cycle with `$B optmark`)

| Structure | Stop | Take profit | Time |
|---|---|---|---|
| Call debit spread | underlying stop, or spread value ≤ 50% of debit | spread value ≥ 80% of width, or underlying target | ≤ 7 DTE, or past the stock horizon |
| Long call | underlying stop, or premium ≤ 50% of debit | premium ≥ 2× debit, or underlying target | ≤ 7 DTE, or past the stock horizon |
| Put credit spread | cost to close ≥ 2× credit, or SPY/QQQ at the short strike | cost to close ≤ 50% of credit | ≤ 21 DTE |

Option R = P&L ÷ max loss. Score closed option trades with §5.5 like any other trade.

Marking: each cycle, quote every leg (`get_option_quotes`) and pass the per-share value to
`$B optmark SYMBOL VALUE`: the option's mark for a long call, long-leg mark minus short-leg mark
for a debit spread, short-leg mark minus long-leg mark (the cost to close) for a credit spread.

## Interaction with the account limits (read before trading)

- The daily loss limit is 25% of the day's starting value and the breaker trips at 20% drawdown. An
  option stop at 50% of a max-size debit costs ~25% of the account, enough to trip both at once.
  That is the intended behavior of those limits; it is not a reason to skip them.
- At today's ~$30–60 account, a single $100-risk trade is impossible (buying power) and even a
  $40 trade is most of the account. Expect options to fire rarely until the account grows.
