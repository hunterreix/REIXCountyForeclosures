"""Tests for the gentle-by-design parts of taxes.py: the real HTTP client against a stand-in site (pacing, headers,
blocking), and the nightly batch (order, limits, lock, pausing, daily cap, windows).
Run from the repository root:   python3 tools/test_taxes_batch.py"""
import datetime as dt
import json
import os
import sys
import threading
import time
import urllib.request

os.environ.update(REIX_SYNC_SECRET="s3cretvalue", REIX_INSECURE_COOKIES="1", TAX_AUTORUN="0", TAX_MIN_INTERVAL="0",
                  TAX_CONTACT="tax-test@example.com")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import fake_tax_site as F          # noqa: E402
import db                          # noqa: E402
import app as A                    # noqa: E402,F401  (creates the tables)
import taxes as T                  # noqa: E402


def ok(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else "   " + str(extra)))
    assert cond, label


PORT = 5057
F.serve(PORT)
BASE = f"http://127.0.0.1:{PORT}/act_webdev/bexar/"
ctl = lambda path: urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}").read().decode()
srvlog = lambda: json.loads(ctl("/__log"))
conn = db.get_conn()

# ============================================================ the real client against the stand-in site
ctl("/__reset")
site = T.TaxSite(base=BASE, interval=0.3, jitter=0)
html = site.search("727 Venice")
found = T.parse_results(html)
ok("the client searches the way the form does (POST, searchby=6) and reads the results", found and found[0]["account"] == "096622090170")
detail = T.parse_detail(site.account_page("096622090170"))
ok("...then opens the account page by its address (GET showdetail2.jsp?can=...)", detail["total_due"] == 84171.53)
lg = srvlog()
ok("the requests are exactly the two a person's browser would make", [(e["method"], e["path"].split("/")[-1]) for e in lg] == [("POST", "showlist.jsp"), ("GET", "showdetail2.jsp")] and lg[0]["form"] == {"searchby": "6", "criteria": "727 Venice"} and lg[1]["query"] == "can=096622090170", lg)
ok("it identifies itself, with a contact", "REIXForeclosureData/1.0" in lg[0]["ua"] and "tax-test@example.com" in lg[0]["ua"], lg[0]["ua"])
ctl("/__reset")
site2 = T.TaxSite(base=BASE, interval=0.3, jitter=0)
t0 = time.time()
for i in range(3):
    (site if i % 2 == 0 else site2).account_page("093210210050")          # two clients taking turns: they share one pace
lg = srvlog()
gaps = [round(b["t"] - a["t"], 2) for a, b in zip(lg, lg[1:])]
ok("requests are spaced at least the minimum interval apart, even across two clients/workers", all(g >= 0.27 for g in gaps) and time.time() - t0 >= 0.55, gaps)
ctl("/__reset")
siteJ = T.TaxSite(base=BASE, interval=0.2, jitter=0.4)
for i in range(4):
    siteJ.account_page("093210210050")
gaps = [b["t"] - a["t"] for a, b in zip(srvlog(), srvlog()[1:])]
ok("random jitter is added on top of the minimum (never below it)", min(gaps) >= 0.18 and max(gaps) > 0.21, [round(g, 2) for g in gaps])
waits = []
conn.execute("DELETE FROM tax_state WHERE key = 'last_request_at'"); conn.commit()       # a clean slate for the fake clock
fake_clock = [5000.0]
sl = T.TaxSite(base=BASE, interval=3.0, jitter=0, sleep=lambda s: waits.append(round(s, 2)), now=lambda: fake_clock[0])
sl.account_page("093210210050"); sl.account_page("093210210050"); fake_clock[0] += 10; sl.account_page("093210210050")
ok("it waits out the rest of the interval (3s) when asked again at once, and not at all when long enough has passed", len(waits) == 1 and 2.9 <= waits[0] <= 3.0 and sl.requests_made == 3, waits)

# ---- being blocked or challenged
def kind(mode, page="search"):
    ctl("/__reset"); ctl(f"/__mode?m={mode}")
    s = T.TaxSite(base=BASE, interval=0, jitter=0)
    try:
        s.search("727 Venice") if page == "search" else s.account_page("096622090170")
        return "fine"
    except T.TaxBlocked:
        return "blocked"
    except T.TaxError:
        return "error"
    finally:
        ctl("/__mode?m=ok")
ok("HTTP 403, 429 and 503 are treated as being blocked", kind("403") == "blocked" and kind("429") == "blocked" and kind("503") == "blocked")
ok("a CAPTCHA page is treated as being blocked", kind("captcha") == "blocked" and kind("captcha", "detail") == "blocked")
ok("a redirect to another website is treated as being blocked, and is NOT followed", kind("redirect") == "blocked" and not any("challenge" in json.dumps(e) for e in srvlog()))
ok("a redirect within the Tax Office site is followed", kind("local") == "fine")
ok("an ordinary server error (500) is a retryable error, not a block", kind("500") == "error")
try:
    T.TaxSite(base="http://127.0.0.1:1/x/", interval=0, jitter=0).search("1 A"); err = "fine"
except T.TaxError:
    err = "error"
except T.TaxBlocked:
    err = "blocked"
ok("an unreachable site is a retryable error", err == "error")
ctl("/__reset")

# ============================================================ the nightly batch
class Direct:
    """No network. Counts requests; can fail or block on demand; can slow each request down."""
    def __init__(self, error=None, block_after=None, delay=0.0):
        self.searches, self.pages, self.requests_made, self.error, self.block_after, self.delay = [], [], 0, error, block_after, delay

    def _tick(self):
        self.requests_made += 1
        time.sleep(self.delay)
        if self.block_after is not None and self.requests_made > self.block_after:
            raise T.TaxBlocked("blocked")
        if self.error:
            raise T.TaxError(self.error)

    def search(self, term):
        self._tick(); self.searches.append(term)
        hits = F.SEARCH.get(term.lower())
        return F.results_page(term, hits) if hits else F.search_form_page()

    def account_page(self, acct):
        self._tick(); self.pages.append(acct)
        return F.detail_page(acct) if acct in F.ACCOUNTS else F.search_form_page()


IDS = []


def mk(n, y=2026, m=11, layer="mortgage"):
    """A property at '<100+n> BATCH ST' with a matching account on the stand-in site."""
    street = f"{100 + n} BATCH ST"
    acct = f"09000000{n:04d}"
    F.ACCOUNTS[acct] = dict(site=street, legal=f"NCB 9000 BLK {n} LOT 1", year=2026, levy=1000.0 + n, prior=100.0 * n, last=None)
    F.SEARCH[f"{100 + n} batch"] = [acct]
    cur = conn.execute("INSERT INTO properties (county,source_layer,doc_number,address,city,zip,year,month,first_seen,last_seen) VALUES ('Bexar',?,?,?,'SAN ANTONIO','78228',?,?,'x','x')", (layer, f"2026{900000 + n}", street, y, m))
    conn.commit()
    return cur.lastrowid


def reset_all():
    conn.execute("DELETE FROM property_taxes"); conn.execute("DELETE FROM tax_runs"); conn.execute("DELETE FROM tax_state"); conn.commit()


ids = [mk(n) for n in range(1, 9)]
done = lambda: {r["property_id"] for r in conn.execute("SELECT property_id FROM property_taxes").fetchall()}
run = lambda limit=None, site=None, **kw: T.run_batch(limit=limit, site=site or Direct(), **kw)

r = run(3)
ok("a batch does the number asked, never-checked first, newest first", r["processed"] == 3 and r["ok"] == 3 and r["stopped"] == "done" and done() == set(ids[-3:]), (r, done()))
ok("...two requests each (search + account page)", r["requests"] == 6)
rr = conn.execute("SELECT * FROM tax_runs ORDER BY id DESC LIMIT 1").fetchone()
ok("...and the run is recorded", rr["finished_at"] and rr["processed"] == 3 and rr["ok"] == 3 and rr["trigger"] == "manual" and rr["stopped_reason"] == "done")
r = run(100)
ok("the next batch does the rest and does not repeat the first three", r["processed"] == 5 and done() == set(ids) and r["requests"] == 10)
ok("when nothing is due, nothing is requested", run(100)["processed"] == 0 and run(100)["requests"] == 0)
old = (dt.datetime.utcnow() - dt.timedelta(days=31)).isoformat(timespec="seconds")
conn.execute("UPDATE property_taxes SET checked_at = ? WHERE property_id = ?", (old, ids[0])); conn.commit()
Dr = Direct(); r = run(100, Dr)
ok("after 30 days a property is refreshed, with one request (its account is known)", r["processed"] == 1 and r["requests"] == 1 and Dr.searches == [] and Dr.pages == ["090000000001"], (r, Dr.pages))
ok("a property checked within 30 days is left alone", run(100)["processed"] == 0)

# ---- the rules for what is due
reset_all()
now = dt.datetime.utcnow()
ago = lambda **k: (now - dt.timedelta(**k)).isoformat(timespec="seconds")
mk_row = lambda pid, status, attempted, checked="", tries=0, manual=0: conn.execute(
    "INSERT OR REPLACE INTO property_taxes (property_id, status, attempted_at, checked_at, tries, manual) VALUES (?,?,?,?,?,?)", (pid, status, attempted, checked, tries, manual))
cases = {"ok_fresh": (ids[0], "ok", ago(days=2), ago(days=2), 0, 0), "ok_old": (ids[1], "ok", ago(days=2), ago(days=40), 0, 0),
         "amb_recent": (ids[2], "ambiguous", ago(days=5), "", 0, 0), "amb_old": (ids[3], "ambiguous", ago(days=15), "", 0, 0),
         "err_recent": (ids[4], "error", ago(hours=1), "", 1, 0), "err_retry": (ids[5], "error", ago(hours=13), "", 1, 0),
         "err_gaveup": (ids[6], "error", ago(hours=13), "", 4, 0), "nomatch_old": (ids[7], "no_match", ago(days=20), "", 0, 0)}
for c in cases.values():
    mk_row(*c)
conn.commit()
due = {r["id"] for r in T.due_properties(conn, 100)}
want = {ids[1], ids[3], ids[5], ids[7]}
ok("due: a stale 'ok', an ambiguous/no-match not tried for 14 days, and an error not tried for 12 hours", due == want, (sorted(due), sorted(want)))
ok("not due: fresh 'ok', recent ambiguous, a recent error, and an error that has failed 4 times (it waits a month)", not ({ids[0], ids[2], ids[4], ids[6]} & due))
conn.execute("UPDATE property_taxes SET manual = 1, status = 'ambiguous', attempted_at = ? WHERE property_id = ?", (ago(days=30), ids[3])); conn.commit()
ok("a person's choice is never re-searched (a manual row is not due as 'ambiguous')", ids[3] not in {r["id"] for r in T.due_properties(conn, 100)})
extra = mk(50, layer="tax"); extra2 = mk(51)
conn.execute("UPDATE properties SET address = '' WHERE id = ?", (extra2,)); conn.commit()
ok("only Bexar mortgage notices with an address are ever looked up", not ({extra, extra2} & {r["id"] for r in T.due_properties(conn, 100)}))

# ---- being blocked mid-run
reset_all()
Db = Direct(block_after=3)
r = run(100, Db)
ok("a block stops the whole run at once, the property in progress stores nothing, and lookups pause", r["processed"] == 1 and r["stopped"].startswith("blocked") and len(done()) == 1 and T.paused_until(conn), r)
ok("...no further requests are made after the block was seen", Db.requests_made == 4)
ok("...and a new run while paused does nothing", "paused until" in run(100)["skipped"] and Direct().requests_made == 0)
T.resume(conn)
r = run(100)
ok("after resuming, the run picks up where it stopped: the other seven, including the one cut off", r["processed"] == 7 and r["ok"] == 7 and len(done()) == 8, r)
reset_all()

# ---- failures
De = Direct(error="the site is down")
r = run(100, De)
ok("five failures in a row stop the run and pause for an hour", r["processed"] == 5 and r["errors"] == 5 and "five lookups in a row" in r["stopped"], r)
left = (dt.datetime.fromisoformat(T.paused_until(conn)) - dt.datetime.utcnow()).total_seconds()
ok("...the pause is about an hour", 3000 < left < 3700, left)
T.resume(conn); reset_all()

# ---- one run at a time
conn.execute("INSERT INTO tax_runs (started_at, heartbeat_at, trigger) VALUES (?,?, 'other worker')", (T._utc(), T._utc())); conn.commit()
ok("while another worker's run is active, a second run does not start", "in progress" in run(100)["skipped"])
conn.execute("UPDATE tax_runs SET heartbeat_at = ?", (ago(minutes=20),)); conn.commit()
r = run(1)
ok("a run that stopped reporting 20 minutes ago is treated as abandoned, and work continues", r.get("processed") == 1 and conn.execute("SELECT stopped_reason FROM tax_runs ORDER BY id LIMIT 1").fetchone()["stopped_reason"] == "abandoned")
reset_all()
out = []
slow = Direct(delay=0.05)
threads = [threading.Thread(target=lambda: out.append(T.run_batch(limit=2, site=slow))) for _ in range(4)]       # four workers at once
[t.start() for t in threads]; [t.join() for t in threads]
ran = [o for o in out if "processed" in o]
ok("four workers starting together: exactly one runs, the others stand aside", len(ran) == 1 and len([o for o in out if "skipped" in o]) == 3 and ran[0]["processed"] == 2, out)
ok("...so no property was looked up twice", len(slow.pages) == 2 and len(set(slow.pages)) == 2)
reset_all()

# ---- limits
os.environ["TAX_BATCH"] = "3"
conn.execute("INSERT INTO tax_runs (started_at, heartbeat_at, finished_at, trigger, processed) VALUES (?,?,?, 'scheduled', 3)", (T._utc(), T._utc(), T._utc())); conn.commit()
ok("the daily limit stops the scheduled job once reached", "limit" in T.run_batch(trigger="scheduled", site=Direct(), daily_cap=True)["skipped"])
ok("...but not a deliberate manual run", T.run_batch(limit=1, site=Direct())["processed"] == 1)
conn.execute("UPDATE tax_runs SET processed = 1"); conn.commit()
Dd = Direct(); r = T.run_batch(trigger="scheduled", site=Dd, daily_cap=True)
ok("with part of the day's limit used (2 of 3), only the remainder is done", r["processed"] == 1, r)
del os.environ["TAX_BATCH"]
reset_all()
r = T.run_batch(limit=5, site=Direct(), max_minutes=0)
ok("a time limit stops a run", r["processed"] == 0 and r["stopped"] == "time limit reached")

# ---- the schedule
at = lambda h: dt.datetime(2026, 11, 12, h, 30)
ok("the job only runs in the overnight window (default 8-12 UTC = 3-7 AM Central)", T._in_window(at(8)) and T._in_window(at(11)) and not T._in_window(at(12)) and not T._in_window(at(7)) and not T._in_window(at(15)) and not T._in_window(at(0)))
os.environ["TAX_WINDOW_UTC"] = "22-24"
ok("...and the window is configurable", T._in_window(at(23)) and not T._in_window(at(8)))
os.environ["TAX_WINDOW_UTC"] = "nonsense"
ok("...a bad setting falls back to the default", T._in_window(at(9)))
del os.environ["TAX_WINDOW_UTC"]
ok("TAX_AUTORUN=0 means the scheduler never starts", T.start_scheduler() is None)
os.environ["TAX_AUTORUN"] = "1"; T._scheduler_started = False
t = T.start_scheduler()
ok("otherwise one background thread starts per worker (and only one)", t is not None and t.is_alive() and t.daemon and T.start_scheduler() is None)
os.environ["TAX_AUTORUN"] = "0"

# ============================================================ a whole batch over HTTP, with real timing
reset_all(); ctl("/__reset")
ids2 = [mk(n) for n in range(20, 23)]
real = T.TaxSite(base=BASE, interval=0.25, jitter=0)
t0 = time.time()
r = T.run_batch(limit=3, site=real)
lg = srvlog()
gaps = [b["t"] - a["t"] for a, b in zip(lg, lg[1:])]
ok("a batch over real HTTP: three properties, six requests, none closer than the interval", r["processed"] == 3 and r["ok"] == 3 and len(lg) == 6 and min(gaps) >= 0.23, (r, [round(g, 2) for g in gaps]))
ok("...and the figures read over HTTP are stored", conn.execute("SELECT COUNT(*) c FROM property_taxes WHERE status = 'ok' AND total_due IS NOT NULL").fetchone()["c"] == 3)
reset_all(); ctl("/__reset"); ctl("/__mode?m=403")
r = T.run_batch(limit=3, site=T.TaxSite(base=BASE, interval=0, jitter=0))
ok("a block over real HTTP: exactly ONE request is made, the run stops, and lookups pause", r["processed"] == 0 and r["stopped"].startswith("blocked") and len(srvlog()) == 1 and T.paused_until(conn))
ctl("/__mode?m=ok"); T.resume(conn)
print("\nALL TAX BATCH TESTS PASSED")
