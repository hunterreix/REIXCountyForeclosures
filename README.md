# REIX Bexar & Collin County Foreclosure Feed

A small, self-contained system that turns Texas county foreclosure notices
into (1) a free API and (2) a simple search/list/map web app — both
designed as a lead-gen "hook" pointing back to **https://app.reix.co/**.

**Want to put this online right now?** See [`DEPLOY.md`](DEPLOY.md) for a
step-by-step GitHub → Render walkthrough.

## Multi-county architecture

Each county is a self-contained adapter under `sources/`. The DB, API, and
web app are all county-agnostic — `county` is just another field/filter
everywhere. Adding a new county later is additive:

```
sources/
  base.py       - the CountySource interface every adapter implements
  bexar.py      - Bexar County (ArcGIS FeatureServer, JSON API)
  collin.py     - Collin County (structured HTML, no JSON API found)
  registry.py   - the list of counties currently wired in
```

## Which counties are in, and why

I surveyed the largest Texas counties plus the statewide tax-sale
aggregators before building anything, specifically looking for **easy**
sources — a real API or at minimum a consistent, script-friendly page.
Most counties don't have one:

| County | System | Verdict |
|---|---|---|
| **Bexar** | ArcGIS FeatureServer, clean JSON, no scraping | ✅ Built — easiest tier |
| **Collin** | Dedicated county web app, filterable, server-rendered HTML | ✅ Built — structured but no JSON API |
| Harris | No open feed; market served by paid data brokers (TaxNetUSA, etc.) | ❌ Skipped |
| Dallas | Only general parcel/GIS data, nothing foreclosure-specific | ❌ Skipped |
| Tarrant | Open data portal exists, no foreclosure layer | ❌ Skipped |
| Statewide tax-sale aggregator (`taxsales.lgbs.com`, ~150+ counties) | Click-through JS app, no public API, anti-scraping friction | ❌ Skipped |

More counties can be added as they're identified — just say the word and
I'll go survey the next batch (smaller counties served by regional GIS
consortiums are a promising next lead).

## How it fits together

```
County sources (sources/*.py)  --->  scraper.py  --->  SQLite DB  --->  app.py (Flask)
                                      (scheduled)                        |
                                                                          +--> /api/public/*  (used by static/index.html)
                                                                          +--> /api/v1/*      (external developers, API key)
                                                                          +--> /              (the web app itself)
                                                                          +--> /docs          (developer docs)
                                                                          +--> /internal/sync (secret-protected, for schedulers)
```

## Bexar County data source detail

- **Primary, structured, already-an-API — no scraping required:**
  `https://maps.bexar.org/arcgis/rest/services/CC/ForeclosuresProd/MapServer`
  - Layer `0` = Mortgage foreclosures, Layer `1` = Tax foreclosures
  - Public, no key needed, supports JSON/GeoJSON, pagination, and returns
    coordinates already converted to lat/lon (`outSR=4326`).

- **Secondary / good for cross-checks later, not scraped:**
  - `bexar.org/DocumentCenter/View/505/...` and its mirror on
    `gis-bexar.opendata.arcgis.com` are the same monthly **PDF** notice
    list — useful as a manual sanity check, not needed as a primary source.
  - `bexar.tx.publicsearch.us` is the County Clerk's official records
    search — the right place to eventually resolve a **doc number → the
    actual recorded document image**. I couldn't find a stable deep-link
    pattern, so `doc_link` currently points at a pre-filled search query
    as a placeholder (see `DOC_LINK_TEMPLATE` in `sources/bexar.py`).

## Collin County data source detail

`https://apps2.collincountytx.gov/ForeclosureNotices` — a county-built web
app, not a documented API, but the base page is server-rendered so plain
`requests` + BeautifulSoup can read it. Two things flagged in
`sources/collin.py` for when you have a real browser handy:
- Only page 1 is scraped right now (~25-50 most recent notices). The page
  numbers render as an interactive grid, which usually means there's a
  clean JSON endpoint underneath — open DevTools → Network on the live
  site to find it, and swap it in for full pagination + more reliable parsing.
- The real county document number lives on each notice's detail page, not
  the list view, so `doc_number` for Collin is currently a synthetic ID
  (address + dates hash) until a detail-page fetch step is added.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python db.py               # create the SQLite schema
python scraper.py          # pull real data from all enabled counties (needs internet)
python scraper.py --county Bexar   # or just one county
python app.py               # run the API + web app on http://localhost:5000
```

Open `http://localhost:5000/` for the web app, `http://localhost:5000/docs`
for the API docs page.

> **Note on this sandbox:** I built and unit-tested everything here, but this
> environment has no outbound internet access, so I couldn't run `scraper.py`
> against the live county sites from inside it. I seeded `seed_demo_data.py`
> with sample rows across both counties so you can see the whole app working
> end-to-end right now. Once you run `scraper.py` anywhere with internet
> access, wipe the demo rows and it'll fill with live data:
> ```bash
> rm reix_foreclosures.db && python db.py && python scraper.py
> ```

## Keeping data fresh

- **Recommended (see DEPLOY.md):** a free GitHub Actions cron calls the
  secret-protected `POST /internal/sync` endpoint daily — no separate
  worker process or paid cron add-on needed.
- **Or, on your own server:** `0 6 * * * cd /path/to/app && venv/bin/python scraper.py`
- **Or, a built-in loop:** `python scraper.py --loop 21600` (re-syncs every 6 hours, forever)
- Every run is idempotent — records are upserted on `(county, doc_number, type)`,
  so re-running never creates duplicates, and `first_seen`/`last_seen`
  timestamps let you tell how long a notice has been active.

## The two-tier API

- **`/api/public/*`** — no key, used only by our own `static/index.html`. Keeps
  the web app friction-free for visitors.
- **`/api/v1/*`** — requires an `X-API-Key` header. Any outside team gets one
  instantly via `POST /api/v1/keys` — no approval queue, on purpose, since the
  whole point is to make it dead simple for other apps to pull this feed.
  Full docs live at `/docs` and are meant to be handed to a partner as-is.

## Web app (`static/index.html`)

Deliberately plain, per your ask — no dashboard chrome, just:
- **List** tab: search box (address/ZIP/doc #), county + type + city filters, paginated table
- **Map** tab: Leaflet + free OpenStreetMap tiles, color-coded mortgage vs. tax pins,
  auto-fits to whatever county/filter is active (note: Collin records don't have
  coordinates yet, so they show in List/Summary but not the map until geocoding is added)
- **Summary** tab: total counts, by-county and mortgage-vs-tax breakdowns, top cities, notices/month

Every row has a **"View on REIX"** button and the header/footer both link to
`https://app.reix.co/` — that's the hook. The Doc # column is already a link
(currently to each county's public records search) so swapping in the real
courthouse document viewer later is a one-line change per adapter.

## Deploying for real

See [`DEPLOY.md`](DEPLOY.md) for the full walkthrough. Short version: push to
GitHub, deploy the included `render.yaml` blueprint on Render (persistent
disk + auto-HTTPS), run one manual sync, then let the included GitHub Actions
workflow keep it fresh daily for free.

## Files

| File | Purpose |
|---|---|
| `sources/` | One adapter per county + the shared interface and registry |
| `db.py` | SQLite schema + connection helper |
| `scraper.py` | Loops over enabled sources, upserts into the DB |
| `seed_demo_data.py` | Sample rows across both counties for local testing |
| `app.py` | Flask API (`/api/public/*`, `/api/v1/*`, `/internal/sync`) + serves the web app |
| `static/index.html` | The list/map/summary web app |
| `static/docs.html` | Developer-facing API docs |
| `requirements.txt` | Flask, requests, beautifulsoup4, gunicorn |
| `Procfile`, `render.yaml` | Production deploy config |
| `.github/workflows/sync.yml` | Free daily data refresh via GitHub Actions |
| `DEPLOY.md` | Step-by-step GitHub → Render deployment guide |

