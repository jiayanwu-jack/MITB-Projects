# smu_sourcer — startup founder sourcing pipeline

A clean redesign of `../smu_crawler` around one goal: **automatically obtain, for
startup projects, the founder's name, email, industry, and the startup's URL.**

Where `smu_crawler` grew a founder feature onto a tool built for org-level contacts,
this is built for startups from the ground up, around a single record and a pluggable
pipeline:

```
  SOURCE                 ENRICH                       EMAIL-FIND            EXPORT
  find startups     →    read the startup's site  →   fill missing     →   startups.csv
  ────────────           ─────────────────────        emails               + .jsonl
  · accelerator          homepage + about/team/       · scraped from        + report.json
    portfolios           contact pages, then:           the site
  · university           Claude on Bedrock (or        · pattern guess
    competition            deterministic) extracts      (first.last@…)
    winners                { founders:[{name,role,     · MX verify
  · web search             email}], industry, … }
  · startup news
  · Tracxn (paid DB)
```

Every startup ends as one `Startup` record with a list of `Founder`s
(`name, role, email, email_status`). No API key is required to run; Claude on AWS
Bedrock is an optional quality upgrade for the extraction step.

## Install

```bash
cd smu_sourcer
python -m pip install -r requirements.txt
# optional extras:
python -m pip install dnspython                 # MX verification of guessed emails
python -m pip install "anthropic[bedrock]"      # Claude on Bedrock extraction (--llm)
```

## Configuration (.env)

`run.py` auto-loads a `.env` file from the project root, so you can keep your keys in
one place instead of setting them each session. Copy the template and fill it in:

```bash
cp .env.example .env      # then edit .env
```

`.env` is gitignored. Real environment variables (`setx` / `export`) still take
precedence over anything in `.env`. Recognised keys: `AWS_REGION`, `SMU_BEDROCK_MODEL`,
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN`, `TRACXN_API_KEY`,
`TRACXN_API_URL`, `SERPER_API_KEY`, `BRAVE_API_KEY`. All are optional — the pipeline
runs without any of them.

## Prove it works (no external network)

```bash
python run.py --self-test
```

Spins up two localhost servers — an accelerator portfolio page and a startup's own
site — and runs the full pipeline over real HTTP, asserting it discovers the startup
URL from the portfolio, follows the startup's team page, and extracts the founder's
name, role, and email.

## Run it for real

```bash
# Default: accelerator portfolios + university competition winners
python run.py --workers 4

# Choose sources; pass a per-source argument after ':'
python run.py --source accelerator --source university
python run.py --source "websearch:NUS startup competition winners 2025"
python run.py --source "news:student startup raises seed 2025"
python run.py --source "tracxn:climate student startup"     # needs TRACXN_API_KEY
python run.py --source csv:my_startups.csv                  # enrich a hand-made list

# Use Claude on Bedrock for the founder/industry extraction step
export AWS_REGION=us-east-1
export SMU_BEDROCK_MODEL=anthropic.claude-sonnet-5   # or your inference-profile id
python run.py --llm
```

## Sources

| Source | What it does | Needs |
|--------|--------------|-------|
| `accelerator` | Scrapes portfolio pages in `data/accelerators.csv` for outbound startup links | — |
| `university` | Scrapes competition-winners / alumni-ventures pages in `data/universities.csv` | — |
| `csv` | Reads a plain CSV of startups you already have (`--source csv:path.csv`) | — |
| `websearch` | Turns a query into candidate startup URLs (`--source websearch:<query>`) | `SERPER_API_KEY` or `BRAVE_API_KEY` |
| `news` | Startup-news search for recently funded / launched ventures | `SERPER_API_KEY` or `BRAVE_API_KEY` |
| `tracxn` | Paid startup database — company search returns name/site/sector/founders | `TRACXN_API_KEY` (add later) |

Sources that need a key **degrade gracefully**: without the key they print a one-line
notice and return nothing, so the rest of the pipeline still runs. To add a source,
drop a module in `sourcer/sources/` implementing `discover(ctx) -> list[Startup]` and
register it in `sourcer/sources/__init__.py`.

Seed the `data/*.csv` lists with the **exact** portfolio / winners page for each
accelerator or university (not the site root) for far better yield.

## Founder email strategy

For each founder, in order: (1) an email found on the site is trusted as-is
(`email_status=scraped`); (2) otherwise common patterns are generated from the name +
domain (`jane.tan@acme.io`, `jtan@acme.io`, …); (3) if `dnspython` is installed and the
domain has an MX record the best guess is marked `mx_ok`, otherwise `guessed`. Live
per-address SMTP probing is intentionally **not** done — it's unreliable and can hurt
sender reputation. **Review every `guessed`/`mx_ok` email before any outreach.**

## Claude on AWS Bedrock (`--llm`)

The extraction step ([sourcer/llm.py](sourcer/llm.py)) uses the Anthropic SDK's Bedrock
"Mantle" client with **structured outputs** (a JSON schema), so Claude returns exactly
`{industry, description, founders:[{name, role, email}]}`. It never invents an email —
it only reports one that's on the page. Without `--llm` (or if the Bedrock client isn't
installed), the deterministic extractor in [sourcer/extract.py](sourcer/extract.py)
(`guess_people`) is used instead — it still gets names/roles/scraped emails but not a
reliably-labelled industry.

Bedrock model IDs take an `anthropic.` prefix; some accounts must use a cross-region
inference-profile id (e.g. `us.anthropic.claude-sonnet-5`). Set `SMU_BEDROCK_MODEL` to
whatever you've enabled and `AWS_REGION` to a region where Claude is available.

## Output (`output/`)

- `startups.csv` — **one row per founder** (or one row if none). Columns:
  `startup_name, url, domain, industry, country, status, confidence, founder_name,
  founder_role, founder_email, email_status, email_source, sourced_from, source,
  source_detail, description, notes`. `sourced_from` is the name of the university /
  organisation the startup was found via (e.g. "Y Combinator", "NUS Enterprise
  portfolio"); `source` is the channel type and `source_detail` the exact page/URL.
- `startups.jsonl` — one JSON object per startup, founders nested.
- `report.json` — run summary (enriched / with_email / verified / by_source / by_status).

Filter `status == enriched` and `email_status in (scraped, mx_ok)` for the contacts
safest to reach; verify the rest against the startup site.

## Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--source NAME[:ARG]` | `accelerator`, `university` | source to run (repeatable) |
| `--limit N` | 20 | max startups per source |
| `--workers N` | 4 | parallel enrichment workers (rate-limited per host) |
| `--delay S` | 1.0 | min seconds between hits to one host |
| `--data DIR` | `data/` | seed-list directory |
| `--out DIR` | `output/` | output directory |
| `--llm` | off | use Claude on Bedrock for extraction |
| `--self-test` | — | localhost proof, then exit |

## Layout

```
smu_sourcer/
  run.py                 # CLI + --self-test
  requirements.txt
  .env.example           # copy to .env; keys loaded automatically by run.py
  sourcer/
    models.py            # Startup, Founder
    env.py               # zero-dependency .env loader
    http.py              # fetcher: rate limit, retries, cache, get_json
    extract.py           # text/email/link extraction, deterministic name/role guessing
    llm.py               # OPTIONAL Claude-on-Bedrock structured extraction
    email_find.py        # scrape -> pattern guess -> MX verify
    pipeline.py          # sources -> dedupe -> enrich -> email-find
    export.py            # startups.csv / .jsonl / report.json
    sources/
      base.py            # Source protocol + Context
      accelerator.py     # portfolio scraper
      university.py      # competition-winners scraper
      _portfolio.py      # shared listing-page scraping
      csv_source.py      # read a plain startup list
      websearch.py       # Serper/Brave search (key-gated)
      news.py            # startup-news search (key-gated)
      tracxn.py          # Tracxn paid DB (key-gated, add key later)
  data/                  # seed accelerator + university listing pages
  tests/test_sourcer.py  # offline unit tests
```

> Small-scale / educational use, with instructor sign-off. Even public data is
> regulated (PDPA in Singapore, GDPR in the EU) when you contact people — keep target
> lists small, review flagged rows, and honour each site's terms.
