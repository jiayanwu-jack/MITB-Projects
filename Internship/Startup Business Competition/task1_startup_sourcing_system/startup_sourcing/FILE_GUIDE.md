# `startup_sourcing` — File-by-File Guide

This document explains, in detail, what every file in the
`task1_startup_sourcing_system/startup_sourcing/` package does.

The package is a **university-startup sourcing pipeline**. Given a seed list of universities
/ organisations, it crawls their web pages, uses an LLM to extract candidate student-founded
startups, decides which are **eligible** for a competition, finds the **founders' contact
details** through a multi-stage "waterfall", and finally drafts and (optionally) **sends
invitation emails**.

---

## 1. The big picture — how the files fit together

```
seed_loader ─► crawler ─► extractor ─► website_discovery ─► eligibility ─► dedupe
   (input)     (fetch)    (LLM parse)   (find real site)    (score/gate)   (merge)
                                                                              │
                                                                              ▼
                                                   contact_waterfall  (find founder emails)
                                                   ├─ 1. contact_enrichment  (crawl own site)   ── free
                                                   ├─ 2. search_enrichment   (Brave + PDFs)      ── free
                                                   └─ 3. provider_enrichment (Hunter, Apollo)    ── paid
                                                                              │
                                                                              ▼
                                            outreach (draft email) ─► exporter / founder_records
                                                                        (write CSV / JSON output)
```

`pipeline.py` is the conductor that calls all of the above in order. `config.py`, `schemas.py`,
and `http_utils.py` are shared foundations used everywhere.

### Quick reference

| File | One-line role |
|------|---------------|
| `__init__.py` | Marks the folder as a Python package; holds the version number. |
| `config.py` | Central settings loaded from `.env` (backends, API keys, competition details). |
| `schemas.py` | Pydantic data models — the typed contract for LLM output and email drafts. |
| `seed_loader.py` | Reads the seed-sources CSV (which universities to crawl) with a priority. |
| `crawler.py` | Fetches web pages → markdown. Backends: mock, Firecrawl, Crawl4AI. |
| `extractor.py` | LLM turns a page into structured startup candidates. Backends: mock, OpenAI, Ollama. |
| `website_discovery.py` | Works out a startup's real official website from links/search. |
| `eligibility.py` | Deterministic eligibility gate + numeric priority score. |
| `dedupe.py` | Collapses duplicate startups (by domain / fuzzy name). |
| `contact_waterfall.py` | Orchestrates the 3-stage founder-email discovery, cheapest first. |
| `contact_enrichment.py` | Stage 1 + the shared email-classification engine (crawl the startup's own site). |
| `search_enrichment.py` | Stage 2: Brave web search + PDF mining for emails. |
| `provider_enrichment.py` | Stage 3: paid providers (Hunter, Apollo) as a last resort. |
| `http_utils.py` | Tiny stdlib JSON HTTP helper used by the API clients. |
| `founder_records.py` | Flattens candidates into one row per founder (for reporting). |
| `exporter.py` | Writes output to CSV / JSONL / JSON. |
| `outreach.py` | Drafts invitation emails and sends them (Gmail / SMTP / Outlook). |
| `pipeline.py` | Top-level orchestrator; exposes `run_pipeline` and `enrich_candidates_with_contacts`. |

---

## 2. Foundations (used everywhere)

### `__init__.py`
**Purpose:** Marks `startup_sourcing` as an importable Python package.
**Contents:** A docstring and `__version__ = "1.0.0"`. No logic.

### `config.py`
**Purpose:** Single source of truth for all configuration, read once from environment
variables / the project `.env` file at import time.

**What it does:** On import it calls `load_dotenv(PROJECT_ROOT / ".env")`, then builds a frozen
`Settings` dataclass whose fields default from `os.getenv(...)`. Two helpers support this:
`_project_path_env` resolves a path env-var to an absolute path under the project root, and
`_env_bool` interprets `1/true/yes/on` as `True`. A module-level singleton `settings =
Settings()` is what the rest of the code imports.

**What it configures (grouped):**
- **Backends:** `crawler_backend` (default `firecrawl`), `llm_backend` (default `ollama`).
- **OpenAI:** `openai_api_key`, `openai_model`.
- **Ollama (local LLM):** `ollama_host`, `ollama_model` (default `qwen3:14b`), input-size limits.
- **Firecrawl / Crawl4AI:** API key; browser depth, timeout, headless flag.
- **Contact crawl:** `contact_max_pages` (how many pages of a startup site to crawl).
- **Search enrichment:** `search_backend` (`none`/`brave`), `brave_search_api_key`, result/page/query limits, founder-search and PDF-search toggles.
- **Paid providers:** `email_provider` (`none`/`hunter`), `people_provider` (`none`/`apollo`), their keys, `verify_emails`, `paid_fallback_only`, and Apollo options.
- **Competition details:** `competition_name`, `competition_year`, organiser name/signature, URL. The `graduation_cutoff_year` property returns `competition_year - 5` (the 5-year graduate window).
- **Sending:** `allow_live_send` (master safety switch), Gmail OAuth paths, Outlook client/tenant, and **SMTP** settings (`smtp_host`, `smtp_port`, `smtp_user`, `smtp_password`, `smtp_from`, `smtp_use_ssl`).

**Depends on:** `python-dotenv`. Imported by nearly every other module.

### `schemas.py`
**Purpose:** Defines the Pydantic models that structure the LLM's output and the email draft —
the typed "contract" between the AI steps and the rest of the pipeline.

**Models:**
- `FounderEvidence` — one founder: `name`, `university`, `status`, `graduation_year` (int|None), `evidence`, `linkedin_url`.
- `CandidateExtract` — a startup candidate. Only `startup_name` is required; everything else has defaults. Holds a `founders` list, `eligibility_status` (default `"review"`), `eligibility_confidence` bounded **0.0–1.0**, and four quality scores (`innovation_score`, `scalability_score`, `international_potential_score`, `competition_fit_score`) each bounded **1–10** (default 5), plus `evidence_quotes`.
- `CandidateBatch` — `candidates: list[CandidateExtract]`; the shape an extractor returns per page.
- `EmailDraft` — a required `subject` and `body`.

**Enums (`Literal` types):**
- `FounderStatus = "current_student" | "recent_graduate" | "older_graduate" | "unknown"`
- `EligibilityStatus = "qualified" | "not_qualified" | "review"`

**Depends on:** `pydantic`. No in-package imports.

### `http_utils.py`
**Purpose:** A minimal, dependency-free JSON HTTP helper used by the API clients (Brave,
Hunter, Apollo).

**Key function:**
- `json_request(url, *, method="GET", params=None, headers=None, json_body=None, timeout=30.0) -> dict` — builds the query string (skipping `None` params), sets `Accept`/`Content-Type` defaults, sends the request via `urllib`, and returns parsed JSON (`{}` for an empty body).

**Notable behaviour:** Converts transport errors into a clear `RuntimeError` — an `HTTPError`
becomes `HTTP {code} from {url}: {body[:1000]}`, a `URLError` becomes `Request failed for
{url}: {exc}`. Decodes with `errors="replace"` so bad bytes never crash it. Stdlib only.

---

## 3. Input → candidates

### `seed_loader.py`
**Purpose:** Loads the seed-sources CSV (the universities/organisations to crawl) into dict rows.

**Key function:**
- `load_seed_sources(path) -> list[dict]` — validates the file exists and is `.csv`, reads it as `utf-8-sig`, and adds an integer `priority` field parsed from the `"Priority (1=highest)"` column (defaulting to **99** — lowest — if missing/unparseable, so malformed rows are naturally filtered out by the pipeline's `max_priority`).

**Depends on:** stdlib only.

### `crawler.py`
**Purpose:** Turns a seed source into a list of markdown pages. Defines the crawler abstraction
plus three interchangeable backends.

**Core types:**
- `CrawledPage` (dataclass) — `source_id`, `source_org`, `seed_url`, `page_url`, `title`, `markdown`.
- `Crawler` (Protocol) — every backend implements `crawl_source(source, max_pages) -> list[CrawledPage]`.
- `_run_async(awaitable)` — helper that runs a coroutine from sync code, spinning up a worker thread if an event loop is already running (e.g. inside Streamlit/Jupyter).

**Backends:**
- `MockCrawler` — returns up to two hard-coded synthetic pages for demos/tests (no network).
- `FirecrawlCrawler` — calls the hosted **Firecrawl** API for markdown main-content; requires `firecrawl_api_key`; defensively maps each returned document to a `CrawledPage`.
- `Crawl4AICrawler` — local, browser-based deep crawl using the `crawl4ai` package. Runs a headless `BFSDeepCrawlStrategy` bounded by `max_depth`/`max_pages`, excludes external links, drops failed/≥400/empty pages, and normalises Crawl4AI's markdown shapes into plain text.

**Depends on:** `config`; lazily imports `firecrawl` / `crawl4ai` only when used.

### `extractor.py`
**Purpose:** The extraction layer — turns a crawled page into a `CandidateBatch` of structured
startups. Pluggable backends: rule-based mock, OpenAI, Ollama.

**What it does:** Declares abstract `CandidateExtractor.extract(page) -> CandidateBatch`, then:
- `MockExtractor` — splits page markdown on `## ` headings; for each chunk regex-mines email, website, a founder name (`founded by …` / `Founder …`), and a graduation year, derives a founder `status`, and applies **local rules** for eligibility, confidence, industry and the four quality scores (no network).
- `OpenAIExtractor` — sends the shared system prompt + up to 120k chars of page markdown to OpenAI's `responses.parse` with `text_format=CandidateBatch` (structured output).
- `OllamaExtractor` — same idea against a local Ollama server, embedding the JSON schema, `temperature=0`, `think=False`.

**Key shared helper:** `_candidate_system_prompt(settings)` — the LLM instruction that encodes
the eligibility rule (current student, or graduated in the cutoff year or later), forbids
inventing facts, and tells the model to ignore sponsors/investors/universities/service providers.

**Notable logic (mock backend):** status heuristic (`current student` → `current_student`; year
≥ cutoff → `recent_graduate`; older year → `older_graduate`; else `unknown`) → eligibility
mapping (`qualified` at 0.92–0.96 confidence, `not_qualified` at 0.95, else `review` at 0.45).

**Depends on:** `config`, `crawler.CrawledPage`, `schemas`; lazily imports `openai` / `ollama`.

### `website_discovery.py`
**Purpose:** Works out a startup's **real official website** from page links or search results —
deliberately conservative so the contact stage never crawls the wrong domain.

**Key functions:**
- `canonicalize_website(url) -> str` — cleans/validates a URL (adds scheme, drops fragment/trailing slash, rejects `mailto:`/`tel:`/`javascript:`), returns `""` if not a real http(s) host.
- `is_probable_startup_website(url, source_url="") -> bool` — rejects social/directory hosts and the source's own host; accepts plausible standalone company sites.
- `discover_startup_website(startup_name, markdown, source_url) -> (url, confidence, method)` — scans the source page's links, fuzzy-matches the company name against anchor text and domain stem (rapidfuzz), and returns the best host **only if score ≥ 68** (`source_page_link_match`), else a weak/empty result.
- `discover_startup_website_from_search(startup_name, search_results, existing_source_urls=None) -> (url, confidence, relationship, evidence)` — same idea over search results; returns `official_company` when score ≥ 70, else `uncertain`.

**Notable constants:** `SOCIAL_OR_DIRECTORY_DOMAINS` (LinkedIn, Facebook, Crunchbase, Wellfound,
etc. — always excluded). **Depends on:** `rapidfuzz`, stdlib. A leaf utility used by `pipeline`.

---

## 4. Judging & de-duplicating

### `eligibility.py`
**Purpose:** Re-judges eligibility by **fixed rules** (not trusting the model) and computes a
numeric priority score.

**Key functions:**
- `enforce_eligibility(candidate, settings) -> dict` — buckets each founder into qualifying / disqualifying / unknown by status and graduation year vs `graduation_cutoff_year`. Sets `qualified` if any founder qualifies; `not_qualified` only when there are founders, none unknown, and at least one disqualifies; otherwise `review`. Overwrites `eligibility_status` and `eligibility_reason`.
- `compute_priority_score(candidate, source_priority) -> float` — a 0–100 score: `min(100, eligibility_conf*50 + quality_mean*3 + source_bonus + gate_bonus)`, where `quality_mean` is the mean of the four scores, `source_bonus = max(0, 4 - priority)*3`, and `gate_bonus` is 20/5/0 for qualified/review/not_qualified. Eligibility confidence dominates (up to 50 pts).

**Depends on:** `config`. Pure logic, no third-party libs.

### `dedupe.py`
**Purpose:** Collapses duplicate startup candidates, keeping the highest-priority row and
merging useful fields from the duplicates.

**Key functions:**
- `normalize_name(name) -> str` — lowercases, strips company suffixes (ltd/inc/llc/pte/…), removes non-alphanumerics.
- `website_domain(url) -> str` — hostname without `www.`.
- `deduplicate(candidates) -> list[dict]` — processes candidates in **descending priority** so the best row is canonical; matches a new candidate to a kept one if they share a domain **or** their normalised names are ≥ 92 similar (rapidfuzz). On a match it only **fills empty fields**, appends new founders, and unions `source_urls`; otherwise appends the candidate.

**Depends on:** `rapidfuzz`, stdlib.

---

## 5. The contact waterfall (finding founder emails)

The waterfall tries the **cheapest, most authoritative** sources first and only escalates to
paid APIs when needed.

### `contact_waterfall.py`
**Purpose:** Orchestrates the three contact-discovery stages in priority order and stamps a final
status.

**Key class:** `ContactWaterfall` composes a required `WebsiteContactEnricher` and optional
`SearchContactEnricher` / `PaidProviderEnricher`.
- `enrich(candidate) -> dict` runs: **Stage 1** always (crawl the startup's own site); **Stage 2** (web search) only if a search enricher exists and no `founder_email` was found yet; **Stage 3** (paid) only when a provider enricher exists and either `paid_fallback_only` is off, or no founder email yet, or verification is enabled. Then it de-dupes attempted methods and sets `contact_waterfall_status` by priority: founder email > company email > any contact email > none.

**Depends on:** the three enricher modules below.

### `contact_enrichment.py`
**Purpose:** Stage 1 crawler **plus the shared email-classification engine** reused by the search
and provider stages. This is the heart of contact discovery.

**What it does:** Defines the email/LinkedIn regexes and lexicons (`GENERIC_BLOCKLIST`,
`FREE_MAIL_DOMAINS`, `GENERIC_ROLE_LOCALS` like info/hello, `FOUNDER_ROLE_LOCALS` like
founder/ceo, `IGNORE_LOCAL_HINTS` like noreply/legal which are dropped, and path terms for
choosing which internal pages to crawl). `EmailFinding` is an immutable record of one discovered
email with type, confidence, source, founder-match and verification fields.

**Key functions:**
- `extract_public_emails(text) -> list[str]` — de-obfuscates `(at)`/`(dot)`, regex-extracts, and filters junk/blocklisted/noreply addresses.
- `extract_linkedin_urls(text) -> (people, companies)` — splits personal profile vs company-page URLs.
- `classify_email(email, founder_names, startup_domain, source_url) -> EmailFinding` — the scoring core: builds founder local-part patterns (`first`, `last`, `first.last`, `flast`, …) and assigns a **deterministic confidence** — a founder-pattern match on the company domain scores highest (0.98), founder-role locals mid (~0.76), generic role/company addresses lower, free-mail unmatched lowest.
- `apply_findings_to_candidate(candidate, findings, people=None, companies=None)` — merges/ranks all evidence (valid > confidence > verification score) and selects the winning founder email, company email, overall contact email, and LinkedIn URLs. (Shared by all three stages.)
- `WebsiteContactEnricher.enrich(candidate) -> dict` — BFS-crawls the startup's own site starting from the homepage plus common paths (`/team`, `/founders`, `/about`, `/contact`, …), bounded by `max_pages`; extracts and classifies emails/LinkedIn per page, follows relevant same-site links, and applies the findings. Sets `contact_enrichment_status`.

**Depends on:** `website_discovery.canonicalize_website`; consumes markdown from an injected crawler.
No direct API calls (it's the classification backbone).

### `search_enrichment.py`
**Purpose:** Stage 2 — uses the **Brave** web-search API (and PDFs it surfaces) to recover a
startup's domain, founder identities, and emails when the site crawl alone didn't find a founder
email.

**Key pieces:**
- `SearchResult` (dataclass) — a normalised search hit.
- `BraveSearchClient.search(query, count=None) -> list[SearchResult]` — calls Brave Web Search (`X-Subscription-Token` header), counts calls.
- `PdfContactExtractor.extract_text(url, max_bytes=15MB) -> str` — downloads a PDF (size-capped) and extracts up to 50 pages via `pypdf`.
- `SearchContactEnricher.build_queries(candidate) -> list[str]` — assembles de-duplicated queries: startup contact/founder queries, `site:domain`, a `filetype:pdf` query, and per-founder queries (name+email, name+@domain, LinkedIn, founder, founder-PDF).
- `SearchContactEnricher.enrich(candidate) -> dict` — runs the queries, mines snippet text for emails (confidence clamped to 0.62–0.94, provider `brave`) and LinkedIn URLs, recovers the official domain if still missing, then ranks and fetches the top result pages/PDFs (`_page_score`) to mine their full text, and applies findings.

**Depends on:** `contact_enrichment` (classification helpers), `http_utils`, `website_discovery`;
`rapidfuzz`, `pypdf`. **External:** Brave Search API + arbitrary PDF/page fetches.

### `provider_enrichment.py`
**Purpose:** Stage 3 — the **paid** providers (Hunter, Apollo), called by default only as a
fallback after the free methods.

**Key pieces:**
- `HunterClient` — Hunter.io v2: `domain_search`, `email_finder`, `verify` (with call counters).
- `ApolloClient` — Apollo `people/match` plus async webhook polling (`poll_waterfall`) for Apollo's asynchronous email reveal.
- `PaidProviderEnricher.enrich(candidate) -> dict` — runs **Hunter first** (domain search, then per-founder email-finder for anyone still unmatched), then **Apollo** (per-founder `people_match`, extract/poll emails, back-fill LinkedIn), then — if `verify_emails` is on — re-verifies not-yet-valid emails through Hunter. Applies findings and sets `provider_enrichment_status`.
- `provider_call_totals(enricher) -> dict` — aggregates per-endpoint call counts for the run report.

**Depends on:** `contact_enrichment`, `http_utils`. **External:** Hunter.io v2 and Apollo APIs.

---

## 6. Output & outreach

### `founder_records.py`
**Purpose:** Flattens the nested candidate→founders structure into **one row per founder** for
reporting/export.

**Key function:**
- `build_founder_rows(candidates) -> list[dict]` — for each named founder, picks the best matching `contact_evidence` record (valid > confidence > verification score) and emits a flat row combining startup fields (name, website, domain, eligibility, priority) + founder fields (university, status, graduation year, LinkedIn) + the best email and its provenance.

**Depends on:** stdlib only (tolerates dict or Pydantic founders).

### `exporter.py`
**Purpose:** Writes pipeline output to disk.

**Key functions:**
- `write_csv(path, rows)` — writes a **`utf-8-sig` (Excel-friendly BOM)** CSV whose header is the union of all row keys in first-seen order; list/dict cells are JSON-encoded.
- `write_jsonl(path, rows)` — one JSON object per line (`default=str`).
- `write_json(path, payload)` — one indented JSON document.

**Depends on:** stdlib only.

### `outreach.py`
**Purpose:** Drafts invitation emails and (optionally) sends them. All live sending is gated
behind `settings.allow_live_send`.

**Drafters:**
- `MockEmailDrafter` — deterministic templated invitation (no LLM).
- `OpenAIEmailDrafter` / `OllamaEmailDrafter` — LLM drafters using structured `EmailDraft` output. A guardrail system prompt (`_draft_system_prompt`) forbids fabricating awards/traction/bios and caps the body at 180 words; only whitelisted facts (`_draft_facts`) are passed in.

**Senders (each `send()` raises `PermissionError` unless `ALLOW_LIVE_SEND=true`):**
- `GmailClient` — Gmail API via OAuth installed-app flow; `create_draft` or `send`.
- `SmtpClient` — plain SMTP with username + app password (no OAuth); SSL (465) or STARTTLS (587); usable as a context manager to reuse one connection for a batch; `verify()` tests the login.
- `OutlookGraphClient` — Microsoft Graph `sendMail` via MSAL device-code flow.

**Depends on:** `config`, `schemas.EmailDraft`; lazily imports `openai`, `ollama`, the Google auth
stack, `msal`, `requests`. **External:** OpenAI, Ollama, Gmail API, SMTP, Microsoft Graph.

---

## 7. The orchestrator

### `pipeline.py`
**Purpose:** The top-level conductor that wires every stage together. Exposes the two entrypoints
the app/CLI call.

**Backend selector constants:**
`CRAWLER_BACKENDS = ("firecrawl", "crawl4ai")`, `LLM_BACKENDS = ("openai", "ollama")`,
`SEARCH_BACKENDS = ("none", "brave")`, `EMAIL_PROVIDERS = ("none", "hunter")`,
`PEOPLE_PROVIDERS = ("none", "apollo")`, plus `JSON_CANDIDATE_FIELDS` (columns that are JSON/lists
on CSV round-trip).

**Factory functions:**
- `build_crawler(mode, settings, crawler_backend=None)` → `MockCrawler` / `FirecrawlCrawler` / `Crawl4AICrawler`.
- `build_components(mode, settings, …)` → a `(crawler, extractor, drafter)` triple (mock, or OpenAI/Ollama).
- `build_contact_waterfall(*, crawler, settings, search_backend, email_provider, people_provider, …)` → a `ContactWaterfall` plus its sub-enrichers, validating backend names and constructing `BraveSearchClient` / `HunterClient` / `ApolloClient` as required.

**Entrypoints:**
- `run_pipeline(seed_csv, output_dir, settings, mode="mock", …) -> dict` — the full run: load seeds (filtered by `max_priority`, sliced by `seed_start_index`/`max_sources`) → crawl each source → extract candidates per page → resolve each startup's website (accept the LLM URL if `is_probable_startup_website`, else `discover_startup_website`) → `enforce_eligibility` → `compute_priority_score` → `deduplicate` → sort by score → run the contact waterfall (only when `enrich_contacts and mode=="live"`) → rank → draft emails for qualified candidates → write CSV/JSONL/JSON outputs → return a summary dict.
- `enrich_candidates_with_contacts(candidates, output_dir, settings, mode="live", …) -> dict` — a lighter variant that skips crawling/extraction/drafting and just runs the contact waterfall over an already-loaded candidate list (used by the app's "run waterfall on an uploaded CSV" feature).

**CSV round-trip helpers:**
- `load_candidates_csv(path)` / `normalize_candidate_row(row)` / `_csv_cell(value)` — read candidate CSVs back into proper Python types (parsing JSON-looking cells, coercing `JSON_CANDIDATE_FIELDS` into lists).

**Depends on:** essentially every other module in the package.

---

## 8. End-to-end flow in one paragraph

`run_pipeline` loads the **seed** universities (`seed_loader`), **crawls** each one to markdown
(`crawler`), and asks an **LLM to extract** candidate startups and their founders (`extractor`,
typed by `schemas`). For each candidate it resolves the **real website** (`website_discovery`),
applies the deterministic **eligibility** gate and **priority score** (`eligibility`), and
**de-duplicates** across sources (`dedupe`). For eligible candidates it runs the **contact
waterfall** (`contact_waterfall`) — first crawling the startup's own site (`contact_enrichment`),
then **Brave search + PDFs** (`search_enrichment`), then **paid Hunter/Apollo** (`provider_
enrichment`) — to find the founders' emails, with `http_utils` handling the API calls. Finally it
**drafts invitation emails** (`outreach`), **flattens** results to per-founder rows
(`founder_records`), and **writes** the CSV/JSON outputs (`exporter`). `config` supplies settings
throughout, and the Streamlit `app.py` (one level up) lets you send the drafted emails via Gmail,
SMTP, or Outlook.
