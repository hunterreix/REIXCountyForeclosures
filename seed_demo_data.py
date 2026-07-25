"""
seed_demo_data.py - Loads a handful of realistic sample records so you can
run/test the API and web app immediately, before scraper.py has been run
against the live Bexar County service (which requires outbound network
access this sandbox doesn't have).

Delete these rows (or just wipe the DB) once scraper.py has populated real
data: `rm reix_foreclosures.db && python db.py && python scraper.py`
"""
import datetime as dt
from db import get_conn, init_db

SAMPLE = [
    ("Bexar", "mortgage", "20260041231", "1042 CULEBRA RD", "SAN ANTONIO", "78201", "SAN ANTONIO ISD", 2026, 7, 29.4521, -98.5218),
    ("Bexar", "mortgage", "20260041288", "7815 MARBACH RD", "SAN ANTONIO", "78227", "EDGEWOOD ISD", 2026, 7, 29.4088, -98.6431),
    ("Bexar", "tax", "20260039012", "215 E COMMERCE ST", "SAN ANTONIO", "78205", "SAN ANTONIO ISD", 2026, 7, 29.4246, -98.4903),
    ("Bexar", "mortgage", "20260041355", "3390 BROADWAY ST", "SAN ANTONIO", "78209", "ALAMO HEIGHTS ISD", 2026, 7, 29.4699, -98.4703),
    ("Bexar", "tax", "20260039055", "912 NW LOOP 410", "SAN ANTONIO", "78216", "NORTH EAST ISD", 2026, 7, 29.5109, -98.5104),
    ("Bexar", "mortgage", "20260041390", "4521 HARRY WURZBACH RD", "SAN ANTONIO", "78209", "NORTH EAST ISD", 2026, 7, 29.4813, -98.4451),
    ("Bexar", "mortgage", "20260035120", "823 S FLORES ST", "SAN ANTONIO", "78204", "SAN ANTONIO ISD", 2026, 6, 29.4145, -98.4989),
    ("Bexar", "tax", "20260034988", "1108 PLEASANTON RD", "SAN ANTONIO", "78221", "SOUTH SAN ANTONIO ISD", 2026, 6, 29.3612, -98.5065),
    ("Bexar", "mortgage", "20260035201", "2210 BANDERA RD", "SAN ANTONIO", "78228", "EDGEWOOD ISD", 2026, 6, 29.4667, -98.5541),
    ("Bexar", "mortgage", "20260029900", "555 W SUNSET RD", "UNIVERSAL CITY", "78148", "JUDSON ISD", 2026, 5, 29.5497, -98.2933),
    ("Collin", "mortgage", "COLLIN-demo0001", "1007 STONEPORT LN", "ALLEN", "75002", None, 2026, 6, None, None),
    ("Collin", "mortgage", "COLLIN-demo0002", "10594 PRAIRIE ROSE RD", "FRISCO", "75035", None, 2026, 4, None, None),
    ("Collin", "mortgage", "COLLIN-demo0003", "1122 WATERFORD WAY", "ALLEN", "75013", None, 2026, 5, None, None),
    ("Collin", "mortgage", "COLLIN-demo0004", "114 CREEKSIDE DR", "MURPHY", "75094", None, 2026, 4, None, None),
]


def main():
    init_db()
    conn = get_conn()
    now = dt.datetime.utcnow().isoformat()
    for county, source_layer, doc, addr, city, zip_c, isd, year, month, lat, lon in SAMPLE:
        if county == "Bexar":
            doc_link = f"https://bexar.tx.publicsearch.us/results?searchType=quickSearch&query={doc}"
        else:
            doc_link = "https://apps2.collincountytx.gov/ForeclosureNotices"
        conn.execute(
            """INSERT OR IGNORE INTO properties
               (county, source_layer, doc_number, address, city, zip, school_dist, year, month,
                lat, lon, object_id, doc_link, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (county, source_layer, doc, addr, city, zip_c, isd, year, month, lat, lon, None, doc_link, now, now),
        )
    conn.commit()
    conn.close()
    print(f"Seeded {len(SAMPLE)} demo rows across {len(set(s[0] for s in SAMPLE))} counties.")


if __name__ == "__main__":
    main()
