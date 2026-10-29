# Roadmap and ideas

Improvement ideas collected along the way, with the reasoning and measurements behind them, so they can be picked up later. Nothing here is planned or promised; items are roughly ordered by value for effort within each section.

Measurements were taken on synthetic data: 320 tracks around Paris (~290 k points after simplification, 2 430 non-empty tiles over zooms 8–16) on a 2-vCPU machine, unless stated otherwise.

## Performance

Where a full recompute spent its time in 1.0.7 (demo data, 31.8 s): drawing, blur and colors 49 %, PNG writing 47 %, point selection 2 %. Palette PNGs (1.0.8) made encoding ~10x faster, so drawing now dominates (full recompute 19.6 s with 1 process, 16.6 s with 2). Zooms 15 and 16 hold 92 % of the tiles to compute.

### Render high zooms on demand
Pre-render only up to zoom 14; render zoom 15+ tiles on first request (a few ms each) and keep them on disk. After an import, delete the affected tiles instead of re-rendering them.
- **Gain**: full recomputes and imports ~10x faster, since most high-zoom tiles are never viewed.
- **Cost**: a short delay the first time an area is viewed at high zoom; needs the hierarchical selection cache of the admin preview so a single tile doesn't scan every point.
- Probably best as an option (`render.prerender_max_zoom`).

### Single-file tile storage (MBTiles or PMTiles)
Thousands of small PNG files cost ~2 s of open/rename/delete per recompute on the demo data, and slow down NAS backups and file indexing.
- **Gain**: moderate on speed, large on backups and file-system load.
- **Cost**: tile serving and atomic replacement have to move into SQLite; migration of the existing tile folder.

### Level of detail per zoom
At low zooms many points fall into the same pixel (a pixel is ~600 m at zoom 8). Simplifying tracks with a tolerance of ~0.5 px of the zoom being rendered would cut drawing time at low and mid zooms (and in the admin preview), where tiles cost 25–80 ms each.
- **Cost**: one index array per zoom level (geometric series, so small), computed at import.

### Spatial index
Point selection is ~2 % of the time today thanks to the parent-tile filtering. A grid index only becomes useful beyond several million points (see *Known limitations* in the README).

### WebP tiles
Measured: lossless WebP is ~10 % larger than palette PNG and 8x slower to encode, so not worth it for now. Lossy WebP would be smaller but less universally supported by WMTS clients.

### Not tested yet
- Multi-process rendering uses `forkserver` on Linux; it is covered by the CI tests (Ubuntu) but was only measured on Windows (`spawn`). Measure the actual speedup on a 4-core NAS.

## Memory

Current state (1.0.7+): 12 bytes per point in memory, peaks of ~50 bytes per imported point during an import, startup peak close to the in-memory size. A GPS-watch history of ~5 years fits in ~20 MB after simplification.

### One BLOB per track in the database
Store each track as a binary array (one row per segment) instead of one SQLite row per point.
- **Gain**: database 3–4x smaller, near-instant startup (no per-row decoding).
- **Cost**: schema migration of existing databases.

### Memory-mapped points
Keep the point arrays in `.npy` files opened with `mmap_mode`, so the OS can page them out under memory pressure. Already used to share points with the render processes.

## Features

### Filter by year and by activity type
A track only belongs to its year layer and to the "all years" layer, so this costs ~2x the CPU and disk of today, not one full rendering per year. A new activity only re-renders its own year and the global layer.
- Requires storing each activity's date (from the GPX timestamps; a reindex backfills existing tracks) and type (Garmin provides it).
- Exposed as extra WMTS layers (`heatmap-2024`, `heatmap-run`…), which QGIS and other clients show as regular layers.
- Alternative: on-demand rendering with a cache, which allows free date ranges without extra storage but slower first display.

### Privacy zones
Hide points within a radius of chosen places (home, work) before rendering — important as soon as the map is shared.

### Public map page
A `/map` page with the basemap, year/sport selection and a shareable link; today viewing needs a GIS client or the admin.

### Statistics
Distance per year and per sport, "new roads this year", streaks.

### More import formats
FIT files (native watch format, more precise than GPX), Strava bulk export (zip), TCX and KML.

### Server messages in English
The admin is translated client-side; error messages coming from the server (validation, Garmin errors) are still in French.

## Fun

- **Timelapse**: the heatmap building up month after month, exported as GIF or MP4 (filter tracks by date, render one frame per month).
- **Recency coloring**: recent tracks bright, older ones fading.
- **New territory**: roads ridden for the first time this month in a distinct color.
- **Explored squares**: count the visited cells of a grid (e.g. zoom-14 tiles) and the largest fully covered square — a classic exploration challenge, and the tile machinery is already there.
- **Poster export**: a high-resolution render of an area on a sober basemap, ready to print.

## Operations

- `HEALTHCHECK` in the Dockerfile, so Synology / Portainer show the service health.
- Optional token on tiles, to expose the map on the internet without making it public.
- CI runs on `ubuntu-latest`; pin `ubuntu-24.04` if the Ubuntu 26 migration ever causes trouble.
