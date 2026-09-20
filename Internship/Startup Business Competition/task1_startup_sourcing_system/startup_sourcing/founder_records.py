from __future__ import annotations


def build_founder_rows(candidates: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for candidate in candidates:
        startup = candidate.get("startup_name", "")
        evidence = candidate.get("contact_evidence", []) or []
        for founder in candidate.get("founders", []) or []:
            if isinstance(founder, dict):
                f = founder
            else:
                f = founder.model_dump() if hasattr(founder, "model_dump") else {}
            name = str(f.get("name", "")).strip()
            if not name:
                continue
            matched = [
                e for e in evidence
                if str(e.get("founder_match", "")).strip().lower() == name.lower()
            ]
            matched.sort(
                key=lambda e: (
                    1 if e.get("verification_status") == "valid" else 0,
                    float(e.get("confidence", 0) or 0),
                    float(e.get("verification_score", 0) or 0),
                ),
                reverse=True,
            )
            best = matched[0] if matched else {}
            email = best.get("email", "")
            email_type = best.get("email_type", "")
            provider = best.get("provider", "")
            origin = best.get("origin", "")
            source_url = best.get("source_url", "")
            # Fallback: when the enrichment waterfall produced no verified email
            # for this founder, surface any contact email the extractor already
            # scraped from the page. This lets 'mock' runs (which skip
            # enrichment) exercise the outreach flow end-to-end, and fills
            # otherwise-blank rows in live runs. Tagged origin='page_extract'.
            if not email:
                page_email = str(
                    candidate.get("founder_email", "") or candidate.get("contact_email", "")
                ).strip()
                if page_email:
                    email = page_email
                    email_type = "personal" if candidate.get("founder_email") else "generic"
                    provider = provider or "page"
                    origin = "page_extract"
                    source_url = source_url or candidate.get("website", "")
            rows.append(
                {
                    "startup_name": startup,
                    "website": candidate.get("website", ""),
                    "official_domain": candidate.get("official_domain", ""),
                    "industry": candidate.get("industry", ""),
                    "country": candidate.get("country", ""),
                    "founder_name": name,
                    "university": f.get("university", ""),
                    "founder_status": f.get("status", "unknown"),
                    "graduation_year": f.get("graduation_year", ""),
                    "eligibility_evidence": f.get("evidence", ""),
                    "linkedin_url": f.get("linkedin_url", "") or candidate.get("founder_linkedin_url", ""),
                    "email": email,
                    "email_type": email_type,
                    "email_confidence": best.get("confidence", ""),
                    "verification_status": best.get("verification_status", ""),
                    "verification_score": best.get("verification_score", ""),
                    "provider": provider,
                    "origin": origin,
                    "email_source_url": source_url,
                    "startup_eligibility_status": candidate.get("eligibility_status", ""),
                    "priority_score": candidate.get("priority_score", 0),
                }
            )
    return rows
