"""
CSV source: read a plain list of startups you already have.

Always works offline - point it at any CSV with at least a `url` (or `portal_url`)
column; `name`, `country`, `industry` are used if present. This is the fallback when
you just want to enrich a hand-made list.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from ..extract import registrable_domain
from ..models import Startup
from .base import Context, read_csv_rows


class CsvSource:
    name = "csv"

    def discover(self, ctx: Context) -> Iterable[Startup]:
        path = Path(ctx.arg or (Path(ctx.data_dir) / "startups.csv"))
        if not path.exists():
            return []
        out: list[Startup] = []
        rows = read_csv_rows(path)
        cols = list(rows[0].keys()) if rows else []
        url_col = "url" if "url" in cols else "portal_url"
        for r in rows:
            url = (r.get(url_col) or "").strip()
            if not url:
                continue
            out.append(Startup(
                name=(r.get("name") or url).strip(),
                url=url,
                domain=registrable_domain(urlparse(url).netloc),
                country=(r.get("country") or "").strip(),
                industry=(r.get("industry") or "").strip(),
                source=self.name, source_detail=str(path),
            ))
            if ctx.limit and len(out) >= ctx.limit:
                break
        return out
