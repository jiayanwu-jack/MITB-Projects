# smu_crawler — real recruitment-contact crawler

A standalone, small-scale crawler, for educational use, that finds a recruitment contact
for:

- **University entrepreneurship / innovation offices** (option 1),
- **Public organisations** — student entrepreneurship clubs, accelerators/incubators,
  and competition organisers (option 3), and
- **Startup projects** — for these, it looks for the **founder's own name and email**
  rather than a generic inbox.

Unlike the demo in `../smu_task1_sourcing` (which serves local fixtures), this crawls the
**real web over HTTP**. It runs with no API key; Claude is an optional refinement.

## What it will and won't do

**Will:** fetch public web pages, follow a few office/contact/team links per site, extract
emails (including light `a [at] b [dot] c` de-obfuscation), pick one best contact per
target (a role inbox for universities/organisations, a named founder for startups), and
write a reviewable CSV with provenance.

**Behaviour by target kind:**
- **University / organisation** targets: if a page looks like a **people directory** (many
  emails), only role inboxes are kept and individuals are dropped. A role inbox (`info@`,
  `hello@`, `contact@`) is preferred; a personal address is kept only if nothing else
  exists and is flagged `needs_review`.
- **Startup** targets: the opposite preference applies — a named founder/co-founder's
  personal email is preferred over a generic inbox, and the crawler also attempts to guess
  the person's name and title from the surrounding page text (`contact_name`,
  `contact_role` in the output).
- No logins, no LinkedIn/Crunchbase, no social networks.
- Rate-limits per host, identifies itself with a User-Agent + contact address, caps
  download size, times out, retries politely, and caches fetched pages.

**Startup lead discovery (a by-product of the same crawl):** while crawling a university
or organisation site, any page that looks like a portfolio / alumni-ventures /
competition-winners listing (hint words: "portfolio", "our startups", "success stories",
"past winners", "spin-out", "cohort", ...) is scanned for outbound links to external
startup websites (social/vendor hosts like LinkedIn are filtered out). These are written
to `output/startup_leads.csv` in the same shape as `targets/startups.csv` — review and
trim it, then re-run with `--targets output/startup_leads.csv` to get each founder's name
and email via the startup-kind logic above.

> This build does not check or obey `robots.txt`, and personal (non-role) emails are
> intentionally surfaced for startup targets — both by request, for a small-scale,
> educational use case with instructor sign-off. **You are still responsible for lawful
> use** of whatever you send outreach to: PDPA (Singapore) and GDPR (EU) still apply to
> contacting people. Keep target lists small, review every `needs_review` row before any
> outreach, and don't scale this up to mass harvesting without revisiting these defaults.

## Install

```bash
cd smu_crawler
python -m pip install -r requirements.txt
# optional, only for --llm:
python -m pip install "anthropic>=0.40" && export ANTHROPIC_API_KEY=sk-ant-...
```

## Prove it works (no external network)

```bash
python run_crawl.py --self-test
```

This spins up a localhost web server with a small sample site and crawls it over **real
HTTP**, asserting it picks the office role inbox and (for a club) the committee inbox over
the president's personal address.

## Generate the target list from the QS rankings

Instead of hand-maintaining `targets/universities.csv`, build it from the **QS World
University Rankings** automatically:

```bash
python build_targets.py --top 200                 # -> targets/universities_qs.csv
python build_targets.py --top 50 --no-resolve     # names/ranks only, add URLs later
```

How it works (`crawler/qs_targets.py`):
1. Reads the QS ranking order (rank / name / country / QS profile URL) from QS's JSON
   endpoint behind <https://www.topuniversities.com/world-university-rankings>. It stores
   only factual identifiers — **not** QS's proprietary indicator scores.
2. Resolves each institution's **official website** from **Wikidata** (property P856,
   CC0 data), because QS does not publish the homepage and the crawler must never
   fabricate a URL. Results are cached in `targets/.domain_cache.json`.
3. Writes a crawler-ready CSV: `id,name,country,qs_rank,portal_url,qs_profile_url,verified`.
   Universities whose website can't be resolved get an empty `portal_url` (fill manually;
   the crawler skips blanks).

Then crawl it:
```bash
python run_crawl.py --targets targets/universities_qs.csv --workers 4
```

> ⚠️ **The QS ranking is QS Quacquarelli Symonds' proprietary dataset.** This helper is for
> building an internal outreach target list; confirm permitted use before redistributing
> the ordering. Be polite: keep `--delay` ≥ 1s (it also rate-limits the Wikidata lookups).

## Run it for real

```bash
# Universities (seed list of 40 real institutions):
python run_crawl.py --targets targets/universities.csv --workers 4 --delay 1.0

# Public organisations:
python run_crawl.py --targets targets/organisations.csv --workers 4

# Startup projects (looks for the founder's name + email, not a generic inbox):
python run_crawl.py --targets targets/startups.csv --workers 4

# Optional Claude refinement of the chosen contact:
python run_crawl.py --targets targets/organisations.csv --llm

# Try a handful first:
python run_crawl.py --targets targets/universities.csv --limit 5
```

### Output (`output/`)
- `contacts.csv` — one row per target. Columns: `target_kind, target_id, target_name,
  country, status, contact_email, contact_name, contact_role, role_inbox, confidence,
  needs_review, page_found_url, evidence, pages_crawled, error`. `contact_name` is only
  populated when a person (typically a startup founder) was identified.
- `contacts.jsonl` — same data, one JSON object per line.
- `report.json` — run summary (found / ready / needs_review / by_status).
- `startup_leads.csv` — external startup sites found on portfolio-style pages during this
  run (see "Startup lead discovery" above). Columns: `id, name, country, org_type,
  startup_focus, portal_url, verified, source_target_name, source_page_url`.

Filter `status == ok` and `needs_review == False` for the contacts safe to invite; verify
the rest against `page_found_url` + `evidence`.

## Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--targets` | `targets/universities.csv` | CSV of targets (see below) |
| `--limit N` | all | crawl only the first N |
| `--workers N` | 4 | targets in parallel (rate-limit is **per host**, so this stays polite) |
| `--max-pages N` | 6 | max pages fetched per site |
| `--max-depth N` | 2 | max link depth per site |
| `--delay S` | 1.0 | min seconds between hits to one host |
| `--llm` | off | refine the pick with Claude (needs `ANTHROPIC_API_KEY`) |
| `--self-test` | — | localhost HTTP proof, then exit |

## Target CSV format

- **Universities:** `id,name,country,portal_url` (extra columns like `qs_rank`,
  `startup_focus`, `verified` are ignored). `kind` is `university`.
- **Organisations / startups:** include an `org_type` column
  (`student_club|accelerator|competition|startup`) and a `portal_url`; the loader
  auto-detects the org format. Use `org_type=startup` (see `targets/startups.csv`) to get
  founder-name/email extraction instead of a generic role inbox.

`portal_url` may be a site root (the crawler discovers the office/contact page) or a known
office/contact page directly (fewer hops, better result).

## Scaling to the top 200

The crawler is list-length agnostic and **fails safe** on bad URLs (the row comes back
`fetch_failed`/`no_contact`, flagged — it never invents a contact). To reach 200:

1. Generate the 200-row list automatically: `python build_targets.py --top 200`
   (see "Generate the target list from the QS rankings" above).
2. Prefer pointing `portal_url` at each institution's **entrepreneurship/innovation office
   page** rather than the bare root — you get a far better contact and fewer hops. The seed
   rows use root domains (`verified=false`) precisely because those exact office URLs must
   be confirmed by a human.
3. Run with a sensible `--delay` (≥1s) and `--workers` (≤8), then review flagged rows.

## Layout

```
smu_crawler/
  run_crawl.py          # CLI + --self-test
  build_targets.py      # build targets/universities_qs.csv from the QS rankings
  requirements.txt
  crawler/
    qs_targets.py       # QS ranking reader + Wikidata (P856) website resolver
    fetch.py            # real HTTP: rate limit, retries, cache, size cap
    extract.py          # text/email extraction, de-obfuscation, role-inbox, link scoring
    discover.py         # bounded best-first per-site crawl + portfolio-page lead scraping
    rank.py             # pick one contact + confidence (role inbox vs. named founder)
    llm.py              # OPTIONAL Claude refinement
    export.py           # contacts.csv / .jsonl / report.json / startup_leads.csv
    models.py           # Target, Candidate, Result, StartupLead
  targets/              # seed university + organisation lists (real domains)
  tests/test_crawler.py # offline unit tests
```
