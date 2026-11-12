# Property taxes (from the Tax Assessor-Collector)

For every foreclosure notice's property, the service finds its tax account on the County Tax Assessor-Collector's public
search (https://bexar.acttax.com/act_webdev/bexar/index.jsp) and records the figures that account page shows.

| What we keep | Where it comes from on the account page |
|---|---|
| `taxes_annual` (and `taxes_year`) | "2026 Year Tax Levy" |
| `taxes_back_owed` | "Prior Year(s) Amount Due" (0 when that line is absent) |
| `taxes_current_due` | "2026 Year Amount Due" |
| `taxes_total_due` | "Total Amount Due" (the red line) |
| `taxes_delinquent_after` | "Delinquent After" |
| `tax_account`, `tax_link` | the 12-digit account number, and the permanent link to its page |
| `taxes_as_of` | the day we read it. Amounts include penalty and interest and **change over time** |

We read the labelled lines, not the colour. Owner names and mailing addresses appear on those pages; they are read past
and **never stored**.

## How it runs

* **Overnight, automatically.** Every worker runs a small scheduler; a database lock lets exactly one do the work.
  Default window 08:00-12:00 UTC (3-7 AM Central), at most 250 properties a day, newest notices first, each re-checked at most
  every 30 days. A property whose account is known needs one request, otherwise two (search, then the account page).
* **By staff, one property at a time.** The property page has a **Tax documents** table (category 4) with a **Property taxes** card
  inside it, with *Check now* / *Check again*.
  A property with several matching accounts lists them for a person to choose (business personal-property accounts are
  set aside automatically); a person can also type an account number. Their choice is remembered.

## How gentle it is (the site publishes no robots.txt or terms, so these are our own limits)

* one request at a time, at least `TAX_MIN_INTERVAL` seconds apart (default 3, plus up to 1s of jitter), **shared by every
  worker**; measured with 4 gunicorn workers and 8 simultaneous lookups, no two requests were closer than the minimum
* only our own properties, and only the two pages a person would open
* it **stops at the first sign of being blocked** (HTTP 401/403/407/429/503, a CAPTCHA or "access denied" page, a redirect to
  another website) and pauses for 6 hours. It never works around a CAPTCHA or a block. Five failures in a row pause it for an hour
* it identifies itself in the User-Agent; set `TAX_CONTACT` to an address the Tax Office can reach

## Settings (Render environment variables; all optional)

| Variable | Default | Meaning |
|---|---|---|
| `TAX_AUTORUN` | `1` | `0` turns the nightly job off completely (staff can still check one property) |
| `TAX_WINDOW_UTC` | `8-12` | hours (UTC) in which the nightly job may run |
| `TAX_BATCH` | `250` | most properties looked up per day by the nightly job |
| `TAX_MIN_INTERVAL` | `3` | seconds between requests |
| `TAX_REFRESH_DAYS` | `30` | how long before a property is re-checked |
| `TAX_MAX_MINUTES` | `120` | longest a single run may last |
| `TAX_CONTACT` | this site's address | put your email here: it goes in the User-Agent |
| `TAX_BASE_URL` | the Tax Office site | for testing against a stand-in |

## Operating it (use the same secret as the other `/internal/` calls)

```
curl -H "X-Sync-Secret: $SECRET" https://reix-foreclosures-phk9.onrender.com/internal/taxes/status            # counts, pause, recent runs
curl -X POST -H "X-Sync-Secret: $SECRET" -H "Content-Type: application/json" -d '{"limit": 25}' \
     https://reix-foreclosures-phk9.onrender.com/internal/taxes/run                                          # start a run now
curl -X POST -H "X-Sync-Secret: $SECRET" https://reix-foreclosures-phk9.onrender.com/internal/taxes/resume   # clear a pause
```

## Outcomes per property

`ok` (figures read) | `ambiguous` (several real-property accounts; a person chooses) | `no_match` (no account at that address)
| `error` (page unreadable or site unreachable; retried after 12 hours, up to 4 times, then monthly). A failed refresh keeps
the last good figures and says why.

## Limits to know about

* The Tax Office's search wants a simplified address ("1234 Main", not "1234 Main Street"); we format it that way and, for
  directional streets, retry without the direction. Odd addresses can come back `no_match`.
* If the Tax Office changes its page layout we report `error` rather than guess.
* We cannot promise the Tax Office will keep allowing this. If it ever asks us to stop, set `TAX_AUTORUN=0`. Asking the
  office about a bulk tax-roll file (an Open Records Request) would be the more permanent source.

## API

Every property record (`/api/v1/properties`, the public list, the monthly CSVs) carries `tax_status`, `tax_account`,
`tax_link`, `taxes_annual`, `taxes_back_owed`, `taxes_current_due`, `taxes_total_due`, `taxes_year`,
`taxes_delinquent_after` and `taxes_as_of`. The change feed has a `taxes_updated` event (with `taxes_annual`,
`taxes_back_owed`, `taxes_total_due`) when figures are new or change. See REIX_SYNC.md.

## Tests

`python3 tools/test_taxes.py` and `python3 tools/test_taxes_batch.py` run against a stand-in Tax Office site
(`tools/fake_tax_site.py`) that mirrors the real page structure, including simulated blocking.
