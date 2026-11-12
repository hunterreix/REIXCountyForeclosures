"""Tests for taxes.py: addresses, page parsing, every outcome of a lookup, endpoints, API output, privacy.
Run from the repository root:   python3 tools/test_taxes.py        (uses a throwaway database in the current folder)"""
import csv
import datetime as dt
import io
import json
import os
import sys
import time

os.environ.update(REIX_SYNC_SECRET="s3cretvalue", REIX_INSECURE_COOKIES="1", TAX_AUTORUN="0", TAX_MIN_INTERVAL="0",
                  TAX_CONTACT="tax-test@example.com")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import fake_tax_site as F          # noqa: E402
import db                          # noqa: E402
import app as A                    # noqa: E402
import taxes as T                  # noqa: E402


def ok(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else "   " + str(extra)))
    assert cond, label


# ============================================================ addresses
terms = T.search_terms
ok("terms: a plain street", terms("727 VENICE") == ["727 VENICE"], terms("727 VENICE"))
ok("terms: street type dropped (St/Dr/Ave/Rd are not typed)", terms("318 BURTON AVE") == ["318 BURTON"] and terms("7108 BANDERA RD") == ["7108 BANDERA"] and terms("1234 MAIN ST") == ["1234 MAIN"])
ok("terms: direction goes in front of the street, with a second try without it", terms("931 W LAUREL") == ["931 W LAUREL", "931 LAUREL"] and terms("1234 N MAIN ST") == ["1234 N MAIN", "1234 MAIN"])
ok("terms: a second street word is cut to its first letter (the site's own example: 567 Tobin G)", terms("567 TOBIN GARDENS") == ["567 TOBIN G"] and terms("100 SAN PEDRO AVE") == ["100 SAN P"])
ok("terms: units, fractions and punctuation are dropped", terms("55 OAK DR APT 4") == ["55 OAK"] and terms("55 OAK DR #12") == ["55 OAK"] and terms("123 1/2 MAIN ST") == ["123 MAIN"] and terms("10 ELM ST., UNIT B") == ["10 ELM"])
ok("terms: numbered roads", terms("400 LOOP 410") == ["400 LOOP 4"])
ok("terms: no house number or empty means no search", terms("ST MARYS ST") == [] and terms("") == [] and terms(None) == [] and terms("MAIN") == [])
ours = T.parse_address("727 VENICE")
sm = lambda a, b: T.same_street(T.parse_address(a), T.parse_address(b))
ok("same street: the suffix does not matter", sm("727 VENICE", "727 VENICE ST") and sm("1306 CULEBRA", "1306 CULEBRA AV") and sm("1306 CULEBRA", "1306 CULEBRA RD"))
ok("same street: a different number or street does not match", not sm("727 VENICE", "7270 VENICE") and not sm("727 VENICE", "727 VENUS") and not sm("727 VENICE", "72 VENICE"))
ok("same street: a direction must agree when both have one", sm("931 W LAUREL", "931 LAUREL") and sm("931 W LAUREL", "931 W LAUREL") and not sm("931 W LAUREL", "931 E LAUREL"))
real = T.is_real_property
ok("real property: accounts with a legal description (NCB / CB / lot / block)", real("096622090170", "NCB 9662 BLK 209 LOT 17") and real("044465010060", "CB 4446E BLK 1 LOT 6") and real("002100070120", "NCB 210 BLK 7 LOT 12 FIRST QUALITYCYLINDERS INC") and real("000010010005", "NCB 1 BLK 1 LOT 5"))
ok("business personal-property accounts are not real property", not real("902043500000", "BERNARD LUMBER CO 1306 CULEBRA AVE") and not real("000001357843", "LAS PRINCESAS 1306 CULEBRA RD INV FURNFIXT EQPT SUP") and not real("921030757650", "UNICORN COMPUTERS001306 00 CULEBRA RDINV FURN FIXT EQUIP SUP") and not real("000001365517", "HOPSCOTCH HEALTH CHILDRENS URGENT CAREPLLC 7108 BANDERA RD"))
ok("...even if the text happens to contain 'LOT'", not real("912026151280", "PARKING LOT MAINTENANCE INC"))

# ============================================================ reading the two pages
res = T.parse_results(F.results_page("7108 BANDERA", F.SEARCH["7108 bandera"]))
ok("results page: every account, with site address and legal description", [r["account"] for r in res] == F.SEARCH["7108 bandera"] and res[3] == {"account": "044465010060", "site_address": "7108 BANDERA RD", "legal": "CB 4446E BLK 1 LOT 6"}, res)
ok("results page: the owner columns are never read", "SECRET" not in json.dumps(res))
ok("results page: a single match", len(T.parse_results(F.results_page("727 VENICE", ["096622090170"]))) == 1)
ok("a search with no result comes back as the search form (no accounts)", T.parse_results(F.search_form_page()) == [] and T.is_search_form(F.search_form_page()) and not T.is_search_form(F.detail_page("096622090170")))
d = T.parse_detail(F.detail_page("096622090170"))
ok("account page: the figures (annual tax, current due, back taxes, total, delinquent-after, last payment)", d["tax_year"] == 2026 and d["annual_levy"] == 4045.80 and d["current_year_due"] == 4045.80 and d["prior_years_due"] == 80125.73 and d["total_due"] == 84171.53 and d["delinquent_after"] == "02/01/2027" and d["last_payment"] == 1412.11 and d["account"] == "096622090170" and d["note"] == "", d)
ok("account page: the owner's name and address are never read", "SECRET" not in json.dumps(d))
d0 = T.parse_detail(F.detail_page("060000020010"))
ok("a property that owes nothing: no 'prior years' line means 0, not missing", d0["prior_years_due"] == 0.0 and d0["total_due"] == 0.0 and d0["current_year_due"] == 0.0 and d0["last_payment"] == 1500.0, d0)
try:
    T.parse_detail(F.detail_page("070000030010")); bad = False
except T.TaxPageChanged:
    bad = True
ok("an unexpected page is reported as 'the page changed', not guessed at", bad)
try:
    T.parse_detail(F.search_form_page()); bad = False
except T.TaxPageChanged:
    bad = True
ok("a search form where an account page should be is also refused", bad)
odd = F.detail_page("096622090170").replace("$84,171.53", "$99,999.99")
ok("figures that do not add up are flagged on the record", "do not add up" in T.parse_detail(odd)["note"])

# ============================================================ one property at a time
conn = db.get_conn()
P, DOC = {}, {}


def addp(key, addr, layer="mortgage", county="Bexar", y=2026, m=11):
    doc = f"2026{len(P) + 1:07d}"
    cur = conn.execute("INSERT INTO properties (county,source_layer,doc_number,address,city,zip,year,month,first_seen,last_seen) VALUES (?,?,?,?,'SAN ANTONIO','78228',?,?,'x','x')", (county, layer, doc, addr, y, m))
    P[key], DOC[key] = cur.lastrowid, doc
    conn.commit()
    return {"id": cur.lastrowid, "address": addr}


class Direct:
    """A tax site with no network: pages straight from the fake site's generators, counting requests."""
    def __init__(self, error=None, block_after=None):
        self.searches, self.pages, self.requests_made, self.error, self.block_after = [], [], 0, error, block_after

    def _tick(self):
        self.requests_made += 1
        if self.block_after is not None and self.requests_made > self.block_after:
            raise T.TaxBlocked("blocked")
        if self.error:
            raise T.TaxError(self.error)

    def search(self, term):
        self._tick()
        self.searches.append(term)
        hits = F.SEARCH.get(term.lower())
        return F.results_page(term, hits) if hits else F.search_form_page()

    def account_page(self, acct):
        self._tick()
        self.pages.append(acct)
        return F.detail_page(acct) if acct in F.ACCOUNTS else F.search_form_page()


ev = lambda pid: conn.execute("SELECT event_type, detail FROM property_events WHERE property_id = ? AND event_type = 'taxes_updated' ORDER BY id", (pid,)).fetchall()
row = lambda pid: conn.execute("SELECT * FROM property_taxes WHERE property_id = ?", (pid,)).fetchone()
prop = {k: addp(k, a) for k, a in [("venice", "727 VENICE"), ("burton", "318 BURTON AVE"), ("laurel", "931 W LAUREL"), ("arbor", "601 ARBOR PL"),
                                    ("culebra", "1306 CULEBRA"), ("bandera", "7108 BANDERA RD"), ("duplex", "500 DUPLEX ST"), ("nowhere", "1 NOWHERE LN"),
                                    ("paidup", "12 PAIDUP DR"), ("weird", "9 WEIRD ST"), ("noaddr", ""), ("taxlayer", "727 VENICE")]}
conn.execute("UPDATE properties SET source_layer = 'tax' WHERE id = ?", (P["taxlayer"],))
conn.commit()

D = Direct()
r = T.check_property(conn, prop["venice"], D)
ok("a one-match address: annual tax, back taxes and total due are read", r["status"] == "ok" and r["account"] == "096622090170" and r["annual"] == 4045.80 and r["back_owed"] == 80125.73 and r["total_due"] == 84171.53 and r["tax_year"] == 2026 and r["delinquent_after"] == "02/01/2027" and r["last_payment"] == 1412.11, r)
ok("...in two requests (the search, then the account page), searching exactly as the site wants it", D.requests_made == 2 and D.searches == ["727 VENICE"] and D.pages == ["096622090170"])
ok("...with a permanent link to the account page and an 'as of' time", r["link"] == "https://bexar.acttax.com/act_webdev/bexar/showdetail2.jsp?can=096622090170" and r["as_of"] and r["manual"] is False)
ok("...and one taxes_updated event announcing the figures", len(ev(P["venice"])) == 1 and json.loads(ev(P["venice"])[0]["detail"]) == {"taxes_annual": 4045.8, "taxes_back_owed": 80125.73, "taxes_total_due": 84171.53})
D2 = Direct(); r2 = T.check_property(conn, prop["venice"], D2)
ok("a refresh of a known account needs ONE request (no search)", D2.requests_made == 1 and D2.searches == [] and r2["status"] == "ok")
ok("...and unchanged figures make no new event", len(ev(P["venice"])) == 1)
F.ACCOUNTS["096622090170"]["prior"] = 70000.00
r3 = T.check_property(conn, prop["venice"], Direct())
F.ACCOUNTS["096622090170"]["prior"] = 80125.73
ok("changed figures are stored and announced", r3["back_owed"] == 70000.00 and len(ev(P["venice"])) == 2 and json.loads(ev(P["venice"])[1]["detail"])["taxes_back_owed"] == 70000.0)
T.check_property(conn, prop["venice"], Direct())        # read the restored figures back in
ok("a direction fallback: tries '931 W LAUREL' first", T.check_property(conn, prop["laurel"], (Dl := Direct()))["status"] == "ok" and Dl.searches == ["931 W LAUREL"])
F.ACCOUNTS["080000010010"] = dict(site="55 FALLBACK ST", legal="NCB 8000 BLK 1 LOT 1", year=2026, levy=500.0, prior=0.0, last=None)
F.SEARCH["55 fallback"] = ["080000010010"]
fb = addp("fallback", "55 N FALLBACK ST"); Df = Direct()
ok("...and retries without the direction when the first try finds nothing", T.check_property(conn, fb, Df)["status"] == "ok" and Df.searches == ["55 N FALLBACK", "55 FALLBACK"])
rc = T.check_property(conn, prop["culebra"], (Dc := Direct()))
ok("seven accounts at one address: the one real-property account is chosen, business accounts are ignored", rc["status"] == "ok" and rc["account"] == "020860020170" and rc["back_owed"] == 14701.69 and Dc.pages == ["020860020170"], rc)
rb = T.check_property(conn, prop["bandera"], Direct())
ok("four accounts: the real-property one again", rb["status"] == "ok" and rb["account"] == "044465010060" and rb["annual"] == 23123.78 and rb["total_due"] == 123906.54)
rd = T.check_property(conn, prop["duplex"], (Dd := Direct()))
ok("two real accounts at one address: not guessed. Listed for a person to choose", rd["status"] == "ambiguous" and [c["account"] for c in rd["candidates"]] == ["050000010010", "050000010020"] and "annual" not in rd and Dd.pages == [], rd)
ok("...with no figures stored for it", row(P["duplex"])["total_due"] is None and row(P["duplex"])["account"] == "")
rn = T.check_property(conn, prop["nowhere"], Direct())
ok("an address with no tax account is 'no match'", rn["status"] == "no_match" and "no tax account" in rn["note"])
ok("an empty address is 'no match' without asking the site", T.check_property(conn, prop["noaddr"], (Dz := Direct()))["status"] == "no_match" and Dz.requests_made == 0)
rw = T.check_property(conn, prop["weird"], Direct())
ok("an unreadable account page is an error that will be retried, not bad data", rw["status"] == "error" and row(P["weird"])["tries"] == 1 and row(P["weird"])["total_due"] is None, rw)
rp = T.check_property(conn, prop["paidup"], Direct())
ok("a paid-up property is recorded as owing $0 (not as missing)", rp["status"] == "ok" and rp["back_owed"] == 0.0 and rp["total_due"] == 0.0)
# a failed refresh keeps the figures we had
before = T.summary(row(P["burton"]))
T.check_property(conn, prop["burton"], Direct())
rb2 = T.check_property(conn, prop["burton"], Direct(error="timed out"))
ok("a failed refresh keeps the last good figures and says why", rb2["status"] == "ok" and rb2["back_owed"] == 42754.98 and "timed out" in rb2["note"] and row(P["burton"])["tries"] == 1, rb2)
try:
    T.check_property(conn, prop["arbor"], Direct(block_after=0)); blocked = False
except T.TaxBlocked:
    blocked = True
ok("a block is raised to the caller, and nothing is stored for that property", blocked and row(P["arbor"]) is None)
# staff choose the account
rs = T.set_account(conn, P["duplex"], "050000010020", Direct())
ok("a person picks the right account of two: figures read and the choice remembered", rs["status"] == "ok" and rs["account"] == "050000010020" and rs["manual"] is True and rs["back_owed"] == 500.0)
Dm = Direct(); T.check_property(conn, prop["duplex"], Dm)
ok("...later refreshes use it (one request, no search)", Dm.requests_made == 1 and Dm.searches == [] and Dm.pages == ["050000010020"] and T.summary(row(P["duplex"]))["manual"] is True)
try:
    T.set_account(conn, P["duplex"], "999999999999", Direct()); bad = False
except T.TaxError:
    bad = True
ok("an account number that returns no tax record is refused, and the old choice stays", bad and row(P["duplex"])["account"] == "050000010020")

# ============================================================ endpoints
S = {"X-Sync-Secret": "s3cretvalue"}
anon = A.app.test_client()
for email, role in (("viewer@x.co", "viewer"), ("worker@reix.co", "intake")):
    anon.post("/internal/intake-users", json={"email": email, "name": role, "password": "longpassword1", "role": role}, headers=S)


def login(email):
    c = A.app.test_client()
    r = c.post("/auth/login", json={"email": email, "password": "longpassword1"}).get_json()
    return c, {"X-CSRF-Token": r["csrf"]}


v, vh = login("viewer@x.co")
w, wh = login("worker@reix.co")
T._site = lambda: Direct()
url = lambda key, tail="": f"/intake/api/property/{P[key]}/taxes{tail}"
ok("the tax card is for staff only", anon.get(url("arbor")).status_code == 401 and v.get(url("arbor")).status_code == 403)
g = w.get(url("arbor")).get_json()
ok("a property not yet looked up has no taxes, and says whether the nightly job is on", g["taxes"] is None and g["autorun"] is False and g["paused_until"] is None and g["search_url"].endswith("index.jsp"), g)
ok("a lookup needs the staff session's CSRF token", w.post(url("arbor", "/check")).status_code == 403 and v.post(url("arbor", "/check"), headers=vh).status_code == 403)
r = w.post(url("arbor", "/check"), headers=wh)
j = r.get_json()
ok("'Check now' reads the figures and returns them", r.status_code == 200 and j["taxes"]["status"] == "ok" and j["taxes"]["annual"] == 933.57 and j["taxes"]["back_owed"] == 17049.15 and j["taxes"]["total_due"] == 17982.72, j)
ok("...and is rate-limited per property (a second click right away is told to wait)", w.post(url("arbor", "/check"), headers=wh).status_code == 429)
ok("...and shows on the tax card afterwards", w.get(url("arbor")).get_json()["taxes"]["back_owed"] == 17049.15)
ok("an unknown property is a 404", w.get("/intake/api/property/99999/taxes").status_code == 404 and w.post("/intake/api/property/99999/taxes/check", headers=wh).status_code == 404)
ok("choosing an account needs a 12-digit number", w.post(url("culebra", "/account"), headers=wh, json={"account": "123"}).status_code == 400 and w.post(url("culebra", "/account"), headers=wh, json={}).status_code == 400)
ok("an account that returns no record is a clear 422", w.post(url("culebra", "/account"), headers=wh, json={"account": "999999999999"}).status_code == 422)
r = w.post(url("duplex", "/account"), headers=wh, json={"account": "0500 0001-0010"})
ok("choosing an account (typed with spaces and dashes) works", r.status_code == 200 and r.get_json()["taxes"]["account"] == "050000010010" and r.get_json()["taxes"]["manual"] is True)
T.pause(conn, 6, "test pause")
ok("while paused, a check is refused with a clear message and the pause is visible", w.post(url("nowhere", "/check"), headers=wh).status_code == 503 and w.get(url("nowhere")).get_json()["paused_until"] and w.post(url("culebra", "/account"), headers=wh, json={"account": "020860020170"}).status_code == 503)
ok("the internal controls need the secret", anon.get("/internal/taxes/status").status_code == 401 and anon.post("/internal/taxes/resume").status_code == 401 and anon.post("/internal/taxes/run").status_code == 401 and w.get("/internal/taxes/status").status_code == 401)
addp("unchecked", "88 UNCHECKED ST")      # one property that has never been looked up
st = anon.get("/internal/taxes/status", headers=S).get_json()
ok("status shows the pause, the counts by outcome and how many have never been checked", st["paused_until"] and st["pause_reason"] == "test pause" and st["counts"].get("ok", 0) >= 6 and st["counts"].get("no_match") == 2 and st["counts"].get("error") == 1 and st["never_checked"] == 1 and st["properties"] == 13 and st["autorun"] is False and st["window_utc"] == "8-12", st)
ok("resume clears the pause", anon.post("/internal/taxes/resume", headers=S).status_code == 200 and anon.get("/internal/taxes/status", headers=S).get_json()["paused_until"] is None)
conn.execute("UPDATE property_taxes SET attempted_at = '2000-01-01T00:00:00' WHERE property_id = ?", (P["nowhere"],)); conn.commit()
ok("after resuming, checks work again", w.post(url("nowhere", "/check"), headers=wh).status_code == 200)
# a block seen by a staff check pauses everything
T._site = lambda: Direct(block_after=0)
conn.execute("UPDATE property_taxes SET attempted_at = '2000-01-01T00:00:00' WHERE property_id = ?", (P["nowhere"],)); conn.commit()
r = w.post(url("nowhere", "/check"), headers=wh)
ok("a block during a staff check pauses all lookups for 6 hours", r.status_code == 503 and anon.get("/internal/taxes/status", headers=S).get_json()["paused_until"], r.get_json())
anon.post("/internal/taxes/resume", headers=S)
T._site = lambda: Direct()

# ============================================================ API, CSV and change feed
KH = {"X-API-Key": v.post("/api/v1/keys", json={"name": "REIX"}, headers=vh).get_json()["api_key"]}
get = lambda key: anon.get("/api/v1/properties", headers=KH, query_string={"property_key": f"bexar-mortgage-{DOC[key]}"}).get_json()["results"][0]
p = get("venice")
ok("the API publishes the tax figures on each property", p["tax_status"] == "ok" and p["tax_account"] == "096622090170" and p["taxes_annual"] == 4045.8 and p["taxes_back_owed"] == 80125.73 and p["taxes_total_due"] == 84171.53 and p["taxes_current_due"] == 4045.8 and p["taxes_year"] == 2026 and p["taxes_delinquent_after"] == "02/01/2027" and p["taxes_as_of"] == dt.datetime.utcnow().strftime("%Y-%m-%d") and p["tax_link"].endswith("showdetail2.jsp?can=096622090170"), {k: p[k] for k in p if k.startswith("tax")})
pd = get("duplex")
ok("...a property whose account was chosen by a person publishes it too", pd["tax_status"] == "ok" and pd["tax_account"] == "050000010010")
pn = get("nowhere")
ok("...a property with no account says so, without figures", pn["tax_status"] == "no_match" and pn["taxes_back_owed"] is None and pn["tax_account"] is None)
pw = get("weird")
ok("...and one not yet looked up (or failed) has empty fields", pw["tax_status"] == "error" and pw["taxes_total_due"] is None and get("fallback")["tax_status"] == "ok")
ok("a paid-up property publishes $0 back taxes, not null", get("paidup")["taxes_back_owed"] == 0.0 and get("paidup")["taxes_total_due"] == 0.0)
pub = {x["doc_number"]: x for x in v.get("/api/public/properties?per_page=50").get_json()["results"]}
ok("signed-in users see them on the list too", pub[DOC["venice"]]["taxes_back_owed"] == 80125.73 and pub[DOC["venice"]]["tax_link"])
rows = {r["notice_number"]: r for r in csv.DictReader(io.StringIO(anon.get("/api/v1/lists/2026-11?format=csv", headers=KH).data.decode()))}
ok("the monthly CSV has the tax columns", rows[DOC["venice"]]["tax_account"] == "096622090170" and rows[DOC["venice"]]["taxes_annual"] == "4045.8" and rows[DOC["venice"]]["taxes_back_owed"] == "80125.73" and rows[DOC["venice"]]["taxes_total_due"] == "84171.53" and rows[DOC["nowhere"]]["taxes_back_owed"] == "", list(rows[DOC["venice"]].keys())[-5:])
evs = [e for e in anon.get("/api/v1/changes?after=0&limit=500", headers=KH).get_json()["events"] if e["type"] == "taxes_updated"]
ok("the change feed announces tax updates, with the new figures", any(e["property_key"].endswith(DOC["venice"]) and e["detail"]["taxes_back_owed"] == 80125.73 for e in evs) and len(evs) >= 6, [e["detail"] for e in evs][:2])
ok("...and an event is not repeated when nothing changed", len([e for e in evs if e["property_key"].endswith(DOC["arbor"])]) == 1)

# ============================================================ privacy
dump = ""
for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
    for r in conn.execute(f'SELECT * FROM "{name}"').fetchall():
        dump += json.dumps([str(x) for x in tuple(r)]) + "\n"
ok("no owner name or mailing address is stored anywhere in the database", "SECRET" not in dump and "FRIENDSWOOD" not in dump)
ok("...but the tax data is there", "096622090170" in dump and "80125.73" in dump)
print("\nALL TAX LOOKUP TESTS PASSED")
