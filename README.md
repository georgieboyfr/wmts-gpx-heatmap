# WMTS GPX Heatmap

A lightweight, self-hosted server that turns a collection of GPX tracks into a Strava-style heatmap, exposed as a standard **OGC WMTS 1.0.0** feed — compatible with QGIS, Leaflet, OpenLayers, MapLibre GL, and most GIS/outdoor apps.

Built to run on modest hardware (a home NAS such as Synology, a Raspberry Pi...): a single Docker container, no heavy database, no external GIS engine.

## Features

- **Heatmap rendering** by accumulating tracks (each track is drawn as a line, not a point cloud), with a gaussian "glow" blur and a fully customizable color gradient.
- **Automatic folder watching**: drop a `.gpx` file, it's on the map within seconds.
- **Optimized bulk import**: on first startup (or on demand), hundreds of pre-existing files are processed as a single batch — far faster than processing them one by one.
- **Automatic Garmin Connect sync** (optional): fetches and ingests your activities with no manual step, with an automatic fallback to the TCX export when Garmin's GPX export is incomplete.
- **Standard WMTS feed**: GetCapabilities + GetTile, both KVP and RESTful encodings, open CORS for web apps.
- **Everything is configurable without touching the code**: colors, line width, saturation threshold, contrast, zoom range, folders, Garmin sync frequency.

## Architecture at a glance

A single Python container (FastAPI + numpy/scipy/Pillow):
- GPX tracks are stored as continuous polylines (SQLite for persistence, numpy arrays in memory for computation);
- tiles are pre-rendered as PNG and served as static files (no on-the-fly rendering per request);
- only the tiles actually affected by a new track are (re)computed — never the whole pyramid.

## Requirements

- Docker + Docker Compose (v2, `docker compose` command) on the host machine (NAS, server, Raspberry Pi...).
- Outbound network access if you use Garmin Connect sync.

## Installation

```bash
git clone <repo-url> wmts-gpx-heatmap
cd wmts-gpx-heatmap

mkdir -p gpx tiles db garmin_tokens

# edit config.yaml: server.base_url must point to your machine (IP or hostname)
#   base_url: "http://192.168.1.50:8088"

docker compose up -d --build
```

Port `8088` is exposed by default (configurable in `docker-compose.yml`). Drop your `.gpx` files into the `gpx/` folder (e.g. via a network share, a scheduled task, or Garmin sync — see below).

**Rebuilding after a code / Python config change:**
```bash
docker compose down
docker compose up -d --build
```
*(A plain `docker compose restart` is enough to reload `config.yaml`, but not to pick up a source code change — when in doubt, rebuild.)*

## Available URLs

| URL | Method | Description |
|---|---|---|
| `/wmts?SERVICE=WMTS&REQUEST=GetCapabilities` | GET | WMTS capabilities (feed this to QGIS, OpenLayers, etc.) |
| `/wmts?SERVICE=WMTS&REQUEST=GetTile&LAYER=heatmap&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}` | GET | PNG tile, KVP encoding |
| `/wmts/1.0.0/heatmap/default/GoogleMapsCompatible/{z}/{y}/{x}.png` | GET | PNG tile, RESTful encoding (works as a plain tile source in Leaflet/MapLibre) |
| `/health` | GET | Confirms the service is up, returns the number of points currently in memory |
| `/admin/config-status` | GET | The configuration **actually loaded in memory** (useful to confirm a restart picked up a `config.yaml` change) |
| `/admin/rerender` | POST | Recomputes **all** tiles from scratch (after a style/gradient change) |
| `/admin/bulk-import` | POST | Imports, as a single batch, every `.gpx` in the watched folder not yet ingested (use after dropping many files while the container was already running) |
| `/admin/garmin-sync` | POST | Triggers an immediate Garmin Connect sync (in addition to the automatic cycle) |

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

Only read **at container startup** — after any change, `docker compose restart` (check `/admin/config-status` to confirm what's actually loaded).

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
  # credentials: NOT here, see .env / docker-compose.yml
```

### Tuning the visual rendering

- **A single pass too faint?** Raise `contrast_boost` (e.g. 20-40) rather than lowering `color_saturation` — this keeps the distinction between lightly and heavily traveled areas.
- **Everything saturates to red/white too fast?** Raise `color_saturation`.
- **Gradient**: fully customizable (colors, transparency, number of stops). Example included: a "Strava-style" blue gradient, best viewed over a dark basemap.
- After any rendering change, call `POST /admin/rerender` so already-generated tiles get recomputed (otherwise only new ingests benefit from it).

## Bulk import (hundreds of GPX files)

- **On the very first startup**: everything already present in `gpx/` is automatically processed as a single batch.
- **If you drop many files while the container is already running**: they go through the watcher's one-file-at-a-time path (slower, and it slows down further as the dataset grows). Drop them all first, then:
  ```bash
  curl -X POST http://<nas>:8088/admin/bulk-import
  ```

**Time estimate** (measured, extrapolated for an entry/mid-range NAS CPU such as a Celeron): roughly 480-500 ms per file in batch mode for a ~1h track at 1 point/second, default zoom range — so **~20-30 minutes for 600 files**. Longer if your tracks are longer or the zoom range wider; shorter if you reduce `max_zoom`.

## Automatic sync from Garmin Connect

Uses the unofficial [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library, which replicates the mobile app's authentication flow — **this is not a Garmin-sanctioned public API**. It can break if Garmin changes their auth flow, and automating it is at your own risk with respect to their terms of service (personal use only, similar in spirit to other community tools like GarminDB).

**Setup:**

1. Copy `.env.example` to `.env` and fill in `GARMIN_EMAIL` / `GARMIN_PASSWORD` (never commit/share this file — already excluded via `.gitignore`).
2. In `config.yaml`, set `garmin.enabled: true`.
3. `docker compose up -d --build`.
4. **One-time interactive login** (handles any MFA prompt and generates a cached session token, reused and refreshed automatically afterward):
   ```bash
   docker exec -it wmts-gpx-heatmap python -m app.garmin_login
   ```
5. From then on, new activities are checked every `sync_interval_minutes`, downloaded, and ingested automatically.

**Trigger an immediate sync:**
```bash
curl -X POST http://<nas>:8088/admin/garmin-sync
```

**Specific behaviors:**
- Automatic fallback to the **TCX** export when Garmin's GPX export is empty (a metadata-only "shell" with no GPS points, which can happen even for a legitimate outdoor activity — a known Garmin quirk).
- A corrupted GPX file or a GPS-less activity doesn't interrupt the rest of the batch.
- If the session expires (rare, after several months), the service logs a clear error instead of looping/crashing: just repeat step 4.

## Troubleshooting

- **A `config.yaml` change seems to have no effect** → config is only read at startup. Check what's actually loaded with `GET /admin/config-status`, restart if needed.
- **`/admin/rerender` doesn't seem to change anything** → same cause: restart the container first to load the new config, then call `/admin/rerender`.
- **`No module named app.xxx` when running `docker exec`** → the running image predates the code currently on disk: `docker compose down && docker compose up -d --build`.
- **`no such service` with `docker compose`** → the command isn't run from the folder containing `docker-compose.yml` (`cd` into the project folder, check with `docker compose config --services`).
- **Ingestion slows down after many files trickle in over time** → expected with the one-file-at-a-time path; use `/admin/bulk-import` (see above).

## Known limitations

- Point-to-tile filtering uses "raw" numpy (fast up to a few million points; beyond that, a spatial grid index would be worth adding).
- No authentication on tiles or `/admin/*` endpoints — put this behind a reverse proxy with restricted access if exposed to the internet.
- CORS is open to any origin (`*`), read-only — restrict `allow_origins` in `app/main.py` if the service leaves your local network.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
