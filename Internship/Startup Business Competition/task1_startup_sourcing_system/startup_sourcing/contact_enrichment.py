from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urldefrag, urljoin, urlparse

from .website_discovery import canonicalize_website


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
LINKEDIN_PERSON_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9_%\-./?=&]+", re.I)
LINKEDIN_COMPANY_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/[A-Za-z0-9_%\-./?=&]+", re.I)

GENERIC_BLOCKLIST = {
    "example@example.com",
    "name@example.com",
    "email@example.com",
    "test@test.com",
    "hello@example.com",
}
FREE_MAIL_DOMAINS = {
    "gmail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
    "icloud.com", "proton.me", "protonmail.com", "hey.com",
}
GENERIC_ROLE_LOCALS = {
    "hello", "hi", "info", "contact", "contactus", "enquiry", "enquiries",
    "team", "office", "admin", "support", "help", "sales", "business",
    "partnerships", "partnership", "general", "mail", "inbox", "careers",
}
FOUNDER_ROLE_LOCALS = {
    "founder", "founders", "cofounder", "cofounders", "ceo", "ceooffice",
    "foundingteam", "leadership",
}
IGNORE_LOCAL_HINTS = {"noreply", "no-reply", "donotreply", "do-not-reply", "privacy", "legal", "abuse"}
RELEVANT_PATH_TERMS = {
    "contact": 100,
    "founder": 98,
    "team": 95,
    "leadership": 94,
    "people": 92,
    "about": 90,
    "company": 84,
    "story": 80,
    "who-we-are": 80,
    "management": 78,
    "connect": 76,
}
COMMON_PATHS = (
    "",
    "/team",
    "/our-team",
    "/founders",
    "/leadership",
    "/people",
    "/about",
    "/about-us",
    "/contact",
    "/contact-us",
    "/company",
)


@dataclass(frozen=True)
class EmailFinding:
    email: str
    email_type: str
    confidence: float
    source_url: str
    founder_match: str = ""
    origin: str = "website"
    provider: str = "public_web"
    verification_status: str = "unverified"
    verification_score: float = 0.0
    verification_provider: str = ""

    def as_dict(self) -> dict:
        return {
            "email": self.email,
            "email_type": self.email_type,
            "confidence": round(self.confidence, 3),
            "source_url": self.source_url,
            "founder_match": self.founder_match,
            "origin": self.origin,
            "provider": self.provider,
            "verification_status": self.verification_status,
            "verification_score": round(self.verification_score, 3),
            "verification_provider": self.verification_provider,
        }


def _deobfuscate_email_text(text: str) -> str:
    value = text or ""
    # Common public-site obfuscations, intentionally conservative.
    value = re.sub(r"\s*[\[(]\s*at\s*[\])]+\s*", "@", value, flags=re.I)
    value = re.sub(r"\s*[\[(]\s*dot\s*[\])]+\s*", ".", value, flags=re.I)
    return value


def extract_public_emails(text: str) -> list[str]:
    value = _deobfuscate_email_text(text)
    found: set[str] = set()
    for raw in EMAIL_RE.findall(value):
        email = raw.lower().strip(".,;:()[]{}<>\"'")
        if email in GENERIC_BLOCKLIST:
            continue
        local, _, domain = email.partition("@")
        if not local or not domain or domain.startswith(".") or domain.endswith("."):
            continue
        if any(hint in local for hint in IGNORE_LOCAL_HINTS):
            continue
        found.add(email)
    return sorted(found)


def extract_linkedin_urls(text: str) -> tuple[list[str], list[str]]:
    people = sorted({u.rstrip(".,;)\"]'") for u in LINKEDIN_PERSON_RE.findall(text or "")})
    companies = sorted({u.rstrip(".,;)\"]'") for u in LINKEDIN_COMPANY_RE.findall(text or "")})
    return people, companies


def _ascii_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", normalized.lower())


def _name_parts(name: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    parts = re.findall(r"[A-Za-z0-9]+", normalized.lower())
    return [p for p in parts if len(p) >= 2 and p not in {"dr", "mr", "mrs", "ms", "prof"}]


def _founder_local_patterns(name: str) -> set[str]:
    parts = _name_parts(name)
    if not parts:
        return set()
    first = parts[0]
    last = parts[-1]
    patterns = {first, last}
    if len(parts) >= 2:
        patterns |= {
            first + last,
            f"{first}.{last}",
            f"{first}_{last}",
            f"{first}-{last}",
            first[0] + last,
            first + last[0],
        }
    return patterns


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _same_site(url: str, base_host: str) -> bool:
    host = _host(url)
    return bool(host and base_host and (host == base_host or host.endswith("." + base_host) or base_host.endswith("." + host)))


def _founder_names(candidate: dict) -> list[str]:
    names: list[str] = []
    for founder in candidate.get("founders", []) or []:
        if isinstance(founder, dict):
            name = founder.get("name", "")
        else:
            name = getattr(founder, "name", "")
        if name and name not in names:
            names.append(name)
    return names


def _startup_domain(candidate: dict) -> str:
    official_domain = str(candidate.get("official_domain", "") or "").strip()
    if official_domain:
        official_host = _host(
            official_domain if "://" in official_domain else f"https://{official_domain}"
        )
        if official_host:
            return official_host
    return _host(canonicalize_website(candidate.get("website", "")))


def classify_email(email: str, founder_names: list[str], startup_domain: str, source_url: str) -> EmailFinding:
    local, _, domain = email.lower().partition("@")
    compact_local = _ascii_token(local)
    same_domain = bool(startup_domain and (domain == startup_domain or domain.endswith("." + startup_domain)))

    best_founder = ""
    best_score = 0.0
    for name in founder_names:
        patterns = _founder_local_patterns(name)
        if local in patterns or compact_local in {_ascii_token(p) for p in patterns}:
            score = 0.98 if same_domain else 0.94
        else:
            parts = _name_parts(name)
            first = parts[0] if parts else ""
            last = parts[-1] if parts else ""
            both = bool(first and last and first in compact_local and last in compact_local)
            one_strong = bool((first and len(first) >= 4 and first in compact_local) or (last and len(last) >= 4 and last in compact_local))
            score = (0.96 if same_domain else 0.92) if both else ((0.84 if same_domain else 0.78) if one_strong else 0.0)
        if score > best_score:
            best_score = score
            best_founder = name

    if best_score:
        return EmailFinding(email, "founder_named", best_score, source_url, best_founder)

    if compact_local in {_ascii_token(v) for v in FOUNDER_ROLE_LOCALS}:
        return EmailFinding(email, "founder_role", 0.76 if same_domain else 0.64, source_url, "Founder team")

    if compact_local in {_ascii_token(v) for v in GENERIC_ROLE_LOCALS}:
        return EmailFinding(email, "company_generic", 0.58 if same_domain else 0.42, source_url)

    if domain in FREE_MAIL_DOMAINS:
        return EmailFinding(email, "public_personal_unmatched", 0.5, source_url)

    return EmailFinding(email, "company_named_or_other", 0.7 if same_domain else 0.48, source_url)


def _relevant_link_urls(markdown: str, page_url: str, base_host: str) -> list[str]:
    scored: dict[str, int] = {}
    for anchor, href in MARKDOWN_LINK_RE.findall(markdown or ""):
        absolute = canonicalize_website(urljoin(page_url, href))
        if not absolute or not _same_site(absolute, base_host):
            continue
        path_text = (urlparse(absolute).path + " " + anchor).lower()
        score = max((weight for term, weight in RELEVANT_PATH_TERMS.items() if term in path_text), default=0)
        if score:
            clean, _ = urldefrag(absolute)
            scored[clean.rstrip("/")] = max(score, scored.get(clean.rstrip("/"), 0))
    return [url for url, _ in sorted(scored.items(), key=lambda item: (-item[1], item[0]))]


def founder_names(candidate: dict) -> list[str]:
    return _founder_names(candidate)


def startup_domain(candidate: dict) -> str:
    return _startup_domain(candidate)


def finding_from_dict(row: dict) -> EmailFinding:
    return EmailFinding(
        email=str(row.get("email", "")),
        email_type=str(row.get("email_type", "company_named_or_other")),
        confidence=float(row.get("confidence", 0.0) or 0.0),
        source_url=str(row.get("source_url", "")),
        founder_match=str(row.get("founder_match", "")),
        origin=str(row.get("origin", "website")),
        provider=str(row.get("provider", "public_web")),
        verification_status=str(row.get("verification_status", "unverified")),
        verification_score=float(row.get("verification_score", 0.0) or 0.0),
        verification_provider=str(row.get("verification_provider", "")),
    )


def evidence_key(row: dict) -> tuple:
    return (
        str(row.get("email", "")).strip().lower(),
        str(row.get("provider", "")).strip().lower(),
        str(row.get("origin", "")).strip().lower(),
        str(row.get("source_url", "")).strip(),
        str(row.get("founder_match", "")).strip().lower(),
        str(row.get("verification_status", "")).strip().lower(),
        str(row.get("verification_score", "")).strip(),
        str(row.get("verification_provider", "")).strip().lower(),
    )


def apply_findings_to_candidate(
    candidate: dict,
    findings: dict[str, EmailFinding],
    people: list[str] | None = None,
    companies: list[str] | None = None,
) -> None:
    WebsiteContactEnricher._apply_findings(candidate, findings, people or [], companies or [])


class WebsiteContactEnricher:
    """Find and classify public founder/company contacts on a startup website.

    The enricher never invents an address. It only stores addresses present in public
    crawled text, plus the source URL and a deterministic match score.
    """

    def __init__(
        self,
        crawler,
        max_pages: int = 8,
        progress: Callable[[str], None] | None = None,
        enrichment_scope: str = "all",
    ):
        self.crawler = crawler
        self.max_pages = max(1, max_pages)
        self.progress = progress or (lambda _: None)
        if enrichment_scope not in {"all", "company", "founder"}:
            raise ValueError("enrichment_scope must be 'all', 'company', or 'founder'")
        self.enrichment_scope = enrichment_scope

    def _crawl_target(self, candidate: dict, url: str, max_pages: int = 1):
        pseudo_source = {
            "ID": candidate.get("source_id", "ENRICH"),
            "Organization / University": candidate.get("startup_name", ""),
            "Seed URL": url,
        }
        return self.crawler.crawl_source(pseudo_source, max_pages=max_pages)

    def enrich(self, candidate: dict) -> dict:
        website = canonicalize_website(candidate.get("website", ""))
        candidate["website"] = website
        candidate.setdefault("founder_email", "")
        candidate.setdefault("founder_email_confidence", 0.0)
        candidate.setdefault("founder_email_type", "")
        candidate.setdefault("company_email", "")
        candidate.setdefault("company_email_confidence", 0.0)
        candidate.setdefault("contact_email_type", "")
        candidate.setdefault("email_source_url", "")
        candidate.setdefault("founder_linkedin_url", "")
        candidate.setdefault("linkedin_url", "")
        candidate.setdefault("contact_evidence", [])
        candidate.setdefault("enrichment_pages_crawled", 0)
        candidate.setdefault("enrichment_urls_attempted", [])
        candidate.setdefault("contact_enrichment_errors", [])

        founder_names = _founder_names(candidate)
        base_host = _startup_domain(candidate)
        if base_host and not candidate.get("official_domain"):
            candidate["official_domain"] = base_host
            candidate["domain_confidence"] = float(candidate.get("website_discovery_confidence", 0.0) or 0.0)
            candidate["domain_relationship"] = candidate.get("domain_relationship") or "probable_official"
            candidate["domain_evidence"] = candidate.get("domain_evidence") or f"Candidate startup website: {website}"
        findings: dict[str, EmailFinding] = {}
        person_linkedins: list[str] = []
        company_linkedins: list[str] = []

        # Preserve and classify an email extracted from the original university page.
        existing_email = (candidate.get("contact_email") or "").strip().lower()
        if existing_email and "@" in existing_email:
            original_source = (candidate.get("source_urls") or [""])[0]
            findings[existing_email] = classify_email(existing_email, founder_names, base_host, original_source)

        if not website or not base_host:
            candidate["contact_enrichment_status"] = "no_startup_website"
            self._apply_findings(candidate, findings, person_linkedins, company_linkedins)
            return candidate

        queue: list[str] = []
        queued: set[str] = set()

        def add_url(url: str) -> None:
            clean = canonicalize_website(url)
            if not clean or not _same_site(clean, base_host):
                return
            clean, _ = urldefrag(clean)
            clean = clean.rstrip("/")
            if clean not in queued:
                queued.add(clean)
                queue.append(clean)

        add_url(website)
        for path in COMMON_PATHS[1:]:
            add_url(urljoin(website.rstrip("/") + "/", path.lstrip("/")))

        successful_pages = 0
        attempts = 0
        max_attempts = self.max_pages + 8
        while queue and successful_pages < self.max_pages and attempts < max_attempts:
            target = queue.pop(0)
            attempts += 1
            candidate["enrichment_urls_attempted"].append(target)
            self.progress(f"  Contact crawl: {candidate.get('startup_name', '')} → {target}")
            try:
                pages = self._crawl_target(candidate, target, max_pages=1)
            except Exception as exc:
                candidate["contact_enrichment_errors"].append(
                    {"url": target, "error": str(exc)}
                )
                continue
            for page in pages:
                if successful_pages >= self.max_pages:
                    break
                successful_pages += 1
                for email in extract_public_emails(page.markdown):
                    finding = classify_email(email, founder_names, base_host, page.page_url)
                    if (
                        self.enrichment_scope == "company"
                        and not finding.email_type.startswith("company_")
                    ):
                        continue
                    previous = findings.get(email)
                    if previous is None or finding.confidence > previous.confidence:
                        findings[email] = finding
                people, companies = extract_linkedin_urls(page.markdown)
                person_linkedins.extend(u for u in people if u not in person_linkedins)
                company_linkedins.extend(u for u in companies if u not in company_linkedins)
                for discovered in _relevant_link_urls(page.markdown, page.page_url, base_host):
                    add_url(discovered)

        candidate["enrichment_pages_crawled"] = successful_pages
        self._apply_findings(candidate, findings, person_linkedins, company_linkedins)
        if not successful_pages:
            candidate["contact_enrichment_status"] = "crawl_failed_or_no_pages"
        elif findings:
            candidate["contact_enrichment_status"] = "email_found"
        else:
            candidate["contact_enrichment_status"] = "no_public_email_found"
        return candidate

    @staticmethod
    def _apply_findings(candidate: dict, findings: dict[str, EmailFinding], people: list[str], companies: list[str]) -> None:
        verification_rank = {"valid": 4, "accept_all": 3, "unknown": 1, "unverified": 0}
        existing = [
            row for row in candidate.get("contact_evidence", []) or []
            if isinstance(row, dict) and row.get("email")
        ]
        combined: list[dict] = []
        seen: set[tuple] = set()
        for row in [*existing, *(f.as_dict() for f in findings.values())]:
            key = evidence_key(row)
            if key in seen:
                continue
            seen.add(key)
            combined.append(row)

        ordered = sorted(
            [finding_from_dict(row) for row in combined],
            key=lambda f: (
                -verification_rank.get(f.verification_status, 0),
                -f.confidence,
                -f.verification_score,
                f.email,
            ),
        )
        candidate["contact_evidence"] = combined
        candidate["all_public_emails"] = list(dict.fromkeys([
            *(candidate.get("all_public_emails", []) or []),
            *(f.email for f in ordered),
        ]))

        founder_candidates = [f for f in ordered if f.email_type in {"founder_named", "founder_role"}]
        named_founders = [f for f in founder_candidates if f.email_type == "founder_named"]
        company_candidates = [f for f in ordered if f.email_type.startswith("company_")]

        chosen_founder = (named_founders or founder_candidates)[0] if founder_candidates else None
        chosen_company = company_candidates[0] if company_candidates else None
        chosen_contact = chosen_founder or chosen_company or (ordered[0] if ordered else None)

        if chosen_founder:
            candidate["founder_email"] = chosen_founder.email
            candidate["founder_email_confidence"] = round(chosen_founder.confidence, 3)
            candidate["founder_email_type"] = chosen_founder.email_type
        if chosen_company:
            candidate["company_email"] = chosen_company.email
            candidate["company_email_confidence"] = round(chosen_company.confidence, 3)
        if chosen_contact:
            candidate["contact_email"] = chosen_contact.email
            candidate["contact_email_type"] = chosen_contact.email_type
            candidate["email_source_url"] = chosen_contact.source_url
            candidate["contact_source_url"] = chosen_contact.source_url

        # Prefer the founder's LinkedIn already extracted by the LLM, then public site links.
        existing_founder_linkedin = ""
        for founder in candidate.get("founders", []) or []:
            if isinstance(founder, dict) and founder.get("linkedin_url"):
                existing_founder_linkedin = founder["linkedin_url"]
                break
        candidate["founder_linkedin_url"] = (
            existing_founder_linkedin
            or candidate.get("founder_linkedin_url", "")
            or (people[0] if people else "")
        )
        candidate["linkedin_url"] = candidate.get("linkedin_url", "") or (companies[0] if companies else "")
