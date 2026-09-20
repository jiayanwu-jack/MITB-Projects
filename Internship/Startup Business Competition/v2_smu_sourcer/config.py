"""
smu_sourcer configuration.

Edit the values below to change how a run behaves WITHOUT touching run.py.
Everything here is a DEFAULT - if you pass the matching command-line flag, it still
overrides the value for that one run.

    Run with these defaults:     python run.py
    Override just this run:       python run.py --workers 4 --no-llm

(API keys and the LLM backend choice live in .env, not here.)
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parent


# --------------------------------------------------------------------------- #
# WHICH SOURCES TO PULL STARTUPS FROM
# --------------------------------------------------------------------------- #
# A list of source names to run. Each source discovers startups from its own channel.
#   Available: "accelerator", "university", "csv", "websearch", "news", "tracxn"
# Set to None to use the built-in default (accelerator + university).
# You can pass a per-source argument after a colon, e.g. "websearch:climate student startup".
SOURCES: list[str] | None = None


# --------------------------------------------------------------------------- #
# HOW MANY STARTUPS TO TAKE FROM EACH SOURCE
# --------------------------------------------------------------------------- #
# Default cap applied to EVERY source (stops one big source from dominating the run).
LIMIT_DEFAULT = 15

# Per-source overrides:  None = use LIMIT_DEFAULT  |  0 = skip that source entirely  |  N = cap at N
LIMIT_UNIVERSITY: int | None = 30        # max startups from the 'university' source
LIMIT_ACCELERATOR: int | None = 0         # 0 = don't pull any accelerator startups


# --------------------------------------------------------------------------- #
# CRAWL BEHAVIOUR
# --------------------------------------------------------------------------- #
# How many startups to enrich IN PARALLEL. Higher = faster.
#   * Cloud LLM / no LLM: 4-8 is fine.
#   * Local Ollama model: keep this LOW (1-2) - it processes ~one request at a time,
#     so more workers just queue and can also hit context/memory limits.
WORKERS = 2

# Minimum seconds between requests to the SAME website (politeness / rate-limiting).
# Different sites are still hit in parallel; this only throttles per-host.
DELAY = 1.0


# --------------------------------------------------------------------------- #
# LLM EXTRACTION
# --------------------------------------------------------------------------- #
# Use an LLM to read each startup page and extract founder name/role/email + industry.
# The backend (Gemini / AWS Bedrock / local Ollama) is chosen by SMU_LLM_PROVIDER in .env.
# When False, only the deterministic regex extractor runs (no industry, weaker names).
USE_LLM = True


# --------------------------------------------------------------------------- #
# CONTACT FINDING  (only for founders with NO email found on their own site)
# --------------------------------------------------------------------------- #
# These two are ALTERNATIVES. If both are True, FIND_CONTACTS wins and LLM_SEARCH is skipped.

# FIND_CONTACTS: deterministic lookup - search for the founder's LinkedIn URL
#   (needs SERPER_API_KEY or BRAVE_API_KEY in .env) and look up an email via Hunter.io
#   (needs HUNTER_API_KEY). Emails from Hunter are marked 'api_verified'/'api_unverified'.
FIND_CONTACTS = False

# LLM_SEARCH: the LLM reads web-search RESULT PAGES to find the founder's email/LinkedIn
#   (needs a search key + a working LLM). Slower (a search + page fetches + an LLM call
#   per emailless founder). Only runs when FIND_CONTACTS is False. Emails are UNVERIFIED
#   ('llm_search') - review before use.
LLM_SEARCH = False

# LLM_INFER_EMAIL: absolute LAST resort. For any founder STILL without an email after all
#   of the above, the LLM GUESSES the most likely address from their name + the company
#   domain (copying the company's pattern from any example address it can see). Needs a
#   working LLM; no web search or API key required. These emails are GUESSES, marked
#   'llm_inferred' - always verify before contacting. Runs in addition to the options
#   above (it only fills the remaining gaps).
LLM_INFER_EMAIL = True


# --------------------------------------------------------------------------- #
# FOLDERS
# --------------------------------------------------------------------------- #
# Where the seed CSVs (accelerators.csv, universities.csv, ...) are read from.
DATA_DIR = HERE / "data"

# Where startups.csv / startups.jsonl / report.json are written.
OUTPUT_DIR = HERE / "output"
