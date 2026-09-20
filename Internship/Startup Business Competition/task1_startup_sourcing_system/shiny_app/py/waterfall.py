"""Run the contact waterfall on an uploaded candidates CSV.

Invoked by the R Shiny app (Candidates tab). Reads a JSON config:
{project, candidates_csv, output_dir, log, mode, crawler_backend, search_backend,
 email_provider, people_provider, verify, paid_fallback_only}
and writes candidates_enriched.csv / founders.csv / contact_evidence.csv +
_shiny_summary.json into output_dir.
"""
import sys, json, os, csv, traceback

cfg = json.load(open(sys.argv[1], encoding="utf-8"))
sys.path.insert(0, cfg["project"])

from startup_sourcing.config import settings
from startup_sourcing.pipeline import enrich_candidates_with_contacts

log = open(cfg["log"], "w", encoding="utf-8", buffering=1)
def prog(m):
    log.write(str(m) + "\n"); log.flush()

def parse(cell):
    t = (cell or "").strip()
    if not t or t[0] not in "[{":
        return []
    try:
        return json.loads(t)
    except Exception:
        return []

prog("Loading uploaded candidates ...")
try:
    with open(cfg["candidates_csv"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["founders"] = parse(r.get("founders"))
        r["contact_evidence"] = parse(r.get("contact_evidence"))
    prog(f"{len(rows)} candidate(s) loaded. Running the free-first contact waterfall ...")
    res = enrich_candidates_with_contacts(
        candidates=rows, output_dir=cfg["output_dir"], settings=settings,
        mode=cfg.get("mode", "live"),
        crawler_backend=(cfg.get("crawler_backend") or None),
        search_backend=(cfg.get("search_backend") or None),
        email_provider=(cfg.get("email_provider") or None),
        people_provider=(cfg.get("people_provider") or None),
        verify_emails=bool(cfg.get("verify", False)),
        paid_fallback_only=bool(cfg.get("paid_fallback_only", True)),
        progress=prog,
    )
    json.dump(res["summary"], open(os.path.join(cfg["output_dir"], "_shiny_summary.json"), "w",
              encoding="utf-8"), indent=2, default=str)
    s = res["summary"]
    prog("Founder emails: %s | company emails: %s | evidence rows: %s"
         % (s.get("founder_emails_found"), s.get("company_emails_found"), s.get("contact_evidence_rows")))
    prog("WATERFALL_DONE")
except Exception as e:
    prog("ERROR: " + repr(e))
    log.write(traceback.format_exc())
    prog("WATERFALL_FAILED")
finally:
    log.close()
