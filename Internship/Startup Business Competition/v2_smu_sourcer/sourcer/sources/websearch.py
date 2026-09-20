"""
Web-search source (Serper / Brave / Bing).

Turns a query like "university startup competition winners 2025" into candidate
startup URLs. Needs an API key; without one it returns nothing and prints how to
enable it, so the pipeline still runs. Set the query with `--source websearch:<query>`.

    export SERPER_API_KEY=...     # https://serper.dev  (Google results, JSON)
      or  export BRAVE_API_KEY=...  # https://brave.com/search/api/
"""
from __future__ import annotations

import os
from typing import Iterable
from urllib.parse import urlparse

from ..extract import registrable_domain
from ..models import Startup
from .base import Context

_SKIP = ("google.", "wikipedia.", "linkedin.", "youtube.", "facebook.",
         "twitter.", "x.com", "crunchbase.", "medium.", "reddit.")


class WebSearchSource:
    name = "websearch"

    def discover(self, ctx: Context) -> Iterable[Startup]:
        query = ctx.arg or "university startup competition finalists"
        results = self._search(ctx, query)
        if results is None:
            print("  [websearch] no API key set (SERPER_API_KEY or BRAVE_API_KEY) - "
                  "skipping. See sourcer/sources/websearch.py.")
            return []
        out: dict[str, Startup] = {}
        for title, url in results:
            host = urlparse(url).netloc.lower()
            if not host or any(s in host for s in _SKIP):
                continue
            dom = registrable_domain(host)
            if dom in out:
                continue
            out[dom] = Startup(name=title or dom, url=url, domain=dom,
                               source=self.name, source_detail=query)
            if ctx.limit and len(out) >= ctx.limit:
                break
        return list(out.values())

    def _search(self, ctx: Context, query: str) -> list[tuple[str, str]] | None:
        serper = os.environ.get("SERPER_API_KEY")
        brave = os.environ.get("BRAVE_API_KEY")
        if serper:
            import json
            import urllib.request
            req = urllib.request.Request(
                "https://google.serper.dev/search",
                data=json.dumps({"q": query, "num": 20}).encode(),
                headers={"X-API-KEY": serper, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=ctx.fetcher.timeout) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
            except Exception:
                return []
            return [(o.get("title", ""), o.get("link", ""))
                    for o in data.get("organic", []) if o.get("link")]
        if brave:
            from urllib.parse import quote
            url = f"https://api.search.brave.com/res/v1/web/search?q={quote(query)}&count=20"
            data = ctx.fetcher.get_json(url, headers={"X-Subscription-Token": brave})
            web = (data or {}).get("web", {}).get("results", []) if data else []
            return [(o.get("title", ""), o.get("url", "")) for o in web if o.get("url")]
        return None
