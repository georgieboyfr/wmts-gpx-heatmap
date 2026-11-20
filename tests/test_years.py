"""Couches annuelles : année des activités, rendu par année, dégradés par année."""
import sqlite3
from datetime import datetime, timezone

import mercantile
import numpy as np
import pytest
from PIL import Image

from app.garmin_sync import _points_to_gpx_bytes
from app.gpx_utils import parse_gpx, read_year
from app.gradients import PRESETS, validate_year_gradients
from app.heatmap import HeatmapEngine
from app.settings import Settings

from .conftest import write_gpx

LAT = 48.85
TILE = mercantile.tile(2.005, LAT, 14)
OTHER = mercantile.tile(2.205, LAT, 14)


def rgba(path):
    return np.asarray(Image.open(path).convert("RGBA")).astype(int)


@pytest.fixture
def two_years(engine, gpx_dir):
    """Une activité en 2024 autour de TILE, une en 2025 autour de OTHER."""
    a = write_gpx(gpx_dir / "a.gpx", [(LAT, 2.0), (LAT, 2.01)], time="2024-05-01T08:00:00Z")
    b = write_gpx(gpx_dir / "b.gpx", [(LAT, 2.2), (LAT, 2.21)], time="2025-03-02T08:00:00Z")
    engine.bulk_ingest([a, b])
    return a, b


def test_year_from_first_point_then_metadata(tmp_path):
    p = write_gpx(tmp_path / "a.gpx", [(LAT, 2.0), (LAT, 2.01)], time="2023-12-31T23:00:00Z")
    assert parse_gpx(str(p))[2] == 2023 and read_year(str(p)) == 2023
    assert read_year(str(write_gpx(tmp_path / "b.gpx", [(LAT, 2.0)]))) == 0  # non daté
    # repli TCX de Garmin : pas d'heure par point, la date de l'activité est dans les métadonnées
    q = tmp_path / "c.gpx"
    q.write_bytes(_points_to_gpx_bytes([(LAT, 2.0)], "x", start=datetime(2022, 7, 1, tzinfo=timezone.utc)))
    assert read_year(str(q)) == 2022


def test_each_year_has_its_own_layer(engine, two_years):
    assert engine.pts.years() == [2024, 2025]
    assert engine.year_stats() == {2024: 1, 2025: 1}
    # couche globale : les deux activités ; chaque année : la sienne seulement
    assert engine.get_tile_path(14, TILE.x, TILE.y) and engine.get_tile_path(14, OTHER.x, OTHER.y)
    assert engine.get_tile_path(14, TILE.x, TILE.y, 2024) and not engine.get_tile_path(14, OTHER.x, OTHER.y, 2024)
    assert engine.get_tile_path(14, OTHER.x, OTHER.y, 2025) and not engine.get_tile_path(14, TILE.x, TILE.y, 2025)
    # même dégradé par défaut : la tuile annuelle est identique à la globale là où l'année est seule
    assert np.array_equal(rgba(engine.get_tile_path(14, TILE.x, TILE.y)),
                          rgba(engine.get_tile_path(14, TILE.x, TILE.y, 2024)))


def test_removing_an_activity_clears_its_year(engine, gpx_dir, two_years):
    a, _ = two_years
    a.unlink()
    engine.sync_folder(gpx_dir)
    assert not engine.get_tile_path(14, TILE.x, TILE.y, 2024)
    assert engine.get_tile_path(14, OTHER.x, OTHER.y, 2025)
    engine.rerender_all()
    assert not (engine.tiles_dir / "years" / "2024").exists()
    assert (engine.tiles_dir / "years" / "2025").exists()


def test_year_gradient_only_rerenders_that_year(engine, two_years):
    glob_before = engine.get_tile_path(14, TILE.x, TILE.y).read_bytes()
    other_before = engine.get_tile_path(14, OTHER.x, OTHER.y, 2025).read_bytes()
    assert engine.set_year_gradients({"2024": "fire"}) == {2024}
    engine.rerender_all(years=[2024], include_global=False)
    fire = rgba(engine.get_tile_path(14, TILE.x, TILE.y, 2024))
    peak = fire[..., 3].argmax()
    r, g, b = fire.reshape(-1, 4)[peak][:3]
    assert r > b, "le dégradé « fire » doit donner des tons chauds"
    assert engine.get_tile_path(14, TILE.x, TILE.y).read_bytes() == glob_before
    assert engine.get_tile_path(14, OTHER.x, OTHER.y, 2025).read_bytes() == other_before
    # un recalcul complet garde le choix et ne mélange pas les dossiers des couches
    engine.rerender_all()
    assert np.array_equal(rgba(engine.get_tile_path(14, TILE.x, TILE.y, 2024)), fire)
    assert engine.set_year_gradients({"2024": "fire"}) == set()
    with pytest.raises(ValueError):
        engine.set_year_gradients({"2024": "nope"})


def test_backfill_years_of_an_older_database(config, gpx_dir, two_years, engine):
    # base d'une version précédente : année inconnue, pas de tuiles annuelles
    engine.conn.execute("UPDATE ingested_files SET year = NULL")
    engine.conn.commit()
    engine.close()
    import shutil
    shutil.rmtree(engine.tiles_dir / "years")

    reopened = HeatmapEngine(config)
    try:
        assert reopened.needs_year_backfill() and reopened.pts.years() == []
        assert reopened.backfill_years() == 2
        assert not reopened.needs_year_backfill()
        assert reopened.pts.years() == [2024, 2025]
        assert reopened.get_tile_path(14, TILE.x, TILE.y, 2024)
        assert reopened.get_tile_path(14, OTHER.x, OTHER.y, 2025)
        assert reopened.backfill_years() == 0
    finally:
        reopened.close()
    rows = sqlite3.connect(config["paths"]["db"]).execute("SELECT year FROM ingested_files ORDER BY year").fetchall()
    assert rows == [(2024,), (2025,)]


def test_year_gradient_settings(tmp_path):
    assert validate_year_gradients({2024: "fire", "2025": None}) == {"2024": "fire", "2025": None}
    for bad in ({"24": "fire"}, {"2024": "nope"}, ["fire"]):
        with pytest.raises(ValueError):
            validate_year_gradients(bad)
    s = Settings({"year_gradients": {2023: "green"}}, tmp_path / "settings.json")
    assert s.effective()["year_gradients"] == {"2023": "green"}
    s.set_year_gradients({"2024": "fire", "2023": None})
    assert s.effective()["year_gradients"] == {"2024": "fire"}
    s.set_year_gradients({"2023": "green"})  # retour à la valeur de config.yaml : plus de surcharge
    assert s.overrides()["year_gradients"] == {"2024": "fire"}
    assert set(PRESETS) >= {"ice", "fire", "green"}
