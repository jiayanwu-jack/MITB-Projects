from __future__ import annotations

import re
from urllib.parse import urlparse

from rapidfuzz.fuzz import ratio


def normalize_name(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"\b(ltd|limited|inc|incorporated|llc|pte|plc|company|co)\b", "", name)
    return re.sub(r"[^a-z0-9]+", "", name)


def website_domain(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    domain = urlparse(url).netloc.lower()
    return domain.removeprefix("www.")


def deduplicate(candidates: list[dict]) -> list[dict]:
    kept: list[dict] = []
    for candidate in sorted(candidates, key=lambda x: x.get("priority_score", 0), reverse=True):
        n1 = normalize_name(candidate.get("startup_name", ""))
        d1 = website_domain(candidate.get("website", ""))
        duplicate_index = None
        for i, existing in enumerate(kept):
            n2 = normalize_name(existing.get("startup_name", ""))
            d2 = website_domain(existing.get("website", ""))
            same_domain = bool(d1 and d2 and d1 == d2)
            near_name = bool(n1 and n2 and ratio(n1, n2) >= 92)
            if same_domain or near_name:
                duplicate_index = i
                break
        if duplicate_index is None:
            candidate["duplicate_of"] = ""
            kept.append(candidate)
        else:
            existing = kept[duplicate_index]
            # Merge useful fields and provenance into the retained row. A lower-ranked
            # duplicate can still contain the only startup website, founder name, or email.
            for field in (
                "website", "website_discovery_method", "website_discovery_confidence",
                "contact_email",
            ):
                if not existing.get(field) and candidate.get(field):
                    existing[field] = candidate[field]

            existing_founders = existing.get("founders", []) or []
            incoming_founders = candidate.get("founders", []) or []
            known = {
                str(f.get("name", "")).strip().lower()
                for f in existing_founders
                if isinstance(f, dict) and f.get("name")
            }
            for founder in incoming_founders:
                if not isinstance(founder, dict):
                    continue
                key = str(founder.get("name", "")).strip().lower()
                if key and key not in known:
                    existing_founders.append(founder)
                    known.add(key)
            existing["founders"] = existing_founders

            urls = set(existing.get("source_urls", [])) | set(candidate.get("source_urls", []))
            existing["source_urls"] = sorted(u for u in urls if u)
    return kept
