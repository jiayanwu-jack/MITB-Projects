"""
Startup-news source.

Finds recently-funded / newly-launched startups from news search, then keeps the
articles' outbound company links. Uses the same search backend as the websearch
source (Serper/Brave) but with news-flavoured queries; needs an API key. Without a
key it returns nothing and explains how to enable it.

Set the query with `--source news:<query>` (default targets student/university startups).
"""
from __future__ import annotations

import os
from typing import Iterable

from ..models import Startup
from .base import Context
from .websearch import WebSearchSource


class NewsSource:
    name = "news"

    def discover(self, ctx: Context) -> Iterable[Startup]:
        if not (os.environ.get("SERPER_API_KEY") or os.environ.get("BRAVE_API_KEY")):
            print("  [news] no API key set (SERPER_API_KEY or BRAVE_API_KEY) - skipping. "
                  "See sourcer/sources/news.py.")
            return []
        query = ctx.arg or ("university student startup raises seed funding 2025 "
                            "OR launches OR wins competition")
        # Reuse the web-search backend; tag the results as coming from `news`.
        ctx2 = Context(fetcher=ctx.fetcher, limit=ctx.limit,
                       data_dir=ctx.data_dir, arg=query)
        results = list(WebSearchSource().discover(ctx2))
        for s in results:
            s.source = self.name
        return results
