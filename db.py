"""
db.py - SQLite schema + connection helper for the REIX Bexar County
Foreclosure API.

Kept intentionally simple (plain sqlite3, no ORM) so it's easy to read,
deploy, and swap for Postgres later if traffic grows.
"""
import sqlite3
import os

DB_PATH = os.environ.get("REIX_DB_PATH", os.path.join(os.path.dirname(__file__), "reix_foreclosures.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    county        TEXT NOT NULL DEFAULT 'Bexar',
    source_layer  TEXT NOT NULL,          -- 'mortgage' or 'tax'
    doc_number    TEXT NOT NULL,
    address       TEXT,
    city          TEXT,
    zip           TEXT,
    school_dist   TEXT,
    year          INTEGER,
    month         INTEGER,
    lat           REAL,
    lon           REAL,
    object_id     INTEGER,
    doc_link      TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    UNIQUE(county, doc_number, source_layer)
);

CREATE INDEX IF NOT EXISTS idx_properties_county ON properties(county);
CREATE INDEX IF NOT EXISTS idx_properties_city ON properties(city);
CREATE INDEX IF NOT EXISTS idx_properties_zip ON properties(zip);
CREATE INDEX IF NOT EXISTS idx_properties_type ON properties(source_layer);
CREATE INDEX IF NOT EXISTS idx_properties_yearmonth ON properties(year, month);

CREATE TABLE IF NOT EXISTS api_keys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key       TEXT UNIQUE NOT NULL,
    name          TEXT,
    email         TEXT,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    request_count INTEGER DEFAULT 0,
    active        INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS scrape_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TEXT NOT NULL,
    layer       TEXT,
    fetched     INTEGER,
    inserted    INTEGER,
    updated     INTEGER,
    status      TEXT,
    message     TEXT
);

-- Court-house documents linked to a notice: the notice itself, the deed of
-- trust it forecloses, and related assignments/releases/liens. We store
-- LINKS (and a little metadata), never copies of the documents.
CREATE TABLE IF NOT EXISTS property_documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id   INTEGER NOT NULL,
    doc_role      TEXT NOT NULL,   -- notice | deed_of_trust | assignment | release | lien
    doc_number    TEXT,
    doc_type      TEXT,
    recorded_date TEXT,
    url           TEXT NOT NULL,
    match_method  TEXT,            -- how we found it (e.g. 'clerk_search_by_doc_number')
    confidence    TEXT,            -- high | medium | low
    fetched_at    TEXT NOT NULL,
    UNIQUE(property_id, doc_role, url)
);
CREATE INDEX IF NOT EXISTS idx_propdocs_property ON property_documents(property_id);

-- One row per property tracking document-resolution attempts, so the worker
-- doesn't retry forever and we can see what's pending/failed.
CREATE TABLE IF NOT EXISTS doc_resolution (
    property_id  INTEGER PRIMARY KEY,
    status       TEXT NOT NULL,    -- resolved | not_found | error
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_attempt TEXT,
    message      TEXT
);

-- Deed history from the Bexar Central Appraisal District (BCAD) property record:
-- the ownership transfers (vesting deeds) for a notice's property, newest first.
-- Not the deed of trust being foreclosed -- that comes from the notice itself.
-- Empty strings (not NULL) mean "not provided", so re-imports stay idempotent.
CREATE TABLE IF NOT EXISTS property_deeds (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id       INTEGER NOT NULL,
    bcad_property_id  TEXT NOT NULL DEFAULT '',
    deed_date         TEXT NOT NULL DEFAULT '',   -- YYYY-MM-DD
    deed_type         TEXT NOT NULL DEFAULT '',   -- e.g. WD
    deed_type_desc    TEXT NOT NULL DEFAULT '',   -- e.g. Warranty Deed
    grantor           TEXT NOT NULL DEFAULT '',
    grantee           TEXT NOT NULL DEFAULT '',
    volume            TEXT NOT NULL DEFAULT '',
    page              TEXT NOT NULL DEFAULT '',
    instrument_number TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL DEFAULT '',   -- bcad_manual | bcad_export
    fetched_at        TEXT NOT NULL,
    UNIQUE(property_id, deed_date, instrument_number, grantor, grantee)
);
CREATE INDEX IF NOT EXISTS idx_propdeeds_property ON property_deeds(property_id);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized DB at {DB_PATH}")


# Counties we used to serve and no longer do. At startup their rows (and everything tied to them) are
# copied to a JSON file next to the database and then deleted. Idempotent: once they are gone this does nothing.
# A property that has staff-uploaded documents is NEVER deleted.
REMOVED_COUNTIES = ("Collin",)


def purge_removed_counties():
    import datetime as dt
    import json
    try:
        conn = get_conn()
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for county in REMOVED_COUNTIES:
            props = [dict(r) for r in conn.execute("SELECT * FROM properties WHERE county = ?", (county,)).fetchall()]
            if not props:
                conn.execute("DELETE FROM scrape_log WHERE layer = ?", (county,))   # leftover sync history
                conn.commit()
                continue
            chunks = lambda ids: [ids[i:i + 500] for i in range(0, len(ids), 500)]
            keep = set()                                    # properties that have staff-uploaded documents are never deleted
            if "property_files" in tables:
                for c in chunks([p["id"] for p in props]):
                    keep |= {r["property_id"] for r in conn.execute(
                        f"SELECT DISTINCT property_id FROM property_files WHERE property_id IN ({','.join('?' * len(c))})", c).fetchall()}
            if keep:
                print(f"[cleanup] keeping {len(keep)} {county} propert{'y that has' if len(keep) == 1 else 'ies that have'} uploaded documents")
            doomed = [p for p in props if p["id"] not in keep]
            if not doomed:
                continue
            folder = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "removed")
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, f"{county.lower()}-{dt.date.today().isoformat()}.json"), "w") as f:
                json.dump(doomed, f, indent=1, default=str)      # a copy of what is being removed
            for c in chunks([p["id"] for p in doomed]):
                marks = ",".join("?" * len(c))
                for t in ("property_documents", "doc_resolution", "property_deeds", "property_events"):
                    if t in tables:
                        conn.execute(f"DELETE FROM {t} WHERE property_id IN ({marks})", c)
                conn.execute(f"DELETE FROM properties WHERE id IN ({marks})", c)
            if not keep:
                conn.execute("DELETE FROM scrape_log WHERE layer = ?", (county,))
            conn.commit()
            print(f"[cleanup] removed {len(doomed)} {county} propert{'y' if len(doomed) == 1 else 'ies'} (a copy was saved in {folder})")
        conn.close()
    except Exception as e:       # never let cleanup stop the app from starting
        print(f"[cleanup] skipped: {e}")
