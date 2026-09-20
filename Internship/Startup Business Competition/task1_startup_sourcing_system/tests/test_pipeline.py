import dataclasses
from pathlib import Path

import startup_sourcing.pipeline as pipeline_module
from startup_sourcing.config import settings
from startup_sourcing.pipeline import build_components, run_pipeline


def test_mock_pipeline(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    result = run_pipeline(
        seed_csv=root / "data" / "seed_sources.csv",
        output_dir=tmp_path,
        settings=settings,
        mode="mock",
        max_sources=2,
        max_priority=1,
        max_pages_per_source=2,
    )
    summary = result["summary"]
    assert summary["sources_attempted"] == 2
    assert summary["crawler_backend"] == "mock"
    assert summary["llm_backend"] == "mock"
    assert summary["unique_candidates"] >= 4
    assert summary["qualified"] >= 2
    assert (tmp_path / "candidates_ranked.csv").exists()
    assert (tmp_path / "outreach_drafts.csv").exists()


def test_pipeline_starts_from_requested_seed_source(tmp_path: Path):
    seed_csv = tmp_path / "seed.csv"
    seed_csv.write_text(
        "ID,Organization / University,Seed URL,Priority (1=highest)\n"
        "S1,First University,https://first.example/startups,1\n"
        "S2,Second University,https://second.example/startups,1\n",
        encoding="utf-8",
    )
    result = run_pipeline(
        seed_csv=seed_csv,
        output_dir=tmp_path / "out",
        settings=settings,
        mode="mock",
        max_sources=1,
        seed_start_index=2,
        max_priority=1,
        max_pages_per_source=1,
        generate_drafts=False,
    )

    summary = result["summary"]
    assert summary["seed_start_index"] == 2
    assert summary["sources_attempted"] == 1
    assert {candidate["source_id"] for candidate in result["candidates"]} == {"S2"}


def test_live_backend_selection_crawl4ai_ollama(monkeypatch):
    monkeypatch.setattr(pipeline_module, "Crawl4AICrawler", lambda cfg: "crawl4ai-crawler")
    monkeypatch.setattr(pipeline_module, "OllamaExtractor", lambda cfg, model=None: ("ollama-extractor", model))
    monkeypatch.setattr(pipeline_module, "OllamaEmailDrafter", lambda cfg, model=None: ("ollama-drafter", model))

    crawler, extractor, drafter = build_components(
        mode="live",
        settings=settings,
        crawler_backend="crawl4ai",
        llm_backend="ollama",
        ollama_model="qwen3:14b",
    )

    assert crawler == "crawl4ai-crawler"
    assert extractor == ("ollama-extractor", "qwen3:14b")
    assert drafter == ("ollama-drafter", "qwen3:14b")


def test_live_backend_selection_google(monkeypatch):
    monkeypatch.setattr(pipeline_module, "Crawl4AICrawler", lambda cfg: "crawl4ai-crawler")
    monkeypatch.setattr(pipeline_module, "GoogleAIStudioExtractor", lambda cfg, model=None: ("google-extractor", model))
    monkeypatch.setattr(pipeline_module, "GoogleEmailDrafter", lambda cfg, model=None: ("google-drafter", model))

    crawler, extractor, drafter = build_components(
        mode="live",
        settings=settings,
        crawler_backend="crawl4ai",
        llm_backend="google",
        google_model="gemini-2.5-flash",
    )

    assert crawler == "crawl4ai-crawler"
    assert extractor == ("google-extractor", "gemini-2.5-flash")
    assert drafter == ("google-drafter", "gemini-2.5-flash")


def test_live_backend_selection_claude(monkeypatch):
    monkeypatch.setattr(pipeline_module, "Crawl4AICrawler", lambda cfg: "crawl4ai-crawler")
    monkeypatch.setattr(pipeline_module, "ClaudeExtractor", lambda cfg, model=None: ("claude-extractor", model))
    monkeypatch.setattr(pipeline_module, "ClaudeEmailDrafter", lambda cfg, model=None: ("claude-drafter", model))

    crawler, extractor, drafter = build_components(
        mode="live",
        settings=settings,
        crawler_backend="crawl4ai",
        llm_backend="claude",
        claude_model="claude-3-5-sonnet-latest",
    )

    assert crawler == "crawl4ai-crawler"
    assert extractor == ("claude-extractor", "claude-3-5-sonnet-latest")
    assert drafter == ("claude-drafter", "claude-3-5-sonnet-latest")


def test_ollama_extractor_uses_schema_and_default_qwen_model(monkeypatch):
    import sys
    import types

    from startup_sourcing.crawler import CrawledPage
    from startup_sourcing.extractor import OllamaExtractor

    captured = {}

    class FakeMessage:
        content = '{"candidates": []}'

    class FakeResponse:
        message = FakeMessage()

    class FakeClient:
        def __init__(self, host):
            captured["host"] = host

        def chat(self, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

    fake_ollama = types.ModuleType("ollama")
    fake_ollama.Client = FakeClient
    monkeypatch.setitem(sys.modules, "ollama", fake_ollama)

    page = CrawledPage(
        source_id="T1",
        source_org="Example University",
        seed_url="https://example.edu/startups",
        page_url="https://example.edu/startups/demo",
        title="Demo",
        markdown="# Demo\nNo startups on this page.",
    )
    extractor = OllamaExtractor(dataclasses.replace(settings, ollama_think=True), model="qwen3:14b")
    batch = extractor.extract(page)

    assert batch.candidates == []
    assert captured["model"] == "qwen3:14b"
    assert captured["stream"] is False
    assert captured["format"]["type"] == "object"
    assert captured["options"]["temperature"] == 0

    # Thinking must follow the setting, not a hard-coded value: with it off, a thinking
    # model answers a dense block with an empty batch instead of extracting it.
    assert captured["think"] is True
    OllamaExtractor(dataclasses.replace(settings, ollama_think=False), model="qwen3:14b").extract(page)
    assert captured["think"] is False


def test_crawl4ai_markdown_result_is_normalized_to_plain_string():
    from startup_sourcing.crawler import Crawl4AICrawler

    class FakeMarkdownGenerationResult:
        fit_markdown = None
        raw_markdown = "# Startup Portfolio\nExample content"
        markdown_with_citations = ""

        def __str__(self):
            return self.raw_markdown

    text = Crawl4AICrawler._markdown_text(FakeMarkdownGenerationResult())
    assert isinstance(text, str)
    assert text.startswith("# Startup Portfolio")


def test_jsonl_export_has_safe_fallback_for_string_compatible_objects(tmp_path: Path):
    import json

    from startup_sourcing.exporter import write_jsonl

    class FakeMarkdownGenerationResult:
        def __str__(self):
            return "# Raw markdown"

    path = tmp_path / "pages.jsonl"
    write_jsonl(path, [{"markdown": FakeMarkdownGenerationResult()}])

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["markdown"] == "# Raw markdown"


def test_website_discovery_recovers_startup_domain_from_source_page():
    from startup_sourcing.website_discovery import discover_startup_website

    markdown = """
    ## SolarSeed Labs
    [SolarSeed Labs](https://solarseed.io) develops agritech tools.
    [LinkedIn](https://www.linkedin.com/company/solarseed)
    """
    url, confidence, method = discover_startup_website(
        "SolarSeed Labs", markdown, "https://university.example/portfolio"
    )
    assert url == "https://solarseed.io"
    assert confidence >= 0.68
    assert method == "source_page_link_match"


def test_contact_enricher_finds_and_classifies_founder_and_company_email():
    from startup_sourcing.contact_enrichment import WebsiteContactEnricher
    from startup_sourcing.crawler import CrawledPage

    class FakeCrawler:
        def crawl_source(self, source, max_pages=1):
            url = source["Seed URL"]
            if url.rstrip("/") == "https://acme.ai":
                markdown = """
                # Acme AI
                Meet our [team](/team) or [contact us](/contact).
                Company LinkedIn: https://www.linkedin.com/company/acme-ai
                """
            elif url.endswith("/team"):
                markdown = """
                # Team
                Jane Tan — Founder & CEO
                Reach Jane at jane.tan@acme.ai
                https://www.linkedin.com/in/jane-tan-founder
                """
            elif url.endswith("/contact"):
                markdown = "Contact us at hello@acme.ai"
            else:
                return []
            return [
                CrawledPage(
                    source_id="T1",
                    source_org="Acme AI",
                    seed_url=url,
                    page_url=url,
                    title="",
                    markdown=markdown,
                )
            ]

    candidate = {
        "startup_name": "Acme AI",
        "website": "https://acme.ai",
        "founders": [{"name": "Jane Tan", "linkedin_url": ""}],
        "source_urls": ["https://university.example/portfolio"],
        "contact_email": "",
    }
    result = WebsiteContactEnricher(FakeCrawler(), max_pages=8).enrich(candidate)
    assert result["founder_email"] == "jane.tan@acme.ai"
    assert result["founder_email_type"] == "founder_named"
    assert result["founder_email_confidence"] >= 0.9
    assert result["company_email"] == "hello@acme.ai"
    assert result["contact_email"] == "jane.tan@acme.ai"
    assert result["founder_linkedin_url"].startswith("https://www.linkedin.com/in/")
    assert len(result["contact_evidence"]) == 2


def test_contact_enricher_handles_obfuscated_public_email():
    from startup_sourcing.contact_enrichment import extract_public_emails

    emails = extract_public_emails("Email: maya.chen [at] novagrid.ai")
    assert emails == ["maya.chen@novagrid.ai"]


def test_live_pipeline_contact_enrichment_outputs_evidence(monkeypatch, tmp_path: Path):
    from startup_sourcing.crawler import CrawledPage
    from startup_sourcing.schemas import CandidateBatch, CandidateExtract, EmailDraft, FounderEvidence

    seed_csv = tmp_path / "seed.csv"
    seed_csv.write_text(
        "ID,Organization / University,Seed URL,Priority (1=highest)\n"
        "T1,Example University,https://university.example/startups,1\n",
        encoding="utf-8",
    )

    class FakeCrawler:
        def crawl_source(self, source, max_pages=1):
            url = source["Seed URL"]
            if "university.example" in url:
                return [
                    CrawledPage(
                        source_id="T1",
                        source_org="Example University",
                        seed_url=url,
                        page_url=url,
                        title="Portfolio",
                        markdown="## Acme AI\n[Acme AI](https://acme.ai) builds AI tools.",
                    )
                ]
            if url.rstrip("/") == "https://acme.ai":
                md = "[Team](/team) [Contact](/contact)"
            elif url.endswith("/team"):
                md = "Founder Jane Tan. Email jane.tan@acme.ai"
            elif url.endswith("/contact"):
                md = "hello@acme.ai"
            else:
                return []
            return [
                CrawledPage(
                    source_id="T1", source_org="Acme AI", seed_url=url,
                    page_url=url, title="", markdown=md,
                )
            ]

    class FakeExtractor:
        def extract(self, page):
            return CandidateBatch(candidates=[
                CandidateExtract(
                    startup_name="Acme AI",
                    description="AI tools",
                    website="",  # forces source-page website recovery
                    founders=[FounderEvidence(name="Jane Tan", status="current_student", evidence="current student")],
                    eligibility_status="qualified",
                    eligibility_confidence=0.95,
                )
            ])

    class FakeDrafter:
        def draft(self, candidate):
            return EmailDraft(subject="Invite", body="Hello")

    monkeypatch.setattr(
        pipeline_module,
        "build_components",
        lambda **kwargs: (FakeCrawler(), FakeExtractor(), FakeDrafter()),
    )

    result = run_pipeline(
        seed_csv=seed_csv,
        output_dir=tmp_path / "out",
        settings=settings,
        mode="live",
        crawler_backend="crawl4ai",
        llm_backend="ollama",
        max_sources=1,
        max_priority=1,
        max_pages_per_source=1,
        enrich_contacts=True,
        contact_max_pages=8,
    )

    candidate = result["candidates"][0]
    assert candidate["website"] == "https://acme.ai"
    assert candidate["founder_email"] == "jane.tan@acme.ai"
    assert candidate["company_email"] == "hello@acme.ai"
    assert result["summary"]["founder_emails_found"] == 1
    assert result["summary"]["company_emails_found"] == 1
    assert result["drafts"][0]["to_email"] == "jane.tan@acme.ai"
    assert (tmp_path / "out" / "contact_evidence.csv").exists()


def test_enrich_uploaded_candidates_with_contact_waterfall(monkeypatch, tmp_path: Path):
    import json

    from startup_sourcing.crawler import CrawledPage
    from startup_sourcing.pipeline import enrich_candidates_with_contacts

    class FakeCrawler:
        def crawl_source(self, source, max_pages=1):
            url = source["Seed URL"]
            if url.rstrip("/") == "https://acme.ai":
                md = "[Team](/team) [Contact](/contact)"
            elif url.endswith("/team"):
                md = "Founder Jane Tan. Email jane.tan@acme.ai"
            elif url.endswith("/contact"):
                md = "hello@acme.ai"
            else:
                return []
            return [
                CrawledPage(
                    source_id="UPLOAD", source_org="Acme AI", seed_url=url,
                    page_url=url, title="", markdown=md,
                )
            ]

    monkeypatch.setattr(pipeline_module, "build_crawler", lambda *args, **kwargs: FakeCrawler())

    rows = [{
        "startup_name": "Acme AI",
        "website": "https://acme.ai",
        "founders": json.dumps([{"name": "Jane Tan", "linkedin_url": ""}]),
        "contact_email": "",
        "source_urls": json.dumps(["https://university.example/portfolio"]),
    }]
    result = enrich_candidates_with_contacts(
        candidates=rows,
        output_dir=tmp_path / "upload",
        settings=settings,
        mode="live",
        crawler_backend="crawl4ai",
        search_backend="none",
    )

    candidate = result["candidates"][0]
    assert candidate["founder_email"] == "jane.tan@acme.ai"
    assert candidate["company_email"] == "hello@acme.ai"
    assert result["summary"]["founder_emails_found"] == 1
    assert (tmp_path / "upload" / "candidates_enriched.csv").exists()
    assert (tmp_path / "upload" / "contact_evidence.csv").exists()


def test_normalize_founders_csv_row_preserves_hunter_inputs():
    from startup_sourcing.pipeline import normalize_candidate_row

    row = {
        "startup_name": "SolarSeed Labs",
        "website": "",
        "official_domain": "",
        "founder_name": "Maya Chen",
        "university": "SMU",
        "founder_status": "current_student",
        "linkedin_url": "https://www.linkedin.com/in/maya-chen-founder/",
        "email": "",
        "startup_eligibility_status": "qualified",
    }
    candidate = normalize_candidate_row(row)

    assert candidate["founders"][0]["name"] == "Maya Chen"
    assert candidate["founders"][0]["linkedin_url"].endswith("/maya-chen-founder/")
    assert candidate["founder_linkedin_url"].endswith("/maya-chen-founder/")
    assert candidate["eligibility_status"] == "qualified"


def test_normalize_founders_csv_row_rebuilds_existing_contact_evidence():
    from startup_sourcing.pipeline import normalize_candidate_row

    candidate = normalize_candidate_row({
        "startup_name": "SolarSeed Labs",
        "founder_name": "Maya Chen",
        "email": "maya@solarseed.io",
        "email_type": "founder_named",
        "email_confidence": "0.91",
        "verification_status": "valid",
        "verification_score": "0.88",
        "provider": "hunter",
        "origin": "provider_email_finder",
        "email_source_url": "https://solarseed.io/team",
    })

    assert candidate["founder_email"] == "maya@solarseed.io"
    assert candidate["contact_email"] == "maya@solarseed.io"
    assert candidate["contact_evidence"] == [{
        "email": "maya@solarseed.io",
        "email_type": "founder_named",
        "confidence": "0.91",
        "verification_status": "valid",
        "verification_score": "0.88",
        "provider": "hunter",
        "origin": "provider_email_finder",
        "source_url": "https://solarseed.io/team",
        "founder_match": "Maya Chen",
    }]


def test_search_enricher_recovers_domain_and_founder_email():
    from startup_sourcing.search_enrichment import SearchContactEnricher, SearchResult
    from startup_sourcing.crawler import CrawledPage

    class FakeSearch:
        calls = 0
        def search(self, query, count=8):
            self.calls += 1
            return [
                SearchResult(
                    title="SolarSeed Labs — Official Site",
                    url="https://solarseed.io",
                    description="Founded by Maya Chen. Contact maya.chen@solarseed.io",
                    extra_snippets=("https://www.linkedin.com/in/maya-chen-founder",),
                )
            ]

    class FakeCrawler:
        def crawl_source(self, source, max_pages=1):
            url = source["Seed URL"]
            return [CrawledPage(
                source_id="SEARCH", source_org="SolarSeed Labs", seed_url=url,
                page_url=url, title="", markdown="Maya Chen, Founder — maya.chen@solarseed.io",
            )]

    candidate = {
        "startup_name": "SolarSeed Labs",
        "website": "",
        "source_urls": ["https://university.example/portfolio"],
        "founders": [{"name": "Maya Chen", "linkedin_url": ""}],
        "contact_email": "",
        "contact_evidence": [],
    }
    enricher = SearchContactEnricher(
        FakeSearch(), FakeCrawler(), max_results=3, max_pages_per_candidate=1,
        queries_per_founder=1, enable_founder_search=True, enable_pdf_search=False,
    )
    result = enricher.enrich(candidate)
    assert result["website"] == "https://solarseed.io"
    assert result["official_domain"] == "solarseed.io"
    assert result["founder_email"] == "maya.chen@solarseed.io"
    assert result["founder_linkedin_url"].startswith("https://www.linkedin.com/in/")
    assert result["search_results_considered"] == 1


def test_search_queries_are_separated_by_company_and_founder_scope():
    from startup_sourcing.search_enrichment import SearchContactEnricher

    candidate = {
        "startup_name": "SolarSeed Labs",
        "official_domain": "solarseed.io",
        "founders": [{"name": "Maya Chen"}],
    }
    company = SearchContactEnricher(
        object(), object(), enable_pdf_search=False, enrichment_scope="company"
    ).build_queries(candidate)
    founder = SearchContactEnricher(
        object(), object(), enable_pdf_search=False, enrichment_scope="founder"
    ).build_queries(candidate)

    assert company == ['"SolarSeed Labs" contact email', "site:solarseed.io email"]
    assert founder
    assert all("Maya Chen" in query for query in founder)
    assert not any("Maya Chen" in query or "founder" in query.lower() for query in company)


def test_paid_provider_scopes_separate_hunter_domain_and_person_calls():
    from startup_sourcing.provider_enrichment import PaidProviderEnricher

    class FakeHunter:
        def __init__(self):
            self.calls = {"domain_search": 0, "email_finder": 0, "email_verifier": 0}
        def domain_search(self, domain, limit=10):
            self.calls["domain_search"] += 1
            return {"data": {"emails": [
                {"value": "hello@acme.ai", "confidence": 90},
                {"value": "jane.tan@acme.ai", "confidence": 95},
            ]}}
        def email_finder(self, **kwargs):
            self.calls["email_finder"] += 1
            return {"data": {"email": "jane.tan@acme.ai", "score": 95}}
        def verify(self, email):
            self.calls["email_verifier"] += 1
            return {"data": {"status": "valid", "score": 100}}

    base = {
        "startup_name": "Acme AI",
        "website": "https://acme.ai",
        "official_domain": "acme.ai",
        "founders": [{"name": "Jane Tan"}],
        "contact_evidence": [],
        "founder_email": "",
        "company_email": "",
    }
    company_hunter = FakeHunter()
    company = PaidProviderEnricher(
        hunter=company_hunter, fallback_only=True, enrichment_scope="company"
    ).enrich({**base, "contact_evidence": []})
    founder_hunter = FakeHunter()
    founder = PaidProviderEnricher(
        hunter=founder_hunter, fallback_only=True, enrichment_scope="founder"
    ).enrich({**base, "contact_evidence": []})

    assert company_hunter.calls["domain_search"] == 1
    assert company_hunter.calls["email_finder"] == 0
    assert company["company_email"] == "hello@acme.ai"
    assert not company.get("founder_email")
    assert founder_hunter.calls["domain_search"] == 0
    assert founder_hunter.calls["email_finder"] == 1
    assert founder["founder_email"] == "jane.tan@acme.ai"


def test_founder_scope_skips_repeated_company_website_crawl():
    from startup_sourcing.contact_waterfall import ContactWaterfall

    class Website:
        calls = 0
        def enrich(self, candidate):
            self.calls += 1
            return candidate

    website = Website()
    result = ContactWaterfall(
        website_enricher=website, enrichment_scope="founder"
    ).enrich({"startup_name": "Acme AI"})

    assert website.calls == 0
    assert result["contact_waterfall_status"] == "no_contact_found"


def test_paid_provider_enricher_hunter_finds_founder_and_verifies():
    from startup_sourcing.provider_enrichment import PaidProviderEnricher

    class FakeHunter:
        def __init__(self):
            self.calls = {"domain_search": 0, "email_finder": 0, "email_verifier": 0}
        def domain_search(self, domain, limit=10):
            self.calls["domain_search"] += 1
            return {"data": {"emails": [{
                "value": "jane.tan@acme.ai",
                "confidence": 96,
                "verification": {"status": "valid"},
                "sources": [{"uri": "https://hunter.example/source"}],
            }]}}
        def email_finder(self, **kwargs):
            self.calls["email_finder"] += 1
            return {"data": {
                "email": "jane.tan@acme.ai", "score": 96,
                "verification": {"status": "valid"},
                "sources": [{"uri": "https://acme.ai/team"}],
            }}
        def verify(self, email):
            self.calls["email_verifier"] += 1
            return {"data": {"status": "valid", "score": 100}}

    candidate = {
        "startup_name": "Acme AI",
        "website": "https://acme.ai",
        "official_domain": "acme.ai",
        "founders": [{"name": "Jane Tan"}],
        "contact_evidence": [],
        "contact_email": "",
        "founder_email": "",
        "company_email": "",
    }
    result = PaidProviderEnricher(
        hunter=FakeHunter(), verify_emails=True, fallback_only=True
    ).enrich(candidate)
    assert result["founder_email"] == "jane.tan@acme.ai"
    evidence = result["contact_evidence"][0]
    assert evidence["provider"] == "hunter"
    assert evidence["verification_status"] == "valid"
    assert evidence["founder_match"] == "Jane Tan"


def test_paid_provider_appends_without_dropping_existing_brave_evidence():
    from startup_sourcing.provider_enrichment import PaidProviderEnricher

    class FakeHunter:
        def __init__(self):
            self.calls = {"domain_search": 0, "email_finder": 0, "email_verifier": 0}
        def domain_search(self, domain, limit=10):
            self.calls["domain_search"] += 1
            return {"data": {"emails": [{
                "value": "jane.tan@acme.ai",
                "confidence": 96,
                "verification": {"status": "valid"},
                "sources": [{"uri": "https://hunter.example/source"}],
            }]}}
        def email_finder(self, **kwargs):
            self.calls["email_finder"] += 1
            return {"data": {
                "email": "jane.tan@acme.ai", "score": 96,
                "verification": {"status": "valid"},
                "sources": [{"uri": "https://hunter.example/source"}],
            }}
        def verify(self, email):
            self.calls["email_verifier"] += 1
            return {"data": {"status": "valid", "score": 100}}

    candidate = {
        "startup_name": "Acme AI",
        "website": "https://acme.ai",
        "official_domain": "acme.ai",
        "founders": [{"name": "Jane Tan"}],
        "contact_email": "jane.tan@acme.ai",
        "founder_email": "",
        "company_email": "",
        "search_queries_run": ['"Acme AI" founder email'],
        "provider_calls": ["brave.search"],
        "contact_evidence": [{
            "email": "jane.tan@acme.ai",
            "email_type": "founder_named",
            "confidence": 0.9,
            "source_url": "https://brave.example/result",
            "founder_match": "Jane Tan",
            "origin": "search_snippet",
            "provider": "brave",
            "verification_status": "unverified",
            "verification_score": 0,
        }],
    }

    result = PaidProviderEnricher(
        hunter=FakeHunter(), verify_emails=True, fallback_only=True
    ).enrich(candidate)

    providers = [row["provider"] for row in result["contact_evidence"]]
    assert "brave" in providers
    assert "hunter" in providers
    assert result["search_queries_run"] == ['"Acme AI" founder email']
    assert "brave.search" in result["provider_calls"]
    assert result["founder_email"] == "jane.tan@acme.ai"
    assert any(
        row["provider"] == "hunter" and row["verification_status"] == "valid"
        for row in result["contact_evidence"]
    )
    assert any(
        row["provider"] == "brave" and row.get("verification_provider") == "hunter"
        for row in result["contact_evidence"]
    )


def test_hunter_email_finder_uses_linkedin_and_company_when_domain_missing():
    from startup_sourcing.provider_enrichment import PaidProviderEnricher

    captured = {}

    class FakeHunter:
        def __init__(self):
            self.calls = {"domain_search": 0, "email_finder": 0, "email_verifier": 0}
        def domain_search(self, *args, **kwargs):
            raise AssertionError("Domain search should not run without a domain")
        def email_finder(self, **kwargs):
            self.calls["email_finder"] += 1
            captured.update(kwargs)
            return {"data": {
                "email": "maya@solarseed.io",
                "score": 87,
                "verification": {"status": "valid"},
                "sources": [{"uri": "https://www.linkedin.com/in/maya-chen-founder"}],
            }}
        def verify(self, email):
            self.calls["email_verifier"] += 1
            return {"data": {"status": "valid", "score": 100}}

    candidate = {
        "startup_name": "SolarSeed Labs",
        "website": "",
        "official_domain": "",
        "founders": [{
            "name": "Maya Chen",
            "linkedin_url": "https://www.linkedin.com/in/maya-chen-founder/",
        }],
        "contact_evidence": [],
        "contact_email": "",
        "founder_email": "",
        "company_email": "",
    }
    result = PaidProviderEnricher(hunter=FakeHunter(), fallback_only=True).enrich(candidate)

    assert captured["full_name"] == "Maya Chen"
    assert captured["company"] == "SolarSeed Labs"
    assert captured["linkedin_handle"] == "maya-chen-founder"
    assert "domain" not in captured or captured["domain"] == ""
    assert result["founder_email"] == "maya@solarseed.io"


def test_hunter_client_retries_rate_limit_403(monkeypatch):
    from startup_sourcing import provider_enrichment
    from startup_sourcing.provider_enrichment import HunterClient

    calls = {"count": 0}

    def fake_json_request(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            raise RuntimeError("HTTP 403 from https://api.hunter.io/v2/email-finder: rate limit")
        return {"data": {"email": "maya@solarseed.io"}}

    monkeypatch.setattr(provider_enrichment, "json_request", fake_json_request)

    client = HunterClient(
        "test-key",
        request_delay_seconds=0,
        retry_attempts=3,
        retry_backoff_seconds=0,
    )
    result = client.email_finder(full_name="Maya Chen", domain="solarseed.io")

    assert calls["count"] == 3
    assert result["data"]["email"] == "maya@solarseed.io"


def test_hunter_email_finder_ignores_placeholder_email_values():
    from startup_sourcing.provider_enrichment import PaidProviderEnricher

    class FakeHunter:
        def __init__(self):
            self.calls = {"domain_search": 0, "email_finder": 0, "email_verifier": 0}
        def domain_search(self, *args, **kwargs):
            self.calls["domain_search"] += 1
            return {"data": {"emails": []}}
        def email_finder(self, **kwargs):
            self.calls["email_finder"] += 1
            return {"data": {
                "email": "none",
                "score": 90,
                "verification": {"status": "unverified"},
            }}
        def verify(self, email):
            self.calls["email_verifier"] += 1
            return {"data": {"status": "valid", "score": 100}}

    candidate = {
        "startup_name": "SolarSeed Labs",
        "website": "https://solarseed.io",
        "official_domain": "solarseed.io",
        "founders": [{"name": "Maya Chen"}],
        "contact_evidence": [],
        "contact_email": "",
        "founder_email": "",
        "company_email": "",
    }

    result = PaidProviderEnricher(hunter=FakeHunter(), fallback_only=False).enrich(candidate)

    assert result["founder_email"] == ""
    assert all(row["email"] != "none" for row in result["contact_evidence"])


def test_paid_provider_fallback_skips_hunter_when_founder_email_already_exists():
    from startup_sourcing.provider_enrichment import PaidProviderEnricher

    class FailHunter:
        calls = {"domain_search": 0, "email_finder": 0, "email_verifier": 0}
        def domain_search(self, *args, **kwargs):
            raise AssertionError("Hunter should not be called")

    candidate = {
        "startup_name": "Acme AI",
        "website": "https://acme.ai",
        "official_domain": "acme.ai",
        "founders": [{"name": "Jane Tan"}],
        "founder_email": "jane.tan@acme.ai",
        "contact_email": "jane.tan@acme.ai",
        "contact_evidence": [{
            "email": "jane.tan@acme.ai", "email_type": "founder_named",
            "confidence": 0.98, "source_url": "https://acme.ai/team",
            "founder_match": "Jane Tan", "origin": "website", "provider": "public_web",
            "verification_status": "unverified", "verification_score": 0,
        }],
    }
    result = PaidProviderEnricher(hunter=FailHunter(), fallback_only=True).enrich(candidate)
    assert result["founder_email"] == "jane.tan@acme.ai"


def test_outlook_graph_client_sends_mail_payload(monkeypatch, tmp_path: Path):
    import sys
    import types
    from types import SimpleNamespace

    from startup_sourcing.outreach import OutlookGraphClient

    captured = {}

    class FakeCache:
        has_state_changed = False
        def deserialize(self, value):
            pass
        def serialize(self):
            return "{}"

    class FakePublicClientApplication:
        def __init__(self, **kwargs):
            captured["msal_kwargs"] = kwargs
        def get_accounts(self):
            return [{"username": "user@example.com"}]
        def acquire_token_silent(self, scopes, account):
            captured["scopes"] = scopes
            captured["account"] = account
            return {"access_token": "token"}

    fake_msal = types.ModuleType("msal")
    fake_msal.SerializableTokenCache = FakeCache
    fake_msal.PublicClientApplication = FakePublicClientApplication
    monkeypatch.setitem(sys.modules, "msal", fake_msal)

    class FakeResponse:
        status_code = 202
        text = ""

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse()

    fake_requests = types.ModuleType("requests")
    fake_requests.post = fake_post
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    cfg = SimpleNamespace(
        outlook_client_id="client-id",
        outlook_tenant_id="common",
        outlook_token_path=tmp_path / "outlook_token_cache.json",
        allow_live_send=True,
    )

    client = OutlookGraphClient(cfg)
    result = client.send("founder@example.com", "Subject", "Body")

    assert result["status_code"] == 202
    assert captured["url"] == "https://graph.microsoft.com/v1.0/me/sendMail"
    assert captured["headers"]["Authorization"] == "Bearer token"
    assert captured["json"]["message"]["toRecipients"][0]["emailAddress"]["address"] == "founder@example.com"
    assert captured["json"]["message"]["subject"] == "Subject"
    assert captured["json"]["message"]["body"]["content"] == "Body"
    assert captured["json"]["saveToSentItems"] is True


def test_founder_rows_are_person_level_and_keep_provider_metadata():
    from startup_sourcing.founder_records import build_founder_rows

    candidates = [{
        "startup_name": "Acme AI",
        "website": "https://acme.ai",
        "official_domain": "acme.ai",
        "eligibility_status": "qualified",
        "priority_score": 91,
        "founders": [
            {"name": "Jane Tan", "university": "SMU", "status": "current_student", "graduation_year": None, "evidence": "student", "linkedin_url": ""},
            {"name": "Alex Lim", "university": "SMU", "status": "recent_graduate", "graduation_year": 2024, "evidence": "graduated 2024", "linkedin_url": ""},
        ],
        "contact_evidence": [{
            "email": "jane@acme.ai", "email_type": "founder_named", "confidence": 0.95,
            "source_url": "https://acme.ai/team", "founder_match": "Jane Tan",
            "provider": "public_web", "origin": "website",
            "verification_status": "valid", "verification_score": 1.0,
        }],
    }]
    rows = build_founder_rows(candidates)
    assert len(rows) == 2
    jane = next(r for r in rows if r["founder_name"] == "Jane Tan")
    alex = next(r for r in rows if r["founder_name"] == "Alex Lim")
    assert jane["email"] == "jane@acme.ai"
    assert jane["verification_status"] == "valid"
    assert alex["email"] == ""
