# Strategy: momentum / relative strength

Buy a **leader that separates from its own peers**, after it pulls back to rising short-term
support and holds. Runs every cycle in parallel with mean-reversion, on the full universe.

## Universe

The same 40 large caps as mean-reversion (`UNIVERSE` in `trading/bot.py`), each compared with its
sector ETF and with the other universe names in that sector.

## Shortlist

`$B scan` lists every name up on the day and at least 1.5 points stronger than its sector ETF
(`momentum`, sorted by relative strength). `standout: true` means it is also ≥1 point stronger than
every other universe name in its sector. A whole sector moving together is not a signal: wait for
one name to separate.

## Gate questions (answer each in writing in `state.md`; one FAIL = no trade)

1. **Genuine relative-strength divergence vs the peer complex.** The name must lead its sector ETF
   and its peers (`standout: true`, or a clear multi-day lead in the bars). A uniform sector-wide
   move with no standout is a FAIL: the whole group may still be falling or rising together.
2. **A real catalyst, confirmed by more than the price move.** News, an earnings beat with raised
   guidance, an upgrade, a product or contract announcement: found via search, and dated. "It's
   up a lot today" is not a catalyst. FAIL if earnings are due within the holding horizon.
3. **A held pullback or retest.** Not the first tick down, and never the first tick of a bounce
   off a low. From `$B trend`: `sma10_rising: true` and `support_held: true` (close at or above
   the 10-day average, and recent lows holding above the prior lows) plus `higher_low: true`. One
   green candle is not a base.
4. **Not in the opening range.** No entries in the first 30–60 minutes: never on the 09:47 cycle.
   The earliest momentum entry is the 10:47 cycle.

## Entry, stop, target, horizon

- Entry: market, `dollar_amount` $10 (fractional), regular hours, spread ≤0.10%.
- Stop: just under the held pullback low (the most recent higher low) or 0.5% under the 10-day
  average, whichever is closer, no more than 5% below entry.
- Target: 2R, or the prior swing high if that is nearer but still at least 1.5R.
- Horizon: 5 trading days.

## Position management

- Trim (sell half) into a 2R+ extension, or ahead of a known catalyst. The rest trails: raise the
  stop to breakeven at +1R, then to under each new higher low.
- Exit on invalidation (stop), a thesis change, or **relative-strength loss vs the sector even
  above the stop**: if the sector ETF holds or rises while the name falls back to the pack for two
  cycles running, exit.
- Overnight: momentum entries may be held overnight while the thesis is intact and no catalyst is
  due before the next open. If a cycle ends with the position open, say so in `state.md`.
- PDT: same-day exits count as day trades. Keep `day_trades_last_5_sessions` ≤3.

## Logging

Every shortlisted name is logged as a trade or with `$B skip … --why` naming the gate that failed.
The weekly review compares skipped names' next-5-day returns with traded ones, to check whether
the relative-strength and base filters separate leaders from noise.
