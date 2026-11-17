"""
syncheck.py - Two safety nets so the REIX app never silently falls behind.

1. FINGERPRINT (details_hash). A short code for everything REIX shows about a property: its address and
   sale date, the lien position, back taxes and tax figures, and every document on file (and what was read from
   it). If anything changes, the code changes. REIX stores the code with the property; on any later look, a
   different code means "pull this property again". It works even if REIX missed events or lost its place.

2. CHECKER (reconcile). Compares what is on file with what the change feed has announced and announces anything
   that was never announced (a document with no event, a removal with no event, tax figures with no event).
   It runs by itself every few hours and on demand (POST /internal/sync/check). Safe to run any time and
   as often as you like: when nothing is missing it does nothing.
"""
import datetime as dt
import hashlib
import json
import os
import random
import threading
import time

from db import get_conn
from events import ensure_schema, log_event
from taxes import api_fields_map

_PROP_COLS = ("address", "city", "zip", "school_dist", "year", "month", "lat", "lon", "doc_link", "sale_date")
_TAX_KEYS = ("tax_status", "tax_account", "taxes_annual", "taxes_back_owed", "taxes_current_due",
             "taxes_total_due", "taxes_year", "taxes_delinquent_after")      # not taxes_as_of: it moves on every check


def fingerprints(conn, ids):
    """{property id: 16-character code}. Changes whenever anything REIX shows for the property changes."""
    ids = list(ids)
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    props = {r["id"]: r for r in conn.execute(
        f"SELECT id, {','.join(_PROP_COLS)} FROM properties WHERE id IN ({marks})", ids).fetchall()}
    files = {}
    for r in conn.execute(
            f"SELECT property_id, id, doc_role, doc_subtype, status, doc_number, sha256, fields_json FROM property_files "
            f"WHERE property_id IN ({marks}) AND status != 'removed' ORDER BY id", ids).fetchall():
        try:
            facts = json.loads(r["fields_json"] or "{}")
        except ValueError:
            facts = {}
        files.setdefault(r["property_id"], []).append(
            [r["id"], r["doc_role"], r["doc_subtype"] or "", r["status"], r["doc_number"] or "", r["sha256"] or "", facts])
    taxes = api_fields_map(conn, ids)
    out = {}
    for pid in ids:
        p = props.get(pid)
        if not p:
            continue
        t = taxes.get(pid) or {}
        blob = {"p": [p[c] if not isinstance(p[c], float) else round(p[c], 6) for c in _PROP_COLS],
                "f": files.get(pid, []), "t": [t.get(k) for k in _TAX_KEYS]}
        out[pid] = hashlib.sha256(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return out


def reconcile(conn):
    """Announce anything on file that the change feed never announced. Returns a report."""
    ensure_schema(conn)
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")          # one worker at a time, so two never announce the same thing twice
    try:
        repaired = {"documents": 0, "removals": 0, "taxes": 0}
        announced = {(r["ref_id"], r["event_type"]) for r in conn.execute(
            "SELECT ref_id, event_type FROM property_events WHERE ref_id IS NOT NULL").fetchall()}
        had_doc_event = {ref for ref, t in announced if t in ("document_added", "document_updated", "document_none_found")}
        had_removal = {ref for ref, t in announced if t == "document_removed"}
        for f in conn.execute("SELECT id, property_id, doc_role, status FROM property_files").fetchall():
            if f["status"] == "removed":
                if f["id"] not in had_removal:
                    log_event(conn, f["property_id"], "document_removed", f["id"], f["doc_role"] + " (checker)")
                    repaired["removals"] += 1
            elif f["id"] not in had_doc_event:
                kind = "document_none_found" if f["status"] == "none_found" else "document_added"
                log_event(conn, f["property_id"], kind, f["id"], f["doc_role"] + " (checker)")
                repaired["documents"] += 1
        has_tax_event = {r["property_id"] for r in conn.execute(
            "SELECT DISTINCT property_id FROM property_events WHERE event_type = 'taxes_updated'").fetchall()}
        for t in conn.execute("SELECT property_id, annual_levy, prior_years_due, total_due FROM property_taxes WHERE status = 'ok'").fetchall():
            if t["property_id"] not in has_tax_event:
                log_event(conn, t["property_id"], "taxes_updated", None, json.dumps(
                    {"taxes_annual": t["annual_levy"], "taxes_back_owed": t["prior_years_due"], "taxes_total_due": t["total_due"]}))
                repaired["taxes"] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    n = conn.execute("SELECT COUNT(*) c FROM property_files WHERE status = 'uploaded'").fetchone()["c"]
    return {"checked_at": dt.datetime.utcnow().isoformat(timespec="seconds"), "documents_on_file": n,
            "announced_by_checker": repaired, "ok": not any(repaired.values())}


_started = False


def start_checker():
    """Runs reconcile every few hours in a background thread (any worker; the lock inside reconcile keeps it tidy)."""
    global _started
    if _started or os.environ.get("SYNC_CHECK_AUTORUN", "1") == "0":
        return None
    _started = True
    hours = float(os.environ.get("SYNC_CHECK_HOURS", "6"))

    def loop():
        time.sleep(random.uniform(60, 240))
        while True:
            try:
                c = get_conn()
                try:
                    r = reconcile(c)
                finally:
                    c.close()
                if not r["ok"]:
                    print("sync checker announced:", r["announced_by_checker"], flush=True)
            except Exception as e:
                print("sync checker:", type(e).__name__, e, flush=True)
            time.sleep(hours * 3600 + random.uniform(0, 600))
    threading.Thread(target=loop, daemon=True, name="sync-checker").start()
    return True
