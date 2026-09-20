from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from rapidfuzz.fuzz import ratio


MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
BARE_URL_RE = re.compile(r"https?://[^\s<>\])}\"']+")
SOCIAL_OR_DIRECTORY_DOMAINS = {
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "medium.com",
    "crunchbase.com",
    "wellfound.com",
    "angel.co",
    "producthunt.com",
}

# Accelerators / incubators / investors and startup databases whose portfolio or profile
# pages get mis-scored as a startup's OWN site (e.g. Mito Robotics -> massrobotics.org).
INCUBATOR_INVESTOR_DOMAINS = {
    "ycombinator.com", "techstars.com", "500.co", "plugandplaytechcenter.com",
    "alchemistaccelerator.com", "gener8tor.com", "masschallenge.org", "startx.com",
    "massrobotics.org", "third-derivative.org", "activate.org", "skydeck.berkeley.edu",
    "dmz.torontomu.ca", "antler.co", "afore.vc",
    "pitchbook.com", "tracxn.com", "cbinsights.com", "dealroom.co", "f6s.com",
    "gust.com", "eu-startups.com", "startupblink.com", "golden.com", "owler.com",
    "zoominfo.com", "rocketreach.co", "signalhire.com", "getlatka.com", "vcbacked.co",
}


def _host(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def _domain_stem(host: str) -> str:
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return parts[0] if parts else ""
    # This intentionally stays dependency-free. For common domains, the second-level
    # label is usually the useful company token; scoring is conservative enough to
    # avoid auto-selecting weak matches.
    return parts[-2]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _is_excluded_host(host: str) -> bool:
    """True if the host must never be accepted as a startup's OWN website - social
    networks, directories, accelerators/investors, or academic/government domains."""
    if not host:
        return True
    for d in SOCIAL_OR_DIRECTORY_DOMAINS | INCUBATOR_INVESTOR_DOMAINS:
        if host == d or host.endswith("." + d):
            return True
    if re.search(r"\.(edu|gov)(\.[a-z]{2,3})?$", host):   # .edu, .gov, .edu.sg, .gov.uk
        return True
    if ".ac." in host or host.endswith(".ac"):            # academic (cam.ac.uk, univ-x.ac.jp)
        return True
    return False


def canonicalize_website(url: str) -> str:
    value = (url or "").strip().strip("<>[](){}.,;\"'")
    if not value or value.lower().startswith(("mailto:", "tel:", "javascript:")):
        return ""
    if value.startswith("//"):
        value = "https:" + value
    elif "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    clean = parsed._replace(fragment="").geturl()
    return clean.rstrip("/")



def is_probable_startup_website(url: str, source_url: str = "") -> bool:
    canonical = canonicalize_website(url)
    if not canonical:
        return False
    host = _host(canonical)
    if not host:
        return False
    if _is_excluded_host(host):
        return False
    source_host = _host(source_url)
    if source_host and (host == source_host or host.endswith("." + source_host)):
        return False
    return True

def discover_startup_website(startup_name: str, markdown: str, source_url: str) -> tuple[str, float, str]:
    """Conservatively infer a startup website from links already present on a source page.

    Returns (url, confidence, method). Weak matches are deliberately rejected so that
    a wrong company domain is not crawled for contact data.
    """
    source_host = _host(source_url)
    company_norm = _norm(startup_name)
    if not company_norm:
        return "", 0.0, ""

    candidates: dict[str, tuple[str, float]] = {}

    for anchor, href in MARKDOWN_LINK_RE.findall(markdown or ""):
        absolute = canonicalize_website(urljoin(source_url, href))
        if not absolute:
            continue
        host = _host(absolute)
        if not host or host == source_host or host.endswith("." + source_host):
            continue
        if _is_excluded_host(host):
            continue
        anchor_norm = _norm(anchor)
        stem_norm = _norm(_domain_stem(host))
        anchor_score = ratio(company_norm, anchor_norm) if anchor_norm else 0
        domain_score = ratio(company_norm, stem_norm) if stem_norm else 0
        contains_bonus = 12 if (company_norm in anchor_norm or anchor_norm in company_norm) and anchor_norm else 0
        score = min(100.0, 0.58 * anchor_score + 0.42 * domain_score + contains_bonus)
        prev = candidates.get(host)
        if prev is None or score > prev[1]:
            candidates[host] = (absolute, score)

    # Bare URLs are useful when Markdown conversion loses anchor text.
    for href in BARE_URL_RE.findall(markdown or ""):
        absolute = canonicalize_website(href)
        if not absolute:
            continue
        host = _host(absolute)
        if not host or host == source_host or host.endswith("." + source_host):
            continue
        if _is_excluded_host(host):
            continue
        stem_norm = _norm(_domain_stem(host))
        score = float(ratio(company_norm, stem_norm)) if stem_norm else 0.0
        prev = candidates.get(host)
        if prev is None or score > prev[1]:
            candidates[host] = (absolute, score)

    if not candidates:
        return "", 0.0, ""

    best_url, best_score = max(candidates.values(), key=lambda item: item[1])
    if best_score < 68:
        return "", round(best_score / 100, 3), "weak_source_page_link_match"
    return best_url, round(best_score / 100, 3), "source_page_link_match"


def discover_startup_website_from_search(
    startup_name: str,
    search_results: list[dict],
    existing_source_urls: list[str] | None = None,
) -> tuple[str, float, str, str]:
    """Resolve a probable official startup site from search results.

    Returns (url, confidence, relationship, evidence). The function is conservative:
    social networks, directories, and already-known university/source domains are excluded.
    """
    company_norm = _norm(startup_name)
    if not company_norm:
        return "", 0.0, "uncertain", ""

    source_hosts = {_host(u) for u in (existing_source_urls or []) if _host(u)}
    scored: list[tuple[float, str, str]] = []
    for result in search_results:
        url = canonicalize_website(str(result.get("url", "")))
        if not url:
            continue
        host = _host(url)
        if not host or host in source_hosts or any(host.endswith("." + s) for s in source_hosts):
            continue
        if _is_excluded_host(host):
            continue
        title = str(result.get("title", ""))
        description = str(result.get("description", ""))
        stem_norm = _norm(_domain_stem(host))
        title_norm = _norm(title)
        text_norm = _norm(title + " " + description)
        domain_score = ratio(company_norm, stem_norm) if stem_norm else 0
        title_score = ratio(company_norm, title_norm) if title_norm else 0
        exact_bonus = 16 if company_norm and company_norm in text_norm else 0
        home_bonus = 5 if urlparse(url).path in {"", "/"} else 0
        score = min(100.0, 0.52 * domain_score + 0.38 * title_score + exact_bonus + home_bonus)
        evidence = f"Search result: {title} — {url}"
        scored.append((score, url, evidence))

    if not scored:
        return "", 0.0, "uncertain", ""
    score, url, evidence = max(scored, key=lambda x: x[0])
    # Accept moderate matches too: a name rarely equals its domain stem exactly, and a
    # wrong domain is caught downstream by email verification. Below this floor, stay blank.
    if score < 64:
        return "", round(score / 100, 3), "uncertain", evidence
    return url, round(score / 100, 3), "official_company", evidence
