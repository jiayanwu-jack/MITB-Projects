# University Startup Sourcing Pipeline v3

A configurable sourcing and contact-intelligence pipeline for university startup competitions.

## What v3 adds

The contact stage is now a free-first waterfall:

1. Crawl the startup's public website and dynamically prioritize team, founder, leadership, about, and contact links.
2. Optionally use Brave Search to recover missing startup domains, search each founder independently, inspect rich search snippets, crawl high-value result pages, and inspect public PDFs.
3. Optionally use Hunter Domain Search and Email Finder after free methods fail.
4. Optionally use Apollo People Enrichment after earlier stages fail.
5. Optionally verify discovered emails through Hunter.
6. Rank contacts with source, origin, founder match, confidence, verification status, and evidence URL.

The pipeline never intentionally invents an email address. Pattern guesses are not generated or sent.

## Outputs

Each run writes:

- `candidates_ranked.csv` — startup-level review queue.
- `founders.csv` — one row per founder with founder-specific contact fields.
- `contact_evidence.csv` — all email evidence, source URLs, providers, confidence, and verification results.
- `search_queries.csv` — audit trail of search queries run.
- `outreach_drafts.csv` — drafts for automatically qualified candidates.
- `crawled_pages.jsonl`, `candidates_raw.csv`, `errors.csv`, and `run_summary.json`.

## Installation

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
crawl4ai-setup
```

For Ollama:

```cmd
ollama pull qwen3:14b
```

Copy `.env.example` to `.env` and add only the API keys for providers you actually use.

## Recommended staged rollout

### A. Free local baseline

```cmd
python run_pipeline.py --mode live --crawler-backend crawl4ai --llm-backend ollama --ollama-model qwen3:14b --max-sources 1 --max-priority 1 --max-pages-per-source 2 --enrich-contacts --search-backend none --email-provider none --people-provider none --output-dir outputs/v3_baseline
```

### B. Add search discovery

Set `BRAVE_SEARCH_API_KEY` in `.env`, then:

```cmd
python run_pipeline.py --mode live --crawler-backend crawl4ai --llm-backend ollama --ollama-model qwen3:14b --max-sources 1 --max-priority 1 --max-pages-per-source 2 --enrich-contacts --search-backend brave --enable-founder-search --enable-pdf-search --email-provider none --people-provider none --output-dir outputs/v3_search
```

### C. Add Hunter fallback and verification

Set `HUNTER_API_KEY` in `.env`:

```cmd
python run_pipeline.py --mode live --crawler-backend crawl4ai --llm-backend ollama --ollama-model qwen3:14b --max-sources 1 --max-priority 1 --max-pages-per-source 2 --enrich-contacts --search-backend brave --email-provider hunter --verify-emails --paid-fallback-only --output-dir outputs/v3_hunter
```

### D. Add Apollo as the last fallback

Set `APOLLO_API_KEY` in `.env`:

```cmd
python run_pipeline.py --mode live --crawler-backend crawl4ai --llm-backend ollama --ollama-model qwen3:14b --max-sources 1 --max-priority 1 --max-pages-per-source 2 --enrich-contacts --search-backend brave --email-provider hunter --people-provider apollo --verify-emails --paid-fallback-only --output-dir outputs/v3_full
```

Apollo personal-email reveal and waterfall enrichment are disabled by default. Enable them explicitly only after reviewing the provider's current terms, privacy requirements, and credit implications:

```cmd
--apollo-reveal-personal-emails --apollo-run-waterfall-email
```

## Main v3 CLI options

```text
--search-backend {none,brave}
--search-max-results N
--search-max-pages-per-candidate N
--search-queries-per-founder N
--enable-founder-search / --no-enable-founder-search
--enable-pdf-search / --no-enable-pdf-search
--email-provider {none,hunter}
--people-provider {none,apollo}
--verify-emails / --no-verify-emails
--paid-fallback-only / --no-paid-fallback-only
--apollo-reveal-personal-emails
--apollo-run-waterfall-email
```

## Contact ranking fields

Important startup-level columns include:

- `founder_email`, `founder_email_confidence`, `founder_email_type`
- `company_email`, `company_email_confidence`
- `contact_email`, `contact_email_type`, `email_source_url`
- `official_domain`, `domain_confidence`, `domain_relationship`, `domain_evidence`
- `founder_linkedin_url`, `linkedin_url`
- `contact_waterfall_status`, `contact_methods_attempted`
- `search_queries_run`, `search_results_considered`, `search_pages_crawled`
- `provider_calls`, `provider_enrichment_errors`

`founders.csv` should be your main file when you want one row per person rather than one row per startup.

## Safety and data-quality principles

- Paid providers are fallbacks by default (`PAID_FALLBACK_ONLY=true`).
- The system retains evidence URLs and provider origins for audit.
- Verification status is separate from identity-match confidence.
- A verified mailbox can still belong to the wrong person; inspect founder match and source evidence too.
- Publicly available contact data may still be subject to privacy, anti-spam, university, competition, and provider rules. Keep a human review step before outreach.
- Gmail sending remains separately locked by `ALLOW_LIVE_SEND=false` unless explicitly changed.

## Tests

```cmd
pytest -q
```

## Streamlit

```cmd
streamlit run app.py
```

The sidebar exposes crawling, LLM, search, Hunter, Apollo, verification, and fallback controls.

## Re-enrich an existing candidate CSV

You do not need to rerun university crawling and LLM extraction when you already have `candidates_ranked.csv`.

```cmd
python enrich_candidates.py --input-csv candidates_ranked.csv --crawler-backend crawl4ai --search-backend brave --email-provider hunter --people-provider apollo --verify-emails --output-dir outputs/reenriched_v3
```

This produces `candidates_ranked_enriched.csv`, `founders.csv`, `contact_evidence.csv`, and `reenrichment_summary.json`.
