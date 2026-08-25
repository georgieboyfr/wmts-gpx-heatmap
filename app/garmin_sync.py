"""
Synchronisation Garmin Connect -> dossier GPX surveillé.

Utilise la librairie non-officielle "garminconnect" (cyberjunky), qui reproduit
le flux d'authentification de l'appli mobile Garmin Connect. Ce n'est PAS une
API publique sanctionnée par Garmin : ça peut casser si Garmin change son flux
d'auth, et l'usage automatisé est à tes risques vis-à-vis de leurs CGU (usage
personnel, comparable à d'autres outils d'auto-hébergement type GarminDB -
largement utilisé par la communauté, mais bon à savoir).

Authentification : la 1ère connexion (avec code MFA si activé) doit être faite
une fois "à la main" via `garmin_login.py` (voir README) pour générer le jeton
mis en cache sur disque. Ensuite, ce module recharge ce jeton et le renouvelle
automatiquement, sans interaction.
"""
import io
import logging
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
        f"<trk><name>{name}</name><trkseg>",
    ]
    for lat, lon in points:
        lines.append(f'<trkpt lat="{lat:.7f}" lon="{lon:.7f}"></trkpt>')
    lines.append("</trkseg></trk></gpx>")
    return "\n".join(lines).encode("utf-8")


class GarminSync:
    def __init__(self, email, password, gpx_dir, token_store, lookback_days=365):
        self.email = email
        self.password = password
        self.gpx_dir = Path(gpx_dir)
        self.gpx_dir.mkdir(parents=True, exist_ok=True)
        self.token_store = token_store
        self.lookback_days = lookback_days
        self._client = None

    def _prompt_mfa(self):
        # Le sync tourne en tâche de fond, sans terminal interactif : si Garmin
        # redemande un MFA ici, c'est que le jeton en cache a expiré/été révoqué.
        # -> il faut relancer garmin_login.py à la main (voir README).
        raise RuntimeError(
            "Garmin demande un code MFA mais aucune saisie interactive n'est possible "
            "en tâche de fond. Relance `python -m app.garmin_login` à la main pour "
            "régénérer le jeton mis en cache."
        )

    def _get_client(self):
        from garminconnect import Garmin
        if self._client is None:
            self._client = Garmin(self.email, self.password, prompt_mfa=self._prompt_mfa)
            self._client.login(self.token_store)
        return self._client

    def sync(self, on_batch=None, ingest_batch_size=25):
        """Récupère les nouvelles activités et télécharge leur export GPX dans gpx_dir.

        Si on_batch est fourni :
        - ingest_batch_size > 0 : appelé au fur et à mesure, dès que ce nombre de
          nouveaux fichiers a été téléchargé (résultat visible progressivement).
        - ingest_batch_size = 0 (ou None) : appelé UNE SEULE FOIS à la toute fin,
          une fois tout le téléchargement terminé (un seul bulk_ingest global).

        Retourne la liste complète des chemins de fichiers nouvellement écrits."""
        from garminconnect import Garmin

        client = self._get_client()
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)

        new_paths = []
        pending_batch = []
        start = 0
        batch = 50
        consecutive_known = 0

        def flush_pending():
            nonlocal pending_batch
            if pending_batch and on_batch:
                on_batch(list(pending_batch))
            pending_batch = []

        while True:
            activities = client.get_activities(start, batch)
            if not activities:
                break

            for act in activities:
                act_id = act.get("activityId")
                if act_id is None:
                    continue
                out_path = self.gpx_dir / f"garmin_{act_id}.gpx"

                if out_path.exists():
                    consecutive_known += 1
                    if consecutive_known >= 20:
                        # Garmin trie du plus récent au plus ancien : après une série
                        # d'activités déjà connues, tout le reste l'est forcément aussi.
                        log.info("Rattrapé : %d fichiers déjà à jour, arrêt.", len(new_paths))
                        flush_pending()
                        return new_paths
                    continue
                consecutive_known = 0

                start_str = act.get("startTimeGMT")
                if start_str:
                    try:
                        act_time = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        if act_time < cutoff:
                            log.info("Limite d'historique atteinte (%s), arrêt.", start_str)
                            flush_pending()
                            return new_paths
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

                    out_path.write_bytes(data)
                    new_paths.append(out_path)
                    pending_batch.append(out_path)
                    log.info("Téléchargé (%d au total): %s", len(new_paths), out_path.name)
                    if ingest_batch_size and len(pending_batch) >= ingest_batch_size:
                        flush_pending()
                except Exception:
                    log.exception("Echec téléchargement activité %s", act_id)

            start += batch
            if len(activities) < batch:
                break

        flush_pending()
        return new_paths
