from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


FounderStatus = Literal["current_student", "recent_graduate", "older_graduate", "unknown"]
EligibilityStatus = Literal["qualified", "not_qualified", "review"]


class FounderEvidence(BaseModel):
    name: str = ""
    university: str = ""
    status: FounderStatus = "unknown"
    graduation_year: int | None = None
    evidence: str = ""
    linkedin_url: str = ""


class CandidateExtract(BaseModel):
    startup_name: str
    description: str = ""
    industry: str = ""
    country: str = ""
    website: str = ""
    contact_email: str = ""
    founders: list[FounderEvidence] = Field(default_factory=list)
    eligibility_status: EligibilityStatus = "review"
    eligibility_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    eligibility_reason: str = ""
    innovation_score: int = Field(default=5, ge=1, le=10)
    scalability_score: int = Field(default=5, ge=1, le=10)
    international_potential_score: int = Field(default=5, ge=1, le=10)
    competition_fit_score: int = Field(default=5, ge=1, le=10)
    evidence_quotes: list[str] = Field(default_factory=list)


class CandidateBatch(BaseModel):
    candidates: list[CandidateExtract] = Field(default_factory=list)


class EmailDraft(BaseModel):
    subject: str
    body: str
