"""
events.py - An ordered change log, so a partner app (app.reix.co) can ask
"what changed since I last looked?".

Why it exists: a notice (and its address) is delivered first; its documents are
gathered by a person and uploaded hours or days later. The partner app needs to
(a) recognise the same property later, using a stable property_key, and (b) be
told when something about it changed. Every event has an increasing id, which the
partner stores as its cursor and passes back as ?after=<id>.

Event types: property_added, document_added, document_removed,
             document_none_found, deeds_updated
"""
import datetime as dt
import json

_TABLE = """CREATE TABLE IF NOT EXISTS property_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    property_id INTEGER NOT NULL,
    event_type  TEXT NOT NULL,
    ref_id      INTEGER,
    detail      TEXT NOT NULL DEFAULT ''
)"""
_INDEX = "CREATE INDEX IF NOT EXISTS idx_events_property ON property_events(property_id)"

LAYERS = ("mortgage", "tax")


def ensure_schema(conn):
    # separate statements (not executescript) so a caller's open transaction isn't committed early
    conn.execute(_TABLE)
    conn.execute(_INDEX)


def log_event(conn, property_id, event_type, ref_id=None, detail=""):
    """Record an event on the caller's connection. The caller commits."""
    ensure_schema(conn)
    conn.execute("INSERT INTO property_events (at, property_id, event_type, ref_id, detail) VALUES (?,?,?,?,?)",
                 (dt.datetime.utcnow().isoformat(), property_id, event_type, ref_id, str(detail)[:200]))


def property_key(county, layer, doc_number):
    """A stable id for a notice, e.g. 'bexar-mortgage-20261000380'. It never changes,
    even if the county later drops the notice from its feed."""
    return f"{str(county).lower()}-{layer}-{doc_number}"


def parse_key(key):
    parts = str(key or "").strip().lower().split("-", 2)
    if len(parts) != 3 or parts[1] not in LAYERS or not parts[2]:
        return None
    return parts[0], parts[1], parts[2]


def _detail(r):
    """sale_date_set events say which monthly list the notice is now in (and which it came from)."""
    if r["event_type"] != "sale_date_set":
        return None
    try:
        return json.loads(r["detail"])
    except ValueError:
        return None


def changes_after(conn, after, limit):
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT e.id, e.at, e.event_type, e.ref_id, e.detail, p.county, p.source_layer, p.doc_number
           FROM property_events e JOIN properties p ON p.id = e.property_id
           WHERE e.id > ? ORDER BY e.id LIMIT ?""", (after, limit + 1)).fetchall()
    more = len(rows) > limit
    events = [{"id": r["id"], "type": r["event_type"], "at": r["at"],
               "property_key": property_key(r["county"], r["source_layer"], r["doc_number"]),
               "file_id": r["ref_id"] if r["event_type"].startswith("document_") else None,
               "detail": _detail(r)}
              for r in rows[:limit]]
    latest = conn.execute("SELECT COALESCE(MAX(id), 0) m FROM property_events").fetchone()["m"]
    return events, more, latest
