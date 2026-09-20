"""
Deterministic ranking - pick ONE contact and score confidence.

  * From a directory-like page (many emails) we keep ONLY role inboxes - this avoids
    scraping an entire people list when we only need one contact.
  * For universities and non-startup organisations we prefer a role inbox (info@/
    contact@); a personal-looking address is kept only if nothing else exists, and is
    flagged needs_review.
  * For `startup` targets we want the opposite: the founder/co-founder's own name and
    email, not a generic inbox - so a named, personal address is preferred and a role
    inbox is only kept as a fallback.

Confidence is 0..1; below `REVIEW_THRESHOLD` is flagged for human verification before
outreach.
"""
from __future__ import annotations

import re

from .extract import is_role_inbox, registrable_domain
from .models import Candidate, Target

REVIEW_THRESHOLD = 0.6
_ROLE_KW = ("director", "head", "manager", "lead", "office", "programme", "program",
            "entrepreneur", "innovation", "incubator", "enterprise", "coordinator")
_FOUNDER_KW = ("founder", "co-founder", "cofounder", "ceo", "chief executive",
              "president", "managing director")

_NAME_RE = r"[A-Z][a-zA-Z'’\-]+(?:\s+[A-Z][a-zA-Z'’\-]+){1,2}"
_TITLES = ("Founder", "Co-Founder", "Cofounder", "CEO", "Chief Executive Officer",
          "Managing Director", "President", "Director", "Head", "Manager", "Lead",
          "Coordinator")
_TITLE_ALT = "|".join(re.escape(t) for t in _TITLES)
_NAME_THEN_TITLE = re.compile(rf"({_NAME_RE})\s*[,\-:|]\s*({_TITLE_ALT})", re.I)
_TITLE_THEN_NAME = re.compile(rf"({_TITLE_ALT})\s*[,\-:|]?\s+({_NAME_RE})", re.I)


def _domain_matches(email: str, target_url: str) -> bool:
    from urllib.parse import urlparse
    edom = email.split("@", 1)[-1].lower()
    tdom = registrable_domain(urlparse(target_url).netloc)
    return bool(tdom) and (edom == tdom or edom.endswith("." + tdom) or tdom.endswith("." + edom))


def guess_person(cand: Candidate, target: Target) -> tuple[str, str]:
    """Best-effort (name, role) guess from the page context around the chosen email."""
    ctx = cand.context
    m = _NAME_THEN_TITLE.search(ctx)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = _TITLE_THEN_NAME.search(ctx)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    if target.kind == "startup" and not is_role_inbox(cand.email):
        # No title nearby - fall back to turning the local-part into a display name
        # (e.g. jane.tan@acme.io -> "Jane Tan").
        local = cand.email.split("@", 1)[0]
        parts = [p for p in re.split(r"[._\-+0-9]+", local) if p.isalpha() and len(p) > 1]
        if len(parts) >= 2:
            return " ".join(p.capitalize() for p in parts[:2]), ""
    if is_role_inbox(cand.email):
        return "", "Organisation contact inbox"
    return "", ("Entrepreneurship / innovation office" if not target.is_org
               else "Organisation contact")


def _score(cand: Candidate, target: Target) -> float:
    s = 0.2
    role = is_role_inbox(cand.email)
    is_startup = target.kind == "startup"
    if is_startup:
        if not role:
            s += 0.35                    # a named founder beats a generic inbox
        if any(k in cand.context.lower() for k in _FOUNDER_KW):
            s += 0.20
    elif role:
        s += 0.40
    if cand.source == "mailto":
        s += 0.10
    if _domain_matches(cand.email, target.url):
        s += 0.20
    if cand.office_page:
        s += 0.15
    if any(k in cand.context.lower() for k in _ROLE_KW):
        s += 0.10
    if target.is_org and not is_startup and not role:
        s -= 0.30            # non-startup orgs must reach an inbox, not a person
    return max(0.0, min(1.0, s))


def choose(target: Target, candidates: list[Candidate]) -> tuple[Candidate | None, float, str]:
    """Return (best_candidate, confidence, note). note explains any preference applied."""
    # Directory guard: from many-email pages, keep only role inboxes.
    pool = [c for c in candidates if is_role_inbox(c.email) or not c.directory_page]
    dropped = len(candidates) - len(pool)
    note = ("Suppressed individual addresses from a people-directory page. "
            if dropped else "")

    if not pool:
        return None, 0.0, (note + "No usable contact.").strip()

    best = max(pool, key=lambda c: _score(c, target))
    conf = _score(best, target)

    if target.kind == "startup":
        if is_role_inbox(best.email):
            # A named founder/co-founder is worth more than a generic inbox here.
            personal = [c for c in pool if not is_role_inbox(c.email)]
            if personal:
                best = max(personal, key=lambda c: _score(c, target))
                conf = _score(best, target)
                note += "Chose a named founder contact over a generic inbox. "
    elif target.is_org and not is_role_inbox(best.email):
        # Prefer any role inbox even if lower-scored; else keep but flag hard.
        role_only = [c for c in pool if is_role_inbox(c.email)]
        if role_only:
            best = max(role_only, key=lambda c: _score(c, target))
            conf = _score(best, target)
            note += "Chose the organisational inbox over a personal address. "
        else:
            conf = min(conf, 0.4)
            note += ("Only a personal-looking address was found for this organisation; "
                     "verify it is an appropriate org contact, not an individual. ")
    return best, conf, note.strip()
