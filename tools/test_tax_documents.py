import csv, io, json
import db
import app as A
import intake
from events import log_event

def ok(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else "   " + str(extra))); assert cond, label

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF"
S = {"X-Sync-Secret": "s3cretvalue"}; anon = A.app.test_client()
conn = db.get_conn(); ids = {}
for name, doc in [("A", "20261100001"), ("B", "20261100002"), ("C", "20261100003")]:
    cur = conn.execute("INSERT INTO properties (county,source_layer,doc_number,address,city,zip,year,month,first_seen,last_seen) VALUES ('Bexar','mortgage',?,?,'SAN ANTONIO','78228',2026,11,'x','x')", (doc, f"{name} MAIN ST"))
    ids[name] = cur.lastrowid; log_event(conn, cur.lastrowid, "property_added")
conn.commit(); conn.close()
for email, role in (("viewer@x.co", "viewer"), ("worker@reix.co", "intake")):
    anon.post("/internal/intake-users", json={"email": email, "name": role, "password": "longpassword1", "role": role}, headers=S)
def login(email):
    c = A.app.test_client(); r = c.post("/auth/login", json={"email": email, "password": "longpassword1"}).get_json(); return c, {"X-CSRF-Token": r["csrf"]}
v, vh = login("viewer@x.co"); w, wh = login("worker@reix.co")
n = [0]
def up(pid, role, fields, sub=""):
    n[0] += 1
    return w.post(f"/intake/api/property/{pid}/upload", headers=wh, data={"doc_role": role, "doc_subtype": sub, "fields": json.dumps(fields), "file": (io.BytesIO(PDF + b"\n%" + str(n[0]).encode()), "d.pdf")}, content_type="multipart/form-data")
detail = lambda pid: w.get(f"/intake/api/property/{pid}").get_json()
files = lambda pid: [f for f in detail(pid)["files"] if f["status"] == "uploaded"]
err = lambda r: (r.get_json() or {}).get("error", "")
A_, B_, C_ = ids["A"], ids["B"], ids["C"]
edit = lambda fid, fields, **kw: w.post(f"/intake/api/file/{fid}/fields", headers=wh, json={"fields": fields, **kw})

# ============ the category exists, and the four steps are in order
g = detail(A_)["guidance"]
ok("the property has four document steps: notice, deed of trust, additional documents, tax documents", [s["role"] for s in g["steps"]] == ["notice", "deed_of_trust", "other_doc", "tax_doc"] and [s["label"] for s in g["steps"]][2:] == ["Additional documents", "Tax documents"], g["steps"])
ok("...the last two are optional and count what has been added", g["steps"][3]["state"] == "optional" and g["steps"][3]["count"] == 0 and g["tax_docs"] == 0 and g["extras"] == 0)
ok("the category has its label", intake.ROLE_LABELS["tax_doc"] == "Tax document" and intake.TAX_ROLE == "tax_doc")
ok("the twelve names are split: four tax names, eight additional", set(intake.TAX_NAME_CODES) == {"back_tax_report", "tax_lien", "irs_lien", "federal_tax_lien"} and len(intake.EXTRA_NAME_CODES) == 8 and not (set(intake.TAX_NAME_CODES) & set(intake.EXTRA_NAME_CODES)))

# ============ each name belongs to one category only
ok("a tax name cannot be filed as an additional document", all(up(A_, "other_doc", {"stamp_doc": "20200000001", "amount": "5"}, sub=t).status_code == 400 and "document name" in err(up(A_, "other_doc", {"stamp_doc": "20200000001"}, sub=t)) for t in intake.TAX_NAME_CODES))
ok("an additional name cannot be filed as a tax document", all(up(A_, "tax_doc", {"stamp_doc": "20200000001"}, sub=x).status_code == 400 for x in intake.EXTRA_NAME_CODES))
ok("a tax document must have a name", up(A_, "tax_doc", {"stamp_doc": "20200000001"}, sub="").status_code == 400 and up(A_, "tax_doc", {"stamp_doc": "20200000001"}, sub="assignment").status_code == 400)
ok("nothing was saved by the refused attempts", not files(A_))

# ============ tax documents: the requirements
ok("a tax lien needs its document number", up(A_, "tax_doc", {"amount": "900"}, sub="tax_lien").status_code == 400 and "document number" in err(up(A_, "tax_doc", {}, sub="irs_lien")))
ok("a back tax report needs the amount owed (and no number)", up(A_, "tax_doc", {"stamp_doc": "20200000009"}, sub="back_tax_report").status_code == 400 and up(A_, "tax_doc", {"amount": "$12,345.67"}, sub="back_tax_report").status_code == 200)
ok("tax lien, IRS lien and federal tax lien save with a number", all(up(A_, "tax_doc", {"stamp_doc": f"2020000010{i}", "amount": "1,000", "assignor": "Internal Revenue Service", "notes": "private"}, sub=t).status_code == 200 for i, t in enumerate(["tax_lien", "irs_lien", "federal_tax_lien"])))
td = {f["doc_subtype"]: f for f in files(A_) if f["doc_role"] == "tax_doc"}
ok("each is stored as a tax document, titled with its name, keeping its amount and the taxing authority", sorted(td) == ["back_tax_report", "federal_tax_lien", "irs_lien", "tax_lien"] and td["irs_lien"]["fields"]["title"] == "IRS Lien" and td["irs_lien"]["fields"]["assignor"] == "Internal Revenue Service" and td["back_tax_report"]["fields"]["amount"] == "12,345.67", td)
ok("fields a tax document does not use are dropped (no 'assignee', no 'references')", up(A_, "tax_doc", {"stamp_doc": "20200000555", "assignee": "X", "references": "20190000001"}, sub="tax_lien").status_code == 200 and "assignee" not in [f for f in files(A_) if f["doc_number"] == "20200000555"][0]["fields"])
g = detail(A_)["guidance"]
ok("the tax step counts them; additional documents are not affected; completion is not affected", g["steps"][3]["count"] == 5 and g["tax_docs"] == 5 and g["extras"] == 0 and g["complete"] is False and g["progress"]["percent"] == 0)
up(A_, "other_doc", {"stamp_doc": "20200077777"}, sub="judgment")
ok("an additional document counts separately", detail(A_)["guidance"]["extras"] == 1 and detail(A_)["guidance"]["steps"][3]["count"] == 5)

# ============ editing
bt = td["back_tax_report"]
ok("a tax document's amount can be edited", edit(bt["id"], {"amount": "15,000.00"}, doc_subtype="back_tax_report").status_code == 200 and [f for f in files(A_) if f["id"] == bt["id"]][0]["fields"]["amount"] == "15,000.00")
ok("...a back tax report cannot lose its amount", edit(bt["id"], {"notes": "x"}, doc_subtype="back_tax_report").status_code == 400)
ok("...a tax document can be renamed to another tax name, but not to an additional name", edit(td["tax_lien"]["id"], {"stamp_doc": "20200000100"}, doc_subtype="irs_lien").status_code == 200 and edit(td["tax_lien"]["id"], {"stamp_doc": "20200000100"}, doc_subtype="judgment").status_code == 400)
oid = [f for f in files(A_) if f["doc_role"] == "other_doc"][0]["id"]
ok("...and an additional document cannot be renamed to a tax name", edit(oid, {"stamp_doc": "20200077777"}, doc_subtype="tax_lien").status_code == 400)

# ============ migration: tax names saved as additional documents move over
c = db.get_conn()
for sub, flds in (("back_tax_report", {"title": "Back Tax Report", "amount": "7,000.00"}), ("tax_lien", {"title": "Tax Lien", "stamp_doc": "20190000777", "assignee": "keep me"}), ("irs_lien", {"title": "IRS Lien", "stamp_doc": "20190000778"}), ("affidavit", {"title": "Affidavit", "stamp_doc": "20190000779"})):
    c.execute("INSERT INTO property_files (property_id, doc_role, doc_subtype, status, fields_json, stored_path, original_name, uploaded_at) VALUES (?,?,?,?,?,?,?,?)", (B_, "other_doc", sub, "uploaded", json.dumps(flds), "x.pdf", "x.pdf", "2026-11-01"))
c.commit()
before = c.execute("SELECT COUNT(*) c FROM property_events WHERE event_type = 'document_updated'").fetchone()["c"]
intake._migrate_tax_docs(c); c.commit()
rows = {r["doc_subtype"]: dict(r) for r in c.execute("SELECT doc_role, doc_subtype, fields_json FROM property_files WHERE property_id = ?", (B_,)).fetchall()}
ok("existing back tax reports and tax liens move to the Tax documents category", rows["back_tax_report"]["doc_role"] == "tax_doc" and rows["tax_lien"]["doc_role"] == "tax_doc" and rows["irs_lien"]["doc_role"] == "tax_doc")
ok("...an affidavit stays an additional document", rows["affidavit"]["doc_role"] == "other_doc")
ok("...their details are kept exactly as they were", json.loads(rows["tax_lien"]["fields_json"]) == {"title": "Tax Lien", "stamp_doc": "20190000777", "assignee": "keep me"} and json.loads(rows["back_tax_report"]["fields_json"])["amount"] == "7,000.00")
ok("...each move is announced on the change feed (document_updated)", c.execute("SELECT COUNT(*) c FROM property_events WHERE event_type = 'document_updated'").fetchone()["c"] == before + 3)
intake._migrate_tax_docs(c); c.commit()
ok("running it again changes nothing", c.execute("SELECT COUNT(*) c FROM property_events WHERE event_type = 'document_updated'").fetchone()["c"] == before + 3)
c.close()

# ============ API, CSV, viewers
up(C_, "notice", {"stamp_doc": "20261100003"}); up(C_, "deed_of_trust", {"stamp_doc": "20190100003", "lien_position": "first"})
up(C_, "tax_doc", {"amount": "2,500.00", "notes": "internal"}, sub="back_tax_report"); up(C_, "tax_doc", {"stamp_doc": "20200000300", "amount": "900", "clerk_url": "https://bexar.tx.publicsearch.us/doc/300"}, sub="federal_tax_lien")
KH = {"X-API-Key": v.post("/api/v1/keys", json={"name": "REIX"}, headers=vh).get_json()["api_key"]}
KID = anon.get("/internal/api-keys", headers=S).get_json()["keys"][0]["id"]; anon.post("/internal/api-keys/scopes", json={"id": KID, "scopes": ["documents"]}, headers=S)
get = lambda doc: anon.get("/api/v1/properties", headers=KH, query_string={"property_key": f"bexar-mortgage-{doc}"}).get_json()["results"][0]
pc, pa = get("20261100003"), get("20261100001")
ok("the API counts tax documents separately from additional documents", pc["docs_tax"] == 2 and pc["docs_extra"] == 0 and pa["docs_tax"] == 5 and pa["docs_extra"] == 1 and pa["docs_complete"] is False, (pc["docs_tax"], pa["docs_tax"], pa["docs_extra"]))
ok("...the back tax amount owed comes from the tax document", pc["back_tax_amount_owed"] == "2,500.00" and pa["back_tax_amount_owed"] == "15,000.00")
ok("...required-document status is untouched by either", pc["docs_status"] == {"notice": "done", "deed_of_trust": "done"} and pc["docs_complete"] is True)
docs = anon.get("/api/v1/properties/bexar-mortgage-20261100003/documents", headers=KH).get_json()["files"]
tdocs = [d for d in docs if d["role"] == "tax_doc"]
ok("the documents endpoint lists them with role tax_doc, their name and a courthouse link", len(tdocs) == 2 and {d["document_name"] for d in tdocs} == {"Back Tax Report", "Federal Tax Lien"} and [d for d in tdocs if d["subtype"] == "federal_tax_lien"][0]["courthouse_url"].endswith("/doc/300") and [d for d in tdocs if d["subtype"] == "federal_tax_lien"][0]["facts"]["amount"] == "900")
rows = {r["notice_number"]: r for r in csv.DictReader(io.StringIO(anon.get("/api/v1/lists/2026-11?format=csv", headers=KH).data.decode()))}
ok("the monthly CSV has a docs_tax column next to docs_extra", rows["20261100003"]["docs_tax"] == "2" and rows["20261100001"]["docs_tax"] == "5" and list(rows["20261100003"]).index("docs_tax") == list(rows["20261100003"]).index("docs_extra") + 1)
pv = {p["doc_number"]: p for p in v.get("/api/public/properties?per_page=50").get_json()["results"]}
vc = pv["20261100003"]
ok("signed-in viewers see tax documents on a finished property, with amounts and no internal notes", sorted(d["role"] for d in vc["doc_files"]) == ["deed_of_trust", "notice", "tax_doc", "tax_doc"] and sorted(t.get("amount") for t in vc["doc_facts"]["tax_docs"]) == ["2,500.00", "900"] and "internal" not in json.dumps(vc["doc_facts"]), vc["doc_facts"])
ok("...the facts group them as tax_docs, apart from other_docs", vc["doc_facts"]["other_docs"] == [] and {t["title"] for t in vc["doc_facts"]["tax_docs"]} == {"Back Tax Report", "Federal Tax Lien"})
ok("...and an unfinished property shows nothing", pv["20261100001"]["doc_files"] == [] and pv["20261100001"]["doc_facts"] is None)
fid = [d for d in vc["doc_files"] if d["role"] == "tax_doc"][0]["id"]; fid_a = td["irs_lien"]["id"]
ok("viewers can open a tax PDF on a finished property but not on an unfinished one", v.get(f"/api/public/files/{fid}").status_code == 200 and v.get(f"/api/public/files/{fid_a}").status_code in (403, 404))
ok("staff can open any", w.get(f"/intake/api/file/{fid_a}").status_code == 200)
print("\nALL TAX-DOCUMENT CATEGORY TESTS PASSED")
