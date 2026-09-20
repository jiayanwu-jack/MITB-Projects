from __future__ import annotations

from .config import Settings


def enforce_eligibility(
    candidate: dict, settings: Settings, source_guaranteed: bool = False
) -> dict:
    """Apply a deterministic eligibility gate after LLM extraction.

    When ``source_guaranteed`` is True the candidate came from a pre-vetted student
    competition whose entry rules already enforce eligibility, so we trust the source: a
    "review" verdict (no public graduation evidence) is upgraded to "qualified", and a
    deterministic "not_qualified" (explicit graduation year before the cutoff, most often
    a mis-attributed mentor/co-founder) is softened to "review" for a human to check
    rather than silently dropped.
    """
    founders = candidate.get("founders") or []
    qualifying = []
    disqualifying = []
    unknown = []

    for founder in founders:
        status = founder.get("status", "unknown")
        year = founder.get("graduation_year")
        if status == "current_student":
            qualifying.append(founder)
        elif year is not None and int(year) >= settings.graduation_cutoff_year:
            qualifying.append(founder)
        elif year is not None and int(year) < settings.graduation_cutoff_year:
            disqualifying.append(founder)
        else:
            unknown.append(founder)

    if qualifying:
        candidate["eligibility_status"] = "qualified"
        candidate["eligibility_reason"] = (
            f"At least one founder is a current student or graduated in "
            f"{settings.graduation_cutoff_year} or later."
        )
    elif founders and not unknown and disqualifying:
        candidate["eligibility_status"] = "not_qualified"
        candidate["eligibility_reason"] = (
            f"Available graduation evidence predates the {settings.graduation_cutoff_year} cutoff."
        )
    else:
        candidate["eligibility_status"] = "review"
        candidate["eligibility_reason"] = (
            "Insufficient public evidence to verify student or recent-graduate status."
        )

    if source_guaranteed:
        if candidate["eligibility_status"] == "not_qualified":
            candidate["eligibility_status"] = "review"
            candidate["eligibility_reason"] = (
                "Pre-vetted student competition source, but extracted graduation evidence "
                "predates the cutoff - review the founder identity before discarding."
            )
        else:
            candidate["eligibility_status"] = "qualified"
            candidate["eligibility_reason"] = (
                "Sourced from a pre-vetted student competition; eligibility is enforced by "
                "the competition's own entry rules."
            )
            if float(candidate.get("eligibility_confidence") or 0.0) < 0.9:
                candidate["eligibility_confidence"] = 0.9

    return candidate


def compute_priority_score(candidate: dict, source_priority: int) -> float:
    eligibility_conf = float(candidate.get("eligibility_confidence") or 0.0)
    component_scores = [
        float(candidate.get("innovation_score") or 0),
        float(candidate.get("scalability_score") or 0),
        float(candidate.get("international_potential_score") or 0),
        float(candidate.get("competition_fit_score") or 0),
    ]
    quality = sum(component_scores) / 4.0
    source_bonus = max(0, 4 - int(source_priority)) * 3
    gate_bonus = {"qualified": 20, "review": 5, "not_qualified": 0}.get(
        candidate.get("eligibility_status", "review"), 5
    )
    return round(min(100.0, eligibility_conf * 50 + quality * 3 + source_bonus + gate_bonus), 1)
