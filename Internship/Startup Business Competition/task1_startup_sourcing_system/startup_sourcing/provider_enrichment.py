from __future__ import annotations

import time
from typing import Callable
from urllib.parse import urlparse

from .contact_enrichment import (
    EmailFinding,
    apply_findings_to_candidate,
    classify_email,
    evidence_key,
    extract_public_emails,
    finding_from_dict,
    founder_names,
    startup_domain,
)
from .http_utils import json_request


class HunterClient:
    base_url = "https://api.hunter.io/v2"

    def __init__(
        self,
        api_key: str,
        *,
        request_delay_seconds: float = 0.25,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 2.0,
    ):
        if not api_key:
            raise ValueError("HUNTER_API_KEY is required when email_provider=hunter or verification is enabled")
        self.api_key = api_key
        self.calls = {"domain_search": 0, "email_finder": 0, "email_verifier": 0}
        self.request_delay_seconds = max(0.0, request_delay_seconds)
        self.retry_attempts = max(1, retry_attempts)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._last_request_at = 0.0

    def _get(self, path: str, params: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(self.retry_attempts):
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.request_delay_seconds:
                time.sleep(self.request_delay_seconds - elapsed)
            try:
                response = json_request(
                    self.base_url + path,
                    params=params,
                    headers={"X-API-KEY": self.api_key},
                    timeout=35,
                )
                self._last_request_at = time.monotonic()
                return response
            except RuntimeError as exc:
                self._last_request_at = time.monotonic()
                last_error = exc
                if "HTTP 403" not in str(exc) or attempt >= self.retry_attempts - 1:
                    raise
                time.sleep(self.retry_backoff_seconds * (attempt + 1))
        if last_error:
            raise last_error
        return {}

    def domain_search(self, domain: str, limit: int = 10) -> dict:
        self.calls["domain_search"] += 1
        return self._get("/domain-search", {"domain": domain, "limit": max(1, min(100, limit))})

    def email_finder(
        self,
        *,
        full_name: str = "",
        domain: str = "",
        company: str = "",
        linkedin_handle: str = "",
    ) -> dict:
        self.calls["email_finder"] += 1
        params = {
            "full_name": full_name or None,
            "domain": domain or None,
            "company": company or None,
            "linkedin_handle": linkedin_handle or None,
            "max_duration": 10,
        }
        return self._get(
            "/email-finder",
            {key: value for key, value in params.items() if value},
        )

    def verify(self, email: str) -> dict:
        self.calls["email_verifier"] += 1
        return self._get("/email-verifier", {"email": email})


class ApolloClient:
    match_endpoint = "https://api.apollo.io/api/v1/people/match"
    poll_endpoint = "https://api.apollo.io/api/v1/webhook_result/{request_id}"

    def __init__(self, api_key: str, poll_attempts: int = 2, poll_delay_seconds: float = 2.0):
        if not api_key:
            raise ValueError("APOLLO_API_KEY is required when people_provider=apollo")
        self.api_key = api_key
        self.poll_attempts = max(0, poll_attempts)
        self.poll_delay_seconds = max(0.0, poll_delay_seconds)
        self.calls = {"people_match": 0, "poll": 0}

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def people_match(
        self,
        *,
        name: str,
        organization_name: str = "",
        domain: str = "",
        linkedin_url: str = "",
        reveal_personal_emails: bool = False,
        run_waterfall_email: bool = False,
    ) -> dict:
        self.calls["people_match"] += 1
        params = {
            "name": name,
            "organization_name": organization_name or None,
            "domain": domain or None,
            "linkedin_url": linkedin_url or None,
            "reveal_personal_emails": str(bool(reveal_personal_emails)).lower(),
            "run_waterfall_email": str(bool(run_waterfall_email)).lower(),
        }
        return json_request(
            self.match_endpoint,
            method="POST",
            params=params,
            headers=self.headers,
            timeout=45,
        )

    def poll(self, request_id: int | str) -> dict:
        self.calls["poll"] += 1
        return json_request(
            self.poll_endpoint.format(request_id=request_id),
            headers=self.headers,
            timeout=30,
        )

    def poll_waterfall(self, request_id: int | str) -> dict:
        last: dict = {}
        for attempt in range(self.poll_attempts):
            if attempt and self.poll_delay_seconds:
                time.sleep(self.poll_delay_seconds)
            try:
                last = self.poll(request_id)
            except Exception:
                continue
            if last:
                return last
        return last


def _first_source_url(sources: list[dict] | None, fallback: str) -> str:
    for row in sources or []:
        uri = row.get("uri") or row.get("url")
        if uri:
            return str(uri)
    return fallback


def _provider_email(value: object) -> str:
    emails = extract_public_emails(str(value or ""))
    return emails[0] if emails else ""


def _linkedin_handle(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if "linkedin.com" not in value.lower():
        return value.strip("/")
    try:
        parsed = urlparse(value if "://" in value else "https://" + value)
    except ValueError:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0].lower() == "in":
        return parts[1]
    return ""


def _candidate_founders(candidate: dict) -> list[dict]:
    rows: list[dict] = []
    for founder in candidate.get("founders", []) or []:
        if isinstance(founder, dict):
            row = founder
        elif hasattr(founder, "model_dump"):
            row = founder.model_dump()
        else:
            row = {}
        name = str(row.get("name", "")).strip()
        linkedin_url = str(row.get("linkedin_url", "") or candidate.get("founder_linkedin_url", "")).strip()
        if name or linkedin_url:
            rows.append({
                "name": name,
                "linkedin_url": linkedin_url,
                "linkedin_handle": _linkedin_handle(linkedin_url),
            })
    if not rows:
        name = str(candidate.get("founder_name", "")).strip()
        linkedin_url = str(candidate.get("linkedin_url", "") or candidate.get("founder_linkedin_url", "")).strip()
        if name or linkedin_url:
            rows.append({
                "name": name,
                "linkedin_url": linkedin_url,
                "linkedin_handle": _linkedin_handle(linkedin_url),
            })
    return rows


def _extract_apollo_emails(payload: dict) -> list[tuple[str, str]]:
    person = payload.get("person") or payload.get("data") or {}
    rows: list[tuple[str, str]] = []
    email = person.get("email")
    if email:
        rows.append((str(email), "apollo_native"))
    for key in ("personal_emails", "emails"):
        values = person.get(key) or []
        if isinstance(values, str):
            values = [values]
        for value in values:
            if isinstance(value, dict):
                address = value.get("email") or value.get("value")
            else:
                address = value
            if address:
                rows.append((str(address), "apollo_personal"))
    # Waterfall poll payloads may nest data differently.
    for container_key in ("webhook_payload", "result", "payload"):
        container = payload.get(container_key) or {}
        if isinstance(container, dict):
            for item in _extract_apollo_emails({"person": container}):
                rows.append(item)
    dedup: dict[str, str] = {}
    for email, origin in rows:
        dedup[email.lower()] = origin
    return sorted(dedup.items())


class PaidProviderEnricher:
    """Optional paid-provider waterfall. Calls providers only after free methods by default."""

    def __init__(
        self,
        *,
        hunter: HunterClient | None = None,
        apollo: ApolloClient | None = None,
        verify_emails: bool = False,
        fallback_only: bool = True,
        apollo_reveal_personal_emails: bool = False,
        apollo_run_waterfall_email: bool = False,
        enrichment_scope: str = "all",
        progress: Callable[[str], None] | None = None,
    ):
        self.hunter = hunter
        self.apollo = apollo
        self.verify_emails = verify_emails
        self.fallback_only = fallback_only
        self.apollo_reveal_personal_emails = apollo_reveal_personal_emails
        self.apollo_run_waterfall_email = apollo_run_waterfall_email
        if enrichment_scope not in {"all", "company", "founder"}:
            raise ValueError("enrichment_scope must be 'all', 'company', or 'founder'")
        self.enrichment_scope = enrichment_scope
        self.progress = progress or (lambda _: None)

    def enrich(self, candidate: dict) -> dict:
        candidate.setdefault("contact_evidence", [])
        candidate.setdefault("contact_methods_attempted", [])
        candidate.setdefault("provider_enrichment_errors", [])
        candidate.setdefault("provider_calls", [])
        candidate.setdefault("apollo_waterfall_request_ids", [])

        findings: dict[str, EmailFinding] = {
            f"existing:{i}:{row.get('email', '')}": finding_from_dict(row)
            for i, row in enumerate(candidate.get("contact_evidence", []) or [])
            if isinstance(row, dict) and row.get("email")
        }

        def add_finding(finding: EmailFinding) -> None:
            if self.enrichment_scope == "company" and not finding.email_type.startswith("company_"):
                return
            if self.enrichment_scope == "founder" and not finding.email_type.startswith("founder_"):
                return
            findings["|".join(str(part) for part in evidence_key(finding.as_dict()))] = finding

        domain = candidate.get("official_domain") or startup_domain(candidate)
        names = founder_names(candidate)
        company = str(candidate.get("startup_name", "")).strip()
        founder_rows = _candidate_founders(candidate)

        company_missing = not candidate.get("company_email")
        founder_missing = not candidate.get("founder_email")

        if (
            self.enrichment_scope in {"all", "company"}
            and self.hunter and domain
            and (not self.fallback_only or company_missing)
        ):
            candidate["contact_methods_attempted"].append("hunter")
            try:
                candidate["provider_calls"].append("hunter.domain_search")
                payload = self.hunter.domain_search(domain)
                for row in ((payload.get("data") or {}).get("emails") or []):
                    email = _provider_email(row.get("value", ""))
                    if not email:
                        continue
                    source = _first_source_url(row.get("sources"), f"hunter://domain-search/{domain}")
                    base = classify_email(email, names, domain, source)
                    hunter_conf = float(row.get("confidence", 0) or 0) / 100.0
                    verification = (row.get("verification") or {}).get("status", "unverified")
                    finding = EmailFinding(
                        email=email,
                        email_type=base.email_type,
                        confidence=max(base.confidence, hunter_conf),
                        source_url=source,
                        founder_match=base.founder_match,
                        origin="provider_domain_search",
                        provider="hunter",
                        verification_status=str(verification or "unverified"),
                        verification_score=hunter_conf,
                        verification_provider="hunter" if verification else "",
                    )
                    add_finding(finding)
            except Exception as exc:
                candidate["provider_enrichment_errors"].append({"provider": "hunter.domain_search", "error": str(exc)})

        if (
            self.enrichment_scope in {"all", "founder"}
            and self.hunter and founder_rows
            and (not self.fallback_only or founder_missing)
        ):
            candidate["contact_methods_attempted"].append("hunter")

            # Direct founder lookups can use domain, company name, and/or LinkedIn handle.
            matched = {f.founder_match.lower() for f in findings.values() if f.founder_match}
            for founder in founder_rows:
                name = founder["name"]
                linkedin_handle = founder["linkedin_handle"]
                if name.lower() in matched:
                    continue
                if not (name or linkedin_handle):
                    continue
                if not (domain or company or linkedin_handle):
                    candidate["provider_enrichment_errors"].append({
                        "provider": "hunter.email_finder",
                        "founder": name,
                        "error": "Missing domain, company, and LinkedIn handle for Hunter Email Finder.",
                    })
                    continue
                try:
                    candidate["provider_calls"].append("hunter.email_finder")
                    payload = self.hunter.email_finder(
                        full_name=name,
                        domain=domain,
                        company=company,
                        linkedin_handle=linkedin_handle,
                    )
                    data = payload.get("data") or {}
                    email = _provider_email(data.get("email", ""))
                    if not email:
                        continue
                    score = float(data.get("score", 0) or 0) / 100.0
                    source_key = domain or company or linkedin_handle
                    source = _first_source_url(data.get("sources"), f"hunter://email-finder/{source_key}/{name or linkedin_handle}")
                    verification = (data.get("verification") or {}).get("status", "unverified")
                    add_finding(EmailFinding(
                        email=email,
                        email_type="founder_named",
                        confidence=max(0.82, score),
                        source_url=source,
                        founder_match=name,
                        origin="provider_email_finder",
                        provider="hunter",
                        verification_status=str(verification or "unverified"),
                        verification_score=score,
                        verification_provider="hunter" if verification else "",
                    ))
                except Exception as exc:
                    candidate["provider_enrichment_errors"].append({"provider": "hunter.email_finder", "founder": name, "error": str(exc)})

        # Apollo person enrichment comes after Hunter in the waterfall.
        current_founder_matches = {f.founder_match.lower() for f in findings.values() if f.founder_match}
        if (
            self.enrichment_scope in {"all", "founder"}
            and self.apollo and names
            and (not self.fallback_only or len(current_founder_matches) < len(names))
        ):
            candidate["contact_methods_attempted"].append("apollo")
            founder_linkedin = candidate.get("founder_linkedin_url", "")
            for name in names:
                if self.fallback_only and name.lower() in current_founder_matches:
                    continue
                try:
                    candidate["provider_calls"].append("apollo.people_match")
                    payload = self.apollo.people_match(
                        name=name,
                        organization_name=candidate.get("startup_name", ""),
                        domain=domain,
                        linkedin_url=founder_linkedin,
                        reveal_personal_emails=self.apollo_reveal_personal_emails,
                        run_waterfall_email=self.apollo_run_waterfall_email,
                    )
                    request_id = payload.get("request_id") or (payload.get("data") or {}).get("request_id")
                    if request_id:
                        candidate["apollo_waterfall_request_ids"].append(str(request_id))
                    email_rows = _extract_apollo_emails(payload)
                    if self.apollo_run_waterfall_email and request_id and not email_rows:
                        polled = self.apollo.poll_waterfall(request_id)
                        email_rows.extend(_extract_apollo_emails(polled))
                    for email, origin in email_rows:
                        email = _provider_email(email)
                        if not email:
                            continue
                        add_finding(EmailFinding(
                            email=email,
                            email_type="founder_named",
                            confidence=0.9 if origin == "apollo_native" else 0.82,
                            source_url=founder_linkedin or f"apollo://people-match/{name}",
                            founder_match=name,
                            origin=origin,
                            provider="apollo",
                        ))
                    person = payload.get("person") or payload.get("data") or {}
                    if not candidate.get("founder_linkedin_url") and isinstance(person, dict):
                        candidate["founder_linkedin_url"] = person.get("linkedin_url") or candidate.get("founder_linkedin_url", "")
                except Exception as exc:
                    candidate["provider_enrichment_errors"].append({"provider": "apollo.people_match", "founder": name, "error": str(exc)})

        # Optional verification of the strongest addresses. Hunter is currently the verifier implementation.
        verification_findings = {
            key: finding for key, finding in findings.items()
            if (
                self.enrichment_scope == "all"
                or (self.enrichment_scope == "founder" and finding.email_type.startswith("founder_"))
                or (self.enrichment_scope == "company" and finding.email_type.startswith("company_"))
            )
        }
        if self.verify_emails and self.hunter and verification_findings:
            candidate["contact_methods_attempted"].append("email_verification")
            for email, old in list(verification_findings.items()):
                if old.verification_status in {"valid", "accept_all"}:
                    continue
                try:
                    candidate["provider_calls"].append("hunter.email_verifier")
                    payload = self.hunter.verify(email)
                    data = payload.get("data") or {}
                    status = str(data.get("status", "unknown"))
                    score = float(data.get("score", 0) or 0) / 100.0
                    add_finding(EmailFinding(
                        email=old.email,
                        email_type=old.email_type,
                        confidence=old.confidence,
                        source_url=old.source_url,
                        founder_match=old.founder_match,
                        origin=old.origin,
                        provider=old.provider,
                        verification_status=status,
                        verification_score=score,
                        verification_provider="hunter",
                    ))
                except Exception as exc:
                    candidate["provider_enrichment_errors"].append({"provider": "hunter.email_verifier", "email": email, "error": str(exc)})

        candidate["contact_methods_attempted"] = list(dict.fromkeys(candidate["contact_methods_attempted"]))
        apply_findings_to_candidate(candidate, findings, [], [])
        candidate["provider_enrichment_status"] = (
            "founder_email_found" if candidate.get("founder_email") else
            "company_email_found" if candidate.get("company_email") else
            "providers_exhausted_no_email"
        )
        return candidate


def provider_call_totals(enricher: PaidProviderEnricher | None) -> dict[str, int]:
    if enricher is None:
        return {}
    totals: dict[str, int] = {}
    if enricher.hunter:
        totals.update({f"hunter_{k}_calls": v for k, v in enricher.hunter.calls.items()})
    if enricher.apollo:
        totals.update({f"apollo_{k}_calls": v for k, v in enricher.apollo.calls.items()})
    return totals
