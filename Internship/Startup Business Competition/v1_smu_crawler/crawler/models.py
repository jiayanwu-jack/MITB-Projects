"""Data structures passed between the crawler stages."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Target:
    """One institution to crawl. `kind` is 'university' or an org type."""
    id: str
    name: str
    country: str
    url: str                       # site root or a known office/contact page
    kind: str = "university"       # university | student_club | accelerator | competition | startup
    startup_focus: Optional[str] = None

    @property
    def is_org(self) -> bool:
        return self.kind != "university"


@dataclass
class Candidate:
    """An email found on a page, with the context needed to judge it."""
    email: str
    context: str = ""
    source: str = ""               # "mailto" | "text"
    page_url: str = ""
    page_title: str = ""
    office_page: bool = False      # found on an entrepreneurship/innovation-office page
    directory_page: bool = False   # found on a many-email listing (people directory)


@dataclass
class StartupLead:
    """An external startup website found on a university/org portfolio-style page."""
    name: str
    url: str
    source_target_id: str = ""
    source_target_name: str = ""
    source_page_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Result:
    """The crawl outcome for one target - one org-level contact (or a flagged miss)."""
    target_id: str
    target_kind: str
    target_name: str
    country: str
    status: str                    # ok | no_contact | fetch_failed | error
    contact_email: str = ""
    contact_name: str = ""         # the person's name, when the contact is an individual
    contact_role: str = ""
    role_inbox: bool = False
    confidence: float = 0.0
    needs_review: bool = True
    page_found_url: str = ""
    evidence: str = ""
    pages_crawled: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
