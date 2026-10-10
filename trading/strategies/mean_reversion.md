# Strategy: mean-reversion

Buy a quality large-cap that is oversold because of **sector, macro or index-flow weakness**, not
because of its own bad news, and sell the snap-back. Runs every cycle in parallel with momentum,
on the full universe.

## Universe

The 40 names in `UNIVERSE` (`trading/bot.py`): US large caps (market cap ≫ $50B), average
volume in the millions of shares, tight spreads, across 11 sectors plus semis. Each name is mapped
to its sector ETF for the "is it external?" check. No small caps, no names under $5, no ETFs as
trades (ETFs are the regime read).

## Shortlist

`$B scan` lists every name down ≥2.0% on the day (`mean_reversion`), with a `cause_hint`:
- `external (sector/index weak)`: the sector ETF or SPY is down ≥0.75% and the stock is not more
  than 2.5 points worse than its sector. This is the starting point for a real look.
- `likely company-specific`: the stock is ≥2.5 points worse than its own sector. Default FAIL
  unless the news check shows otherwise.
- `unclear`: the sector isn't weak. Needs the news check.

## Tiers (by depth of drop and time of day, ET)

| Tier | When | Drop (day change) | Extra requirement |
|---|---|---|---|
| **clean** | 10:30–12:30 | ≤ −3.0% | sector or SPY down ≥1%; price ≥5% below its 20-day high; all four gates pass cleanly |
| **looser** | 12:30–14:45 | ≤ −2.0% | all four gates pass; marginal passes allowed on gate 3 only |
| **any-reasonable** | 14:45–15:50 | ≤ −2.0% | only if no trade qualified in either earlier tier today; all four gates still pass |

The 09:47 cycle never enters mean-reversion (the opening drop is not yet a settled price).

## Gate questions (answer each in writing in `state.md`; one FAIL = no trade)

1. **What caused the drop?** It must be external: sector rotation, macro data, rates, index flow.
   A company-specific cause (earnings miss, guidance cut, downgrade, legal or regulatory issue,
   executive departure, offering) is an automatic FAIL. Read the tape first (scan output), then
   search news for the name and its sector to confirm.
2. **What's the most recent fundamental datapoint?** The latest earnings report or guidance must be
   neutral or better. A miss or guidance cut is an automatic FAIL regardless of how oversold the
   technicals look. Also FAIL if earnings are due within the holding horizon.
3. **Is there a genuine upside anchor?** One of: consensus analyst target well above price
   (`get_equity_analyst_ratings`), an intact structural growth driver, or a support level the stock
   has held repeatedly (visible in the daily bars).
4. **Dislocation or real downtrend?** From `$B trend`: `downtrend: true` (lower highs and lower
   lows on each of the last 3 weekly comparisons) is a FAIL regardless of how far the stock has
   fallen. A dislocation looks like a stock above or near its 50-day average that fell sharply with
   its sector.

## Entry, stop, target, horizon

- Entry: market, `dollar_amount` $10 (fractional), regular hours, only if the quoted spread is ≤0.10%.
- Stop: below the day's low by 0.3% or 1.5× the day's remaining range, whichever is tighter, but
  no more than 4% below entry. Record the exact price.
- Target: the prior close (a full gap fill) or the 10-day average, whichever is nearer, giving at
  least 1.5R. If the nearest sensible target is under 1.5R, skip.
- Horizon: 3 trading days.

## Position management

- Trim (sell half) into a 2R+ extension, or ahead of a known catalyst (earnings, an index event,
  a scheduled macro print) rather than holding the full move unconditionally.
- Exit on invalidation (stop), a thesis change (company news appears after all), or **loss of
  relative strength vs the sector even above the stop**: the sector ETF recovers and the stock
  doesn't.
- Overnight: prefer closing a same-day entry before 15:50 if the target is already ≥1R in hand;
  otherwise hold overnight only if the thesis is intact and no catalyst is due before the next
  open. If a cycle ends with the position still open, say so in `state.md` explicitly.
- PDT: a same-day exit is a day trade. If `day_trades_last_5_sessions` is 3, hold to the next
  session unless the stop forces the exit.

## Logging

Every shortlisted name is logged either as a trade or with `$B skip … --why` naming the gate
that failed and its answer. The weekly review in `state.md` looks back at skipped names'
next-3-day returns, so the gate questions are checked against what actually happened (a gate
that keeps rejecting winners is too strict; one that lets losers through is too loose).
