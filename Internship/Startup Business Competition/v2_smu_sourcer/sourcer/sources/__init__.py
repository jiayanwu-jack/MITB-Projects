"""Source registry: name -> Source instance."""
from __future__ import annotations

from .accelerator import AcceleratorSource
from .base import Context, Source
from .csv_source import CsvSource
from .news import NewsSource
from .tracxn import TracxnSource
from .university import UniversitySource
from .websearch import WebSearchSource

_REGISTRY = {
    s.name: s for s in (
        AcceleratorSource(), UniversitySource(), CsvSource(),
        WebSearchSource(), NewsSource(), TracxnSource(),
    )
}

DEFAULT_SOURCES = ["accelerator", "university"]


def get_source(name: str) -> Source | None:
    return _REGISTRY.get(name)


def source_names() -> list[str]:
    return list(_REGISTRY)


__all__ = ["Context", "Source", "get_source", "source_names", "DEFAULT_SOURCES"]
