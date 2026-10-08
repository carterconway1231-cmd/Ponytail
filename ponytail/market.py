"""Per-run cache of market data as Robinhood actually returned it.

PostToolUse hooks feed every Robinhood read response through `ingest`, so
risk checks run against broker data rather than numbers the model typed.
Anything not observed this run is simply absent, which fails closed.
"""
import json
import re
from datetime import datetime, timezone

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


SAVED_OUTPUT_RE = re.compile(r"Output has been saved to (\S+?\.(?:txt|json))")


def decode_tool_response(resp):
    """MCP tool responses reach hooks as a JSON string, a list of content
    blocks, or a dict wrapping either. Return the decoded payload dict.

    Responses too large for the model's context (multi-year bar history)
    arrive as a stub naming the file Claude Code saved them to; follow it."""
    if isinstance(resp, str):
        try:
            return decode_tool_response(json.loads(resp))
        except json.JSONDecodeError:
            m = SAVED_OUTPUT_RE.search(resp)
            if m:
                try:
                    with open(m.group(1)) as f:
                        return decode_tool_response(f.read())
                except OSError:
                    return None
            return None
    if isinstance(resp, list):
        for block in resp:
            text = block.get("text") if isinstance(block, dict) else block if isinstance(block, str) else None
            if text:
                decoded = decode_tool_response(text)
                if decoded is not None:
                    return decoded
        return None
    if isinstance(resp, dict):
        if "data" in resp:
            return resp
        if "content" in resp:
            return decode_tool_response(resp["content"])
        if "result" in resp:
            return decode_tool_response(resp["result"])
    return None


def _f(x):
    return None if x is None or x == "" else float(x)


class MarketCache:
    def __init__(self):
        self.bars = {}         # symbol -> {"day" | "hour" | ...: [raw OHLCV bars, oldest first]}
        self.instruments = {}  # option_id -> {symbol, type, strike, expiration, tradable}
        self.quotes = {}       # option_id -> {bid, ask, mark, delta, open_interest, iv, updated_at}
        self.earnings = {}     # symbol -> [YYYY-MM-DD] report dates (empty list = looked up, none)
        self.broker_positions = None  # option_id -> quantity, once get_option_positions is seen
        self.orders = {}       # order_id -> normalized option order (fills, stops, cancels)

    def ingest(self, tool, tool_input, resp):
        payload = decode_tool_response(resp)
        if not payload or not isinstance(payload.get("data"), dict):
            return
        data = payload["data"]
        handler = getattr(self, f"_ingest_{tool}", None)
        if handler:
            handler(data, tool_input or {})

    def _ingest_get_equity_historicals(self, data, tool_input):
        for result in data.get("results", []):
            bars = [b for b in result.get("bars", []) if not b.get("interpolated") and b.get("close_price")]
            if bars:
                self.bars.setdefault(result["symbol"].upper(), {})[result.get("interval") or "day"] = bars

    def _ingest_get_option_instruments(self, data, tool_input):
        for inst in data.get("instruments", []):
            self.instruments[inst["id"]] = {
                "symbol": inst.get("chain_symbol", "").upper(),
                "type": inst.get("type"),
                "strike": _f(inst.get("strike_price")),
                "expiration": inst.get("expiration_date"),
                "tradable": inst.get("tradability") == "tradable" and inst.get("state") == "active",
                "ticks": {"above": _f((inst.get("min_ticks") or {}).get("above_tick")),
                          "below": _f((inst.get("min_ticks") or {}).get("below_tick")),
                          "cutoff": _f((inst.get("min_ticks") or {}).get("cutoff_price"))},
            }

    def _ingest_get_option_quotes(self, data, tool_input):
        for result in data.get("results", []):
            q = result.get("quote") or result
            if not q.get("instrument_id"):
                continue
            self.quotes[q["instrument_id"]] = {
                "bid": _f(q.get("bid_price")),
                "ask": _f(q.get("ask_price")),
                "mark": _f(q.get("mark_price")),
                "delta": _f(q.get("delta")),
                "iv": _f(q.get("implied_volatility")),
                "open_interest": int(q.get("open_interest") or 0),
                "updated_at": q.get("updated_at"),
            }

    def _ingest_get_earnings_results(self, data, tool_input):
        symbol = (tool_input.get("symbol") or "").strip().upper()
        if symbol:
            self.earnings[symbol] = [
                r["report"]["date"] for r in data.get("results", []) if (r.get("report") or {}).get("date")
            ]

    def _ingest_get_option_positions(self, data, tool_input):
        if not tool_input.get("nonzero"):
            return  # only an open-positions listing tells us what is held
        held = {} if self.broker_positions is None else self.broker_positions
        for pos in data.get("positions", data.get("results", [])):
            option_id = pos.get("option_id")
            if not option_id:
                m = UUID_RE.search(str(pos.get("option", "")))
                option_id = m.group(0) if m else None
            qty = _f(pos.get("quantity")) or 0
            if option_id and qty > 0 and pos.get("type", "long") == "long":
                held[option_id] = qty
        self.broker_positions = held

    def _ingest_get_option_orders(self, data, tool_input):
        for o in data.get("orders", []):
            legs = o.get("legs") or [{}]
            self.ingest_order(o, legs[0])

    def ingest_order(self, o, leg):
        if not o.get("id"):
            return
        self.orders[o["id"]] = {
            "state": o.get("state"), "type": o.get("type"), "trigger": o.get("trigger"),
            "option_id": leg.get("option_id"), "side": leg.get("side"), "effect": leg.get("position_effect"),
            "quantity": _f(o.get("quantity")), "processed_quantity": _f(o.get("processed_quantity")) or 0,
            "processed_premium": _f(o.get("processed_premium")), "multiplier": _f(o.get("trade_value_multiplier")) or 100,
            "stop_price": _f(o.get("stop_price")), "time_in_force": o.get("time_in_force"),
        }

    def summarize(self, tool, payload):
        """Compact stand-in for bulky responses, so the model gets what it
        needs to decide without spending context on raw rows. Returns None to
        pass the original through."""
        data = (payload or {}).get("data")
        if not isinstance(data, dict):
            return None
        if tool == "get_equity_historicals":
            lines = []
            for r in data.get("results", []):
                bars = [b for b in r.get("bars", []) if not b.get("interpolated")]
                if bars:
                    lines.append(f"{r['symbol']} {r.get('interval')}: {len(bars)} bars "
                                 f"{bars[0]['begins_at'][:10]}..{bars[-1]['begins_at'][:10]}, "
                                 f"last close {float(bars[-1]['close_price']):.2f}")
                else:
                    lines.append(f"{r.get('symbol')} {r.get('interval')}: no real bars (gap-fill only)")
            missing = data.get("not_found")
            return ("Bars stored for compute_signals / warm_start (raw rows omitted to save context):\n"
                    + "\n".join(lines) + (f"\nnot found: {missing}" if missing else ""))
        if tool == "get_option_instruments":
            rows = [{"id": i["id"], "strike": float(i["strike_price"]), "type": i["type"],
                     "exp": i["expiration_date"], "tradable": i.get("tradability") == "tradable"}
                    for i in data.get("instruments", [])]
            return json.dumps({"instruments": rows, "next": data.get("next")}, separators=(",", ":"))
        if tool == "get_option_orders":
            rows = []
            for o in data.get("orders", []):
                n = self.orders.get(o.get("id"), {})
                rows.append({"id": o.get("id"), "state": o.get("state"), "type": o.get("type"),
                             "trigger": o.get("trigger"), "side": n.get("side"), "effect": n.get("effect"),
                             "option_id": n.get("option_id"), "qty": n.get("quantity"),
                             "filled_qty": n.get("processed_quantity"), "stop_price": n.get("stop_price"),
                             "created_at": o.get("created_at"), "chain": o.get("chain_symbol")})
            return json.dumps({"orders": rows, "next": data.get("next")}, separators=(",", ":"))
        return None

    def quote_age_minutes(self, option_id, now=None):
        updated = (self.quotes.get(option_id) or {}).get("updated_at")
        if not updated:
            return None
        # Robinhood sends nanoseconds; fromisoformat takes at most microseconds.
        ts = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", updated.replace("Z", "+00:00")))
        return ((now or datetime.now(timezone.utc)) - ts).total_seconds() / 60
