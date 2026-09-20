"""
Data structures for the sourcing pipeline.

One record type flows end to end: a `Startup`, which carries a list of `Founder`s.
Sources produce partial `Startup`s (often just name + url); enrichment fills in
founders, industry, and emails. This replaces the older Target/Candidate/Result/
StartupLead spread from smu_crawler.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Founder:
    """One person at a startup - the recruitment contact we actually want."""
    name: str = ""
    role: str = ""                 # "Founder", "Co-Founder & CEO", ...
    email: str = ""
    email_status: str = ""         # scraped | mx_ok | guessed | api_verified | api_unverified
    source: str = ""               # how the email was obtained: page | pattern | hunter
    linkedin_url: str = ""         # public LinkedIn profile URL (a contact channel; not scraped for email)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Startup:
    """A startup project and its founder contact(s)."""
    name: str
    url: str = ""
    domain: str = ""               # registrable domain, e.g. acme.io
    industry: str = ""
    country: str = ""
    description: str = ""
    founders: list[Founder] = field(default_factory=list)
    source: str = ""               # which Source produced it (accelerator, university, ...)
    source_org: str = ""           # name of the university/org it was found via (e.g. "Y Combinator")
    source_detail: str = ""        # the page it was discovered on
    confidence: float = 0.0        # 0..1, how sure we are about the contact
    status: str = "new"            # new | enriched | no_contact | fetch_failed | error
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["founders"] = [f.to_dict() for f in self.founders]
        return d
