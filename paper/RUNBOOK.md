# Paper-trading cycle (STRATEGY=premium)

Run by the scheduled check-ins in the Claude Code session. Robinhood data comes from that
session's Robinhood connector; every response goes through `python -m ponytail.drive`, which applies
the agent's own ingest hooks, guardrails and paper interception. Nothing here can send a real order:
the driver refuses `LIVE_TRADING=true`, and the agentic account has no options approval.

Setup each cycle (fresh containers start clean):

```bash
cd /home/user/Ponytail && git pull origin claude/claude-code-6fa8w3
set -a; . paper/paper.env; ROBINHOOD_ACCOUNT_NUMBER=<agentic account from get_accounts>; set +a
D="python -m ponytail.drive"
```

Save each Robinhood response verbatim to `.ponytail_run/<name>.json` (or pass the path Claude Code
saved an oversized result to), then `$D ingest <tool> <file> '<tool input json>'`.

1. `$D start`: skip entries if `entry_window_open` is false (09:45-15:45 ET), still manage positions.
2. **Bars**: `get_equity_historicals` symbols SPY,QQQ, interval `day`, start about 45 days back → ingest.
3. **Context** (judgment only, not ingested): `get_equity_quotes` SPY,QQQ and VIX via
   `get_indexes` + `get_index_quotes`. Skip new entries on a disorderly tape (SPY down >2% on the day,
   VIX up >20% on the day); the code's IV ≥ RV filter handles the rest.
4. **Positions** (if any open): `get_option_positions` (nonzero=true) and quotes for both legs of each
   held spread → ingest → `$D call review_positions`. For each `CLOSE` row: `$D order '<close_order>'`.
5. **Entries** per symbol (if window open and no open spread on it):
   - `get_option_chains` → choose the expiration with DTE closest to 45 inside 30-60.
   - `get_option_instruments` chain_symbol, that expiration, type `put`, with `cursor` set to
     base64 of `p=<strike ~7% below spot>` (e.g. `python -c "import base64;print(base64.b64encode(b'p=722.0000').decode())"`):
     the cursor is the strike to start after, so this skips ~100 deep-OTM strikes. Save only the
     strikes you will quote → ingest.
   - Quote ~16 puts: 2 strikes nearest spot (ATM IV) and every strike from ~3% to ~9% below spot
     (SPY step $1-5, QQQ $1-5; include the strikes one width beyond the shorts) → ingest.
   - `$D call rank_credit_spreads '{"symbol":"SPY"}'`. If `premium_rich` is false or no candidates: skip.
   - Pick the top candidate unless the context argues against it;
     `$D call propose_credit_spread '{...short, long, quantity=max_quantity, limit_credit=suggested_limit_credit, thesis}'`.
   - If approved: `$D order '<order>'` (paper fill at the limit).
6. `$D call portfolio_status` / `$D call performance` for the summary.
7. Commit `paper/agent_state.json` (message: `paper cycle YYYY-MM-DD HH:MM ET: <one line>`) and push.
8. Quotes must be fresh (< 30 min) for an approval; the pre-market quotes are the prior close.
9. Report to the user only if something happened (open, close, breaker, error); otherwise stay quiet.
