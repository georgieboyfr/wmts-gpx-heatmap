import os
import time

import pytest

GRADIENT = [[0.0, [0, 0, 0, 0]], [1.0, [255, 255, 255, 255]]]


def make_config(tmp_path, **render):
    return {
        "paths": {
            "gpx_watch": str(tmp_path / "gpx"),
            "tiles": str(tmp_path / "tiles"),
            "db": str(tmp_path / "db" / "points.sqlite3"),
        },
        "render": {"min_zoom": 12, "max_zoom": 14, "line_width_px": 2, "blur_sigma_px": 2,
                   "color_saturation": 5.0, "contrast_boost": 10.0, **render},
        "color_gradient": GRADIENT,
        "server": {"base_url": "http://localhost:8088", "layer_name": "heatmap", "layer_title": "Heatmap"},
        "garmin": {"enabled": False},
    }


def write_gpx(path, *segments, waypoints=()):
    """Écrit un GPX ; chaque segment est une liste de (lat, lon). Change le mtime
    à chaque réécriture, même sur un système de fichiers à résolution grossière."""
    trksegs = "".join(
        "<trkseg>" + "".join(f'<trkpt lat="{lat}" lon="{lon}"></trkpt>' for lat, lon in seg) + "</trkseg>"
        for seg in segments
    )
    wpts = "".join(f'<wpt lat="{lat}" lon="{lon}"></wpt>' for lat, lon in waypoints)
    previous = os.stat(path).st_mtime if os.path.exists(path) else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" creator="test" '
        f'xmlns="http://www.topografix.com/GPX/1/1">{wpts}<trk>{trksegs}</trk></gpx>',
        encoding="utf-8",
    )
    if previous is not None:
        t = previous + 10
        os.utime(path, (t, t))
    return path


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def gpx_dir(tmp_path):
    d = tmp_path / "gpx"
    d.mkdir()
    return d


@pytest.fixture
def engine(config):
    from app.heatmap import HeatmapEngine
    e = HeatmapEngine(config)
    yield e
    e.close()
