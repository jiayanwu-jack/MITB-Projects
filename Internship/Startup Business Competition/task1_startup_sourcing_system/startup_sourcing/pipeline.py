from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .contact_enrichment import WebsiteContactEnricher
from .contact_waterfall import ContactWaterfall
from .crawler import Crawl4AICrawler, FirecrawlCrawler, MockCrawler
from .dedupe import deduplicate
from .eligibility import compute_priority_score, enforce_eligibility
from .exporter import write_csv, write_json, write_jsonl
from .extractor import (
    EMPTY_RESULT_WARN_CHARS,
    ClaudeExtractor,
    DeepSeekExtractor,
    GoogleAIStudioExtractor,
    MockExtractor,
    OllamaExtractor,
    OpenAIExtractor,
)
from .founder_records import build_founder_rows
from .outreach import (
    ClaudeEmailDrafter,
    DeepSeekEmailDrafter,
    GoogleEmailDrafter,
    MockEmailDrafter,
    OllamaEmailDrafter,
    OpenAIEmailDrafter,
)
from .provider_enrichment import ApolloClient, HunterClient, PaidProviderEnricher, provider_call_totals
from .search_enrichment import BraveSearchClient, SearchContactEnricher, TavilySearchClient
from .seed_loader import load_seed_sources
from .website_discovery import canonicalize_website, discover_startup_website, is_probable_startup_website


Progress = Callable[[str], None]
StopCheck = Callable[[], bool]
CRAWLER_BACKENDS = ("firecrawl", "crawl4ai")
LLM_BACKENDS = ("openai", "ollama", "google", "claude", "deepseek")
SEARCH_BACKENDS = ("none", "brave", "tavily")
EMAIL_PROVIDERS = ("none", "hunter")
PEOPLE_PROVIDERS = ("none", "apollo")


def _noop(_: str) -> None:
    pass


def _never_stop() -> bool:
    return False


JSON_CANDIDATE_FIELDS = {
    "founders",
    "evidence_quotes",
    "source_urls",
    "contact_evidence",
    "enrichment_urls_attempted",
    "contact_enrichment_errors",
    "all_public_emails",
    "contact_methods_attempted",
    "search_queries_run",
    "search_enrichment_errors",
    "provider_calls",
    "provider_enrichment_errors",
    "apollo_waterfall_request_ids",
}


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    value = value.strip()
    if not value:
        return ""
    if value[0] in "[{":
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def normalize_candidate_row(row: dict[str, Any]) -> dict[str, Any]:
    candidate = {key: _csv_cell(value) for key, value in row.items()}
    for field in JSON_CANDIDATE_FIELDS:
        value = candidate.get(field)
        if value in (None, ""):
            candidate[field] = []
        elif field in JSON_CANDIDATE_FIELDS and isinstance(value, str) and value[:1] in "[{":
            try:
                candidate[field] = json.loads(value)
            except json.JSONDecodeError:
                candidate[field] = []
        elif field != "contact_evidence" and not isinstance(value, list):
            candidate[field] = [value]
        elif field == "contact_evidence" and not isinstance(value, list):
            candidate[field] = []
    if not candidate.get("founders") and candidate.get("founder_name"):
        candidate["founders"] = [{
            "name": candidate.get("founder_name", ""),
            "university": candidate.get("university", ""),
            "status": candidate.get("founder_status", "unknown"),
            "graduation_year": candidate.get("graduation_year", ""),
            "evidence": candidate.get("eligibility_evidence", ""),
            "linkedin_url": candidate.get("linkedin_url", "") or candidate.get("founder_linkedin_url", ""),
        }]
    if candidate.get("email") and not candidate.get("founder_email"):
        candidate["founder_email"] = candidate.get("email")
    if candidate.get("email") and not candidate.get("contact_email"):
        candidate["contact_email"] = candidate.get("email")
    if candidate.get("email") and not candidate.get("contact_evidence"):
        candidate["contact_evidence"] = [{
            "email": candidate.get("email", ""),
            "email_type": candidate.get("email_type", ""),
            "confidence": candidate.get("email_confidence", ""),
            "verification_status": candidate.get("verification_status", ""),
            "verification_score": candidate.get("verification_score", ""),
            "provider": candidate.get("provider", ""),
            "origin": candidate.get("origin", ""),
            "source_url": candidate.get("email_source_url", ""),
            "founder_match": candidate.get("founder_name", ""),
        }]
    if candidate.get("linkedin_url") and not candidate.get("founder_linkedin_url"):
        candidate["founder_linkedin_url"] = candidate.get("linkedin_url")
    if candidate.get("startup_eligibility_status") and not candidate.get("eligibility_status"):
        candidate["eligibility_status"] = candidate.get("startup_eligibility_status")
    return candidate


def load_candidates_csv(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Candidate file not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [normalize_candidate_row(row) for row in csv.DictReader(f)]


def build_crawler(
    mode: str,
    settings: Settings,
    crawler_backend: str | None = None,
):
    if mode == "mock":
        return MockCrawler()
    if mode != "live":
        raise ValueError("mode must be 'mock' or 'live'")

    crawler_name = (crawler_backend or settings.crawler_backend).lower()
    if crawler_name == "firecrawl":
        return FirecrawlCrawler(settings)
    if crawler_name == "crawl4ai":
        return Crawl4AICrawler(settings)
    raise ValueError(f"crawler_backend must be one of {CRAWLER_BACKENDS}")


def build_components(
    mode: str,
    settings: Settings,
    crawler_backend: str | None = None,
    llm_backend: str | None = None,
    ollama_model: str | None = None,
    google_model: str | None = None,
    claude_model: str | None = None,
    deepseek_model: str | None = None,
):
    if mode == "mock":
        return MockCrawler(), MockExtractor(settings), MockEmailDrafter(settings)
    if mode != "live":
        raise ValueError("mode must be 'mock' or 'live'")

    crawler_name = (crawler_backend or settings.crawler_backend).lower()
    llm_name = (llm_backend or settings.llm_backend).lower()

    crawler = build_crawler(mode, settings, crawler_name)

    if llm_name == "openai":
        extractor = OpenAIExtractor(settings)
        drafter = OpenAIEmailDrafter(settings)
    elif llm_name == "ollama":
        extractor = OllamaExtractor(settings, model=ollama_model)
        drafter = OllamaEmailDrafter(settings, model=ollama_model)
    elif llm_name == "google":
        extractor = GoogleAIStudioExtractor(settings, model=google_model)
        drafter = GoogleEmailDrafter(settings, model=google_model)
    elif llm_name == "claude":
        extractor = ClaudeExtractor(settings, model=claude_model)
        drafter = ClaudeEmailDrafter(settings, model=claude_model)
    elif llm_name == "deepseek":
        extractor = DeepSeekExtractor(settings, model=deepseek_model)
        drafter = DeepSeekEmailDrafter(settings, model=deepseek_model)
    else:
        raise ValueError(f"llm_backend must be one of {LLM_BACKENDS}")

    return crawler, extractor, drafter


def build_contact_waterfall(
    *,
    crawler,
    settings: Settings,
    search_backend: str,
    email_provider: str,
    people_provider: str,
    contact_max_pages: int,
    search_max_results: int,
    search_max_pages_per_candidate: int,
    search_queries_per_founder: int,
    enable_founder_search: bool,
    enable_pdf_search: bool,
    verify_emails: bool,
    paid_fallback_only: bool,
    apollo_reveal_personal_emails: bool,
    apollo_run_waterfall_email: bool,
    progress: Progress,
    enrichment_scope: str = "all",
):
    website = WebsiteContactEnricher(
        crawler,
        max_pages=contact_max_pages,
        progress=progress,
        enrichment_scope=enrichment_scope,
    )

    search_enricher = None
    if search_backend in ("brave", "tavily"):
        if search_backend == "tavily":
            search_client = TavilySearchClient(settings.tavily_api_key or "", max_results=search_max_results)
        else:
            search_client = BraveSearchClient(settings.brave_search_api_key or "", max_results=search_max_results)
        search_enricher = SearchContactEnricher(
            search_client,
            crawler,
            max_results=search_max_results,
            max_pages_per_candidate=search_max_pages_per_candidate,
            queries_per_founder=search_queries_per_founder,
            enable_founder_search=enable_founder_search,
            enable_pdf_search=enable_pdf_search,
            enrichment_scope=enrichment_scope,
            progress=progress,
        )
    elif search_backend != "none":
        raise ValueError(f"search_backend must be one of {SEARCH_BACKENDS}")

    hunter = None
    apollo = None
    if email_provider == "hunter" or verify_emails:
        hunter = HunterClient(
            settings.hunter_api_key or "",
            request_delay_seconds=settings.hunter_request_delay_seconds,
            retry_attempts=settings.hunter_retry_attempts,
            retry_backoff_seconds=settings.hunter_retry_backoff_seconds,
        )
    elif email_provider != "none":
        raise ValueError(f"email_provider must be one of {EMAIL_PROVIDERS}")

    if people_provider == "apollo":
        apollo = ApolloClient(
            settings.apollo_api_key or "",
            poll_attempts=settings.apollo_poll_attempts,
            poll_delay_seconds=settings.apollo_poll_delay_seconds,
        )
    elif people_provider != "none":
        raise ValueError(f"people_provider must be one of {PEOPLE_PROVIDERS}")

    provider_enricher = None
    if hunter or apollo:
        provider_enricher = PaidProviderEnricher(
            hunter=hunter,
            apollo=apollo,
            verify_emails=verify_emails,
            fallback_only=paid_fallback_only,
            apollo_reveal_personal_emails=apollo_reveal_personal_emails,
            apollo_run_waterfall_email=apollo_run_waterfall_email,
            enrichment_scope=enrichment_scope,
            progress=progress,
        )

    waterfall = ContactWaterfall(
        website_enricher=website,
        search_enricher=search_enricher,
        provider_enricher=provider_enricher,
        paid_fallback_only=paid_fallback_only,
        enrichment_scope=enrichment_scope,
        progress=progress,
    )
    return waterfall, search_enricher, provider_enricher


def enrich_candidates_with_contacts(
    candidates: list[dict[str, Any]],
    output_dir: str | Path,
    settings: Settings,
    mode: str = "live",
    crawler_backend: str | None = None,
    contact_max_pages: int | None = None,
    search_backend: str | None = None,
    email_provider: str | None = None,
    people_provider: str | None = None,
    search_max_results: int | None = None,
    search_max_pages_per_candidate: int | None = None,
    search_queries_per_founder: int | None = None,
    enable_founder_search: bool | None = None,
    enable_pdf_search: bool | None = None,
    verify_emails: bool | None = None,
    paid_fallback_only: bool | None = None,
    apollo_reveal_personal_emails: bool | None = None,
    apollo_run_waterfall_email: bool | None = None,
    enrichment_scope: str = "all",
    progress: Progress = _noop,
) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    crawler_name = "mock" if mode == "mock" else (crawler_backend or settings.crawler_backend).lower()
    search_name = "none" if mode == "mock" else (search_backend or settings.search_backend).lower()
    email_provider_name = "none" if mode == "mock" else (email_provider or settings.email_provider).lower()
    people_provider_name = "none" if mode == "mock" else (people_provider or settings.people_provider).lower()

    effective_contact_max_pages = contact_max_pages or settings.contact_max_pages
    effective_search_max_results = search_max_results or settings.search_max_results
    effective_search_max_pages = search_max_pages_per_candidate or settings.search_max_pages_per_candidate
    effective_queries_per_founder = search_queries_per_founder or settings.search_queries_per_founder
    effective_founder_search = settings.enable_founder_search if enable_founder_search is None else enable_founder_search
    effective_pdf_search = settings.enable_pdf_search if enable_pdf_search is None else enable_pdf_search
    effective_verify = settings.verify_emails if verify_emails is None else verify_emails
    effective_paid_fallback = settings.paid_fallback_only if paid_fallback_only is None else paid_fallback_only
    effective_apollo_personal = settings.apollo_reveal_personal_emails if apollo_reveal_personal_emails is None else apollo_reveal_personal_emails
    effective_apollo_waterfall = settings.apollo_run_waterfall_email if apollo_run_waterfall_email is None else apollo_run_waterfall_email

    ranked = [normalize_candidate_row(candidate) for candidate in candidates]
    errors: list[dict] = []
    contacts_before = sum(bool(c.get("contact_email")) for c in ranked)

    crawler = build_crawler(mode, settings, crawler_name if mode == "live" else None)
    progress(
        f"Contact waterfall backends: crawler={crawler_name}, search={search_name}, "
        f"email_provider={email_provider_name}, people_provider={people_provider_name}"
    )
    waterfall, search_enricher, provider_enricher = build_contact_waterfall(
        crawler=crawler,
        settings=settings,
        search_backend=search_name,
        email_provider=email_provider_name,
        people_provider=people_provider_name,
        contact_max_pages=effective_contact_max_pages,
        search_max_results=effective_search_max_results,
        search_max_pages_per_candidate=effective_search_max_pages,
        search_queries_per_founder=effective_queries_per_founder,
        enable_founder_search=effective_founder_search,
        enable_pdf_search=effective_pdf_search,
        verify_emails=effective_verify,
        paid_fallback_only=effective_paid_fallback,
        apollo_reveal_personal_emails=effective_apollo_personal,
        apollo_run_waterfall_email=effective_apollo_waterfall,
        enrichment_scope=enrichment_scope,
        progress=progress,
    )

    enriched: list[dict] = []
    for idx, candidate in enumerate(ranked, start=1):
        progress(f"Contact waterfall {idx}/{len(ranked)}: {candidate.get('startup_name', '')}")
        try:
            enriched.append(waterfall.enrich(candidate))
        except Exception as exc:
            candidate["contact_waterfall_status"] = "waterfall_error"
            candidate.setdefault("contact_enrichment_errors", []).append({"stage": "waterfall", "error": str(exc)})
            errors.append({
                "source_id": candidate.get("source_id", ""),
                "startup_name": candidate.get("startup_name", ""),
                "stage": "contact_waterfall",
                "error": str(exc),
            })
            enriched.append(candidate)
    ranked = enriched
    contacts_after = sum(bool(c.get("contact_email")) for c in ranked)

    for i, candidate in enumerate(ranked, start=1):
        candidate["rank"] = i
        candidate.setdefault("review_decision", "Pending")

    founder_rows = build_founder_rows(ranked)
    contact_evidence_rows: list[dict] = []
    for candidate in ranked:
        for evidence in candidate.get("contact_evidence", []) or []:
            contact_evidence_rows.append({
                "startup_name": candidate.get("startup_name", ""),
                "website": candidate.get("website", ""),
                "official_domain": candidate.get("official_domain", ""),
                **evidence,
            })

    search_evidence_rows: list[dict] = []
    for candidate in ranked:
        for query in candidate.get("search_queries_run", []) or []:
            search_evidence_rows.append({
                "startup_name": candidate.get("startup_name", ""),
                "query": query,
                "search_backend": search_name,
            })

    write_csv(output_dir / "candidates_enriched.csv", ranked)
    write_csv(output_dir / "founders.csv", founder_rows)
    write_csv(output_dir / "contact_evidence.csv", contact_evidence_rows)
    write_csv(output_dir / "search_queries.csv", search_evidence_rows)
    write_csv(output_dir / "errors.csv", errors)

    provider_totals = provider_call_totals(provider_enricher)
    search_calls = getattr(getattr(search_enricher, "search_client", None), "calls", 0) if search_enricher else 0
    verified_valid = sum(
        any(e.get("verification_status") == "valid" for e in c.get("contact_evidence", []) or [])
        for c in ranked
    )
    summary = {
        "mode": mode,
        "enrichment_scope": enrichment_scope,
        "crawler_backend": crawler_name,
        "search_backend": search_name,
        "email_provider": email_provider_name,
        "people_provider": people_provider_name,
        "candidates_uploaded": len(ranked),
        "contacts_enriched": max(0, contacts_after - contacts_before),
        "candidates_with_any_contact": sum(bool(c.get("contact_email")) for c in ranked),
        "founder_emails_found": sum(bool(c.get("founder_email")) for c in ranked),
        "company_emails_found": sum(bool(c.get("company_email")) for c in ranked),
        "verified_valid_candidates": verified_valid,
        "startup_websites_available": sum(bool(c.get("website")) for c in ranked),
        "founder_records": len(founder_rows),
        "founder_records_with_email": sum(bool(r.get("email")) for r in founder_rows),
        "contact_evidence_rows": len(contact_evidence_rows),
        "search_api_calls": search_calls,
        **provider_totals,
        "errors": len(errors),
    }
    write_json(output_dir / "run_summary.json", summary)
    progress("Contact waterfall complete.")
    return {
        "summary": summary,
        "candidates": ranked,
        "founders": founder_rows,
        "contact_evidence": contact_evidence_rows,
        "errors": errors,
        "output_dir": str(output_dir),
    }


def run_pipeline(
    seed_csv: str | Path,
    output_dir: str | Path,
    settings: Settings,
    mode: str = "mock",
    crawler_backend: str | None = None,
    llm_backend: str | None = None,
    ollama_model: str | None = None,
    google_model: str | None = None,
    claude_model: str | None = None,
    deepseek_model: str | None = None,
    max_sources: int = 5,
    seed_start_index: int = 1,
    selected_ids: list[str] | None = None,
    max_priority: int = 2,
    max_pages_per_source: int = 2,
    generate_drafts: bool = True,
    enrich_contacts: bool = False,
    contact_max_pages: int | None = None,
    search_backend: str | None = None,
    email_provider: str | None = None,
    people_provider: str | None = None,
    search_max_results: int | None = None,
    search_max_pages_per_candidate: int | None = None,
    search_queries_per_founder: int | None = None,
    enable_founder_search: bool | None = None,
    enable_pdf_search: bool | None = None,
    verify_emails: bool | None = None,
    paid_fallback_only: bool | None = None,
    apollo_reveal_personal_emails: bool | None = None,
    apollo_run_waterfall_email: bool | None = None,
    progress: Progress = _noop,
    should_stop: StopCheck = _never_stop,
) -> dict:
    _run_started = time.monotonic()
    stopped = False
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    crawler_name = "mock" if mode == "mock" else (crawler_backend or settings.crawler_backend).lower()
    llm_name = "mock" if mode == "mock" else (llm_backend or settings.llm_backend).lower()
    search_name = "none" if mode == "mock" else (search_backend or settings.search_backend).lower()
    email_provider_name = "none" if mode == "mock" else (email_provider or settings.email_provider).lower()
    people_provider_name = "none" if mode == "mock" else (people_provider or settings.people_provider).lower()
    if mode == "mock":
        effective_llm_model = "mock"
    elif llm_name == "ollama":
        effective_llm_model = ollama_model or settings.ollama_model
    elif llm_name == "google":
        effective_llm_model = google_model or settings.google_model
    elif llm_name == "claude":
        effective_llm_model = claude_model or settings.claude_model
    elif llm_name == "deepseek":
        effective_llm_model = deepseek_model or settings.deepseek_model
    else:
        effective_llm_model = settings.openai_model

    effective_contact_max_pages = contact_max_pages or settings.contact_max_pages
    effective_search_max_results = search_max_results or settings.search_max_results
    effective_search_max_pages = search_max_pages_per_candidate or settings.search_max_pages_per_candidate
    effective_queries_per_founder = search_queries_per_founder or settings.search_queries_per_founder
    effective_founder_search = settings.enable_founder_search if enable_founder_search is None else enable_founder_search
    effective_pdf_search = settings.enable_pdf_search if enable_pdf_search is None else enable_pdf_search
    effective_verify = settings.verify_emails if verify_emails is None else verify_emails
    effective_paid_fallback = settings.paid_fallback_only if paid_fallback_only is None else paid_fallback_only
    effective_apollo_personal = settings.apollo_reveal_personal_emails if apollo_reveal_personal_emails is None else apollo_reveal_personal_emails
    effective_apollo_waterfall = settings.apollo_run_waterfall_email if apollo_run_waterfall_email is None else apollo_run_waterfall_email

    crawler, extractor, drafter = build_components(
        mode=mode,
        settings=settings,
        crawler_backend=crawler_name if mode == "live" else None,
        llm_backend=llm_name if mode == "live" else None,
        ollama_model=ollama_model,
        google_model=google_model,
        claude_model=claude_model,
        deepseek_model=deepseek_model,
    )
    progress(
        f"Backends: crawler={crawler_name}, llm={llm_name}, model={effective_llm_model}, "
        f"search={search_name}, email_provider={email_provider_name}, people_provider={people_provider_name}"
    )

    all_sources = load_seed_sources(seed_csv)
    start_index = max(1, seed_start_index)
    if selected_ids is not None:
        # Explicit pick: run exactly the chosen seeds, in file order, ignoring priority/slice.
        wanted = {str(i) for i in selected_ids}
        sources = [s for s in all_sources if str(s.get("ID", "")) in wanted]
        progress(f"Selected {len(sources)} seed source(s) by explicit selection.")
    else:
        eligible_sources = [s for s in all_sources if s["priority"] <= max_priority]
        sources = eligible_sources[start_index - 1:start_index - 1 + max_sources]
        progress(f"Selected {len(sources)} seed sources starting at eligible source #{start_index}.")

    pages = []
    raw_candidates: list[dict] = []
    errors: list[dict] = []

    for index, source in enumerate(sources, start=1):
        if should_stop():
            stopped = True
            progress(f"Stop requested — halting after {index - 1}/{len(sources)} sources; writing partial results.")
            break
        progress(f"Crawling source {index}/{len(sources)}: {source.get('Organization / University', '')}")
        try:
            source_pages = crawler.crawl_source(source, max_pages=max_pages_per_source)
        except Exception as exc:
            errors.append({"source_id": source.get("ID", ""), "stage": "crawl", "error": str(exc)})
            continue
        pages.extend(source_pages)

        for page in source_pages:
            try:
                batch = extractor.extract(page)
            except Exception as exc:
                errors.append({"source_id": page.source_id, "page_url": page.page_url, "stage": "extract", "error": str(exc)})
                continue
            # A content-bearing page yielding nothing is usually a failed extraction, not an
            # empty roster. Record it so it cannot pass silently as "no startups here".
            if not batch.candidates and len(page.markdown.strip()) >= EMPTY_RESULT_WARN_CHARS:
                errors.append({
                    "source_id": page.source_id, "page_url": page.page_url, "stage": "extract",
                    "error": f"no candidates returned from {len(page.markdown):,} chars of page content",
                })
            for candidate_model in batch.candidates:
                candidate = candidate_model.model_dump()
                candidate["source_id"] = page.source_id
                candidate["source_org"] = page.source_org
                candidate["source_priority"] = source["priority"]
                candidate["source_urls"] = [page.page_url]

                existing_website = canonicalize_website(candidate.get("website", ""))
                if existing_website and is_probable_startup_website(existing_website, page.page_url):
                    candidate["website"] = existing_website
                    candidate["website_discovery_method"] = "llm_extracted"
                    candidate["website_discovery_confidence"] = 1.0
                else:
                    discovered_url, website_confidence, method = discover_startup_website(
                        candidate.get("startup_name", ""), page.markdown, page.page_url
                    )
                    candidate["website"] = discovered_url
                    candidate["website_discovery_method"] = method or (
                        "rejected_source_or_directory_url" if existing_website else ""
                    )
                    candidate["website_discovery_confidence"] = website_confidence

                guaranteed = bool(source.get("eligibility_guaranteed"))
                candidate["source_eligibility_guaranteed"] = guaranteed
                candidate = enforce_eligibility(candidate, settings, source_guaranteed=guaranteed)
                candidate["priority_score"] = compute_priority_score(candidate, source["priority"])
                raw_candidates.append(candidate)

    ranked = deduplicate(raw_candidates)
    ranked.sort(key=lambda x: x.get("priority_score", 0), reverse=True)

    contacts_before = sum(bool(c.get("contact_email")) for c in ranked)
    search_enricher = None
    provider_enricher = None
    if enrich_contacts and mode == "live":
        progress("Building free-first contact intelligence waterfall.")
        waterfall, search_enricher, provider_enricher = build_contact_waterfall(
            crawler=crawler,
            settings=settings,
            search_backend=search_name,
            email_provider=email_provider_name,
            people_provider=people_provider_name,
            contact_max_pages=effective_contact_max_pages,
            search_max_results=effective_search_max_results,
            search_max_pages_per_candidate=effective_search_max_pages,
            search_queries_per_founder=effective_queries_per_founder,
            enable_founder_search=effective_founder_search,
            enable_pdf_search=effective_pdf_search,
            verify_emails=effective_verify,
            paid_fallback_only=effective_paid_fallback,
            apollo_reveal_personal_emails=effective_apollo_personal,
            apollo_run_waterfall_email=effective_apollo_waterfall,
            progress=progress,
        )
        enriched: list[dict] = []
        for idx, candidate in enumerate(ranked, start=1):
            if should_stop():
                stopped = True
                progress(f"Stop requested — enriched {idx - 1}/{len(ranked)}; keeping the rest un-enriched.")
                enriched.extend(ranked[idx - 1:])   # preserve the remaining candidates
                break
            progress(f"Contact waterfall {idx}/{len(ranked)}: {candidate.get('startup_name', '')}")
            try:
                enriched.append(waterfall.enrich(candidate))
            except Exception as exc:
                candidate["contact_waterfall_status"] = "waterfall_error"
                candidate.setdefault("contact_enrichment_errors", []).append({"stage": "waterfall", "error": str(exc)})
                errors.append({
                    "source_id": candidate.get("source_id", ""),
                    "startup_name": candidate.get("startup_name", ""),
                    "stage": "contact_waterfall",
                    "error": str(exc),
                })
                enriched.append(candidate)
        ranked = enriched
    contacts_after = sum(bool(c.get("contact_email")) for c in ranked)

    for i, candidate in enumerate(ranked, start=1):
        candidate["rank"] = i
        candidate["review_decision"] = "Pending"

    drafts: list[dict] = []
    if generate_drafts:
        for candidate in ranked:
            if candidate.get("eligibility_status") != "qualified":
                continue
            try:
                draft = drafter.draft(candidate)
            except Exception as exc:
                errors.append({
                    "source_id": candidate.get("source_id", ""),
                    "startup_name": candidate.get("startup_name", ""),
                    "stage": "draft",
                    "error": str(exc),
                })
                continue
            drafts.append({
                "startup_name": candidate.get("startup_name", ""),
                "to_email": candidate.get("founder_email") or candidate.get("contact_email", ""),
                "contact_type": candidate.get("founder_email_type") or candidate.get("contact_email_type", ""),
                "verification_status": next((
                    e.get("verification_status", "") for e in candidate.get("contact_evidence", [])
                    if e.get("email") == (candidate.get("founder_email") or candidate.get("contact_email"))
                ), ""),
                "priority_score": candidate.get("priority_score", 0),
                "subject": draft.subject,
                "body": draft.body,
                "approval_status": "Pending",
            })

    founder_rows = build_founder_rows(ranked)

    write_jsonl(output_dir / "crawled_pages.jsonl", [asdict(p) for p in pages])
    write_csv(output_dir / "candidates_raw.csv", raw_candidates)
    write_csv(output_dir / "candidates_ranked.csv", ranked)
    write_csv(output_dir / "founders.csv", founder_rows)

    contact_evidence_rows: list[dict] = []
    for candidate in ranked:
        for evidence in candidate.get("contact_evidence", []) or []:
            contact_evidence_rows.append({
                "startup_name": candidate.get("startup_name", ""),
                "website": candidate.get("website", ""),
                "official_domain": candidate.get("official_domain", ""),
                **evidence,
            })
    write_csv(output_dir / "contact_evidence.csv", contact_evidence_rows)

    search_evidence_rows: list[dict] = []
    for candidate in ranked:
        for query in candidate.get("search_queries_run", []) or []:
            search_evidence_rows.append({
                "startup_name": candidate.get("startup_name", ""),
                "query": query,
                "search_backend": search_name,
            })
    write_csv(output_dir / "search_queries.csv", search_evidence_rows)
    write_csv(output_dir / "outreach_drafts.csv", drafts)
    write_csv(output_dir / "errors.csv", errors)

    provider_totals = provider_call_totals(provider_enricher)
    search_calls = getattr(getattr(search_enricher, "search_client", None), "calls", 0) if search_enricher else 0
    verified_valid = sum(
        any(e.get("verification_status") == "valid" for e in c.get("contact_evidence", []) or [])
        for c in ranked
    )

    summary = {
        "mode": mode,
        "crawler_backend": crawler_name,
        "llm_backend": llm_name,
        "llm_model": effective_llm_model,
        "search_backend": search_name,
        "email_provider": email_provider_name,
        "people_provider": people_provider_name,
        "competition_name": settings.competition_name,
        "competition_year": settings.competition_year,
        "graduation_cutoff_year": settings.graduation_cutoff_year,
        "sources_attempted": len(sources),
        "seed_start_index": start_index,
        "pages_processed": len(pages),
        "raw_candidate_rows": len(raw_candidates),
        "unique_candidates": len(ranked),
        "qualified": sum(c.get("eligibility_status") == "qualified" for c in ranked),
        "review_required": sum(c.get("eligibility_status") == "review" for c in ranked),
        "not_qualified": sum(c.get("eligibility_status") == "not_qualified" for c in ranked),
        "drafts_generated": len(drafts),
        "contacts_enriched": max(0, contacts_after - contacts_before),
        "candidates_with_any_contact": sum(bool(c.get("contact_email")) for c in ranked),
        "founder_emails_found": sum(bool(c.get("founder_email")) for c in ranked),
        "company_emails_found": sum(bool(c.get("company_email")) for c in ranked),
        "verified_valid_candidates": verified_valid,
        "startup_websites_available": sum(bool(c.get("website")) for c in ranked),
        "founder_records": len(founder_rows),
        "founder_records_with_email": sum(bool(r.get("email")) for r in founder_rows),
        "contact_evidence_rows": len(contact_evidence_rows),
        "search_api_calls": search_calls,
        **provider_totals,
        "errors": len(errors),
        "stopped": stopped,
        "elapsed_seconds": round(time.monotonic() - _run_started, 1),
    }
    write_json(output_dir / "run_summary.json", summary)
    progress(
        ("Pipeline stopped early — partial results written." if stopped else "Pipeline complete.")
        + f" ({summary['elapsed_seconds']}s)"
    )
    return {
        "summary": summary,
        "candidates": ranked,
        "founders": founder_rows,
        "drafts": drafts,
        "errors": errors,
        "stopped": stopped,
        "elapsed_seconds": summary["elapsed_seconds"],
    }
