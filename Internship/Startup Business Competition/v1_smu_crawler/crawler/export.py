"""Write crawl results to contacts.csv + contacts.jsonl + a run report."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .models import Result, StartupLead

FIELDS = [
    "target_kind", "target_id", "target_name", "country", "status",
    "contact_email", "contact_name", "contact_role", "role_inbox", "confidence",
    "needs_review", "page_found_url", "evidence", "pages_crawled", "error",
]

# Same shape as targets/startups.csv (id,name,...,org_type,...,portal_url,...) plus
# provenance columns, so this file can be reviewed, trimmed, and fed straight back in
# via --targets.
LEAD_FIELDS = [
    "id", "name", "country", "org_type", "startup_focus", "portal_url", "verified",
    "source_target_name", "source_page_url",
]


def write_csv(results: list[Result], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in results:
            d = r.to_dict()
            w.writerow({k: d[k] for k in FIELDS})
    return path


def write_jsonl(results: list[Result], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")
    return path


def write_startup_leads(leads: list[StartupLead], path: str | Path) -> Path:
    """Write external startup sites discovered on portfolio-style pages, in the same
    CSV shape as targets/startups.csv, ready to review and re-run with --targets."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LEAD_FIELDS)
        w.writeheader()
        for i, lead in enumerate(leads, 1):
            w.writerow({
                "id": f"lead{i:04d}", "name": lead.name, "country": "",
                "org_type": "startup", "startup_focus": "", "portal_url": lead.url,
                "verified": "false", "source_target_name": lead.source_target_name,
                "source_page_url": lead.source_page_url,
            })
    return path


def write_report(results: list[Result], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok = [r for r in results if r.status == "ok"]
    report = {
        "targets": len(results),
        "contacts_found": len(ok),
        "ready_to_send": sum(1 for r in ok if not r.needs_review),
        "needs_review": sum(1 for r in ok if r.needs_review),
        "role_inboxes": sum(1 for r in ok if r.role_inbox),
        "by_status": {s: sum(1 for r in results if r.status == s)
                      for s in sorted({r.status for r in results})},
    }
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path
