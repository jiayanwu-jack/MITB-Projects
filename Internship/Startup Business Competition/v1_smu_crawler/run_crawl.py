#!/usr/bin/env python3
"""
smu_crawler CLI - actually crawl the web for org-level recruitment contacts.

Examples
--------
  # Dry, offline end-to-end proof over real HTTP (localhost) - run this first:
  python run_crawl.py --self-test

  # Crawl the seed university list (real network), 4 workers:
  python run_crawl.py --targets targets/universities.csv --workers 4

  # Crawl public organisations, with optional Claude refinement:
  python run_crawl.py --targets targets/organisations.csv --llm

Output -> output/contacts.csv, output/contacts.jsonl, output/report.json

Note: small-scale/educational use only - does not check robots.txt, rate-limits are
minimal, and `startup`-kind targets intentionally surface a named founder's personal
email rather than a generic inbox (see README.md).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from crawler.crawl import crawl_all
from crawler.export import write_csv, write_jsonl, write_report, write_startup_leads
from crawler.fetch import Fetcher
from crawler.models import Result, Target

HERE = Path(__file__).parent


def load_targets(path: Path, limit: int | None = None) -> list[Target]:
    targets: list[Target] = []
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        is_org = "org_type" in cols
        url_col = "portal_url" if "portal_url" in cols else "url"
        for r in reader:
            url = (r.get(url_col) or "").strip()
            if not url:
                continue
            targets.append(Target(
                id=r.get("id") or url,
                name=r.get("name") or url,
                country=r.get("country") or "",
                url=url,
                kind=(r.get("org_type") or "").strip() if is_org else "university",
                startup_focus=(r.get("startup_focus") or None),
            ))
            if limit and len(targets) >= limit:
                break
    return targets


def _print_line(r: Result) -> None:
    if r.status == "ok":
        flag = "REVIEW" if r.needs_review else "ready "
        box = "inbox" if r.role_inbox else "person"
        who = f" ({r.contact_name})" if r.contact_name else ""
        print(f"  [{flag}] {r.confidence:.2f} {box:<6} {r.target_name[:34]:<34} "
              f"{r.contact_email}{who}")
    else:
        print(f"  [{r.status:<12}] {r.target_name[:34]:<34} ({r.error[:40]})")


# --------------------------------------------------------------------------- #
# Self-test: a real HTTP crawl against a localhost site (no external network).
# --------------------------------------------------------------------------- #
def self_test() -> int:
    import functools
    import http.server
    import socketserver
    import tempfile
    import threading

    pages = {
        "robots.txt": "User-agent: *\nAllow: /\n",
        "index.html": """<h1>Test University</h1>
            <a href="/ie/">Innovation & Entrepreneurship</a>
            <a href="/contact.html">Contact us</a>""",
        "ie/index.html": """<title>Innovation & Entrepreneurship Office</title>
            <main><h1>Entrepreneurship & Innovation Office</h1>
            <p>We support student startups and incubation.</p>
            <p>Office enquiries: <a href="mailto:info@testu.edu">info@testu.edu</a></p>
            <p>Dr Jane Doe, Director - <a href="mailto:jane.doe@testu.edu">jane.doe@testu.edu</a></p>
            </main>""",
        "contact.html": """<title>Contact</title><p>General:
            <a href="mailto:enquiries@testu.edu">enquiries@testu.edu</a></p>""",
        "club/index.html": """<title>Founders Club</title>
            <a href="/club/contact.html">Contact the club</a>
            <p>The student entrepreneurship club.</p>""",
        "club/contact.html": """<title>Founders Club - Contact</title><main>
            <p>Email the committee: <a href="mailto:hello@club.org">hello@club.org</a></p>
            <p>President Sam Lee: <a href="mailto:sam.lee@club.org">sam.lee@club.org</a></p>
            </main>""",
        "accel/index.html": """<title>Test Accelerator</title>
            <main><h1>Test Accelerator</h1>
            <a href="/accel/portfolio.html">Success Stories</a></main>""",
        "accel/portfolio.html": """<title>Test Accelerator - Our Startup Portfolio</title>
            <main><h1>Our Startup Portfolio</h1>
            <a href="https://example.org/acme">Acme Robotics</a>
            <a href="https://linkedin.com/company/acme">LinkedIn</a>
            </main>""",
    }
    tmp = Path(tempfile.mkdtemp(prefix="smu_crawler_selftest_"))
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

    # Fresh cache dir so the test is real each run.
    fetcher = Fetcher(delay=0.0, cache_dir=tmp / ".cache")
    uni = Target(id="t1", name="Test University", country="Testland",
                 url=base + "/", kind="university")
    club = Target(id="t2", name="Founders Club", country="Testland",
                  url=base + "/club/", kind="student_club")
    accel = Target(id="t3", name="Test Accelerator", country="Testland",
                   url=base + "/accel/", kind="accelerator")

    from crawler.crawl import crawl_target
    r_uni, _ = crawl_target(uni, fetcher, max_pages=6, max_depth=2)
    r_club, _ = crawl_target(club, fetcher, max_pages=6, max_depth=2)
    r_accel, leads_accel = crawl_target(accel, fetcher, max_pages=6, max_depth=2)
    httpd.shutdown()

    print("Self-test (real HTTP over localhost):")
    _print_line(r_uni)
    _print_line(r_club)
    print(f"  [leads  ] Test Accelerator portfolio page -> {len(leads_accel)} startup lead(s)")

    checks = [
        ("university fetch ok", r_uni.status == "ok"),
        ("university picked office role inbox", r_uni.contact_email == "info@testu.edu"),
        ("university crawled >1 page", r_uni.pages_crawled >= 1),
        ("club fetch ok", r_club.status == "ok"),
        ("club picked role inbox over president", r_club.contact_email == "hello@club.org"),
        ("club marked role inbox", r_club.role_inbox is True),
        ("accelerator portfolio yielded exactly the external startup lead",
         len(leads_accel) == 1 and leads_accel[0].url == "https://example.org/acme"),
        ("accelerator portfolio filtered out the LinkedIn link",
         not any("linkedin.com" in l.url for l in leads_accel)),
    ]
    ok = True
    print()
    for label, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
        ok = ok and passed
    print("\nSELF-TEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--targets", type=Path, default=HERE / "targets" / "universities_qs.csv")
    ap.add_argument("--limit", type=int, default=30, help="crawl only the first N targets")
    ap.add_argument("--workers", type=int, default=4, help="parallel targets (rate-limited per host)")
    ap.add_argument("--max-pages", type=int, default=6, help="max pages per site")
    ap.add_argument("--max-depth", type=int, default=2, help="max link depth per site")
    ap.add_argument("--delay", type=float, default=1.0, help="min seconds between hits to one host")
    ap.add_argument("--out", type=Path, default=HERE / "output")
    ap.add_argument("--llm", action="store_true", help="refine with Claude (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--self-test", action="store_true", help="prove the HTTP path on localhost, then exit")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if not args.targets.exists():
        print(f"targets file not found: {args.targets}", file=sys.stderr)
        return 2

    targets = load_targets(args.targets, args.limit)
    if not targets:
        print("no targets loaded (check the CSV has a portal_url/url column).", file=sys.stderr)
        return 2

    fetcher = Fetcher(delay=args.delay, cache_dir=args.out / ".cache")
    print(f"Crawling {len(targets)} targets from {args.targets.name} "
          f"(workers={args.workers}, max_pages={args.max_pages}, delay={args.delay}s, "
          f"llm={'on' if args.llm else 'off'})\n")

    results, leads = crawl_all(targets, fetcher, workers=args.workers, use_llm=args.llm,
                              max_pages=args.max_pages, max_depth=args.max_depth,
                              on_done=_print_line)

    csv_path = write_csv(results, args.out / "contacts.csv")
    write_jsonl(results, args.out / "contacts.jsonl")
    report_path = write_report(results, args.out / "report.json")
    leads_path = write_startup_leads(leads, args.out / "startup_leads.csv")

    ok = [r for r in results if r.status == "ok"]
    print(f"\nDone. {len(ok)}/{len(results)} contacts found  "
          f"({sum(1 for r in ok if not r.needs_review)} ready, "
          f"{sum(1 for r in ok if r.needs_review)} need review).")
    print(f"Contacts -> {csv_path}")
    print(f"Report   -> {report_path}")
    print(f"Startup leads found on portfolio-style pages -> {leads_path} ({len(leads)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
