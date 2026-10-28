import os
import sqlite3
import threading

import mercantile
import numpy as np
import pytest
from PIL import Image, ImageDraw
from scipy import ndimage

from app.gpx_utils import normalize_path
from app.heatmap import TILE_SIZE, HeatmapEngine

from .conftest import make_config, write_gpx

LAT = 48.85


def alpha(engine, tile):
    p = engine.get_tile_path(tile.z, tile.x, tile.y)
    assert p is not None, f"tuile {tile} absente"
    return np.asarray(Image.open(p))[:, :, 3].astype(int)


def test_line_crossing_tile_without_points_is_drawn(engine, gpx_dir):
    # 2 points seulement, ~9 tuiles d'écart au zoom 14 : la tuile du milieu ne contient aucun point
    write_gpx(gpx_dir / "a.gpx", [(LAT, 2.0), (LAT, 2.2)])
    engine.bulk_ingest([gpx_dir / "a.gpx"])
    middle = mercantile.tile(2.1, LAT, 14)
    assert alpha(engine, middle).max() > 0


def test_no_seam_between_vertical_neighbours(engine, gpx_dir):
    top = mercantile.tile(2.1, LAT, 14)
    b = mercantile.bounds(top)
    lat = b.south + 2 * (b.north - b.south) / 256  # ligne à ~2 px du bord bas de la tuile
    write_gpx(gpx_dir / "a.gpx", [(lat, 2.09), (lat, 2.11)])
    engine.bulk_ingest([gpx_dir / "a.gpx"])
    below = mercantile.Tile(top.x, top.y + 1, 14)
    a_top, a_below = alpha(engine, top), alpha(engine, below)
    assert a_below[0].max() > 0, "le flou doit déborder sur la tuile voisine"
    assert np.abs(a_top[255] - a_below[0]).max() <= 40


def reference_alpha(engine, segment, tile, pad=80):
    """Rendu de référence : le segment entier dessiné d'un seul trait sur un grand canevas."""
    size = TILE_SIZE + 2 * pad
    a = np.array(segment)
    px, py = engine._lonlat_to_pixel(a[:, 1], a[:, 0], tile.z)
    img = Image.new("L", (size, size))
    coords = list(zip((px - tile.x * TILE_SIZE + pad).tolist(), (py - tile.y * TILE_SIZE + pad).tolist()))
    ImageDraw.Draw(img).line(coords, fill=255, width=engine.line_width_px, joint="curve")
    canvas = ndimage.gaussian_filter((np.asarray(img) > 0).astype(np.float32), sigma=engine.blur_sigma_px)
    return np.asarray(engine._colorize(canvas[pad:pad + TILE_SIZE, pad:pad + TILE_SIZE]))[:, :, 3].astype(int)


def test_out_and_back_track_counts_once(engine, gpx_dir):
    # aller-retour qui sort de la zone de la tuile : les 2 portions visibles se superposent
    # mais ne doivent compter que pour un passage (pas de double intensité)
    seg = [(LAT, 2.1), (LAT, 2.12), (LAT, 2.2), (LAT, 2.12), (LAT, 2.1)]
    engine.bulk_ingest([write_gpx(gpx_dir / "a.gpx", seg)])
    tile = mercantile.tile(2.1, LAT, 14)
    assert np.array_equal(alpha(engine, tile), reference_alpha(engine, seg, tile))


def test_concurrent_ingestion_has_no_duplicates(config, gpx_dir):
    paths = [write_gpx(gpx_dir / f"{i}.gpx", [(LAT + i * 0.001, 2.0), (LAT + i * 0.001, 2.01)]) for i in range(12)]
    engine = HeatmapEngine(config)
    threads = [threading.Thread(target=engine.bulk_ingest, args=(paths[i % 3:],)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert engine.point_count == 24
    engine.close()

    reopened = HeatmapEngine(config)
    assert reopened.point_count == 24
    assert len(np.unique(reopened.seg)) == 12
    reopened.close()


def test_modified_file_replaces_its_points(engine, gpx_dir):
    p = write_gpx(gpx_dir / "a.gpx", [(LAT, 2.0), (LAT, 2.01)])
    assert engine.bulk_ingest([p]) == 1
    assert engine.bulk_ingest([p]) == 0  # inchangé : ignoré
    write_gpx(p, [(LAT, 2.0), (LAT, 2.01), (LAT, 2.02)])
    assert engine.needs_ingest(p)
    assert engine.bulk_ingest([p]) == 1
    assert engine.point_count == 3


def test_removed_file_disappears_from_tiles(engine, gpx_dir):
    p = write_gpx(gpx_dir / "a.gpx", [(LAT, 2.0), (LAT, 2.01)])
    engine.bulk_ingest([p])
    assert list(engine.tiles_dir.rglob("*.png"))
    p.unlink()
    assert engine.remove_files([p]) == 1
    assert engine.point_count == 0
    assert not list(engine.tiles_dir.rglob("*.png"))


def test_invalid_file_is_retried_once_fixed(engine, gpx_dir):
    p = gpx_dir / "a.gpx"
    p.write_text("<gpx><trk><trkseg><trkpt lat=", encoding="utf-8")  # copie tronquée
    engine.bulk_ingest([p])
    assert engine.point_count == 0
    assert engine.bulk_ingest([p]) == 0  # pas retenté tant qu'il ne change pas
    write_gpx(p, [(LAT, 2.0), (LAT, 2.01)])
    assert engine.bulk_ingest([p]) == 1
    assert engine.point_count == 2


def test_sync_folder_imports_and_removes(engine, gpx_dir):
    a = write_gpx(gpx_dir / "a.gpx", [(LAT, 2.0), (LAT, 2.01)])
    write_gpx(gpx_dir / "b.GPX", [(LAT, 2.1), (LAT, 2.11)])
    assert engine.sync_folder(gpx_dir) == {"imported": 2, "removed": 0}
    a.unlink()
    assert engine.sync_folder(gpx_dir) == {"imported": 0, "removed": 1}
    assert engine.point_count == 2


def test_legacy_database_is_migrated_without_reimport(config, gpx_dir):
    p = write_gpx(gpx_dir / "a.gpx", [(LAT, 2.0), (LAT, 2.01)])
    db = config["paths"]["db"]
    os.makedirs(os.path.dirname(db))
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE points (seg_id INTEGER, seq INTEGER, lat REAL, lon REAL)")
    conn.execute("CREATE TABLE ingested_files (path TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO points VALUES (?,?,?,?)", [(0, 0, LAT, 2.0), (0, 1, LAT, 2.01)])
    conn.execute("INSERT INTO ingested_files VALUES (?)", (normalize_path(p),))
    conn.commit()
    conn.close()

    engine = HeatmapEngine(config)
    assert not engine.needs_ingest(p)
    assert engine.sync_folder(gpx_dir)["imported"] == 0
    assert engine.point_count == 2
    assert engine.reindex(gpx_dir) == {"imported": 1}
    assert engine.point_count == 2
    p.unlink()
    assert engine.sync_folder(gpx_dir)["removed"] == 1
    assert engine.point_count == 0  # après reindex, la suppression est gérée
    engine.close()


def test_rerender_all_removes_stale_tiles_only(engine, gpx_dir):
    engine.bulk_ingest([write_gpx(gpx_dir / "a.gpx", [(LAT, 2.0), (LAT, 2.01)])])
    real = set(engine.tiles_dir.rglob("*.png"))
    stale = engine.tiles_dir / "9" / "1" / "1.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x")
    leftover = next(iter(real)).with_name("999.png.tmp")
    leftover.write_bytes(b"x")
    engine.rerender_all()
    assert set(engine.tiles_dir.rglob("*.png")) == real
    assert not (engine.tiles_dir / "9").exists()
    assert not leftover.exists()


def test_stats_and_file_listing(engine, gpx_dir):
    write_gpx(gpx_dir / "a.gpx", [(LAT, 2.0), (LAT, 2.01)], [(LAT, 2.1)])
    (gpx_dir / "b.gpx").write_text("pas un gpx", encoding="utf-8")
    engine.sync_folder(gpx_dir)
    stats = engine.stats()
    assert (stats["files"], stats["files_ok"], stats["files_failed"]) == (2, 1, 1)
    assert stats["points"] == 3 and stats["segments"] == 2 and stats["tiles"] > 0
    w, s, e, n = stats["data_bounds"]
    assert 2.0 <= w < e <= 2.1 and abs(s - LAT) < 1e-6  # centiles : les extrêmes peuvent être rognés

    listing = engine.list_files(status="ok")
    assert listing["total"] == 1 and listing["items"][0]["points"] == 3
    assert engine.list_files(query="b.g")["items"][0]["status"] == "failed"
    assert engine.failed_files() == [normalize_path(gpx_dir / "b.gpx")]


def test_preview_uses_candidate_params_without_writing(engine, config, gpx_dir):
    from app.heatmap import RenderParams
    engine.bulk_ingest([write_gpx(gpx_dir / "a.gpx", [(LAT, 2.0), (LAT, 2.01)])])
    tile = mercantile.tile(2.005, LAT, 14)
    before = engine.get_tile_path(14, tile.x, tile.y).read_bytes()
    thick = RenderParams.from_config({**config["render"], "line_width_px": 8}, [[0, [0, 0, 0, 0]], [1, [255, 0, 0, 255]]])
    png = engine.render_preview(14, tile.x, tile.y, thick)
    assert png and png != before
    assert engine.get_tile_path(14, tile.x, tile.y).read_bytes() == before
    assert engine.render_preview(5, 0, 0, thick) is None  # hors plage de zoom


def test_cancel_during_parsing_leaves_store_untouched(engine, gpx_dir):
    from app.jobs import JobManager
    paths = [write_gpx(gpx_dir / f"{i}.gpx", [(LAT, 2.0), (LAT, 2.01)]) for i in range(5)]
    jobs = JobManager(lanes=("data",))

    class CancelAfterTwo:
        def __init__(self, job):
            self.job, self.n = job, 0

        def phase(self, *a, **k):
            self.job.phase(*a, **k)

        def advance(self, n=1, phase=None):
            self.n += n
            if self.n == 2:
                jobs.cancel(self.job.id)
            self.job.advance(n, phase)

    job, _ = jobs.submit("x", "x", lambda job: engine.bulk_ingest(paths, progress=CancelAfterTwo(job)))
    assert job.wait(10) and job.status == "cancelled"
    assert engine.point_count == 0 and engine.stats()["files"] == 0
    assert engine.bulk_ingest(paths) == 5  # rien n'a été marqué comme importé


@pytest.mark.parametrize("render", [{"color_saturation": 0}, {"min_zoom": 10, "max_zoom": 8}, {"line_width_px": 0}])
def test_invalid_config_is_rejected(tmp_path, render):
    with pytest.raises(ValueError, match="Configuration invalide"):
        HeatmapEngine(make_config(tmp_path, **render))


def test_contrast_boost_zero_is_linear(tmp_path, gpx_dir):
    engine = HeatmapEngine(make_config(tmp_path, contrast_boost=0))
    engine.bulk_ingest([write_gpx(gpx_dir / "a.gpx", [(LAT, 2.0), (LAT, 2.01)])])
    assert list(engine.tiles_dir.rglob("*.png"))
    engine.close()
