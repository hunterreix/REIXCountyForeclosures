"""
A stand-in for the Bexar County Tax Assessor-Collector's public search, for tests and local development.
It mirrors the structure of the real pages (a results table with six columns, and an account page made of
<div><label>Name:</label> value</div> lines), with made-up owners so tests can prove owner data is never kept.

    python3 tools/fake_tax_site.py 5055        # then run the app with TAX_BASE_URL=http://127.0.0.1:5055/act_webdev/bexar/

Control (for tests):  GET /__mode?m=ok|403|429|503|500|captcha|redirect|local   GET /__log   GET /__reset
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

OWNER = "SECRET OWNER NAME"

# account -> what the account page shows
ACCOUNTS = {
    "096622090170": dict(site="727 VENICE", legal="NCB 9662 BLK 209 LOT 17", year=2026, levy=4045.80, prior=80125.73, last=1412.11),
    "093210210050": dict(site="318 BURTON AVE", legal="NCB 9321 BLK 21 LOT 5", year=2026, levy=1138.40, prior=42754.98, last=250.00),
    "002100070120": dict(site="931 W LAUREL", legal="NCB 210 BLK 7 LOT 12 FIRST QUALITYCYLINDERS INC", year=2026, levy=28223.19, prior=612177.93, last=None),
    "021550020240": dict(site="601 ARBOR PL", legal="NCB 2155 BLK 2 LOT S 121 .8 FT OF 24", year=2026, levy=933.57, prior=17049.15, last=2990.81),
    "020860020170": dict(site="1306 CULEBRA RD", legal="NCB 2086 BLK 2 LOT 17, 18 & E 16.6 FTOF 38", year=2026, levy=6020.50, prior=14701.69, last=501.24),
    "044465010060": dict(site="7108 BANDERA RD", legal="CB 4446E BLK 1 LOT 6", year=2026, levy=23123.78, prior=100782.76, last=2802.00),
    "050000010010": dict(site="500 DUPLEX ST", legal="NCB 5000 BLK 1 LOT 1", year=2026, levy=2000.00, prior=0.0, last=None),
    "050000010020": dict(site="500 DUPLEX ST", legal="NCB 5000 BLK 1 LOT 2", year=2026, levy=2100.00, prior=500.00, last=None),
    "060000020010": dict(site="12 PAIDUP DR", legal="NCB 6000 BLK 2 LOT 1", year=2026, levy=1500.00, prior=None, last=1500.00, paid=True),
    "070000030010": dict(site="9 WEIRD ST", legal="NCB 7000 BLK 3 LOT 1", broken=True),
    # business personal-property accounts at the same addresses (inventory, furniture and fixtures)
    "902043500000": dict(site="1306 CULEBRA AVE", legal="BERNARD LUMBER CO 1306 CULEBRA AVE", year=2026, levy=100.0, prior=0.0, last=None),
    "908057395100": dict(site="1306 CULEBRA AVE", legal="HOME BUYER REAL EST CO 1306 CULEBRA AVE", year=2026, levy=90.0, prior=0.0, last=None),
    "912026151280": dict(site="1306 CULEBRA RD", legal="LAS PRINCESAS 001306 00 CULEBRA RD INVFURN FIXT EQUIP SUP", year=2026, levy=80.0, prior=0.0, last=None),
    "000001357843": dict(site="1306 CULEBRA RD", legal="LAS PRINCESAS 1306 CULEBRA RD INV FURNFIXT EQPT SUP", year=2026, levy=70.0, prior=0.0, last=None),
    "912045890000": dict(site="1306 CULEBRA AV", legal="PERSONAL PROPERTY AT1306 CULEBRA AVE (N82)", year=2026, levy=60.0, prior=0.0, last=None),
    "921030757650": dict(site="1306 CULEBRA RD", legal="UNICORN COMPUTERS001306 00 CULEBRA RDINV FURN FIXT EQUIP SUPCOMPUTER SALES", year=2026, levy=50.0, prior=0.0, last=None),
    "902020849965": dict(site="7108 BANDERA RD", legal="BANK OF AMERICA TEXAS NA007108 00 BANDERA RDFURN FIXT EQPT SUPBANK", year=2026, levy=40.0, prior=0.0, last=None),
    "902020847770": dict(site="7108 BANDERA RD", legal="BANK OF AMERICA 007108 00 BANDERA RDFURN FIXT EQPT SUP 90604 320 5060", year=2026, levy=30.0, prior=0.0, last=None),
    "000001365517": dict(site="7108 BANDERA RD", legal="HOPSCOTCH HEALTH CHILDRENS URGENT CAREPLLC 7108 BANDERA RD", year=2026, levy=20.0, prior=0.0, last=None),
}
# what the search returns for what is typed (lower case), as the real site does for these addresses
SEARCH = {
    "727 venice": ["096622090170"], "318 burton": ["093210210050"], "931 w laurel": ["002100070120"], "601 arbor": ["021550020240"],
    "1306 culebra": ["902043500000", "908057395100", "020860020170", "912026151280", "000001357843", "912045890000", "921030757650"],
    "7108 bandera": ["902020849965", "902020847770", "000001365517", "044465010060"],
    "500 duplex": ["050000010010", "050000010020"], "12 paidup": ["060000020010"], "9 weird": ["070000030010"],
}


def money(v):
    return "${:,.2f}".format(v)


def search_form_page():
    return ('<html><body><h1>Find Your Property Tax Account</h1><form action="showlist.jsp" method="post">'
            '<select name="searchby"><option value="3">Owner/Business Name</option><option value="6">Property Address: 1234 Main or 567 Tobin G</option></select>'
            '<input type="text" name="criteria"><input type="submit" value="Search"></form></body></html>')


def results_page(term, accounts):
    n = len(accounts)
    rows = "".join(
        f'<tr id="account-container" valign="top"><td class="else"></td><td class="account-responsive"><a href="showdetail2.jsp?can={a}">{a}</a></td>'
        f'<td align="center">{OWNER}<br>1 SECRET ST<br>FRIENDSWOOD, TX 77546</td><td>{ACCOUNTS[a]["site"]}</td><td>{ACCOUNTS[a]["legal"]}</td><td>443588</td></tr>'
        for a in accounts)
    return (f'<html><head><title>Bexar County Tax Assessor-Collector</title></head><body><div align="center"><h1 class="headline"> Your Search Results </h1>'
            f'<p><b>The following is the result of your Property Address search for "{term.upper()}"</b></p>'
            f'<p><b>There {"is" if n == 1 else "are"} {n} match{"" if n == 1 else "es"}.</b></p></div>'
            '<table class="tablesorter responsive-table" width="100%"><thead><tr><th><b>Cart</b></th><th><b>Account Number</b></th><th>Owner</th>'
            '<th><b>Property Site Address</b></th><th><b>Legal Description</b></th><th><b>CAD Reference No.</b></th></tr></thead><tbody>'
            f'{rows}</tbody></table></body></html>')


def detail_page(account):
    a = ACCOUNTS[account]
    if a.get("broken"):
        return "<html><body><h1>Property Tax Balance</h1><p>We are experiencing technical difficulties.</p></body></html>"
    cur = a["levy"] if not a.get("paid") else 0.0
    prior = a["prior"]
    total = cur + (prior or 0.0)
    lines = [f'<div><label>Account Number:</label> {account} </div>',
             f'<div><label>Owner Name:</label><br> {OWNER}<br>1 SECRET ST<br>FRIENDSWOOD, TX 77546 </div>',
             f'<div><label>Property Site Address:</label><br> {a["site"]} </div>',
             f'<div><label>Legal Description:</label><br> {a["legal"]}<br></div>',
             f'<div><label> {a["year"]} Year Tax Levy: </label> {money(a["levy"])} </div>',
             f'<div><label> {a["year"]} Year Amount Due: </label> &nbsp; {money(cur)} </div>',
             '<div><label>Delinquent After:</label> 02/01/2027 </div>',
             f'<div><label>Half Payment Option Amount (1/2 of Current Tax Levy):</label> &nbsp; {money(a["levy"] / 2)} <br> * Pay by Jan 31, 2027 </div>']
    if prior is not None:
        lines.append(f'<div><label>Prior Year(s) Amount Due:</label> {money(prior)} </div>')
    lines.append(f'<div style="color:red;"><label> Total Amount Due:</label> {money(total)} </div>')
    if a.get("last") is not None:
        lines.append(f'<div><label> Last Payment Amount Received:</label> {money(a["last"])} </div>')
    lines.append(f'<div><label>Last Payer:</label><br> {OWNER} </div>')
    return ('<html><body><h1>PROPERTY TAX BALANCE</h1><table><tr><td class="responsive-table" width="60%" valign="top">'
            + "".join(lines) + '</td></tr></table></body></html>')


class Handler(BaseHTTPRequestHandler):
    mode = "ok"
    log = []

    def _send(self, code, body, ctype="text/html; charset=ISO-8859-1", headers=None):
        data = body.encode("utf-8", "replace")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _control(self, url):
        if url.path == "/__mode":
            Handler.mode = parse_qs(url.query).get("m", ["ok"])[0]
            return self._send(200, "ok", "text/plain") or True
        if url.path == "/__log":
            return self._send(200, json.dumps(Handler.log), "application/json") or True
        if url.path == "/__reset":
            Handler.log.clear()
            Handler.mode = "ok"
            return self._send(200, "ok", "text/plain") or True
        return False

    def _gate(self):
        m = Handler.mode
        if m in ("403", "429", "503", "500"):
            self._send(int(m), "<html><body>blocked</body></html>")
        elif m == "local" and self.path.endswith("showlist.jsp"):         # a redirect that stays on the site
            self._send(302, "", headers={"Location": "index.jsp"})
        elif m == "captcha":
            self._send(200, "<html><body><div class='g-recaptcha'>Please complete the CAPTCHA to continue</div></body></html>")
        elif m == "redirect":
            self._send(302, "", headers={"Location": "https://challenge.example.invalid/check"})
        else:
            return False
        return True

    def do_GET(self):
        url = urlparse(self.path)
        if self._control(url):
            return
        Handler.log.append({"t": time.time(), "method": "GET", "path": url.path, "query": url.query, "ua": self.headers.get("User-Agent", "")})
        if self._gate():
            return
        if url.path.endswith("showdetail2.jsp"):
            acct = parse_qs(url.query).get("can", [""])[0]
            return self._send(200, detail_page(acct)) if acct in ACCOUNTS else self._send(200, search_form_page())
        if url.path.endswith("index.jsp"):
            return self._send(200, search_form_page())
        self._send(404, "Not Found")

    def do_POST(self):
        url = urlparse(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)).decode()
        form = parse_qs(body)
        Handler.log.append({"t": time.time(), "method": "POST", "path": url.path, "form": {k: v[0] for k, v in form.items()}, "ua": self.headers.get("User-Agent", "")})
        if self._gate():
            return
        if url.path.endswith("showlist.jsp"):
            term = form.get("criteria", [""])[0].strip()
            hits = SEARCH.get(term.lower())
            return self._send(200, results_page(term, hits) if hits else search_form_page())
        self._send(404, "Not Found")

    def log_message(self, *a):
        pass


def serve(port=5055):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5055
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("fake Tax Office site on port", port, flush=True)
    srv.serve_forever()
