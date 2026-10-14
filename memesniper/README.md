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
| Hard screen | code | Mint and freeze authority revoked. No Token-2022 fee/hook/delegate/pause extensions. Top-10 holders ≤ 35% (bonding curve and pool vaults excluded). Creator holds ≤ 10%. RugCheck score ≤ 5000. A Jupiter buy→sell round trip loses ≤ 12% (catches honeypots and sell taxes). ≥ 8 distinct buyers. Dev sold ≤ 50%. DEX pairs need ≥ $5k liquidity. Creator has no rug seen by this bot and ≤ 3 other launches in 24h. Fails never reach Claude. |
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
python -m memesniper outcomes --days 1        # how tokens did after each decision (tuning data)
python -m memesniper creator <WALLET>         # a creator's launch history and wallet age
python -m memesniper test-alert               # check phone alerts are wired up
python -m memesniper wallet                   # which wallet the bot trades from (live)
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

## Outcome tracking: is any of this working?

Every token Claude reviews, and a 10% sample of safety rejections, is tracked after the
decision. The bot re-quotes the position it would have bought and records its value at 5m,
15m, 1h and 4h. `python -m memesniper outcomes` (add `--days 1` for recent only) shows:

- **By decision** (BUY / SKIP / BLOCKED / REJECT): median value vs. entry at each checkpoint,
  the share that reached 2x, and the share that rugged. If SKIPs do as well as BUYs, Claude isn't
  adding value. If BUYs fade from 15m to 1h, the exits hold too long.
- **Rejections by the check that failed first**: a check whose rejects often hit 2x is
  throwing away winners and should be loosened. One whose rejects mostly rug is earning its keep.

A "no sell route" at a checkpoint counts as 0x (usually pulled liquidity). Checkpoints missed
while the bot was off are left out, not guessed. Tune with `OUTCOME_SAMPLE_REJECTS` (0.1) and
`OUTCOME_MAX_NEW_PER_HOUR` (300); each tracked token costs four Jupiter quotes.

## Creator history

Before anything else, each token's creator is checked against:
- **what this bot has seen:** their other launches, and how earlier tokens it tracked turned
  out. A creator whose earlier token fell >90% within an hour is rejected
  (`CREATOR_MAX_PRIOR_RUGS`, 1). This needs no network and builds up as the bot runs.
- **pump.fun's public API:** past coins and how many graduated. More than
  `CREATOR_MAX_LAUNCHES_24H` (3) other launches in a day is rejected as a serial launcher.
  This API is unofficial and may change; set `MEMESNIPER_PUMPFUN_API_URL=` to disable it.
- **wallet age (RPC):** a wallet under 24h old is a warning for Claude, or a hard reject if you set
  `CREATOR_MIN_WALLET_AGE_HOURS`.

Claude sees the whole profile. `python -m memesniper creator <wallet>` shows it for any wallet.

## Connecting the FOMO app

FOMO (fomo.family) has no public trading API, so the bot can't plug into the app itself.
What works is **sharing a wallet**. FOMO wallets are non-custodial, and the app lets you export
the private key (Face ID required). If the bot trades with that key, its buys and sells are
ordinary on-chain trades from your FOMO wallet, so the tokens appear in your FOMO holdings.

1. **Use a second FOMO account for the bot**, not your main one. The bot's caps limit what it
   spends, but a key the bot holds can move *everything* in that wallet. A dedicated account
   funded with what you're willing to lose keeps your main FOMO balance out of reach.
2. In that account, export the private key and put it in `.env` as `MEMESNIPER_PRIVATE_KEY`.
   Run `python -m memesniper wallet` and check the address matches the one FOMO shows.
3. The bot trades **SOL**. If `wallet` shows little SOL, swap some USDC to SOL in FOMO first.
   Keep at least `MIN_WALLET_RESERVE_SOL` (0.05) for fees.
4. You can still trade the bot's coins by hand in FOMO. On every monitor tick the bot compares
   its books with the wallet. If you sold some or all of a position in the app, it stops
   managing those tokens instead of trying to sell them again. It records the sale (its
   proceeds are estimated from the current quote) and alerts you. Coins you buy yourself in
   FOMO are never touched: the bot only manages positions it opened.
5. Keep the key only in `.env` on a machine you control. Never commit it or paste it anywhere.

## Phone alerts

The bot alerts on every buy, partial sell, close, write-off, manual (FOMO) close, and when a
limit pauses it. Set any of these in `.env`, then run `python -m memesniper test-alert`:

| Channel | Setup |
|---|---|
| ntfy (easiest) | Install the ntfy app, subscribe to a long random topic name, set `MEMESNIPER_NTFY_TOPIC` to it. Anyone who knows the topic can read it, so make it unguessable. |
| Telegram | Create a bot with @BotFather and message it once. Set `MEMESNIPER_TELEGRAM_BOT_TOKEN`, plus `MEMESNIPER_TELEGRAM_CHAT_ID` (your chat id, e.g. from @userinfobot). |
| Webhook | `MEMESNIPER_WEBHOOK_URL` gets a JSON POST `{event, title, message, data}` (Discord, Zapier, your own server). |

`MEMESNIPER_ALERT_ON_SKIPS=true` also alerts when Claude passes on a candidate. That can be noisy.

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
| `engine.py` | Orchestration, position monitor, wallet sync for manual (FOMO app) sells |
| `notify.py` | Phone alerts: ntfy, Telegram, webhook |
| `outcomes.py` | Tracks tokens after each decision; the `outcomes` report |
| `creator.py` | Creator history: local launches/rugs, pump.fun history, wallet age |
| `state.py` | SQLite journal |

Tests: `python -m pytest tests/test_memesniper.py`. They run fully offline against fakes.
