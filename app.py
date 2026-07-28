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


def row_to_dict(row):
    d = dict(row)
    d["full_address"] = build_full_address(d.get("address"), d.get("city"), d.get("zip"))
    d["reix_link"] = REIX_URL
    return d


def build_filters(args):
    """Shared query-building logic for both public and v1 endpoints."""
    clauses = []
    params = []

    county = args.get("county", "").strip()
    if county:
        clauses.append("county = ?")
        params.append(county)

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


def query_properties(args):
    conn = get_conn()
    where, params = build_filters(args)
    page, per_page = paginate_args(args)
    offset = (page - 1) * per_page

    total = conn.execute(f"SELECT COUNT(*) AS c FROM properties {where}", params).fetchone()["c"]

    rows = conn.execute(
        f"""SELECT id, county, source_layer AS type, doc_number, address, city, zip,
                   school_dist, year, month, lat, lon, doc_link, first_seen, last_seen
            FROM properties {where}
            ORDER BY year DESC, month DESC, id DESC
            LIMIT ? OFFSET ?""",
        params + [per_page, offset],
    ).fetchall()
    conn.close()

    return {
        "results": [row_to_dict(r) for r in rows],
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
        """SELECT year, month, COUNT(*) AS c FROM properties
           WHERE year IS NOT NULL AND month IS NOT NULL
           GROUP BY year, month ORDER BY year DESC, month DESC LIMIT 12"""
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
    conn.close()
    return {"types": ["mortgage", "tax"], "cities": cities, "counties": counties}


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
    return jsonify(query_properties(request.args))


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
def create_key():
    """Instant self-serve API key. No approval queue, on purpose -- we want
    this to be the easiest possible way for an app to plug into our feed."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()[:120]
    email = (body.get("email") or "").strip()[:200]

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
                  school_dist, year, month, lat, lon, doc_link, first_seen, last_seen
           FROM properties WHERE id = ?""",
        (property_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(row_to_dict(row))


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
