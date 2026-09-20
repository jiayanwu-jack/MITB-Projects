"""Source interface: each source discovers startups from one channel."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

from ..http import Fetcher
from ..models import Startup


def read_csv_rows(path: Path) -> list[dict]:
    """Read a CSV tolerant of how it was saved. Excel/Windows often writes cp1252 (a stray
    smart-quote or degree sign then crashes a strict utf-8 read), so try utf-8 (with BOM),
    then cp1252, then latin-1 (which decodes any byte). Newlines handled by the csv module.
    """
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with path.open(encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    with path.open(encoding="utf-8", errors="replace", newline="") as f:  # last resort
        return list(csv.DictReader(f))


@dataclass
class Context:
    """Everything a source needs to run."""
    fetcher: Fetcher
    limit: int | None = None                # max startups to return from this source
    data_dir: str = "data"                  # seed lists live here
    arg: str = ""                           # free-form CLI arg (e.g. a search query)


class Source(Protocol):
    name: str

    def discover(self, ctx: Context) -> Iterable[Startup]:
        ...
