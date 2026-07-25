"""
sources/collin.py - Collin County adapter.

Source: the county's own foreclosure notices web app
  https://apps2.collincountytx.gov/ForeclosureNotices

Unlike Bexar, this is NOT a documented JSON API -- it's a county-built
.NET web app with a filterable grid (by sale date, city, property type).
The good news: the base listing page is server-rendered, so plain
`requests` + BeautifulSoup can read it without a real browser. It also
gives every property a stable identity via address + sale date + file
date, so records can be deduped/upserted reliably even without a county
"doc number" in the list view.

WHAT'S CONFIRMED WORKING (verified against the live site):
  - The base URL returns page 1 of results server-rendered, with:
    address, city, sale date, file date, property type per row.
  - There were 683 total notices / 28 pages at time of writing.

WHAT STILL NEEDS 10 MINUTES OF MANUAL VERIFICATION (couldn't confirm
  from this environment, which can't run a real browser or inspect
  network requests):
  - The exact mechanism for pages 2-28. The page numbers render as an
    interactive grid control (likely Kendo/Telerik), which usually means
    there's a clean JSON endpoint underneath (e.g. a POST to something
    like Property/Read with take/skip params) -- that would be the
    "easy tier" version of this adapter. To find it: open the page in
    a real browser, open DevTools > Network, click to page 2, and see
    what request fires. Swap `_fetch_page_1_html` below for a call to
    that JSON endpoint once confirmed -- it'll be more reliable AND
    give you every field (including the real document/instrument
    number from the detail page) in one shot.
  - Until then, this adapter pulls page 1 only (25-50 most recent
    notices) as a working starting point.
  - The real county document number lives on each DetailPage/{id} view,
    not the list view -- so `doc_number` here is a synthetic, stable ID
    built from address+dates until a detail-page fetch step is added.
"""
import hashlib
import re

import requests
from bs4 import BeautifulSoup

from sources.base import CountySource, NormalizedRecord

LIST_URL = "https://apps2.collincountytx.gov/ForeclosureNotices"


class CollinSource(CountySource):
    county_name = "Collin"

    def fetch(self) -> list[NormalizedRecord]:
        resp = requests.get(LIST_URL, timeout=30, headers={"User-Agent": "REIXBot/1.0"})
        resp.raise_for_status()
        return self._parse(resp.text)

    def _parse(self, html: str) -> list[NormalizedRecord]:
        soup = BeautifulSoup(html, "html.parser")
        records = []

        # Each result row contains an address line, then City/Sale Date/File
        # Date/Property Type. The exact wrapper element/class can shift with
        # site updates, so we search text patterns rather than depend on a
        # specific CSS class -- more resilient to markup tweaks, at the cost
        # of being a bit more permissive. Verify against the live DOM if
        # this stops matching after a county site update.
        text_blocks = soup.get_text("\n").split("\n")
        row_pattern = re.compile(
            r"^(?P<address>.+?)\s+(?P<city2>[A-Z ]+),\s*TX\s+(?P<zip>\d{5})", re.IGNORECASE
        )

        # Fallback: iterate over table/list item elements if present, else
        # scan for the "City: X   Sale Date: X   File Date: X" pattern in
        # the raw text as a last resort.
        candidates = soup.select("li, tr, .property, .result-item")
        parsed_any = False
        for el in candidates:
            row_text = el.get_text(" ", strip=True)
            m = re.search(
                r"(?P<address>[\d].+?)\s+(?P<city>[A-Za-z ]+),\s*TX\s+(?P<zip>\d{5}).*?"
                r"City:\s*(?P<city2>[A-Za-z ]+)\s*Sale Date:\s*(?P<sale>[\d/]+)\s*"
                r"File Date:\s*(?P<filed>[\d/]+)",
                row_text,
            )
            if not m:
                continue
            parsed_any = True
            address = m.group("address").strip()
            city = m.group("city2").strip()
            zip_code = m.group("zip").strip()
            sale_date = m.group("sale").strip()
            file_date = m.group("filed").strip()

            month, year = None, None
            fm = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", file_date)
            if fm:
                month, year = int(fm.group(1)), int(fm.group(3))

            synthetic_id = "COLLIN-" + hashlib.sha1(
                f"{address}|{sale_date}|{file_date}".encode()
            ).hexdigest()[:16]

            records.append(NormalizedRecord(
                county="Collin",
                source_layer="mortgage",  # this feed is trustee/mortgage notices only
                doc_number=synthetic_id,
                address=address,
                city=city,
                zip=zip_code,
                school_dist=None,
                year=year,
                month=month,
                lat=None,   # no geocoding available from this source yet
                lon=None,
                doc_link=LIST_URL,  # TODO: swap for the real DetailPage/{id} link
            ))

        if not parsed_any:
            raise RuntimeError(
                "Collin adapter found 0 matching rows -- the site's markup likely "
                "changed. Open https://apps2.collincountytx.gov/ForeclosureNotices "
                "in a browser and update the parsing pattern in sources/collin.py."
            )

        return records
