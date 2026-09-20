"""
Per-target discovery: a bounded, polite, same-site crawl.

From the target's start URL it does a best-first walk (max_pages / max_depth), always
preferring office-looking links (entrepreneurship/innovation/incubator) then contact
links. It collects candidate emails from every page visited and stops early once it has
a strong role-inbox contact, to minimise load on the site.

Along the way it also watches for portfolio-style pages (portfolio, alumni ventures,
competition winners) and collects the external startup sites they link out to as
StartupLeads - e.g. a university's "Startups we support" page, or an accelerator's
portfolio page. These are a free by-product of the same crawl, not a separate pass.

Returns (candidates, pages_crawled, last_reason, startup_leads).
"""
from __future__ import annotations

from urllib.parse import urlparse

from .extract import (clean_text, extract_emails, find_links, find_portfolio_links,
                      is_role_inbox, looks_like_office_page, looks_like_portfolio_page,
                      page_title)
from .fetch import Fetcher
from .models import Candidate, StartupLead, Target


def discover(target: Target, fetcher: Fetcher, *, max_pages: int = 6,
             max_depth: int = 2) -> tuple[list[Candidate], int, str, list[StartupLead]]:
    start = target.url
    queue: list[tuple[str, int]] = [(start, 0)]
    seen: set[str] = set()
    candidates: dict[str, Candidate] = {}
    leads: dict[str, StartupLead] = {}
    pages = 0
    last_reason = "no pages fetched"

    while queue and pages < max_pages:
        # best-first: the queue is kept sorted by (depth asc); links are pre-scored.
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)

        res = fetcher.get(url)
        last_reason = res.reason
        if not res.ok or not res.html:
            continue
        pages += 1

        title = page_title(res.html)
        text = clean_text(res.html)
        office = looks_like_office_page(res.url, title, text)

        for c in extract_emails(res.html, res.url, title, office):
            candidates.setdefault(c.email.lower(), c)

        if looks_like_portfolio_page(res.url, title, text):
            for name, link_url in find_portfolio_links(res.html, res.url):
                leads.setdefault(link_url, StartupLead(
                    name=name, url=link_url, source_target_id=target.id,
                    source_target_name=target.name, source_page_url=res.url))

        # Early stop: a domain-agnostic role inbox on an office page is good enough.
        if any(is_role_inbox(c.email) and c.office_page for c in candidates.values()):
            break

        if depth < max_depth:
            links = [u for _, u in find_links(res.html, res.url) if u not in seen]
            # Enqueue a bounded number of the highest-scoring links, deeper last.
            for link in links[:5]:
                queue.append((link, depth + 1))
            # Keep breadth-first-ish ordering: shallower first.
            queue.sort(key=lambda t: t[1])

    return list(candidates.values()), pages, last_reason, list(leads.values())
