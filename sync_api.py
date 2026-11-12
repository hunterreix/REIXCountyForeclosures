"""
sync_api.py - How the REIX app (app.reix.co) stays in step with us.

THE SCENARIO THIS SUPPORTS
  Day 1: a notice arrives; REIX app is given the property (with its stable property_key).
  Day 2: a person uploads the notice and deed of trust (and any additional documents) for it.
  Day 2+: REIX app learns about it from the change feed, fetches the documents for that
          same property_key, and pairs them with the property it already holds.

ENDPOINTS (all need an API key in the X-API-Key header)
  GET /api/v1/changes?after=<cursor>&limit=100     what changed since the cursor      (any key)
  GET /api/v1/properties?property_key=<key>        one property, by its stable key    (any key)
  GET /api/v1/properties/<key>/documents           documents + extracted facts        (key with 'documents' scope)
  GET /api/v1/files/<file_id>                      the PDF itself                     (key with 'documents' scope)

PDFs and the facts read from them are NOT available to ordinary self-serve keys. An admin grants
the 'documents' scope to a specific key with POST /internal/api-keys/scopes.
"""
import datetime as dt
import hmac
import json
import os

from flask import Blueprint, jsonify, request, send_file

from db import get_conn
from events import changes_after, parse_key, property_key
from intake import DOCS_DIR, ROLES, ROLE_LABELS

sync_bp = Blueprint("sync_api", __name__)


# ------------------------------------------------------------------ small helpers

def _key(scope=None, header_only=False):
    """Validate the API key. Returns (row, None) or (None, error_response)."""
    key = request.headers.get("X-API-Key") or ("" if header_only else request.args.get("api_key", ""))
    if not key:
        return None, (jsonify({"error": "Missing API key (X-API-Key header)"}), 401)
    conn = get_conn()
    row = conn.execute("SELECT * FROM api_keys WHERE api_key = ? AND active = 1", (key,)).fetchone()
    if row:
        conn.execute("UPDATE api_keys SET last_used_at = ?, request_count = request_count + 1 WHERE id = ?",
                     (dt.datetime.utcnow().isoformat(), row["id"]))
        conn.commit()
    conn.close()
    if not row:
        return None, (jsonify({"error": "Invalid or inactive API key"}), 401)
    if scope and scope not in (row["scopes"] or "").split(","):
        return None, (jsonify({"error": f"this API key does not have the '{scope}' permission"}), 403)
    return row, None


def _secret_ok():
    expected = os.environ.get("REIX_SYNC_SECRET")
    return bool(expected) and hmac.compare_digest(request.headers.get("X-Sync-Secret", ""), expected)


def attach_status(conn, items):
    """Adds property_key and docs_status (notice / deed_of_trust -> missing | done |
    none_found) to each property. Safe to show to any signed-in user: no names, no files."""
    ids = [i["id"] for i in items]
    for i in items:
        i["property_key"] = property_key(i["county"], i["type"], i["doc_number"])
        i["docs_status"] = {r: "missing" for r in ROLES}
        i["docs_extra"] = 0                      # optional additional documents on file
    if ids:
        marks = ",".join("?" * len(ids))
        by_id = {i["id"]: i for i in items}
        for r in conn.execute(f"SELECT property_id, doc_role, status FROM property_files "
                              f"WHERE property_id IN ({marks}) AND status != 'removed'", ids).fetchall():
            if r["doc_role"] not in ROLES:       # additional documents are counted, not part of the status
                if r["status"] == "uploaded":
                    by_id[r["property_id"]]["docs_extra"] += 1
                continue
            st = by_id[r["property_id"]]["docs_status"]
            if r["status"] == "uploaded":
                st[r["doc_role"]] = "done"
            elif r["status"] == "none_found" and st[r["doc_role"]] != "done":
                st[r["doc_role"]] = "none_found"
    for i in items:
        s = i["docs_status"]
        i["docs_complete"] = s["notice"] == "done" and s["deed_of_trust"] == "done"
    return items


def _property_by_key(conn, key):
    parsed = parse_key(key)
    if not parsed:
        return None
    county, layer, doc = parsed
    return conn.execute("SELECT id, county, source_layer, doc_number FROM properties "
                        "WHERE LOWER(county) = ? AND source_layer = ? AND doc_number = ?", (county, layer, doc)).fetchone()


# ---------------------------------------------------------------- partner endpoints

@sync_bp.route("/api/v1/changes")
def changes():
    row, err = _key()
    if err:
        return err
    try:
        after = max(0, int(request.args.get("after", 0)))
        limit = max(1, min(int(request.args.get("limit", 100)), 500))
    except ValueError:
        return jsonify({"error": "after and limit must be numbers"}), 400
    conn = get_conn()
    events, more, latest = changes_after(conn, after, limit)
    conn.close()
    return jsonify({"events": events, "next_after": events[-1]["id"] if events else after,
                    "has_more": more, "latest_id": latest})


@sync_bp.route("/api/v1/properties/<key>/documents")
def property_documents(key):
    row, err = _key("documents", header_only=True)
    if err:
        return err
    conn = get_conn()
    prop = _property_by_key(conn, key)
    if not prop:
        conn.close()
        return jsonify({"error": "unknown property_key"}), 404
    files = conn.execute(
        """SELECT id, doc_role, doc_subtype, status, doc_number, stamp_matches, fields_json, size_bytes,
                  sha256, note, uploaded_at FROM property_files
           WHERE property_id = ? AND status IN ('uploaded', 'none_found') ORDER BY id""", (prop["id"],)).fetchall()
    conn.close()
    out, status = [], {r: "missing" for r in ROLES}
    for f in files:
        try:
            fields = json.loads(f["fields_json"] or "{}")
        except ValueError:
            fields = {}
        if f["doc_role"] in ROLES:               # additional documents don't change the required-document status
            if f["status"] == "uploaded":
                status[f["doc_role"]] = "done"
            elif status[f["doc_role"]] != "done":
                status[f["doc_role"]] = "none_found"
        out.append({"file_id": f["id"], "role": f["doc_role"], "role_label": ROLE_LABELS[f["doc_role"]],
                    "subtype": f["doc_subtype"] or None, "status": f["status"], "doc_number": f["doc_number"] or None,
                    "number_matches_expected": None if f["stamp_matches"] is None else bool(f["stamp_matches"]),
                    "facts": fields, "size_bytes": f["size_bytes"], "sha256": f["sha256"] or None,
                    "note": f["note"] or None, "uploaded_at": f["uploaded_at"],
                    "download_url": f"/api/v1/files/{f['id']}" if f["status"] == "uploaded" else None})
    return jsonify({"property_key": property_key(prop["county"], prop["source_layer"], prop["doc_number"]),
                    "docs_status": status, "files": out})


@sync_bp.route("/api/v1/files/<int:fid>")
def download_file(fid):
    row, err = _key("documents", header_only=True)
    if err:
        return err
    conn = get_conn()
    f = conn.execute("SELECT stored_path, original_name FROM property_files WHERE id = ? AND status = 'uploaded'", (fid,)).fetchone()
    conn.close()
    if not f or not f["stored_path"]:
        return jsonify({"error": "not found"}), 404
    path = os.path.realpath(os.path.join(DOCS_DIR, f["stored_path"]))
    if not path.startswith(os.path.realpath(DOCS_DIR) + os.sep) or not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    resp = send_file(path, mimetype="application/pdf", download_name=f["original_name"] or "document.pdf")
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ------------------------------------------------------------------ admin endpoints

@sync_bp.route("/internal/api-keys")
def list_keys():
    """Admin: list API keys (the secret part is hidden)."""
    if not _secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    conn = get_conn()
    rows = conn.execute("SELECT id, api_key, name, email, created_at, last_used_at, request_count, active, scopes "
                        "FROM api_keys ORDER BY id").fetchall()
    conn.close()
    return jsonify({"keys": [{**{k: r[k] for k in r.keys() if k != "api_key"}, "key_preview": r["api_key"][:10] + "..."} for r in rows]})


@sync_bp.route("/internal/api-keys/scopes", methods=["POST"])
def set_scopes():
    """Admin: set what a key may do. Body: {"id": 3, "scopes": ["documents"], "active": true}"""
    if not _secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    b = request.get_json(silent=True) or {}
    scopes = b.get("scopes", [])
    scopes = [scopes] if isinstance(scopes, str) else scopes
    if not isinstance(b.get("id"), int) or any(s not in ("documents",) for s in scopes):
        return jsonify({"error": "id (int) and scopes (only 'documents' is available) required"}), 400
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM api_keys WHERE id = ?", (b["id"],)).fetchone():
        conn.close()
        return jsonify({"error": "unknown key id"}), 404
    conn.execute("UPDATE api_keys SET scopes = ? WHERE id = ?", (",".join(scopes), b["id"]))
    if b.get("active") is not None:
        conn.execute("UPDATE api_keys SET active = ? WHERE id = ?", (1 if b["active"] else 0, b["id"]))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "id": b["id"], "scopes": scopes})
