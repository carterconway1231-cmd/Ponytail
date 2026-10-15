"""Credibility: is there a real project behind the token?

Checks, each fail-soft (unknown is never treated as good):
- links: website, X/Twitter, Telegram, Discord gathered from pump.fun metadata and DexScreener
- website: loads, has real content, mentions this token's address or ticker
- domain age via RDAP (a domain registered yesterday is a throwaway)
- Telegram: public member count from the t.me preview page
- reuse: the same X/Telegram/website already used by other tokens this bot has seen, which
  is how copycat scams borrow a real project's credibility

The result is a 0-100 score plus the facts behind it. Claude sees all of it; code only
hard-rejects copied socials and, if you set them, a minimum score or required socials.
X/Twitter itself can't be read without a paid API, so handles are collected, not verified.
"""
import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from .net import get_json, get_text

FREE_HOSTS = ("vercel.app", "netlify.app", "github.io", "pages.dev", "linktr.ee", "carrd.co",
              "wixsite.com", "webflow.io", "pump.fun", "dexscreener.com", "x.com", "twitter.com",
              "t.me", "telegram.me", "medium.com", "gitbook.io", "notion.site")
_TG_MEMBERS = re.compile(r'tgme_page_extra">\s*([\d\s,.]+)\s*(members|subscribers)', re.I)
_TWITTER = re.compile(r"(?:twitter\.com|x\.com)/(?:#!/)?@?([A-Za-z0-9_]{1,15})(?:[/?#]|$)", re.I)
_TELEGRAM = re.compile(r"(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_+]{3,64})", re.I)
_TWITTER_RESERVED = {"home", "search", "intent", "share", "i", "explore", "hashtag"}


def _url(u):
    u = (u or "").strip()
    if not u:
        return ""
    return u if re.match(r"^https?://", u, re.I) else f"https://{u}"


def extract_links(launch, pair=None):
    """All social/website links we know about -> {website, twitter, telegram, discord}."""
    candidates = list((launch.socials or {}).items())
    pair = pair or {}
    candidates += [("website", w) for w in pair.get("websites") or []]
    candidates += list((pair.get("socials") or {}).items())
    out = {}
    for kind, url in candidates:
        url = _url(url if isinstance(url, str) else "")
        if not url:
            continue
        kind = (kind or "").lower()
        if m := _TWITTER.search(url):
            if m.group(1).lower() not in _TWITTER_RESERVED:
                out.setdefault("twitter", m.group(1).lower())
        elif m := _TELEGRAM.search(url):
            out.setdefault("telegram", m.group(1).lower())
        elif "discord" in url.lower():
            out.setdefault("discord", url)
        elif kind in ("website", "web", "site", "homepage", "link") or kind == "":
            host = urlparse(url).hostname or ""
            if host and not any(host.endswith(h) for h in ("dexscreener.com", "pump.fun")):
                out.setdefault("website", url)
    return out


def link_keys(links):
    """Normalized identifiers used to detect the same socials on other tokens."""
    keys = []
    if links.get("twitter"):
        keys.append(f"x:{links['twitter']}")
    if links.get("telegram"):
        keys.append(f"tg:{links['telegram']}")
    if links.get("website"):
        host = (urlparse(links["website"]).hostname or "").lower().removeprefix("www.")
        if host and not any(host.endswith(h) for h in FREE_HOSTS):
            keys.append(f"web:{host}")
    return keys


def parse_tg_members(html):
    m = _TG_MEMBERS.search(html or "")
    if not m:
        return None
    digits = re.sub(r"[^\d]", "", m.group(1))
    return int(digits) if digits else None


def domain_age_days(rdap, now=None):
    for ev in (rdap or {}).get("events") or []:
        if ev.get("eventAction") == "registration" and ev.get("eventDate"):
            try:
                reg = datetime.fromisoformat(ev["eventDate"].replace("Z", "+00:00"))
            except ValueError:
                return None
            now = now or datetime.now(timezone.utc)
            return max((now - reg).days, 0)
    return None


def website_facts(status, html, launch):
    text = (html or "").lower()
    visible = re.sub(r"<[^>]+>", " ", text)
    words = len(visible.split())
    sym = (launch.symbol or "").lower()
    mentions = bool(launch.mint and launch.mint.lower() in text) or (
        len(sym) >= 3 and (f"${sym}" in text or re.search(rf"\b{re.escape(sym)}\b", visible) is not None))
    return {"status": status, "live": status == 200 and words >= 30, "words": words,
            "mentions_token": mentions}


def score(facts):
    """0-100. Each point needs evidence; missing data scores nothing."""
    s = 0
    web = facts.get("website") or {}
    if web.get("live"):
        s += 20
        s += 15 if web.get("mentions_token") else 0
    age = facts.get("domain_age_days")
    if age is not None:
        s += 10 if age >= 30 else 5 if age >= 7 else 0
    if facts.get("links", {}).get("twitter"):
        s += 15
    if facts.get("links", {}).get("telegram"):
        s += 5
        members = facts.get("telegram_members") or 0
        s += 15 if members >= 1000 else 10 if members >= 100 else 0
    if len(facts.get("description") or "") >= 40:
        s += 5
    if not facts.get("reused_by"):
        s += 15 if facts.get("links") else 0
    return min(s, 100)


class CredibilityChecker:
    def __init__(self, settings, state, client, rdap_url="https://rdap.org/domain"):
        self.s, self.state, self.client, self.rdap = settings, state, client, rdap_url.rstrip("/")

    async def check(self, launch, pair=None):
        links = extract_links(launch, pair)
        facts = {"links": links, "description": (launch.description or "")[:300]}
        jobs = {}
        if links.get("website"):
            jobs["website"] = get_text(self.client, links["website"])
            host = (urlparse(links["website"]).hostname or "").lower().removeprefix("www.")
            if host and not any(host.endswith(h) for h in FREE_HOSTS):
                jobs["rdap"] = get_json(self.client, f"{self.rdap}/{host}")
            else:
                facts["free_hosting"] = True
        if links.get("telegram"):
            jobs["telegram"] = get_text(self.client, f"https://t.me/{links['telegram']}")
        results = dict(zip(jobs, await asyncio.gather(*jobs.values()))) if jobs else {}
        if "website" in results:
            facts["website"] = website_facts(*results["website"], launch)
        if "rdap" in results:
            facts["domain_age_days"] = domain_age_days(results["rdap"])
        if "telegram" in results:
            facts["telegram_members"] = parse_tg_members(results["telegram"][1])
        keys = link_keys(links)
        facts["reused_by"] = self.state.link_reuse(keys, launch.key) if keys else {}
        self.state.record_links(keys, launch.key)
        facts["score"] = score(facts)
        return facts


def check_credibility(report, facts, settings):
    report.metrics["credibility"] = facts
    worst = max((facts.get("reused_by") or {}).values(), default=0)
    if worst > settings.cred_max_link_reuse:
        link = max(facts["reused_by"], key=facts["reused_by"].get)
        report.fail(f"socials copied: {link} also used by {worst} other tokens")
    elif worst:
        report.warnings.append(f"socials shared with {worst} other token(s)")
    if settings.cred_require_social and not (facts["links"].get("twitter") or facts["links"].get("telegram")):
        report.fail("no X/Twitter or Telegram")
    if facts["score"] < settings.cred_min_score:
        report.fail(f"credibility score {facts['score']} < {settings.cred_min_score}")
    web = facts.get("website") or {}
    if facts["links"].get("website") and not web.get("live"):
        report.warnings.append("website listed but not loading or empty")
    age = facts.get("domain_age_days")
    if age is not None and age < 3:
        report.warnings.append(f"website domain registered {age} day(s) ago")
