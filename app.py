"""
app.py - REIX Bexar County Foreclosure API + simple web app.

Two layers live in the same Flask app:

1. Public, unauthenticated, rate-limited-by-nothing-fancy endpoints under
   /api/public/* -- used ONLY by our own web app (static/index.html) so
   visitors don't need to sign up for a key just to browse the list.

2. Developer-facing endpoints under /api/v1/* -- require an API key
   (header: X-API-Key). Any outside app/team can self-serve a key at
   POST /api/v1/keys -- no approval step, so it's frictionless to plug in.

Every response is small, flat JSON. No auth ceremony beyond the header.
"""
import datetime as dt
import math
import secrets

from flask import Flask, jsonify, request, send_from_directory

from db import get_conn, init_db

app = Flask(__name__, static_folder="static", static_url_path="")

# Ensure the schema exists whenever this module is imported -- including
# under gunicorn, which imports `app:app` directly and never runs the
# `if __name__ == "__main__"` block below. Safe to call repeatedly
# (CREATE TABLE IF NOT EXISTS).
init_db()

# Human document-intake tool (login, work list, PDF uploads) - see intake.py
from intake import intake_bp, init_intake, require_user, attach_viewer_docs  # noqa: E402
app.register_blueprint(intake_bp)
init_intake(app)

# How the REIX app stays in step with us (change feed, documents by property_key) - see sync_api.py
from sync_api import sync_bp, attach_status  # noqa: E402
from events import log_event  # noqa: E402
app.register_blueprint(sync_bp)

# Counties we no longer serve (Collin) are cleaned out once, at startup - see db.purge_removed_counties
from db import purge_removed_counties  # noqa: E402
purge_removed_counties()


@app.after_request
def add_cors_headers(resp):
    """Manual CORS (no flask-cors dependency) so third-party apps can call
    /api/v1/* directly from browser JS."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/api/<path:_any>", methods=["OPTIONS"])
def cors_preflight(_any):
    return ("", 204)

REIX_URL = "https://app.reix.co/"
MAX_PER_PAGE = 100
DEFAULT_PER_PAGE = 25


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def build_full_address(address, city, zip_code):
    """Assemble a single mailing-address string from the county's separate
    address/city/zip fields. Texas is hardcoded since this feed is Bexar
    County (TX) only -- the source data has no state field."""
    parts = [p for p in [(address or "").strip()] if p]
    city_state_zip = ", ".join(p for p in [(city or "").strip()] if p)
    if city_state_zip:
        city_state_zip += ", TX"
    else:
        city_state_zip = "TX"
    if zip_code:
        city_state_zip += f" {zip_code}"
    parts.append(city_state_zip)
    return ", ".join(parts) if parts[0] else city_state_zip


# Same link format the Bexar County Clerk's own foreclosure map uses for each
# notice's document number. It opens the Clerk's results page for that notice.
# It lists notices with upcoming sale dates; once a sale date has passed the
# Clerk's site no longer returns it by default (the web app copies the doc # so
# it can be pasted into the Clerk's Foreclosures search instead).
BEXAR_NOTICE_LINK = ("https://bexar.tx.publicsearch.us/results?department=FC"
                     "&searchOcrText=false&searchType=quickSearch&searchValue={doc}")


def row_to_dict(row):
    d = dict(row)
    d["full_address"] = build_full_address(d.get("address"), d.get("city"), d.get("zip"))
    d["reix_link"] = REIX_URL
    if d.get("county") == "Bexar" and d.get("type") == "mortgage" and d.get("doc_number"):
        # computed at read time so it also fixes older rows stored with the old link
        d["doc_link"] = BEXAR_NOTICE_LINK.format(doc=d["doc_number"])
    # Monthly list: the real sale month once the notice is processed, otherwise the county's filing cycle
    d["cycle"] = f"{d['year']:04d}-{d['month']:02d}" if d.get("year") and d.get("month") else None
    if d.get("sale_date"):
        d["list_month"], d["list_basis"] = d["sale_date"][:7], "sale_date"
    else:
        d["list_month"], d["list_basis"] = d["cycle"], "county_cycle"
    return d


def build_filters(args):
    """Shared query-building logic for both public and v1 endpoints."""
    clauses = []
    params = []

    county = args.get("county", "").strip()
    if county:
        clauses.append("county = ?")
        params.append(county)

    pkey = args.get("property_key", "").strip().lower()      # e.g. bexar-mortgage-20261000380
    if pkey:
        parts = pkey.split("-", 2)
        if len(parts) == 3 and parts[1] in ("mortgage", "tax"):
            clauses.append("LOWER(county) = ? AND source_layer = ? AND doc_number = ?")
            params += [parts[0], parts[1], parts[2]]
        else:
            clauses.append("1 = 0")

    if args.get("docs") == "complete":          # only properties whose documents are all in
        have = ("EXISTS (SELECT 1 FROM property_files f WHERE f.property_id = properties.id "
                "AND f.doc_role = '{r}' AND f.status IN ({s}))")
        clauses.append(" AND ".join([have.format(r="notice", s="'uploaded'"),
                                     have.format(r="deed_of_trust", s="'uploaded'")]))

    q = args.get("q", "").strip()
    if q:
        clauses.append("(address LIKE ? OR doc_number LIKE ? OR zip LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]

    ptype = args.get("type", "").strip().lower()
    if ptype in ("mortgage", "tax"):
        clauses.append("source_layer = ?")
        params.append(ptype)

    city = args.get("city", "").strip()
    if city:
        clauses.append("city = ?")
        params.append(city)

    zip_code = args.get("zip", "").strip()
    if zip_code:
        clauses.append("zip = ?")
        params.append(zip_code)

    year = args.get("year", "").strip()
    if year.isdigit():
        clauses.append("year = ?")
        params.append(int(year))

    month = args.get("month", "").strip()
    if month.isdigit():
        clauses.append("month = ?")
        params.append(int(month))

    list_month = args.get("list_month", "").strip()          # a monthly list, e.g. 2026-11
    if len(list_month) == 7 and list_month[4] == "-" and (list_month[:4] + list_month[5:]).isdigit():
        clauses.append("COALESCE(substr(sale_date, 1, 7), printf('%04d-%02d', year, month)) = ?")
        params.append(list_month)
    basis = args.get("basis", "").strip().lower()            # confirmed = real sale date known; provisional = filing cycle only
    if basis == "confirmed":
        clauses.append("sale_date IS NOT NULL")
    elif basis == "provisional":
        clauses.append("sale_date IS NULL")

    doc_number = args.get("doc_number", "").strip()
    if doc_number:
        clauses.append("doc_number = ?")
        params.append(doc_number)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def paginate_args(args):
    try:
        page = max(1, int(args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        per_page = int(args.get("per_page", DEFAULT_PER_PAGE))
    except ValueError:
        per_page = DEFAULT_PER_PAGE
    per_page = max(1, min(per_page, MAX_PER_PAGE))
    return page, per_page


def attach_documents(conn, items):
    """Adds a `documents` list to each property dict (one query for all)."""
    ids = [i["id"] for i in items]
    for i in items:
        i["documents"] = []
    if not ids:
        return items
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT property_id, doc_role, doc_number, doc_type, recorded_date,
                   url, confidence
            FROM property_documents WHERE property_id IN ({marks})
            ORDER BY id""",
        ids,
    ).fetchall()
    by_id = {i["id"]: i for i in items}
    for r in rows:
        d = dict(r)
        by_id[d.pop("property_id")]["documents"].append(d)
    # Once a notice has been found on the Clerk's site, `doc_link` IS the
    # direct link to that recorded notice. Done at read time (not stored over
    # the properties row) so the daily county sync can't overwrite it.
    for i in items:
        notice = next((d for d in i["documents"] if d["doc_role"] == "notice"), None)
        i["doc_link_is_direct"] = bool(notice)
        if notice:
            i["doc_link"] = notice["url"]
    return items


# Link to a property's record on BCAD's property search (stable, opens directly).
BCAD_PROPERTY_URL = "https://hgo.harrisgovern.com/bexar/property/{year}-{pid}"
DEED_HISTORY_LIMIT = 3


def attach_deeds(conn, items):
    """Adds the latest few ownership transfers (from the BCAD record) to each
    property: deed_history (newest first), last_transfer, and a bcad_url."""
    ids = [i["id"] for i in items]
    for i in items:
        i["deed_history"] = []
        i["last_transfer"] = None
        i["bcad_url"] = None
    if not ids:
        return items
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT property_id, bcad_property_id, deed_date, deed_type,
                   deed_type_desc, grantor, grantee, volume, page, instrument_number
            FROM property_deeds WHERE property_id IN ({marks})
            ORDER BY (deed_date = ''), deed_date DESC, id DESC""",
        ids,
    ).fetchall()
    by_id = {i["id"]: i for i in items}
    for r in rows:
        d = dict(r)
        item = by_id[d.pop("property_id")]
        bcad = d.pop("bcad_property_id")
        if bcad and not item["bcad_url"]:
            item["bcad_url"] = BCAD_PROPERTY_URL.format(year=dt.date.today().year, pid=bcad)
        if len(item["deed_history"]) < DEED_HISTORY_LIMIT:
            item["deed_history"].append(d)
    for i in items:
        if i["deed_history"]:
            i["last_transfer"] = i["deed_history"][0]
    return items


def query_properties(args):
    conn = get_conn()
    where, params = build_filters(args)
    page, per_page = paginate_args(args)
    offset = (page - 1) * per_page

    total = conn.execute(f"SELECT COUNT(*) AS c FROM properties {where}", params).fetchone()["c"]

    rows = conn.execute(
        f"""SELECT id, county, source_layer AS type, doc_number, address, city, zip,
                   school_dist, year, month, lat, lon, doc_link, first_seen, last_seen,
                   sale_date, sale_date_source
            FROM properties {where}
            ORDER BY year DESC, month DESC, id DESC
            LIMIT ? OFFSET ?""",
        params + [per_page, offset],
    ).fetchall()
    results = attach_status(conn, attach_deeds(conn, attach_documents(conn, [row_to_dict(r) for r in rows])))
    conn.close()

    return {
        "results": results,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": max(1, math.ceil(total / per_page)),
    }


def summary_stats():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) AS c FROM properties").fetchone()["c"]
    by_county = conn.execute(
        "SELECT county, COUNT(*) AS c FROM properties GROUP BY county ORDER BY c DESC"
    ).fetchall()
    by_type = conn.execute(
        "SELECT source_layer AS type, COUNT(*) AS c FROM properties GROUP BY source_layer"
    ).fetchall()
    by_city = conn.execute(
        "SELECT city, COUNT(*) AS c FROM properties WHERE city != '' GROUP BY city ORDER BY c DESC LIMIT 10"
    ).fetchall()
    by_month = conn.execute(
        """SELECT CAST(substr(m, 1, 4) AS INTEGER) AS year, CAST(substr(m, 6, 2) AS INTEGER) AS month, COUNT(*) AS c
           FROM (SELECT COALESCE(substr(sale_date, 1, 7), printf('%04d-%02d', year, month)) AS m FROM properties
                 WHERE sale_date IS NOT NULL OR (year IS NOT NULL AND month IS NOT NULL))
           GROUP BY m ORDER BY m DESC LIMIT 12"""
    ).fetchall()
    last_run = conn.execute(
        """SELECT run_at, layer AS county, fetched, inserted, updated, status, message
           FROM scrape_log ORDER BY id DESC LIMIT 25"""
    ).fetchall()
    most_recent = conn.execute(
        "SELECT run_at FROM scrape_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    return {
        "total_properties": total,
        "by_county": [dict(r) for r in by_county],
        "by_type": [dict(r) for r in by_type],
        "top_cities": [dict(r) for r in by_city],
        "by_month": [dict(r) for r in by_month],
        "last_sync_at": most_recent["run_at"] if most_recent else None,
        "sync_history": [dict(r) for r in last_run],
        "reix_link": REIX_URL,
    }


def meta_options():
    conn = get_conn()
    cities = [r["city"] for r in conn.execute(
        "SELECT DISTINCT city FROM properties WHERE city != '' ORDER BY city"
    ).fetchall()]
    counties = [r["county"] for r in conn.execute(
        "SELECT DISTINCT county FROM properties ORDER BY county"
    ).fetchall()]
    months = [{"month": m["month"], "label": m["label"], "total": m["total"], "basis": m["basis"]}
              for m in list_summaries(conn)]
    conn.close()
    return {"types": ["mortgage", "tax"], "cities": cities, "counties": counties, "months": months}


# --------------------------------------------------------------------------
# API key auth (for /api/v1/*)
# --------------------------------------------------------------------------

def require_api_key():
    key = request.headers.get("X-API-Key") or request.args.get("api_key")
    if not key:
        return None, (jsonify({"error": "Missing API key. Include header 'X-API-Key'. "
                                          "Get one free at POST /api/v1/keys"}), 401)
    conn = get_conn()
    row = conn.execute("SELECT * FROM api_keys WHERE api_key = ? AND active = 1", (key,)).fetchone()
    if not row:
        conn.close()
        return None, (jsonify({"error": "Invalid or inactive API key"}), 401)
    conn.execute(
        "UPDATE api_keys SET last_used_at = ?, request_count = request_count + 1 WHERE id = ?",
        (dt.datetime.utcnow().isoformat(), row["id"]),
    )
    conn.commit()
    conn.close()
    return row, None


# --------------------------------------------------------------------------
# Public endpoints (used by our own web app only, no key required)
# --------------------------------------------------------------------------

@app.route("/api/public/properties")
def public_properties():
    data = query_properties(request.args)
    conn = get_conn()
    attach_viewer_docs(conn, data["results"])   # PDF links + key facts, only where all documents are in
    conn.close()
    return jsonify(data)


@app.route("/api/public/stats/summary")
def public_summary():
    return jsonify(summary_stats())


@app.route("/api/public/meta")
def public_meta():
    return jsonify(meta_options())


# --------------------------------------------------------------------------
# Developer-facing v1 API (API key required, CORS-open, self-serve keys)
# --------------------------------------------------------------------------

@app.route("/api/v1/keys", methods=["POST"])
@require_user
def create_key():
    """API key for a signed-in user. (Keys used to be open to anyone; that would let
    anyone get around the sign-in on the list, so creating one now requires an account.)
    Sign in with POST /auth/login, then send the returned csrf as X-CSRF-Token."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()[:120]
    email = request.intake_user["email"]

    new_key = "reix_" + secrets.token_urlsafe(24)
    conn = get_conn()
    conn.execute(
        "INSERT INTO api_keys (api_key, name, email, created_at) VALUES (?,?,?,?)",
        (new_key, name, email, dt.datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

    return jsonify({
        "api_key": new_key,
        "usage": "Send this as the 'X-API-Key' header on requests to /api/v1/*",
        "docs": "/docs",
    }), 201


@app.route("/api/v1/properties")
def v1_properties():
    _, err = require_api_key()
    if err:
        return err
    return jsonify(query_properties(request.args))


@app.route("/api/v1/properties/<int:property_id>")
def v1_property_detail(property_id):
    _, err = require_api_key()
    if err:
        return err
    conn = get_conn()
    row = conn.execute(
        """SELECT id, county, source_layer AS type, doc_number, address, city, zip,
                  school_dist, year, month, lat, lon, doc_link, first_seen, last_seen,
                  sale_date, sale_date_source
           FROM properties WHERE id = ?""",
        (property_id,),
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    item = attach_status(conn, attach_deeds(conn, attach_documents(conn, [row_to_dict(row)])))[0]
    conn.close()
    return jsonify(item)


@app.route("/api/v1/stats/summary")
def v1_summary():
    _, err = require_api_key()
    if err:
        return err
    return jsonify(summary_stats())


@app.route("/api/v1/meta")
def v1_meta():
    _, err = require_api_key()
    if err:
        return err
    return jsonify(meta_options())


def _sync_secret_ok():
    import os
    expected = os.environ.get("REIX_SYNC_SECRET")
    return bool(expected) and request.headers.get("X-Sync-Secret") == expected


@app.route("/internal/documents/pending")
def internal_docs_pending():
    """Notices that still need their courthouse documents resolved.
    Used by the document worker (GitHub Actions). Secret-protected."""
    if not _sync_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    county = request.args.get("county", "Bexar")
    try:
        limit = max(1, min(int(request.args.get("limit", 25)), 100))
    except ValueError:
        limit = 25
    conn = get_conn()
    rows = conn.execute(
        """SELECT p.id, p.county, p.doc_number, p.address, p.year, p.month
           FROM properties p
           LEFT JOIN doc_resolution r ON r.property_id = p.id
           WHERE p.county = ? AND p.source_layer = 'mortgage'
             AND (r.property_id IS NULL
                  OR (r.status != 'resolved' AND r.attempts < 3))
           ORDER BY p.year DESC, p.month DESC, p.id DESC
           LIMIT ?""",
        (county, limit),
    ).fetchall()
    conn.close()
    return jsonify({"pending": [dict(r) for r in rows]})


@app.route("/internal/documents", methods=["POST"])
def internal_docs_post():
    """Worker reports the outcome for one property: a status plus any
    documents it found. Secret-protected. Idempotent."""
    if not _sync_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    pid = body.get("property_id")
    status = body.get("status")
    if not isinstance(pid, int) or status not in ("resolved", "not_found", "error"):
        return jsonify({"error": "property_id (int) and status "
                                  "(resolved|not_found|error) required"}), 400
    now = dt.datetime.utcnow().isoformat()
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM properties WHERE id = ?", (pid,)).fetchone():
        conn.close()
        return jsonify({"error": "unknown property_id"}), 404
    roles = {"notice", "deed_of_trust", "assignment", "release", "lien"}
    saved = 0
    for d in body.get("documents") or []:
        url = (d.get("url") or "").strip()
        role = d.get("doc_role")
        # only ever store links to the County Clerk's own search site
        if role not in roles or not url.startswith("https://") or ".publicsearch.us/" not in url:
            continue
        conn.execute(
            """INSERT INTO property_documents
                 (property_id, doc_role, doc_number, doc_type, recorded_date,
                  url, match_method, confidence, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(property_id, doc_role, url) DO UPDATE SET
                 doc_number=excluded.doc_number, doc_type=excluded.doc_type,
                 recorded_date=excluded.recorded_date,
                 match_method=excluded.match_method,
                 confidence=excluded.confidence, fetched_at=excluded.fetched_at""",
            (pid, role, d.get("doc_number"), d.get("doc_type"),
             d.get("recorded_date"), url, d.get("match_method"),
             d.get("confidence"), now),
        )
        saved += 1
    conn.execute(
        """INSERT INTO doc_resolution (property_id, status, attempts, last_attempt, message)
           VALUES (?,?,1,?,?)
           ON CONFLICT(property_id) DO UPDATE SET
             status=excluded.status, attempts=attempts+1,
             last_attempt=excluded.last_attempt, message=excluded.message""",
        (pid, status, now, (body.get("message") or "")[:300]),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "documents_saved": saved})


@app.route("/internal/deeds", methods=["POST"])
def internal_deeds_post():
    """Loads BCAD deed history for one notice's property. Secret-protected and
    idempotent. Body:
      {"notice_doc_number": "20261000380", "county": "Bexar",
       "bcad_property_id": "393837",
       "deeds": [{"deed_date": "2023-04-04", "deed_type": "WD",
                  "deed_type_desc": "Warranty Deed", "grantor": "...",
                  "grantee": "...", "volume": "", "page": "",
                  "instrument_number": "20230062125"}]}"""
    import re
    if not _sync_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    doc = str(body.get("notice_doc_number") or "").strip()
    county = str(body.get("county") or "Bexar").strip()
    bcad = str(body.get("bcad_property_id") or "").strip()
    source = str(body.get("source") or "bcad_manual").strip()[:20]
    if not doc or (bcad and not bcad.isdigit()):
        return jsonify({"error": "notice_doc_number required; bcad_property_id must be digits"}), 400

    conn = get_conn()
    prop = conn.execute(
        "SELECT id FROM properties WHERE county = ? AND doc_number = ?", (county, doc)
    ).fetchone()
    if not prop:
        conn.close()
        return jsonify({"error": "unknown notice"}), 404

    now = dt.datetime.utcnow().isoformat()
    saved, skipped = 0, []
    for n, d in enumerate((body.get("deeds") or [])[:25]):
        date = str(d.get("deed_date") or "").strip()
        inst = str(d.get("instrument_number") or "").strip()
        if inst == "0":          # BCAD shows 0 for old deeds recorded by volume/page only
            inst = ""
        if (date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)) or \
           (inst and not re.fullmatch(r"\d{1,20}", inst)):
            skipped.append({"row": n, "reason": "bad deed_date or instrument_number"})
            continue
        clean = lambda k, w=120: str(d.get(k) or "").strip()[:w]
        conn.execute(
            """INSERT INTO property_deeds
                 (property_id, bcad_property_id, deed_date, deed_type, deed_type_desc,
                  grantor, grantee, volume, page, instrument_number, source, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(property_id, deed_date, instrument_number, grantor, grantee)
               DO UPDATE SET bcad_property_id=excluded.bcad_property_id,
                 deed_type=excluded.deed_type, deed_type_desc=excluded.deed_type_desc,
                 volume=excluded.volume, page=excluded.page,
                 source=excluded.source, fetched_at=excluded.fetched_at""",
            (prop["id"], bcad, date, clean("deed_type", 10), clean("deed_type_desc"),
             clean("grantor"), clean("grantee"), clean("volume", 12), clean("page", 12),
             inst, source, now),
        )
        saved += 1
    if saved:
        log_event(conn, prop["id"], "deeds_updated", None, f"{saved} deed rows")
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "deeds_saved": saved, "skipped": skipped})


# Monthly lists (/api/v1/lists) - see lists.py
from lists import register as register_lists, summaries as list_summaries  # noqa: E402
register_lists(app, query_properties, require_api_key, get_conn)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": dt.datetime.utcnow().isoformat()})


@app.route("/internal/sync", methods=["POST"])
def internal_sync():
    """Triggers a data sync. Protected by a shared secret so this can be
    called by a scheduler (e.g. a GitHub Actions cron) without exposing an
    open endpoint that anyone could hammer. Set REIX_SYNC_SECRET in your
    hosting platform's environment variables, and the same value as a
    'REIX_SYNC_SECRET' repo secret in GitHub -- see DEPLOY.md."""
    import os
    expected = os.environ.get("REIX_SYNC_SECRET")
    provided = request.headers.get("X-Sync-Secret")
    if not expected or provided != expected:
        return jsonify({"error": "unauthorized"}), 401

    from scraper import sync_once
    county = request.args.get("county")
    result = sync_once(county)
    return jsonify({"status": "sync complete", **result})


# --------------------------------------------------------------------------
# Static app (list / map / search UI) + docs page
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/docs")
def docs():
    return send_from_directory("static", "docs.html")


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
