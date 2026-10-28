"""
Synchronisation Garmin Connect -> dossier GPX surveillé.

Utilise la librairie non-officielle "garminconnect" (cyberjunky), qui reproduit
le flux d'authentification de l'appli mobile Garmin Connect. Ce n'est PAS une
API publique sanctionnée par Garmin : ça peut casser si Garmin change son flux
d'auth, et l'usage automatisé est à tes risques vis-à-vis de leurs CGU (usage
personnel, comparable à d'autres outils d'auto-hébergement type GarminDB -
largement utilisé par la communauté, mais bon à savoir).

La connexion au compte (code MFA compris) se fait depuis l'admin, voir
garmin_account.py.
"""
import io
import logging
import os
import threading
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from .gpx_utils import normalize_path
from .jobs import NULL_PROGRESS

log = logging.getLogger("garmin_sync")


def _strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _tcx_to_points(data: bytes):
    """Extrait les (lat, lon) d'un export TCX (structure Trackpoint/Position/
    LatitudeDegrees-LongitudeDegrees). Ignore les namespaces XML."""
    points = []
    root = ET.fromstring(data)
    for elem in root.iter():
        if _strip_ns(elem.tag) != "Trackpoint":
            continue
        lat = lon = None
        for child in elem:
            if _strip_ns(child.tag) != "Position":
                continue
            for pchild in child:
                ptag = _strip_ns(pchild.tag)
                if ptag == "LatitudeDegrees":
                    lat = float(pchild.text)
                elif ptag == "LongitudeDegrees":
                    lon = float(pchild.text)
        if lat is not None and lon is not None:
            points.append((lat, lon))
    return points


def _points_to_gpx_bytes(points, name: str) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="wmts-gpx-heatmap-garmin-fallback-tcx">',
        f"<trk><name>{escape(name)}</name><trkseg>",
    ]
    for lat, lon in points:
        lines.append(f'<trkpt lat="{lat:.7f}" lon="{lon:.7f}"></trkpt>')
    lines.append("</trkseg></trk></gpx>")
    return "\n".join(lines).encode("utf-8")


DOWNLOAD_PHASE = "Téléchargement des activités"


class GarminSync:
    """Téléchargement des nouvelles activités vers le dossier surveillé. La connexion
    au compte est gérée à part (voir garmin_account.py) : sync() reçoit un client déjà
    connecté."""

    def __init__(self, gpx_dir):
        self.gpx_dir = Path(gpx_dir)
        self.gpx_dir.mkdir(parents=True, exist_ok=True)
        # fichiers écrits pendant la dernière sync (même interrompue)
        self.downloaded = []
        # fichiers écrits mais pas encore transmis à on_batch : le watcher doit les
        # ignorer, sinon il les ingérerait de son côté, sans attendre le lot
        self._claimed = set()
        self._claimed_lock = threading.Lock()

    def is_claimed(self, path) -> bool:
        with self._claimed_lock:
            return normalize_path(path) in self._claimed

    def sync(self, client, on_batch=None, ingest_batch_size=25, lookback_days=365, progress=NULL_PROGRESS):
        """Récupère les nouvelles activités et télécharge leur export GPX dans gpx_dir.

        Si on_batch est fourni :
        - ingest_batch_size > 0 : appelé au fur et à mesure, dès que ce nombre de
          nouveaux fichiers a été téléchargé (résultat visible progressivement).
        - ingest_batch_size = 0 (ou None) : appelé UNE SEULE FOIS à la toute fin,
          une fois tout le téléchargement terminé (un seul bulk_ingest global).
        Les fichiers déjà téléchargés sont transmis à on_batch même si la sync
        s'interrompt sur une erreur.

        Retourne la liste complète des chemins de fichiers nouvellement écrits."""
        self.downloaded = new_paths = []
        pending_batch = []

        def flush_pending():
            batch_paths = list(pending_batch)
            pending_batch.clear()
            try:
                if batch_paths and on_batch:
                    on_batch(batch_paths)
            finally:
                with self._claimed_lock:
                    self._claimed.difference_update(normalize_path(p) for p in batch_paths)

        def on_written(path):
            new_paths.append(path)
            pending_batch.append(path)
            progress.advance(phase=DOWNLOAD_PHASE)
            if ingest_batch_size and len(pending_batch) >= ingest_batch_size:
                flush_pending()

        progress.phase(DOWNLOAD_PHASE)
        try:
            self._download_new(client, on_written, lookback_days, progress)
        finally:
            flush_pending()
        return new_paths

    def _download_new(self, client, on_written, lookback_days, progress):
        """Parcourt les activités de la plus récente à la plus ancienne et écrit le GPX
        de chaque activité inconnue, en appelant on_written(chemin) après chaque écriture."""
        from garminconnect import Garmin

        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        start = 0
        batch = 50
        consecutive_known = 0
        written = 0

        while True:
            activities = client.get_activities(start, batch)
            if not activities:
                return

            for act in activities:
                progress.check_cancel()
                act_id = act.get("activityId")
                if act_id is None:
                    continue
                out_path = self.gpx_dir / f"garmin_{act_id}.gpx"

                if out_path.exists():
                    consecutive_known += 1
                    if consecutive_known >= 20:
                        # Garmin trie du plus récent au plus ancien : après une série
                        # d'activités déjà connues, tout le reste l'est forcément aussi.
                        log.info("Rattrapé : %d fichiers déjà à jour, arrêt.", written)
                        return
                    continue
                consecutive_known = 0

                start_str = act.get("startTimeGMT")
                if start_str:
                    try:
                        act_time = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        if act_time < cutoff:
                            log.info("Limite d'historique atteinte (%s), arrêt.", start_str)
                            return
                    except ValueError:
                        pass

                try:
                    data = client.download_activity(act_id, dl_fmt=Garmin.ActivityDownloadFormat.GPX)
                    if data[:2] == b"PK":  # parfois renvoyé comme zip contenant le .gpx
                        with zipfile.ZipFile(io.BytesIO(data)) as z:
                            gpx_names = [n for n in z.namelist() if n.lower().endswith(".gpx")]
                            if not gpx_names:
                                raise RuntimeError("archive sans fichier .gpx")
                            data = z.read(gpx_names[0])

                    stripped = data.lstrip()
                    if not (stripped.startswith(b"<?xml") or stripped.startswith(b"<gpx")):
                        # Garmin renvoie parfois un contenu non-GPX (page d'erreur, réponse vide...) -
                        # on ne l'écrit pas sur disque, pour ne pas polluer le dossier surveillé.
                        # Non marqué comme téléchargé : sera retenté au prochain cycle.
                        log.warning("Activité %s : réponse non-GPX reçue, ignorée (%d octets)", act_id, len(data))
                        continue
                    if b"<trkpt" not in data and b"<rtept" not in data and b"<wpt" not in data:
                        # L'export GPX de Garmin est parfois une "coquille vide" (metadonnees
                        # seules, <trkseg/> sans points) MEME pour une activite exterieure avec
                        # de vraies donnees GPS. Repli sur l'export TCX de la meme activite, qui
                        # contient souvent les points quand le GPX ne les a pas.
                        log.info("Activité %s : GPX sans points, tentative via TCX...", act_id)
                        try:
                            tcx_data = client.download_activity(act_id, dl_fmt=Garmin.ActivityDownloadFormat.TCX)
                            points = _tcx_to_points(tcx_data)
                            if points:
                                data = _points_to_gpx_bytes(points, name=f"garmin_{act_id}")
                                log.info("Activité %s : %d points récupérés via TCX", act_id, len(points))
                            else:
                                log.info("Activité %s : TCX également sans points (activité sans GPS)", act_id)
                        except Exception:
                            log.exception("Echec repli TCX pour l'activité %s", act_id)

                    # écriture dans un .part puis renommage atomique : le watcher ne voit
                    # jamais un GPX à moitié écrit, et le fichier est réservé avant d'apparaître
                    tmp_path = out_path.with_name(out_path.name + ".part")
                    tmp_path.write_bytes(data)
                    with self._claimed_lock:
                        self._claimed.add(normalize_path(out_path))
                    os.replace(tmp_path, out_path)
                    written += 1
                    log.info("Téléchargé (%d au total): %s", written, out_path.name)
                except Exception:
                    log.exception("Echec téléchargement activité %s", act_id)
                    continue
                on_written(out_path)

            start += batch
            if len(activities) < batch:
                return
