"""
intake.py - Human document-intake tool for foreclosure notices.

A logged-in "intake user" works through a list of notices, opens each document on
the County Clerk's site, downloads the PDF, and uploads it here. The PDF is read
in the USER'S BROWSER (OCR) -- nothing is sent to a third party -- and the
extracted facts are shown for the person to confirm before saving. The tool then
guides them to the next document in the chain:

    1. notice  ->  2. deed_of_trust (named in the notice)

These two are the REQUIRED documents. Everything else (assignments, notes, liens, releases, HOA or tax
papers, title work...) is an OPTIONAL "additional document": any number, never required, and reachable at
any time, before or after the required ones are in.

ACCOUNTS AND ROLES: everyone who uses the site signs in. Roles:
    viewer  - can use the foreclosure list, map and summary (and create an API key)
    intake  - viewer + the document-intake tool below
    admin   - intake + (reserved for future admin screens)
Accounts are created by an admin with POST /internal/intake-users.

PDFs are stored on disk (REIX_DOCS_DIR, default: a "docs" folder next to the
database, i.e. the Render persistent disk) and are only served to logged-in
intake users. They are never exposed through the public API.
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import time
from functools import wraps

from flask import Blueprint, current_app, jsonify, request, send_file, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

from db import DB_PATH, get_conn
from events import ensure_schema as ensure_events, log_event

intake_bp = Blueprint("intake", __name__)

DOCS_DIR = os.environ.get("REIX_DOCS_DIR", os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "docs"))
MAX_PDF_BYTES = 25 * 1024 * 1024
ROLES = ("notice", "deed_of_trust")   # the required documents
ROLE_LABELS = {"notice": "Foreclosure notice", "deed_of_trust": "Deed of Trust", "loan_doc": "Loan document",
               "other_doc": "Additional document", "tax_doc": "Tax document"}
EXTRA_ROLE = "other_doc"   # optional extra documents: any number, never required for completion
TAX_ROLE = "tax_doc"       # optional tax documents (a back tax report, tax liens): their own category, never required
# Additional documents are chosen from this list (the "document name"). The old free-form types are still
# accepted on documents that already have one, so existing data keeps working until someone edits it.
DOC_NAMES = (("additional_deed_of_trust", "Additional Deed of Trust"), ("modification", "Modification"),
             ("lis_pendens", "Lis Pendens"), ("judgment", "Judgment"), ("tax_lien", "Tax Lien"),
             ("other_lien", "Other Lien"), ("affidavit", "Affidavit"), ("notice", "Notice"),
             ("city_lien", "City Lien"), ("back_tax_report", "Back Tax Report"), ("irs_lien", "IRS Lien"),
             ("federal_tax_lien", "Federal Tax Lien"))
DOC_NAME_LABELS = dict(DOC_NAMES)
TAX_NAME_CODES = ("back_tax_report", "tax_lien", "irs_lien", "federal_tax_lien")        # these live in the Tax documents category
EXTRA_NAME_CODES = tuple(c for c, _ in DOC_NAMES if c not in TAX_NAME_CODES)             # the rest stay in Additional documents
LEGACY_SUBTYPES = ("assignment", "note", "lien", "release", "other")
SUBTYPES = tuple(c for c, _ in DOC_NAMES) + LEGACY_SUBTYPES
LIEN_POSITIONS = {"first": "1st lien", "subordinate": "Subordinate lien"}   # for the deed of trust
# The address of a document's page on the County Clerk's site (a permanent link back to the courthouse record)
COURTHOUSE_URL_RE = re.compile(r"https://bexar\.tx\.publicsearch\.us/doc/\d+(?:[/?#]\S*)?")

CLERK_HOME = "https://bexar.tx.publicsearch.us/"
BEXAR_NOTICE_LINK = ("https://bexar.tx.publicsearch.us/results?department=FC"
                     "&searchOcrText=false&searchType=quickSearch&searchValue={doc}")

# Which fields we accept per document type (anything else is dropped).
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
FIELD_RULES = {
    "notice": {"stamp_doc": "num", "stamp_date": "date", "deed_instrument": "num", "deed_recorded": "date",
               "deed_dated": "date", "borrower": "text", "lender": "text", "principal": "text", "sale_date": "date",
               "clerk_url": "url"},
    "deed_of_trust": {"stamp_doc": "num", "stamp_date": "date", "dated": "date", "borrower": "text",
                      "lender": "text", "trustee": "text", "loan_number": "text", "maturity_date": "date",
                      "principal": "text", "lien_position": "lien", "clerk_url": "url"},   # trustee = "DOT Lender Trustee"; maturity_date = loan end / payoff date
    "other_doc": {"stamp_doc": "num", "stamp_date": "date", "title": "text", "assignor": "text", "assignee": "text",
                  "references": "num", "principal": "text", "amount": "money", "clerk_url": "url", "notes": "text"},
    "tax_doc": {"stamp_doc": "num", "stamp_date": "date", "title": "text", "assignor": "text", "amount": "money",
                "clerk_url": "url", "notes": "text"},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'viewer',   -- viewer | intake | admin
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS property_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id   INTEGER NOT NULL,
    doc_role      TEXT NOT NULL,                 -- notice | deed_of_trust | loan_doc
    doc_subtype   TEXT NOT NULL DEFAULT '',      -- loan_doc: assignment | note | lien | release | other
    status        TEXT NOT NULL DEFAULT 'uploaded',  -- uploaded | none_found | removed
    doc_number    TEXT NOT NULL DEFAULT '',
    stamp_matches INTEGER,                       -- 1 / 0 / NULL (unknown)
    fields_json   TEXT NOT NULL DEFAULT '{}',
    stored_path   TEXT NOT NULL DEFAULT '',
    original_name TEXT NOT NULL DEFAULT '',
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    sha256        TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    uploaded_by   INTEGER,
    uploaded_at   TEXT NOT NULL,
    removed_by    INTEGER,
    removed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_pfiles_property ON property_files(property_id, doc_role);
CREATE TABLE IF NOT EXISTS intake_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    user_id     INTEGER,
    property_id INTEGER,
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT ''
);
"""

_FAILS = {}  # (ip, email) -> [count, first_ts]   (simple login throttle)


def _now():
    return dt.datetime.utcnow().isoformat()


def _client_ip():
    """The caller's address. Behind Render's proxy the real client is the LAST entry of
    X-Forwarded-For (earlier entries can be forged by the caller)."""
    xff = request.headers.get("X-Forwarded-For", "")
    return xff.split(",")[-1].strip() if xff else (request.remote_addr or "")


def _migrate(conn):
    """Add columns that older databases don't have yet (safe to run every start)."""
    def cols(t):
        return [r["name"] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]
    if "role" not in cols("intake_users"):
        conn.execute("ALTER TABLE intake_users ADD COLUMN role TEXT NOT NULL DEFAULT 'viewer'")
    if "scopes" not in cols("api_keys"):   # e.g. 'documents' lets a key download PDFs
        conn.execute("ALTER TABLE api_keys ADD COLUMN scopes TEXT NOT NULL DEFAULT ''")
    if "sale_date" not in cols("properties"):   # the real sale date, read from the notice (YYYY-MM-DD)
        for stmt in ("ALTER TABLE properties ADD COLUMN sale_date TEXT", "ALTER TABLE properties ADD COLUMN sale_date_source TEXT"):
            try:
                conn.execute(stmt)
            except Exception:       # several server processes start at once: another one just added it
                pass


def _gate_public():
    """The list, map and summary data require a signed-in user."""
    if request.path.startswith("/api/public/") and request.method != "OPTIONS" and not _user():
        return jsonify({"error": "login required"}), 401


def init_intake(app):
    """Call once from app.py. Sets up sessions, upload limits, tables, storage."""
    key = os.environ.get("REIX_SESSION_SECRET", "")
    if not key and os.environ.get("REIX_SYNC_SECRET"):
        # derive a stable key so every gunicorn worker agrees, with no new setting
        key = hmac.new(os.environ["REIX_SYNC_SECRET"].encode(), b"reix-intake-session", hashlib.sha256).hexdigest()
    if key:
        app.secret_key = key
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=not os.environ.get("REIX_INSECURE_COOKIES"),
        PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=12),
        MAX_CONTENT_LENGTH=MAX_PDF_BYTES + 1024 * 1024,
    )
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate(conn)
    ensure_events(conn)
    _backfill_sale_dates(conn)
    _migrate_loan_docs(conn)
    _migrate_tax_docs(conn)
    conn.commit()
    conn.close()
    app.before_request(_gate_public)
    try:
        os.makedirs(DOCS_DIR, exist_ok=True)
    except OSError:
        pass  # surfaced in /intake/api/stats and on upload


# ----------------------------------------------------------------- auth helpers

def _user():
    uid = session.get("uid")
    if not uid:
        return None
    conn = get_conn()
    u = conn.execute("SELECT id, email, name, role FROM intake_users WHERE id = ? AND active = 1", (uid,)).fetchone()
    conn.close()
    return u


def _guard(roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            u = _user()
            if not u:
                return jsonify({"error": "login required"}), 401
            if roles and u["role"] not in roles:
                return jsonify({"error": "your account does not have access to this"}), 403
            if request.method not in ("GET", "HEAD"):
                sent, want = request.headers.get("X-CSRF-Token", ""), session.get("csrf", "")
                if not want or not hmac.compare_digest(sent, want):
                    return jsonify({"error": "bad CSRF token - reload the page"}), 403
            request.intake_user = u
            return fn(*a, **kw)
        return wrapper
    return deco


def require_user(fn):      # any signed-in account
    return _guard(None)(fn)


def require_intake(fn):    # intake staff and admins only
    return _guard(("intake", "admin"))(fn)


def _log(user_id, property_id, action, detail=""):
    conn = get_conn()
    conn.execute("INSERT INTO intake_log (at, user_id, property_id, action, detail) VALUES (?,?,?,?,?)",
                 (_now(), user_id, property_id, action, detail[:300]))
    conn.commit()
    conn.close()


def _secret_ok():
    expected = os.environ.get("REIX_SYNC_SECRET")
    return bool(expected) and hmac.compare_digest(request.headers.get("X-Sync-Secret", ""), expected)


# --------------------------------------------------------------------- pages/auth

@intake_bp.route("/intake")
def intake_page():
    return send_from_directory(current_app.static_folder, "intake.html")


@intake_bp.route("/intake/api/login", methods=["POST"])
@intake_bp.route("/auth/login", methods=["POST"])
def login():
    if not current_app.secret_key:
        return jsonify({"error": "intake is not configured on this server"}), 503
    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    key = (_client_ip(), email)
    count, first = _FAILS.get(key, [0, time.time()])
    if time.time() - first > 600:
        count, first = 0, time.time()
    if count >= 5:
        return jsonify({"error": "too many attempts - try again in 10 minutes"}), 429
    conn = get_conn()
    u = conn.execute("SELECT * FROM intake_users WHERE email = ? AND active = 1", (email,)).fetchone()
    conn.close()
    if not u or not check_password_hash(u["password_hash"], password):
        _FAILS[key] = [count + 1, first]
        return jsonify({"error": "wrong email or password"}), 401
    _FAILS.pop(key, None)
    # Signing in again as the same person (for example in a second tab) keeps the same security token,
    # so other open tabs don't suddenly fail with "bad CSRF token".
    keep = session.get("csrf") if session.get("uid") == u["id"] else None
    session.clear()
    session.permanent = True
    session["uid"] = u["id"]
    session["csrf"] = keep or secrets.token_hex(16)
    _log(u["id"], None, "login")
    return jsonify({"user": {"email": u["email"], "name": u["name"], "role": u["role"]}, "csrf": session["csrf"]})


@intake_bp.route("/intake/api/me")
@intake_bp.route("/auth/me")
def me():
    u = _user()
    if not u:
        return jsonify({"user": None}), 200
    return jsonify({"user": {"email": u["email"], "name": u["name"], "role": u["role"]}, "csrf": session.get("csrf")})


@intake_bp.route("/intake/api/logout", methods=["POST"])
@intake_bp.route("/auth/logout", methods=["POST"])
@require_user
def logout():
    session.clear()
    return jsonify({"status": "ok"})


@intake_bp.route("/internal/intake-users", methods=["POST"])
def manage_users():
    """Create an intake user or reset a password (secret-protected, for the admin).
    Body: {"email": "...", "name": "...", "password": "10+ chars", "role": "viewer|intake|admin", "active": true}"""
    if not _secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    b = request.get_json(silent=True) or {}
    email = str(b.get("email") or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify({"error": "valid email required"}), 400
    conn = get_conn()
    row = conn.execute("SELECT id FROM intake_users WHERE email = ?", (email,)).fetchone()
    pw = str(b.get("password") or "")
    if pw and len(pw) < 10:
        conn.close()
        return jsonify({"error": "password must be at least 10 characters"}), 400
    if not row and not pw:
        conn.close()
        return jsonify({"error": "password required for a new user"}), 400
    active = 0 if b.get("active") is False else 1
    role = b.get("role")
    if role is not None and role not in ("viewer", "intake", "admin"):
        conn.close()
        return jsonify({"error": "role must be viewer, intake or admin"}), 400
    if row:
        if role:
            conn.execute("UPDATE intake_users SET role = ? WHERE id = ?", (role, row["id"]))
        if pw:
            conn.execute("UPDATE intake_users SET password_hash = ? WHERE id = ?", (generate_password_hash(pw), row["id"]))
        if b.get("name"):
            conn.execute("UPDATE intake_users SET name = ? WHERE id = ?", (str(b["name"])[:80], row["id"]))
        conn.execute("UPDATE intake_users SET active = ? WHERE id = ?", (active, row["id"]))
        result = "updated"
    else:
        conn.execute("INSERT INTO intake_users (email, name, password_hash, role, active, created_at) VALUES (?,?,?,?,?,?)",
                     (email, str(b.get("name") or "")[:80], generate_password_hash(pw), role or "viewer", active, _now()))
        result = "created"
    conn.commit()
    stored = conn.execute("SELECT role FROM intake_users WHERE email = ?", (email,)).fetchone()["role"]
    conn.close()
    return jsonify({"status": result, "email": email, "role": stored})


# --------------------------------------------------------------- work list / state

def _file_rows(conn, ids):
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT f.id, f.property_id, f.doc_role, f.doc_subtype, f.status, f.doc_number, f.stamp_matches,
                   f.fields_json, f.original_name, f.size_bytes, f.note, f.uploaded_at, u.name AS by_name, u.email AS by_email
            FROM property_files f LEFT JOIN intake_users u ON u.id = f.uploaded_by
            WHERE f.property_id IN ({marks}) AND f.status != 'removed' ORDER BY f.id""", ids).fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        try:
            d["fields"] = json.loads(d.pop("fields_json") or "{}")
        except ValueError:
            d["fields"] = {}
        d["by"] = d.pop("by_name") or d.pop("by_email") or ""
        d["courthouse_url"] = courthouse_url(d["doc_role"], d["doc_number"], d["fields"])
        d.pop("by_email", None)
        out.setdefault(d["property_id"], []).append(d)
    return out


def _role_state(files, role):
    fs = [f for f in files if f["doc_role"] == role]
    if any(f["status"] == "uploaded" for f in fs):
        return "done"
    if any(f["status"] == "none_found" for f in fs):
        return "none_found"
    return "missing"


def _latest_fields(files, role):
    for f in reversed(files):
        if f["doc_role"] == role and f["status"] == "uploaded":
            return f["fields"]
    return {}


def _sync_sale_date(conn, pid):
    """Keep properties.sale_date equal to the sale date on the property's current (uploaded) notice.
    A date more than 6 months away from the county's filing cycle is almost certainly a typo and is ignored.
    When the date changes, publish a sale_date_set event that names the monthly list the notice is now in.
    Returns True if something changed."""
    row = conn.execute("SELECT year, month, sale_date FROM properties WHERE id = ?", (pid,)).fetchone()
    if not row:
        return False
    new = _latest_fields(_file_rows(conn, [pid]).get(pid, []), "notice").get("sale_date") or None
    if new and row["year"] and row["month"]:
        if abs((int(new[:4]) * 12 + int(new[5:7])) - (row["year"] * 12 + row["month"])) > 6:
            new = None
    old = row["sale_date"]
    if new == old:
        return False
    cur = conn.execute("UPDATE properties SET sale_date = ?, sale_date_source = ? "
                       "WHERE id = ? AND COALESCE(sale_date, '') = COALESCE(?, '')", (new, "notice" if new else None, pid, old))
    if cur.rowcount != 1:      # another worker already made this change
        return False
    cycle = f"{row['year']:04d}-{row['month']:02d}" if row["year"] and row["month"] else None
    log_event(conn, pid, "sale_date_set", None, json.dumps({"sale_date": new, "list_month": new[:7] if new else cycle,
                                                           "previous_list_month": old[:7] if old else cycle}))
    return True


def _backfill_sale_dates(conn):
    """At startup: give every property that already has a processed notice its sale date."""
    for r in conn.execute("SELECT DISTINCT property_id FROM property_files WHERE doc_role = 'notice' AND status = 'uploaded'").fetchall():
        _sync_sale_date(conn, r["property_id"])


def _migrate_loan_docs(conn):
    """The loan document used to be a third required step. It is now just another additional document:
    existing loan documents become additional documents (keeping their type and details, with a title),
    and old 'none found' marks are retired. Idempotent: once converted, nothing is left to change."""
    conn.execute("UPDATE property_files SET status = 'removed', removed_at = ? WHERE doc_role = 'loan_doc' AND status = 'none_found'", (_now(),))
    for r in conn.execute("SELECT id, property_id, doc_subtype, fields_json FROM property_files "
                          "WHERE doc_role = 'loan_doc' AND status = 'uploaded'").fetchall():
        try:
            f = json.loads(r["fields_json"] or "{}")
        except ValueError:
            f = {}
        f = _clean_fields("other_doc", f)
        if not f.get("title"):
            f["title"] = "Loan document" + (f" ({r['doc_subtype']})" if r["doc_subtype"] else "")
        cur = conn.execute("UPDATE property_files SET doc_role = 'other_doc', fields_json = ? WHERE id = ? AND doc_role = 'loan_doc'",
                           (json.dumps(f), r["id"]))
        if cur.rowcount == 1:      # (another server process may have just done it)
            log_event(conn, r["property_id"], "document_updated", r["id"], "other_doc")


def _migrate_tax_docs(conn):
    """Tax documents became their own category: a back tax report or tax lien saved as an additional document moves
    over (its details are kept as they are). Idempotent."""
    marks = ",".join("?" * len(TAX_NAME_CODES))
    for r in conn.execute(f"SELECT id, property_id FROM property_files WHERE doc_role = 'other_doc' AND doc_subtype IN ({marks})",
                          TAX_NAME_CODES).fetchall():
        cur = conn.execute("UPDATE property_files SET doc_role = 'tax_doc' WHERE id = ? AND doc_role = 'other_doc'", (r["id"],))
        if cur.rowcount == 1:
            log_event(conn, r["property_id"], "document_updated", r["id"], "tax_doc")


def build_guidance(prop, files):
    """Which document is next, and exactly what to look for."""
    states = {r: _role_state(files, r) for r in ROLES}
    current = next((r for r in ROLES if states[r] == "missing"), None)
    extras = [f for f in files if f["doc_role"] == EXTRA_ROLE and f["status"] == "uploaded"]
    steps = [{"role": r, "label": ROLE_LABELS[r], "state": states[r]} for r in ROLES]
    steps.append({"role": EXTRA_ROLE, "label": "Additional documents", "state": "optional", "count": len(extras)})
    taxdocs = [f for f in files if f["doc_role"] == TAX_ROLE and f["status"] == "uploaded"]
    steps.append({"role": TAX_ROLE, "label": "Tax documents", "state": "optional", "count": len(taxdocs)})
    resolved = sum([states["notice"] == "done", states["deed_of_trust"] == "done"])
    g = {"steps": steps, "current": current, "extras": len(extras), "tax_docs": len(taxdocs), "complete": _complete(files),
         "progress": {"done": resolved, "total": len(ROLES), "percent": round(100 * resolved / len(ROLES))}}
    nf = _latest_fields(files, "notice")
    df = _latest_fields(files, "deed_of_trust")
    if current == "notice":
        g.update(title="Upload the foreclosure notice",
                 lookup={"doc_number": prop["doc_number"], "department": "Foreclosures",
                         "open_url": BEXAR_NOTICE_LINK.format(doc=prop["doc_number"])},
                 instructions=["Open the notice on the Clerk's site and open it in the viewer.",
                               "Download or print the notice as a PDF and upload it here."])
    elif current == "deed_of_trust":
        inst = nf.get("deed_instrument", "")
        g.update(title="Find the Deed of Trust named in the notice",
                 lookup={"doc_number": inst, "department": "Land Records", "recorded_date": nf.get("deed_recorded", ""),
                         "borrower": nf.get("borrower", ""), "lender": nf.get("lender", ""), "open_url": CLERK_HOME},
                 instructions=(["Open the Clerk's site, choose the Land Records department, and search for the document number below."]
                               if inst else ["The deed of trust number was not read from the notice. Open the notice (section "
                                              "'Type of Sale') and type the number into the notice's fields, or search by the borrower's name."]) +
                              ["Confirm the result is a DEED OF TRUST recorded on the date shown, then download it and upload it here.",
                               "On the form, choose whether it is a 1st lien or a Subordinate lien, and paste the address of the "
                               "document's page on the Clerk's site so anyone can link back to it.",
                               "If it cannot be found, use 'Not found' and add a note."])
    elif g["complete"]:
        g.update(title="All required documents are in", lookup={},
                 instructions=["Everything required (the notice and the deed of trust) is on file. Add any other documents you find "
                               "(modifications, liens, judgments, affidavits...) as an 'Additional document', and a back tax report or tax lien as a 'Tax document'."])
    else:
        g.update(title="The deed of trust was marked not found", lookup={},
                 instructions=["The notice and deed of trust are needed to complete a property. Re-check the Clerk's site, "
                               "and upload the deed of trust when you find it."])
    return g


@intake_bp.route("/intake/api/stats")
@require_intake
def stats():
    conn = get_conn()
    base = "FROM properties p WHERE p.county='Bexar' AND p.source_layer='mortgage'"
    total = conn.execute(f"SELECT COUNT(*) c {base}").fetchone()["c"]

    def has(role, statuses):
        q = ",".join("'%s'" % s for s in statuses)
        return conn.execute(f"SELECT COUNT(*) c {base} AND EXISTS (SELECT 1 FROM property_files f WHERE f.property_id=p.id "
                            f"AND f.doc_role='{role}' AND f.status IN ({q}))").fetchone()["c"]
    n, d = has("notice", ["uploaded"]), has("deed_of_trust", ["uploaded"])
    complete = conn.execute(
        f"""SELECT COUNT(*) c {base}
            AND EXISTS (SELECT 1 FROM property_files f WHERE f.property_id=p.id AND f.doc_role='notice' AND f.status='uploaded')
            AND EXISTS (SELECT 1 FROM property_files f WHERE f.property_id=p.id AND f.doc_role='deed_of_trust' AND f.status='uploaded')
         """).fetchone()["c"]
    used = conn.execute("SELECT COALESCE(SUM(size_bytes),0) s FROM property_files WHERE status != 'removed'").fetchone()["s"]
    conn.close()
    disk = {}
    try:
        du = shutil.disk_usage(DOCS_DIR)
        disk = {"free_mb": round(du.free / 1e6), "total_mb": round(du.total / 1e6)}
    except OSError:
        disk = {"error": "storage folder not available"}
    return jsonify({"total": total, "with_notice": n, "with_deed": d, "complete": complete,
                    "stored_mb": round(used / 1e6, 1), "disk": disk})


def _worklist_parts(a):
    """The work-list filters as SQL: (inner select, params, condition for the missing-documents filter)."""
    where, params = ["p.county='Bexar'", "p.source_layer='mortgage'"], []
    lm = "COALESCE(substr(p.sale_date, 1, 7), printf('%04d-%02d', p.year, p.month))"   # the monthly list
    if str(a.get("year", "")).isdigit():
        where.append(f"substr({lm}, 1, 4) = ?")
        params.append(f"{int(a['year']):04d}")
    if str(a.get("month", "")).isdigit():
        where.append(f"substr({lm}, 6, 2) = ?")
        params.append(f"{int(a['month']):02d}")
    q = (a.get("q") or "").strip()
    if q:
        where.append("(p.address LIKE ? OR p.doc_number LIKE ? OR p.zip LIKE ?)")
        params += [f"%{q}%"] * 3
    cond = {"notice": "n_notice = 0", "deed_of_trust": "n_deed = 0",
            "complete": "n_notice > 0 AND n_deed > 0",
            "any": "(n_notice = 0 OR n_deed = 0)",
            "minimum": "(n_notice = 0 OR n_deed = 0)"}.get(a.get("missing", "any"), "1=1")
    inner = f"""SELECT p.id, p.doc_number, p.address, p.city, p.zip, p.year, p.month, p.sale_date,
        (SELECT COUNT(*) FROM property_files f WHERE f.property_id=p.id AND f.doc_role='notice' AND f.status='uploaded') AS n_notice,
        (SELECT COUNT(*) FROM property_files f WHERE f.property_id=p.id AND f.doc_role='deed_of_trust' AND f.status='uploaded') AS n_deed,
        (SELECT COUNT(*) FROM property_files f WHERE f.property_id=p.id AND f.doc_role='deed_of_trust' AND f.status='none_found') AS deed_none,
        (SELECT COUNT(*) FROM property_files f WHERE f.property_id=p.id AND f.doc_role='other_doc' AND f.status='uploaded') AS n_extra
        FROM properties p WHERE {' AND '.join(where)}"""
    return inner, params, cond


@intake_bp.route("/intake/api/worklist")
@require_intake
def worklist():
    a = request.args
    inner, params, cond = _worklist_parts(a)
    try:
        page = max(1, int(a.get("page", 1)))
    except ValueError:
        page = 1
    per = 50
    conn = get_conn()
    total = conn.execute(f"SELECT COUNT(*) c FROM ({inner}) WHERE {cond}", params).fetchone()["c"]
    rows = conn.execute(f"SELECT * FROM ({inner}) WHERE {cond} ORDER BY year DESC, month DESC, id DESC LIMIT ? OFFSET ?",
                        params + [per, (page - 1) * per]).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["notice_state"] = "done" if d["n_notice"] else "missing"
        d["deed_state"] = "done" if d["n_deed"] else "missing"
        d["extras"] = d["n_extra"]
        d["progress"] = round(100 * (bool(d["n_notice"]) + bool(d["n_deed"])) / 2)
        d["next"] = "notice" if not d["n_notice"] else "deed_of_trust" if not d["n_deed"] else None
        d["notice_link"] = BEXAR_NOTICE_LINK.format(doc=d["doc_number"])
        d["list_month"] = d["sale_date"][:7] if d["sale_date"] else (f"{d['year']:04d}-{d['month']:02d}" if d["year"] and d["month"] else "")
        out.append(d)
    return jsonify({"results": out, "total": total, "page": page, "per_page": per})


def _property(conn, pid):
    return conn.execute("SELECT id, doc_number, address, city, zip, year, month, sale_date FROM properties "
                        "WHERE id = ? AND county='Bexar' AND source_layer='mortgage'", (pid,)).fetchone()


def _detail(conn, pid):
    prop = _property(conn, pid)
    if not prop:
        return None
    files = _file_rows(conn, [pid]).get(pid, [])
    return {"property": dict(prop), "files": files, "guidance": build_guidance(dict(prop), files)}


@intake_bp.route("/intake/api/property/<int:pid>")
@require_intake
def property_detail(pid):
    conn = get_conn()
    d = _detail(conn, pid)
    conn.close()
    return (jsonify(d), 200) if d else (jsonify({"error": "not found"}), 404)


# ------------------------------------------------------------------ write actions

def _clean_fields(role, raw):
    out = {}
    rules = FIELD_RULES[role]
    if not isinstance(raw, dict):
        return out
    for k, kind in rules.items():
        v = str(raw.get(k) or "").strip()[:200]
        if not v:
            continue
        if kind == "num" and not re.fullmatch(r"\d{1,20}", v):
            continue
        if kind == "date" and not DATE_RE.fullmatch(v):
            continue
        if kind == "lien" and v not in LIEN_POSITIONS:
            continue
        if kind == "url" and not COURTHOUSE_URL_RE.fullmatch(v):
            continue
        if kind == "money":
            v = v.replace("$", "").replace(" ", "")
            if not re.fullmatch(r"(\d{1,3}(,\d{3})+|\d+)(\.\d{1,2})?", v):
                continue
        out[k] = v
    return out


def courthouse_url(role, doc_number, fields):
    """Where to review the document at the courthouse: the page address staff pasted, or (for the notice, whose
    number is the property's own) the Clerk's foreclosure search."""
    u = (fields or {}).get("clerk_url")
    if u:
        return u
    if role == "notice" and doc_number:
        return BEXAR_NOTICE_LINK.format(doc=doc_number)
    return None


def _validate_document(role, subtype, fields, notice_number):
    """The details every document must have. Returns an error message, or None when fine. Fills in what is
    automatic: a notice's own number, and an additional document's title (its document name)."""
    if role == "notice":
        fields.setdefault("stamp_doc", notice_number)         # the notice's own number is already known
    elif role == "deed_of_trust":
        if "lien_position" not in fields:
            return "choose the lien position: 1st lien or Subordinate lien"
        if not fields.get("stamp_doc"):
            return "enter the deed of trust's document number (the number stamped on it)"
    elif role in (EXTRA_ROLE, TAX_ROLE):
        if subtype not in (TAX_NAME_CODES if role == TAX_ROLE else EXTRA_NAME_CODES):
            return "choose the document name from the list"
        fields["title"] = DOC_NAME_LABELS[subtype]
        if subtype == "back_tax_report":                      # no document number: the amount owed is the data point
            if not fields.get("amount"):
                return "enter the amount owed shown on the back tax report"
        elif not fields.get("stamp_doc"):
            return "enter the document number (the number stamped on the document)"
    return None


@intake_bp.route("/intake/api/property/<int:pid>/upload", methods=["POST"])
@require_intake
def upload(pid):
    role = request.form.get("doc_role", "")
    if role not in ROLES + (EXTRA_ROLE, TAX_ROLE):
        return jsonify({"error": "unknown document type"}), 400
    subtype = request.form.get("doc_subtype", "") if role in (EXTRA_ROLE, TAX_ROLE) else ""      # an additional document's type is optional
    if subtype and subtype not in SUBTYPES:
        return jsonify({"error": "unknown document type"}), 400
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    data = f.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        return jsonify({"error": "file is larger than 25 MB"}), 413
    if not data.startswith(b"%PDF-"):
        return jsonify({"error": "that file is not a PDF"}), 400
    try:
        raw_fields = json.loads(request.form.get("fields") or "{}")
    except ValueError:
        raw_fields = {}
    fields = _clean_fields(role, raw_fields)

    conn = get_conn()
    prop = _property(conn, pid)
    if not prop:
        conn.close()
        return jsonify({"error": "not found"}), 404
    err = _validate_document(role, subtype, fields, prop["doc_number"])
    if err:
        conn.close()
        return jsonify({"error": err}), 400
    files = _file_rows(conn, [pid]).get(pid, [])
    expected = ""
    if role == "notice":
        expected = prop["doc_number"]
    elif role == "deed_of_trust":
        expected = _latest_fields(files, "notice").get("deed_instrument", "")
    stamp = fields.get("stamp_doc", "")
    matches = None if not (expected and stamp) else int(stamp == expected)

    sha = hashlib.sha256(data).hexdigest()
    if conn.execute("SELECT 1 FROM property_files WHERE property_id=? AND sha256=? AND status='uploaded'", (pid, sha)).fetchone():
        conn.close()
        return jsonify({"error": "this exact file was already uploaded for this property"}), 409

    folder = os.path.join(DOCS_DIR, str(pid))
    name = f"{role}-{sha[:10]}-{secrets.token_hex(4)}.pdf"
    try:
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), "wb") as out:
            out.write(data)
    except OSError:
        conn.close()
        return jsonify({"error": "could not save the file - the storage disk may be full"}), 507

    uid = request.intake_user["id"]
    replaced = [r["id"] for r in conn.execute(
        "SELECT id FROM property_files WHERE property_id=? AND doc_role=? AND status='uploaded'", (pid, role)).fetchall()]
    if role in ("notice", "deed_of_trust"):  # one current document for these; the older one is kept but retired
        conn.execute("UPDATE property_files SET status='removed', removed_by=?, removed_at=? "
                     "WHERE property_id=? AND doc_role=? AND status IN ('uploaded','none_found')", (uid, _now(), pid, role))
    cur = conn.execute(
        """INSERT INTO property_files (property_id, doc_role, doc_subtype, status, doc_number, stamp_matches, fields_json,
                                       stored_path, original_name, size_bytes, sha256, note, uploaded_by, uploaded_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, role, subtype, "uploaded", stamp, matches, json.dumps(fields), os.path.join(str(pid), name),
         (f.filename or "document.pdf")[:120], len(data), sha, "", uid, _now()))
    if role in ("notice", "deed_of_trust"):
        for rid in replaced:                       # a replaced document is reported as removed
            log_event(conn, pid, "document_removed", rid, role)
    log_event(conn, pid, "document_added", cur.lastrowid, role)
    if role == "notice":
        _sync_sale_date(conn, pid)
    conn.commit()
    d = _detail(conn, pid)
    conn.close()
    _log(uid, pid, "upload", f"{role} {stamp} match={matches}")
    d["stamp_matches"] = matches
    return jsonify(d)


@intake_bp.route("/intake/api/property/<int:pid>/none-found", methods=["POST"])
@require_intake
def none_found(pid):
    b = request.get_json(silent=True) or {}
    role = b.get("doc_role")
    if role != "deed_of_trust":
        return jsonify({"error": "only the deed of trust can be marked not found"}), 400
    note = str(b.get("note") or "").strip()[:300]
    conn = get_conn()
    if not _property(conn, pid):
        conn.close()
        return jsonify({"error": "not found"}), 404
    uid = request.intake_user["id"]
    conn.execute("UPDATE property_files SET status='removed', removed_by=?, removed_at=? "
                 "WHERE property_id=? AND doc_role=? AND status='none_found'", (uid, _now(), pid, role))
    cur = conn.execute("INSERT INTO property_files (property_id, doc_role, status, note, uploaded_by, uploaded_at) VALUES (?,?,?,?,?,?)",
                       (pid, role, "none_found", note, uid, _now()))
    log_event(conn, pid, "document_none_found", cur.lastrowid, role)
    conn.commit()
    d = _detail(conn, pid)
    conn.close()
    _log(uid, pid, "none_found", f"{role}: {note}")
    return jsonify(d)


@intake_bp.route("/intake/api/file/<int:fid>/remove", methods=["POST"])
@require_intake
def remove_file(fid):
    conn = get_conn()
    row = conn.execute("SELECT id, property_id, doc_role FROM property_files WHERE id=? AND status != 'removed'", (fid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    uid = request.intake_user["id"]
    conn.execute("UPDATE property_files SET status='removed', removed_by=?, removed_at=? WHERE id=?", (uid, _now(), fid))
    log_event(conn, row["property_id"], "document_removed", fid, row["doc_role"])
    if row["doc_role"] == "notice":
        _sync_sale_date(conn, row["property_id"])
    conn.commit()
    d = _detail(conn, row["property_id"])
    conn.close()
    _log(uid, row["property_id"], "remove", f"file {fid} ({row['doc_role']})")  # the PDF stays on disk for the audit trail
    return jsonify(d)


@intake_bp.route("/intake/api/file/<int:fid>")
@require_intake
def get_file(fid):
    conn = get_conn()
    row = conn.execute("SELECT stored_path, original_name FROM property_files WHERE id=? AND status='uploaded'", (fid,)).fetchone()
    conn.close()
    if not row or not row["stored_path"]:
        return jsonify({"error": "not found"}), 404
    path = os.path.realpath(os.path.join(DOCS_DIR, row["stored_path"]))
    if not path.startswith(os.path.realpath(DOCS_DIR) + os.sep) or not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    resp = send_file(path, mimetype="application/pdf", download_name=row["original_name"] or "document.pdf")
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ------------------------------------------------------------------ documents shown in the search list
# Signed-in users see a "Documents" button on a property once its REQUIRED documents are in
# (the notice and the deed of trust). Staff can open any uploaded file.
FACT_KEYS = {
    "notice": ("stamp_doc", "stamp_date", "sale_date", "deed_instrument"),
    "deed_of_trust": ("stamp_doc", "stamp_date", "dated", "borrower", "lender", "trustee",
                      "loan_number", "maturity_date", "principal", "lien_position"),
    "other_doc": ("stamp_doc", "stamp_date", "title", "assignor", "assignee", "references", "principal", "amount"),
    "tax_doc": ("stamp_doc", "stamp_date", "title", "assignor", "amount"),
}


def _complete(files):
    return _role_state(files, "notice") == "done" and _role_state(files, "deed_of_trust") == "done"


def attach_viewer_docs(conn, items):
    """Adds doc_files (PDF links) and doc_facts to each property whose documents are complete."""
    for i in items:
        i["doc_files"], i["doc_facts"] = [], None
    ids = [i["id"] for i in items if i.get("docs_complete")]
    if not ids:
        return items
    files_by = _file_rows(conn, ids)
    for i in items:
        if i["id"] not in ids:
            continue
        facts = {"notice": {}, "deed_of_trust": {}, "other_docs": [], "tax_docs": []}
        for f in files_by.get(i["id"], []):
            if f["status"] != "uploaded":
                continue
            i["doc_files"].append({"id": f["id"], "role": f["doc_role"], "label": ROLE_LABELS[f["doc_role"]],
                                   "subtype": f["doc_subtype"] or None, "uploaded_at": f["uploaded_at"],
                                   "title": f["fields"].get("title") or None,
                                   "number": f["fields"].get("stamp_doc") or None,
                                   "courthouse_url": f.get("courthouse_url"),
                                   "url": f"/api/public/files/{f['id']}"})
            picked = {k: v for k, v in f["fields"].items() if k in FACT_KEYS[f["doc_role"]]}
            if f["doc_role"] == EXTRA_ROLE:
                picked["type"] = f["doc_subtype"] or ""
                facts["other_docs"].append(picked)
            elif f["doc_role"] == TAX_ROLE:
                picked["type"] = f["doc_subtype"] or ""
                facts["tax_docs"].append(picked)
            else:
                facts[f["doc_role"]] = picked
        i["doc_facts"] = facts
    return items


@intake_bp.route("/api/public/files/<int:fid>")
def public_file(fid):
    u = _user()
    if not u:
        return jsonify({"error": "login required"}), 401
    conn = get_conn()
    row = conn.execute("SELECT property_id, stored_path, original_name FROM property_files "
                       "WHERE id = ? AND status = 'uploaded'", (fid,)).fetchone()
    if row and u["role"] not in ("intake", "admin"):
        # everyone else may only open the files of properties whose documents are all in
        if not _complete(_file_rows(conn, [row["property_id"]]).get(row["property_id"], [])):
            row = None
    conn.close()
    if not row or not row["stored_path"]:
        return jsonify({"error": "not available"}), 404
    path = os.path.realpath(os.path.join(DOCS_DIR, row["stored_path"]))
    if not path.startswith(os.path.realpath(DOCS_DIR) + os.sep) or not os.path.isfile(path):
        return jsonify({"error": "not available"}), 404
    resp = send_file(path, mimetype="application/pdf", download_name=row["original_name"] or "document.pdf")
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ------------------------------------------------------- next property, and editing a saved document's details
@intake_bp.route("/intake/api/property/<int:pid>/next")
@require_intake
def next_property(pid):
    """The next property after this one in the work list (same filters, same order). missing=minimum means
    'still lacks the notice or the deed of trust'."""
    inner, params, cond = _worklist_parts(request.args)
    conn = get_conn()
    cur = conn.execute("SELECT id, year, month FROM properties WHERE id = ?", (pid,)).fetchone()
    if not cur:
        conn.close()
        return jsonify({"error": "not found"}), 404
    y, m = cur["year"] or 0, cur["month"] or 0
    row = conn.execute(
        f"""SELECT id FROM ({inner}) WHERE ({cond}) AND
              (COALESCE(year, 0) < ? OR (COALESCE(year, 0) = ? AND (COALESCE(month, 0) < ? OR (COALESCE(month, 0) = ? AND id < ?))))
            ORDER BY COALESCE(year, 0) DESC, COALESCE(month, 0) DESC, id DESC LIMIT 1""",
        params + [y, y, m, m, cur["id"]]).fetchone()
    conn.close()
    return jsonify({"next_id": row["id"] if row else None})


def _recheck_stamps(conn, pid):
    """After details change, re-compare the number stamped on the notice and deed with the number each should have."""
    prop = _property(conn, pid)
    files = _file_rows(conn, [pid]).get(pid, [])
    expected = {"notice": prop["doc_number"], "deed_of_trust": _latest_fields(files, "notice").get("deed_instrument", "")}
    for f in files:
        if f["status"] != "uploaded" or f["doc_role"] not in expected:
            continue
        stamp, want = f["fields"].get("stamp_doc") or "", expected[f["doc_role"]]
        conn.execute("UPDATE property_files SET stamp_matches = ? WHERE id = ?", (None if not (want and stamp) else int(stamp == want), f["id"]))


@intake_bp.route("/intake/api/file/<int:fid>/fields", methods=["POST"])
@require_intake
def update_fields(fid):
    """Edit the details read from a saved document. The PDF itself is untouched. Body:
    {"fields": {...}, "doc_subtype": "assignment"}   (doc_subtype only for additional documents)"""
    b = request.get_json(silent=True) or {}
    conn = get_conn()
    row = conn.execute("SELECT id, property_id, doc_role, doc_subtype, fields_json FROM property_files WHERE id = ? AND status = 'uploaded'", (fid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    pid, role = row["property_id"], row["doc_role"]
    fields = _clean_fields(role, b.get("fields"))
    subtype = row["doc_subtype"]
    if role in (EXTRA_ROLE, TAX_ROLE):         # an extra document's name must come from its list
        subtype = str(b.get("doc_subtype", subtype) or "")
        if subtype and subtype not in SUBTYPES:
            conn.close()
            return jsonify({"error": "unknown document type"}), 400
    err = _validate_document(role, subtype, fields, _property(conn, pid)["doc_number"])
    if err:
        conn.close()
        return jsonify({"error": err}), 400
    if role in (EXTRA_ROLE, TAX_ROLE):         # keep an old free-form title as a note rather than losing it
        try:
            old_title = (json.loads(row["fields_json"] or "{}").get("title") or "").strip()
        except ValueError:
            old_title = ""
        if old_title and old_title not in DOC_NAME_LABELS.values() and not fields.get("notes"):
            fields["notes"] = old_title[:200]
    conn.execute("UPDATE property_files SET fields_json = ?, doc_subtype = ?, doc_number = ? WHERE id = ?",
                 (json.dumps(fields), subtype, fields.get("stamp_doc", ""), fid))
    _recheck_stamps(conn, pid)
    if role == "notice":
        _sync_sale_date(conn, pid)           # a corrected sale date can move the property to another monthly list
    log_event(conn, pid, "document_updated", fid, role)
    conn.commit()
    d = _detail(conn, pid)
    conn.close()
    _log(request.intake_user["id"], pid, "edit_details", f"file {fid} ({role})")
    return jsonify(d)
