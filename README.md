# WMTS GPX Heatmap

A lightweight, self-hosted server that turns a collection of GPX tracks into a Strava-style heatmap, exposed as a standard **OGC WMTS 1.0.0** feed — compatible with QGIS, Leaflet, OpenLayers, MapLibre GL, and most GIS/outdoor apps.

Built to run on modest hardware (a home NAS such as Synology, a Raspberry Pi...): a single Docker container, no heavy database, no external GIS engine.

## Features

- **Heatmap rendering** by accumulating tracks (each track is drawn as a line, not a point cloud), with a gaussian "glow" blur and a fully customizable color gradient.
- **Automatic folder watching**: drop a `.gpx` file, it's on the map within seconds. Deleting or replacing a file removes or updates its track too.
- **Optimized batch import**: files are always ingested in batches (at startup, and when many files land at once while running) — each affected tile is rendered once per batch, not once per file.
- **Automatic Garmin Connect sync** (optional): fetches and ingests your activities with no manual step, with an automatic fallback to the TCX export when Garmin's GPX export is incomplete.
- **Standard WMTS feed**: GetCapabilities + GetTile, both KVP and RESTful encodings, open CORS for web apps.
- **Everything is configurable without touching the code**: colors, line width, saturation threshold, contrast, zoom range, folders, Garmin sync frequency.

## Preview

<p align="center">
  <img src="docs/heatmap-example.png" alt="Heatmap Example" width="500">
</p>


## Architecture at a glance

A single Python container (FastAPI + numpy/scipy/Pillow):
- GPX tracks are stored as continuous polylines (SQLite for persistence, numpy arrays in memory for computation);
- tiles are pre-rendered as PNG and served as static files (no on-the-fly rendering per request);
- only the tiles actually affected by a new track are (re)computed — never the whole pyramid;
- tiles are written atomically, so a client never receives a half-written tile, and are served with an `ETag` (cheap `304` revalidation).

## Requirements

- Docker + Docker Compose (v2, `docker compose` command) on the host machine (NAS, server, Raspberry Pi...).
- Outbound network access if you use Garmin Connect sync.

## Installation

A ready-to-use image is published for Intel/AMD and ARM machines: **`ghcr.io/georgieboyfr/wmts-gpx-heatmap`**. All you need is a compose file — no source code, no `git`, no build. This also works as-is as a *project* in Synology Container Manager or Portainer.

```yaml
services:
  wmts-gpx-heatmap:
    image: ghcr.io/georgieboyfr/wmts-gpx-heatmap:1.0.5
    container_name: wmts-gpx-heatmap
    restart: unless-stopped
    user: "1000:1000"                    # owner of the data folders (`id` on the host; often 1026:100 on Synology)
    ports:
      - "8088:8088"
    volumes:
      - /volume1/docker/wmts-gpx-heatmap/gpx:/data/gpx
      - /volume1/docker/wmts-gpx-heatmap/tiles:/data/tiles
      - /volume1/docker/wmts-gpx-heatmap/db:/data/db
      - /volume1/docker/wmts-gpx-heatmap/garmin_tokens:/data/garmin_tokens
    environment:
      - BASE_URL=https://heatmap.example.com   # public URL, used in GetCapabilities
      - ADMIN_TOKEN=a-long-random-string        # password of the /admin interface
      - GARMIN_TOKEN_STORE=/data/garmin_tokens
```

1. Create the four data folders, writable by the `user:` above.
2. Start the stack (`docker compose up -d`, or *Build* in Container Manager).
3. Open `http://<host>:8088/admin` and sign in with `ADMIN_TOKEN`.

The repository's [`docker-compose.yml`](docker-compose.yml) is the same file, reading its values from a `.env` (copy [`.env.example`](.env.example)). `config.yaml` is optional: the defaults shipped in the image are used, render and Garmin settings are changed from the admin, and a custom file can still be mounted on `/config/config.yaml`.

**Updating:** change the version in `image:` (see the [changelog](CHANGELOG.md)) and recreate the container (`docker compose pull && docker compose up -d`). The `latest`, `1` and `1.0` tags are also published.

**Building from source** instead: clone the repository, replace `image:` with `build: .` and run `docker compose up -d --build`.

## Administration (`/admin`)

Open **`http://<nas>:8088/admin`** and sign in with the `ADMIN_TOKEN` from `.env` (the admin is disabled when it is not set). Everything is done from there, no manual `POST` needed:

- **Dashboard** — files, points, tiles, Garmin status; one-click *Rescan folder*, *Recompute tiles*, *Sync Garmin*, *Reindex everything*. Every long operation runs as a background **task** with live progress per phase (files read, tiles computed, ETA), a **Cancel** button and a history.
- **Garmin** — connect your account (email + password, then the **MFA code** if Garmin asks for one), see its status, last/next sync, sync now, disconnect. When connecting you choose whether the password is **remembered** (stored on the server, so the session can be renewed on its own) or **not** (recommended: only the session token is kept; reconnect from the admin if it ever expires). Auto-sync on/off, interval, history depth and batch size are editable live.
- **Data** — drag & drop `.gpx` files to upload them; searchable list of imported files with their status, errors on unreadable files, and *Retry*.
- **Render** — line width, blur, saturation, contrast, zoom range and color gradient, with a **live preview map** computed on the fly from the form; *Apply* saves and recomputes every tile.
- **Logs** — the application log, live, filterable by level.

Settings changed in the admin are stored as overrides in `settings.json` next to the database (`/data/db`), on top of `config.yaml`; *Back to config.yaml* removes them. The map basemap of the Render tab is loaded from OpenStreetMap (internet access needed for it only).

## Available URLs

| URL | Method | Description |
|---|---|---|
| `/wmts?SERVICE=WMTS&REQUEST=GetCapabilities` | GET | WMTS capabilities (feed this to QGIS, OpenLayers, etc.) |
| `/wmts?SERVICE=WMTS&REQUEST=GetTile&LAYER=heatmap&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}` | GET | PNG tile, KVP encoding |
| `/wmts/1.0.0/heatmap/default/GoogleMapsCompatible/{z}/{y}/{x}.png` | GET | PNG tile, RESTful encoding (works as a plain tile source in Leaflet/MapLibre) |
| `/health` | GET | Confirms the service is up, returns the number of points currently in memory |
| `/admin` | GET | Administration interface (see above) |

For scripts, the admin API accepts the token as a bearer token. The historical endpoints still exist and now start a background task (`202`, with the task in the response): `POST /admin/rerender`, `/admin/bulk-import` (rescan), `/admin/reindex`, `/admin/garmin-sync`, and `GET /admin/config-status`. The admin's own API lives under `/admin/api/` (e.g. `POST /admin/api/actions/{rescan|rerender|reindex|garmin-sync}`, `GET /admin/api/status`).
```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://<nas>:8088/admin/api/actions/rescan
```

Replace `<nas>` / the hostname and port with your own deployment in every example below.

### Connecting from your client

**QGIS / desktop GIS software**: *Layer > Add Layer > WMS/WMTS Layer* → paste the `GetCapabilities` URL → connect → check the `heatmap` layer.

**Leaflet**
```js
L.tileLayer('http://<nas>:8088/wmts/1.0.0/heatmap/default/GoogleMapsCompatible/{z}/{y}/{x}.png', {
  minZoom: 8, maxZoom: 16, opacity: 0.85
}).addTo(map);
```

**MapLibre GL JS**
```js
map.addSource('heatmap', {
  type: 'raster',
  tiles: ['http://<nas>:8088/wmts/1.0.0/heatmap/default/GoogleMapsCompatible/{z}/{y}/{x}.png'],
  tileSize: 256
});
map.addLayer({ id: 'heatmap-layer', type: 'raster', source: 'heatmap', paint: { 'raster-opacity': 0.85 } });
```

**OpenLayers** (parses the actual `GetCapabilities`)
```js
import WMTSCapabilities from 'ol/format/WMTSCapabilities';
import { optionsFromCapabilities } from 'ol/source/WMTS';

const resp = await fetch('http://<nas>:8088/wmts?SERVICE=WMTS&REQUEST=GetCapabilities');
const caps = new WMTSCapabilities().read(await resp.text());
const options = optionsFromCapabilities(caps, { layer: 'heatmap', matrixSet: 'GoogleMapsCompatible' });
```

**Mobile apps** (Locus Map, OruxMaps, Gaia GPS, IGN Rando...): add a custom tile source using the RESTful URL template above (`{z}/{y}/{x}` — some apps expect `{x}/{y}` instead, check your app's convention).

## Configuration (`config.yaml`)

Only read **at container startup** — after any change, `docker compose restart`. The `render`, `color_gradient` and `garmin` sections are the **defaults**: they can be changed live from the admin, which then takes precedence for the values it changed (*Back to config.yaml* in the admin restores them).

```yaml
paths:
  gpx_watch: /data/gpx      # folder watched for new .gpx files
  tiles: /data/tiles        # generated PNG tile cache
  db: /data/db/points.sqlite3

render:
  min_zoom: 8
  max_zoom: 16
  line_width_px: 3            # line thickness before blur
  blur_sigma_px: 4            # gaussian blur strength (the "glow" effect)
  color_saturation: 6.0       # number of overlapping passes needed to reach full-intensity color
  contrast_boost: 9.0         # boosts the visibility of low densities (e.g. a single pass) without
                               # touching the saturation threshold; try 20-40+ to make a single track stand out more

color_gradient:                # [position 0-1, [R, G, B, Alpha]] - fully free gradient, as many stops as you want
  - [0.00, [10, 15, 40, 0]]
  - [0.15, [15, 40, 95, 130]]
  - [0.40, [25, 110, 210, 190]]
  - [0.65, [80, 190, 255, 225]]
  - [0.85, [190, 235, 255, 245]]
  - [1.00, [255, 255, 255, 255]]

server:
  base_url: "http://your-nas:8088"   # public URL used inside GetCapabilities
  layer_name: "heatmap"
  layer_title: "Heatmap GPX"

garmin:
  enabled: false                    # true to enable Garmin Connect sync
  sync_interval_minutes: 60         # how often to check for new activities
  lookback_days_first_sync: 1825    # max history depth on the very first sync
  ingest_batch_size: 0              # 0 = a single batch ingest once the whole download finishes
                                     # >0 = ingest progressively, in batches of this size
  # account: connected from the admin (Garmin tab), not here
```

### Tuning the visual rendering

- **A single pass too faint?** Raise `contrast_boost` (e.g. 20-40) rather than lowering `color_saturation` — this keeps the distinction between lightly and heavily traveled areas.
- **Everything saturates to red/white too fast?** Raise `color_saturation`.
- **Gradient**: fully customizable (colors, transparency, number of stops). Example included: a "Strava-style" blue gradient, best viewed over a dark basemap.
- Easiest: tune it in the admin's **Render** tab with the live preview, then *Apply* (tiles are recomputed automatically). After editing `config.yaml` instead, restart and run *Recompute tiles*.

## Bulk import (hundreds of GPX files)

- **At startup**: the database is aligned with `gpx/` in a single batch — new or modified files are imported, tracks of files deleted while the service was stopped are removed. This runs in the background: the server answers right away and tiles appear as soon as the batch is done.
- **While running**: the watcher waits until a file has stopped changing (slow network copies are handled), then ingests every file ready at that moment as one batch. Dropping hundreds of files at once is therefore processed in a few batches, not one by one.
- **From the admin**: drop files in the *Data* tab, or click *Rescan folder* if file events were missed (some network shares don't report them). Progress is shown live on the dashboard.

**Time estimate** (measured, extrapolated for an entry/mid-range NAS CPU such as a Celeron): roughly 480-500 ms per file in batch mode for a ~1h track at 1 point/second, default zoom range — so **~20-30 minutes for 600 files**. Longer if your tracks are longer or the zoom range wider; shorter if you reduce `max_zoom`.

## Automatic sync from Garmin Connect

Uses the unofficial [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library, which replicates the mobile app's authentication flow — **this is not a Garmin-sanctioned public API**. It can break if Garmin changes their auth flow, and automating it is at your own risk with respect to their terms of service (personal use only, similar in spirit to other community tools like GarminDB).

**Setup** — all from the admin's **Garmin** tab:

1. Enter your Garmin Connect email and password, and choose whether the password is remembered (see *Administration* above). If Garmin asks for a verification code (MFA), a field appears to type it (it must be entered within 5 minutes).
2. Turn on **automatic sync** and adjust the interval if needed. The first sync starts right away; then new activities are checked every interval, downloaded, and ingested automatically.
3. *Sync now* triggers an immediate sync; its progress shows on the dashboard.

The session token is stored in the `garmin_tokens` volume (a remembered password too, in `credentials.json`, readable only by the service user). Alternatives still supported: `GARMIN_EMAIL` / `GARMIN_PASSWORD` in `.env` act as a remembered password, and `docker exec -it wmts-gpx-heatmap python -m app.garmin_login` performs the login from a terminal.

**Specific behaviors:**
- Automatic fallback to the **TCX** export when Garmin's GPX export is empty (a metadata-only "shell" with no GPS points, which can happen even for a legitimate outdoor activity — a known Garmin quirk).
- A corrupted GPX file or a GPS-less activity doesn't interrupt the rest of the batch; files already downloaded are ingested even if the sync stops on an error.
- Files are written atomically and ingested by the sync itself, in batches of `ingest_batch_size` (the watcher leaves them alone).
- If the session expires (rare) and cannot be renewed on its own, the Garmin tab shows **Reconnection needed**: reconnect from there.

## Troubleshooting

- **A `config.yaml` change seems to have no effect** → config is only read at startup, and a value changed in the admin takes precedence over it (the admin says "modified from the admin"; *Back to config.yaml* restores the file's values).
- **`No module named app.xxx` when running `docker exec`** → the running image predates the code currently on disk: `docker compose down && docker compose up -d --build`.
- **`no such service` with `docker compose`** → the command isn't run from the folder containing `docker-compose.yml` (`cd` into the project folder, check with `docker compose config --services`).
- **The admin says it is disabled / `403` on `/admin/*`** → `ADMIN_TOKEN` isn't set in `.env` (restart after setting it). `401` from a script → send `Authorization: Bearer <ADMIN_TOKEN>`.
- **`Permission denied` / `readonly database` in the logs** → the data folders aren't writable by `PUID:PGID` (see *Upgrading* below).
- **The container exits on startup with `Configuration invalide`** → a `render` / `color_gradient` value in `config.yaml` is out of range; the message says which one.

## Upgrading from an earlier version

- Switch the compose file to the published image (see *Installation*): replace `build: .` with `image: ghcr.io/georgieboyfr/wmts-gpx-heatmap:<version>`. If you had a customized `config.yaml`, keep mounting it (`./config.yaml:/config/config.yaml:ro`); otherwise set `BASE_URL`.
- The container no longer runs as root. Either give the data folders to the default user (`sudo chown -R 1000:1000 /volume1/docker/wmts-gpx-heatmap`) or set `PUID`/`PGID` in `.env` to their current owner.
- Set `ADMIN_TOKEN` in `.env` (the admin is disabled without it).
- The existing database is migrated automatically and nothing is re-imported. Run *Reindex everything* once from the admin so that deleting or modifying files imported before the upgrade is also handled — and to clean up possible duplicate tracks left by older versions.
- An existing Garmin session (token from `garmin_login`) keeps working; `garmin.enabled` from `config.yaml` is still honored until changed in the admin.

## Known limitations

- Point-to-tile filtering uses numpy, narrowed per parent tile at `min_zoom` (fast up to a few million points; beyond that, a real spatial index would be worth adding).
- Tiles are not authenticated, and the admin only by a shared token over plain HTTP (the session cookie is marked `Secure` automatically behind HTTPS) — put this behind a reverse proxy with TLS and restricted access if exposed to the internet.
- CORS is open to any origin (`*`), read-only — restrict `allow_origins` in `app/main.py` if the service leaves your local network.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
