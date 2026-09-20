"""
Tracxn source (paid startup database) - API scaffold, add your key later.

Tracxn exposes a company-search API. This source is wired up but disabled until you
set a key; when TRACXN_API_KEY is present it calls the endpoint, maps each company to
a Startup (Tracxn already gives name / website / sector, so these arrive well-populated
and often need no page enrichment), and respects `--limit`.

    export TRACXN_API_KEY=...
    # optional overrides if your plan's endpoint/shape differs:
    export TRACXN_API_URL=https://platform.tracxn.com/api/2.2/companies

Because API response shapes vary by plan, the field mapping in `_map` is deliberately
defensive and easy to adjust once you can see a real response. Pass a search filter
with `--source tracxn:<query>`.
"""
from __future__ import annotations

import json
import os
from typing import Iterable
from urllib.parse import urlparse

from ..extract import registrable_domain
from ..models import Founder, Startup
from .base import Context

_DEFAULT_URL = "https://platform.tracxn.com/api/2.2/companies"


class TracxnSource:
    name = "tracxn"

    def discover(self, ctx: Context) -> Iterable[Startup]:
        key = os.environ.get("TRACXN_API_KEY")
        if not key:
            print("  [tracxn] TRACXN_API_KEY not set - skipping. Add your key to enable "
                  "the paid-database source (see sourcer/sources/tracxn.py).")
            return []
        url = os.environ.get("TRACXN_API_URL", _DEFAULT_URL)
        body = {"filter": {"keyword": [ctx.arg or "student startup"]},
                "size": ctx.limit or 25}
        data = self._post(ctx, url, key, body)
        if not data:
            return []
        rows = data.get("result") or data.get("companies") or data.get("data") or []
        out: list[Startup] = []
        for row in rows:
            s = self._map(row)
            if s:
                out.append(s)
            if ctx.limit and len(out) >= ctx.limit:
                break
        return out

    def _post(self, ctx: Context, url: str, key: str, body: dict) -> dict | None:
        import urllib.request
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"accessToken": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=ctx.fetcher.timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            print(f"  [tracxn] request failed: {e!r}")
            return None

    def _map(self, row: dict) -> Startup | None:
        name = row.get("name") or row.get("companyName") or ""
        website = (row.get("website") or row.get("domain") or row.get("url") or "").strip()
        if website and not website.startswith("http"):
            website = "https://" + website
        if not (name or website):
            return None
        dom = registrable_domain(urlparse(website).netloc) if website else ""
        s = Startup(
            name=name or dom, url=website, domain=dom,
            industry=row.get("sector") or row.get("industry") or "",
            country=row.get("country") or row.get("location") or "",
            description=row.get("description") or "",
            source=self.name, source_org="Tracxn", source_detail="tracxn-api",
            confidence=0.6,
        )
        for f in row.get("founders") or row.get("people") or []:
            fname = f.get("name") if isinstance(f, dict) else str(f)
            if fname:
                s.founders.append(Founder(
                    name=fname, role=(f.get("role") if isinstance(f, dict) else "") or "Founder"))
        return s
