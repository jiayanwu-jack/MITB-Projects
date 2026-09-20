"""
Orchestrator: sources -> dedupe -> enrich (fetch + LLM/deterministic) -> email-find.

    startups = collect(sources)          # each source yields partial Startups
    for s in startups: enrich(s)         # founders + industry from the startup's own site
    -> email-find fills any missing founder emails

Targets run concurrently; the shared Fetcher rate-limits per host so parallelism only
helps across different sites.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable
from urllib.parse import urlparse

from . import email_find, founder_lookup, llm
from .extract import (clean_text, company_name, find_internal_links, guess_people,
                      is_role_inbox, looks_like_url_name, page_title, prettify_domain,
                      registrable_domain)
from .http import Fetcher
from .models import Founder, Startup
from .sources import Context, get_source

_ENRICH_MAX_PAGES = 5                      # homepage + a few about/team/contact/founder pages


def collect(source_specs: list[str], fetcher: Fetcher, *, limit: int | None,
            data_dir: str, limits: dict[str, int] | None = None,
            on_source: Callable[[str, int], None] | None = None
            ) -> list[Startup]:
    """Run each `name` or `name:arg` source; dedupe startups by registrable domain.

    `limits` maps a source name to its own max (e.g. {"university": 50, "accelerator": 10});
    a source without an entry uses the global `limit`.
    """
    limits = limits or {}
    by_domain: dict[str, Startup] = {}
    for spec in source_specs:
        name, _, arg = spec.partition(":")
        src = get_source(name)
        if src is None:
            print(f"  [!] unknown source '{name}' (have: accelerator, university, csv, "
                  "websearch, news, tracxn)")
            continue
        src_limit = limits.get(name, limit)          # per-source override, else global
        if src_limit == 0:                            # 0 = take none -> skip this source
            if on_source:
                on_source(name, 0)
            continue
        ctx = Context(fetcher=fetcher, limit=src_limit, data_dir=data_dir, arg=arg)
        found = list(src.discover(ctx))
        for s in found:
            key = s.domain or s.url or s.name
            by_domain.setdefault(key, s)
        if on_source:
            on_source(name, len(found))
    return list(by_domain.values())


def enrich(startup: Startup, fetcher: Fetcher, *, use_llm: bool = True,
           find_contacts: bool = False, llm_search: bool = False,
           llm_infer_email: bool = False) -> Startup:
    """Fill founders + industry from the startup's own site, then find emails."""
    if not startup.url:
        startup.status = "no_contact"
        return startup
    try:
        llm_ran = _crawl_and_extract(startup, fetcher, use_llm=use_llm)
    except Exception as e:                 # never let one site kill the run
        startup.status = "error"
        startup.notes = repr(e)
        return startup

    dom = startup.domain or registrable_domain(urlparse(startup.url).netloc)
    startup.domain = dom

    # Final safety net: if the name still looks like a URL (e.g. the homepage failed to
    # fetch), fall back to a prettified domain rather than showing a raw URL.
    if looks_like_url_name(startup.name, dom):
        startup.name = prettify_domain(dom) or startup.name

    # Opt-in lookup for founders with no email. Two mutually-exclusive strategies:
    #   --find-contacts : deterministic (LinkedIn-URL search + Hunter.io email).
    #   --llm-search    : LLM reads web-search result pages to find email + LinkedIn.
    # find_contacts wins if both are on; llm_search only runs when find_contacts is off.
    if find_contacts:
        try:
            founder_lookup.enrich_contact(startup, fetcher, use_api=True)
        except Exception:
            pass                           # never let enrichment kill the run
    elif llm_search:
        try:
            founder_lookup.llm_search_contact(startup, fetcher)
        except Exception:
            pass

    # Deterministic email is a FALLBACK, same as for names: only pattern-guess + MX-verify
    # when the LLM did NOT run or failed. When the LLM ran, its email answer is
    # authoritative - including an empty email meaning "no public founder email on the
    # page" - so we don't fabricate first.last@domain over it.
    if not llm_ran:
        for f in startup.founders:
            email_find.fill_email(f, dom)

    # LAST resort: if a founder STILL has no email after every method above, let the LLM
    # infer the most likely address from their name + the company domain (copying the
    # company's pattern from any example address it can see). Marked 'llm_inferred' and
    # UNVERIFIED - review before outreach. Opt-in via --llm-infer-email / config.
    if llm_infer_email:
        try:
            _llm_infer_emails(startup, dom)
        except Exception:
            pass                           # never let inference kill the run

    named = [f for f in startup.founders if f.name]
    with_email = [f for f in named if f.email]
    if with_email:
        startup.status = "enriched"
        startup.confidence = max(startup.confidence, 0.8 if any(
            f.email_status in ("scraped", "mx_ok", "api_verified") for f in with_email) else 0.55)
    elif named:
        startup.status = "enriched"
        startup.confidence = max(startup.confidence, 0.4)
        startup.notes = (startup.notes + " founder name found, email unverified").strip()
    else:
        startup.status = "no_contact"
    return startup


def _crawl_and_extract(startup: Startup, fetcher: Fetcher, *, use_llm: bool) -> bool:
    """Crawl the site and fill founders/industry. Returns True if the LLM ran (so the
    caller knows whether to use deterministic email-guessing as a fallback)."""
    start = startup.url
    queue = [start]
    seen: set[str] = set()
    pages_text: list[str] = []
    htmls: list[str] = []
    pages = 0

    while queue and pages < _ENRICH_MAX_PAGES:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        res = fetcher.get(url)
        if not res.ok or not res.html:
            if pages == 0:
                startup.status = "fetch_failed"
                startup.notes = res.reason
            continue
        pages += 1
        htmls.append(res.html)
        pages_text.append(clean_text(res.html))
        # Prefer the real company name from the homepage whenever the name we were
        # given is missing or is really a URL/host (e.g. a bare portfolio link).
        if pages == 1:
            dom = startup.domain or registrable_domain(urlparse(res.url).netloc)
            if looks_like_url_name(startup.name, dom):
                startup.name = company_name(res.html, dom) or startup.name
            # Follow the best about/team/contact/founder links from the homepage.
            for link in find_internal_links(res.html, res.url)[:_ENRICH_MAX_PAGES - 1]:
                if link not in seen:
                    queue.append(link)

    # The LLM is authoritative. Only fall back to the (noisier) deterministic name
    # extractor when the LLM is OFF or its call actually FAILED - never when it ran and
    # simply found no founders (that's a real "no founder on these pages" answer, and we
    # prefer an empty result to regex garbage).
    llm_ran = False
    if use_llm and llm.available() and pages_text:
        data = llm.extract(startup, "\n\n".join(pages_text))
        if data is not None:
            llm.apply(startup, data)
            llm_ran = True

    if not llm_ran:
        for html in htmls:
            for name, role, email in guess_people(html):
                _add_founder(startup, name, role, email)

    return llm_ran


def _llm_infer_emails(startup: Startup, dom: str) -> None:
    """Last resort: for each founder still WITHOUT an email, ask the LLM to guess the most
    likely address from their name + the company domain. Fills gaps only; never overwrites
    a real email. The guess is marked email_status/source='llm_inferred' (UNVERIFIED)."""
    if not dom or not llm.available():
        return
    # Feed the model any addresses we already have on this domain, so it copies the real
    # company pattern (e.g. first.last@) rather than guessing blindly.
    known = [f"{f.name}: {f.email}" for f in startup.founders if f.email and f.name]
    context = "Known addresses at this company:\n" + "\n".join(known) if known else ""
    if startup.description:
        context += ("\n\n" if context else "") + f"About: {startup.description}"

    for f in startup.founders:
        if f.email or not f.name:
            continue                       # only fill founders with no email
        data = llm.infer_email(f.name, dom, startup.name, context)
        if not data:
            continue
        email = str(data.get("email") or "").strip().lower()
        if "@" not in email:
            continue
        local, _, host = email.partition("@")
        # Accept only a syntactically valid address on the company's own domain.
        if not local or not (host == dom or host.endswith("." + dom)):
            continue
        f.email, f.email_status, f.source = email, "llm_inferred", "llm_inferred"


def _add_founder(startup: Startup, name: str, role: str, email: str) -> None:
    key = name.lower()
    for f in startup.founders:
        if f.name.lower() == key:
            f.role = f.role or role
            if email and not f.email:
                f.email, f.email_status, f.source = email, "scraped", "page"
            return
    startup.founders.append(Founder(
        name=name, role=role, email=email,
        email_status="scraped" if email else "", source="page" if email else ""))


def run(source_specs: list[str], fetcher: Fetcher, *, limit: int | None = None,
        limits: dict[str, int] | None = None,
        workers: int = 4, use_llm: bool = True, find_contacts: bool = False,
        llm_search: bool = False, llm_infer_email: bool = False, data_dir: str = "data",
        on_source: Callable[[str, int], None] | None = None,
        on_done: Callable[[Startup], None] | None = None) -> list[Startup]:
    startups = collect(source_specs, fetcher, limit=limit, limits=limits,
                       data_dir=data_dir, on_source=on_source)
    results: list[Startup] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(enrich, s, fetcher, use_llm=use_llm,
                            find_contacts=find_contacts, llm_search=llm_search,
                            llm_infer_email=llm_infer_email): s
                for s in startups}
        for fut in as_completed(futs):
            s = fut.result()
            results.append(s)
            if on_done:
                on_done(s)
    results.sort(key=lambda s: (s.status != "enriched", -s.confidence, s.name.lower()))
    return results
