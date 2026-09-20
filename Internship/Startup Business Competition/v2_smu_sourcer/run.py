#!/usr/bin/env python3
"""
smu_sourcer CLI - source startup founders (URL, name, email, industry) from the web.

Examples
--------
  # Offline end-to-end proof over real HTTP (localhost) - run this first:
  python run.py --self-test

  # Default: accelerator portfolios + university competition winners (real network):
  python run.py --workers 4

  # Pick sources explicitly; pass a per-source arg after ':' :
  python run.py --source accelerator --source university
  python run.py --source "websearch:NUS startup competition winners 2025"
  python run.py --source "tracxn:climate student startup"      # needs TRACXN_API_KEY
  python run.py --source csv:my_startups.csv                    # enrich a hand-made list

  # Use Claude on AWS Bedrock for founder/industry extraction:
  pip install "anthropic[bedrock]" && export AWS_REGION=us-east-1
  python run.py --llm

Output -> output/startups.csv, output/startups.jsonl, output/report.json

Small-scale / educational use. Review every guessed/unverified email before outreach.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import config                      # tunable parameters live here (edit config.py, not run.py)
from sourcer.env import load_dotenv

HERE = Path(__file__).parent
# Load .env before importing modules that read config at import time (e.g. llm).
load_dotenv(HERE / ".env")

from sourcer import llm  # noqa: E402
from sourcer.export import write_csv, write_jsonl, write_report  # noqa: E402
from sourcer.http import Fetcher  # noqa: E402
from sourcer.pipeline import run  # noqa: E402
from sourcer.sources import DEFAULT_SOURCES  # noqa: E402


def _print_line(s) -> None:
    if s.status == "enriched":
        f = s.founders[0] if s.founders else None
        who = f"{f.name} <{f.email or 'no email'}> [{f.email_status or '-'}]" if f else "-"
        ind = f" {s.industry}" if s.industry else ""
        print(f"  [ok  {s.confidence:.2f}] {s.name[:28]:<28}{ind:<16} {who}")
    else:
        print(f"  [{s.status:<12}] {s.name[:28]:<28} ({s.notes[:40] or s.url[:40]})")


def self_test() -> int:
    import functools
    import http.server
    import socketserver
    import tempfile
    import threading

    pages = {
        # An accelerator portfolio page linking out to a startup's own site.
        "accel/index.html": """<title>Test Accelerator - Portfolio</title><main>
            <h1>Our Startup Portfolio</h1>
            <a href="https://startup.example/">Acme Robotics</a>
            <a href="https://www.linkedin.com/company/acme">LinkedIn</a>
            </main>""",
    }
    tmp = Path(tempfile.mkdtemp(prefix="smu_sourcer_selftest_"))
    for rel, html in pages.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(html, encoding="utf-8")

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp))
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    # A second server for the "startup's own site" on a different host:port so it
    # counts as an external link and gets a distinct registrable domain.
    startup_dir = Path(tempfile.mkdtemp(prefix="smu_sourcer_startup_"))
    (startup_dir / "index.html").write_text(
        """<title>Acme Robotics</title><main><h1>Acme Robotics</h1>
        <p>We build warehouse robots. <a href="/team.html">Our team</a></p></main>""",
        encoding="utf-8")
    (startup_dir / "team.html").write_text(
        """<title>Acme Robotics - Team</title><main><h1>Team</h1>
        <p>Jane Tan, Co-Founder &amp; CEO -
           <a href="mailto:jane.tan@acme-robotics.example">jane.tan@acme-robotics.example</a></p>
        </main>""", encoding="utf-8")
    h2 = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(startup_dir))
    httpd2 = socketserver.ThreadingTCPServer(("127.0.0.1", 0), h2)
    httpd2.daemon_threads = True
    port2 = httpd2.server_address[1]
    threading.Thread(target=httpd2.serve_forever, daemon=True).start()

    # Rewrite the portfolio link to point at the startup server. Use `localhost` so
    # its registrable domain differs from the accelerator server's `127.0.0.1`
    # (otherwise the link counts as same-site and is skipped).
    idx = tmp / "accel" / "index.html"
    idx.write_text(idx.read_text("utf-8").replace(
        "https://startup.example/", f"http://localhost:{port2}/"), encoding="utf-8")

    # Seed CSV pointing the accelerator source at the local portfolio page.
    data_dir = tmp / "data"
    data_dir.mkdir()
    (data_dir / "accelerators.csv").write_text(
        f"name,url\nTest Accelerator,{base}/accel/\n", encoding="utf-8")

    fetcher = Fetcher(delay=0.0, cache_dir=tmp / ".cache")
    results = run(["accelerator"], fetcher, workers=2, use_llm=False,
                  data_dir=str(data_dir))
    httpd.shutdown()
    httpd2.shutdown()

    print("Self-test (real HTTP over localhost):")
    for s in results:
        _print_line(s)

    acme = results[0] if len(results) == 1 else None
    founder = acme.founders[0] if acme and acme.founders else None
    checks = [
        ("accelerator discovered exactly the external startup", len(results) == 1),
        ("startup URL captured", bool(acme and acme.url.startswith("http://localhost"))),
        ("founder name extracted", bool(founder and founder.name == "Jane Tan")),
        ("founder role extracted", bool(founder and "founder" in founder.role.lower())),
        ("founder email scraped from team page",
         bool(founder and founder.email == "jane.tan@acme-robotics.example")),
        ("status enriched", bool(acme and acme.status == "enriched")),
        ("sourced_from records the accelerator name",
         bool(acme and acme.source_org == "Test Accelerator")),
    ]
    print()
    ok = True
    for label, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
        ok = ok and passed
    print("\nSELF-TEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append", dest="sources", metavar="NAME[:ARG]",
                    help="source to run (repeatable). Default: accelerator, university")
    # Defaults come from config.py; a flag here overrides it for this run only.
    ap.add_argument("--limit", type=int, default=config.LIMIT_DEFAULT,
                    help="default max startups per source")
    ap.add_argument("--limit-university", type=int, default=config.LIMIT_UNIVERSITY,
                    help="max startups from the university source (overrides --limit)")
    ap.add_argument("--limit-accelerator", type=int, default=config.LIMIT_ACCELERATOR,
                    help="max startups from the accelerator source (overrides --limit)")
    ap.add_argument("--workers", type=int, default=config.WORKERS,
                    help="parallel enrichment workers")
    ap.add_argument("--delay", type=float, default=config.DELAY,
                    help="min seconds between hits to one host")
    ap.add_argument("--data", type=Path, default=config.DATA_DIR, help="seed-list directory")
    ap.add_argument("--out", type=Path, default=config.OUTPUT_DIR, help="output directory")
    ap.add_argument("--llm", action=argparse.BooleanOptionalAction, default=config.USE_LLM,
                    help="use an LLM for founder/industry extraction (--llm / --no-llm)")
    ap.add_argument("--find-contacts", action=argparse.BooleanOptionalAction,
                    default=config.FIND_CONTACTS,
                    help="deterministic LinkedIn+Hunter lookup for founders with no email")
    ap.add_argument("--llm-search", action=argparse.BooleanOptionalAction,
                    default=config.LLM_SEARCH,
                    help="LLM reads web-search results for missing emails (only if find-contacts off)")
    ap.add_argument("--llm-infer-email", action=argparse.BooleanOptionalAction,
                    default=config.LLM_INFER_EMAIL,
                    help="last resort: LLM guesses a founder's email from name+domain (UNVERIFIED)")
    ap.add_argument("--self-test", action="store_true",
                    help="prove the pipeline on localhost, then exit")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    sources = args.sources or config.SOURCES or DEFAULT_SOURCES
    use_llm = args.llm
    if use_llm and not llm.available():
        prov = llm.PROVIDER
        hint = "  [llm] --llm on but no LLM backend is usable "
        hint += f"(SMU_LLM_PROVIDER={prov}). "
        if prov in ("ollama", "auto"):
            hint += (f"Ollama: start the server (open the Ollama app or run `ollama serve`) "
                     f"so {llm.OLLAMA_HOST} is reachable; `ollama list` should show "
                     f"{llm.OLLAMA_MODEL}. ")
        if prov in ("gemini", "auto"):
            hint += "Gemini: set GEMINI_API_KEY in .env. "
        if prov in ("bedrock", "auto"):
            hint += "Bedrock: needs anthropic[bedrock] + IAM model access. "
        hint += "Falling back to deterministic extraction.\n"
        print(hint)
        use_llm = False
    elif use_llm:
        print(f"  [llm] backend: {llm.active_provider()}\n")

    if args.find_contacts:
        from sourcer import founder_lookup
        caps = founder_lookup.keys_present()
        if not any(caps.values()):
            print("  [find-contacts] set, but no keys found. LinkedIn search needs "
                  "SERPER_API_KEY or BRAVE_API_KEY; email lookup needs HUNTER_API_KEY. "
                  "Add them to .env (steps still run, just find nothing).")
        else:
            print(f"  [find-contacts] linkedin_search={caps['linkedin_search']} "
                  f"email_api={caps['email_api']}")

    if args.llm_search and args.find_contacts:
        print("  [llm-search] ignored - --find-contacts is on (they are alternatives).")
    elif args.llm_search:
        from sourcer import founder_lookup
        has_search = founder_lookup.keys_present()["linkedin_search"]
        if has_search and use_llm:
            print("  [llm-search] on - the LLM will read web-search results for missing emails.")
        else:
            print("  [llm-search] set but needs a web-search key (SERPER_API_KEY/BRAVE_API_KEY) "
                  "AND a usable LLM; something is missing, so it will find nothing.")

    if args.llm_infer_email:
        if use_llm:
            print("  [llm-infer-email] on - LLM will GUESS a name+domain email for any founder "
                  "still missing one (marked 'llm_inferred', UNVERIFIED - review before use).")
        else:
            print("  [llm-infer-email] set but needs a usable LLM; with the LLM off it does nothing.")

    # Per-source limit overrides (fall back to --limit when not set).
    limits: dict[str, int] = {}
    if args.limit_university is not None:
        limits["university"] = args.limit_university
    if args.limit_accelerator is not None:
        limits["accelerator"] = args.limit_accelerator

    fetcher = Fetcher(delay=args.delay, cache_dir=args.out / ".cache")
    limit_desc = ", ".join([f"{k}={v}" for k, v in limits.items()] + [f"default={args.limit}"])
    print(f"Sourcing from {sources} (limits: {limit_desc}; workers={args.workers}, "
          f"llm={'on' if use_llm else 'off'}, find_contacts={'on' if args.find_contacts else 'off'})\n")

    results = run(sources, fetcher, limit=args.limit, limits=limits, workers=args.workers,
                  use_llm=use_llm, find_contacts=args.find_contacts, llm_search=args.llm_search,
                  llm_infer_email=args.llm_infer_email,
                  data_dir=str(args.data),
                  on_source=lambda n, c: print(f"  {n}: {c} startup(s) discovered"),
                  on_done=_print_line)

    csv_path = write_csv(results, args.out / "startups.csv")
    write_jsonl(results, args.out / "startups.jsonl")
    report_path = write_report(results, args.out / "report.json")

    enriched = [s for s in results if s.status == "enriched"]
    with_email = sum(1 for s in results if any(f.email for f in s.founders))
    print(f"\nDone. {len(enriched)}/{len(results)} enriched, {with_email} with an email.")
    print(f"Startups -> {csv_path}")
    print(f"Report   -> {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
