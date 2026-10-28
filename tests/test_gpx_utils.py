from app.gpx_utils import is_gpx, normalize_path, parse_gpx_segments

from .conftest import write_gpx


def test_parse_segments_and_waypoints(tmp_path):
    p = write_gpx(tmp_path / "a.gpx", [(48.0, 2.0), (48.1, 2.1)], [(47.0, 1.0)], waypoints=[(45.0, 5.0)])
    segments, waypoints = parse_gpx_segments(str(p))
    assert segments == [[(48.0, 2.0), (48.1, 2.1)], [(47.0, 1.0)]]
    assert waypoints == [(45.0, 5.0)]


def test_empty_segment_is_skipped(tmp_path):
    p = write_gpx(tmp_path / "a.gpx", [])
    assert parse_gpx_segments(str(p)) == ([], [])


def test_path_helpers(tmp_path):
    assert is_gpx("x/TRACE.GPX") and not is_gpx("x/trace.gpx.part")
    assert normalize_path(str(tmp_path) + "/./sub/../a.gpx") == normalize_path(tmp_path / "a.gpx")
