"""Send emails via the tested Python SmtpClient. Reads {project, out, messages:[{to,subject,body}]}."""
import sys, json

cfg = json.load(open(sys.argv[1], encoding="utf-8"))
sys.path.insert(0, cfg["project"])
from startup_sourcing.config import settings
from startup_sourcing.outreach import SmtpClient

results = []
try:
    with SmtpClient(settings) as client:
        for m in cfg["messages"]:
            try:
                client.send(m["to"], m["subject"], m["body"])
                results.append({"to": m["to"], "status": "sent"})
            except Exception as e:
                results.append({"to": m["to"], "status": "error", "error": str(e)[:200]})
except Exception as e:
    results = [{"status": "connect_error", "error": str(e)[:300]}]

json.dump(results, open(cfg["out"], "w", encoding="utf-8"), indent=2)
