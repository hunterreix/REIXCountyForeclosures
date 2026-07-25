"""
sources/bexar.py - Bexar County adapter.

Source: the ArcGIS FeatureServer that backs maps.bexar.org/foreclosures/
  https://maps.bexar.org/arcgis/rest/services/CC/ForeclosuresProd/MapServer
  Layer 0 = Mortgage, Layer 1 = Tax. Public, no key, JSON/GeoJSON, paginated,
  and returns coordinates pre-projected to WGS84 when asked (outSR=4326).

This is the easiest tier: a real, documented, structured API. No HTML
parsing required.
"""
import requests

from sources.base import CountySource, NormalizedRecord

BASE_URL = "https://maps.bexar.org/arcgis/rest/services/CC/ForeclosuresProd/MapServer"
LAYERS = {0: "mortgage", 1: "tax"}
PAGE_SIZE = 1000

# Placeholder until a direct document-viewer URL pattern is confirmed --
# see the note in README.md about linking Doc # to the recorded document.
DOC_LINK_TEMPLATE = "https://bexar.tx.publicsearch.us/results?searchType=quickSearch&query={doc_number}"


class BexarSource(CountySource):
    county_name = "Bexar"

    def fetch(self) -> list[NormalizedRecord]:
        records = []
        for layer_id, layer_name in LAYERS.items():
            records.extend(self._fetch_layer(layer_id, layer_name))
        return records

    def _fetch_layer(self, layer_id: int, layer_name: str) -> list[NormalizedRecord]:
        out = []
        offset = 0
        while True:
            params = {
                "where": "1=1",
                "outFields": "*",
                "outSR": 4326,
                "f": "json",
                "resultOffset": offset,
                "resultRecordCount": PAGE_SIZE,
            }
            resp = requests.get(f"{BASE_URL}/{layer_id}/query", params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(f"Bexar ArcGIS error (layer {layer_id}): {data['error']}")

            features = data.get("features", [])
            for feat in features:
                attrs = feat.get("attributes", {})
                geom = feat.get("geometry") or {}
                doc_number = (attrs.get("DOC_NUMBER") or "").strip()
                if not doc_number:
                    continue
                out.append(NormalizedRecord(
                    county="Bexar",
                    source_layer=layer_name,
                    doc_number=doc_number,
                    address=(attrs.get("ADDRESS") or "").strip(),
                    city=(attrs.get("CITY") or "").strip(),
                    zip=(attrs.get("ZIP") or "").strip(),
                    school_dist=(attrs.get("SCHOOL_DIST") or "").strip(),
                    year=attrs.get("YEAR"),
                    month=attrs.get("MONTH"),
                    lat=geom.get("y"),
                    lon=geom.get("x"),
                    doc_link=DOC_LINK_TEMPLATE.format(doc_number=doc_number),
                ))

            if not data.get("exceededTransferLimit") or not features:
                break
            offset += PAGE_SIZE

        return out
