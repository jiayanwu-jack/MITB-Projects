from __future__ import annotations

import argparse
from pathlib import Path

from startup_sourcing.config import settings
from startup_sourcing.pipeline import (
    CRAWLER_BACKENDS,
    EMAIL_PROVIDERS,
    LLM_BACKENDS,
    PEOPLE_PROVIDERS,
    SEARCH_BACKENDS,
    run_pipeline,
)

ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="University startup sourcing pipeline")
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--crawler-backend", choices=CRAWLER_BACKENDS, default=settings.crawler_backend)
    parser.add_argument("--llm-backend", choices=LLM_BACKENDS, default=settings.llm_backend)
    parser.add_argument("--ollama-model", default=settings.ollama_model)
    parser.add_argument("--google-model", default=settings.google_model)
    parser.add_argument("--claude-model", default=settings.claude_model)
    parser.add_argument("--deepseek-model", default=settings.deepseek_model)

    parser.add_argument("--seed-csv", default=ROOT / "data" / "seed_sources.csv")
    parser.add_argument("--output-dir", default=ROOT / "outputs" / "latest")
    parser.add_argument("--max-sources", type=int, default=5)
    parser.add_argument("--seed-start-index", type=int, default=1)
    parser.add_argument("--max-priority", type=int, default=2)
    parser.add_argument("--max-pages-per-source", type=int, default=2)
    parser.add_argument("--no-drafts", action="store_true")

    parser.add_argument(
        "--enrich-contacts", action="store_true",
        help="Run the contact intelligence waterfall after candidate extraction.",
    )
    parser.add_argument("--contact-max-pages", type=int, default=settings.contact_max_pages)

    parser.add_argument("--search-backend", choices=SEARCH_BACKENDS, default=settings.search_backend)
    parser.add_argument("--search-max-results", type=int, default=settings.search_max_results)
    parser.add_argument(
        "--search-max-pages-per-candidate", type=int,
        default=settings.search_max_pages_per_candidate,
    )
    parser.add_argument(
        "--search-queries-per-founder", type=int,
        default=settings.search_queries_per_founder,
    )
    parser.add_argument(
        "--enable-founder-search", action=argparse.BooleanOptionalAction,
        default=settings.enable_founder_search,
    )
    parser.add_argument(
        "--enable-pdf-search", action=argparse.BooleanOptionalAction,
        default=settings.enable_pdf_search,
    )

    parser.add_argument("--email-provider", choices=EMAIL_PROVIDERS, default=settings.email_provider)
    parser.add_argument("--people-provider", choices=PEOPLE_PROVIDERS, default=settings.people_provider)
    parser.add_argument(
        "--verify-emails", action=argparse.BooleanOptionalAction,
        default=settings.verify_emails,
    )
    parser.add_argument(
        "--paid-fallback-only", action=argparse.BooleanOptionalAction,
        default=settings.paid_fallback_only,
        help="When enabled, paid providers run only if no founder email was found by free methods.",
    )
    parser.add_argument(
        "--apollo-reveal-personal-emails", action=argparse.BooleanOptionalAction,
        default=settings.apollo_reveal_personal_emails,
        help="Potentially consumes additional Apollo credits and is off by default.",
    )
    parser.add_argument(
        "--apollo-run-waterfall-email", action=argparse.BooleanOptionalAction,
        default=settings.apollo_run_waterfall_email,
        help="Run Apollo waterfall email enrichment; off by default because it may consume credits.",
    )
    args = parser.parse_args()

    result = run_pipeline(
        seed_csv=args.seed_csv,
        output_dir=args.output_dir,
        settings=settings,
        mode=args.mode,
        crawler_backend=args.crawler_backend,
        llm_backend=args.llm_backend,
        ollama_model=args.ollama_model,
        google_model=args.google_model,
        claude_model=args.claude_model,
        deepseek_model=args.deepseek_model,
        max_sources=args.max_sources,
        seed_start_index=args.seed_start_index,
        max_priority=args.max_priority,
        max_pages_per_source=args.max_pages_per_source,
        generate_drafts=not args.no_drafts,
        enrich_contacts=args.enrich_contacts,
        contact_max_pages=args.contact_max_pages,
        search_backend=args.search_backend,
        email_provider=args.email_provider,
        people_provider=args.people_provider,
        search_max_results=args.search_max_results,
        search_max_pages_per_candidate=args.search_max_pages_per_candidate,
        search_queries_per_founder=args.search_queries_per_founder,
        enable_founder_search=args.enable_founder_search,
        enable_pdf_search=args.enable_pdf_search,
        verify_emails=args.verify_emails,
        paid_fallback_only=args.paid_fallback_only,
        apollo_reveal_personal_emails=args.apollo_reveal_personal_emails,
        apollo_run_waterfall_email=args.apollo_run_waterfall_email,
        progress=print,
    )
    print("\nRun summary")
    for key, value in result["summary"].items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
