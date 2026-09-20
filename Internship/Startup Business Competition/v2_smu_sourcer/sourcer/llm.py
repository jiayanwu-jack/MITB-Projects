"""
OPTIONAL LLM extraction of founder + industry from a startup page.

Three interchangeable backends behind one interface (available / extract / apply):

  * Google AI Studio (Gemini)  - just an API key, no cloud IAM. Zero extra packages.
  * AWS Bedrock (Claude)        - via the Anthropic SDK's Bedrock client. Needs
                                 `anthropic[bedrock]` + AWS credentials + IAM model access.
  * Local model via Ollama      - runs entirely offline on your machine (llama3, mistral,
                                 qwen, ...). No API key, no cloud. Zero extra packages.

The pipeline runs fine without any of them - `sourcer.extract.guess_people` is the
deterministic fallback - but an LLM reads a founder's name/role/email and the startup's
industry out of messy page text far better.

Backend selection (env `SMU_LLM_PROVIDER`):
  * "gemini"  -> force Google AI Studio (needs GEMINI_API_KEY / GOOGLE_API_KEY)
  * "bedrock" -> force AWS Bedrock (needs anthropic[bedrock] + AWS creds)
  * "ollama"  -> force a local Ollama server (needs `ollama serve` running)
  * "auto"    -> (default) Gemini if a key is set, else Bedrock, else a local Ollama server.

Local Ollama setup (fully offline, no key, no rate limits):
    1. Install Ollama (https://ollama.com) and pull a model:  ollama pull llama3
    2. Make sure it's running:  ollama serve   (usually auto-starts on install)
    3. In .env:  SMU_LLM_PROVIDER=ollama    (and optionally OLLAMA_MODEL=llama3)
    4. python run.py --llm
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

from .models import Founder, Startup

# --- backend selection ------------------------------------------------------ #
PROVIDER = os.environ.get("SMU_LLM_PROVIDER", "auto").lower()

# --- Google AI Studio (Gemini) --------------------------------------------- #
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# --- AWS Bedrock (Claude) --------------------------------------------------- #
MODEL = os.environ.get("SMU_BEDROCK_MODEL", "anthropic.claude-sonnet-4-6")
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
CLIENT_KIND = os.environ.get("SMU_BEDROCK_CLIENT", "legacy").lower()

# --- Local model via Ollama (llama3, etc.) ---------------------------------- #
OLLAMA_HOST = (os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3")

_SYSTEM = (
    "You extract recruitment-relevant facts about a STARTUP from the text of its own "
    "website. Identify the founder(s)/co-founder(s) by name and role, any email that "
    "clearly belongs to a named person (never invent an address - leave email empty if "
    "it is not on the page), a short industry label (e.g. 'FinTech', 'HealthTech', "
    "'Climate'), and a one-line description. Prefer a named founder over a generic "
    "inbox. If a fact is not present, use an empty string / empty list."
)

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "industry": {"type": "string"},
        "description": {"type": "string"},
        "founders": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "email": {"type": "string"},
                },
                "required": ["name", "role", "email"],
            },
        },
    },
    "required": ["industry", "description", "founders"],
}


def _build_prompt(startup: Startup, page_text: str) -> str:
    return (
        f"Startup: {startup.name}\nWebsite: {startup.url}\n\n"
        f"Page text (truncated):\n{page_text[:14000]}\n\n"
        "Extract the fields. Respond ONLY with a single JSON object of the form "
        '{"industry": "", "description": "", "founders": [{"name": "", "role": "", '
        '"email": ""}]} and nothing else.'
    )


def _parse_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- #
# Backend: Google AI Studio (Gemini) - plain REST, no SDK needed
# --------------------------------------------------------------------------- #
def _gemini_available() -> bool:
    return bool(GEMINI_API_KEY)


def _gemini_json(system: str, user: str, schema: dict | None = None) -> dict | None:
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 800,
            "responseMimeType": "application/json",   # ask Gemini for pure JSON
        },
    }
    payload = json.dumps(body).encode("utf-8")
    # Retry on 429/5xx with backoff - the free tier rate-limits bursts, and a swallowed
    # 429 is exactly why most rows silently fell back to the deterministic extractor.
    for attempt in range(4):
        req = urllib.request.Request(
            _GEMINI_URL.format(model=GEMINI_MODEL), data=payload,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            parts = data["candidates"][0]["content"]["parts"]
            return _parse_json("".join(p.get("text", "") for p in parts))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < 3:
                time.sleep(2 * (attempt + 1))       # 2s, 4s, 6s
                continue
            return None
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError,
                json.JSONDecodeError):
            return None
    return None


# --------------------------------------------------------------------------- #
# Backend: AWS Bedrock (Claude via the Anthropic SDK)
# --------------------------------------------------------------------------- #
def _client_class():
    """The Bedrock client class to use, honouring SMU_BEDROCK_CLIENT."""
    try:
        import anthropic
    except ImportError:
        return None
    mantle = getattr(anthropic, "AnthropicBedrockMantle", None)
    legacy = getattr(anthropic, "AnthropicBedrock", None)
    if CLIENT_KIND == "mantle":
        return mantle or legacy
    return legacy or mantle          # "legacy" (default) and any other value


def _bedrock_available() -> bool:
    if _client_class() is None:
        return False
    try:
        import botocore  # noqa: F401 - needed for AWS request signing
    except ImportError:
        return False
    return True


_client = None


def _get_client():
    global _client
    if _client is None:
        _client = _client_class()(aws_region=REGION)
    return _client


def _bedrock_json(system: str, user: str, schema: dict | None = None) -> dict | None:
    client = _get_client()
    # Prefer structured outputs (when a schema is given); if the SDK/model rejects it, retry plainly.
    extras = [{"output_config": {"format": {"type": "json_schema", "schema": schema}}}, {}] \
        if schema else [{}]
    for extra in extras:
        try:
            msg = client.messages.create(
                model=MODEL, max_tokens=800, system=system,
                messages=[{"role": "user", "content": user}], **extra)
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            return _parse_json(text)
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- #
# Backend: local model via Ollama (llama3, mistral, qwen, ...) - local HTTP, no key
# --------------------------------------------------------------------------- #
_ollama_ok: bool | None = None


def _ollama_available() -> bool:
    """True if a local Ollama server is reachable. Cached after the first probe."""
    global _ollama_ok
    if _ollama_ok is None:
        try:
            with urllib.request.urlopen(OLLAMA_HOST + "/api/tags", timeout=3) as r:
                _ollama_ok = r.status == 200
        except Exception:
            _ollama_ok = False
    return _ollama_ok


def _ollama_json(system: str, user: str, schema: dict | None = None) -> dict | None:
    base = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {
            "temperature": 0,
            "num_predict": 1024,
            "num_ctx": 8192,        # fit the ~14k-char page text (Ollama defaults ~4k = truncation)
        },
    }
    # Prefer schema-constrained JSON (Ollama structured outputs; great on Qwen3/Gemma3);
    # fall back to plain "json" for models/versions that reject a schema.
    for fmt in ((schema, "json") if schema else ("json",)):
        payload = json.dumps(dict(base, format=fmt)).encode("utf-8")
        for attempt in range(3):
            req = urllib.request.Request(
                OLLAMA_HOST + "/api/chat", data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=300) as r:   # local models can be slow
                    data = json.loads(r.read().decode("utf-8", "replace"))
                parsed = _parse_json((data.get("message") or {}).get("content", ""))
                if parsed is not None:
                    return parsed
                break                       # got a reply but unparseable -> try next format
            except urllib.error.HTTPError:
                break                       # server rejected this format -> try next format
            except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError):
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return None                 # server unreachable -> the other format won't help
    return None


# --------------------------------------------------------------------------- #
# Public interface (unchanged): available / extract / apply
# --------------------------------------------------------------------------- #
def active_provider() -> str | None:
    """Which backend will be used, or None if none is usable."""
    if PROVIDER == "gemini":
        return "gemini" if _gemini_available() else None
    if PROVIDER == "bedrock":
        return "bedrock" if _bedrock_available() else None
    if PROVIDER == "ollama":
        return "ollama" if _ollama_available() else None
    # auto: prefer Gemini (simplest), then Bedrock, then a local Ollama server.
    if _gemini_available():
        return "gemini"
    if _bedrock_available():
        return "bedrock"
    if _ollama_available():
        return "ollama"
    return None


def available() -> bool:
    return active_provider() is not None


def complete_json(system: str, user: str, schema: dict | None = None) -> dict | None:
    """Run one system+user prompt through the active backend and return parsed JSON."""
    provider = active_provider()
    if provider == "gemini":
        return _gemini_json(system, user, schema)
    if provider == "bedrock":
        return _bedrock_json(system, user, schema)
    if provider == "ollama":
        return _ollama_json(system, user, schema)
    return None


def extract(startup: Startup, page_text: str) -> dict | None:
    """Return {industry, description, founders:[{name,role,email}]} or None on failure."""
    if not page_text.strip():
        return None
    return complete_json(_SYSTEM, _build_prompt(startup, page_text), _SCHEMA)


# --- Contact finding from web-search results (used by founder_lookup --llm-search) --- #
_CONTACT_SYSTEM = (
    "You find the public professional contact for ONE specific startup founder, using web "
    "search results and page excerpts provided to you. Return the founder's professional "
    "email and LinkedIn profile URL ONLY if the evidence clearly shows they belong to THIS "
    "person at THIS startup. Never invent an address or URL - if it is not clearly present, "
    "return an empty string. Prefer a personal/work email over a generic inbox."
)
_CONTACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "email": {"type": "string"},
        "linkedin_url": {"type": "string"},
    },
    "required": ["email", "linkedin_url"],
}


def find_contact(founder_name: str, startup_name: str, startup_url: str,
                 context: str) -> dict | None:
    """Extract {email, linkedin_url} for a founder from web-search context. None on failure."""
    if not context.strip():
        return None
    user = (
        f"Founder: {founder_name}\nStartup: {startup_name} ({startup_url})\n\n"
        f"Web search results and page excerpts:\n{context[:12000]}\n\n"
        'Return JSON of the form {"email": "", "linkedin_url": ""} for THIS founder only.'
    )
    return complete_json(_CONTACT_SYSTEM, user, _CONTACT_SCHEMA)


# --- Email inference from name + domain (LAST resort, used by pipeline) ------ #
_INFER_SYSTEM = (
    "You infer the SINGLE most likely professional email address for a named startup "
    "founder, given their name and the company's email domain. If the context contains "
    "example addresses at that company, COPY their pattern exactly (e.g. seeing "
    "'jane.doe@acme.io' means the company uses first.last@domain, so John Smith -> "
    "john.smith@acme.io). If there is no example, use the most common convention. Use ONLY "
    "the given domain. This is a best-effort GUESS, not a verified address: return your "
    "single best candidate and a confidence 0-1. If you cannot form a plausible address, "
    "return an empty email."
)
_INFER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "email": {"type": "string"},
        "pattern": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["email", "pattern", "confidence"],
}


def infer_email(founder_name: str, domain: str, startup_name: str = "",
                context: str = "") -> dict | None:
    """Best-effort GUESS of a founder's email from their name + company domain.

    Returns {email, pattern, confidence} or None. The email is UNVERIFIED - this is a
    last resort for when no real contact was found by any other method. `context` may
    contain example addresses at the company so the model can copy the real pattern.
    """
    if not founder_name or not domain:
        return None
    user = (
        f"Founder: {founder_name}\n"
        f"Company: {startup_name or domain}\n"
        f"Email domain: {domain}\n"
    )
    if context.strip():
        user += ("\nContext (may contain example addresses that reveal the company's "
                 f"email pattern):\n{context[:4000]}\n")
    user += (
        f'\nReturn JSON of the form {{"email": "", "pattern": "", "confidence": 0.0}} with '
        f'the single most likely address at @{domain} for this founder.'
    )
    return complete_json(_INFER_SYSTEM, user, _INFER_SCHEMA)


def apply(startup: Startup, data: dict) -> None:
    """Apply an extract() result to the Startup in place.

    The LLM is AUTHORITATIVE for founders: when it returns a non-empty founder list,
    that list REPLACES the deterministic one (so regex noise never survives). Any email
    or LinkedIn URL already found on the page is preserved by matching on the founder's
    name, so we never lose real scraped data the model happened to omit. If the LLM
    returns no founders, the deterministic result is kept untouched.
    """
    if not data:
        return
    startup.industry = str(data.get("industry") or "") or startup.industry
    startup.description = str(data.get("description") or "") or startup.description

    llm_founders = [f for f in (data.get("founders") or [])
                    if str(f.get("name") or "").strip()]
    if not llm_founders:
        return                              # LLM found no founders -> keep deterministic

    prior = {f.name.lower(): f for f in startup.founders}   # preserve scraped emails/linkedin
    new_list: list[Founder] = []
    seen: set[str] = set()
    for f in llm_founders:
        name = str(f.get("name")).strip()
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        p = prior.get(key)
        email = str(f.get("email") or "").strip()
        if email:
            status, src = "scraped", "llm"          # model is instructed to use on-page emails only
        elif p and p.email:
            email, status, src = p.email, p.email_status, p.source   # inherit page-scraped email
        else:
            email, status, src = "", "", ""
        new_list.append(Founder(
            name=name, role=str(f.get("role") or ""), email=email,
            email_status=status, source=src,
            linkedin_url=(p.linkedin_url if p else ""),
        ))
    startup.founders = new_list
