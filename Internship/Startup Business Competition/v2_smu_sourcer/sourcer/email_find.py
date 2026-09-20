"""
Founder email finding: scrape -> pattern-guess -> verify.

Order of preference for each founder:
  1. An email already found on the site (email_status="scraped") - trusted as-is.
  2. Otherwise generate common patterns from the name + the startup domain
     (jane.tan@acme.io, jtan@acme.io, ...) and mark the best one "guessed".
  3. Verify the domain can receive mail (MX record) when dnspython is installed;
     a domain with MX upgrades the guess to "mx_ok". Real per-address SMTP probing
     is intentionally NOT done by default - it is unreliable and can harm sender
     reputation. Every "guessed"/"mx_ok" address must be human-reviewed before use.
"""
from __future__ import annotations

import re

from .models import Founder

try:
    import dns.resolver  # type: ignore
    _HAVE_DNS = True
except Exception:
    _HAVE_DNS = False

_mx_cache: dict[str, bool] = {}


def _has_mx(domain: str) -> bool | None:
    """True/False if we could check, None if we can't (no dnspython)."""
    if not _HAVE_DNS:
        return None
    if domain in _mx_cache:
        return _mx_cache[domain]
    ok = False
    try:
        ok = len(dns.resolver.resolve(domain, "MX")) > 0
    except Exception:
        try:                                   # some domains accept mail on the A record
            ok = len(dns.resolver.resolve(domain, "A")) > 0
        except Exception:
            ok = False
    _mx_cache[domain] = ok
    return ok


def _name_parts(name: str) -> tuple[str, str]:
    parts = [re.sub(r"[^a-z]", "", p.lower()) for p in name.split()]
    parts = [p for p in parts if p]
    if not parts:
        return "", ""
    return parts[0], (parts[-1] if len(parts) > 1 else "")


def candidate_emails(name: str, domain: str) -> list[str]:
    first, last = _name_parts(name)
    if not domain or not first:
        return []
    pats = []
    if last:
        pats += [f"{first}.{last}", f"{first}{last}", f"{first[0]}{last}",
                 f"{first}_{last}", f"{last}.{first}", f"{first[0]}.{last}"]
    pats += [first]
    seen, out = set(), []
    for p in pats:
        e = f"{p}@{domain}"
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def fill_email(founder: Founder, domain: str) -> None:
    """Populate founder.email if empty, using pattern + MX verification."""
    if founder.email:                          # already scraped from the page
        if not founder.email_status:
            founder.email_status = "scraped"
        return
    cands = candidate_emails(founder.name, domain)
    if not cands:
        return
    mx = _has_mx(domain)
    founder.email = cands[0]                    # first.last is the most common default
    founder.source = "pattern"
    if mx is True:
        founder.email_status = "mx_ok"
    elif mx is False:
        founder.email = ""                      # domain can't receive mail - drop it
        founder.source = ""
    else:
        founder.email_status = "guessed"        # couldn't verify (no dnspython)
