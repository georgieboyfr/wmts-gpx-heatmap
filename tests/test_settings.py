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
