# Contributing

This project started from a personal need: running a lightweight GPX heatmap server on a home NAS, without pulling in a full GIS stack. It grew from there, one problem at a time, and is shared in case it's useful to someone else facing the same kind of setup.

There's no formal process and no strict roadmap. Any contribution is welcome:

- Bug reports and fixes
- Performance improvements
- New rendering options (color gradients, styles, etc.)
- Support for other GPS platforms beyond Garmin Connect
- Documentation improvements or translations
- Ideas that don't fit any of the above

## How to contribute

1. Open an issue first for anything non-trivial (new feature, behavior change) so it can be discussed before you spend time on it. Small fixes and typos can go straight to a pull request.
2. Fork the repo, make your changes.
3. Test locally with `docker compose up -d --build` before opening a PR — there's no automated test suite yet, so manual verification matters.
4. Open a pull request with a short description of what changed and why.

## Development notes

- The service is a single Python container (FastAPI + numpy/scipy/Pillow) — see the README for the architecture overview.
- `config.yaml` is only read at container startup; a code change requires a rebuild (`docker compose up -d --build`), not just a restart.
- If you touch the rendering pipeline (`app/heatmap.py`), please sanity-check the output visually (render a few tiles, eyeball them) — some past bugs (ghost lines connecting unrelated parts of a track) only show up visually, not in unit-level checks.

No contribution is too small. If you're not sure whether something is worth submitting, open an issue and ask.
