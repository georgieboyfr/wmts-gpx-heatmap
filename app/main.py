import io
import logging
import os
import threading
import time

import yaml
from fastapi import FastAPI, Query, Response
from fastapi.middleware.cors import CORSMiddleware

from . import heatmap, watcher

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

LAYER = CONFIG["server"]["layer_name"]
TITLE = CONFIG["server"]["layer_title"]
BASE_URL = CONFIG["server"]["base_url"].rstrip("/")
MIN_Z = CONFIG["render"]["min_zoom"]
MAX_Z = CONFIG["render"]["max_zoom"]

app = FastAPI(title="WMTS GPX Heatmap")

# CORS ouvert en lecture seule (GET) : nécessaire pour que des applis web en JS
# (Leaflet, OpenLayers, MapLibre GL...) sur un autre domaine/port puissent charger
# les tuiles. Pas de données sensibles ni d'écriture exposées via ces routes.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
engine = heatmap.HeatmapEngine(CONFIG)

_transparent_png = None


def transparent_png() -> bytes:
    global _transparent_png
    if _transparent_png is None:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(buf, format="PNG")
        _transparent_png = buf.getvalue()
    return _transparent_png


@app.on_event("startup")
def _startup():
    watch_dir = CONFIG["paths"]["gpx_watch"]
    watcher.start_watcher(engine, watch_dir)
    log.info("Watcher démarré sur %s", watch_dir)

    garmin_cfg = CONFIG.get("garmin", {})
    if garmin_cfg.get("enabled"):
        threading.Thread(target=_garmin_sync_loop, args=(garmin_cfg, watch_dir), daemon=True).start()
        log.info("Synchronisation Garmin Connect activée (toutes les %s min)",
                  garmin_cfg.get("sync_interval_minutes", 60))


def _garmin_sync_loop(garmin_cfg, watch_dir):
    from . import garmin_sync

    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    token_store = os.environ.get("GARMIN_TOKEN_STORE", "/data/garmin_tokens")

    if not email or not password:
        log.error("GARMIN_EMAIL / GARMIN_PASSWORD non définis : synchronisation Garmin désactivée.")
        return

    syncer = garmin_sync.GarminSync(
        email, password, watch_dir, token_store,
        lookback_days=garmin_cfg.get("lookback_days_first_sync", 365),
    )
    interval_s = garmin_cfg.get("sync_interval_minutes", 60) * 60

    while True:
        try:
            def _ingest_batch(paths):
                log.info("Garmin: lot de %d nouveau(x) GPX, ingestion...", len(paths))
                engine.bulk_ingest([str(p) for p in paths])

            new_files = syncer.sync(
                on_batch=_ingest_batch,
                ingest_batch_size=garmin_cfg.get("ingest_batch_size", 0),
            )
            if not new_files:
                log.info("Garmin: rien de nouveau.")
        except Exception:
            log.exception("Echec de la synchronisation Garmin")
        time.sleep(interval_s)


@app.post("/admin/garmin-sync")
def admin_garmin_sync():
    """Déclenche une synchronisation Garmin immédiate (en plus du cycle automatique)."""
    garmin_cfg = CONFIG.get("garmin", {})
    if not garmin_cfg.get("enabled"):
        return Response(status_code=400, content="Synchronisation Garmin désactivée dans config.yaml")

    from . import garmin_sync
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    token_store = os.environ.get("GARMIN_TOKEN_STORE", "/data/garmin_tokens")
    watch_dir = CONFIG["paths"]["gpx_watch"]

    syncer = garmin_sync.GarminSync(
        email, password, watch_dir, token_store,
        lookback_days=garmin_cfg.get("lookback_days_first_sync", 365),
    )
    new_files = syncer.sync(
        on_batch=lambda paths: engine.bulk_ingest([str(p) for p in paths]),
        ingest_batch_size=garmin_cfg.get("ingest_batch_size", 0),
    )
    return {"nouveaux_fichiers": len(new_files)}


def build_capabilities() -> str:
    origin_x, origin_y = -20037508.342789244, 20037508.342789244
    matrices = []
    for z in range(MIN_Z, MAX_Z + 1):
        scale_denom = 559082264.028717 / (2 ** z)
        matrix_wh = 2 ** z
        matrices.append(f"""
      <TileMatrix>
        <ows:Identifier>{z}</ows:Identifier>
        <ScaleDenominator>{scale_denom:.6f}</ScaleDenominator>
        <TopLeftCorner>{origin_x} {origin_y}</TopLeftCorner>
        <TileWidth>256</TileWidth>
        <TileHeight>256</TileHeight>
        <MatrixWidth>{matrix_wh}</MatrixWidth>
        <MatrixHeight>{matrix_wh}</MatrixHeight>
      </TileMatrix>""")
    matrices_xml = "".join(matrices)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Capabilities xmlns="http://www.opengis.net/wmts/1.0"
              xmlns:ows="http://www.opengis.net/ows/1.1"
              xmlns:xlink="http://www.w3.org/1999/xlink"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              version="1.0.0">
  <ows:ServiceIdentification>
    <ows:Title>{TITLE}</ows:Title>
    <ows:ServiceType>OGC WMTS</ows:ServiceType>
    <ows:ServiceTypeVersion>1.0.0</ows:ServiceTypeVersion>
  </ows:ServiceIdentification>
  <Contents>
    <Layer>
      <ows:Title>{TITLE}</ows:Title>
      <ows:Identifier>{LAYER}</ows:Identifier>
      <ows:BoundingBox crs="urn:ogc:def:crs:EPSG::3857">
        <ows:LowerCorner>-20037508.342789 -20037508.342789</ows:LowerCorner>
        <ows:UpperCorner>20037508.342789 20037508.342789</ows:UpperCorner>
      </ows:BoundingBox>
      <Style isDefault="true">
        <ows:Identifier>default</ows:Identifier>
      </Style>
      <Format>image/png</Format>
      <TileMatrixSetLink>
        <TileMatrixSet>GoogleMapsCompatible</TileMatrixSet>
      </TileMatrixSetLink>
      <ResourceURL format="image/png" resourceType="tile"
        template="{BASE_URL}/wmts/1.0.0/{LAYER}/default/GoogleMapsCompatible/{{TileMatrix}}/{{TileRow}}/{{TileCol}}.png"/>
    </Layer>
    <TileMatrixSet>
      <ows:Identifier>GoogleMapsCompatible</ows:Identifier>
      <ows:SupportedCRS>urn:ogc:def:crs:EPSG::3857</ows:SupportedCRS>{matrices_xml}
    </TileMatrixSet>
  </Contents>
  <ServiceMetadataURL xlink:href="{BASE_URL}/wmts?SERVICE=WMTS&amp;REQUEST=GetCapabilities"/>
</Capabilities>"""


def serve_tile(z: int, x: int, y: int) -> Response:
    p = engine.get_tile_path(z, x, y)
    if p is not None:
        return Response(content=p.read_bytes(), media_type="image/png")
    return Response(content=transparent_png(), media_type="image/png")


@app.get("/wmts")
def wmts_kvp(
    SERVICE: str = Query(None),
    REQUEST: str = Query(None),
    LAYER: str = Query(None),
    TILEMATRIX: str = Query(None),
    TILEROW: int = Query(None),
    TILECOL: int = Query(None),
    FORMAT: str = Query(None),
):
    req = (REQUEST or "").upper()
    if req == "GETCAPABILITIES":
        return Response(content=build_capabilities(), media_type="application/xml")
    if req == "GETTILE":
        if TILEMATRIX is None or TILEROW is None or TILECOL is None:
            return Response(status_code=400, content="Paramètres manquants")
        return serve_tile(int(TILEMATRIX), int(TILECOL), int(TILEROW))
    return Response(status_code=400, content="REQUEST non supportée (GetCapabilities / GetTile)")


@app.get("/wmts/1.0.0/{layer}/default/GoogleMapsCompatible/{z}/{y}/{x}.png")
def wmts_restful(layer: str, z: int, y: int, x: int):
    return serve_tile(z, x, y)


@app.post("/admin/rerender")
def admin_rerender():
    """Force un recalcul complet de toutes les tuiles (après modif du dégradé/config par ex.)."""
    engine.rerender_all()
    return {"status": "ok"}


@app.post("/admin/bulk-import")
def admin_bulk_import():
    """Importe en UN SEUL LOT tous les .gpx du dossier surveillé qui ne sont pas encore
    ingérés. À utiliser quand beaucoup de fichiers ont été déposés d'un coup pendant que
    le conteneur tournait déjà (le watcher les traite alors un par un, plus lentement) :
    dépose tous tes fichiers, puis appelle cet endpoint une fois, plutôt que de laisser
    le watcher les traiter un par un au fil de l'eau."""
    from pathlib import Path
    watch_dir = CONFIG["paths"]["gpx_watch"]
    pending = [str(p) for p in Path(watch_dir).glob("*.gpx") if not engine.already_ingested(str(p))]
    if not pending:
        return {"fichiers_importes": 0}
    engine.bulk_ingest(pending)
    return {"fichiers_importes": len(pending)}


@app.get("/health")
def health():
    return {"status": "ok", "points": int(len(engine.lat))}


@app.get("/admin/config-status")
def admin_config_status():
    """Montre la config RÉELLEMENT chargée en mémoire par ce processus (pas ce qui est
    écrit sur disque dans config.yaml). Utile pour diagnostiquer un "j'ai modifié la
    config mais rien ne change" : si les valeurs affichées ici ne correspondent pas à
    ton fichier, c'est qu'un redémarrage du conteneur est nécessaire pour la recharger."""
    garmin_cfg = CONFIG.get("garmin", {})
    return {
        "config_path": CONFIG_PATH,
        "render": CONFIG.get("render", {}),
        "garmin_enabled": bool(garmin_cfg.get("enabled")),
        "garmin_sync_interval_minutes": garmin_cfg.get("sync_interval_minutes"),
        "garmin_credentials_present": bool(os.environ.get("GARMIN_EMAIL") and os.environ.get("GARMIN_PASSWORD")),
        "points_en_memoire": int(len(engine.lat)),
        "gpx_watch_dir": CONFIG["paths"]["gpx_watch"],
    }
