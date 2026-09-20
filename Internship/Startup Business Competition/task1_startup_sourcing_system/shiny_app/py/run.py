"""Runner invoked by the R Shiny app: reads a JSON config, runs run_pipeline, writes a log."""
import sys, json, os, traceback

cfg = json.load(open(sys.argv[1], encoding="utf-8"))
sys.path.insert(0, cfg["project"])

from startup_sourcing.config import settings
from startup_sourcing.pipeline import run_pipeline

log = open(cfg["log"], "w", encoding="utf-8", buffering=1)
def prog(m):
    log.write(str(m) + "\n"); log.flush()

prog("Starting pipeline run ...")

# Stop signal: the R app writes this file to request a graceful stop (partial export).
stop_file = cfg.get("stop_file") or os.path.join(cfg["output_dir"], "_stop.flag")
try:
    if os.path.exists(stop_file):
        os.remove(stop_file)   # clear any stale request from a previous run
except Exception:
    pass
def should_stop():
    return os.path.exists(stop_file)

try:
    sel = cfg.get("selected_ids") or None
    res = run_pipeline(
        seed_csv=cfg["seed_csv"], output_dir=cfg["output_dir"], settings=settings,
        mode=cfg.get("mode", "mock"),
        crawler_backend=(cfg.get("crawler_backend") or None),
        llm_backend=(cfg.get("llm_backend") or None),
        ollama_model=(cfg.get("ollama_model") or None),
        google_model=(cfg.get("google_model") or None),
        selected_ids=sel,
        max_sources=int(cfg.get("max_sources", 5)),
        seed_start_index=int(cfg.get("seed_start_index", 1)),
        max_priority=int(cfg.get("max_priority", 2)),
        max_pages_per_source=int(cfg.get("max_pages", 2)),
        generate_drafts=False,
        enrich_contacts=bool(cfg.get("enrich", False)),
        search_backend=(cfg.get("search_backend") or None),
        email_provider=(cfg.get("email_provider") or None),
        people_provider=(cfg.get("people_provider") or None),
        verify_emails=bool(cfg.get("verify", False)),
        paid_fallback_only=bool(cfg.get("paid_fallback_only", True)),
        progress=prog,
        should_stop=should_stop,
    )
    json.dump(res["summary"], open(os.path.join(cfg["output_dir"], "_shiny_summary.json"), "w",
              encoding="utf-8"), indent=2, default=str)
    try:
        if os.path.exists(stop_file):
            os.remove(stop_file)
    except Exception:
        pass
    prog("PIPELINE_STOPPED" if res.get("stopped") else "PIPELINE_DONE")
except Exception as e:
    prog("ERROR: " + repr(e))
    log.write(traceback.format_exc())
    prog("PIPELINE_FAILED")
finally:
    log.close()
