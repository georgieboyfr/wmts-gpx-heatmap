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
    write_gpx(p, [(LAT, 2.0), (LAT, 2.01), (LAT + 0.01, 2.02)])  # 3e point hors ligne : gardé
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


def test_preview_follows_point_changes(engine, gpx_dir):
    tile = mercantile.tile(2.005, LAT, 14)
    engine.bulk_ingest([write_gpx(gpx_dir / "a.gpx", [(LAT, 2.0), (LAT, 2.01)])])
    first = engine.render_preview(14, tile.x, tile.y, engine.params)
    assert engine.render_preview(14, tile.x, tile.y, engine.params) == first  # sélection en cache
    engine.bulk_ingest([write_gpx(gpx_dir / "b.gpx", [(LAT, 2.0), (LAT, 2.01)])])  # 2e passage
    assert engine.render_preview(14, tile.x, tile.y, engine.params) != first


def test_points_reloaded_in_chunks_are_identical(config, gpx_dir, monkeypatch):
    from app import heatmap
    engine = HeatmapEngine(config)
    engine.bulk_ingest([write_gpx(gpx_dir / f"{i}.gpx", [(LAT, 2.0 + i * 0.01), (LAT + 0.001, 2.005 + i * 0.01), (LAT, 2.01 + i * 0.01)],
                                  waypoints=[(LAT + 0.002, 2.0)]) for i in range(3)], render=False)
    before = engine.pts
    engine.close()
    monkeypatch.setattr(heatmap, "LOAD_CHUNK", 2)  # blocs plus petits qu'un segment
    engine = HeatmapEngine(config)
    for name in ("seg", "lat", "lon"):
        assert np.array_equal(getattr(engine.pts, name), getattr(before, name))
    assert engine.pts.seg.dtype == np.int32
    engine.close()


def test_preview_cache_is_bounded_in_bytes(engine, gpx_dir, monkeypatch):
    from app import heatmap
    monkeypatch.setattr(heatmap, "PREVIEW_CACHE_BYTES", 1)  # rien ne tient : seule la dernière reste
    engine.bulk_ingest([write_gpx(gpx_dir / "a.gpx", [(LAT, 2.0), (LAT, 2.2)])])
    for lon in (2.05, 2.1, 2.15):
        tile = mercantile.tile(lon, LAT, 14)
        assert engine.render_preview(14, tile.x, tile.y, engine.params)
        assert len(engine._preview_cache[1]) == 1


@pytest.mark.parametrize("width, sigma", [(2, 2), (3, 4), (6, 0)])
def test_single_pass_reaches_saturation_despite_blur(tmp_path, gpx_dir, width, sigma):
    # color_saturation = 1 : un passage isolé doit atteindre le haut du dégradé, quel que
    # soit l'étalement du trait par le flou
    engine = HeatmapEngine(make_config(tmp_path, line_width_px=width, blur_sigma_px=sigma,
                                       color_saturation=1.0, contrast_boost=0))
    engine.bulk_ingest([write_gpx(gpx_dir / "a.gpx", [(LAT, 2.0), (LAT, 2.01)])])
    assert alpha(engine, mercantile.tile(2.005, LAT, 14)).max() >= 250
    engine.close()


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


# ---------- simplification des traces ----------

def test_simplify_drops_points_close_to_the_line():
    from app.heatmap import simplify_track
    rng = np.random.default_rng(0)
    lon = np.linspace(2.0, 2.01, 200)  # ~730 m, un point tous les ~3,7 m
    lat = LAT + rng.normal(0, 2e-6, 200)  # bruit de ~0,2 m
    assert list(simplify_track(np.c_[lat, lon], 1.0)) == [0, 199]
    assert len(simplify_track(np.c_[lat, lon], 0)) == 200  # 0 = désactivée


@pytest.mark.parametrize("back_to", [2.0, 2.005])
def test_simplify_keeps_turnaround_of_out_and_back(back_to):
    # aller puis retour (complet ou partiel) sur la même ligne : le demi-tour est sur la
    # droite qui relie les extrémités, mais pas sur le segment qui les relie
    from app.heatmap import simplify_track
    lon = np.r_[np.linspace(2.0, 2.01, 11), np.linspace(2.01, back_to, 11)[1:]]
    kept = simplify_track(np.c_[np.full(len(lon), LAT), lon], 1.0)
    assert 10 in kept and np.isclose(lon[kept].max(), 2.01)


def test_simplified_track_renders_like_the_original(tmp_path, gpx_dir):
    rng = np.random.default_rng(1)
    n = 600  # trace sinueuse, ~1 point/3 m, bruit GPS de ~1 m
    t = np.linspace(0, 1, n)
    seg = list(zip(LAT + 0.004 * np.sin(t * 9) + rng.normal(0, 1e-5, n), 2.0 + 0.02 * t + rng.normal(0, 1e-5, n)))
    alphas = {}
    for tol in (0, 1.0):
        engine = HeatmapEngine({**make_config(tmp_path / str(tol), min_zoom=14, max_zoom=14),
                                "ingest": {"simplify_tolerance_m": tol}})
        engine.bulk_ingest([write_gpx(gpx_dir / f"{tol}.gpx", seg)])
        alphas[tol] = {p.relative_to(engine.tiles_dir): np.asarray(Image.open(p))[:, :, 3].astype(int)
                       for p in engine.tiles_dir.rglob("*.png")}
        points = engine.point_count
        engine.close()
    assert points < n / 2  # au moins 2 fois moins de points
    assert alphas[0].keys() == alphas[1.0].keys()
    diff = max(np.abs(alphas[0][k] - alphas[1.0][k]).max() for k in alphas[0])
    assert diff <= 40, diff  # quelques niveaux d'opacité sur le bord du trait, au plus
