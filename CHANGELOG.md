# Changelog

## Unreleased

### Performance
- **Tiles saved as palette PNGs**: a tile only holds the 256 colors of the gradient, so it is now written with 1 byte per pixel instead of 4, pixel-identical (transparency included). PNG encoding is ~10 times faster and dense tiles ~2 times smaller (12 % less disk on the demo data, where many tiles are mostly transparent). Full recompute of the demo data: 31.9 s -> 19.6 s.
- **Large recomputes on several processes** (`performance.render_workers`, `0` = auto): each process renders a tile and all its descendants, reading the points from a shared memory-mapped file (no copy per process). Auto mode uses the available cores (at most 4) and no more processes than free memory allows (~80 MB each while computing); small updates (fewer than 256 tiles) stay in the main process. Demo data on a 2-vCPU machine: 19.6 s -> 16.6 s; more on a NAS with 4 real cores.

## 1.0.7 — 2026-10-29

### Performance
- Much lower memory peaks, which could exhaust the RAM of a small NAS on a large history (measured with 2 M points):
  - **Import**: GPX files read in a batch (first Garmin sync, reindex...) are kept as compact numpy arrays and inserted into the database as a stream, instead of Python lists of tuples: peak memory down from ~284 to ~49 bytes per imported point.
  - **Startup**: points are loaded in chunks into pre-allocated arrays instead of one Python tuple per point: peak down from 359 MB to 77 MB for 46 MB of points.
  - **Admin preview**: the point selection cache is now limited to 64 MB instead of a number of entries, which could reach several hundred MB on a large database.
- Points take half the memory: coordinates are kept as 32-bit integers in microdegrees (~11 cm precision) and track ids / point indices as 32-bit integers, i.e. 12 bytes per point instead of 24 (the database still stores full-precision coordinates).
- **Tracks are simplified on import** (Douglas-Peucker): points closer than `ingest.simplify_tolerance_m` (1 m by default) to the simplified line are dropped, with no visible change in the rendering. Typically 4 to 10 times fewer points for a GPS watch (only ~1.3x for very noisy raw GPS), so less memory and faster rendering. `0` keeps every point. **Run *Reindex everything* once** to simplify tracks imported by earlier versions.

## 1.0.6 — 2026-10-29

### Admin
- The admin interface is available in English and French: it follows the browser language, and a FR / EN switch in the top bar remembers the choice. Task labels, phases and results sent by the server are translated too; error messages coming from the server stay in French.

## 1.0.5 — 2026-10-29

### Fixes
- Single passes rendered dull and semi-transparent whatever the contrast setting: the blur spreads the line and lowered its peak intensity (to ~0.38 with a 2 px line and a blur of 2), so `color_saturation` passes never actually reached the top of the gradient. Intensities are now normalized so that one isolated pass counts as 1 after the blur. **Tiles look brighter: run a full tile recompute after upgrading** (or lower `color_saturation` / `contrast_boost` to keep the previous look). The contrast slider now goes up to 200.

### Admin
- Render preview: much faster when moving the map or changing a setting (point selections are cached per tile and filtered from the parent tile, as for regular rendering), and a banner on the map shows tiles being computed (`n/total`) or that the current zoom is outside the rendered range.

## 1.0.4 — 2026-10-28

### Performance
- Tile rendering about 3 times faster (measured on a full recompute): each tile now filters the points of its parent tile at the previous zoom instead of all the points of its region, so densely ridden areas no longer slow the computation down at high zooms; PNG tiles are compressed in a single pass (`compress_level=6` instead of `optimize=True`, ~3 % larger files).

## 1.0.3 — 2026-10-28

### Fixes
- Image publication workflow: run the tests on Python 3.12 (the 1.0.2 run failed before building, so no 1.0.2 image was published).

## 1.0.2 — 2026-10-28

### Changes
- Docker image published on GitHub Container Registry for amd64 and arm64 (`ghcr.io/georgieboyfr/wmts-gpx-heatmap`), built and tested automatically for each version tag. `docker-compose.yml` now uses it: no source code or git needed on the host.
- `config.yaml` is optional: without a mounted file, the defaults shipped in the image are used, and the public URL is set with the `BASE_URL` environment variable.

## 1.0.1 — 2026-10-28

### Fixes
- Docker image build failing with `failed to parse Dockerfile: Syntax error - can't find = in "\n"` (malformed `LABEL` line in 1.0.0).

## 1.0.0 — 2026-10-28

First versioned release.

### Administration interface (`/admin`)
- Web admin protected by `ADMIN_TOKEN` (session cookie, CSRF-safe): dashboard, Garmin, data, render and logs tabs.
- Background tasks for rescan, reindex, tile recompute and Garmin sync, with live per-phase progress, ETA, cancellation and history.
- Garmin Connect account connected from the admin, MFA code included; choice to remember the password on the server or not; status, last/next sync, disconnect.
- GPX upload by drag & drop, file list with errors and retry.
- Render settings and color gradient editable live, with an on-the-fly preview map; settings stored as overrides of `config.yaml`.
- Live, filterable application log.

### Fixes
- Duplicate tracks when the watcher and the Garmin sync ingested the same file; segment ids colliding under concurrent ingestion.
- Files copied slowly ingested truncated and then ignored forever.
- Segments crossing a tile without a point in it were not drawn; out-and-back tracks counted twice where their visible parts overlapped.
- Half-written tiles could be served; tiles disappeared during a full recompute.
- Admin endpoints exposed without authentication (CSRF from any web page).

### Changes
- Deleted or modified GPX files are removed/updated on the map; database migrated automatically.
- Watcher waits for files to be stable and ingests them in batches; startup import runs in the background.
- WMTS: case-insensitive KVP parameters, escaped capabilities, `ETag`/`304` on tiles.
- Container runs as a non-root user (`PUID`/`PGID`), with a health check; dependencies pinned; test suite added.
