#!/usr/bin/env python3
"""
Generate targets/universities_qs.csv from the QS World University Rankings.

Pulls the QS top-N (rank / name / country) and resolves each institution's official
website via Wikidata (P856), producing a CSV the crawler can consume directly.

Examples
--------
  python build_targets.py --top 200
  python build_targets.py --top 50 --out targets/universities_qs.csv
  python build_targets.py --top 200 --no-resolve      # names/ranks only, fill URLs later

Then crawl the generated list:
  python run_crawl.py --targets targets/universities_qs.csv --workers 4

NOTE: the QS ranking is QS's proprietary dataset - confirm permitted use before
redistributing. Rows with no resolved website are written with an empty portal_url
(verified=false) for a human to complete; the crawler skips empty URLs.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from crawler.qs_targets import build_target_csv

HERE = Path(__file__).parent


def _progress(i: int, total: int, name: str, portal: str) -> None:
    mark = portal if portal else "(no website found - fill in manually)"
    print(f"  [{i:>3}/{total}] {name[:40]:<40} {mark}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=200, help="how many top universities")
    ap.add_argument("--out", type=Path, default=HERE / "targets" / "universities_qs.csv")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--no-resolve", action="store_true",
                    help="skip Wikidata domain resolution (portal_url left blank)")
    args = ap.parse_args()

    print(f"Building QS top-{args.top} target list -> {args.out}")
    print("(reading QS ranking order + resolving official websites from Wikidata)\n")
    path, stats = build_target_csv(
        args.top, args.out, resolve_domains=not args.no_resolve,
        delay=args.delay, progress=_progress)

    print(f"\nWrote {stats['fetched']} universities -> {path}")
    if stats["domains_resolved"] is not None:
        print(f"  {stats['domains_resolved']} with a resolved website, "
              f"{stats['unresolved']} need a manual portal_url.")
    print("\nNext: python run_crawl.py --targets "
          f"{path.relative_to(HERE) if path.is_relative_to(HERE) else path} --workers 4")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
