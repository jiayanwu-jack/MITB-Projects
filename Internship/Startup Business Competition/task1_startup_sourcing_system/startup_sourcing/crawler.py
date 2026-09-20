from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Protocol, TypeVar

from .config import Settings


@dataclass
class CrawledPage:
    source_id: str
    source_org: str
    seed_url: str
    page_url: str
    title: str
    markdown: str


class Crawler(Protocol):
    def crawl_source(self, source: dict, max_pages: int = 5) -> list[CrawledPage]: ...


T = TypeVar("T")


def _run_async(awaitable: Awaitable[T]) -> T:
    """Run an async Crawl4AI task safely from CLI or UI code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result: list[T] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(awaitable))
        except BaseException as exc:  # propagate from worker thread
            error.append(exc)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


class MockCrawler:
    """Deterministic synthetic pages for demonstrations and tests."""

    def crawl_source(self, source: dict, max_pages: int = 5) -> list[CrawledPage]:
        source_id = source.get("ID", "MOCK")
        org = source.get("Organization / University", "Example University")
        seed_url = source.get("Seed URL", "https://example.edu/startups")
        n = min(max_pages, 2)
        pages: list[CrawledPage] = []
        templates = [
            f"""# 2027 Venture Showcase — Synthetic Demonstration Page

## NovaGrid Analytics
NovaGrid Analytics builds AI forecasting tools for campus microgrids. The company was founded by Maya Chen, a current master's student at {org}. Website: https://novagrid.example. Contact: hello@novagrid.example.

## AquaLoop Labs
AquaLoop Labs develops low-cost water reuse monitoring. Founder Arjun Mehta graduated from {org} in 2024. Website: https://aqualoop.example. Contact: team@aqualoop.example.

## FinLoop Pay
FinLoop Pay is a fintech platform for cross-border student payments, based in Singapore. Founder Priya Raman is a current master's student at {org}. Website: https://finloop.example. Contact: priya.raman@finloop.example.

## MediSync Health
MediSync Health builds an AI health records assistant for clinics, based in United Kingdom. Founder James Bennett graduated from {org} in 2024. Website: https://medisync.example. Contact: james.bennett@medisync.example.

## CargoWise Robotics
CargoWise Robotics develops autonomous warehouse logistics robots, based in Australia. Founder Daniel Wong is a current student at {org}. Website: https://cargowise.example. Contact: daniel.wong@cargowise.example.

This entire page is synthetic test content generated for the pipeline demo.""",
            f"""# Alumni Venture Notes — Synthetic Demonstration Page

## Heritage Systems
Heritage Systems provides enterprise document management software. Founder Lena Ortiz graduated from {org} in 2015. Website: https://heritage.example.

## Orbital Orchard
Orbital Orchard is exploring robotic crop monitoring for tropical farms. The team page names founder Daniel Lim and says he is an alumnus of {org}, but gives no graduation year. Website: https://orbitalorchard.example.

This entire page is synthetic test content generated for the pipeline demo.""",
        ]
        for i in range(n):
            pages.append(
                CrawledPage(
                    source_id=source_id,
                    source_org=org,
                    seed_url=seed_url,
                    page_url=f"{seed_url.rstrip('/')}/mock-page-{i+1}",
                    title=f"Synthetic startup page {i+1}",
                    markdown=templates[i],
                )
            )
        return pages


class FirecrawlCrawler:
    def __init__(self, settings: Settings):
        if not settings.firecrawl_api_key:
            raise ValueError("FIRECRAWL_API_KEY is required when crawler_backend=firecrawl.")
        from firecrawl import Firecrawl

        self.client = Firecrawl(api_key=settings.firecrawl_api_key)

    @staticmethod
    def _doc_attr(doc: Any, name: str, default: Any = "") -> Any:
        value = getattr(doc, name, None)
        return value if value is not None else default

    def crawl_source(self, source: dict, max_pages: int = 5) -> list[CrawledPage]:
        seed_url = source["Seed URL"]
        source_id = source.get("ID", "")
        source_org = source.get("Organization / University", "")
        result = self.client.crawl(
            url=seed_url,
            limit=max_pages,
            scrape_options={"formats": ["markdown"], "only_main_content": True},
        )
        documents = getattr(result, "data", None) or []
        pages: list[CrawledPage] = []
        for doc in documents:
            metadata = getattr(doc, "metadata", None) or {}
            if hasattr(metadata, "model_dump"):
                metadata = metadata.model_dump()
            page_url = metadata.get("sourceURL") or metadata.get("source_url") or seed_url
            title = metadata.get("title", "")
            markdown = self._doc_attr(doc, "markdown", "")
            if markdown:
                pages.append(
                    CrawledPage(
                        source_id=source_id,
                        source_org=source_org,
                        seed_url=seed_url,
                        page_url=page_url,
                        title=title,
                        markdown=markdown,
                    )
                )
        return pages


class Crawl4AICrawler:
    """Local browser-based crawler using Crawl4AI bounded BFS deep crawling."""

    def __init__(self, settings: Settings):
        self.settings = settings
        try:
            import crawl4ai  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "Crawl4AI is not installed. Run `pip install -r requirements.txt` and `crawl4ai-setup`."
            ) from exc

    @staticmethod
    def _markdown_text(value: Any) -> str:
        """Normalize Crawl4AI markdown outputs to a plain Python string.

        Crawl4AI versions may expose ``result.markdown`` as either a string-like
        value or a ``MarkdownGenerationResult`` Pydantic model.  Never let the
        latter escape this adapter, because downstream JSONL export and LLM
        clients expect plain text.
        """
        if value is None:
            return ""

        # Prefer explicit Crawl4AI fields before generic string conversion.
        for attr in ("fit_markdown", "raw_markdown", "markdown_with_citations"):
            candidate = getattr(value, attr, None)
            if isinstance(candidate, str) and candidate.strip():
                return candidate

        # Some Pydantic-backed versions/wrappers expose the fields only through
        # model_dump().  Handle those without importing Crawl4AI model classes.
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                data = model_dump()
            except Exception:
                data = None
            if isinstance(data, dict):
                for key in ("fit_markdown", "raw_markdown", "markdown_with_citations"):
                    candidate = data.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        return candidate

        if isinstance(value, str):
            return value

        # MarkdownGenerationResult.__str__ returns raw_markdown in current
        # Crawl4AI releases.  This is the final compatibility fallback.
        return str(value)

    async def _crawl_source_async(self, source: dict, max_pages: int) -> list[CrawledPage]:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
        from crawl4ai.deep_crawling import BFSDeepCrawlStrategy

        seed_url = source["Seed URL"]
        source_id = source.get("ID", "")
        source_org = source.get("Organization / University", "")

        browser_config = BrowserConfig(
            headless=self.settings.crawl4ai_headless,
            text_mode=True,
            verbose=False,
        )
        run_config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            deep_crawl_strategy=BFSDeepCrawlStrategy(
                max_depth=self.settings.crawl4ai_max_depth,
                include_external=False,
                max_pages=max_pages,
            ),
            stream=False,
            verbose=False,
            page_timeout=self.settings.crawl4ai_page_timeout_ms,
        )

        async with AsyncWebCrawler(config=browser_config) as crawler:
            results = await crawler.arun(url=seed_url, config=run_config)

        if not isinstance(results, (list, tuple)):
            results = [results]

        pages: list[CrawledPage] = []
        for result in results:
            if getattr(result, "success", True) is False:
                continue
            status_code = getattr(result, "status_code", None)
            if isinstance(status_code, int) and status_code >= 400:
                continue
            markdown = self._markdown_text(getattr(result, "markdown", ""))
            if not markdown.strip():
                continue
            metadata = getattr(result, "metadata", None) or {}
            title = metadata.get("title", "") if isinstance(metadata, dict) else ""
            page_url = getattr(result, "url", None) or seed_url
            pages.append(
                CrawledPage(
                    source_id=source_id,
                    source_org=source_org,
                    seed_url=seed_url,
                    page_url=page_url,
                    title=title,
                    markdown=markdown,
                )
            )
        return pages[:max_pages]

    def crawl_source(self, source: dict, max_pages: int = 5) -> list[CrawledPage]:
        return _run_async(self._crawl_source_async(source, max_pages))
