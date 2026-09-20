"""Write results to startups.csv (one row per founder) + startups.jsonl + report.json."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .models import Startup

FIELDS = [
    "startup_name", "url", "domain", "industry", "country", "status", "confidence",
    "founder_name", "founder_role", "founder_email", "email_status", "email_source",
    "founder_linkedin", "sourced_from", "source", "source_detail", "description", "notes",
]


def _rows(s: Startup):
    base = {
        "startup_name": s.name, "url": s.url, "domain": s.domain, "industry": s.industry,
        "country": s.country, "status": s.status, "confidence": round(s.confidence, 2),
        "sourced_from": s.source_org, "source": s.source, "source_detail": s.source_detail,
        "description": s.description, "notes": s.notes,
    }
    if not s.founders:
        yield {**base, "founder_name": "", "founder_role": "", "founder_email": "",
               "email_status": "", "email_source": "", "founder_linkedin": ""}
        return
    for f in s.founders:
        yield {**base, "founder_name": f.name, "founder_role": f.role,
               "founder_email": f.email, "email_status": f.email_status,
               "email_source": f.source, "founder_linkedin": f.linkedin_url}


def write_csv(results: list[Startup], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-SIG writes a BOM so Excel (esp. on a non-UTF-8 Windows locale) detects UTF-8
    # and shows accented founder names correctly instead of "?" / mojibake. Our own CSV
    # reader (read_csv_rows) strips the BOM, so re-feeding this file still works.
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for s in results:
            for row in _rows(s):
                w.writerow(row)
    return path


def write_jsonl(results: list[Startup], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for s in results:
            f.write(json.dumps(s.to_dict(), ensure_ascii=False) + "\n")
    return path


def write_report(results: list[Startup], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    enriched = [s for s in results if s.status == "enriched"]
    founders = [f for s in results for f in s.founders]
    report = {
        "startups": len(results),
        "enriched": len(enriched),
        "with_founder_name": sum(1 for s in results if any(f.name for f in s.founders)),
        "with_email": sum(1 for s in results if any(f.email for f in s.founders)),
        "founders_total": len(founders),
        "emails_verified": sum(1 for f in founders if f.email_status in ("scraped", "mx_ok")),
        "by_status": {st: sum(1 for s in results if s.status == st)
                      for st in sorted({s.status for s in results})},
        "by_source": {sc: sum(1 for s in results if s.source == sc)
                      for sc in sorted({s.source for s in results})},
    }
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path
