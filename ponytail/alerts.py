"""Push notifications for things worth knowing about right away.

POSTs JSON to ALERT_WEBHOOK_URL. The payload carries the message as both
"text" (Slack incoming webhooks) and "content" (Discord), and ntfy.sh topics
accept any body, so one URL setting covers the common free options. Failures
never interrupt trading.
"""
import json
import logging
import urllib.request

log = logging.getLogger("ponytail.alerts")

ALERT_KINDS = {"opened", "closed", "stopped_out", "close_submitted", "circuit_breaker", "order_failed",
               "go_live_blocked", "run_failed", "position_dropped", "dropped"}


def format_event(kind, fields, mode):
    tag = f"[ponytail {mode}]"
    if kind == "opened":
        c = fields.get("contract") or {}
        return f"{tag} OPENED {c.get('symbol')} {c.get('strike')} {c.get('type')} {c.get('expiration')} x{fields.get('quantity'):g} @ {fields.get('price')}"
    if kind in ("closed", "stopped_out"):
        return f"{tag} {'STOPPED OUT' if kind == 'stopped_out' else 'CLOSED'} {fields.get('option_id', '')[:8]} @ {fields.get('price')} P&L {fields.get('pnl')}"
    if kind == "circuit_breaker":
        return f"{tag} circuit breaker: {'; '.join(fields.get('tripped', []))}"
    return f"{tag} {kind}: {json.dumps(fields, default=str)[:300]}"


def send(url, message, timeout=5):
    if not url:
        return False
    body = json.dumps({"text": message, "content": message}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception as e:  # noqa: BLE001 - alerts are best-effort by design
        log.warning("alert failed: %s", e)
        return False
