"""
Deterministic HTML extraction - no LLM here.

  * clean_text / page_title       - readable text + title.
  * extract_emails                - emails with surrounding context (mailto + text,
                                    light de-obfuscation, asset/vendor filtering).
  * is_role_inbox                 - generic inbox (info@/contact@) vs a person.
  * registrable_domain            - www.acme.io -> acme.io.
  * find_internal_links           - same-site about/team/contact/founder links to crawl.
  * find_external_links           - outbound startup links from a portfolio/winners page.
  * guess_people                  - best-effort (name, role, email) triples from text,
                                    the deterministic fallback when Bedrock is unavailable.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_STRIP = ["script", "style", "noscript"]

_EMAIL_BLOCK_SUBSTR = ("@2x", "@3x", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
_EMAIL_BLOCK_DOMAINS = (
    "example.com", "example.org", "domain.com", "email.com", "sentry.io",
    "wixpress.com", "godaddy.com", "squarespace.com",
)

_ROLE_TOKENS = {
    "info", "hello", "hi", "hey", "contact", "contactus", "enquiry", "enquiries",
    "inquiries", "admin", "office", "team", "general", "support", "help", "mail",
    "email", "press", "media", "sales", "careers", "jobs", "hr", "partnerships",
}

_FOUNDER_HINTS = {
    "founder": 6, "co-founder": 6, "cofounder": 6, "ceo": 5, "team": 3,
    "about": 2, "people": 3, "leadership": 4, "our-team": 4, "founders": 6,
}
_CONTACT_HINTS = {"contact": 5, "connect": 2}

_PORTFOLIO_HINTS = (
    "portfolio", "our startups", "startups we", "companies", "alumni ventures",
    "success stories", "success story", "past winners", "winners", "finalists",
    "cohort", "spin-out", "spinout", "spin out", "invested",
)
_SKIP_LINK_DOMAINS = (
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "youtube.com", "youtu.be", "tiktok.com", "wikipedia.org", "google.com",
    "goo.gl", "apple.com", "play.google.com", "medium.com", "eventbrite.com",
    "crunchbase.com", "github.com", "wixpress.com", "squarespace.com",
)

_TITLES = ("Co-Founder", "Cofounder", "Co Founder", "Founder", "CEO",
           "Chief Executive Officer", "Managing Director", "President", "CTO",
           "COO", "Director")
# First + last only (exactly two capitalised words). Allowing a third word lets a
# leading company/section word glue on ("Biomedical Dhruv Agarwal"); two words avoids
# that. Middle names are rare and the LLM handles them when it runs.
_NAME = r"[A-Z][a-zA-Z'’\-]+\s+[A-Z][a-zA-Z'’\-]+"
_TITLE_ALT = "|".join(re.escape(t) for t in _TITLES)
# Separator between name and title. Allows comma/colon/pipe, a SPACED dash, or plain
# whitespace - but NOT a bare hyphen glued to a word, so the internal hyphen of
# "Co-Founder" can no longer split a name into "... Co" + "Founder".
_SEP = r"(?:\s*[,:|]\s*|\s+[-–—]\s+|\s+)"
# Only the TITLE is case-insensitive (scoped (?i:...)); the NAME stays case-sensitive so
# its [A-Z] anchors still require real capitalised names.
_NAME_THEN_TITLE = re.compile(rf"({_NAME}){_SEP}\b((?i:{_TITLE_ALT}))\b")
_TITLE_THEN_NAME = re.compile(rf"\b((?i:{_TITLE_ALT}))\b{_SEP}({_NAME})")

# Words that are never part of a real person's name - used to trim/reject bad captures
# (title fragments that slipped in, or page boilerplate like "Our Team").
_NON_NAME = {
    "our", "the", "meet", "contact", "about", "team", "home", "welcome", "us",
    "company", "startup", "startups", "founders", "leadership", "and",
    "co", "cofounder", "founder", "ceo", "cto", "coo", "cfo", "cmo",
    "president", "director", "managing", "chief", "executive", "officer",
}


# Local-part tokens that mean an address is NOT a person (role/dept/company suffix).
_NON_PERSON_LOCAL = {
    "info", "hello", "hi", "contact", "admin", "office", "team", "sales", "support",
    "help", "hr", "gm", "cs", "pr", "it", "press", "media", "careers", "jobs",
    "enquiries", "enquiry", "mail", "billing", "accounts", "finance", "legal",
    "marketing", "partnerships", "invest", "investors", "general", "no", "noreply",
    "gmbh", "ltd", "inc", "llc", "co", "corp", "group", "company", "india", "usa",
}


def _clean_person_name(name: str) -> str:
    """Trim leading/trailing non-name words; return '' if it isn't a plausible full name."""
    def norm(t: str) -> str:
        return re.sub(r"[^a-z]", "", t.lower())
    toks = name.split()
    while toks and norm(toks[0]) in _NON_NAME:
        toks.pop(0)
    while toks and norm(toks[-1]) in _NON_NAME:
        toks.pop()
    if len(toks) < 2 or any(norm(t) in _NON_NAME for t in toks):
        return ""                          # need first + last, and no boilerplate words
    return " ".join(toks)


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


def registrable_domain(host: str) -> str:
    host = host.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("ac", "edu", "co", "com", "org", "gov", "net"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


_TITLE_SEP = re.compile(r"[|\-–—:·•»]")
_GENERIC_NAME = {"", "home", "homepage", "home page", "welcome", "index", "main",
                 "official website", "official site"}


def prettify_domain(domain: str) -> str:
    """acme-robotics.io -> 'Acme Robotics' (last-resort display name)."""
    core = registrable_domain(domain).split(".")[0] if domain else ""
    core = re.sub(r"[-_]+", " ", core).strip()
    return core.title() if core else (domain or "")


def looks_like_url_name(name: str, domain: str = "") -> bool:
    """True if `name` is empty or is really a URL/host, not a company name."""
    n = (name or "").strip().lower()
    if not n:
        return True
    if "://" in n or n.startswith(("http", "www.")):
        return True
    if re.fullmatch(r"[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}", n):     # bare host like acme.io
        return True
    return bool(domain) and n == registrable_domain(domain)


def _brand_from_title(title: str, domain: str = "") -> str:
    """Pick the brand segment out of a page <title> like 'Tagline | Acme Robotics'."""
    segs = [s.strip() for s in _TITLE_SEP.split(title) if s.strip()]
    if not segs:
        return ""
    core = re.sub(r"[^a-z0-9]", "", registrable_domain(domain).split(".")[0]) if domain else ""
    if core:
        for s in segs:                        # prefer the segment matching the domain
            flat = re.sub(r"[^a-z0-9]", "", s.lower())
            if flat and (flat.startswith(core) or core.startswith(flat) or core in flat):
                return s
    segs = [s for s in segs if s.lower() not in _GENERIC_NAME] or segs
    return min(segs, key=len)                 # brands are usually the shortest segment


def company_name(html: str, domain: str = "") -> str:
    """Best display name for a company from its own page:
    og:site_name -> brand from <title> -> <h1> -> prettified domain."""
    soup = _soup(html)
    for sel in ('meta[property="og:site_name"]', 'meta[name="application-name"]'):
        tag = soup.select_one(sel)
        val = (tag.get("content") or "").strip() if tag else ""
        if val and val.lower() not in _GENERIC_NAME:
            return val
    if soup.title and soup.title.string:
        brand = _brand_from_title(soup.title.string.strip(), domain)
        if brand and brand.lower() not in _GENERIC_NAME:
            return brand
    h1 = soup.find("h1")
    if h1:
        t = h1.get_text(" ", strip=True)
        if t and t.lower() not in _GENERIC_NAME:
            return t
    return prettify_domain(domain)


def is_role_inbox(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    tokens = {t for t in re.split(r"[._\-+]", local) if t}
    return bool(tokens & _ROLE_TOKENS) or local in _ROLE_TOKENS


def _valid_email(email: str) -> bool:
    low = email.lower()
    if any(s in low for s in _EMAIL_BLOCK_SUBSTR):
        return False
    dom = low.split("@", 1)[-1]
    return not any(dom == d or dom.endswith("." + d) for d in _EMAIL_BLOCK_DOMAINS)


def _deobfuscate(text: str) -> str:
    text = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", text, flags=re.I)
    text = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", text, flags=re.I)
    return text


def extract_emails(html: str) -> list[tuple[str, str]]:
    """Return (email, context) pairs, mailto links first."""
    soup = _soup(html)
    for t in soup(["script", "style"]):
        t.decompose()
    found: dict[str, str] = {}

    for a in soup.select('a[href^="mailto:"]'):
        email = a.get("href", "").split("mailto:", 1)[-1].split("?", 1)[0].strip()
        if not EMAIL_RE.fullmatch(email) or not _valid_email(email):
            continue
        cont = a.find_parent(["p", "li", "td", "div", "section"]) or a
        ctx = re.sub(r"\s+", " ", cont.get_text(" ", strip=True))[:240]
        found.setdefault(email.lower(), ctx)

    text = _deobfuscate(re.sub(r"\s+", " ", soup.get_text(" ", strip=True)))
    for m in EMAIL_RE.finditer(text):
        key = m.group(0).lower()
        if key in found or not _valid_email(m.group(0)):
            continue
        s, e = max(0, m.start() - 120), min(len(text), m.end() + 120)
        found[key] = text[s:e].strip()
    return list(found.items())


def looks_like_portfolio(url: str, title: str, text: str) -> bool:
    hay = (url + " " + title + " " + text[:600]).lower()
    return any(h in hay for h in _PORTFOLIO_HINTS)


def find_internal_links(html: str, base_url: str) -> list[str]:
    """Same-site about/team/contact/founder links, best-first."""
    soup = _soup(html)
    base_host = urlparse(base_url).netloc
    scored: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        absolute = urljoin(base_url, a["href"]).split("#", 1)[0]
        pu = urlparse(absolute)
        if pu.scheme not in ("http", "https"):
            continue
        if pu.netloc and pu.netloc != base_host:
            continue
        hay = (a["href"] + " " + a.get_text(" ", strip=True)).lower()
        score = sum(w for h, w in _FOUNDER_HINTS.items() if h in hay)
        score += sum(w for h, w in _CONTACT_HINTS.items() if h in hay)
        if score:
            scored[absolute] = max(scored.get(absolute, 0), score)
    return [u for u, _ in sorted(scored.items(), key=lambda kv: kv[1], reverse=True)]


def find_external_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """(name, url) for outbound links that look like a startup's own site."""
    soup = _soup(html)
    base_reg = registrable_domain(urlparse(base_url).netloc)
    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        absolute = urljoin(base_url, a["href"]).split("#", 1)[0]
        pu = urlparse(absolute)
        if pu.scheme not in ("http", "https") or not pu.netloc:
            continue
        reg = registrable_domain(pu.netloc)
        if reg == base_reg:
            continue
        if any(reg == d or reg.endswith("." + d) for d in _SKIP_LINK_DOMAINS):
            continue
        name = re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:80] or pu.netloc
        if len(name) < 2:
            continue
        out.setdefault(absolute, name)
    return [(name, url) for url, name in out.items()]


def guess_people(html: str) -> list[tuple[str, str, str]]:
    """Deterministic (name, role, email) triples from a team/about page.

    Fallback for when the Bedrock extractor is unavailable. Matches "Name, Title"
    or "Title Name" near the text, and pairs each with the nearest email if one is
    present in the same block; also derives a display name from a personal email's
    local-part (jane.tan@acme.io -> "Jane Tan") when no title is found.
    """
    soup = _soup(html)
    for t in soup(["script", "style"]):
        t.decompose()
    people: dict[str, tuple[str, str, str]] = {}

    for block in soup.find_all(["li", "div", "section", "article", "p", "td"]):
        btext = re.sub(r"\s+", " ", block.get_text(" ", strip=True))
        if not btext or len(btext) > 400:
            continue
        m = _NAME_THEN_TITLE.search(btext) or None
        name = role = ""
        if m:
            name, role = m.group(1).strip(), m.group(2).strip()
        else:
            m = _TITLE_THEN_NAME.search(btext)
            if m:
                role, name = m.group(1).strip(), m.group(2).strip()
        name = _clean_person_name(name)
        if not name:
            continue
        email = ""
        em = EMAIL_RE.search(_deobfuscate(btext))
        if em and _valid_email(em.group(0)) and not is_role_inbox(em.group(0)):
            email = em.group(0)
        people.setdefault(name.lower(), (name, role, email))

    # Also: personal emails whose local-part yields a plausible name.
    for email, _ in extract_emails(html):
        if is_role_inbox(email):
            continue
        local = email.split("@", 1)[0]
        parts = [p for p in re.split(r"[._\-+0-9]+", local) if p.isalpha() and len(p) > 1]
        if len(parts) < 2:
            continue
        if any(p.lower() in _NON_PERSON_LOCAL for p in parts):
            continue                                   # gm.india, rjs.gmbh, cs_ua, ...
        if not any(len(p) >= 3 for p in parts):
            continue                                   # need at least one real word (not "cs ua")
        name = _clean_person_name(" ".join(p.capitalize() for p in parts[:2]))
        if name:
            people.setdefault(name.lower(), (name, "", email))
    return list(people.values())
