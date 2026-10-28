import importlib
import json
import sys
import xml.etree.ElementTree as ET

import mercantile
import pytest
import yaml
from fastapi.testclient import TestClient

from .conftest import make_config, wait_until, write_gpx
from .fake_garmin import MFA_CODE, PASSWORD, FakeGarmin

TOKEN = "s3cret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
UI = {"X-Requested-With": "admin-ui"}


@pytest.fixture
def main(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["server"]["layer_title"] = 'Heat & "map" <test>'
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("GARMIN_TOKEN_STORE", str(tmp_path / "tokens"))
    monkeypatch.delenv("GARMIN_EMAIL", raising=False)
    monkeypatch.delenv("GARMIN_PASSWORD", raising=False)
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    module.account._garmin_class = lambda: FakeGarmin
    yield module
    module.engine.close()
    sys.modules.pop("app.main", None)


@pytest.fixture
def client(main):
    return TestClient(main.app)  # sans "with" : ni watcher ni boucle Garmin


def wait_job(main, response):
    job = main.jobs.get(response.json()["job"]["id"])
    assert job.wait(15)
    return job


def test_capabilities_is_valid_xml_with_escaped_title(client):
    r = client.get("/wmts", params={"service": "WMTS", "request": "GetCapabilities"})
    assert r.status_code == 200
    root = ET.fromstring(r.content)
    assert root.find(".//ows:Title", {"ows": "http://www.opengis.net/ows/1.1"}).text == 'Heat & "map" <test>'


def test_tiles_kvp_restful_and_etag(main, client, tmp_path):
    main.engine.bulk_ingest([write_gpx(tmp_path / "gpx" / "a.gpx", [(48.85, 2.0), (48.85, 2.01)])])
    t = mercantile.tile(2.005, 48.85, 14)

    r = client.get("/wmts", params={"Request": "GetTile", "TileMatrix": "EPSG:3857:14",
                                    "TileRow": t.y, "TileCol": t.x})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    etag = r.headers["etag"]
    assert etag != '"empty"'
    r2 = client.get(f"/wmts/1.0.0/heatmap/default/GoogleMapsCompatible/14/{t.y}/{t.x}.png",
                    headers={"If-None-Match": etag})
    assert r2.status_code == 304
    empty = client.get("/wmts/1.0.0/heatmap/default/GoogleMapsCompatible/14/0/0.png")
    assert empty.status_code == 200 and empty.headers["etag"] == '"empty"'
    assert client.get("/wmts", params={"REQUEST": "GetTile", "TILEMATRIX": "x"}).status_code == 400


# ---------- authentification ----------

def test_admin_page_is_public_but_api_is_not(client):
    assert client.get("/admin").status_code == 200
    assert client.get("/admin/static/admin.js").status_code == 200
    assert client.get("/admin/api/session").json() == {"admin_enabled": True, "authenticated": False}
    assert client.get("/admin/api/status").status_code == 401
    assert client.post("/admin/rerender").status_code == 401
    assert client.get("/admin/config-status", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_session_login_and_csrf_header(client):
    assert client.post("/admin/api/login", json={"token": "faux"}).status_code == 401
    r = client.post("/admin/api/login", json={"token": TOKEN})
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower()
    assert client.get("/admin/api/session").json()["authenticated"]
    assert client.get("/admin/api/status").status_code == 200
    # avec le seul cookie, une action doit porter l'en-tête ajouté par l'interface
    assert client.post("/admin/api/actions/rescan").status_code == 403
    assert client.post("/admin/api/actions/rescan", headers=UI).status_code == 202
    client.post("/admin/api/logout", headers=UI)
    assert client.get("/admin/api/status").status_code == 401


def test_admin_disabled_without_token(main, client, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_TOKEN", "")
    assert client.get("/admin/api/session").json()["admin_enabled"] is False
    assert client.post("/admin/api/login", json={"token": ""}).status_code == 403
    assert client.post("/admin/rerender", headers={"Authorization": "Bearer "}).status_code == 403


# ---------- tâches ----------

def test_actions_run_as_jobs_with_status(main, client, tmp_path):
    write_gpx(tmp_path / "gpx" / "a.gpx", [(48.85, 2.0), (48.85, 2.01)])
    r = client.post("/admin/api/actions/rescan", headers=AUTH)
    assert r.status_code == 202
    job = wait_job(main, r)
    assert job.status == "done" and job.result == {"fichiers importés": 1, "fichiers retirés": 0}

    status = client.get("/admin/api/status", headers=AUTH).json()
    assert status["stats"]["files_ok"] == 1 and status["stats"]["points"] == 2
    assert status["stats"]["data_bounds"] is not None
    assert status["jobs"][0]["id"] == job.id
    assert [p["name"] for p in status["jobs"][0]["phases"]][:2] == ["Analyse du dossier", "Lecture des GPX"]

    job = wait_job(main, client.post("/admin/api/actions/reindex", headers=AUTH))
    assert job.status == "done" and job.result["fichiers importés"] == 1
    assert client.post("/admin/api/actions/nope", headers=AUTH).status_code == 404
    assert client.post("/admin/api/actions/garmin-sync", headers=AUTH).status_code == 400


def test_legacy_endpoints_return_jobs(main, client):
    r = client.post("/admin/bulk-import", headers=AUTH)
    assert r.status_code == 202 and "job" in r.json()
    wait_job(main, r)
    assert client.get("/admin/config-status", headers=AUTH).status_code == 200


# ---------- fichiers ----------

def test_upload_list_and_retry(main, client, tmp_path):
    r = client.put("/admin/api/files/upload", params={"name": "../../sortie.gpx"}, headers=AUTH,
                   content=b"<gpx><trk><trkseg><trkpt lat=")
    assert r.status_code == 200 and r.json()["name"] == "sortie.gpx"  # nom ramené au dossier surveillé
    assert (tmp_path / "gpx" / "sortie.gpx").exists()
    assert client.put("/admin/api/files/upload", params={"name": "x.txt"}, headers=AUTH, content=b"x").status_code == 400

    wait_job(main, client.post("/admin/api/actions/rescan", headers=AUTH))
    files = client.get("/admin/api/files", params={"status": "failed"}, headers=AUTH).json()
    assert files["total"] == 1 and files["items"][0]["error"]

    # corrigé sur disque sans changer de signature détectable ? le bouton "réessayer" force la relecture
    write_gpx(tmp_path / "gpx" / "sortie.gpx", [(48.85, 2.0), (48.85, 2.01)])
    job = wait_job(main, client.post("/admin/api/files/retry", json={"all_failed": True}, headers=AUTH))
    assert job.result == {"fichiers retentés": 1}
    assert client.get("/admin/api/files", params={"q": "sortie"}, headers=AUTH).json()["items"][0]["status"] == "ok"
    assert client.post("/admin/api/files/retry", json={"paths": ["/etc/passwd"]}, headers=AUTH).status_code == 400


# ---------- réglages ----------

def test_render_settings_preview_and_apply(main, client, tmp_path):
    main.engine.bulk_ingest([write_gpx(tmp_path / "gpx" / "a.gpx", [(48.85, 2.0), (48.85, 2.01)])])
    settings = client.get("/admin/api/settings", headers=AUTH).json()
    cfg = {"render": {**settings["effective"]["render"], "line_width_px": 6},
           "color_gradient": settings["effective"]["color_gradient"]}
    t = mercantile.tile(2.005, 48.85, 14)

    preview = client.get(f"/admin/api/preview/14/{t.x}/{t.y}.png", params={"s": json.dumps(cfg)}, headers=AUTH)
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/png"
    assert preview.content != main.transparent_png()
    bad = {**cfg, "render": {**cfg["render"], "color_saturation": 0}}
    assert client.get("/admin/api/preview/14/0/0.png", params={"s": json.dumps(bad)}, headers=AUTH).status_code == 400
    assert client.put("/admin/api/settings/render", json=bad, headers=AUTH).status_code == 400

    r = client.put("/admin/api/settings/render", json={**cfg, "render": {**cfg["render"], "max_zoom": 13}}, headers=AUTH)
    assert r.status_code == 202
    assert wait_job(main, r).status == "done"
    assert main.engine.params.line_width_px == 6
    assert not (main.engine.tiles_dir / "14").exists()  # zoom retiré : tuiles supprimées
    caps = client.get("/wmts", params={"REQUEST": "GetCapabilities"}).text
    assert "<ows:Identifier>13</ows:Identifier>" in caps and "<ows:Identifier>14</ows:Identifier>" not in caps

    wait_job(main, client.delete("/admin/api/settings/render", headers=AUTH))
    assert main.engine.params.max_zoom == 14


def test_garmin_settings(main, client):
    r = client.put("/admin/api/settings/garmin", json={"enabled": True, "sync_interval_minutes": 15}, headers=AUTH)
    assert r.status_code == 200 and r.json()["sync_interval_minutes"] == 15
    assert client.put("/admin/api/settings/garmin", json={"sync_interval_minutes": 1}, headers=AUTH).status_code == 400
    assert client.delete("/admin/api/settings/garmin", headers=AUTH).json()["enabled"] is False


# ---------- Garmin ----------

def test_garmin_login_with_mfa_from_admin(main, client, monkeypatch):
    monkeypatch.setattr(FakeGarmin, "needs_mfa", True)
    r = client.post("/admin/api/garmin/login", json={"email": "a@b.c", "password": "faux"}, headers=AUTH)
    assert r.status_code == 400 and "refusé" in r.json()["detail"]

    r = client.post("/admin/api/garmin/login", json={"email": "a@b.c", "password": PASSWORD, "remember": False},
                    headers=AUTH)
    assert r.json()["state"] == "mfa_pending"
    assert client.post("/admin/api/garmin/mfa", json={"code": "000"}, headers=AUTH).status_code == 400
    g = client.post("/admin/api/garmin/mfa", json={"code": MFA_CODE}, headers=AUTH).json()
    assert g["state"] == "connected" and g["account_name"] == "Camille Test" and not g["password_remembered"]

    job = wait_job(main, client.post("/admin/api/actions/garmin-sync", headers=AUTH))
    assert job.status == "done" and job.result == {"nouvelles activités": 0}
    assert client.get("/admin/api/garmin", headers=AUTH).json()["last_sync_at"]

    assert client.post("/admin/api/garmin/logout", headers=AUTH).json()["state"] == "disconnected"


def test_logs_endpoint(main, client):
    import logging
    logging.getLogger("test").warning("message de test")
    entries = client.get("/admin/api/logs", params={"level": "WARNING"}, headers=AUTH).json()
    assert any(e["message"] == "message de test" for e in entries)


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "version": "1.0.0", "points": 0}
