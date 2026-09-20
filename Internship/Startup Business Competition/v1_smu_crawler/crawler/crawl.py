"""
Orchestrator: target -> discover -> rank -> (optional LLM) -> Result.

crawl_all runs targets concurrently with a thread pool; politeness is preserved because
the shared Fetcher rate-limits PER HOST, so parallelism only helps across different sites.

Alongside the per-target Result, discover() may also surface StartupLeads (external
startup sites found on a university/org's portfolio-style pages). crawl_target and
crawl_all pass those through unchanged so callers can turn them into new startup targets.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from . import llm as llm_mod
from .discover import discover
from .extract import is_role_inbox
from .fetch import Fetcher
from .models import Result, StartupLead, Target
from .rank import REVIEW_THRESHOLD, choose, guess_person


def crawl_target(target: Target, fetcher: Fetcher, *, use_llm: bool = False,
                 max_pages: int = 6, max_depth: int = 2
                 ) -> tuple[Result, list[StartupLead]]:
    try:
        candidates, pages, reason, leads = discover(
            target, fetcher, max_pages=max_pages, max_depth=max_depth)
    except Exception as e:                       # never let one site kill the run
        return Result(target.id, target.kind, target.name, target.country,
                      status="error", error=repr(e)), []

    if not candidates:
        status = "fetch_failed" if reason and reason not in ("fetched", "cache") else "no_contact"
        return Result(target.id, target.kind, target.name, target.country,
                      status=status, pages_crawled=pages, error=reason), leads

    best, conf, note = choose(target, candidates)
    if best is None:
        return Result(target.id, target.kind, target.name, target.country,
                      status="no_contact", pages_crawled=pages, error=note), leads

    role = ""
    name = ""
    if use_llm:
        refined = llm_mod.refine(target, candidates)
        if refined:
            best = next((c for c in candidates
                         if c.email.lower() == str(refined["email"]).lower()), best)
            role = str(refined.get("role") or "")
            name = str(refined.get("name") or "")
            try:
                conf = max(conf, float(refined.get("confidence", conf)))
            except (TypeError, ValueError):
                pass

    role_inbox = is_role_inbox(best.email)
    if target.kind == "startup":
        needs_review = conf < REVIEW_THRESHOLD
    else:
        needs_review = conf < REVIEW_THRESHOLD or (target.is_org and not role_inbox)

    if not role or not name:
        guessed_name, guessed_role = guess_person(best, target)
        role = role or guessed_role
        name = name or guessed_name

    return Result(
        target_id=target.id, target_kind=target.kind, target_name=target.name,
        country=target.country, status="ok", contact_email=best.email, contact_name=name,
        contact_role=role, role_inbox=role_inbox, confidence=round(conf, 2),
        needs_review=needs_review, page_found_url=best.page_url, evidence=best.context,
        pages_crawled=pages, error=note,
    ), leads


def crawl_all(targets: list[Target], fetcher: Fetcher, *, workers: int = 4,
              use_llm: bool = False, max_pages: int = 6, max_depth: int = 2,
              on_done: Callable[[Result], None] | None = None
              ) -> tuple[list[Result], list[StartupLead]]:
    results: list[Result] = []
    leads: dict[str, StartupLead] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(crawl_target, t, fetcher, use_llm=use_llm,
                            max_pages=max_pages, max_depth=max_depth): t for t in targets}
        for fut in as_completed(futs):
            r, r_leads = fut.result()
            results.append(r)
            for lead in r_leads:
                leads.setdefault(lead.url, lead)
            if on_done:
                on_done(r)
    # stable, useful ordering for the output file
    results.sort(key=lambda r: (r.status != "ok", -r.confidence, r.target_name))
    return results, list(leads.values())
