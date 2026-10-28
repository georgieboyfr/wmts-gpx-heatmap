import pytest

from app.settings import Settings

from .conftest import make_config


def test_overrides_are_layered_on_config_and_persisted(tmp_path):
    base = make_config(tmp_path)
    path = tmp_path / "settings.json"
    s = Settings(base, path)
    render = {**base["render"], "line_width_px": 5}
    s.set_render(render, base["color_gradient"])
    s.set_garmin({"enabled": True, "sync_interval_minutes": 30})

    # seules les valeurs différentes de config.yaml deviennent des surcharges
    assert s.overrides() == {"render": {"line_width_px": 5}, "garmin": {"enabled": True, "sync_interval_minutes": 30}}

    reloaded = Settings(base, path)
    eff = reloaded.effective()
    assert eff["render"]["line_width_px"] == 5
    assert eff["render"]["blur_sigma_px"] == base["render"]["blur_sigma_px"]
    assert eff["garmin"]["enabled"] is True and eff["garmin"]["lookback_days_first_sync"] == 365

    reloaded.reset("render")
    assert reloaded.effective()["render"] == base["render"]


@pytest.mark.parametrize("values", [{"enabled": "oui"}, {"sync_interval_minutes": 1}, {"inconnu": 1},
                                    {"ingest_batch_size": True}])
def test_invalid_garmin_settings_are_rejected(tmp_path, values):
    s = Settings(make_config(tmp_path), tmp_path / "settings.json")
    with pytest.raises(ValueError):
        s.set_garmin(values)


def test_unreadable_settings_file_falls_back_to_config(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{pas du json", encoding="utf-8")
    base = make_config(tmp_path)
    assert Settings(base, path).effective()["render"] == base["render"]


def test_load_config_falls_back_to_default_and_applies_base_url(tmp_path):
    from pathlib import Path
    from app.settings import load_config
    default = Path(__file__).resolve().parent.parent / "config.yaml"
    path, cfg = load_config(tmp_path / "absent.yaml", default, "https://heatmap.example.test")
    assert path == str(default)
    assert cfg["server"]["base_url"] == "https://heatmap.example.test"
    assert cfg["paths"]["db"].startswith("/data/")  # chemins prévus pour les volumes de l'image

    custom = tmp_path / "config.yaml"
    custom.write_text("server: {base_url: 'http://x', layer_name: h, layer_title: H}\n", encoding="utf-8")
    assert load_config(custom, default)[1]["server"]["base_url"] == "http://x"
