"""Dump competition/SMTP settings as JSON so the R app can render templates + show send status."""
import sys, json
sys.path.insert(0, sys.argv[1])
from startup_sourcing.config import settings

print(json.dumps({
    "competition_name": settings.competition_name,
    "competition_year": settings.competition_year,
    "organizer_name": settings.organizer_name,
    "organizer_signature": settings.organizer_signature,
    "competition_url": settings.competition_url,
    "allow_live_send": bool(settings.allow_live_send),
    "smtp_user": settings.smtp_user or "",
    "smtp_host": settings.smtp_host,
}, default=str))
