"""
Tiny zero-dependency .env loader.

Reads KEY=VALUE lines from a .env file into os.environ WITHOUT overriding variables
already set in the real environment (so an explicit setx/export always wins). Blank
lines and lines starting with '#' are ignored; a leading 'export ' is allowed; and
surrounding single/double quotes around a value are stripped.

Call load_dotenv() *before* importing modules that read configuration at import time
(e.g. sourcer.llm reads SMU_BEDROCK_MODEL / AWS_REGION when it is first imported).
"""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env", *, override: bool = False) -> int:
    """Load KEY=VALUE pairs from `path`. Returns how many variables were set."""
    p = Path(path)
    if not p.exists():
        return 0
    loaded = 0
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and (override or key not in os.environ):
            os.environ[key] = val
            loaded += 1
    return loaded
