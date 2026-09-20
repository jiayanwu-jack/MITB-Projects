"""
Shared portfolio-scraping logic for the accelerator and university sources.

Both read a seed CSV of listing-page URLs (an accelerator's portfolio, or a
university's competition-winners / alumni-ventures page), fetch each, confirm it looks
like a company listing, and harvest the outbound links to individual startup sites.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from ..extract import (clean_text, find_external_links, looks_like_portfolio,
                       page_title, registrable_domain)
from ..models import Startup
from .base import Context, read_csv_rows


def scrape_listing(ctx: Context, seed_csv: str, source_name: str) -> list[Startup]:
    path = Path(ctx.arg) if ctx.arg else Path(ctx.data_dir) / seed_csv
    if not path.exists():
        return []

    rows = read_csv_rows(path)

    out: dict[str, Startup] = {}
    for row in rows:
        listing_url = (row.get("url") or row.get("portal_url") or "").strip()
        if not listing_url:
            continue
        origin = (row.get("name") or urlparse(listing_url).netloc).strip()
        country = (row.get("country") or "").strip()

        res = ctx.fetcher.get(listing_url)
        if not res.ok or not res.html:
            continue
        title, text = page_title(res.html), clean_text(res.html)
        if not looks_like_portfolio(res.url, title, text):
            # Not obviously a listing page - still harvest, but it may be noisy.
            pass
        for name, url in find_external_links(res.html, res.url):
            dom = registrable_domain(urlparse(url).netloc)
            if not dom or dom in out:
                continue
            out[dom] = Startup(
                name=name, url=url, domain=dom, country=country,
                source=source_name, source_org=origin, source_detail=listing_url,
            )
            if ctx.limit and len(out) >= ctx.limit:
                return list(out.values())
    return list(out.values())
