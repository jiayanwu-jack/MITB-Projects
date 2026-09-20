"""
Deterministic HTML extraction - no LLM here.

  * clean_text / page_title        - readable text and title.
  * extract_emails(html, url,...)  - candidate email contacts with context, with light
                                     de-obfuscation ("a [at] b [dot] c"). Marks a page
                                     as a directory (many emails) so the ranker can
                                     refuse to harvest individuals from a people list.
  * is_role_inbox                  - functional/org inbox vs a named individual.
  * find_links                     - internal links scored by how office/contact-like
                                     they look (drives the multi-hop discovery).
  * looks_like_office_page         - does this page look like an entrepreneurship/
                                     innovation office (boosts contacts found there).
  * looks_like_portfolio_page /
    find_portfolio_links           - does this page list startups (portfolio, success
                                     stories, competition winners) and, if so, the
                                     external startup sites it links out to.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .models import Candidate

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_STRIP = ["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]

# Emails we never want (assets, trackers, placeholders, vendors).
_EMAIL_BLOCK_SUBSTR = ("@2x", "@3x", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
_EMAIL_BLOCK_DOMAINS = (
    "example.com", "example.org", "domain.com", "email.com", "sentry.io",
    "wixpress.com", "godaddy.com", "squarespace.com", "sentry-next.wixpress.com",
)

# Role/functional inbox local-parts -> contact the organisation, not a person.
_ROLE_TOKENS = {
    "info", "hello", "hi", "hey", "contact", "contactus", "enquiry", "enquiries",
    "inquiries", "admin", "office", "team", "general", "startups", "startup",
    "apply", "applications", "connect", "community", "events", "programs",
    "programmes", "entrepreneurship", "entrepreneur", "innovation", "incubator",
    "accelerator", "ventures", "enterprise", "support", "help", "mail", "email",
    "ie", "eship", "hola", "press", "media", "partnerships",
}

_OFFICE_HINTS = {
    "entrepreneur": 6, "entrepreneurship": 6, "innovation": 5, "incubator": 5,
    "accelerator": 5, "startup": 4, "start-up": 4, "enterprise": 4, "venture": 4,
    "ventures": 4, "i&e": 4, "e-ship": 4,
}
_CONTACT_HINTS = {
    "contact": 5, "people": 3, "staff": 3, "team": 3, "directory": 3,
    "connect": 2, "about": 1, "founder": 6, "founders": 6, "leadership": 3,
}
_MAX_EMAILS_BEFORE_DIRECTORY = 10       # a page with more looks like a people directory

# Pages that list actual startup companies - portfolios, alumni ventures, competition
# results - as opposed to pages that merely talk about entrepreneurship in the abstract.
_PORTFOLIO_HINTS = {
    "portfolio": 6, "our startups": 6, "startups we support": 6, "alumni ventures": 5,
    "success stories": 4, "success story": 4, "competition winners": 5,
    "past winners": 5, "spin-out": 4, "spinout": 4, "spin out": 4, "cohort": 3,
}

# Hosts that are never a startup's own site - social/vendor/document platforms.
_SKIP_LINK_DOMAINS = (
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "youtube.com", "youtu.be", "tiktok.com", "wikipedia.org", "google.com",
    "goo.gl", "apple.com", "play.google.com", "medium.com", "eventbrite.com",
    "wixpress.com", "godaddy.com", "squarespace.com",
)


def _deobfuscate(text: str) -> str:
    """Turn 'name [at] domain [dot] com' style obfuscation into real addresses."""
    text = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", text, flags=re.I)
    text = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", text, flags=re.I)
    return text


def _valid_email(email: str) -> bool:
    low = email.lower()
    if any(s in low for s in _EMAIL_BLOCK_SUBSTR):
        return False
    domain = low.split("@", 1)[-1]
    return not any(domain == d or domain.endswith("." + d) for d in _EMAIL_BLOCK_DOMAINS)


def is_role_inbox(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    tokens = {t for t in re.split(r"[._\-+]", local) if t}
    return bool(tokens & _ROLE_TOKENS) or local in _ROLE_TOKENS


def registrable_domain(host: str) -> str:
    """Best-effort registrable domain, e.g. 'www.nus.edu.sg' -> 'nus.edu.sg'."""
    host = host.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("ac", "edu", "co", "com", "org", "gov", "net"):
        return ".".join(parts[-3:])          # e.g. nus.edu.sg, iitb.ac.in
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def clean_text(html: str) -> str:
    soup = _soup(html)
    for t in soup(_STRIP):
        t.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    return re.sub(r"\s+", " ", main.get_text(" ", strip=True)).strip()


def page_title(html: str) -> str:
    soup = _soup(html)
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else ""


def looks_like_office_page(url: str, title: str, text: str) -> bool:
    hay = (url + " " + title + " " + text[:400]).lower()
    return any(h in hay for h in _OFFICE_HINTS)


def extract_emails(html: str, page_url: str, title: str, office_page: bool) -> list[Candidate]:
    soup = _soup(html)
    for t in soup(["script", "style"]):
        t.decompose()

    found: dict[str, Candidate] = {}

    # 1) mailto: links - the most reliable signal.
    for a in soup.select('a[href^="mailto:"]'):
        email = a.get("href", "").split("mailto:", 1)[-1].split("?", 1)[0].strip()
        if not EMAIL_RE.fullmatch(email) or not _valid_email(email):
            continue
        cont = a.find_parent(["p", "li", "td", "div", "section"]) or a
        ctx = re.sub(r"\s+", " ", cont.get_text(" ", strip=True))[:240]
        found.setdefault(email.lower(), Candidate(email=email, context=ctx, source="mailto",
                                                  page_url=page_url, page_title=title,
                                                  office_page=office_page))

    # 2) plain-text emails (incl. de-obfuscated).
    text = _deobfuscate(re.sub(r"\s+", " ", soup.get_text(" ", strip=True)))
    for m in EMAIL_RE.finditer(text):
        email = m.group(0)
        key = email.lower()
        if key in found or not _valid_email(email):
            continue
        s, e = max(0, m.start() - 120), min(len(text), m.end() + 120)
        found[key] = Candidate(email=email, context=text[s:e].strip(), source="text",
                               page_url=page_url, page_title=title, office_page=office_page)

    directory = len(found) > _MAX_EMAILS_BEFORE_DIRECTORY
    cands = list(found.values())
    for c in cands:
        c.directory_page = directory
    return cands


def looks_like_portfolio_page(url: str, title: str, text: str) -> bool:
    """Does this page list actual startups (portfolio / alumni ventures / winners)?"""
    hay = (url + " " + title + " " + text[:600]).lower()
    return any(h in hay for h in _PORTFOLIO_HINTS)


def find_portfolio_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Return (name_guess, absolute_url) for external links that look like a startup's
    own site, found on a portfolio/alumni-ventures/competition-winners style page."""
    soup = _soup(html)
    base_registrable = registrable_domain(urlparse(base_url).netloc)
    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        absolute = urljoin(base_url, a["href"]).split("#", 1)[0]
        pu = urlparse(absolute)
        if pu.scheme not in ("http", "https") or not pu.netloc:
            continue
        link_registrable = registrable_domain(pu.netloc)
        if link_registrable == base_registrable:
            continue                              # same site - not an external startup
        if any(link_registrable == d or link_registrable.endswith("." + d)
               for d in _SKIP_LINK_DOMAINS):
            continue                              # social/vendor host, not a startup site
        name = re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:80] or pu.netloc
        if len(name) < 2:
            continue
        out.setdefault(absolute, name)
    return [(name, url) for url, name in out.items()]


def find_links(html: str, base_url: str) -> list[tuple[int, str]]:
    """Return (score, absolute_url) for same-site links that look office/contact-like,
    or that look like a portfolio/alumni-ventures/competition-winners listing (so those
    pages actually get crawled, not just recognised after the fact)."""
    soup = _soup(html)
    base_host = urlparse(base_url).netloc
    scored: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        absolute = urljoin(base_url, a["href"]).split("#", 1)[0]
        pu = urlparse(absolute)
        if pu.scheme not in ("http", "https"):
            continue
        if pu.netloc and pu.netloc != base_host:
            continue                                   # stay on the same site
        hay = (a["href"] + " " + a.get_text(" ", strip=True)).lower()
        score = sum(w for h, w in _OFFICE_HINTS.items() if h in hay)
        score += sum(w for h, w in _CONTACT_HINTS.items() if h in hay)
        score += sum(w for h, w in _PORTFOLIO_HINTS.items() if h in hay)
        if score:
            scored[absolute] = max(scored.get(absolute, 0), score)
    return sorted(((s, u) for u, s in scored.items()), reverse=True)
