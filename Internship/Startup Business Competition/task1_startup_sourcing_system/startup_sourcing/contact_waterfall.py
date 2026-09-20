from __future__ import annotations

from typing import Callable

from .contact_enrichment import WebsiteContactEnricher
from .provider_enrichment import PaidProviderEnricher
from .search_enrichment import SearchContactEnricher


class ContactWaterfall:
    """Free-first contact intelligence waterfall with optional paid fallbacks."""

    def __init__(
        self,
        *,
        website_enricher: WebsiteContactEnricher,
        search_enricher: SearchContactEnricher | None = None,
        provider_enricher: PaidProviderEnricher | None = None,
        paid_fallback_only: bool = True,
        enrichment_scope: str = "all",
        progress: Callable[[str], None] | None = None,
    ):
        self.website_enricher = website_enricher
        self.search_enricher = search_enricher
        self.provider_enricher = provider_enricher
        self.paid_fallback_only = paid_fallback_only
        if enrichment_scope not in {"all", "company", "founder"}:
            raise ValueError("enrichment_scope must be 'all', 'company', or 'founder'")
        self.enrichment_scope = enrichment_scope
        self.progress = progress or (lambda _: None)

    def enrich(self, candidate: dict) -> dict:
        candidate.setdefault("contact_methods_attempted", [])
        startup = candidate.get("startup_name", "")

        if self.enrichment_scope != "founder":
            self.progress(f"  Company public-site crawl: {startup}")
            candidate["contact_methods_attempted"].append("startup_website")
            candidate = self.website_enricher.enrich(candidate)

        target_field = "company_email" if self.enrichment_scope == "company" else "founder_email"
        if self.search_enricher and not candidate.get(target_field):
            self.progress(f"  {self.enrichment_scope.title()} web search: {startup}")
            candidate = self.search_enricher.enrich(candidate)

        should_call_paid = bool(self.provider_enricher) and (
            not self.paid_fallback_only
            or not candidate.get(target_field)
            or bool(getattr(self.provider_enricher, "verify_emails", False))
        )
        if should_call_paid:
            self.progress(f"  {self.enrichment_scope.title()} provider enrichment: {startup}")
            candidate = self.provider_enricher.enrich(candidate)

        candidate["contact_methods_attempted"] = list(
            dict.fromkeys(candidate.get("contact_methods_attempted", []))
        )
        candidate["contact_waterfall_status"] = (
            "founder_email_found" if candidate.get("founder_email") else
            "company_email_found" if candidate.get("company_email") else
            "generic_or_other_contact_found" if candidate.get("contact_email") else
            "no_contact_found"
        )
        return candidate
