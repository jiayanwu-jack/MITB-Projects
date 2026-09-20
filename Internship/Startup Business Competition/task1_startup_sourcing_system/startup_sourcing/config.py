from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _project_path_env(name: str, default: str) -> Path:
    path = Path(os.getenv(name, default))
    return path if path.is_absolute() else PROJECT_ROOT / path


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT

    # Primary pipeline backends.
    crawler_backend: str = os.getenv("CRAWLER_BACKEND", "firecrawl").lower()
    llm_backend: str = os.getenv("LLM_BACKEND", "ollama").lower()

    # *_max_input_chars is the per-call CHUNK size, not a cap on how much of a page is
    # read: pages longer than this are split and every chunk is extracted. Keep each
    # value under the backend's measured failure point — gemini-2.5-flash times out
    # generating the JSON for a 60k chunk, and qwen3.5:9b returns an empty batch past
    # ~26k chars.

    # OpenAI
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5.5")
    openai_max_input_chars: int = int(os.getenv("OPENAI_MAX_INPUT_CHARS", "20000"))

    # DeepSeek (OpenAI-compatible chat-completions API, base_url api.deepseek.com).
    # The API serves deepseek-v4-flash and deepseek-v4-pro; the older deepseek-chat
    # alias is not in the model list. Flash is the cheaper/faster default.
    deepseek_api_key: str | None = os.getenv("DEEPSEEK_API_KEY")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    deepseek_max_input_chars: int = int(os.getenv("DEEPSEEK_MAX_INPUT_CHARS", "20000"))

    # Google AI Studio / Gemini API
    google_api_key: str | None = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    google_model: str = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")
    google_max_input_chars: int = int(os.getenv("GOOGLE_MAX_INPUT_CHARS", "15000"))

    # Anthropic Claude API
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-latest")
    claude_max_input_chars: int = int(os.getenv("CLAUDE_MAX_INPUT_CHARS", "60000"))

    # Ollama local inference
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen3:14b")
    ollama_max_input_chars: int = int(os.getenv("OLLAMA_MAX_INPUT_CHARS", "24000"))
    ollama_num_ctx: int = int(os.getenv("OLLAMA_NUM_CTX", "0"))
    # Thinking models need room to reason before emitting schema-constrained JSON. With it
    # off, qwen3.5:9b answers a 20k roster block with '{"candidates": []}' in 3s; with it on,
    # the same block yields 30 startups. Costs roughly 1.7x the time on blocks that already
    # worked. Set OLLAMA_THINK=false to trade completeness for speed.
    ollama_think: bool = _env_bool("OLLAMA_THINK", True)

    # Firecrawl
    firecrawl_api_key: str | None = os.getenv("FIRECRAWL_API_KEY")

    # Crawl4AI browser/deep-crawl settings
    crawl4ai_max_depth: int = int(os.getenv("CRAWL4AI_MAX_DEPTH", "1"))
    crawl4ai_page_timeout_ms: int = int(os.getenv("CRAWL4AI_PAGE_TIMEOUT_MS", "60000"))
    crawl4ai_headless: bool = _env_bool("CRAWL4AI_HEADLESS", True)

    # Contact website crawl
    contact_max_pages: int = int(os.getenv("CONTACT_MAX_PAGES", "8"))

    # Search enrichment. Backends: none | brave | tavily.
    search_backend: str = os.getenv("SEARCH_BACKEND", "none").lower()
    brave_search_api_key: str | None = os.getenv("BRAVE_SEARCH_API_KEY")
    tavily_api_key: str | None = os.getenv("TAVILY_API_KEY")
    search_max_results: int = int(os.getenv("SEARCH_MAX_RESULTS", "8"))
    search_max_pages_per_candidate: int = int(os.getenv("SEARCH_MAX_PAGES_PER_CANDIDATE", "6"))
    search_queries_per_founder: int = int(os.getenv("SEARCH_QUERIES_PER_FOUNDER", "4"))
    enable_founder_search: bool = _env_bool("ENABLE_FOUNDER_SEARCH", True)
    enable_pdf_search: bool = _env_bool("ENABLE_PDF_SEARCH", True)

    # Paid provider waterfall. Values: none/hunter and none/apollo.
    email_provider: str = os.getenv("EMAIL_PROVIDER", "none").lower()
    people_provider: str = os.getenv("PEOPLE_PROVIDER", "none").lower()
    hunter_api_key: str | None = os.getenv("HUNTER_API_KEY")
    apollo_api_key: str | None = os.getenv("APOLLO_API_KEY")
    verify_emails: bool = _env_bool("VERIFY_EMAILS", False)
    paid_fallback_only: bool = _env_bool("PAID_FALLBACK_ONLY", True)
    hunter_request_delay_seconds: float = float(os.getenv("HUNTER_REQUEST_DELAY_SECONDS", "0.25"))
    hunter_retry_attempts: int = int(os.getenv("HUNTER_RETRY_ATTEMPTS", "3"))
    hunter_retry_backoff_seconds: float = float(os.getenv("HUNTER_RETRY_BACKOFF_SECONDS", "2"))
    apollo_reveal_personal_emails: bool = _env_bool("APOLLO_REVEAL_PERSONAL_EMAILS", False)
    apollo_run_waterfall_email: bool = _env_bool("APOLLO_RUN_WATERFALL_EMAIL", False)
    apollo_poll_attempts: int = int(os.getenv("APOLLO_POLL_ATTEMPTS", "2"))
    apollo_poll_delay_seconds: float = float(os.getenv("APOLLO_POLL_DELAY_SECONDS", "2"))

    competition_name: str = os.getenv(
        "COMPETITION_NAME", "13th Lee Kuan Yew Global Business Plan Competition"
    )
    competition_year: int = int(os.getenv("COMPETITION_YEAR", "2027"))
    organizer_name: str = os.getenv(
        "ORGANIZER_NAME", "SMU Institute of Innovation & Entrepreneurship"
    )
    organizer_signature: str = os.getenv(
        "ORGANIZER_SIGNATURE",
        "Institute of Innovation & Entrepreneurship\nSingapore Management University",
    )
    competition_url: str = os.getenv("COMPETITION_URL", "https://lkygbpc.smu.edu.sg/")
    allow_live_send: bool = _env_bool("ALLOW_LIVE_SEND", False)
    gmail_credentials_path: Path = _project_path_env(
        "GMAIL_CREDENTIALS_PATH", "credentials.json"
    )
    gmail_token_path: Path = _project_path_env("GMAIL_TOKEN_PATH", "token.json")
    outlook_client_id: str | None = os.getenv("OUTLOOK_CLIENT_ID")
    outlook_tenant_id: str = os.getenv("OUTLOOK_TENANT_ID", "common")
    outlook_token_path: Path = _project_path_env(
        "OUTLOOK_TOKEN_PATH", "outlook_token_cache.json"
    )

    # Direct SMTP sending - needs NO OAuth app, API approval, or Azure registration.
    # Easiest setup: a personal Gmail App Password (smtp.gmail.com:587). Any SMTP relay
    # works too. SMTP_USER is the full email address; SMTP_PASSWORD is the app password.
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str | None = os.getenv("SMTP_USER")
    smtp_password: str | None = os.getenv("SMTP_PASSWORD")
    smtp_from: str | None = os.getenv("SMTP_FROM")          # defaults to SMTP_USER
    smtp_use_ssl: bool = _env_bool("SMTP_USE_SSL", False)   # False = STARTTLS (587); True/port 465 = SSL

    @property
    def graduation_cutoff_year(self) -> int:
        return self.competition_year - 5


settings = Settings()
