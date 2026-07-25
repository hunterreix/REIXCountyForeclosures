# Deploying REIX Foreclosures — GitHub → Render, live in ~15 minutes

This assumes zero prior setup. By the end: your app is live at a real URL,
data refreshes itself daily, and app.reix.co can point at it.

## 1. Push this folder to GitHub

```bash
cd reix-foreclosures
git init
git add .
git commit -m "Initial commit"
```

Then on github.com: click **New repository** (e.g. `reix-foreclosures`), leave it
empty (no README/license), and follow the "push an existing repository" instructions
it shows you, which will look like:

```bash
git remote add origin https://github.com/YOUR-USERNAME/reix-foreclosures.git
git branch -M main
git push -u origin main
```

Your code (not your local `.db` file — `.gitignore` excludes it) is now on GitHub.

## 2. Deploy to Render (free/cheap tier, persistent disk, auto-HTTPS)

Render is recommended because it's the least fuss for a small Flask app + SQLite
file that needs to survive redeploys. Railway or Fly.io work too if you prefer them.

1. Go to [render.com](https://render.com) → sign up / log in (can use your GitHub account)
2. **New → Blueprint** → connect your `reix-foreclosures` GitHub repo
3. Render reads `render.yaml` (already in this repo) and sets up:
   - A web service running `gunicorn`
   - A 1GB persistent disk mounted at `/var/data` (so your SQLite database survives redeploys)
   - A random `REIX_SYNC_SECRET` value (used in step 4)
4. Click **Apply** — Render builds and deploys. First deploy takes a few minutes.
5. Once live, note your URL — something like `https://reix-foreclosures.onrender.com`

At this point the app is live but the database is empty. Let's seed it.

## 3. Run the first data sync

From your local machine (or Render's Shell tab under your service):

```bash
curl -X POST "https://YOUR-RENDER-URL.onrender.com/internal/sync" \
  -H "X-Sync-Secret: YOUR_SECRET_FROM_RENDER_ENV_VARS"
```

(Find the actual secret value in Render → your service → Environment →
`REIX_SYNC_SECRET`.) You should get back a JSON summary of how many
properties were inserted per county. Reload your app URL — the list, map,
and summary should now be populated.

## 4. Automate future syncs with GitHub Actions (free, no extra hosting)

This repo already includes `.github/workflows/sync.yml`, which calls your
`/internal/sync` endpoint once a day. You just need to give it two secrets:

1. In your GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**
2. Add `REIX_APP_URL` = `https://YOUR-RENDER-URL.onrender.com` (no trailing slash)
3. Add `REIX_SYNC_SECRET` = the same value from Render's environment variables
4. That's it — it'll run daily at 6am UTC automatically. You can also trigger it
   manually any time from the repo's **Actions** tab → "Sync foreclosure data" → **Run workflow**.

## 5. Point a real domain at it

- In Render: your service → **Settings → Custom Domains** → add e.g. `foreclosures.reix.co`
- In your DNS provider (wherever `reix.co` is managed): add the CNAME record
  Render gives you
- Render issues HTTPS automatically once DNS propagates

## 6. Wire app.reix.co to the live API

Get app.reix.co its own API key:
```bash
curl -X POST "https://foreclosures.reix.co/api/v1/keys" \
  -H "Content-Type: application/json" \
  -d '{"name": "app.reix.co", "email": "you@reix.co"}'
```
Store the returned `api_key` as a secret in wherever app.reix.co's backend runs,
and call `GET /api/v1/properties` from there. Full endpoint docs are always live
at `https://foreclosures.reix.co/docs`.

## Updating the app later

Any time you push to `main` on GitHub, Render redeploys automatically. Your data
persists on the disk across deploys — no reseeding needed.

## Adding a new county

Once you (or I) confirm another county has an easy structured source:
1. Add `sources/<county>.py` implementing the `CountySource` interface
2. Add it to `sources/registry.py`
3. Push to GitHub — it ships on the next deploy, and the next scheduled sync
   picks it up automatically. No frontend or API changes needed; county is
   already a first-class filter everywhere.
