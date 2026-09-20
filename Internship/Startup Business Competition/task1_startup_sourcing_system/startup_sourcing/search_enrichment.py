from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from rapidfuzz.fuzz import ratio

from .contact_enrichment import (
    EmailFinding,
    apply_findings_to_candidate,
    classify_email,
    evidence_key,
    extract_linkedin_urls,
    extract_public_emails,
    finding_from_dict,
    founder_names,
    startup_domain,
)
from .http_utils import json_request
from .website_discovery import (
    canonicalize_website,
    discover_startup_website_from_search,
)


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    description: str = ""
    extra_snippets: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "description": self.description,
            "extra_snippets": list(self.extra_snippets),
        }

    @property
    def text(self) -> str:
        return "\n".join([self.title, self.description, *self.extra_snippets])


class BraveSearchClient:
    endpoint = "https://api.search.brave.com/res/v1/web/search"
    provider = "brave"

    def __init__(self, api_key: str, max_results: int = 8):
        if not api_key:
            raise ValueError("BRAVE_SEARCH_API_KEY is required when search_backend=brave")
        self.api_key = api_key
        self.max_results = max(1, min(20, max_results))
        self.calls = 0

    _warned = False   # class-level: warn once per process on a Brave failure

    def search(self, query: str, count: int | None = None) -> list[SearchResult]:
        self.calls += 1
        try:
            payload = json_request(
                self.endpoint,
                params={
                    "q": query,
                    "count": max(1, min(20, count or self.max_results)),
                    "extra_snippets": "true",
                    "safesearch": "moderate",
                },
                headers={"X-Subscription-Token": self.api_key},
                timeout=30,
            )
        except Exception as exc:
            # Never let one bad Brave call abort the run - but make failures VISIBLE.
            # (A silent auth failure previously hid an invalid key across whole runs.)
            if not BraveSearchClient._warned:
                BraveSearchClient._warned = True
                msg = str(exc)
                hint = ("  <-- invalid/expired BRAVE_SEARCH_API_KEY; fix it in .env"
                        if ("422" in msg or "401" in msg or "TOKEN" in msg.upper()) else "")
                print(f"  [!] Brave search is FAILING - all search enrichment will be empty{hint}\n"
                      f"      {msg[:200]}", flush=True)
            return []
        rows = ((payload.get("web") or {}).get("results") or [])
        results: list[SearchResult] = []
        for row in rows:
            url = canonicalize_website(str(row.get("url", "")))
            if not url:
                continue
            results.append(
                SearchResult(
                    title=str(row.get("title", "")),
                    url=url,
                    description=str(row.get("description", "")),
                    extra_snippets=tuple(str(x) for x in (row.get("extra_snippets") or [])),
                )
            )
        return results


class TavilySearchClient:
    """Tavily search API — an alternative to Brave, built for LLM/agent use.

    Same surface as BraveSearchClient (.search(query, count) -> [SearchResult])
    so the enricher is provider-agnostic. POST /search with Bearer auth; a
    result's `content` maps to our `description`. Like Brave, a failed call is
    logged once and returns [] rather than aborting a whole run. A light
    self-throttle keeps a many-query candidate under Tavily's rate limit.
    """

    endpoint = "https://api.tavily.com/search"
    provider = "tavily"
    _MIN_INTERVAL = 1.05
    _warned = False

    def __init__(self, api_key: str, max_results: int = 8):
        if not api_key:
            raise ValueError("TAVILY_API_KEY is required when search_backend=tavily")
        self.api_key = api_key
        self.max_results = max(1, min(20, max_results))
        self.calls = 0
        self._last_call = 0.0

    def search(self, query: str, count: int | None = None) -> list[SearchResult]:
        import time

        self.calls += 1
        wait = self._MIN_INTERVAL - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            payload = json_request(
                self.endpoint,
                method="POST",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json_body={
                    "query": query,
                    "max_results": max(1, min(20, count or self.max_results)),
                    "topic": "general",
                },
                timeout=30,
            )
        except Exception as exc:
            if not TavilySearchClient._warned:
                TavilySearchClient._warned = True
                msg = str(exc)
                hint = ("  <-- invalid/expired TAVILY_API_KEY; fix it in .env"
                        if ("401" in msg or "403" in msg or "432" in msg) else "")
                print(f"  [!] Tavily search is FAILING - all search enrichment will be empty{hint}\n"
                      f"      {msg[:200]}", flush=True)
            return []
        finally:
            self._last_call = time.monotonic()
        results: list[SearchResult] = []
        for row in (payload.get("results") or []):
            url = canonicalize_website(str(row.get("url", "")))
            if not url:
                continue
            results.append(
                SearchResult(
                    title=str(row.get("title", "")),
                    url=url,
                    description=str(row.get("content", "")),
                )
            )
        return results


class PdfContactExtractor:
    """Download public PDFs surfaced by search and extract text for email discovery."""

    def extract_text(self, url: str, max_bytes: int = 15_000_000) -> str:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF search requires pypdf. Install requirements.txt.") from exc

        req = Request(url, headers={"User-Agent": "Mozilla/5.0 contact-research-bot/1.0"})
        with urlopen(req, timeout=35) as response:
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise RuntimeError(f"PDF exceeds {max_bytes} byte safety limit")
        reader = PdfReader(io.BytesIO(data))
        chunks: list[str] = []
        for page in reader.pages[:50]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(chunks)


class SearchContactEnricher:
    """Use search to recover domains, founder identities, public emails and source pages."""

    def __init__(
        self,
        search_client,
        crawler,
        *,
        max_results: int = 8,
        max_pages_per_candidate: int = 6,
        queries_per_founder: int = 4,
        enable_founder_search: bool = True,
        enable_pdf_search: bool = True,
        enrichment_scope: str = "all",
        progress: Callable[[str], None] | None = None,
    ):
        self.search_client = search_client
        self.crawler = crawler
        self.max_results = max(1, max_results)
        self.max_pages = max(0, max_pages_per_candidate)
        self.queries_per_founder = max(1, queries_per_founder)
        self.enable_founder_search = enable_founder_search
        self.enable_pdf_search = enable_pdf_search
        if enrichment_scope not in {"all", "company", "founder"}:
            raise ValueError("enrichment_scope must be 'all', 'company', or 'founder'")
        self.enrichment_scope = enrichment_scope
        self.progress = progress or (lambda _: None)
        self.pdf = PdfContactExtractor()

    @staticmethod
    def _founder_queries(name: str, startup: str, domain: str) -> list[str]:
        queries = [
            f'"{name}" "{startup}" email',
            f'"{name}" "{startup}" LinkedIn',
            f'"{name}" "{startup}" founder',
        ]
        if domain:
            queries.insert(1, f'"{name}" "@{domain}"')
        return queries

    def build_queries(self, candidate: dict) -> list[str]:
        startup = candidate.get("startup_name", "").strip()
        domain = startup_domain(candidate)
        queries: list[str] = []
        if self.enrichment_scope in {"all", "company"}:
            queries.append(f'"{startup}" contact email')
            if self.enrichment_scope == "all":
                queries.append(f'"{startup}" founder email')
            if domain:
                queries.append(f'site:{domain} email')
                if self.enrichment_scope == "all":
                    queries.append(f'site:{domain} founder')
            else:
                # Recover the official company site before searching its contact pages.
                industry = (candidate.get("industry") or "").strip()
                queries.append(f'"{startup}" official website')
                queries.append(f'"{startup}" {industry} startup' if industry else f'"{startup}" startup')
            if self.enable_pdf_search:
                queries.append(f'"{startup}" filetype:pdf')
        if self.enrichment_scope in {"all", "founder"} and self.enable_founder_search:
            for name in founder_names(candidate):
                queries.extend(self._founder_queries(name, startup, domain)[: self.queries_per_founder])
                if self.enable_pdf_search:
                    queries.append(f'"{name}" "{startup}" filetype:pdf')
        # Stable de-duplication.
        return list(dict.fromkeys(q for q in queries if q.strip()))

    @staticmethod
    def _page_score(result: SearchResult, candidate: dict) -> float:
        startup = re.sub(r"[^a-z0-9]+", "", candidate.get("startup_name", "").lower())
        text = (result.title + " " + result.description).lower()
        score = 0.0
        for term, weight in {
            "contact": 25, "founder": 24, "team": 18, "leadership": 18,
            "email": 22, "about": 12, "profile": 10, "speaker": 8,
        }.items():
            if term in text or term in urlparse(result.url).path.lower():
                score += weight
        compact = re.sub(r"[^a-z0-9]+", "", text)
        if startup:
            score += 0.35 * ratio(startup, compact[: max(len(startup) * 4, 20)])
        if result.url.lower().endswith(".pdf"):
            score += 15
        return score

    def _crawl_search_result(self, candidate: dict, url: str) -> list[tuple[str, str]]:
        pseudo = {
            "ID": candidate.get("source_id", "SEARCH"),
            "Organization / University": candidate.get("startup_name", ""),
            "Seed URL": url,
        }
        pages = self.crawler.crawl_source(pseudo, max_pages=1)
        return [(page.page_url, page.markdown) for page in pages]

    def enrich(self, candidate: dict) -> dict:
        candidate.setdefault("search_queries_run", [])
        candidate.setdefault("search_results_considered", 0)
        candidate.setdefault("search_pages_crawled", 0)
        candidate.setdefault("search_pdf_pages", 0)
        candidate.setdefault("search_enrichment_errors", [])
        candidate.setdefault("contact_evidence", [])
        candidate.setdefault("contact_methods_attempted", [])
        candidate["contact_methods_attempted"] = list(dict.fromkeys([
            *candidate.get("contact_methods_attempted", []), "web_search"
        ]))

        findings: dict[str, EmailFinding] = {
            f"existing:{i}:{row.get('email', '')}": finding_from_dict(row)
            for i, row in enumerate(candidate.get("contact_evidence", []) or [])
            if isinstance(row, dict) and row.get("email")
        }

        def add_finding(finding: EmailFinding) -> None:
            if self.enrichment_scope == "company" and not finding.email_type.startswith("company_"):
                return
            if self.enrichment_scope == "founder" and not finding.email_type.startswith("founder_"):
                return
            findings["|".join(str(part) for part in evidence_key(finding.as_dict()))] = finding

        people_urls: list[str] = [candidate.get("founder_linkedin_url", "")] if candidate.get("founder_linkedin_url") else []
        company_urls: list[str] = [candidate.get("linkedin_url", "")] if candidate.get("linkedin_url") else []

        all_results: list[SearchResult] = []
        seen_result_urls: set[str] = set()
        for query in self.build_queries(candidate):
            candidate["search_queries_run"].append(query)
            self.progress(f"  Search: {query}")
            try:
                results = self.search_client.search(query, count=self.max_results)
            except Exception as exc:
                candidate["search_enrichment_errors"].append({"query": query, "error": str(exc)})
                continue
            for result in results:
                if result.url not in seen_result_urls:
                    seen_result_urls.add(result.url)
                    all_results.append(result)
                text = result.text
                for email in extract_public_emails(text):
                    base = classify_email(email, founder_names(candidate), startup_domain(candidate), result.url)
                    finding = EmailFinding(
                        email=base.email,
                        email_type=base.email_type,
                        confidence=min(0.94, max(base.confidence, 0.62)),
                        source_url=result.url,
                        founder_match=base.founder_match,
                        origin="search_snippet",
                        provider=getattr(self.search_client, "provider", "search"),
                    )
                    add_finding(finding)
                people, companies = extract_linkedin_urls(text + "\n" + result.url)
                people_urls.extend(u for u in people if u not in people_urls)
                company_urls.extend(u for u in companies if u not in company_urls)

        candidate["search_results_considered"] = len(all_results)

        # Search-assisted official-domain recovery.
        if not candidate.get("website") and all_results:
            url, confidence, relationship, evidence = discover_startup_website_from_search(
                candidate.get("startup_name", ""),
                [r.as_dict() for r in all_results],
                candidate.get("source_urls", []),
            )
            if url:
                candidate["website"] = url
                candidate["website_discovery_method"] = "search_result_resolution"
                candidate["website_discovery_confidence"] = confidence
                candidate["official_domain"] = (urlparse(url).hostname or "").removeprefix("www.")
                candidate["domain_confidence"] = confidence
                candidate["domain_relationship"] = relationship
                candidate["domain_evidence"] = evidence

        ranked_results = sorted(all_results, key=lambda r: -self._page_score(r, candidate))
        pages_used = 0
        for result in ranked_results:
            if pages_used >= self.max_pages:
                break
            try:
                if self.enable_pdf_search and result.url.lower().split("?")[0].endswith(".pdf"):
                    text = self.pdf.extract_text(result.url)
                    documents = [(result.url, text)]
                    candidate["search_pdf_pages"] += 1
                else:
                    documents = self._crawl_search_result(candidate, result.url)
                    candidate["search_pages_crawled"] += len(documents)
                pages_used += 1
            except Exception as exc:
                candidate["search_enrichment_errors"].append({"url": result.url, "error": str(exc)})
                continue

            for source_url, text in documents:
                for email in extract_public_emails(text):
                    base = classify_email(email, founder_names(candidate), startup_domain(candidate), source_url)
                    finding = EmailFinding(
                        email=base.email,
                        email_type=base.email_type,
                        confidence=base.confidence,
                        source_url=source_url,
                        founder_match=base.founder_match,
                        origin="search_result_page" if not source_url.lower().endswith(".pdf") else "public_pdf",
                        provider="public_web",
                    )
                    add_finding(finding)
                people, companies = extract_linkedin_urls(text)
                people_urls.extend(u for u in people if u not in people_urls)
                company_urls.extend(u for u in companies if u not in company_urls)

        apply_findings_to_candidate(candidate, findings, people_urls, company_urls)
        candidate["search_enrichment_status"] = (
            "email_found" if findings else "searched_no_email"
        )
        return candidate
