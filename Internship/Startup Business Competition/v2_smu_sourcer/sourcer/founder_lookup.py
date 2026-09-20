"""
Founder contact enrichment via LinkedIn URL + email-finder API.

Fallback used ONLY when no email was found on the startup's own website. Two steps:

  1. find_linkedin_url() - a web search (Serper / Brave) for the founder's public
     LinkedIn profile URL. Useful as a reachable contact channel even when no email
     exists.

     !!! Important: we do NOT (and cannot) scrape an email off LinkedIn. Member emails
     are private and profile pages are login-walled + bot-blocked. LinkedIn gives us the
     identity / profile URL only.

  2. find_email_via_api() - a legitimate email-finder that maps a person + company
     domain to an email from its OWN database (Hunter.io by default). This is the real
     way to turn "a named founder at company X" into an email; direct LinkedIn scraping
     is not. Apollo / RocketReach / Proxycurl work similarly and accept a LinkedIn URL -
     swap the endpoint if you use one of those.

Every step no-ops gracefully when its key is absent, so the pipeline still runs. Keys
(in .env):
    SERPER_API_KEY  or  BRAVE_API_KEY     # for the LinkedIn URL search
    HUNTER_API_KEY                        # https://hunter.io email-finder

Every email produced here is UNVERIFIED-by-us (it comes from a third party or a guess) -
review before outreach.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from . import llm
from .email_find import _name_parts
from .extract import clean_text
from .http import Fetcher
from .models import Startup

HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY")


def keys_present() -> dict[str, bool]:
    """Which enrichment capabilities are usable, for a one-line status message."""
    return {
        "linkedin_search": bool(os.environ.get("SERPER_API_KEY")
                                or os.environ.get("BRAVE_API_KEY")),
        "email_api": bool(HUNTER_API_KEY),
    }


# --------------------------------------------------------------------------- #
# Web search (shared Serper/Brave path) - returns [(title, url)] or None (no key)
# --------------------------------------------------------------------------- #
def _web_search(query: str, fetcher: Fetcher, num: int = 10
                ) -> list[tuple[str, str, str]] | None:
    """Return [(title, url, snippet)] or None if no search key is configured."""
    serper = os.environ.get("SERPER_API_KEY")
    brave = os.environ.get("BRAVE_API_KEY")
    if serper:
        req = urllib.request.Request(
            "https://google.serper.dev/search",
            data=json.dumps({"q": query, "num": num}).encode(),
            headers={"X-API-KEY": serper, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=fetcher.timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return []
        return [(o.get("title", ""), o.get("link", ""), o.get("snippet", ""))
                for o in data.get("organic", []) if o.get("link")]
    if brave:
        url = ("https://api.search.brave.com/res/v1/web/search?"
               + urllib.parse.urlencode({"q": query, "count": num}))
        data = fetcher.get_json(url, headers={"X-Subscription-Token": brave})
        web = (data or {}).get("web", {}).get("results", []) if data else []
        return [(o.get("title", ""), o.get("url", ""), o.get("description", ""))
                for o in web if o.get("url")]
    return None


# --------------------------------------------------------------------------- #
# 1. LinkedIn profile URL
# --------------------------------------------------------------------------- #
def find_linkedin_url(name: str, startup: Startup, fetcher: Fetcher) -> str | None:
    """Search for the founder's public LinkedIn profile URL. None if no key / not found."""
    if not name:
        return None
    query = f'"{name}" {startup.name} founder site:linkedin.com/in'
    results = _web_search(query, fetcher)
    if not results:
        return None
    for _title, url, _snip in results:
        low = url.lower()
        if "linkedin.com/in/" in low:
            return url.split("?", 1)[0].rstrip("/")   # strip tracking params
    return None


# --------------------------------------------------------------------------- #
# 2. Email via a finder API (Hunter.io Email Finder: domain + first + last)
# --------------------------------------------------------------------------- #
def find_email_via_api(name: str, domain: str) -> tuple[str, str] | None:
    """Return (email, status) from Hunter.io, or None if no key / domain / match.

    status: 'api_verified' (Hunter score >= 80) or 'api_unverified'.
    """
    if not HUNTER_API_KEY or not domain or not name:
        return None
    first, last = _name_parts(name)
    if not first:
        return None
    params = {"domain": domain, "first_name": first, "api_key": HUNTER_API_KEY}
    if last:
        params["last_name"] = last
    url = "https://api.hunter.io/v2/email-finder?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    email = ((data or {}).get("data") or {}).get("email")
    if not email:
        return None
    score = ((data or {}).get("data") or {}).get("score") or 0
    return email, ("api_verified" if score >= 80 else "api_unverified")


# --------------------------------------------------------------------------- #
# Orchestrator: fill LinkedIn URL + email for founders that still lack an email
# --------------------------------------------------------------------------- #
def enrich_contact(startup: Startup, fetcher: Fetcher, *, use_api: bool = True) -> None:
    """For founders with NO email yet, add a LinkedIn URL and (optionally) an API email."""
    for f in startup.founders:
        if f.email or not f.name:
            continue                                   # page already gave an email; skip
        if not f.linkedin_url:
            li = find_linkedin_url(f.name, startup, fetcher)
            if li:
                f.linkedin_url = li
        if use_api:
            res = find_email_via_api(f.name, startup.domain)
            if res:
                f.email, f.email_status, f.source = res[0], res[1], "hunter"


# --------------------------------------------------------------------------- #
# LLM-driven: web-search + read result pages, let the LLM find email + LinkedIn
# --------------------------------------------------------------------------- #
def _is_social(url: str) -> bool:
    low = url.lower()
    return any(s in low for s in ("linkedin.com", "twitter.com", "x.com",
                                  "facebook.com", "instagram.com", "youtube.com"))


def llm_search_contact(startup: Startup, fetcher: Fetcher, *, max_pages: int = 3) -> None:
    """For founders with NO email, run a web search, fetch a few result pages, and let the
    active LLM extract/verify the founder's email + LinkedIn URL from that evidence.

    This is the LLM-driven alternative to enrich_contact() (Hunter/deterministic). It needs
    a search key (SERPER_API_KEY / BRAVE_API_KEY) and a usable LLM backend; both absent -> no-op.
    Emails found this way are UNVERIFIED (email_status='llm_search') - review before use.
    """
    if not llm.available():
        return
    for f in startup.founders:
        if f.email or not f.name:
            continue
        query = f'"{f.name}" {startup.name} founder email linkedin'
        results = _web_search(query, fetcher, num=8)
        if not results:
            continue

        lines: list[str] = []
        li_candidate = ""
        fetched = 0
        for title, url, snip in results:
            lines.append(f"- {title}\n  {url}\n  {snip}")
            if "linkedin.com/in/" in url.lower() and not li_candidate:
                li_candidate = url.split("?", 1)[0].rstrip("/")
            # Pull real text from a few non-social result pages (emails live there).
            if fetched < max_pages and not _is_social(url):
                res = fetcher.get(url)
                if res.ok and res.html:
                    lines.append("  PAGE TEXT: " + clean_text(res.html)[:1500])
                    fetched += 1

        data = llm.find_contact(f.name, startup.name, startup.url, "\n".join(lines))
        if not data:
            continue
        email = str(data.get("email") or "").strip()
        li = str(data.get("linkedin_url") or "").strip() or li_candidate
        if email and "@" in email and "." in email.rsplit("@", 1)[-1]:
            f.email, f.email_status, f.source = email, "llm_search", "llm_search"
        if li and "linkedin.com/in" in li.lower() and not f.linkedin_url:
            f.linkedin_url = li.split("?", 1)[0].rstrip("/")
