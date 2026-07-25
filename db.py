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
