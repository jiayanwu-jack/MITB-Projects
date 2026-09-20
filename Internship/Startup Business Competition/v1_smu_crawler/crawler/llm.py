"""
OPTIONAL Claude refinement of the chosen contact.

The crawler is fully functional without this - discovery + deterministic ranking pick a
contact on their own. When ANTHROPIC_API_KEY is set and --llm is passed, Claude is asked
to (a) confirm the single best contact from the EXTRACTED candidates (it may not invent
one) and (b) name the contact/role from the page context. This mirrors the main project's
'model chooses among real candidates' constraint.

Model is a single swappable string; structured outputs would be the production choice,
here we keep it dependency-light and parse a small JSON object.
"""
from __future__ import annotations

import json
import os
import re

from .models import Candidate, Target

MODEL = os.environ.get("SMU_CRAWLER_MODEL", "claude-sonnet-4-6")

_ORG_SYSTEM = (
    "You select the single best RECRUITMENT contact from emails already extracted from a "
    "web page. You MUST choose an email from the provided candidates - never invent one. "
    "For a public organisation strongly prefer a generic org inbox (info@/hello@/contact@) "
    "and never choose an address that belongs to a named individual. Reply with a single "
    "JSON object: {\"email\": ..., \"name\": ..., \"role\": ..., \"confidence\": 0..1}."
)
_STARTUP_SYSTEM = (
    "You select the single best RECRUITMENT contact from emails already extracted from a "
    "startup's web page. You MUST choose an email from the provided candidates - never "
    "invent one. Prefer the founder's or co-founder's own named email over a generic inbox "
    "(info@/hello@/contact@) - the goal is to reach the person running the startup. Reply "
    "with a single JSON object: {\"email\": ..., \"name\": ..., \"role\": ..., "
    "\"confidence\": 0..1}."
)


def available() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def refine(target: Target, candidates: list[Candidate]) -> dict | None:
    """Return {email,name,role,confidence} or None if unavailable/failed."""
    if not available() or not candidates:
        return None
    import anthropic
    client = anthropic.Anthropic()
    cand_json = [{"email": c.email, "source": c.source, "context": c.context[:200]}
                 for c in candidates]
    prompt = (
        f"Target: {target.name} ({target.kind}, {target.country})\n"
        f"Candidates:\n{json.dumps(cand_json, indent=1)}\n\n"
        "Pick the best contact per the rules."
    )
    system = _STARTUP_SYSTEM if target.kind == "startup" else _ORG_SYSTEM
    try:
        msg = client.messages.create(
            model=MODEL, max_tokens=400,
            system=system + "\n\nRespond ONLY with the JSON object.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", text, re.DOTALL)
        data = json.loads(m.group(0)) if m else None
    except Exception:
        return None
    if not data:
        return None
    valid = {c.email.lower() for c in candidates}
    if str(data.get("email", "")).lower() not in valid:
        return None                      # off-list -> ignore, keep deterministic choice
    return data
