from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod

from .config import Settings
from .crawler import CrawledPage
from .schemas import CandidateBatch, CandidateExtract, FounderEvidence

logger = logging.getLogger(__name__)

# A page that carries at least this much text but yields no candidates is reported
# rather than passed off as a genuinely empty roster.
EMPTY_RESULT_WARN_CHARS = 4000

_EXTRACT_ATTEMPTS = 2

# Models sometimes return an empty batch for a block they will happily extract once it is
# split — qwen3.5:9b returns nothing for a 20k roster block in ~1s, yet yields 22 startups
# from the same text in 5k pieces. It is not size alone (4,478 and 4,708 char blocks worked
# while a 4,651 char one did not) and not the content, which is ordinary startup blurbs. So
# an empty result on text that still looks like a roster is retried by halving it.
_SUBDIVIDE_FLOOR_CHARS = 1200
_SUBDIVIDE_MAX_DEPTH = 4

# Rosters are not all shaped alike, so structure is detected from any of these rather than
# one site's convention: SMU uses '## [Name](url)' entries, Tsinghua x-lab lists teams as
# '###' sections and link carousels, others use plain bullet lists.
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+\S")
_LINK_RE = re.compile(r"\[[^\]]+\]\([^)]+\)")
_LIST_ITEM_RE = re.compile(r"(?m)^\s*(?:[-*+]|\d+\.)\s+\S")
_MIN_ROSTER_LINKS = 8


def _looks_extractable(text: str) -> bool:
    """True when text still carries roster-like structure, so an empty result is suspicious.

    Deliberately generous: a false positive costs one extra call that returns empty in about
    a second, while a false negative silently drops every startup on the page.
    """
    body = text.strip()
    if len(body) < _SUBDIVIDE_FLOOR_CHARS:
        return False
    if _HEADING_RE.search(body):
        return True
    if len(_LINK_RE.findall(body)) >= _MIN_ROSTER_LINKS:
        return True
    return len(_LIST_ITEM_RE.findall(body)) >= _MIN_ROSTER_LINKS


def split_markdown(markdown: str, max_chars: int) -> list[str]:
    """Split page markdown into <= max_chars pieces, breaking on markdown headings.

    Roster pages give each startup its own heading, so breaking there keeps a startup's
    name, blurb and founders together in one piece. Any heading level is a boundary —
    depth varies by site ('##' at SMU, '###' at Tsinghua x-lab). Sending a whole page in
    one call is not viable: Gemini times out generating the resulting giant JSON, and
    qwen3.5:9b silently returns an empty batch once the input passes ~26k chars.
    """
    if max_chars <= 0 or len(markdown) <= max_chars:
        return [markdown]

    sections = re.split(r"(?m)(?=^#{1,6}\s+\S)", markdown)
    chunks: list[str] = []
    current = ""
    for section in sections:
        if len(section) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(section), max_chars):
                chunks.append(section[i:i + max_chars])
            continue
        if current and len(current) + len(section) > max_chars:
            chunks.append(current)
            current = ""
        current += section
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()] or [markdown[:max_chars]]


def _is_richer(new: CandidateExtract, old: CandidateExtract) -> bool:
    """Prefer the copy of a repeated startup that carries more evidence."""
    if len(new.founders or []) != len(old.founders or []):
        return len(new.founders or []) > len(old.founders or [])
    return len(new.description or "") > len(old.description or "")


class CandidateExtractor(ABC):
    """Extracts candidates from a page, one chunk at a time.

    Subclasses implement `_extract_once` for a single model call; this base class
    handles splitting, retries, merging and the empty-result warning.
    """

    settings: Settings

    @property
    def _max_input_chars(self) -> int:
        """Chunk size for this backend — must stay under the backend's failure point."""
        return 20000

    @abstractmethod
    def _extract_once(self, page: CrawledPage, text: str) -> CandidateBatch: ...

    def _call_with_retry(self, page: CrawledPage, text: str,
                         failures: list[Exception]) -> CandidateBatch | None:
        for attempt in range(1, _EXTRACT_ATTEMPTS + 1):
            try:
                return self._extract_once(page, text)
            except Exception as exc:
                if attempt == _EXTRACT_ATTEMPTS:
                    failures.append(exc)
                    logger.warning("extract failed on %d chars of %s: %s", len(text), page.page_url, exc)
                else:
                    logger.info("retrying %d chars of %s after: %s", len(text), page.page_url, exc)
        return None

    def _extract_recursive(self, page: CrawledPage, text: str,
                           failures: list[Exception], depth: int = 0) -> list[CandidateExtract]:
        """Extract one chunk, halving it and retrying if it comes back empty."""
        batch = self._call_with_retry(page, text, failures)
        if batch is not None and batch.candidates:
            return list(batch.candidates)

        # Empty (or failed). If it still looks like a roster, the block itself is the
        # problem — split it and try the pieces.
        if depth >= _SUBDIVIDE_MAX_DEPTH or not _looks_extractable(text):
            if batch is not None and not batch.candidates and len(text.strip()) >= EMPTY_RESULT_WARN_CHARS:
                logger.warning("giving up on %d chars of %s after %d subdivisions",
                               len(text), page.page_url, depth)
            return []

        halves = split_markdown(text, max(len(text) // 2, _SUBDIVIDE_FLOOR_CHARS))
        if len(halves) < 2:
            return []
        logger.info("empty result for %d chars of %s — subdividing into %d (depth %d)",
                    len(text), page.page_url, len(halves), depth + 1)
        found: list[CandidateExtract] = []
        for half in halves:
            found.extend(self._extract_recursive(page, half, failures, depth + 1))
        return found

    def extract(self, page: CrawledPage) -> CandidateBatch:
        chunks = split_markdown(page.markdown, self._max_input_chars)
        merged: dict[str, CandidateExtract] = {}
        failures: list[Exception] = []

        for chunk in chunks:
            for candidate in self._extract_recursive(page, chunk, failures):
                key = (candidate.startup_name or "").strip().lower()
                if not key:
                    continue
                existing = merged.get(key)
                if existing is None or _is_richer(candidate, existing):
                    merged[key] = candidate

        # Nothing extracted and every call errored is a real failure, not an empty page.
        if failures and not merged:
            raise failures[-1]

        candidates = list(merged.values())
        if len(chunks) > 1:
            logger.info("extracted %d candidates from %s across %d chunks",
                        len(candidates), page.page_url, len(chunks))
        return CandidateBatch(candidates=candidates)


class MockExtractor(CandidateExtractor):
    """Rule-based extractor for the synthetic mock pages only."""

    startup_re = re.compile(r"^##\s+(.+)$", re.MULTILINE)
    email_re = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
    website_re = re.compile(r"https?://[^\s.]+(?:\.[^\s.]+)+")

    def __init__(self, settings: Settings):
        self.settings = settings

    def _extract_once(self, page: CrawledPage, text: str) -> CandidateBatch:
        chunks = re.split(r"^##\s+", text, flags=re.MULTILINE)[1:]
        candidates: list[CandidateExtract] = []
        for chunk in chunks:
            lines = chunk.splitlines()
            name = lines[0].strip()
            text = " ".join(lines[1:]).strip()
            emails = self.email_re.findall(text)
            websites = re.findall(r"https?://[\w.-]+(?:/[\w./-]*)?", text)
            founder_name = ""
            founder_match = re.search(r"(?:founded by|Founder)\s+([A-Z][A-Za-z-]+\s+[A-Z][A-Za-z-]+)", text)
            if founder_match:
                founder_name = founder_match.group(1)
            year_match = re.search(r"graduated from .*? in (20\d{2})", text)
            year = int(year_match.group(1)) if year_match else None
            country_match = re.search(r"based in ([A-Z][A-Za-z ]+?)\s*[.,]", text)
            country = country_match.group(1).strip() if country_match else ""
            status = "unknown"
            if "current master's student" in text or "current student" in text:
                status = "current_student"
            elif year is not None and year >= self.settings.graduation_cutoff_year:
                status = "recent_graduate"
            elif year is not None:
                status = "older_graduate"

            if status in {"current_student", "recent_graduate"}:
                eligibility = "qualified"
                confidence = 0.96 if status == "current_student" else 0.92
            elif status == "older_graduate":
                eligibility = "not_qualified"
                confidence = 0.95
            else:
                eligibility = "review"
                confidence = 0.45

            candidates.append(
                CandidateExtract(
                    startup_name=name,
                    description=text[:500],
                    industry=(
                        "Energy AI" if "microgrid" in text else
                        "Climate / Water" if "water reuse" in text else
                        "AgriTech" if "crop" in text else
                        "FinTech" if "fintech" in text else
                        "HealthTech" if "health" in text else
                        "Logistics Tech" if "logistics" in text else
                        "Enterprise Software"
                    ),
                    country=country,
                    website=websites[0].rstrip(".") if websites else "",
                    contact_email=emails[0] if emails else "",
                    founders=[
                        FounderEvidence(
                            name=founder_name,
                            university=page.source_org,
                            status=status,
                            graduation_year=year,
                            evidence=text[:350],
                        )
                    ],
                    eligibility_status=eligibility,
                    eligibility_confidence=confidence,
                    eligibility_reason=f"Mock extraction found founder status: {status}.",
                    innovation_score=8 if name in {"NovaGrid Analytics", "AquaLoop Labs"} else 6,
                    scalability_score=8 if name == "NovaGrid Analytics" else 7,
                    international_potential_score=8 if name != "Heritage Systems" else 5,
                    competition_fit_score=9 if eligibility == "qualified" else 5,
                    evidence_quotes=[text[:280]],
                )
            )
        return CandidateBatch(candidates=candidates)


def _candidate_system_prompt(settings: Settings) -> str:
    cutoff = settings.graduation_cutoff_year
    return f"""You extract candidate startups from public web pages for {settings.competition_name}.
Eligibility rule: a startup qualifies when at least one founder is a current university/college student, or graduated in {cutoff} or later for the {settings.competition_year} competition.
Use only evidence in the supplied page. Never invent founder status, graduation year, email, website, country, university, or LinkedIn URL. Only include a founder's linkedin_url if the exact URL appears in the supplied text; otherwise leave it as an empty string. Never guess or construct a LinkedIn URL from a person's name.
When evidence is missing, use empty strings, null graduation year, founder status 'unknown', and eligibility_status 'review'.
Evidence quotes must be short excerpts from the supplied text. Scores are 1-10 screening judgments, not eligibility evidence.
Return every startup that the page clearly identifies, but ignore organizations that are only sponsors, investors, universities, or service providers."""


def _extract_json_payload(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def _candidate_schema_instruction() -> str:
    return (
        "Return only valid JSON matching this schema exactly:\n"
        f"{json.dumps(CandidateBatch.model_json_schema(), ensure_ascii=False)}"
    )


class OpenAIExtractor(CandidateExtractor):
    def __init__(self, settings: Settings):
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when llm_backend=openai.")
        from openai import OpenAI

        self.client = OpenAI(api_key=settings.openai_api_key)
        self.model = settings.openai_model
        self.settings = settings

    @property
    def _max_input_chars(self) -> int:
        return self.settings.openai_max_input_chars

    def _extract_once(self, page: CrawledPage, text: str) -> CandidateBatch:
        system = _candidate_system_prompt(self.settings)
        user = f"SOURCE URL: {page.page_url}\nSOURCE ORGANIZATION: {page.source_org}\n\nPAGE CONTENT:\n{text}"
        response = self.client.responses.parse(
            model=self.model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            text_format=CandidateBatch,
        )
        parsed = response.output_parsed
        if parsed is None:
            return CandidateBatch(candidates=[])
        return parsed


class DeepSeekExtractor(CandidateExtractor):
    """Structured extraction through DeepSeek's OpenAI-compatible chat API.

    DeepSeek reuses the OpenAI SDK with a different base_url. It supports JSON
    mode (response_format={"type":"json_object"}) but not strict json_schema,
    so the schema travels in the prompt and the JSON payload is validated on
    return — the same contract the Gemini/Claude backends already use.
    """

    def __init__(self, settings: Settings, model: str | None = None):
        if not settings.deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY is required when llm_backend=deepseek.")
        from openai import OpenAI

        self.settings = settings
        self.model = model or settings.deepseek_model
        self.client = OpenAI(
            api_key=settings.deepseek_api_key,
            base_url="https://api.deepseek.com/v1",
        )

    @property
    def _max_input_chars(self) -> int:
        return self.settings.deepseek_max_input_chars

    def _extract_once(self, page: CrawledPage, text: str) -> CandidateBatch:
        user = (
            f"SOURCE URL: {page.page_url}\n"
            f"SOURCE ORGANIZATION: {page.source_org}\n\n"
            f"{_candidate_schema_instruction()}\n\n"
            f"PAGE CONTENT:\n{text}"
        )
        completion = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _candidate_system_prompt(self.settings)},
                {"role": "user", "content": user},
            ],
        )
        content = completion.choices[0].message.content or ""
        if not content.strip():
            return CandidateBatch(candidates=[])
        return CandidateBatch.model_validate_json(_extract_json_payload(content))


class GoogleAIStudioExtractor(CandidateExtractor):
    """Structured extraction through the Google AI Studio Gemini API."""

    API_BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, settings: Settings, model: str | None = None):
        if not settings.google_api_key:
            raise ValueError("GOOGLE_API_KEY is required when llm_backend=google.")
        self.settings = settings
        self.api_key = settings.google_api_key
        self.model = model or settings.google_model

    @staticmethod
    def _model_path(model: str) -> str:
        return model if model.startswith("models/") else f"models/{model}"

    @property
    def _max_input_chars(self) -> int:
        return self.settings.google_max_input_chars

    def _extract_once(self, page: CrawledPage, text: str) -> CandidateBatch:
        import requests

        user = (
            f"SOURCE URL: {page.page_url}\n"
            f"SOURCE ORGANIZATION: {page.source_org}\n\n"
            f"{_candidate_schema_instruction()}\n\n"
            f"PAGE CONTENT:\n{text}"
        )
        response = requests.post(
            f"{self.API_BASE}/{self._model_path(self.model)}:generateContent",
            params={"key": self.api_key},
            json={
                "systemInstruction": {
                    "parts": [{"text": _candidate_system_prompt(self.settings)}],
                },
                "contents": [
                    {"role": "user", "parts": [{"text": user}]},
                ],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                },
            },
            timeout=90,
        )
        response.raise_for_status()
        payload = response.json()
        parts = (
            payload.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [])
        )
        text = "\n".join(str(part.get("text", "")) for part in parts if part.get("text"))
        if not text.strip():
            return CandidateBatch(candidates=[])
        return CandidateBatch.model_validate_json(_extract_json_payload(text))


class ClaudeExtractor(CandidateExtractor):
    """Structured extraction through Anthropic Claude's Messages API."""

    API_URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, settings: Settings, model: str | None = None):
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required when llm_backend=claude.")
        self.settings = settings
        self.api_key = settings.anthropic_api_key
        self.model = model or settings.claude_model

    @property
    def _max_input_chars(self) -> int:
        return self.settings.claude_max_input_chars

    def _extract_once(self, page: CrawledPage, text: str) -> CandidateBatch:
        import requests

        user = (
            f"SOURCE URL: {page.page_url}\n"
            f"SOURCE ORGANIZATION: {page.source_org}\n\n"
            f"{_candidate_schema_instruction()}\n\n"
            f"PAGE CONTENT:\n{text}"
        )
        response = requests.post(
            self.API_URL,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 4096,
                "temperature": 0,
                "system": _candidate_system_prompt(self.settings),
                "messages": [{"role": "user", "content": user}],
            },
            timeout=90,
        )
        response.raise_for_status()
        payload = response.json()
        text = "\n".join(
            str(block.get("text", ""))
            for block in payload.get("content", [])
            if block.get("type") == "text" and block.get("text")
        )
        if not text.strip():
            return CandidateBatch(candidates=[])
        return CandidateBatch.model_validate_json(_extract_json_payload(text))


class OllamaExtractor(CandidateExtractor):
    """Structured local extraction through the Ollama Python client."""

    def __init__(self, settings: Settings, model: str | None = None):
        from ollama import Client

        self.settings = settings
        self.model = model or settings.ollama_model
        self.client = Client(host=settings.ollama_host)

    @property
    def _max_input_chars(self) -> int:
        return self.settings.ollama_max_input_chars

    def _extract_once(self, page: CrawledPage, text: str) -> CandidateBatch:
        schema = CandidateBatch.model_json_schema()
        system = _candidate_system_prompt(self.settings)
        user = (
            f"SOURCE URL: {page.page_url}\n"
            f"SOURCE ORGANIZATION: {page.source_org}\n\n"
            f"Return JSON matching this schema exactly:\n{json.dumps(schema)}\n\n"
            f"PAGE CONTENT:\n{text}"
        )
        options: dict[str, int | float] = {"temperature": 0}
        if self.settings.ollama_num_ctx > 0:
            options["num_ctx"] = self.settings.ollama_num_ctx
        response = self.client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            format=schema,
            options=options,
            think=self.settings.ollama_think,
            stream=False,
        )
        content = response.message.content
        return CandidateBatch.model_validate_json(content)
