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
