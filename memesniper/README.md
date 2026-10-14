# memesniper

A meme coin trading bot for **Solana, Base, BSC and Ethereum** where **Claude makes every
entry decision**, inside hard limits that code enforces. It is separate from the Ponytail
stock/options bot and shares no state with it. **Paper trading is the default.**

```
 PumpPortal ws ───┐ new pump.fun tokens + migrations (Solana)
 GeckoTerminal ───┤ new pools on every enabled chain
 DexScreener  ────┤ newly profiled tokens on every enabled chain
 Trending  ───────┤ GeckoTerminal trending pools + DexScreener top boosts → screen → watchlist only
                  ▼
   launch: dedupe ─► observe (pump.fun 30s / pool stats) ─► safety screen (code, per chain)
                                                              │ hard fail → journal, stop
                     passed tokens join the WATCHLIST ◄───────┤
                                                              ▼
                     credibility (website, X, Telegram, copied socials, domain age)
                     + market (buys/sells, unique buyers/sellers, volume per window)
   watchlist, every 60s for 24h:                              ▼
     breakout score ≥ 0.7  ─┐            Claude (BUY/SKIP, size in USD, exits)
     dip in an uptrend     ─┴─► re-screen ─────────►          ▼
                                     RiskManager: USD caps across all chains, clamps size/stops
                                                              ▼
                              Jupiter (Solana) / KyberSwap (EVM)   paper: fills on real quotes
                                                              ▼
   monitor every 5s: stop · liquidity pulled · time stop · take-profit ladder ·
                     momentum fade (in profit) · trailing stop
```

## Who decides what

| Stage | Owner | Rule (defaults) |
|---|---|---|
| Discovery | code | pump.fun launches via PumpPortal; new pools on all enabled chains via GeckoTerminal; new token profiles via DexScreener. |
| Observation | code | pump.fun: the first `OBSERVE_SECONDS` (30) of trades (distinct buyers, buy/sell volume, largest buy, dev selling). Other pools: GeckoTerminal's last-hour buyers, buys and sells. |
| Hard screen, Solana | code | Mint and freeze authority revoked. No Token-2022 fee/hook/delegate/pause extensions. RugCheck score ≤ 5000. |
| Hard screen, EVM | code | GoPlus: no honeypot, hidden owner, reclaimable ownership, owner-editable balances, blacklist, pausable trading, adjustable tax, self-destruct; source verified; buy and sell tax ≤ 10%. honeypot.is: a simulated buy and sell succeeds with ≤ 10% tax. Round-trip gas ≤ 8% of the trade (filters most small Ethereum trades). Warnings for Claude: unlocked liquidity, mintable supply, upgradeable proxy. |
| Hard screen, all chains | code | Top-10 holders ≤ 35% (pools, curves, locks excluded). Creator holds ≤ 10%. A buy→sell quote round trip loses ≤ 12%. ≥ 8 distinct buyers. Dev sold ≤ 50%. ≥ $5k liquidity off the pump.fun curve. Creator has no rug seen by this bot and ≤ 3 other launches in 24h. Fails never reach Claude. |
| Credibility | code + **Claude** | For every candidate that passes the screen: collects website, X, Telegram and Discord links; checks the website loads, has real content and mentions this token; looks up the domain's registration date (RDAP); reads the Telegram member count; flags socials already used by other tokens. 0–100 score for Claude. Hard rejects: socials copied by more than `CRED_MAX_LINK_REUSE` (2) other tokens; optionally no socials (`CRED_REQUIRE_SOCIAL`) or a low score (`CRED_MIN_SCORE`). X itself can't be read without a paid API, so handles are collected, not verified. |
| Market activity | code + **Claude** | GeckoTerminal per-window (5m, 15m, 30m, 1h, 24h) buys, sells, **unique buyers and sellers**, volume, price change, liquidity and pool age. Warnings for sellers outnumbering buyers 2:1 and for many trades from few wallets (wash trading). |
| Watchlist | code | Every token that passes joins the watchlist for 24h and is re-scored every minute. **Breakout:** volume speeding up, buyers outnumbering sellers, price and liquidity rising; score ≥ 0.7, and not already up >300% in the hour. **Dip:** up ≥ 50% over 6h, now 15–40% off its 2h high, liquidity ≥ 85% of its peak, buyers back in control. A signal re-runs the full safety screen first. |
| Entry | **Claude** | One structured-output call per candidate (`claude-opus-5-5`, effort `low`, 25s timeout). Sees the chain, strategy and signal, token, metadata, safety metrics and warnings, portfolio state and the last 10 closed trades. Returns BUY/SKIP, confidence, size in USD, take-profit, stop and max hold. Timeouts, errors and refusals mean SKIP. Strategies left out of `CLAUDE_STRATEGIES` buy on the signal alone. |
| Risk | code | All caps in USD across every chain together: ≤ $10/trade, ≤ 4 open positions, ≤ $40 at risk, ≤ $80 spent/day, $40 daily loss limit (realized plus open positions marked at their stops), ≤ 6 entries/hour, one trade per creator per 24h, never twice in the same token, $5 left in each wallet for gas. Claude's confidence ≥ 0.6. Claude can tighten stops and holds but never loosen them. No native-coin price means no trade. |
| Exits | code | Stop −30%. Sell everything if liquidity drops below 70% of its peak. Time stop 60m. Take-profit ladder (`TP_LADDER`): half at +TP (Claude's, default +100%), half the rest at +2×TP. Momentum fade (sellers 2:1, price falling) sells the rest while in profit. Trailing stop 25% off the high once up 25%. No sell route for 2 minutes means a write-off. |
| Cost | code | ≤ 60 Claude calls/hour and ≤ $5/day of Claude spend. Then candidates are skipped. |

**What this can't do:** know in advance that a coin will "blow up", or buy the exact bottom and
sell the exact top. Those are only visible afterwards. A breakout score shows momentum building,
and most tokens that score high still go nowhere. `outcomes` measures how often each strategy's
signals actually pay, per chain.

## Quick start (paper)

```bash
pip install -r memesniper/requirements.txt   # just the sniper's libraries
export ANTHROPIC_API_KEY=...                  # or `ant auth login`
python -m memesniper run                      # paper: real launches, real quotes, simulated fills
bash memesniper/scripts/run-forever.sh        # same, restarting automatically after crashes (logs/)
python -m memesniper status                   # open positions, today's P&L, Claude spend, halt reason
python -m memesniper journal -n 50            # every decision, including rejections and Claude's reasoning
python -m memesniper check <ADDRESS> --chain base --ask   # screen one token (+ask Claude), no trade
python -m memesniper stop | resume            # kill switch for new entries (exits keep running)
python -m memesniper outcomes --days 1        # how tokens did after each decision (tuning data)
python -m memesniper creator <WALLET>         # a creator's launch history and wallet age
python -m memesniper test-alert               # check phone alerts are wired up
python -m memesniper wallet                   # the bot's Solana and EVM addresses and balances
```

State lives in `memesniper_state.db` (SQLite). Every candidate is journaled with its
metrics, so you can judge the filters and Claude's calls before risking anything.

Choose what runs with `MEMESNIPER_CHAINS` (default `solana,base,bsc,ethereum`),
`MEMESNIPER_STRATEGIES` (default `launch,breakout,dip`) and `MEMESNIPER_CLAUDE_STRATEGIES`
(which strategies need Claude's approval; by default all of them).

## Going live

1. Run paper for at least a few days and read `outcomes`. If paper doesn't make money,
   live won't either: live fills are worse.
2. Create **new burner wallets** and fund them only with what you are prepared to lose
   entirely. Solana needs SOL. One EVM wallet covers Base and Ethereum (ETH) and BSC (BNB).
3. Use paid RPCs (Helius for Solana; Alchemy, QuickNode or similar for EVM). Public
   endpoints are rate-limited and slow. Set them with `MEMESNIPER_RPC_URL`,
   `MEMESNIPER_BASE_RPC_URL`, `MEMESNIPER_BSC_RPC_URL` and `MEMESNIPER_ETHEREUM_RPC_URL`.
4. Set `MEMESNIPER_MODE=live`, `MEMESNIPER_PRIVATE_KEY=<base58>` (Solana) and/or
   `MEMESNIPER_EVM_PRIVATE_KEY=<0x hex>` (EVM) in `.env`, leaving any chain you have no key for
   out of `MEMESNIPER_CHAINS`. Then run `python -m memesniper run --live`. Both switches are
   required. Live mode prints its caps and waits 5s before starting.
5. On EVM chains the first sale of each token needs an extra approval transaction (gas) so the
   router can move it. That cost is included in the position's P&L.

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
`OUTCOME_MAX_NEW_PER_HOUR` (300); each tracked token costs four quotes. The report also
breaks bought tokens down **by strategy** and **by chain**.

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
2. In that account, export the private key and put it in `.env`: a Solana key goes in
   `MEMESNIPER_PRIVATE_KEY`, an EVM key in `MEMESNIPER_EVM_PRIVATE_KEY`. Run
   `python -m memesniper wallet` and check the addresses match the ones FOMO shows. Only the chains
   FOMO actually holds a wallet on can share it; give the bot its own burner for the others.
3. The bot pays in each chain's native coin (SOL, ETH, BNB). If `wallet` shows little of it, swap
   some USDC in first. About $5 per chain stays untouched for fees (`MIN_WALLET_RESERVE_USD`).
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
- Jupiter (Solana) and KyberSwap (EVM) are the only execution venues. A brand-new token can take
  a moment to become routable, so such tokens fail the "no buy route" check rather than being
  bought blind.
- Ethereum gas usually makes $10 trades uneconomic: the gas guard rejects them unless gas is
  unusually cheap. Raise `MAX_USD_PER_TRADE` if you want Ethereum to actually trade.
- Watchlist history is held in memory. After a restart, signals need a few minutes of fresh
  snapshots before they can fire again.
- EVM safety relies on GoPlus and honeypot.is. Both are free third-party services; if GoPlus has
  no data on a token, the token is rejected.

## Layout

| File | Purpose |
|---|---|
| `config.py` | All `MEMESNIPER_*` settings and defaults |
| `chains.py` | Chain registry (Solana, Base, BSC, Ethereum) and per-chain context |
| `feeds.py` | PumpPortal websocket hub, DexScreener and GeckoTerminal feeds, watchlist snapshots |
| `watchlist.py` | Watchlist, breakout score, dip signal, momentum-fade detection |
| `credibility.py` | Website, domain age, Telegram members, copied-socials detection, credibility score |
| `market.py` | Per-window buyers/sellers, volume and liquidity from GeckoTerminal |
| `scripts/run-forever.sh` | Keeps the bot running: restarts it after crashes, logs to `logs/` |
| `safety.py` | Hard rug screen per chain (RPC, RugCheck, round trip, holders, observation) |
| `evm_safety.py` | EVM checks: GoPlus contract flags and taxes, honeypot.is simulation, gas guard |
| `evm.py` | KyberSwap routing, EVM JSON-RPC, signing, paper and live EVM executors |
| `prices.py` | USD price of SOL/ETH/BNB for USD-denominated risk |
| `agent.py` | Claude entry decision (structured output, cost and rate caps, fail-closed) |
| `risk.py` | Caps and clamps applied after Claude |
| `exits.py` | Pure exit rules |
| `executor.py` | Solana paper and live (Jupiter + solders signing) execution |
| `engine.py` | Orchestration, position monitor, wallet sync for manual (FOMO app) sells |
| `notify.py` | Phone alerts: ntfy, Telegram, webhook |
| `outcomes.py` | Tracks tokens after each decision; the `outcomes` report |
| `creator.py` | Creator history: local launches/rugs, pump.fun history, wallet age |
| `state.py` | SQLite journal |

Tests: `python -m pytest tests/test_memesniper.py`. They run fully offline against fakes.
