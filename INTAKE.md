# Document Intake tool (human-in-the-loop)

A person opens each notice's documents on the County Clerk's site, downloads the PDF, and
uploads it here. The tool reads the PDF **in the person's browser**, shows what it found for
them to confirm, stores the PDF with the property, and tells them which document to find next:

    1. Foreclosure notice  ->  2. Deed of Trust (named in the notice)          <- the two REQUIRED documents
    Additional documents: assignments, notes, liens, releases, HOA or tax papers, title work...
       (optional: any number, each with a short title and an optional type; never required)

Each kind of document has its own table on the property page with its own button, and the steps bar is
clickable, so any of them can be filled in at any time (for example, an additional document can be added
before the notice or deed is in).

Open it at `/intake` (e.g. https://reix-foreclosures-phk9.onrender.com/intake).

## Accounts and roles
Everyone signs in; there is no anonymous access to the list, map, summary or API keys any more.
| role | can do |
|---|---|
| `viewer` | use the foreclosure list, map and summary; create an API key |
| `intake` | everything a viewer can, plus the document-intake tool |
| `admin` | everything an intake user can (reserved for future admin screens) |

Accounts are created by an admin (below). If someone has a viewer account and opens `/intake`, they see a
"no access" page, not the tool.

## One-time setup
1. Deploy the code (see the files list at the bottom).
2. Make accounts (needs your `REIX_SYNC_SECRET`; password 10+ characters; `role` is `viewer`, `intake` or `admin`):

       curl -X POST https://YOUR-APP/internal/intake-users \
         -H "X-Sync-Secret: YOUR_SECRET" -H "Content-Type: application/json" \
         -d '{"email":"worker@reix.co","name":"Dana","password":"a-long-password","role":"intake"}'

   Run it again with a new password to reset one, change `role`, or add `"active": false` to switch an account off.
3. **Make the Render disk bigger.** PDFs are saved on the persistent disk (`/var/data/docs`).
   Your disk is 1 GB; all current Bexar notices x 3 documents is roughly 2 GB. In Render:
   service -> Disks -> increase the size (it can only grow). The work-list page shows free space
   and turns red under 200 MB.

Sessions are signed with a key derived from `REIX_SYNC_SECRET`, so no new setting is required
(set `REIX_SESSION_SECRET` if you prefer a separate one).

## How a person uses it
1. Sign in. The list shows every Bexar mortgage notice and which of the three documents is on
   file. Filter by month or by what is missing; search by address or doc #.
2. Click **Work on this**. The top card says what to do next and gives the number to look up
   (with Copy buttons and a link to the Clerk's site).
3. Download the PDF from the Clerk's site, choose it under **Upload a document**, wait for it to be
   read, **check the values** (empty boxes are outlined), and press **Save**.
   - The page compares the document number printed on the PDF with the number it should be and
     shows a green "Matches" or a red "Does NOT match".
4. After the notice is saved, the deed of trust's number, dates, borrower and lender are filled in
   for step 2 automatically. After the deed, the borrower is carried into step 3.
5. If a document truly doesn't exist, use **Not found / None found** and add a note.

## What is stored, and who can see it
- The PDF and the confirmed fields are stored per property, with who uploaded it and when.
  Removing a file hides it but keeps the PDF on disk for the audit trail.
- PDFs and these fields are **only** available to signed-in intake users. The public API and
  the public web app never show them.
- Nothing is sent to any outside service. (The browser downloads two reading libraries,
  PDF.js and Tesseract.js, from public CDNs, so the intake person's computer needs internet.)

## Known limits
- Reading scanned documents (OCR) is slow (a few seconds per page) and imperfect. Treat every
  filled-in value as a suggestion. PDFs that already contain text are read instantly.
- Only the first few pages are read (notice 4, deed of trust 2, additional documents 2).
- The REIX app receives documents and facts through the API, not through this page: see `REIX_SYNC.md`.

## Files
`intake.py` (sign-in, roles, intake server), `events.py` (change log), `sync_api.py` (REIX app interface), `static/intake.html` (page), `static/intake_parse.js` (reads OCR text),
three lines in `app.py` that switch it on, `.gitignore`, `tools/test_intake_parse.js` (parser tests).

## What every document needs before it can be saved

| Document | Required | Also collected |
|---|---|---|
| Foreclosure notice | (its own number is filled in) | sale date, Deed of Trust number, borrower, lender, courthouse page address |
| Deed of Trust | **Lien position (1st lien or Subordinate lien)**, **document number** | grantor, lender, trustee, loan #, loan end date, amount, courthouse page address |
| Additional document | **Document name** (pick from the list), **document number** | recorded date, amount, courthouse page address, parties, notes |
| Tax document | **Document name** (Back Tax Report, Tax Lien, IRS Lien or Federal Tax Lien), **document number** | recorded date, amount, taxing authority, courthouse page address, notes |
| Tax document: Back Tax Report | **Document name**, **amount owed** (no document number needed) | notes |

**The four categories on every property page:** 1. Foreclosure notice, 2. Deed of Trust, 3. Additional documents,
4. Tax documents. The first two are required; the last two are optional and can be filled in at any time.

**Additional document names:** Additional Deed of Trust, Modification, Lis Pendens, Judgment, Other Lien, Affidavit, Notice,
City Lien. For a Modification only the most recent one is needed: saving a newer one offers to replace the one on file.

**Tax document names:** Back Tax Report, Tax Lien, IRS Lien, Federal Tax Lien. The **Property taxes** card (annual tax, back
taxes, total due, read from the Tax Assessor-Collector) sits inside the Tax documents table. If you upload a document that looks
like the other kind, the form switches category for you.

**Courthouse page address (recommended on every document):** open the document on the Clerk's site, copy the address from your
browser (it looks like `bexar.tx.publicsearch.us/doc/123456789`) and paste it into the form. Everyone who can see the document
then gets a "Courthouse record" link straight back to it. The notice's link is built automatically.

Documents saved before these rules still open and still count; they show a warning badge ("lien position missing",
"document # missing", "choose a document name") and are fixed with **Edit details**.
