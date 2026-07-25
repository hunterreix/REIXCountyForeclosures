"""
scraper.py - Pulls foreclosure notices from every county wired up in
sources/registry.py and upserts them into the shared database.

Run:
    python scraper.py                   # one-time sync of all enabled counties
    python scraper.py --county Bexar    # sync just one county
    python scraper.py --loop 21600      # sync every N seconds, forever
"""
import argparse
import datetime as dt
import sys
import time

from db import get_conn, init_db
from sources.registry import ENABLED_SOURCES


def upsert_records(conn, records) -> tuple[int, int]:
    now = dt.datetime.utcnow().isoformat()
    inserted, updated = 0, 0

    for r in records:
        existing = conn.execute(
            "SELECT id FROM properties WHERE county = ? AND doc_number = ? AND source_layer = ?",
            (r.county, r.doc_number, r.source_layer),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE properties SET
                    address=?, city=?, zip=?, school_dist=?, year=?, month=?,
                    lat=?, lon=?, doc_link=?, last_seen=?
                   WHERE id=?""",
                (r.address, r.city, r.zip, r.school_dist, r.year, r.month,
                 r.lat, r.lon, r.doc_link, now, existing["id"]),
            )
            updated += 1
        else:
            conn.execute(
                """INSERT INTO properties
                    (county, source_layer, doc_number, address, city, zip, school_dist,
                     year, month, lat, lon, object_id, doc_link, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r.county, r.source_layer, r.doc_number, r.address, r.city, r.zip,
                 r.school_dist, r.year, r.month, r.lat, r.lon, None, r.doc_link, now, now),
            )
            inserted += 1

    conn.commit()
    return inserted, updated


def log_run(conn, layer, fetched, inserted, updated, status, message=""):
    conn.execute(
        """INSERT INTO scrape_log (run_at, layer, fetched, inserted, updated, status, message)
           VALUES (?,?,?,?,?,?,?)""",
        (dt.datetime.utcnow().isoformat(), layer, fetched, inserted, updated, status, message),
    )
    conn.commit()


def sync_once(county_filter: str = None) -> dict:
    init_db()
    conn = get_conn()
    total_inserted = total_updated = 0
    per_county = {}

    for source in ENABLED_SOURCES:
        if county_filter and source.county_name.lower() != county_filter.lower():
            continue
        try:
            records = source.fetch()
            inserted, updated = upsert_records(conn, records)
            log_run(conn, source.county_name, len(records), inserted, updated, "ok")
            print(f"[{source.county_name}] fetched={len(records)} inserted={inserted} updated={updated}")
            total_inserted += inserted
            total_updated += updated
            per_county[source.county_name] = {"fetched": len(records), "inserted": inserted, "updated": updated}
        except Exception as e:
            log_run(conn, source.county_name, 0, 0, 0, "error", str(e))
            print(f"[{source.county_name}] ERROR: {e}", file=sys.stderr)
            per_county[source.county_name] = {"error": str(e)}

    conn.close()
    print(f"Done. total_inserted={total_inserted} total_updated={total_updated}")
    return {"total_inserted": total_inserted, "total_updated": total_updated, "by_county": per_county}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, default=0,
                         help="If set, re-run every N seconds forever (e.g. 3600 for hourly).")
    parser.add_argument("--county", type=str, default=None,
                         help="Only sync this county (e.g. --county Bexar)")
    args = parser.parse_args()

    if args.loop:
        while True:
            sync_once(args.county)
            print(f"Sleeping {args.loop}s...")
            time.sleep(args.loop)
    else:
        sync_once(args.county)


if __name__ == "__main__":
    main()
