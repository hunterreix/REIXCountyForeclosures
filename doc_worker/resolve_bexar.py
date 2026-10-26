"""
doc_worker/resolve_bexar.py - Phase 1: link each Bexar foreclosure notice to
its recorded document on the County Clerk's site.

WHY A REAL BROWSER: bexar.tx.publicsearch.us only returns results when a search
is submitted through its own form (the same URL loaded cold returns "No
Results Found"), and a notice's direct link is /doc/<internal id> -- an id that
is NOT the instrument number and can only be read from the search result. So
this drives the site's form with a headless browser, exactly as a person would.

HOW IT RUNS: a scheduled GitHub Action (.github/workflows/docs.yml) runs this
script. It asks our app which notices still need a link
(GET /internal/documents/pending), resolves a small batch, and reports back
(POST /internal/documents). Our app stores links only, never document copies.

GOOD-CITIZEN RULES (do not remove):
  * identifies itself with its own User-Agent
  * honors robots.txt
  * small batches with a delay between lookups
  * if the site shows a CAPTCHA / block page, it STOPS -- it never tries to
    get around bot protection. A human should look at that.

SELECTORS: everything site-specific is in the SELECTORS block below. They were
written from a manual walkthrough of the site and have NOT yet been run
unattended -- the first run may need small selector tweaks. On any failure a
screenshot is saved to doc_worker/debug/ (uploaded as a workflow artifact).

Phase 2 (not built yet): read the notice image (OCR) to find the deed of trust
it cites, then find assignments/releases and other liens.
"""
import os
import re
import sys
import time
import datetime as dt
import urllib.robotparser

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

APP_URL = os.environ["REIX_APP_URL"].rstrip("/")
SECRET = os.environ["REIX_SYNC_SECRET"]
BATCH = int(os.environ.get("BATCH", "25"))
DELAY = float(os.environ.get("DELAY_SECONDS", "8"))

CLERK_HOME = "https://bexar.tx.publicsearch.us/"
USER_AGENT = "REIX-DocLinker/1.0 (+https://app.reix.co; links public notices to Clerk records)"
DEBUG_DIR = os.path.join(os.path.dirname(__file__), "debug")

# Verified against the live site on 3 real notices (see test notes in the
# project history). Note: "NOTICE OF ..." text also appears in the results
# page's left-hand filter panel, so the result row MUST be scoped to the table.
SELECTORS = {
    "close_tour": "[aria-label='Close']",                 # first-visit tour popup
    "dept_dropdown": "text=Land Records",                 # shows current department
    "dept_option_foreclosures": "Foreclosures",           # exact text of the option
    "search_box": "input[placeholder^='Search for grantor']",
    "no_results": "text=No Results Found",
    "result_rows": "tbody tr",                            # results table rows
    "row_doc_type_cell": "td.col-3",                      # clickable doc-type cell
    "doc_summary": "main",
}
BLOCK_MARKERS = ("captcha", "verify you are human", "access denied", "unusual traffic")


class Blocked(Exception):
    pass


def headers():
    return {"X-Sync-Secret": SECRET}


def robots_allows():
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(CLERK_HOME + "robots.txt")
    try:
        rp.read()
    except Exception:
        return True  # no readable robots.txt -> no stated restriction
    return rp.can_fetch(USER_AGENT, CLERK_HOME + "results") and rp.can_fetch(USER_AGENT, CLERK_HOME + "doc/1")


def get_pending():
    r = requests.get(f"{APP_URL}/internal/documents/pending",
                     params={"limit": BATCH}, headers=headers(), timeout=30)
    r.raise_for_status()
    return r.json()["pending"]


def report(property_id, status, documents=None, message=""):
    r = requests.post(f"{APP_URL}/internal/documents", headers=headers(), timeout=30,
                      json={"property_id": property_id, "status": status,
                            "documents": documents or [], "message": message})
    r.raise_for_status()


def check_blocked(page):
    text = (page.inner_text("body") or "").lower()
    if any(m in text for m in BLOCK_MARKERS):
        raise Blocked("Clerk site is showing a CAPTCHA/block page")


def snap(page, name):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    try:
        page.screenshot(path=os.path.join(DEBUG_DIR, f"{name}.png"))
    except Exception:
        pass


def parse_summary(text, expected_doc):
    """Reads the document page's summary panel."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    def after(label):
        for i, l in enumerate(lines):
            if l.lower().startswith(label.lower()):
                inline = l.split(":", 1)[1].strip() if ":" in l else ""
                return inline or (lines[i + 1] if i + 1 < len(lines) else "")
        return ""

    doc_number = after("Document Number")
    recorded = after("Recorded Date")
    doc_type = ""
    for i, l in enumerate(lines):
        if l.lower().startswith("document number") and i > 0:
            doc_type = lines[i - 1]
            break
    address = after("Property Address")
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", recorded)
    iso = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else None
    return {"doc_number": doc_number, "doc_type": doc_type, "recorded_date": iso,
            "address": address, "matches": doc_number == expected_doc}


def norm_addr(a):
    return re.sub(r"[^A-Z0-9 ]", "", (a or "").upper()).split()


def address_agrees(ours, clerks):
    """True when our street address appears inside the Clerk's address line
    (their line also carries city/state/zip). Empty on either side = unknown."""
    o, c = norm_addr(ours), norm_addr(clerks)
    if not o or not c:
        return None
    return " ".join(o) in " ".join(c)


def resolve_one(page, item):
    expected = item["doc_number"]
    page.goto(CLERK_HOME, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    check_blocked(page)

    # dismiss the first-visit tour popup if it is showing
    try:
        page.locator(SELECTORS["close_tour"]).first.click(timeout=3000)
    except Exception:
        pass

    # department -> Foreclosures
    page.locator(SELECTORS["dept_dropdown"]).first.click(timeout=10000)
    page.get_by_text(SELECTORS["dept_option_foreclosures"], exact=True).first.click(timeout=10000)

    # search by instrument number
    box = page.locator(SELECTORS["search_box"]).first
    box.click(timeout=10000)
    box.fill(expected)
    box.press("Enter")

    # wait for either a result or the empty-state message
    page.wait_for_selector(f"{SELECTORS['result_rows']}, {SELECTORS['no_results']}", timeout=30000)
    check_blocked(page)
    if page.locator(SELECTORS["no_results"]).count():
        return "not_found", [], "Clerk search returned no results"

    # open the row for THIS doc number (scoped to the results table)
    row = page.locator(SELECTORS["result_rows"], has_text=expected).first
    row.locator(SELECTORS["row_doc_type_cell"]).first.click(timeout=10000)
    page.wait_for_url(re.compile(r"/doc/\d+"), timeout=30000)
    page.wait_for_selector("text=Document Number", timeout=30000)

    info = parse_summary(page.inner_text(SELECTORS["doc_summary"]), expected)
    if not info["matches"]:
        return "not_found", [], f"opened doc but number mismatch ({info['doc_number']!r})"

    agrees = address_agrees(item.get("address"), info.get("address"))
    if agrees is False:
        # same doc number but a different property: do not link, flag for a human
        return "not_found", [], (f"doc number matched but address differs: "
                                 f"ours={item.get('address')!r} clerk={info.get('address')!r}")
    method = "clerk_search_by_doc_number" + ("+address_verified" if agrees else "")
    doc = {"doc_role": "notice", "doc_number": expected, "doc_type": info["doc_type"],
           "recorded_date": info["recorded_date"], "url": page.url.split("?")[0],
           "match_method": method, "confidence": "high" if agrees else "medium"}
    return "resolved", [doc], "ok"


def main():
    if not robots_allows():
        print("robots.txt disallows this path for our user agent - stopping.")
        return 0
    pending = get_pending()
    print(f"{len(pending)} notice(s) to resolve (batch cap {BATCH})")
    if not pending:
        return 0

    resolved = failed = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        for n, item in enumerate(pending, 1):
            tag = f"{item['doc_number']}"
            try:
                status, docs, msg = resolve_one(page, item)
                report(item["id"], status, docs, msg)
                print(f"[{n}/{len(pending)}] {tag}: {status} - {msg}")
                resolved += status == "resolved"
            except Blocked as e:
                snap(page, f"blocked_{tag}")
                print(f"STOPPING: {e}. Not retrying; check the site manually.")
                browser.close()
                return 2
            except (PWTimeout, Exception) as e:
                snap(page, f"error_{tag}")
                failed += 1
                try:
                    report(item["id"], "error", [], f"{type(e).__name__}: {str(e)[:200]}")
                except Exception:
                    pass
                print(f"[{n}/{len(pending)}] {tag}: ERROR {type(e).__name__}: {str(e)[:160]}")
            time.sleep(DELAY)
        browser.close()
    print(f"Done. resolved={resolved} failed={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
