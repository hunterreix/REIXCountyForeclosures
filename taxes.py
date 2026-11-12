"""
Property tax lookup from the Bexar County Tax Assessor-Collector's public search.

WHAT IT DOES
  For each foreclosure notice's property it finds the tax account on the Tax Office's public search
  (https://bexar.acttax.com/act_webdev/bexar/index.jsp) and reads the figures the account page shows:
  the annual tax, back taxes (prior years), the total due and the delinquent-after date. Results are kept per
  property, shown to staff and signed-in users, and published through the API (see TAXES.md).

HOW GENTLE IT IS  (the site publishes no robots.txt or terms, so we hold ourselves to these limits)
  * one request at a time, at least TAX_MIN_INTERVAL seconds apart (default 3, plus up to 1s of random jitter),
    shared by every server worker
  * only our own properties, and only the two pages a person would open (the address search and the account page);
    a property whose account is already known needs one request, not two
  * overnight only (TAX_WINDOW_UTC, default "8-12" = 3-7 AM Central) and at most TAX_BATCH (default 250) per day
  * a property is re-checked at most every TAX_REFRESH_DAYS (default 30)
  * it STOPS and pauses for 6 hours at the first sign of being blocked (HTTP 401/403/407/429/503, a CAPTCHA or
    "access denied" page, or a redirect away from the Tax Office site)
  * it identifies itself with a User-Agent (set TAX_CONTACT so the Tax Office can reach you)
  * owner names and mailing addresses on those pages are read past and never stored
  * TAX_AUTORUN=0 turns the nightly job off completely
"""
import datetime as dt
import json
import os
import random
import re
import threading
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Blueprint, jsonify, request

from db import get_conn
from events import log_event
from intake import require_intake, _secret_ok, _log

TAX_PUBLIC_BASE = "https://bexar.acttax.com/act_webdev/bexar/"
taxes_bp = Blueprint("taxes", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS property_taxes (
    property_id      INTEGER PRIMARY KEY,
    status           TEXT NOT NULL,                   -- ok | ambiguous | no_match | error
    account          TEXT NOT NULL DEFAULT '',        -- the 12-digit tax account
    manual           INTEGER NOT NULL DEFAULT 0,      -- 1 = a person chose the account
    tax_year         INTEGER,
    annual_levy      REAL,                            -- this year's tax
    current_year_due REAL,
    prior_years_due  REAL,                            -- back taxes
    total_due        REAL,
    delinquent_after TEXT NOT NULL DEFAULT '',
    last_payment     REAL,
    candidates_json  TEXT NOT NULL DEFAULT '[]',      -- when several accounts match: account / site address / legal
    note             TEXT NOT NULL DEFAULT '',
    checked_at       TEXT NOT NULL DEFAULT '',        -- when the figures were last read successfully ("as of")
    attempted_at     TEXT NOT NULL DEFAULT '',        -- the last try, successful or not
    changed_at       TEXT NOT NULL DEFAULT '',
    tries            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tax_status ON property_taxes(status, attempted_at);
CREATE TABLE IF NOT EXISTS tax_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tax_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    heartbeat_at   TEXT NOT NULL,
    finished_at    TEXT,
    trigger        TEXT NOT NULL DEFAULT '',
    processed      INTEGER NOT NULL DEFAULT 0,
    ok             INTEGER NOT NULL DEFAULT 0,
    ambiguous      INTEGER NOT NULL DEFAULT 0,
    no_match       INTEGER NOT NULL DEFAULT 0,
    errors         INTEGER NOT NULL DEFAULT 0,
    stopped_reason TEXT NOT NULL DEFAULT ''
);
"""

EMPTY_TAX_FIELDS = {"tax_status": None, "tax_account": None, "tax_link": None, "taxes_annual": None,
                    "taxes_back_owed": None, "taxes_current_due": None, "taxes_total_due": None, "taxes_year": None,
                    "taxes_delinquent_after": None, "taxes_as_of": None}


def _cfg(name, default, cast=str):
    v = os.environ.get(name)
    try:
        return cast(v) if v not in (None, "") else default
    except ValueError:
        return default


def _utc():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _utc_dt():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


_schema_ready = False


def ensure_schema(conn):
    global _schema_ready
    if not _schema_ready:
        conn.executescript(SCHEMA)
        conn.commit()
        _schema_ready = True


# ============================================================ addresses
DIRECTIONS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}
STREET_TYPES = {"ST", "STREET", "DR", "DRIVE", "AVE", "AV", "AVENUE", "BLVD", "BOULEVARD", "RD", "ROAD", "LN", "LANE",
                "CT", "COURT", "PL", "PLACE", "WAY", "TRL", "TRAIL", "PKWY", "PARKWAY", "HWY", "HIGHWAY", "CIR", "CIRCLE",
                "PT", "TER", "TERRACE", "XING", "FWY", "EXPY", "CV", "COVE", "BND", "HOLW", "RDG", "SQ", "ALY", "PASS"}


def _tokens(address):
    a = (address or "").upper()
    a = re.sub(r"#.*$", " ", a)                                                   # unit after '#'
    a = re.sub(r"\b(APT|UNIT|STE|SUITE|BLDG|SPC|TRLR|LOT)\b.*$", " ", a)         # unit words
    a = re.sub(r"\b\d+/\d+\b", " ", a)                                            # 1/2
    a = re.sub(r"[.,]", " ", a)
    return a.split()


def parse_address(address):
    """'7108 BANDERA RD' -> {number, direction, name: [words], type}. None when it does not start with a house number."""
    t = _tokens(address)
    if len(t) < 2 or not re.fullmatch(r"\d+[A-Z]?", t[0]):
        return None
    number, rest, direction, stype = t[0], t[1:], "", ""
    if len(rest) >= 2 and rest[0] in DIRECTIONS:
        direction, rest = rest[0], rest[1:]
    if len(rest) >= 2 and rest[-1] in STREET_TYPES:
        stype, rest = rest[-1], rest[:-1]
    if not rest:
        return None
    return {"number": number, "direction": direction, "name": rest, "type": stype}


def search_terms(address):
    """How to type this address into the Tax Office's search, best first (at most two tries). The site wants the
    number, a direction letter in front of the street name, the first word of the street and the first letter of
    a second word, and no 'St', 'Dr', 'Ave'..."""
    p = parse_address(address)
    if not p:
        return []

    def fmt(direction):
        name = p["name"]
        core = name[0] if len(name) == 1 else name[0] + " " + name[1][0]
        return " ".join(x for x in (p["number"], direction, core) if x)
    out = [fmt(p["direction"])]
    if p["direction"]:
        out.append(fmt(""))              # some streets are listed without the direction
    return out


def same_street(ours, theirs):
    """Is the account's site address on the same street, with the same number, as our property?"""
    if not ours or not theirs:
        return False
    if ours["number"] != theirs["number"] or ours["name"][0] != theirs["name"][0]:
        return False
    if ours["direction"] and theirs["direction"] and ours["direction"] != theirs["direction"]:
        return False
    if len(ours["name"]) > 1 and len(theirs["name"]) > 1 and ours["name"][1][0] != theirs["name"][1][0]:
        return False
    return True


REAL_LEGAL = re.compile(r"\b(NCB|CB|BLK|BLOCK|LOT|ABS|ACRES?|TRACT|SURVEY|SUBD|UNIT|CONDO\w*)\b")


def is_real_property(account, legal):
    """Real property accounts have a legal description (NCB / block / lot...). Business personal-property accounts
    (inventory, furniture and fixtures) start with 9 or 00000 and are described by the business's name."""
    if account.startswith("9") or account.startswith("00000"):
        return False
    return bool(REAL_LEGAL.search((legal or "").upper()))


# ============================================================ reading the two pages
class TaxError(Exception):
    """A failed lookup (network problem, unexpected page). Retried later."""


class TaxPageChanged(TaxError):
    """The page did not look like the Tax Office's pages (its layout may have changed)."""


class TaxBlocked(Exception):
    """The site refused or challenged us. Everything stops and pauses."""


def _txt(el):
    return re.sub(r"\s+", " ", el.get_text(" ")).replace("\xa0", " ").strip()


def parse_results(html_text):
    """The accounts on a search-results page: [{account, site_address, legal}]. The owner columns are ignored."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    out, seen = [], set()
    for a in soup.select('a[href*="showdetail2.jsp"]'):
        m = re.search(r"can=(\d{12})", a.get("href", ""))
        row = a.find_parent("tr")
        cells = row.find_all("td") if row else []
        if not m or m.group(1) in seen or len(cells) < 5:
            continue
        seen.add(m.group(1))
        out.append({"account": m.group(1), "site_address": _txt(cells[3]), "legal": _txt(cells[4])})
    return out


def is_search_form(html_text):
    return bool(re.search(r"name=[\"']searchby[\"']", html_text or "", re.I))


def _money(v):
    m = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", v or "")
    return float(m.group(1).replace(",", "")) if m else None


def parse_detail(html_text):
    """The figures on an account page. Raises TaxPageChanged when the page does not carry them."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    fields = {}
    for lab in soup.find_all("label"):
        name = _txt(lab).rstrip(":").strip()
        if lab.parent is not None and name:
            fields.setdefault(name, _txt(lab.parent).replace(_txt(lab), "", 1).strip())
    year = levy = cur = None
    for k, v in fields.items():
        m = re.fullmatch(r"(\d{4}) Year Tax Levy", k)
        if m:
            year, levy = int(m.group(1)), _money(v)
        m = re.fullmatch(r"(\d{4}) Year Amount Due", k)
        if m:
            cur = _money(v)
    total = _money(fields.get("Total Amount Due"))
    if total is None or levy is None:
        raise TaxPageChanged("the account page did not show the tax figures we expect")
    prior = _money(fields.get("Prior Year(s) Amount Due"))
    if prior is None:
        prior = 0.0                                          # the line is absent when nothing is owed for prior years
    if cur is None:
        cur = levy
    da = re.search(r"\d{2}/\d{2}/\d{4}", fields.get("Delinquent After", ""))
    note = ""
    if abs(total - (cur + prior)) > 1.0:
        note = "the figures on the page do not add up; check the account page"
    return {"account": re.sub(r"\D", "", fields.get("Account Number", ""))[:12], "tax_year": year, "annual_levy": levy,
            "current_year_due": cur, "prior_years_due": prior, "total_due": total,
            "delinquent_after": da.group(0) if da else "", "last_payment": _money(fields.get("Last Payment Amount Received")),
            "note": note}


# ============================================================ state, pacing, pausing
def _state_get(conn, key, default=""):
    r = conn.execute("SELECT value FROM tax_state WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def _state_set(conn, key, value):
    conn.execute("INSERT INTO tax_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, str(value)))


def paused_until(conn):
    v = _state_get(conn, "paused_until")
    return v if v and v > _utc() else None


def pause(conn, hours, reason):
    _state_set(conn, "paused_until", (_utc_dt() + dt.timedelta(hours=hours)).isoformat(timespec="seconds"))
    _state_set(conn, "pause_reason", reason[:300])
    conn.commit()


def resume(conn):
    _state_set(conn, "paused_until", "")
    _state_set(conn, "pause_reason", "")
    conn.commit()


def _reserve_slot(interval, jitter, now):
    """Book the next request slot, shared by every worker, and say how long to wait for it. The booking is made
    first and the waiting happens afterwards, so no database lock is held while sleeping."""
    conn = get_conn()
    conn.isolation_level = None                      # manual transaction
    try:
        conn.execute("BEGIN IMMEDIATE")
        last = float(_state_get(conn, "last_request_at", "0") or 0)
        t = now()
        slot = max(t, last + interval + (random.uniform(0, jitter) if jitter else 0))
        _state_set(conn, "last_request_at", repr(slot))
        conn.execute("COMMIT")
        return max(0.0, slot - t)
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


BLOCK_STATUSES = {401, 403, 407, 429, 503}
BLOCK_WORDS = re.compile(r"captcha|access denied|request blocked|unusual traffic|too many requests|temporarily blocked|"
                         r"are you a robot|automated (?:access|queries)|cloudflare ray id|incapsula|just a moment\.\.\.|"
                         r"attention required", re.I)


class TaxSite:
    """The Tax Office's public search, fetched politely."""

    def __init__(self, base=None, interval=None, session=None, sleep=time.sleep, now=time.time, jitter=None):
        self.base = (base or _cfg("TAX_BASE_URL", TAX_PUBLIC_BASE)).rstrip("/") + "/"
        self.interval = interval if interval is not None else _cfg("TAX_MIN_INTERVAL", 3.0, float)
        self.jitter = 1.0 if jitter is None else jitter
        self.session = session or requests.Session()
        contact = _cfg("TAX_CONTACT", "reix-foreclosures-phk9.onrender.com")
        self.session.headers["User-Agent"] = f"REIXForeclosureData/1.0 (property tax lookups for foreclosure notices; contact: {contact})"
        self.sleep, self.now = sleep, now
        self.requests_made = 0

    def _fetch(self, method, page, **kw):
        url, hops = self.base + page, 0
        while True:
            wait = _reserve_slot(self.interval, self.jitter, self.now)
            if wait > 0:
                self.sleep(wait)
            try:
                r = self.session.request(method, url, timeout=20, allow_redirects=False, **kw)
            except requests.RequestException as e:
                raise TaxError("could not reach the Tax Office site (%s)" % type(e).__name__)
            self.requests_made += 1
            body = r.text or ""
            if r.status_code in BLOCK_STATUSES or BLOCK_WORDS.search(body[:20000]):
                raise TaxBlocked("the Tax Office site refused or challenged the request (HTTP %s)" % r.status_code)
            if 300 <= r.status_code < 400:                    # follow a redirect only within the Tax Office site
                target = urljoin(url, r.headers.get("Location", ""))
                if urlparse(target).netloc != urlparse(self.base).netloc or hops >= 2:
                    raise TaxBlocked("the Tax Office site redirected the request elsewhere")
                url, hops, method, kw = target, hops + 1, "GET", {}
                continue
            if r.status_code != 200:
                raise TaxError("the Tax Office site answered HTTP %s" % r.status_code)
            return body

    def search(self, criteria):
        return self._fetch("POST", "showlist.jsp", data={"searchby": "6", "criteria": criteria})

    def account_page(self, account):
        return self._fetch("GET", "showdetail2.jsp", params={"can": account})


def _site():
    return TaxSite()


# ============================================================ looking up one property
def _save(conn, pid, prev, **kw):
    """Write one property's result. Logs a taxes_updated event when the figures are new or changed."""
    now = _utc()
    p = dict(prev) if prev else {}
    status = kw["status"]
    keep_ok = status == "error" and p.get("status") == "ok"          # a failed refresh keeps the figures we had
    row = {"property_id": pid, "status": "ok" if keep_ok else status, "account": p.get("account", ""), "manual": p.get("manual", 0),
           "tax_year": p.get("tax_year"), "annual_levy": p.get("annual_levy"), "current_year_due": p.get("current_year_due"),
           "prior_years_due": p.get("prior_years_due"), "total_due": p.get("total_due"),
           "delinquent_after": p.get("delinquent_after", ""), "last_payment": p.get("last_payment"),
           "candidates_json": "[]", "note": kw.get("note", ""), "checked_at": p.get("checked_at", ""),
           "attempted_at": now, "changed_at": p.get("changed_at", ""), "tries": 0}
    if keep_ok:
        row["tries"] = int(p.get("tries") or 0) + 1
        row["candidates_json"] = p.get("candidates_json", "[]")
    elif status == "ok":
        d = kw["detail"]
        row.update(account=d["account"] or kw.get("account", ""), manual=int(kw.get("manual", p.get("manual", 0))),
                   tax_year=d["tax_year"], annual_levy=d["annual_levy"], current_year_due=d["current_year_due"],
                   prior_years_due=d["prior_years_due"], total_due=d["total_due"], delinquent_after=d["delinquent_after"],
                   last_payment=d["last_payment"], checked_at=now, note=d["note"] or kw.get("note", ""))
    else:
        row.update(account="", manual=0, tax_year=None, annual_levy=None, current_year_due=None, prior_years_due=None,
                   total_due=None, delinquent_after="", last_payment=None, checked_at="")
        if status == "ambiguous":
            row["candidates_json"] = json.dumps(kw.get("candidates", []))
        if status == "error":
            row["tries"] = int(p.get("tries") or 0) + 1
    figures = ("tax_year", "annual_levy", "prior_years_due", "total_due")
    changed = status == "ok" and (not prev or p.get("status") != "ok" or any(p.get(k) != row[k] for k in figures))
    if changed:
        row["changed_at"] = now
    cols = list(row)
    conn.execute(f"INSERT OR REPLACE INTO property_taxes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 [row[c] for c in cols])
    if changed:
        log_event(conn, pid, "taxes_updated", None, json.dumps({"taxes_annual": row["annual_levy"], "taxes_back_owed": row["prior_years_due"],
                                                                 "taxes_total_due": row["total_due"]}))
    conn.commit()
    return summary(conn.execute("SELECT * FROM property_taxes WHERE property_id = ?", (pid,)).fetchone())


def check_property(conn, prop, site):
    """Look up one property and store the result. Raises TaxBlocked (and stores nothing) if the site pushes back."""
    ensure_schema(conn)
    pid = prop["id"]
    prev = conn.execute("SELECT * FROM property_taxes WHERE property_id = ?", (pid,)).fetchone()
    account = prev["account"] if prev and prev["account"] and (prev["manual"] or prev["status"] == "ok") else ""
    try:
        if not account:
            ours = parse_address(prop["address"])
            terms = search_terms(prop["address"])
            if not terms:
                return _save(conn, pid, prev, status="no_match", note="the address could not be read")
            near = []
            for term in terms:
                html_text = site.search(term)
                found = parse_results(html_text)
                if not found and not is_search_form(html_text):
                    raise TaxPageChanged("the search page did not look as expected")
                near = [c for c in found if same_street(ours, parse_address(c["site_address"]))]
                if near:
                    break
            if not near:
                return _save(conn, pid, prev, status="no_match", note="no tax account found at this address")
            real = [c for c in near if is_real_property(c["account"], c["legal"])]
            if len(real) != 1:
                return _save(conn, pid, prev, status="ambiguous", candidates=(real or near)[:12],
                             note="more than one account matches" if len(near) > 1 else "the only match does not look like a property account")
            account = real[0]["account"]
        detail = parse_detail(site.account_page(account))
        if detail["account"] and detail["account"] != account:
            raise TaxPageChanged("the account page was for a different account")
        return _save(conn, pid, prev, status="ok", account=account, detail=detail, manual=prev["manual"] if prev else 0)
    except TaxBlocked:
        raise
    except TaxError as e:
        return _save(conn, pid, prev, status="error", note=str(e)[:200])


def set_account(conn, pid, account, site):
    """A person chose the account (or pasted its number). Reads that account page and remembers the choice."""
    ensure_schema(conn)
    prev = conn.execute("SELECT * FROM property_taxes WHERE property_id = ?", (pid,)).fetchone()
    detail = parse_detail(site.account_page(account))
    if detail["account"] and detail["account"] != account:
        raise TaxPageChanged("the account page was for a different account")
    return _save(conn, pid, prev, status="ok", account=account, detail=detail, manual=1)


# ============================================================ what we show and publish
def summary(row):
    if not row:
        return None
    r = dict(row)
    out = {"status": r["status"], "account": r["account"] or None, "manual": bool(r["manual"]), "note": r["note"] or None,
           "attempted_at": r["attempted_at"] or None}
    if r["status"] == "ok":
        out.update(tax_year=r["tax_year"], annual=r["annual_levy"], current_due=r["current_year_due"], back_owed=r["prior_years_due"],
                   total_due=r["total_due"], delinquent_after=r["delinquent_after"] or None, last_payment=r["last_payment"],
                   as_of=r["checked_at"] or None, link=TAX_PUBLIC_BASE + "showdetail2.jsp?can=" + r["account"])
    elif r["status"] == "ambiguous":
        out["candidates"] = json.loads(r["candidates_json"] or "[]")
    return out


def api_fields_map(conn, ids):
    """{property id: the flat tax fields published on every property record}"""
    ensure_schema(conn)
    out = {}
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    for r in conn.execute(f"SELECT * FROM property_taxes WHERE property_id IN ({marks})", list(ids)).fetchall():
        f = dict(EMPTY_TAX_FIELDS, tax_status=r["status"])
        if r["status"] == "ok":
            f.update(tax_account=r["account"], tax_link=TAX_PUBLIC_BASE + "showdetail2.jsp?can=" + r["account"],
                     taxes_annual=r["annual_levy"], taxes_back_owed=r["prior_years_due"], taxes_current_due=r["current_year_due"],
                     taxes_total_due=r["total_due"], taxes_year=r["tax_year"], taxes_delinquent_after=r["delinquent_after"] or None,
                     taxes_as_of=(r["checked_at"] or "")[:10] or None)
        out[r["property_id"]] = f
    return out


# ============================================================ the batch job
def due_properties(conn, limit):
    ensure_schema(conn)
    now = _utc_dt()
    iso = lambda d: d.isoformat(timespec="seconds")
    stale = iso(now - dt.timedelta(days=_cfg("TAX_REFRESH_DAYS", 30, int)))
    soft = iso(now - dt.timedelta(days=14))
    retry = iso(now - dt.timedelta(hours=12))
    return conn.execute(
        """SELECT p.id, p.address, p.city, p.zip
           FROM properties p LEFT JOIN property_taxes t ON t.property_id = p.id
           WHERE p.county = 'Bexar' AND p.source_layer = 'mortgage' AND COALESCE(p.address, '') != ''
             AND ( t.property_id IS NULL
                   OR (t.status = 'ok' AND t.checked_at < ?)
                   OR (t.status IN ('ambiguous', 'no_match') AND t.manual = 0 AND t.attempted_at < ?)
                   OR (t.status = 'error' AND ((t.tries < 4 AND t.attempted_at < ?) OR t.attempted_at < ?)) )
           ORDER BY (t.property_id IS NULL) DESC, COALESCE(t.attempted_at, '') ASC, p.year DESC, p.month DESC, p.id DESC
           LIMIT ?""", (stale, soft, retry, stale, int(limit))).fetchall()


def _begin_run(trigger):
    """Take the one-run-at-a-time lock (four workers all try; one wins)."""
    conn = get_conn()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        busy = conn.execute("SELECT id FROM tax_runs WHERE finished_at IS NULL AND heartbeat_at > ?",
                            ((_utc_dt() - dt.timedelta(minutes=15)).isoformat(timespec="seconds"),)).fetchone()
        if busy:
            conn.execute("ROLLBACK")
            return None
        conn.execute("UPDATE tax_runs SET finished_at = ?, stopped_reason = 'abandoned' WHERE finished_at IS NULL", (_utc(),))
        cur = conn.execute("INSERT INTO tax_runs (started_at, heartbeat_at, trigger) VALUES (?,?,?)", (_utc(), _utc(), trigger))
        conn.execute("COMMIT")
        return cur.lastrowid
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _processed_today(conn):
    day = _utc()[:10]
    return conn.execute("SELECT COALESCE(SUM(processed), 0) n FROM tax_runs WHERE started_at >= ?", (day,)).fetchone()["n"]


def run_batch(limit=None, trigger="manual", site=None, max_minutes=None, daily_cap=False):
    """Look up the properties that are due, politely. Returns what happened."""
    conn = get_conn()
    ensure_schema(conn)
    try:
        until = paused_until(conn)
        if until:
            return {"skipped": "paused until %s UTC (%s)" % (until, _state_get(conn, "pause_reason"))}
        cap = _cfg("TAX_BATCH", 250, int)
        limit = cap if limit is None else int(limit)
        if daily_cap:
            limit = min(limit, cap - _processed_today(conn))
        if limit <= 0:
            return {"skipped": "today's limit has been reached"}
        run_id = _begin_run(trigger)
        if not run_id:
            return {"skipped": "another run is in progress"}
        site = site or _site()
        deadline = time.time() + (max_minutes if max_minutes is not None else _cfg("TAX_MAX_MINUTES", 120, float)) * 60
        st = {"processed": 0, "ok": 0, "ambiguous": 0, "no_match": 0, "errors": 0}
        reason, streak = "done", 0
        for prop in due_properties(conn, limit):
            if time.time() > deadline:
                reason = "time limit reached"
                break
            try:
                res = check_property(conn, prop, site)
            except TaxBlocked as e:
                pause(conn, 6, str(e))
                reason = "blocked: " + str(e)
                break
            st["processed"] += 1
            st[{"ok": "ok", "ambiguous": "ambiguous", "no_match": "no_match"}.get(res["status"], "errors")] += 1
            streak = streak + 1 if res["status"] == "error" else 0
            conn.execute("UPDATE tax_runs SET heartbeat_at = ?, processed = ?, ok = ?, ambiguous = ?, no_match = ?, errors = ? WHERE id = ?",
                         (_utc(), st["processed"], st["ok"], st["ambiguous"], st["no_match"], st["errors"], run_id))
            conn.commit()
            if streak >= 5:
                pause(conn, 1, "five lookups in a row failed")
                reason = "five lookups in a row failed"
                break
        conn.execute("UPDATE tax_runs SET finished_at = ?, heartbeat_at = ?, stopped_reason = ? WHERE id = ?", (_utc(), _utc(), reason, run_id))
        conn.commit()
        return dict(st, run=run_id, stopped=reason, requests=getattr(site, "requests_made", None))
    finally:
        conn.close()


_scheduler_started = False


def _in_window(now=None):
    try:
        a, b = [int(x) for x in _cfg("TAX_WINDOW_UTC", "8-12").split("-")]
    except ValueError:
        a, b = 8, 12
    h = (now or _utc_dt()).hour
    return a <= h < b


def start_scheduler():
    """A background thread in every worker; the run lock lets exactly one of them work at a time."""
    global _scheduler_started
    if _scheduler_started or os.environ.get("TAX_AUTORUN", "1") == "0":
        return None
    _scheduler_started = True

    def loop():
        time.sleep(random.uniform(30, 120))
        while True:
            try:
                if _in_window():
                    c = get_conn()
                    try:
                        ready = not paused_until(c) and len(due_properties(c, 1)) > 0
                    finally:
                        c.close()
                    if ready:
                        run_batch(trigger="scheduled", daily_cap=True)
            except Exception as e:               # never let the thread die
                print("tax scheduler:", type(e).__name__, e, flush=True)
            time.sleep(300 + random.uniform(0, 60))
    t = threading.Thread(target=loop, daemon=True, name="tax-scheduler")
    t.start()
    return t


def init_taxes(app):
    conn = get_conn()
    ensure_schema(conn)
    conn.close()
    start_scheduler()


# ============================================================ endpoints
def _prop(conn, pid):
    return conn.execute("SELECT id, address, city, zip FROM properties WHERE id = ?", (pid,)).fetchone()


@taxes_bp.route("/intake/api/property/<int:pid>/taxes")
@require_intake
def get_taxes(pid):
    conn = get_conn()
    ensure_schema(conn)
    if not _prop(conn, pid):
        conn.close()
        return jsonify({"error": "not found"}), 404
    row = conn.execute("SELECT * FROM property_taxes WHERE property_id = ?", (pid,)).fetchone()
    out = {"taxes": summary(row), "paused_until": paused_until(conn), "autorun": os.environ.get("TAX_AUTORUN", "1") != "0",
           "search_url": TAX_PUBLIC_BASE + "index.jsp"}
    conn.close()
    return jsonify(out)


@taxes_bp.route("/intake/api/property/<int:pid>/taxes/check", methods=["POST"])
@require_intake
def check_now(pid):
    conn = get_conn()
    ensure_schema(conn)
    prop = _prop(conn, pid)
    if not prop:
        conn.close()
        return jsonify({"error": "not found"}), 404
    until = paused_until(conn)
    if until:
        conn.close()
        return jsonify({"error": "tax lookups are paused until %s UTC because the Tax Office site pushed back" % until}), 503
    row = conn.execute("SELECT attempted_at FROM property_taxes WHERE property_id = ?", (pid,)).fetchone()
    if row and row["attempted_at"] and row["attempted_at"] > (_utc_dt() - dt.timedelta(seconds=20)).isoformat(timespec="seconds"):
        conn.close()
        return jsonify({"error": "this property was just checked; give it a few seconds"}), 429
    try:
        res = check_property(conn, prop, _site())
    except TaxBlocked as e:
        pause(conn, 6, str(e))
        conn.close()
        return jsonify({"error": "the Tax Office site pushed back, so lookups are paused for 6 hours"}), 503
    conn.close()
    _log(request.intake_user["id"], pid, "taxes_check", res["status"])
    return jsonify({"taxes": res})


@taxes_bp.route("/intake/api/property/<int:pid>/taxes/account", methods=["POST"])
@require_intake
def choose_account(pid):
    account = re.sub(r"\D", "", str((request.get_json(silent=True) or {}).get("account") or ""))
    if len(account) != 12:
        return jsonify({"error": "enter the 12-digit tax account number"}), 400
    conn = get_conn()
    ensure_schema(conn)
    if not _prop(conn, pid):
        conn.close()
        return jsonify({"error": "not found"}), 404
    if paused_until(conn):
        conn.close()
        return jsonify({"error": "tax lookups are paused because the Tax Office site pushed back"}), 503
    try:
        res = set_account(conn, pid, account, _site())
    except TaxBlocked as e:
        pause(conn, 6, str(e))
        conn.close()
        return jsonify({"error": "the Tax Office site pushed back, so lookups are paused for 6 hours"}), 503
    except TaxError as e:
        conn.close()
        return jsonify({"error": "that account number did not return a tax record (%s)" % e}), 422
    conn.close()
    _log(request.intake_user["id"], pid, "taxes_account", account)
    return jsonify({"taxes": res})


@taxes_bp.route("/internal/taxes/run", methods=["POST"])
def run_now():
    """Start a batch now (secret-protected). Body: {"limit": 25}. Returns at once; the work continues in the background."""
    if not _secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    limit = (request.get_json(silent=True) or {}).get("limit")
    threading.Thread(target=run_batch, kwargs={"limit": limit, "trigger": "manual"}, daemon=True).start()
    return jsonify({"started": True, "limit": limit}), 202


@taxes_bp.route("/internal/taxes/status")
def status():
    if not _secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    conn = get_conn()
    ensure_schema(conn)
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM property_taxes GROUP BY status").fetchall()}
    total = conn.execute("SELECT COUNT(*) n FROM properties WHERE county = 'Bexar' AND source_layer = 'mortgage'").fetchone()["n"]
    out = {"paused_until": paused_until(conn), "pause_reason": _state_get(conn, "pause_reason") or None, "properties": total,
           "counts": counts, "never_checked": total - sum(counts.values()), "autorun": os.environ.get("TAX_AUTORUN", "1") != "0",
           "window_utc": _cfg("TAX_WINDOW_UTC", "8-12"), "daily_limit": _cfg("TAX_BATCH", 250, int),
           "recent_runs": [dict(r) for r in conn.execute("SELECT * FROM tax_runs ORDER BY id DESC LIMIT 5").fetchall()]}
    conn.close()
    return jsonify(out)


@taxes_bp.route("/internal/taxes/resume", methods=["POST"])
def resume_now():
    if not _secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    conn = get_conn()
    ensure_schema(conn)
    resume(conn)
    conn.close()
    return jsonify({"resumed": True})
