import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List
from xml.sax.saxutils import escape

import yaml
from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, garmin_account, garmin_sync, heatmap, watcher
from .gpx_utils import is_gpx, normalize_path
from .jobs import JobCancelled, JobManager, LogBuffer
from .settings import Settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")
LOG_BUFFER = LogBuffer()
logging.getLogger().addHandler(LOG_BUFFER)
# l'admin interroge l'API toutes les secondes : inutile d'en remplir les logs d'accès
logging.getLogger("uvicorn.access").addFilter(lambda record: "/admin/api/" not in record.getMessage())

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

LAYER = CONFIG["server"]["layer_name"]
TITLE = CONFIG["server"]["layer_title"]
BASE_URL = CONFIG["server"]["base_url"].rstrip("/")
WATCH_DIR = CONFIG["paths"]["gpx_watch"]
DATA_DIR = Path(CONFIG["paths"]["db"]).parent
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
STATIC_DIR = Path(__file__).parent / "static"
STARTED_AT = time.time()
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
SESSION_COOKIE = "wmts_admin"
SESSION_MAX_AGE = 30 * 24 * 3600

settings = Settings(CONFIG, CONFIG["paths"].get("settings") or DATA_DIR / "settings.json")


def _make_engine():
    try:
        return heatmap.HeatmapEngine(settings.effective())
    except ValueError:
        if "render" not in settings.overrides():
            raise
        log.exception("Réglages de rendu de l'admin invalides : retour aux valeurs de config.yaml")
        settings.reset("render")
        return heatmap.HeatmapEngine(settings.effective())


engine = _make_engine()
account = garmin_account.GarminAccount(
    os.environ.get("GARMIN_TOKEN_STORE", "/data/garmin_tokens"),
    DATA_DIR / "garmin_state.json",
    env_email=os.environ.get("GARMIN_EMAIL"),
    env_password=os.environ.get("GARMIN_PASSWORD"),
)
garmin = garmin_sync.GarminSync(WATCH_DIR)
jobs = JobManager()

_garmin_wakeup = threading.Event()
_next_garmin_sync = None


# ---------- tâches ----------

def _rescan_job(job):
    r = engine.sync_folder(WATCH_DIR, progress=job)
    return {"fichiers importés": r["imported"], "fichiers retirés": r["removed"]}


def submit_rescan(label="Analyse du dossier surveillé"):
    return jobs.submit("rescan", label, _rescan_job, unique=True)


def submit_reindex():
    def run(job):
        r = engine.reindex(WATCH_DIR, progress=job)
        return {"fichiers importés": r["imported"], "points": engine.point_count}
    return jobs.submit("reindex", "Réindexation complète", run, unique=True)


def submit_rerender(label="Recalcul de toutes les tuiles"):
    def run(job):
        engine.rerender_all(progress=job)
        return {"tuiles": engine.tile_count(max_age=0)}
    return jobs.submit("rerender", label, run, unique=True)


def submit_retry(paths):
    def run(job):
        return {"fichiers retentés": engine.bulk_ingest(paths, force=True, progress=job)}
    return jobs.submit("retry", f"Nouvel essai de {len(paths)} fichier(s)", run)


def _garmin_job(job):
    cfg = settings.effective()["garmin"]
    client = account.client()
    ingested = set()

    def on_batch(paths):
        engine.bulk_ingest(paths, progress=job)
        ingested.update(paths)

    try:
        new_files = garmin.sync(client, on_batch=on_batch, ingest_batch_size=cfg["ingest_batch_size"],
                                lookback_days=cfg["lookback_days_first_sync"], progress=job)
    except BaseException as e:
        if not isinstance(e, JobCancelled):
            account.invalidate()
            account.record_error(e)
        if any(p not in ingested for p in garmin.downloaded):
            submit_rescan("Rattrapage après une sync Garmin interrompue")
        raise
    account.record_sync(len(new_files))
    return {"nouvelles activités": len(new_files)}


def submit_garmin_sync(label="Synchronisation Garmin"):
    return jobs.submit("garmin", label, _garmin_job, lane="garmin", unique=True)


_watch_lock = threading.Lock()
_watch_ingest, _watch_remove = set(), set()
_watch_job_queued = False


def _on_watch_ready(to_ingest, to_remove):
    """Fichiers prêts signalés par le watcher : regroupés dans une seule tâche tant
    qu'elle n'a pas démarré."""
    global _watch_job_queued
    with _watch_lock:
        _watch_ingest.difference_update(to_remove)
        _watch_remove.difference_update(to_ingest)
        _watch_ingest.update(to_ingest)
        _watch_remove.update(to_remove)
        if _watch_job_queued:
            return
        _watch_job_queued = True
    jobs.submit("watch", "Import automatique (dossier surveillé)", _watch_job)


def _watch_job(job):
    global _watch_job_queued
    with _watch_lock:
        ingest, remove = sorted(_watch_ingest), sorted(_watch_remove)
        _watch_ingest.clear()
        _watch_remove.clear()
        _watch_job_queued = False
    job.label = f"Import automatique : {len(ingest)} fichier(s)" + (f", {len(remove)} retiré(s)" if remove else "")
    removed = engine.remove_files(remove, progress=job) if remove else 0
    imported = engine.bulk_ingest(ingest, progress=job) if ingest else 0
    return {"fichiers importés": imported, "fichiers retirés": removed}


def _garmin_loop():
    """Sync automatique selon les réglages courants (relus à chaque réveil : activer la
    sync, changer l'intervalle ou connecter le compte depuis l'admin prend effet aussitôt)."""
    global _next_garmin_sync
    last_run = None
    while True:
        cfg = settings.effective()["garmin"]
        timeout = None
        if cfg["enabled"] and account.is_configured():
            interval = cfg["sync_interval_minutes"] * 60
            if last_run is None or time.time() >= last_run + interval:
                submit_garmin_sync("Synchronisation Garmin automatique")
                last_run = time.time()
            _next_garmin_sync = last_run + interval
            timeout = max(1.0, _next_garmin_sync - time.time())
        else:
            _next_garmin_sync = None
        _garmin_wakeup.wait(timeout)
        _garmin_wakeup.clear()


@asynccontextmanager
async def lifespan(app):
    # le watcher démarre AVANT l'import initial : rien de ce qui est déposé pendant
    # l'import n'est manqué (tout passe par la même file de tâches)
    observer = watcher.start_watcher(_on_watch_ready, WATCH_DIR, ignore=garmin.is_claimed)
    log.info("Watcher démarré sur %s", WATCH_DIR)
    submit_rescan("Import initial")
    threading.Thread(target=_garmin_loop, daemon=True, name="garmin-loop").start()
    if not ADMIN_TOKEN:
        log.warning("ADMIN_TOKEN non défini : l'administration (/admin) est désactivée.")
    yield
    observer.stop()
    observer.join(timeout=5)


app = FastAPI(title="WMTS GPX Heatmap", version=__version__, lifespan=lifespan)

# CORS ouvert en lecture seule (GET) : nécessaire pour que des applis web en JS
# (Leaflet, OpenLayers, MapLibre GL...) sur un autre domaine/port puissent charger
# les tuiles. Les actions d'administration exigent soit le jeton en en-tête
# Authorization, soit la session de l'admin ET un en-tête X-Requested-With : une page
# tierce ne peut envoyer ni l'un ni l'autre (requête préliminaire CORS refusée).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------- WMTS ----------

def build_capabilities() -> str:
    params = engine.params
    title = escape(TITLE)
    layer = escape(LAYER, {'"': "&quot;"})
    base_url = escape(BASE_URL, {'"': "&quot;"})
    origin_x, origin_y = -20037508.342789244, 20037508.342789244
    matrices = []
    for z in range(params.min_zoom, params.max_zoom + 1):
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
    <ows:Title>{title}</ows:Title>
    <ows:ServiceType>OGC WMTS</ows:ServiceType>
    <ows:ServiceTypeVersion>1.0.0</ows:ServiceTypeVersion>
  </ows:ServiceIdentification>
  <Contents>
    <Layer>
      <ows:Title>{title}</ows:Title>
      <ows:WGS84BoundingBox>
        <ows:LowerCorner>-180 -85.051129</ows:LowerCorner>
        <ows:UpperCorner>180 85.051129</ows:UpperCorner>
      </ows:WGS84BoundingBox>
      <ows:Identifier>{layer}</ows:Identifier>
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
        template="{base_url}/wmts/1.0.0/{layer}/default/GoogleMapsCompatible/{{TileMatrix}}/{{TileRow}}/{{TileCol}}.png"/>
    </Layer>
    <TileMatrixSet>
      <ows:Identifier>GoogleMapsCompatible</ows:Identifier>
      <ows:SupportedCRS>urn:ogc:def:crs:EPSG::3857</ows:SupportedCRS>{matrices_xml}
    </TileMatrixSet>
  </Contents>
  <ServiceMetadataURL xlink:href="{base_url}/wmts?SERVICE=WMTS&amp;REQUEST=GetCapabilities"/>
</Capabilities>"""


_transparent_png = None


def transparent_png() -> bytes:
    global _transparent_png
    if _transparent_png is None:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(buf, format="PNG")
        _transparent_png = buf.getvalue()
    return _transparent_png


# une tuile change à chaque import touchant sa zone : le client revalide à chaque
# fois (réponse 304 sans contenu si son ETag est toujours valable)
TILE_HEADERS = {"Cache-Control": "no-cache"}


def serve_tile(z: int, x: int, y: int, request: Request) -> Response:
    try:
        with open(engine.tile_path(z, x, y), "rb") as f:
            content = f.read()
            st = os.fstat(f.fileno())
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    except (FileNotFoundError, NotADirectoryError):
        content, etag = transparent_png(), '"empty"'

    headers = {**TILE_HEADERS, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=content, media_type="image/png", headers=headers)


@app.get("/wmts")
def wmts_kvp(request: Request):
    # les noms de paramètres KVP sont insensibles à la casse (norme OGC)
    params = {k.upper(): v for k, v in request.query_params.items()}
    req = params.get("REQUEST", "").upper()
    if req == "GETCAPABILITIES":
        return Response(content=build_capabilities(), media_type="application/xml")
    if req == "GETTILE":
        try:
            # TileMatrix vaut normalement "12", mais certains clients envoient "EPSG:3857:12"
            z = int(params["TILEMATRIX"].rsplit(":", 1)[-1])
            y = int(params["TILEROW"])
            x = int(params["TILECOL"])
        except (KeyError, ValueError):
            return Response(status_code=400, content="Paramètres TILEMATRIX / TILEROW / TILECOL manquants ou invalides")
        return serve_tile(z, x, y, request)
    return Response(status_code=400, content="REQUEST non supportée (GetCapabilities / GetTile)")


@app.get("/wmts/1.0.0/{layer}/default/GoogleMapsCompatible/{z}/{y}/{x}.png")
def wmts_restful(layer: str, z: int, y: int, x: int, request: Request):
    return serve_tile(z, x, y, request)


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__, "points": engine.point_count}


# ---------- authentification de l'admin ----------

def _session_value() -> str:
    # dérivée du jeton : survit aux redémarrages, et changer ADMIN_TOKEN ferme les sessions
    return hmac.new(ADMIN_TOKEN.encode(), b"wmts-gpx-heatmap admin session", hashlib.sha256).hexdigest()


def _has_session(request: Request) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE, "")
    return bool(ADMIN_TOKEN) and secrets.compare_digest(cookie.encode(), _session_value().encode())


def require_admin(request: Request, authorization: str = Header(None)):
    if not ADMIN_TOKEN:
        raise HTTPException(403, "Administration désactivée : définir ADMIN_TOKEN (voir README).")
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() == "bearer" and secrets.compare_digest(supplied.encode(), ADMIN_TOKEN.encode()):
        return
    if _has_session(request):
        if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "admin-ui":
            raise HTTPException(403, "En-tête X-Requested-With manquant")
        return
    raise HTTPException(401, "Authentification requise", headers={"WWW-Authenticate": "Bearer"})


public_admin = APIRouter(prefix="/admin")
admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


class LoginBody(BaseModel):
    token: str


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(STATIC_DIR / "admin.html", headers={"Cache-Control": "no-cache"})


@app.get("/admin/", include_in_schema=False)
def admin_page_slash():
    return RedirectResponse("/admin")


@public_admin.get("/api/session")
def admin_session(request: Request):
    return {"admin_enabled": bool(ADMIN_TOKEN), "authenticated": _has_session(request)}


@public_admin.post("/api/login")
def admin_login(body: LoginBody, request: Request, response: Response):
    if not ADMIN_TOKEN:
        raise HTTPException(403, "Administration désactivée : définir ADMIN_TOKEN (voir README).")
    if not secrets.compare_digest(body.token.encode(), ADMIN_TOKEN.encode()):
        time.sleep(1)  # freine les essais en rafale
        raise HTTPException(401, "Jeton invalide")
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(SESSION_COOKIE, _session_value(), max_age=SESSION_MAX_AGE, httponly=True,
                        samesite="strict", secure=secure, path="/admin")
    return {"authenticated": True}


@public_admin.post("/api/logout")
def admin_logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/admin")
    return {"authenticated": False}


# ---------- API d'administration ----------

def _job_response(submitted):
    job, created = submitted
    return {"job": job.to_dict(), "created": created}


def _garmin_status():
    cfg = settings.effective()["garmin"]
    active = jobs.active("garmin")
    return {**account.status(), "enabled": cfg["enabled"], "next_sync_at": _next_garmin_sync,
            "sync_job": active[0].to_dict() if active else None}


@admin.get("/api/status")
def api_status():
    params = engine.params
    return {
        "service": {
            "version": __version__,
            "uptime_s": round(time.time() - STARTED_AT),
            "watch_dir": WATCH_DIR,
            "base_url": BASE_URL,
            "layer": LAYER,
            "config_path": CONFIG_PATH,
            "zoom": [params.min_zoom, params.max_zoom],
        },
        "stats": engine.stats(),
        "garmin": _garmin_status(),
        "jobs": jobs.list(30),
    }


@admin.get("/api/jobs")
def api_jobs(limit: int = 50):
    return jobs.list(min(max(limit, 1), 100))


@admin.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: int):
    if not jobs.cancel(job_id):
        raise HTTPException(404, "Tâche introuvable ou déjà terminée")
    return {"cancelled": True}


ACTIONS = {
    "rescan": submit_rescan,
    "reindex": submit_reindex,
    "rerender": submit_rerender,
    "garmin-sync": submit_garmin_sync,
}


@admin.post("/api/actions/{action}", status_code=202)
def api_action(action: str):
    if action not in ACTIONS:
        raise HTTPException(404, "Action inconnue")
    if action == "garmin-sync" and not account.is_configured():
        raise HTTPException(400, "Compte Garmin non connecté")
    return _job_response(ACTIONS[action]())


@admin.get("/api/files")
def api_files(q: str = "", status: str = "all", offset: int = 0, limit: int = 50):
    return engine.list_files(q, status, max(offset, 0), min(max(limit, 1), 500))


class RetryBody(BaseModel):
    paths: List[str] = []
    all_failed: bool = False


@admin.post("/api/files/retry", status_code=202)
def api_retry(body: RetryBody):
    watch = normalize_path(WATCH_DIR)
    paths = engine.failed_files() if body.all_failed else body.paths
    paths = [p for p in (normalize_path(p) for p in paths) if os.path.dirname(p) == watch]
    if not paths:
        raise HTTPException(400, "Aucun fichier à retenter")
    return _job_response(submit_retry(paths))


@admin.put("/api/files/upload")
async def api_upload(request: Request, name: str):
    """Dépose un GPX dans le dossier surveillé (corps de la requête = contenu du fichier) ;
    le watcher l'importe ensuite comme n'importe quel fichier déposé."""
    filename = os.path.basename(name.replace("\\", "/")).strip()
    if not is_gpx(filename) or filename.startswith(".") or len(filename) > 200 \
            or any(ord(c) < 32 for c in filename):
        raise HTTPException(400, "Nom de fichier invalide (fichier .gpx attendu)")
    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Fichier trop volumineux (50 Mo max)")
    data = await request.body()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Fichier trop volumineux (50 Mo max)")

    def write():
        target = Path(WATCH_DIR) / filename
        existed = target.exists()
        tmp = target.with_name(target.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return existed

    replaced = await run_in_threadpool(write)
    log.info("Fichier envoyé depuis l'admin : %s%s", filename, " (remplacé)" if replaced else "")
    return {"name": filename, "replaced": replaced}


@admin.get("/api/settings")
def api_settings():
    eff = settings.effective()
    return {
        "effective": {"render": eff["render"], "color_gradient": eff["color_gradient"], "garmin": eff["garmin"]},
        "overrides": settings.overrides(),
        "defaults": {"render": CONFIG["render"], "color_gradient": CONFIG["color_gradient"],
                     "garmin": CONFIG.get("garmin") or {}},
    }


class RenderBody(BaseModel):
    render: dict
    color_gradient: list


@admin.put("/api/settings/render", status_code=202)
def api_set_render(body: RenderBody):
    render = {**settings.effective()["render"], **body.render}
    try:
        engine.set_render_params(render, body.color_gradient)
    except ValueError as e:
        raise HTTPException(400, str(e))
    settings.set_render(render, body.color_gradient)
    log.info("Réglages de rendu modifiés depuis l'admin")
    return _job_response(submit_rerender("Recalcul des tuiles (nouveaux réglages de rendu)"))


@admin.delete("/api/settings/render", status_code=202)
def api_reset_render():
    settings.reset("render")
    eff = settings.effective()
    engine.set_render_params(eff["render"], eff["color_gradient"])
    log.info("Réglages de rendu remis aux valeurs de config.yaml")
    return _job_response(submit_rerender("Recalcul des tuiles (réglages de config.yaml)"))


@admin.put("/api/settings/garmin")
def api_set_garmin(values: dict = Body(...)):
    try:
        settings.set_garmin(values)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _garmin_wakeup.set()
    return settings.effective()["garmin"]


@admin.delete("/api/settings/garmin")
def api_reset_garmin():
    settings.reset("garmin")
    _garmin_wakeup.set()
    return settings.effective()["garmin"]


@admin.get("/api/preview/{z}/{x}/{y}.png")
def api_preview(z: int, x: int, y: int, s: str):
    """Tuile rendue avec des réglages candidats (aperçu en direct de l'onglet Rendu)."""
    try:
        candidate = json.loads(s)
        params = heatmap.RenderParams.from_config(candidate["render"], candidate["color_gradient"])
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, str(e))
    content = engine.render_preview(z, x, y, params) or transparent_png()
    return Response(content=content, media_type="image/png", headers={"Cache-Control": "no-store"})


@admin.get("/api/garmin")
def api_garmin():
    return _garmin_status()


class GarminLoginBody(BaseModel):
    email: str
    password: str
    remember: bool = False


def _garmin_call(fn, *args):
    try:
        return fn(*args)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        name = e.__class__.__name__
        log.warning("Connexion Garmin refusée : %s: %s", name, e)
        status = 429 if "TooManyRequests" in name else 400
        raise HTTPException(status, f"Garmin a refusé la connexion : {e}")


@admin.post("/api/garmin/login")
def api_garmin_login(body: GarminLoginBody):
    result = _garmin_call(account.start_login, body.email.strip(), body.password, body.remember)
    if result["state"] == "connected":
        _garmin_wakeup.set()  # première sync aussitôt si la sync automatique est activée
    return _garmin_status()


class MfaBody(BaseModel):
    code: str


@admin.post("/api/garmin/mfa")
def api_garmin_mfa(body: MfaBody):
    _garmin_call(account.submit_mfa, body.code)
    _garmin_wakeup.set()
    return _garmin_status()


@admin.post("/api/garmin/mfa/cancel")
def api_garmin_mfa_cancel():
    account.cancel_login()
    return _garmin_status()


@admin.post("/api/garmin/logout")
def api_garmin_logout():
    account.logout()
    _garmin_wakeup.set()
    return _garmin_status()


@admin.post("/api/garmin/forget-password")
def api_garmin_forget_password():
    account.forget_password()
    return _garmin_status()


@admin.get("/api/logs")
def api_logs(after: int = 0, level: str = "INFO"):
    return LOG_BUFFER.since(after, level.upper())


# --- compatibilité : anciens endpoints, désormais des tâches de fond ---

@admin.post("/rerender", status_code=202)
def admin_rerender():
    return _job_response(submit_rerender())


@admin.post("/bulk-import", status_code=202)
def admin_bulk_import():
    return _job_response(submit_rescan())


@admin.post("/reindex", status_code=202)
def admin_reindex():
    return _job_response(submit_reindex())


@admin.post("/garmin-sync", status_code=202)
def admin_garmin_sync():
    return api_action("garmin-sync")


@admin.get("/config-status")
def admin_config_status():
    """Configuration réellement utilisée par ce processus (config.yaml + réglages de l'admin)."""
    return {
        "config_path": CONFIG_PATH,
        **api_settings(),
        "garmin": _garmin_status(),
        "points_en_memoire": engine.point_count,
        "gpx_watch_dir": WATCH_DIR,
    }


class RevalidatedStaticFiles(StaticFiles):
    """Fichiers de l'admin toujours revalidés (ETag) : une mise à jour du conteneur est
    prise en compte sans vider le cache du navigateur."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.include_router(public_admin)
app.include_router(admin)
app.mount("/admin/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="admin-static")
