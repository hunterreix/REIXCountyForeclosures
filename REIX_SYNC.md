# Keeping the REIX app in step with the foreclosure service

This is for whoever builds the app.reix.co side.

## The scenario this is designed for
- **Day 1** - a notice comes in. We give you the property (its address, notice number, etc.).
- **Day 2 (or later)** - a person gathers the notice, deed of trust and loan documents for that
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
| `document_none_found` | someone searched and marked the deed of trust or loan document as not existing |
| `deeds_updated` | ownership-transfer history (from the appraisal district) was added or changed |

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
        elif e["type"] in ("document_added", "document_removed", "document_none_found"):
            docs = get(f"/api/v1/properties/{key}/documents").json()   # needs 'documents' permission
            sync_documents(key, docs)              # attach to the property that has this property_key
    cursor = r["next_after"]; save_cursor(cursor)  # only after the events were handled
    if not r["has_more"]:
        break                                       # poll again later (every few minutes is plenty)
```

## First-time import
Capture `latest_id` from `/api/v1/changes` **before** you list properties, then page through
`/api/v1/properties`, import everything, and start the loop above from the `latest_id` you captured.
Nothing is missed, and anything that changed during your import is picked up by the loop.

## Documents endpoint, in detail
`GET /api/v1/properties/<property_key>/documents` returns:
```json
{
  "property_key": "bexar-mortgage-20261000380",
  "docs_status": {"notice": "done", "deed_of_trust": "done", "loan_doc": "none_found"},
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

## Also useful
Every property record now includes `docs_status`, `docs_complete` and `property_key`, so you can show "documents
pending" without calling the documents endpoint.
