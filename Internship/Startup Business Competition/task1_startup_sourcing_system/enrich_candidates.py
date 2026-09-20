from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from startup_sourcing.config import settings
from startup_sourcing.crawler import Crawl4AICrawler, FirecrawlCrawler
from startup_sourcing.exporter import write_csv, write_json
from startup_sourcing.founder_records import build_founder_rows
from startup_sourcing.pipeline import (
    CRAWLER_BACKENDS,
    EMAIL_PROVIDERS,
    PEOPLE_PROVIDERS,
    SEARCH_BACKENDS,
    build_contact_waterfall,
)
from startup_sourcing.provider_enrichment import provider_call_totals

JSON_COLUMNS = {
    "founders", "source_urls", "evidence_quotes", "contact_evidence",
    "enrichment_urls_attempted", "contact_enrichment_errors", "all_public_emails",
    "search_queries_run", "search_enrichment_errors", "provider_calls",
    "provider_enrichment_errors", "apollo_waterfall_request_ids", "contact_methods_attempted",
}


def _parse_json_cell(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, (list, dict)):
        return value
    text = str(value).strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


def load_candidates(path: str | Path) -> list[dict]:
    df = pd.read_csv(path)
    rows = df.where(pd.notna(df), "").to_dict("records")
    for row in rows:
        for col in JSON_COLUMNS:
            if col in row:
                row[col] = _parse_json_cell(row[col])
    return rows


def build_crawler(name: str):
    if name == "crawl4ai":
        return Crawl4AICrawler(settings)
    if name == "firecrawl":
        return FirecrawlCrawler(settings)
    raise ValueError(f"Unsupported crawler backend: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-enrich an existing candidates_ranked.csv without re-running extraction")
    parser.add_argument("--input-csv", required=True)
    parser.add_argument("--output-dir", default="outputs/reenriched")
    parser.add_argument("--crawler-backend", choices=CRAWLER_BACKENDS, default=settings.crawler_backend)
    parser.add_argument("--contact-max-pages", type=int, default=settings.contact_max_pages)
    parser.add_argument("--search-backend", choices=SEARCH_BACKENDS, default=settings.search_backend)
    parser.add_argument("--search-max-results", type=int, default=settings.search_max_results)
    parser.add_argument("--search-max-pages-per-candidate", type=int, default=settings.search_max_pages_per_candidate)
    parser.add_argument("--search-queries-per-founder", type=int, default=settings.search_queries_per_founder)
    parser.add_argument("--email-provider", choices=EMAIL_PROVIDERS, default=settings.email_provider)
    parser.add_argument("--people-provider", choices=PEOPLE_PROVIDERS, default=settings.people_provider)
    parser.add_argument("--no-founder-search", action="store_true")
    parser.add_argument("--no-pdf-search", action="store_true")
    parser.add_argument("--verify-emails", action="store_true", default=settings.verify_emails)
    parser.add_argument("--no-paid-fallback-only", action="store_true")
    parser.add_argument("--apollo-reveal-personal-emails", action="store_true", default=settings.apollo_reveal_personal_emails)
    parser.add_argument("--apollo-run-waterfall-email", action="store_true", default=settings.apollo_run_waterfall_email)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates = load_candidates(args.input_csv)
    crawler = build_crawler(args.crawler_backend)
    waterfall, search_enricher, provider_enricher = build_contact_waterfall(
        crawler=crawler,
        settings=settings,
        search_backend=args.search_backend,
        email_provider=args.email_provider,
        people_provider=args.people_provider,
        contact_max_pages=args.contact_max_pages,
        search_max_results=args.search_max_results,
        search_max_pages_per_candidate=args.search_max_pages_per_candidate,
        search_queries_per_founder=args.search_queries_per_founder,
        enable_founder_search=not args.no_founder_search,
        enable_pdf_search=not args.no_pdf_search,
        verify_emails=args.verify_emails,
        paid_fallback_only=not args.no_paid_fallback_only,
        apollo_reveal_personal_emails=args.apollo_reveal_personal_emails,
        apollo_run_waterfall_email=args.apollo_run_waterfall_email,
        progress=print,
    )

    enriched = []
    for idx, candidate in enumerate(candidates, start=1):
        print(f"Candidate {idx}/{len(candidates)}: {candidate.get('startup_name', '')}")
        enriched.append(waterfall.enrich(candidate))

    founder_rows = build_founder_rows(enriched)
    evidence_rows = []
    for candidate in enriched:
        for evidence in candidate.get("contact_evidence", []) or []:
            evidence_rows.append({
                "startup_name": candidate.get("startup_name", ""),
                "website": candidate.get("website", ""),
                "official_domain": candidate.get("official_domain", ""),
                **evidence,
            })

    write_csv(output_dir / "candidates_ranked_enriched.csv", enriched)
    write_csv(output_dir / "founders.csv", founder_rows)
    write_csv(output_dir / "contact_evidence.csv", evidence_rows)
    summary = {
        "candidates": len(enriched),
        "candidates_with_any_contact": sum(bool(c.get("contact_email")) for c in enriched),
        "founder_emails_found": sum(bool(c.get("founder_email")) for c in enriched),
        "company_emails_found": sum(bool(c.get("company_email")) for c in enriched),
        "founder_records": len(founder_rows),
        "founder_records_with_email": sum(bool(r.get("email")) for r in founder_rows),
        "search_api_calls": getattr(getattr(search_enricher, "search_client", None), "calls", 0) if search_enricher else 0,
        **provider_call_totals(provider_enricher),
    }
    write_json(output_dir / "reenrichment_summary.json", summary)
    print("\nRe-enrichment summary")
    for key, value in summary.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
