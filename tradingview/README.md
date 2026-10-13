# ICT OTE Toolkit (TradingView)

A Pine Script v6 indicator built from scratch around ICT's **Optimal Trade Entry**, plus a matching strategy for TradingView's backtester. It's designed for intraday stocks and ETFs (1m–1h).

| File | What it is |
|---|---|
| `ICT_OTE_Indicator.pine` | Signals, drawings, dashboard and alerts. **Paste this into TradingView.** |
| `ICT_OTE_Strategy.pine` | The same logic with orders, for the Strategy Tester. |
| `src/core.pine` | The shared logic. **Edit this file**, then run `python tradingview/build.py`. |
| `src/indicator.pine`, `src/strategy.pine` | Thin wrappers around the core. |

## Install

1. In TradingView, open **Pine Editor** at the bottom of the chart.
2. Open a new blank script, select all, and paste the contents of `ICT_OTE_Indicator.pine`.
3. Click **Save**, then **Add to chart**.
4. Repeat with `ICT_OTE_Strategy.pine` if you want the backtest. Results appear under **Strategy Tester**.

If the editor shows a compile error, copy the error line and send it over, and we'll fix it.

## How a signal is built

```
swing high/low ──► close beyond it = break (MSS if trend flips, BOS if it continues)
                        │
                        ▼
           impulse leg: 100% = leg start, 0% = leg extreme
                        │
                        ▼
           OTE zone = 62%–79% retracement (70.5% sweet spot, dotted)
                        │  price trades back into the zone
                        ▼
      filters: HTF bias · volume spike in leg · discount/premium
               (optional) prior liquidity sweep · FVG/OB inside OTE
                        │  all pass, no open trade
                        ▼
             BUY / SELL  ─ stop beyond 100% (+ ATR buffer)
                         ─ target −0.27 ext (or 0%, −0.62, R multiple)
```

- **The leg keeps extending.** While it waits for the pullback, the leg grows with each new high (or low for shorts), so the OTE moves with it.
- **Invalidation.** A setup is cancelled (its zone turns grey) when price trades past the 100% point, or when it expires (40 bars by default).
- **Replacement.** A new break replaces a setup that's still pending.
- **One trade at a time.** A trade ends at the stop, at the target, or at 15:55 ET. If the stop and target are both hit on the same bar, it counts as a stop.

## What's on the chart

| Element | Meaning |
|---|---|
| **MSS** (solid line) / **BOS** (dashed) | Structure break. The line runs from the broken swing to the breaking candle. |
| **BSL ✕ / SSL ✕** + dotted line | Liquidity sweep: a wick through a swing high/low that closes back inside. |
| Gold **OTE** box | Live setup zone. Green/red once triggered, grey once invalidated or expired. |
| **FVG ▲/▼** | 3-candle imbalance, at least `Min FVG size × ATR`. |
| **OB ▲/▼** | Last opposite candle at the start of a breaking leg. |
| **IFVG / BRK** (purple) | An FVG or OB that was closed through and flipped direction. |
| Premium / Discount shading | Dealing range from the larger swings (`Dealing range swing length`), split at equilibrium. |
| PDH/PDL, PWH/PWL, Day open, 9:30 open | Key levels. All use completed bars only. |
| White / red / green lines after a signal | Entry, stop and target. A `±xR` label marks the exit. |
| Dashboard | HTF bias, structure, premium/discount %, the pending setup, leg volume, the position, and results. |

## Settings worth tuning

- **Swing length.** 3–5 suits 1–5m charts. Use 5–10 on 15m–1h.
- **OTE after MSS only.** This is the purist ICT model: setups only after a shift, not after continuation breaks.
- **Require liquidity sweep before break.** Requires the full sweep → MSS → OTE sequence. You'll get fewer signals, but they follow the textbook pattern.
- **Require FVG or OB inside the OTE.** Requires confluence between the OTE and a zone.
- **HTF bias.** The default is the 1h chart vs its 50 EMA. You can switch it to `240` or `D` for a slower bias.
- **Target.** Choose the leg extreme, the −0.27 or −0.62 extension, or a fixed R multiple.

## Alerts

Create an alert on the indicator and pick one of these:

- **Any alert() function call**: one alert covering both directions. The message includes entry, stop and target, for example `ICT OTE LONG SPY (5) entry 571.20 stop 570.35 target 573.10`. Use this one for webhooks.
- **A named condition**: `OTE long`, `OTE short`, `Bullish/Bearish MSS`, `Sell-/Buy-side liquidity swept`.

Set the alert's frequency to **Once per bar close**. Signals are confirmed at bar close.

## Notes and limits

- **Repainting.** Nothing repaints once a bar closes. Swings confirm `Swing length` bars late, which is the cost of not repainting. Higher-timeframe values come from the last *closed* higher-timeframe bar.
- **Indicator vs strategy results.** The indicator's results row scores trades from the signal bar's close. The strategy fills at the next bar's open and sizes each trade to risk `Risk per trade %` of equity. Small differences between the two are expected.
- **Volume filter.** On symbols with no volume (some indices), the volume filter is skipped automatically.
- **Display and alerts only.** Nothing here places orders. Wiring it into the Ponytail bot (for example a TradingView webhook → agent) would be a separate step.
