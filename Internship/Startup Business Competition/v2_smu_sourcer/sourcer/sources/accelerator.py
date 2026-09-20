"""Accelerator / incubator portfolio source (YC, Techstars, Antler, ...)."""
from __future__ import annotations

from typing import Iterable

from ..models import Startup
from ._portfolio import scrape_listing
from .base import Context


class AcceleratorSource:
    name = "accelerator"

    def discover(self, ctx: Context) -> Iterable[Startup]:
        return scrape_listing(ctx, "accelerators.csv", self.name)
