"""
lists.py - Monthly lists: the structure used to hand notices to other data providers.

A MONTHLY LIST is every notice for one SALE month, named YYYY-MM (for example 2026-11).

How a notice gets into a list:
  confirmed    its notice document was processed, so we know the real sale date (properties.sale_date)
  provisional  we only know the county's filing cycle (year + month). That is usually, but not always,
               the sale month. When the notice is processed and its sale date lands in another month, the
               notice moves to that month's list and a `sale_date_set` event is published on the change feed.

Endpoints (API key required):
  GET /api/v1/lists                    the available months, with counts
  GET /api/v1/lists/<YYYY-MM>          one month's notices (JSON, paged)
  GET /api/v1/lists/<YYYY-MM>?format=csv   the whole month as a CSV file
"""
import csv
import io

from flask import Response, jsonify, request

# SQL expression: the list a notice belongs to (confirmed sale month, else the county's filing cycle)
MONTH_EXPR = "COALESCE(substr(sale_date, 1, 7), printf('%04d-%02d', year, month))"

_HAVE = ("EXISTS (SELECT 1 FROM property_files f WHERE f.property_id = properties.id "
         "AND f.doc_role = '{r}' AND f.status IN ({s}))")
DOCS_COMPLETE = " AND ".join([_HAVE.format(r="notice", s="'uploaded'"),
                              _HAVE.format(r="deed_of_trust", s="'uploaded'"),
                              _HAVE.format(r="loan_doc", s="'uploaded','none_found'")])

MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]

ABOUT = ("A list is every notice for one sale month. 'confirmed' notices have a real sale_date read from the notice "
         "itself. 'provisional' notices are placed by the county's filing cycle until the notice is processed; if the "
         "real sale date is in another month the notice moves, and a sale_date_set event is published on /api/v1/changes.")


def valid_month(m):
    return (isinstance(m, str) and len(m) == 7 and m[4] == "-" and m[:4].isdigit() and m[5:].isdigit()
            and 1 <= int(m[5:]) <= 12)


def label(m):
    return f"{MONTH_NAMES[int(m[5:]) - 1]} {m[:4]}" if valid_month(m) else m


def summaries(conn, county="", ltype="", only_month=None):
    where = ["(sale_date IS NOT NULL OR (year IS NOT NULL AND month IS NOT NULL))"]
    params = []
    if county:
        where.append("county = ?"); params.append(county)
    if ltype in ("mortgage", "tax"):
        where.append("source_layer = ?"); params.append(ltype)
    rows = conn.execute(
        f"""SELECT {MONTH_EXPR} AS lm, COUNT(*) AS total,
                   SUM(CASE WHEN sale_date IS NOT NULL THEN 1 ELSE 0 END) AS confirmed,
                   SUM(CASE WHEN source_layer = 'mortgage' THEN 1 ELSE 0 END) AS mortgage,
                   SUM(CASE WHEN source_layer = 'tax' THEN 1 ELSE 0 END) AS tax,
                   SUM(CASE WHEN {DOCS_COMPLETE} THEN 1 ELSE 0 END) AS docs_complete,
                   MIN(sale_date) AS first_sale_date, MAX(sale_date) AS last_sale_date
            FROM properties WHERE {' AND '.join(where)} GROUP BY lm ORDER BY lm DESC""", params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["month"] = d.pop("lm")      # (not named "month" in the SQL: that is also a real column and would win in GROUP BY)
        if not valid_month(d["month"]) or (only_month and d["month"] != only_month):
            continue
        d["label"] = label(d["month"])
        d["provisional"] = d["total"] - d["confirmed"]
        d["basis"] = "confirmed" if d["provisional"] == 0 else ("provisional" if d["confirmed"] == 0 else "mixed")
        out.append(d)
    return out


CSV_COLUMNS = ["list_month", "list_basis", "sale_date", "property_key", "county", "type", "notice_number",
               "address", "city", "zip", "lat", "lon", "notice_link", "docs_notice", "docs_deed_of_trust",
               "docs_loan_doc", "docs_complete", "docs_extra"]


def register(app, query_properties, require_api_key, get_conn):
    """Adds the list routes to the app (passed in to avoid a circular import)."""

    @app.route("/api/v1/lists")
    def v1_lists():
        _, err = require_api_key()
        if err:
            return err
        conn = get_conn()
        lists = summaries(conn, request.args.get("county", "").strip(), request.args.get("type", "").strip().lower())
        conn.close()
        return jsonify({"lists": lists, "total_lists": len(lists), "about": ABOUT})

    @app.route("/api/v1/lists/<month>")
    def v1_list(month):
        _, err = require_api_key()
        if err:
            return err
        if not valid_month(month):
            return jsonify({"error": "month must look like 2026-11"}), 400
        args = request.args.to_dict()
        args["list_month"] = month
        county, ltype = args.get("county", "").strip(), args.get("type", "").strip().lower()
        conn = get_conn()
        head = summaries(conn, county, ltype, only_month=month)
        conn.close()
        head = head[0] if head else {"month": month, "label": label(month), "total": 0, "confirmed": 0, "provisional": 0,
                                     "mortgage": 0, "tax": 0, "docs_complete": 0, "first_sale_date": None,
                                     "last_sale_date": None, "basis": "confirmed"}
        if args.get("format", "").lower() == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(CSV_COLUMNS)
            page = 1
            while True:
                data = query_properties(dict(args, page=str(page), per_page="1000"))
                for p in data["results"]:
                    s = p.get("docs_status", {})
                    w.writerow([p["list_month"], p["list_basis"], p.get("sale_date") or "", p["property_key"], p["county"],
                                p["type"], p["doc_number"], p["address"], p["city"], p["zip"], p.get("lat") or "",
                                p.get("lon") or "", p.get("doc_link") or "", s.get("notice"), s.get("deed_of_trust"),
                                s.get("loan_doc"), p.get("docs_complete"), p.get("docs_extra", 0)])
                if page >= data["total_pages"] or page >= 100:
                    break
                page += 1
            return Response(buf.getvalue(), mimetype="text/csv",
                            headers={"Content-Disposition": f"attachment; filename=foreclosure-list-{month}.csv"})
        data = query_properties(args)
        data["list"] = head
        return jsonify(data)
