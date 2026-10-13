"""Phone alerts. Every channel is optional; a failed alert never affects trading.

- ntfy:     install the ntfy app, subscribe to a hard-to-guess topic name, set MEMESNIPER_NTFY_TOPIC.
- Telegram: create a bot with @BotFather, set MEMESNIPER_TELEGRAM_BOT_TOKEN and _CHAT_ID.
- webhook:  any URL that accepts a JSON POST (Discord, Zapier, your own server).
"""
import logging

log = logging.getLogger("memesniper")


class Notifier:
    def __init__(self, settings, client):
        self.s, self.client = settings, client
        self._last_halt = None

    @property
    def enabled(self):
        s = self.s
        return bool(s.ntfy_topic or (s.telegram_bot_token and s.telegram_chat_id) or s.webhook_url)

    async def send(self, title, message, urgent=False, event="info", data=None):
        if not self.enabled:
            return
        s = self.s
        jobs = []
        if s.ntfy_topic:
            jobs.append(self._post(f"{s.ntfy_server.rstrip('/')}/{s.ntfy_topic}",
                                   content=message.encode(),
                                   headers={"Title": title, "Priority": "high" if urgent else "default",
                                            "Tags": event}))
        if s.telegram_bot_token and s.telegram_chat_id:
            jobs.append(self._post(f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
                                   json={"chat_id": s.telegram_chat_id, "text": f"{title}\n{message}",
                                         "disable_web_page_preview": True}))
        if s.webhook_url:
            jobs.append(self._post(s.webhook_url, json={"event": event, "title": title,
                                                        "message": message, "data": data or {}}))
        for job in jobs:
            await job

    async def _post(self, url, **kwargs):
        try:
            r = await self.client.post(url, timeout=5.0, **kwargs)
            if r.status_code >= 300:
                log.warning("alert to %s failed: HTTP %s", url.split("/bot")[0], r.status_code)
        except Exception as e:
            log.warning("alert failed: %s", e)

    async def halted(self, reason):
        """Alert once per distinct halt reason, not on every candidate."""
        if reason == self._last_halt:
            return
        self._last_halt = reason
        if reason:
            await self.send("memesniper paused", reason, urgent=True, event="halt")

    def resumed(self):
        self._last_halt = None


def dexscreener_link(mint):
    return f"https://dexscreener.com/solana/{mint}"
