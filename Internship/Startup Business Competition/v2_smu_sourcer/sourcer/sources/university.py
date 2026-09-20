"""
University source: startups from competition-winners / alumni-ventures pages.

Best match for the competition's "university student / recent alumni" requirement -
but yield is lower and noisier than accelerators, since these pages mix news, program
descriptions, and sponsor logos. Point the seed CSV at the specific winners/portfolio
page rather than a university root for far better results.
"""
from __future__ import annotations

from typing import Iterable

from ..models import Startup
from ._portfolio import scrape_listing
from .base import Context


class UniversitySource:
    name = "university"

    def discover(self, ctx: Context) -> Iterable[Startup]:
        return scrape_listing(ctx, "universities.csv", self.name)
