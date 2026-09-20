from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def load_seed_sources(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Seed source file not found: {path}")
    if path.suffix.lower() != ".csv":
        raise ValueError("This prototype expects the supplied seed_sources.csv file.")

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        raw = row.get("Priority (1=highest)", "")
        try:
            row["priority"] = int(raw)
        except (TypeError, ValueError):
            row["priority"] = 99

        # Pre-vetted student competitions enforce eligibility through their own entry
        # rules, so candidates sourced from them can be auto-qualified. An explicit
        # "Eligibility Guaranteed" column wins; otherwise it is inferred from the Source
        # Type (any "...competition..." source counts as pre-vetted).
        explicit = str(row.get("Eligibility Guaranteed", "") or "").strip().lower()
        if explicit in {"yes", "true", "1", "y"}:
            row["eligibility_guaranteed"] = True
        elif explicit in {"no", "false", "0", "n"}:
            row["eligibility_guaranteed"] = False
        else:
            row["eligibility_guaranteed"] = "competition" in str(
                row.get("Source Type", "") or ""
            ).lower()
    return rows
