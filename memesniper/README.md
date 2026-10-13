# memesniper

A Solana meme coin sniper where **Claude makes every entry decision**, inside hard
limits that code enforces. It is separate from the Ponytail stock/options bot and
shares no state with it. **Paper trading is the default.**

```
 PumpPortal ws ──┐ new pump.fun tokens + migrations
 DexScreener  ───┤ newly profiled Solana tokens
                 ▼
        dedupe ─► observe 30s of trades (pump.fun) ─► safety screen (code)
                                                        │ hard fail → journal, stop
                                                        ▼
                                       Claude (structured BUY/SKIP, size, exits)
                                                        ▼
                                RiskManager clamps size / stop / hold, checks caps
                                                        ▼
                                Jupiter swap  (paper: simulated fill on real quote)
                                                        ▼
                monitor every 5s: stop · time stop · take-profit (half) · trailing stop
```

## Who decides what

| Stage | Owner | Rule (defaults) |
|---|---|---|
| Observation | code | Watches each new pump.fun token's trades for `OBSERVE_SECONDS` (30): distinct buyers, buy/sell SOL, largest buy, dev selling, market-cap change. |
| Hard screen | code | Mint and freeze authority revoked. No Token-2022 fee/hook/delegate/pause extensions. Top-10 holders ≤ 35% (bonding curve and pool vaults excluded). Creator holds ≤ 10%. RugCheck score ≤ 5000. A Jupiter buy→sell round trip loses ≤ 12% (catches honeypots and sell taxes). ≥ 8 distinct buyers. Dev sold ≤ 50%. DEX pairs need ≥ $5k liquidity. Fails never reach Claude. |
| Entry | **Claude** | One structured-output call per candidate (`claude-opus-5-5`, effort `low`, 25s timeout). Sees token, metadata and socials, safety metrics and warnings, the observation, portfolio state and the last 10 closed trades. Returns BUY/SKIP, confidence, size, take-profit, stop and max hold. Timeouts, errors and refusals mean SKIP. Server-side refusal fallbacks are on (`MEMESNIPER_USE_FALLBACKS`). |
| Risk | code | Confidence ≥ 0.6. Size ≤ 0.05 SOL/trade. ≤ 3 open positions. ≤ 0.15 SOL total exposure. ≤ 0.30 SOL spent per day. Daily loss limit 0.15 SOL (realized loss plus open positions marked at their stops). ≤ 6 entries/hour. One trade per creator per 24h. Wallet keeps 0.05 SOL in reserve. Claude can tighten stops and holds but never loosen them past config. |
| Exits | code | Stop −30%. Time stop 60m. Sell half at +100%. Trailing stop 25% off the high, armed once up 25%. No sell route for 2 minutes means a write-off. |
| Cost | code | ≤ 60 Claude calls/hour and ≤ $5/day of Claude spend. Then candidates are skipped. |

## Quick start (paper)

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...                  # or `ant auth login`
python -m memesniper run                      # paper: real launches, real quotes, simulated fills
python -m memesniper status                   # open positions, today's P&L, Claude spend, halt reason
python -m memesniper journal -n 50            # every decision, including rejections and Claude's reasoning
python -m memesniper check <MINT> --ask       # screen one token and ask Claude, without trading
python -m memesniper stop | resume            # kill switch for new entries (exits keep running)
```

State lives in `memesniper_state.db` (SQLite). Every candidate is journaled with its
metrics, so you can judge the filters and Claude's calls before risking anything.

## Going live

1. Run paper for at least a few days and read the journal. If paper doesn't make money,
   live won't either: live fills are worse.
2. Create a **new burner wallet**. Fund it only with what you are prepared to lose
   entirely. Never use a wallet that holds anything else.
3. Use a paid RPC (Helius, Triton, QuickNode). The public endpoint is rate-limited and slow.
4. Set `MEMESNIPER_MODE=live` and `MEMESNIPER_PRIVATE_KEY=<base58>` in `.env`, then run
   `python -m memesniper run --live`. Both are required. Live mode prints its caps and
   waits 5s before starting.

## Honest expectations

- Most new meme coins go to zero. The screen removes the obvious rugs, but it can't
  detect a coordinated dump by wallets that look organic.
- The fastest snipers land in the same block as the launch, using co-located nodes and
  Jito bundles. This bot deliberately doesn't compete there. It waits ~30s, plus Claude's
  few seconds, to see whether real demand shows up, and pays a higher entry for that
  information. Whether that trade-off has an edge is unproven: paper-trade and measure.
- Paper fills use live Jupiter quotes minus `PAPER_EXTRA_SLIPPAGE_PCT` (2%) and fees, but
  can't model being front-run or a failed transaction during a price spike.
- Jupiter is the only execution venue. A brand-new pump.fun token can take a moment to
  become routable, so such tokens fail the "no buy route" check rather than being bought blind.

## Layout

| File | Purpose |
|---|---|
| `config.py` | All `MEMESNIPER_*` settings and defaults |
| `feeds.py` | PumpPortal websocket hub (launches, migrations, per-token trades), DexScreener poller |
| `safety.py` | Hard rug screen (RPC, RugCheck, Jupiter round trip, holders, observation) |
| `agent.py` | Claude entry decision (structured output, cost and rate caps, fail-closed) |
| `risk.py` | Caps and clamps applied after Claude |
| `exits.py` | Pure exit rules |
| `executor.py` | Paper and live (Jupiter + solders signing) execution |
| `engine.py` | Orchestration and position monitor |
| `state.py` | SQLite journal |

Tests: `python -m pytest tests/test_memesniper.py`. They run fully offline against fakes.
