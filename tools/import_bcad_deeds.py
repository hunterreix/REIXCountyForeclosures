"""
tools/import_bcad_deeds.py - Load BCAD deed history for foreclosure notices.

Where the CSV comes from: either a person copying the "Deed History" table from
a property's BCAD record (hgo.harrisgovern.com/bexar/property/<year>-<id>), or a
BCAD appraisal-data export converted to these columns. This script does NOT
touch BCAD's website; it only reads your CSV and sends it to our app.

CSV columns (see bcad_deeds_template.csv):
  notice_doc_number, bcad_property_id, deed_date (MM/DD/YYYY or YYYY-MM-DD),
  deed_type, deed_type_desc, grantor, grantee, volume, page, instrument_number
One row per deed. Rows are grouped by notice_doc_number. Keep the newest few
deeds per property; the API shows the latest 3.

Usage:
  python tools/import_bcad_deeds.py my_deeds.csv --dry-run     # check only
  REIX_APP_URL=https://... REIX_SYNC_SECRET=... python tools/import_bcad_deeds.py my_deeds.csv
Re-running is safe (idempotent).
"""
import csv
import os
import re
import sys
from collections import OrderedDict


def iso_date(s):
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return s  # already ISO, or blank (BCAD sometimes has no date on very old deeds)


def build_payloads(path):
    groups = OrderedDict()
    problems = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for n, row in enumerate(csv.DictReader(f), start=2):
            doc = (row.get("notice_doc_number") or "").strip()
            if not doc.isdigit():
                problems.append(f"line {n}: notice_doc_number must be digits")
                continue
            date = iso_date(row.get("deed_date"))
            if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                problems.append(f"line {n}: unreadable deed_date {row.get('deed_date')!r}")
                continue
            inst = (row.get("instrument_number") or "").strip()
            if inst and not inst.isdigit():
                problems.append(f"line {n}: instrument_number must be digits")
                continue
            g = groups.setdefault(doc, {"notice_doc_number": doc, "county": "Bexar",
                                         "bcad_property_id": (row.get("bcad_property_id") or "").strip(),
                                         "source": "bcad_manual", "deeds": []})
            g["deeds"].append({"deed_date": date,
                               "deed_type": (row.get("deed_type") or "").strip(),
                               "deed_type_desc": (row.get("deed_type_desc") or "").strip(),
                               "grantor": (row.get("grantor") or "").strip(),
                               "grantee": (row.get("grantee") or "").strip(),
                               "volume": (row.get("volume") or "").strip(),
                               "page": (row.get("page") or "").strip(),
                               "instrument_number": inst})
    return list(groups.values()), problems


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    if not args:
        print(__doc__)
        return 1
    payloads, problems = build_payloads(args[0])
    for p in problems:
        print("PROBLEM:", p)
    print(f"{len(payloads)} notice(s), {sum(len(p['deeds']) for p in payloads)} deed row(s) read")
    if problems:
        print("Fix the problems above and re-run.")
        return 1
    if dry:
        print("Dry run - nothing sent.")
        return 0

    import requests
    base = os.environ["REIX_APP_URL"].rstrip("/")
    secret = os.environ["REIX_SYNC_SECRET"]
    bad = 0
    for p in payloads:
        r = requests.post(f"{base}/internal/deeds", json=p, headers={"X-Sync-Secret": secret}, timeout=30)
        ok = r.status_code == 200
        bad += not ok
        print(f"{p['notice_doc_number']}: {'ok ' + str(r.json().get('deeds_saved')) + ' saved' if ok else 'ERROR ' + str(r.status_code) + ' ' + r.text[:120]}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
