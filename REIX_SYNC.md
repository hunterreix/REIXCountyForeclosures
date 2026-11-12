# Keeping the REIX app in step with the foreclosure service

This is for whoever builds the app.reix.co side.

## The scenario this is designed for
- **Day 1** - a notice comes in. We give you the property (its address, notice number, etc.).
- **Day 2 (or later)** - a person gathers the notice and the deed of trust (and any other documents) for that
  property and uploads them to us.
- You need those documents to attach to **the property you already have**, without guessing which
  one it is, and without re-downloading everything.

## The three ideas
1. **`property_key`** - a permanent id for each notice, e.g. `bexar-mortgage-20261000380`.
   It is in every property record. Store it with the property when you first import it. It never
   changes, even if the county later drops the notice from its feed.
2. **The change feed** - `GET /api/v1/changes?after=<cursor>` returns what changed, oldest first.
   Each event has an increasing `id`. Store the last id you processed and send it next time.
3. **Documents by key** - `GET /api/v1/properties/<property_key>/documents` returns the documents on file for
   that property, the facts read from each, and a download link for each PDF.

## Permissions
- Any API key can read properties and the change feed.
- **Documents and PDFs need the `documents` permission**, which an admin grants to your key
  (`POST /internal/api-keys/scopes` with the admin secret). Ordinary keys get a 403 on those two endpoints.
- Keys are created by a signed-in account: `POST /auth/login`, then `POST /api/v1/keys`
  (see `/docs`). Send the key in the `X-API-Key` header. PDF downloads accept the header only.

## Event types
| type | meaning |
|---|---|
| `property_added` | a new notice appeared in the county feed |
| `document_added` | a document was uploaded (`file_id` says which) |
| `document_removed` | a document was removed or replaced (`file_id`); stop using it |
| `document_none_found` | someone searched and marked the deed of trust as not found |
| `deeds_updated` | ownership-transfer history (from the appraisal district) was added or changed |
| `document_updated` | staff corrected the details read from a document (loan #, dates, names...). The PDF is unchanged: re-read `facts` from the documents endpoint (`file_id` says which) |
| `taxes_updated` | the property's tax figures were read for the first time or changed; `detail` has `taxes_annual`, `taxes_back_owed`, `taxes_total_due` |
| `sale_date_set` | the notice's real sale date was confirmed (or withdrawn), which can move it to another monthly list. `detail` says which: `{"sale_date", "list_month", "previous_list_month"}` |

## Monthly lists (how notices are grouped)
Notices are organised into **monthly lists**: every notice for one *sale* month, named `YYYY-MM`.

| endpoint | what it gives you |
|---|---|
| `GET /api/v1/lists` | every month with counts: `total`, `confirmed`, `provisional`, `mortgage`, `tax`, `docs_complete`, first/last sale date |
| `GET /api/v1/lists/2026-11` | that month's notices, paged (`page`, `per_page`), same fields as `/api/v1/properties` plus a `list` summary |
| `GET /api/v1/lists/2026-11?format=csv` | the whole month as a CSV file |

Each notice says how it got into its list:
- **`list_basis: "sale_date"` (confirmed)**: the notice document was processed, so `sale_date` is real.
- **`list_basis: "county_cycle"` (provisional)**: we only know the county's filing cycle (`cycle`, e.g. `2026-10`), which is
  usually but not always the sale month. `sale_date` is `null`.

**A notice can move lists.** When a provisional notice is processed and its real sale date is in a different month, it
moves, and a `sale_date_set` event tells you the new and previous list. Treat a provisional list as "probably complete,
still settling" and a confirmed one as final. Tax notices are not processed by staff, so they stay provisional.

You can also filter any property request with `list_month=2026-11` and `basis=confirmed|provisional`.

## Reference client
`tools/reix_sync_example.py` is a small working example of everything below: first import of the monthly lists,
following the change feed, moving a notice when its sale date moves it, and pairing late documents with a property
by `property_key` (verifying each PDF's checksum). Copy it as a starting point.

## The loop
```python
cursor = load_saved_cursor()                       # 0 the first time
while True:
    r = get(f"/api/v1/changes?after={cursor}&limit=100").json()
    for e in r["events"]:
        key = e["property_key"]
        if e["type"] == "property_added":
            props = get(f"/api/v1/properties?property_key={key}").json()["results"]
            create_property_in_reix(props[0])      # store property_key on it
        elif e["type"] == "sale_date_set":
            move_to_list(key, e["detail"]["list_month"], e["detail"]["sale_date"])   # the notice changed monthly list
        elif e["type"] in ("document_added", "document_removed", "document_none_found", "document_updated"):
            docs = get(f"/api/v1/properties/{key}/documents").json()   # needs 'documents' permission
            sync_documents(key, docs)              # attach to the property that has this property_key
    cursor = r["next_after"]; save_cursor(cursor)  # only after the events were handled
    if not r["has_more"]:
        break                                       # poll again later (every few minutes is plenty)
```

## First-time import
Capture `latest_id` from `/api/v1/changes` **before** you list anything, then read `/api/v1/lists` and page through
each month (or through `/api/v1/properties`), import everything, and start the loop above from the `latest_id` you captured.
Nothing is missed, and anything that changed during your import is picked up by the loop.

## Documents endpoint, in detail
`GET /api/v1/properties/<property_key>/documents` returns:
```json
{
  "property_key": "bexar-mortgage-20261000380",
  "docs_status": {"notice": "done", "deed_of_trust": "done"},
  "files": [{
    "file_id": 12, "role": "deed_of_trust", "role_label": "Deed of Trust", "subtype": null,
    "status": "uploaded", "doc_number": "20190102655", "number_matches_expected": true,
    "facts": {"dated": "2019-05-24", "borrower": "NES SAYAG", "lender": "First United Bank and Trust Co."},
    "size_bytes": 482113, "sha256": "…", "uploaded_at": "2026-11-07T15:02:11",
    "download_url": "/api/v1/files/12"
  }]
}
```
- `docs_status` per document type is `missing`, `done` or `none_found`.
- **Make your import idempotent**: a file is identified by `file_id` (and its `sha256`). A replaced document
  appears as `document_removed` for the old `file_id` and `document_added` for the new one.
- `facts` are read from the document by our staff and are suggestions confirmed by a person. They contain
  names and loan amounts: treat them as sensitive.
- `number_matches_expected: false` means the number stamped on the PDF did not match the number it should
  have; the uploader was warned. Show it for review rather than trusting it blindly.

## Additional documents (optional extras)
Staff can attach any number of extra documents to a property (liens, HOA or tax papers, title work...).
- They appear in the documents list with `"role": "other_doc"` and a `facts.title` such as "HOA lien". They download
  like any other file.
- They **never** change `docs_status` (it has exactly `notice` and `deed_of_trust`) or `docs_complete`.
- Every property record carries `docs_extra`, the number of additional documents on file.
- Each upload is a `document_added` event, and a removal is `document_removed`, exactly like the required documents.
- The Deed of Trust facts now include `loan_number` and `maturity_date` (the loan end / payoff date); `trustee` is the
  "DOT Lender Trustee"; and `borrower` is the Grantor(s) / Borrower(s) named on the deed of trust.
- **A property is complete when the notice and the deed of trust are in.** There is no separate loan-document step any more:
  assignments, notes, liens and releases are simply additional documents (`role: "other_doc"`, with an optional
  `subtype` of `assignment`, `note`, `lien`, `release` or `other`, and optional `assignor`, `assignee`, `references`
  and `principal` in `facts`). Earlier loan documents were converted automatically; you will have seen a
  `document_updated` event for each one. `docs_status` no longer contains `loan_doc`.

## Also useful
Every property record now includes `docs_status`, `docs_complete` and `property_key`, so you can show "documents
pending" without calling the documents endpoint.

## Lien position, document names, courthouse links

- **`lien_position`** (on every property record, and a column in the monthly CSV): `"first"` or `"subordinate"`, read from the
  deed of trust and confirmed by staff (required for every new deed of trust). `null` until a deed has been processed.
- **`back_tax_amount_owed`** (same places): the amount owed on the property's Back Tax Report (for example `"12,345.67"`), or
  `null`. If there are several reports, the most recent upload.
- **Documents come in four categories** (the `role` of each file): `notice`, `deed_of_trust` (the two required ones),
  and two optional categories, `other_doc` (additional documents) and `tax_doc` (tax documents). In the documents endpoint,
  `subtype` is the code and `document_name` the label. Each name belongs to exactly one category:

  | `role` | `subtype` | `document_name` |
  |---|---|---|
  | `other_doc` | `additional_deed_of_trust` | Additional Deed of Trust |
  | `other_doc` | `modification` | Modification (only the most recent is kept) |
  | `other_doc` | `lis_pendens` | Lis Pendens |
  | `other_doc` | `judgment` | Judgment |
  | `other_doc` | `other_lien` | Other Lien |
  | `other_doc` | `affidavit` | Affidavit |
  | `other_doc` | `notice` | Notice |
  | `other_doc` | `city_lien` | City Lien |
  | `tax_doc` | `back_tax_report` | Back Tax Report |
  | `tax_doc` | `tax_lien` | Tax Lien |
  | `tax_doc` | `irs_lien` | IRS Lien |
  | `tax_doc` | `federal_tax_lien` | Federal Tax Lien |

  Property records have `docs_extra` (additional documents) and `docs_tax` (tax documents) counts; neither affects
  `docs_status` or `docs_complete`. Back tax reports and tax liens saved earlier as additional documents were moved to
  `tax_doc` automatically (you will have seen a `document_updated` event for each). On a finished property, `doc_facts`
  has `other_docs` and `tax_docs` lists.
  Documents saved before this list existed may still carry an old `subtype` (`assignment`, `note`, `lien`, `release`,
  `other`) with `document_name: null`; they are corrected when staff next edit them (you will see a `document_updated` event).
- `facts` on a deed of trust now include `lien_position`; `facts` on an additional document include `amount` (the amount
  owed, or the amount of the lien or judgment) where known.
- **Document numbers.** Every uploaded document carries its `doc_number`, except Back Tax Reports (which have no recording
  number; the amount owed is captured instead).
- **`courthouse_url`** on each file (and on each `doc_files` entry in the public API): a link back to the record at the County
  Clerk. For the notice it is the Clerk's foreclosure search for that number. For every other document it is the document
  page address staff paste from the Clerk's site (`https://bexar.tx.publicsearch.us/doc/<id>`), or `null` if none was pasted.
  Only addresses on that exact host and path are accepted. We did not find a link that works reliably from a recording number
  alone for recorded land records, which is why the pasted page address is what gives a dependable hotlink.

## Property taxes

Every property record carries the tax figures read from the County Tax Assessor-Collector's public search (see TAXES.md):
`tax_status` (`ok`, `ambiguous`, `no_match`, `error`, or `null` before the first check), `tax_account`, `tax_link` (the
account page), `taxes_annual` and `taxes_year`, `taxes_back_owed` (prior years), `taxes_current_due`, `taxes_total_due`,
`taxes_delinquent_after` and `taxes_as_of` (the day it was read). The figures are `null` unless `tax_status` is `ok`.
Amounts include penalty and interest and change over time, so treat `taxes_as_of` as part of the value. The monthly CSVs have
`tax_account`, `taxes_annual`, `taxes_back_owed`, `taxes_total_due`, `taxes_as_of` columns. A `taxes_updated` event is
published when figures are new or change.
