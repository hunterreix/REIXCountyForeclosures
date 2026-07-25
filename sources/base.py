"""
sources/base.py - The contract every county adapter implements.

A "source" is one county's data feed. Each source module normalizes its
county's raw format into the same flat record shape so the rest of the
app (db, API, web UI) never has to know or care which county a record
came from, or what format the county happened to publish it in.

Only counties with a genuinely easy, structured, machine-readable feed
belong here (a real API, a clean feature service, or at minimum a
consistent, script-friendly page). Counties whose only option is a PDF
notice, a paid data broker, or a click-through portal designed to be used
by a human in a browser are deliberately left out -- see README.md for
the survey of what was checked and why.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class NormalizedRecord:
    county: str            # e.g. "Bexar", "Collin"
    source_layer: str       # "mortgage" or "tax"
    doc_number: str         # county's unique identifier for this notice
    address: str
    city: str
    zip: str
    school_dist: Optional[str] = None
    year: Optional[int] = None
    month: Optional[int] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    doc_link: Optional[str] = None


class CountySource:
    """Subclass this once per county. Keep fetch() side-effect free --
    it should just return normalized records; scraper.py handles the DB."""

    county_name: str = "override-me"

    def fetch(self) -> list[NormalizedRecord]:
        raise NotImplementedError
