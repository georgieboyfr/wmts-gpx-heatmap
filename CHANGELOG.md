# Changelog

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
