"""
Coeur du système :
- stocke les traces issues des GPX ingérés (SQLite -> tableaux numpy en mémoire),
  groupées par segment continu (une trace = une polyligne), chaque segment étant
  rattaché à son fichier d'origine (suppression / modification d'un GPX possibles)
- pour un nouveau GPX, ne recalcule que les tuiles impactées (par zoom)
- rendu = chaque segment est dessiné comme UNE ligne (donc 1 passage = 1 "unité",
  même si le tracé se recoupe lui-même), les segments s'additionnent, puis un
  flou gaussien est appliqué une fois par tuile pour l'effet "heatmap"

Toutes les opérations qui modifient la base, les tableaux en mémoire ou les tuiles
passent par un verrou unique : watcher, sync Garmin et endpoints admin peuvent
tourner en parallèle sans doublons ni identifiants de segment en collision. La
lecture des tuiles n'en a pas besoin (elles sont remplacées de façon atomique), pas
plus que l'aperçu de rendu de l'admin : les points en mémoire et les réglages de
rendu sont des objets immuables, remplacés d'un bloc à chaque modification.

Les opérations longues acceptent un objet `progress` (voir jobs.py) pour remonter
leur avancement et pouvoir être annulées.
"""
import io
import itertools
import logging
import math
import os
import sqlite3
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from pathlib import Path

import mercantile
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from . import gpx_utils
from .gpx_utils import normalize_path
from .jobs import NULL_PROGRESS

log = logging.getLogger("heatmap")

TILE_SIZE = 256
BLUR_TRUNCATE = 4.0  # rayon du noyau gaussien, en sigmas (valeur par défaut de scipy)
MAX_LAT = 85.05
FILE_STATUS = {1: "ok", 0: "failed", 2: "legacy"}
PREVIEW_CACHE_BYTES = 64 * 2**20  # mémoire max des sélections de points gardées pour l'aperçu
LOAD_CHUNK = 100_000  # points lus par bloc au démarrage
COORD_SCALE = 1_000_000  # coordonnées en mémoire : entiers 32 bits en microdegrés (~11 cm)
DEFAULT_SIMPLIFY_M = 1.0  # tolérance de simplification des traces à l'import (mètres)


def _file_signature(path):
    """(mtime, taille) du fichier, ou None s'il n'existe plus."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return st.st_mtime, st.st_size


def _list_gpx(folder):
    return [normalize_path(p) for p in Path(folder).iterdir() if p.is_file() and gpx_utils.is_gpx(p)]


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_config(render: dict, gradient) -> None:
    errors = []
    min_z, max_z = render.get("min_zoom"), render.get("max_zoom")
    if (not isinstance(min_z, int) or not isinstance(max_z, int) or isinstance(min_z, bool)
            or not 0 <= min_z <= max_z <= 24):
        errors.append("render.min_zoom / max_zoom doivent être des entiers avec 0 <= min_zoom <= max_zoom <= 24")
    lw = render.get("line_width_px", 3)
    if not _is_number(lw) or not 1 <= lw <= 64:
        errors.append("render.line_width_px doit être entre 1 et 64")
    sigma = render.get("blur_sigma_px", 4)
    if not _is_number(sigma) or not 0 <= sigma <= 64:
        errors.append("render.blur_sigma_px doit être entre 0 et 64")
    if not _is_number(render.get("color_saturation", 6.0)) or render.get("color_saturation", 6.0) <= 0:
        errors.append("render.color_saturation doit être > 0")
    boost = render.get("contrast_boost", 9.0)
    if not _is_number(boost) or boost < 0:
        errors.append("render.contrast_boost doit être >= 0 (0 = pas de compression logarithmique)")
    if not isinstance(gradient, (list, tuple)) or len(gradient) < 2:
        errors.append("color_gradient doit contenir au moins 2 paliers")
    else:
        for stop in gradient:
            try:
                ok = (len(stop) == 2 and _is_number(stop[0]) and 0 <= stop[0] <= 1 and len(stop[1]) == 4
                      and all(_is_number(c) and 0 <= c <= 255 for c in stop[1]))
            except TypeError:
                ok = False
            if not ok:
                errors.append(f"palier de color_gradient invalide : {stop} (attendu [0-1, [R,G,B,A]] en 0-255)")
                break
    if errors:
        raise ValueError("Configuration invalide : " + " ; ".join(errors))


def _to_e6(degrees):
    return np.round(np.asarray(degrees, dtype=np.float64) * COORD_SCALE).astype(np.int32)


def _indices(n):
    """0..n-1 dans le plus petit type d'index possible (4 octets au lieu de 8)."""
    return np.arange(n, dtype=np.int32 if n < 2**31 else np.int64)


def simplify_track(latlon, tolerance_m):
    """Indices des points à garder (Douglas-Peucker) : un point est retiré s'il est à
    moins de `tolerance_m` du tracé simplifié. Un GPS à 1 point/s en enregistre beaucoup
    plus qu'il n'en faut pour le dessin (un pixel au zoom 16 fait ~1,6 m à Paris).
    La distance est mesurée au SEGMENT et non à la droite : un aller-retour sur le même
    chemin garde son point de demi-tour."""
    n = len(latlon)
    if n < 3 or tolerance_m <= 0:
        return np.arange(n)
    # projection locale en mètres, suffisante à l'échelle d'une trace
    lat0 = math.radians(float(np.mean(latlon[:, 0])))
    x = latlon[:, 1] * (111_320.0 * math.cos(lat0))
    y = latlon[:, 0] * 110_574.0
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        ax, ay = x[i], y[i]
        dx, dy = x[j] - ax, y[j] - ay
        px, py = x[i + 1:j] - ax, y[i + 1:j] - ay
        length2 = dx * dx + dy * dy
        t = np.clip((px * dx + py * dy) / length2, 0, 1) if length2 > 0 else 0.0
        dist2 = (px - t * dx) ** 2 + (py - t * dy) ** 2
        k = int(np.argmax(dist2))
        if dist2[k] > tolerance_m * tolerance_m:
            k += i + 1
            keep[k] = True
            stack.append((i, k))
            stack.append((k, j))
    return np.flatnonzero(keep)


def _make_lut(gradient_stops, n=256):
    stops = sorted(gradient_stops, key=lambda s: s[0])
    fracs = np.array([s[0] for s in stops])
    colors = np.array([s[1] for s in stops], dtype=np.float64)
    lut = np.zeros((n, 4), dtype=np.uint8)
    xs = np.linspace(0, 1, n)
    for c in range(4):
        lut[:, c] = np.clip(np.interp(xs, fracs, colors[:, c]), 0, 255).astype(np.uint8)
    return lut


def _single_pass_peak(line_width, sigma):
    """Intensité au centre d'un passage isolé (trait droit) après le flou. Le flou étale
    le trait et abaisse fortement ce pic (~0,38 pour 2 px et sigma 2) : sans en tenir
    compte, un passage unique resterait terne quel que soit le contraste choisi."""
    if sigma <= 0:
        return 1.0
    radius = int(math.ceil(BLUR_TRUNCATE * sigma)) + 1
    profile = np.zeros(line_width + 2 * radius)
    profile[radius:radius + line_width] = 1
    return float(ndimage.gaussian_filter1d(profile, sigma, truncate=BLUR_TRUNCATE).max())


@dataclass(frozen=True, eq=False)
class RenderParams:
    """Réglages de rendu validés, figés : modifiables à chaud en remplaçant l'objet."""
    min_zoom: int
    max_zoom: int
    line_width_px: int
    blur_sigma_px: float
    vmax: float  # intensité (après flou) du rouge plein : color_saturation passages
    contrast_boost: float
    lut: np.ndarray
    pad: int

    @classmethod
    def from_config(cls, render: dict, gradient) -> "RenderParams":
        _validate_config(render, gradient)
        line_width = int(render.get("line_width_px", 3))
        sigma = float(render.get("blur_sigma_px", 4))
        return cls(
            min_zoom=render["min_zoom"],
            max_zoom=render["max_zoom"],
            line_width_px=line_width,
            blur_sigma_px=sigma,
            vmax=float(render.get("color_saturation", 6.0)) * _single_pass_peak(line_width, sigma),
            contrast_boost=float(render.get("contrast_boost", 9.0)),
            lut=_make_lut(gradient),
            # marge (px) autour de chaque tuile : le trait et le flou débordent jusqu'à cette
            # distance, on dessine donc sur un canevas élargi pour éviter les coutures aux bords
            pad=int(math.ceil(line_width / 2 + BLUR_TRUNCATE * sigma)) + 2,
        )

    def colorize(self, intensity: np.ndarray) -> Image.Image:
        norm = np.clip(intensity / self.vmax, 0, 1)
        # compression logarithmique : booste la visibilité des faibles densités (ex: un seul
        # passage) sans changer color_saturation. Plus contrast_boost est élevé, plus un
        # passage isolé ressort par rapport à un fond quasi-transparent (0 = linéaire).
        k = self.contrast_boost
        if k > 0:
            norm = np.log1p(norm * k) / np.log1p(k)
        idx = (norm * 255).astype(np.uint8)
        return Image.fromarray(self.lut[idx], mode="RGBA")


@dataclass(frozen=True, eq=False)
class Points:
    """Points en mémoire, triés par segment puis par ordre de passage (les runs d'un
    même seg_id sont contigus). Jamais modifié en place. lat / lon sont des entiers
    32 bits en microdegrés et seg un entier 32 bits : 12 octets par point au lieu de 24
    (la base garde les coordonnées complètes)."""
    seg: np.ndarray
    lat: np.ndarray
    lon: np.ndarray

    @classmethod
    def empty(cls):
        return cls(*(np.empty((0,), dtype=np.int32) for _ in range(3)))

    def __len__(self):
        return len(self.lat)


class HeatmapEngine:
    def __init__(self, config: dict):
        self.params = RenderParams.from_config(config["render"], config.get("color_gradient"))
        self.simplify_m = float((config.get("ingest") or {}).get("simplify_tolerance_m", DEFAULT_SIMPLIFY_M))
        self.tiles_dir = Path(config["paths"]["tiles"])
        self.db_path = Path(config["paths"]["db"])
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.tiles_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._read_lock = threading.Lock()
        self._init_db()
        self._load_points()
        self._bounds_cache = (None, None)
        self._tile_count_cache = (0.0, 0)
        self._preview_lock = threading.Lock()
        self._preview_cache = (None, OrderedDict())  # ((points, marge), {(z, x, y): indices})

    def close(self):
        with self._lock, self._read_lock:
            self.conn.close()
            self._read_conn.close()

    def set_render_params(self, render: dict, gradient) -> RenderParams:
        """Change les réglages de rendu à chaud. Les tuiles existantes ne sont pas
        recalculées : lancer rerender_all() pour les mettre à jour."""
        self.params = RenderParams.from_config(render, gradient)
        return self.params

    # raccourcis (tests, compatibilité)
    min_zoom = property(lambda self: self.params.min_zoom)
    max_zoom = property(lambda self: self.params.max_zoom)
    line_width_px = property(lambda self: self.params.line_width_px)
    blur_sigma_px = property(lambda self: self.params.blur_sigma_px)
    pad = property(lambda self: self.params.pad)
    seg = property(lambda self: self.pts.seg)
    lat = property(lambda self: self.pts.lat)
    lon = property(lambda self: self.pts.lon)

    @property
    def point_count(self) -> int:
        return len(self.pts)

    def _colorize(self, intensity):
        return self.params.colorize(intensity)

    # ---------- stockage ----------

    def _init_db(self):
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        c = self.conn
        c.execute("PRAGMA journal_mode=WAL")  # lectures de l'admin possibles pendant un import
        c.execute("CREATE TABLE IF NOT EXISTS points (seg_id INTEGER, seq INTEGER, lat REAL, lon REAL)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_points_seg ON points(seg_id, seq)")
        c.execute("CREATE TABLE IF NOT EXISTS segments (seg_id INTEGER PRIMARY KEY, path TEXT NOT NULL)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_segments_path ON segments(path)")
        # ok : 1 = importé, 0 = illisible, 2 = importé par une version précédente
        c.execute("CREATE TABLE IF NOT EXISTS ingested_files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, "
                  "ok INTEGER NOT NULL DEFAULT 1, points INTEGER, imported_at REAL, error TEXT)")

        # migration des bases créées par les versions précédentes
        cols = {row[1] for row in c.execute("PRAGMA table_info(ingested_files)")}
        if "mtime" not in cols:
            # fichiers identifiés par leur seul chemin, sans signature ni lien vers leurs segments
            c.execute("ALTER TABLE ingested_files ADD COLUMN mtime REAL")
            c.execute("ALTER TABLE ingested_files ADD COLUMN size INTEGER")
            c.execute("ALTER TABLE ingested_files ADD COLUMN ok INTEGER NOT NULL DEFAULT 2")
            legacy = [r[0] for r in c.execute("SELECT path FROM ingested_files")]
            for path in legacy:
                sig = _file_signature(path)
                if sig is not None:
                    c.execute("UPDATE ingested_files SET mtime=?, size=? WHERE path=?", (*sig, path))
            if legacy:
                log.info("Base migrée : %d fichier(s) importé(s) par une version précédente. "
                         "Une réindexation permet de gérer aussi leur suppression/modification.",
                         len(legacy))
        for col, ddl in (("points", "INTEGER"), ("imported_at", "REAL"), ("error", "TEXT")):
            if col not in cols:
                c.execute(f"ALTER TABLE ingested_files ADD COLUMN {col} {ddl}")
        c.commit()
        self._read_conn = sqlite3.connect(str(self.db_path), check_same_thread=False)

    def _load_points(self):
        # lecture par blocs dans des tableaux alloués d'avance : un fetchall() créerait un
        # tuple Python par point (~180 octets chacun, 7 fois la taille des tableaux finaux)
        n = self.conn.execute("SELECT COUNT(*) FROM points").fetchone()[0]
        seg, lat, lon = (np.empty(n, dtype=np.int32) for _ in range(3))
        cur = self.conn.execute("SELECT seg_id, lat, lon FROM points ORDER BY seg_id, seq")
        i = 0
        while i < n:
            rows = cur.fetchmany(LOAD_CHUNK)
            if not rows:
                break
            arr = np.array(rows, dtype=np.float64)
            seg[i:i + len(arr)] = arr[:, 0]
            lat[i:i + len(arr)] = _to_e6(arr[:, 1])
            lon[i:i + len(arr)] = _to_e6(arr[:, 2])
            i += len(arr)
        self.pts = Points(seg[:i], lat[:i], lon[:i])
        max_seg = self.conn.execute(
            "SELECT MAX(m) FROM (SELECT MAX(seg_id) AS m FROM points UNION ALL SELECT MAX(seg_id) FROM segments)"
        ).fetchone()[0]
        self._next_seg_id = int(max_seg) + 1 if max_seg is not None else 0

    def _file_row(self, path):
        return self.conn.execute("SELECT mtime, size, ok FROM ingested_files WHERE path=?", (path,)).fetchone()

    def _is_up_to_date(self, path, sig) -> bool:
        row = self._file_row(path)
        if row is None:
            return False
        if row[0] is None:
            return True  # importé par une ancienne version, fichier introuvable lors de la migration
        return (row[0], row[1]) == tuple(sig)

    def needs_ingest(self, path) -> bool:
        """True si le fichier existe et n'a jamais été importé, ou a changé depuis."""
        path = normalize_path(path)
        sig = _file_signature(path)
        if sig is None:
            return False
        with self._lock:
            return not self._is_up_to_date(path, sig)

    # ---------- ingestion / suppression ----------

    def ingest_gpx(self, path: str) -> int:
        return self.bulk_ingest([path])

    def bulk_ingest(self, paths, render: bool = True, force: bool = False, progress=NULL_PROGRESS) -> int:
        """Importe les fichiers nouveaux ou modifiés parmi `paths` (tous avec force=True) :
        tout est inséré en base avant de calculer quoi que ce soit, puis chaque tuile impactée
        n'est rendue qu'UNE seule fois pour l'ensemble du lot. Un fichier modifié depuis son
        import voit ses anciens points remplacés. Retourne le nombre de fichiers traités."""
        with self._lock:
            todo = []
            for path in dict.fromkeys(normalize_path(p) for p in paths):
                sig = _file_signature(path)
                if sig is not None and (force or not self._is_up_to_date(path, sig)):
                    todo.append((path, sig))
            if not todo:
                return 0

            progress.phase("Lecture des GPX", len(todo))
            # (chemin, signature, segments ou None, erreur) ; chaque segment est converti tout de
            # suite en tableau (n, 2) : 16 octets par point au lieu de ~150 pour une liste de
            # tuples, ce qui compte pour un gros lot (premier import Garmin, réindexation)
            parsed = []
            for path, sig in todo:
                try:
                    segments, waypoints = gpx_utils.parse_gpx_segments(path)
                    arrays = [np.array(s, dtype=np.float64) for s in segments + [[wp] for wp in waypoints]]
                    arrays = [a[simplify_track(a, self.simplify_m)] for a in arrays]
                    del segments, waypoints
                    parsed.append((path, sig, arrays, None))
                except Exception as e:
                    log.exception("GPX illisible/invalide, ignoré : %s", path)
                    parsed.append((path, sig, None, str(e) or e.__class__.__name__))
                progress.advance()
            # rien n'a été modifié jusqu'ici : une annulation pendant la lecture laisse tout intact

            changed = [p for p, _ in todo if self._file_row(p) is not None]
            old = self._remove_from_store(changed)

            seg_arrays, seg_rows, file_rows = [], [], []  # seg_arrays : (seg_id, tableau (n, 2))
            now = time.time()
            for path, sig, segments, error in parsed:
                if segments is None:
                    # mémorisé avec sa signature : ne sera retenté que si le fichier change
                    # (ex : copie encore en cours au moment de la lecture)
                    file_rows.append((path, *sig, 0, None, now, error[:500]))
                    continue
                for pts in segments:
                    sid = self._next_seg_id
                    self._next_seg_id += 1
                    seg_rows.append((sid, path))
                    seg_arrays.append((sid, pts))
                file_rows.append((path, *sig, 1, sum(len(s) for s in segments), now, None))
            del parsed

            failed = sum(1 for r in file_rows if r[3] == 0)
            if failed:
                log.warning("%d fichier(s) GPX invalide(s) ignoré(s) lors de cet import", failed)

            def point_rows():
                # générées segment par segment : jamais tout le lot en tuples Python à la fois
                for sid, pts in seg_arrays:
                    yield from zip(itertools.repeat(sid), range(len(pts)), pts[:, 0].tolist(), pts[:, 1].tolist())

            self.conn.executemany("INSERT INTO points(seg_id, seq, lat, lon) VALUES (?,?,?,?)", point_rows())
            self.conn.executemany("INSERT INTO segments(seg_id, path) VALUES (?,?)", seg_rows)
            self.conn.executemany(
                "INSERT OR REPLACE INTO ingested_files(path, mtime, size, ok, points, imported_at, error) "
                "VALUES (?,?,?,?,?,?,?)", file_rows
            )
            self.conn.commit()

            if seg_arrays:
                new = Points(np.concatenate([np.full(len(pts), sid, dtype=np.int32) for sid, pts in seg_arrays]),
                             _to_e6(np.concatenate([pts[:, 0] for _, pts in seg_arrays])),
                             _to_e6(np.concatenate([pts[:, 1] for _, pts in seg_arrays])))
                del seg_arrays
                self.pts = Points(np.concatenate([self.pts.seg, new.seg]),
                                  np.concatenate([self.pts.lat, new.lat]),
                                  np.concatenate([self.pts.lon, new.lon]))
            else:
                new = Points.empty()

            if render:
                # une seule passe de rendu pour les anciens points retirés ET les nouveaux
                self._rerender_affected(
                    np.concatenate([old.lat, new.lat]),
                    np.concatenate([old.lon, new.lon]),
                    np.concatenate([old.seg, new.seg]),
                    progress,
                )
            return len(todo)

    def remove_files(self, paths, progress=NULL_PROGRESS) -> int:
        """Retire de la heatmap les points des fichiers donnés (fichiers supprimés)."""
        with self._lock:
            paths = [p for p in dict.fromkeys(normalize_path(p) for p in paths) if self._file_row(p) is not None]
            if not paths:
                return 0
            removed = self._remove_from_store(paths)
            self.conn.commit()
            self._rerender_affected(removed.lat, removed.lon, removed.seg, progress)
            return len(paths)

    def _remove_from_store(self, paths) -> Points:
        """Supprime les fichiers de la base (sans commit) et leurs points de la mémoire.
        Retourne les points retirés, pour recalculer leurs tuiles."""
        seg_ids = []
        for path in paths:
            ids = [r[0] for r in self.conn.execute("SELECT seg_id FROM segments WHERE path=?", (path,))]
            row = self._file_row(path)
            if row is not None and row[2] == 2:
                log.warning("%s : importé par une version précédente, ses anciens points restent "
                            "affichés jusqu'à une réindexation", path)
            seg_ids.extend(ids)
        self.conn.executemany("DELETE FROM points WHERE seg_id=?", [(s,) for s in seg_ids])
        self.conn.executemany("DELETE FROM segments WHERE path=?", [(p,) for p in paths])
        self.conn.executemany("DELETE FROM ingested_files WHERE path=?", [(p,) for p in paths])

        if not seg_ids:
            return Points.empty()
        pts = self.pts
        mask = np.isin(pts.seg, np.array(seg_ids, dtype=pts.seg.dtype))
        keep = ~mask
        self.pts = Points(pts.seg[keep], pts.lat[keep], pts.lon[keep])
        return Points(pts.seg[mask], pts.lat[mask], pts.lon[mask])

    def sync_folder(self, folder, progress=NULL_PROGRESS) -> dict:
        """Aligne la base sur le contenu du dossier : importe en un seul lot les .gpx
        nouveaux ou modifiés, et retire ceux qui ont disparu (ex : supprimés pendant que
        le service était arrêté)."""
        folder = normalize_path(folder)
        if not os.path.isdir(folder):
            # surtout ne pas le traiter comme vide : un partage réseau non monté
            # effacerait toute la heatmap
            log.warning("Dossier %s introuvable, synchronisation ignorée", folder)
            return {"imported": 0, "removed": 0}
        progress.phase("Analyse du dossier")
        present = _list_gpx(folder)
        with self._lock:
            known = [r[0] for r in self.conn.execute("SELECT path FROM ingested_files")]
            gone = [p for p in known if os.path.dirname(p) == folder and not os.path.exists(p)]
            removed = self.remove_files(gone, progress) if gone else 0
            imported = self.bulk_ingest(present, progress=progress)
        return {"imported": imported, "removed": removed}

    def reindex(self, folder, progress=NULL_PROGRESS) -> dict:
        """Vide entièrement la base et réimporte tout le dossier, puis recalcule toutes les
        tuiles. Utile pour une base créée par une version précédente (doublons possibles,
        suppression de fichiers non gérée)."""
        with self._lock:
            progress.phase("Vidage de la base")
            for table in ("points", "segments", "ingested_files"):
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.commit()
            self.conn.execute("VACUUM")
            self._load_points()
            imported = self.bulk_ingest(_list_gpx(folder), render=False, progress=progress)
            self.rerender_all(progress)
        return {"imported": imported}

    # ---------- rendu ----------

    def rerender_all(self, progress=NULL_PROGRESS):
        """Recalcule toutes les tuiles (après un changement de style/config). Les tuiles
        existantes restent servies pendant le calcul (remplacement atomique) ; celles qui
        n'ont plus lieu d'être (zone vide, zoom hors plage) sont supprimées à la fin."""
        with self._lock:
            pts = self.pts
            written = self._rerender_affected(pts.lat, pts.lon, pts.seg, progress)
            progress.phase("Nettoyage des tuiles obsolètes")
            for f in list(self.tiles_dir.rglob("*")):
                if not f.is_file():
                    continue
                try:
                    key = (int(f.parent.parent.name), int(f.parent.name), int(f.name.split(".")[0]))
                except ValueError:
                    key = None
                if f.suffix != ".png" or key not in written:
                    f.unlink(missing_ok=True)
            for d in sorted((d for d in self.tiles_dir.rglob("*") if d.is_dir()), reverse=True):
                if not any(d.iterdir()):
                    d.rmdir()
            self._tile_count_cache = (0.0, 0)

    @staticmethod
    def _buffer_deg(params, z):
        # marge en degrés de longitude ; appliquée aussi à la latitude, ce qui la majore
        # (en Mercator, un pixel couvre moins de degrés de latitude que de longitude)
        return 360.0 / (2 ** z * TILE_SIZE) * params.pad

    @classmethod
    def _canvas_bounds(cls, params, z, x, y):
        b = mercantile.bounds(x, y, z)
        buf = cls._buffer_deg(params, z)
        return b.west - buf, b.south - buf, b.east + buf, b.north + buf

    def _rerender_affected(self, lat_new, lon_new, seg_new, progress=NULL_PROGRESS):
        """Calcule l'ensemble des tuiles à (re)générer comme l'UNION des empreintes de
        chaque trace individuelle (pas la bbox globale du lot). Important pour un import
        groupé de traces géographiquement dispersées (ex: plusieurs pays) : une bbox
        globale engloberait d'immenses zones vides et ferait exploser le nombre de
        tuiles à haut zoom. Retourne l'ensemble des tuiles écrites (non vides)."""
        if len(seg_new) == 0:
            return set()
        params = self.params

        # les tableaux sont dans l'ordre d'insertion (par seg_id croissant) -> les runs
        # d'un même seg_id sont contigus, pas besoin de trier
        starts = np.concatenate([[0], np.flatnonzero(np.diff(seg_new)) + 1])
        bboxes = list(zip(*(
            (reduce.reduceat(coord, starts) / COORD_SCALE).tolist()
            for reduce, coord in ((np.minimum, lon_new), (np.minimum, lat_new), (np.maximum, lon_new), (np.maximum, lat_new))
        )))

        progress.phase("Recherche des tuiles", params.max_zoom - params.min_zoom + 1)
        tiles = set()
        for z in range(params.min_zoom, params.max_zoom + 1):
            buf = self._buffer_deg(params, z)
            for west, south, east, north in bboxes:
                w, e = max(west - buf, -180), min(east + buf, 180)
                s, n = max(south - buf, -MAX_LAT), min(north + buf, MAX_LAT)
                for tile in mercantile.tiles(w, s, e, n, [z]):
                    tiles.add((z, tile.x, tile.y))
            progress.advance()
        return self._render_tiles(tiles, params, progress)

    def _render_tiles(self, tiles, params, progress=NULL_PROGRESS):
        """Rend les tuiles zoom par zoom, du plus petit au plus grand. Les points retenus
        pour une tuile sont filtrés parmi ceux de sa tuile mère au zoom précédent (la zone
        d'une tuile fille est incluse dans celle de sa mère) : le coût d'une tuile dépend
        des points de sa zone, et non de tous ceux de la région, ce qui évite qu'une zone
        très fréquentée ralentisse fortement le calcul aux grands zooms."""
        progress.phase("Calcul des tuiles", len(tiles))
        pts = self.pts
        all_idx = _indices(len(pts))
        selections = {}  # (z, x, y) -> indices des points nécessaires à la tuile

        def selection(z, x, y):
            key = (z, x, y)
            if key not in selections:
                source = all_idx if z <= params.min_zoom else selection(z - 1, x >> 1, y >> 1)
                selections[key] = self._select(pts, source, *self._canvas_bounds(params, z, x, y))
            return selections[key]

        written = set()
        current_zoom = None
        for z, x, y in sorted(tiles):
            if z != current_zoom:
                # nouveau zoom : seules les sélections du zoom précédent servent encore
                for key in [k for k in selections if k[0] < z - 1]:
                    del selections[key]
                current_zoom = z
            out = self.tile_path(z, x, y)
            img = self._draw_tile(pts, params, z, x, y, selection(z, x, y))
            if img is None:
                out.unlink(missing_ok=True)
            else:
                self._write_tile(out, img)
                written.add((z, x, y))
            progress.advance()
        return written

    @staticmethod
    def _select(pts, idx, w, s, e, n):
        """Sous-ensemble (trié) des indices `idx` nécessaires pour dessiner la zone donnée :
        les points qui s'y trouvent, plus les deux extrémités de tout tronçon dont la bbox
        la recoupe. Sans ce dernier cas, un tronçon qui traverse la zone sans y avoir de
        point (points espacés, route planifiée, fort zoom) n'y serait pas dessiné."""
        if len(idx) == 0:
            return idx
        # bornes en microdegrés, arrondies vers l'extérieur
        w, s = math.floor(w * COORD_SCALE), math.floor(s * COORD_SCALE)
        e, n = math.ceil(e * COORD_SCALE), math.ceil(n * COORD_SCALE)
        lon, lat = pts.lon[idx], pts.lat[idx]
        keep = (lon >= w) & (lon <= e) & (lat >= s) & (lat <= n)
        if len(idx) >= 2:
            linked = (idx[1:] == idx[:-1] + 1) & (pts.seg[idx[1:]] == pts.seg[idx[:-1]])
            crossing = (
                linked
                & (np.minimum(lon[:-1], lon[1:]) <= e) & (np.maximum(lon[:-1], lon[1:]) >= w)
                & (np.minimum(lat[:-1], lat[1:]) <= n) & (np.maximum(lat[:-1], lat[1:]) >= s)
            )
            keep[:-1] |= crossing
            keep[1:] |= crossing
        if keep.all():
            return idx  # pas de copie : les tuiles des petits zooms contiennent souvent tout
        return idx[keep]

    def _draw_tile(self, pts, params, z, x, y, idx):
        """Dessine une tuile à partir des points déjà sélectionnés pour elle (voir _select)."""
        if len(idx) == 0:
            return None

        pad = params.pad
        canvas_size = TILE_SIZE + 2 * pad
        canvas = np.zeros((canvas_size, canvas_size), dtype=np.float32)
        px, py = self._lonlat_to_pixel(pts.lon[idx] / COORD_SCALE, pts.lat[idx] / COORD_SCALE, z)
        lx = px - (x * TILE_SIZE - pad)
        ly = py - (y * TILE_SIZE - pad)

        seg_starts = np.flatnonzero(np.diff(pts.seg[idx])) + 1
        for g in np.split(np.arange(len(idx)), seg_starts):
            # portions continues du segment : un trou dans les indices veut dire que la
            # trace est sortie de la zone de cette tuile puis y est revenue plus tard
            # (boucle, aller-retour...) : il ne faut PAS relier ces deux portions, sous
            # peine de créer une fausse ligne droite
            runs = np.split(g, np.flatnonzero(np.diff(idx[g]) != 1) + 1)
            self._draw_segment(canvas, [(lx[r], ly[r]) for r in runs], params.line_width_px)

        if params.blur_sigma_px > 0:
            canvas = ndimage.gaussian_filter(canvas, sigma=params.blur_sigma_px, truncate=BLUR_TRUNCATE)
        return params.colorize(canvas[pad:pad + TILE_SIZE, pad:pad + TILE_SIZE])

    @staticmethod
    def _draw_segment(canvas, runs, line_width):
        """Ajoute un segment (liste de portions (xs, ys)) au canevas. Toutes ses portions
        sont dessinées sur la même image avant d'être ajoutées : une trace ne contribue
        jamais plus d'1 "unité" à un pixel, même si elle se recoupe (aller-retour) ->
        un passage = un passage. Le dessin se fait sur la seule zone englobante."""
        size = canvas.shape[0]
        r = line_width / 2 + 1
        x0 = max(int(math.floor(min(xs.min() for xs, _ in runs) - r)), 0)
        y0 = max(int(math.floor(min(ys.min() for _, ys in runs) - r)), 0)
        x1 = min(int(math.ceil(max(xs.max() for xs, _ in runs) + r)) + 1, size)
        y1 = min(int(math.ceil(max(ys.max() for _, ys in runs) + r)) + 1, size)
        if x0 >= x1 or y0 >= y1:
            return

        temp = Image.new("L", (x1 - x0, y1 - y0), 0)
        draw = ImageDraw.Draw(temp)
        for xs, ys in runs:
            if len(xs) == 1:
                cx, cy, rr = xs[0] - x0, ys[0] - y0, line_width / 2
                draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=255)
            else:
                coords = list(zip((xs - x0).tolist(), (ys - y0).tolist()))
                draw.line(coords, fill=255, width=line_width, joint="curve")
        canvas[y0:y1, x0:x1] += np.asarray(temp) > 0

    def render_preview(self, z, x, y, params: RenderParams):
        """PNG d'une tuile rendue avec des réglages candidats, sans rien écrire (aperçu de
        l'admin). Ne prend pas le verrou : utilisable pendant un import."""
        pts = self.pts
        if not params.min_zoom <= z <= params.max_zoom or not len(pts):
            return None
        img = self._draw_tile(pts, params, z, x, y, self._preview_selection(pts, params, z, x, y))
        if img is None:
            return None
        buf = io.BytesIO()
        img.save(buf, format="PNG", compress_level=1)  # servi en local : vitesse avant taille
        return buf.getvalue()

    def _preview_selection(self, pts, params, z, x, y):
        """Points nécessaires à une tuile d'aperçu, filtrés depuis ceux de sa tuile mère
        comme dans _render_tiles. Les sélections sont gardées entre les requêtes (elles
        ne dépendent que des points et de la marge) : un déplacement de la carte ou un
        nouveau réglage n'ont plus à parcourir tous les points pour chaque tuile. Le verrou
        évite que les tuiles affichées en même temps recalculent chacune leurs ancêtres."""
        with self._preview_lock:
            key, cache = self._preview_cache
            if key != (pts, params.pad):
                cache = OrderedDict()
                self._preview_cache = ((pts, params.pad), cache)
            missing = []
            tile = (z, x, y)
            while tile not in cache and tile[0] > params.min_zoom:
                missing.append(tile)
                tile = (tile[0] - 1, tile[1] >> 1, tile[2] >> 1)
            if tile in cache:
                idx = cache[tile]
                cache.move_to_end(tile)
            else:
                missing.append(tile)
                idx = _indices(len(pts))
            for tile in reversed(missing):
                idx = cache[tile] = self._select(pts, idx, *self._canvas_bounds(params, *tile))
            # limite en octets, pas en nombre : aux petits zooms une sélection peut contenir
            # presque tous les points. Un même tableau partagé par plusieurs tuiles (voir
            # _select) n'est compté qu'une fois ; la sélection demandée est toujours gardée.
            while len(cache) > 1 and sum({id(a): a.nbytes for a in cache.values()}.values()) > PREVIEW_CACHE_BYTES:
                cache.popitem(last=False)
            return idx

    def tile_path(self, z, x, y) -> Path:
        return self.tiles_dir / str(z) / str(x) / f"{y}.png"

    def get_tile_path(self, z, x, y):
        p = self.tile_path(z, x, y)
        return p if p.exists() else None

    @staticmethod
    def _write_tile(path: Path, img: Image.Image):
        # écriture dans un fichier temporaire puis renommage atomique : une requête
        # concurrente lit l'ancienne ou la nouvelle tuile, jamais une tuile à moitié écrite
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        # compress_level=6 plutôt qu'optimize=True : ~3 fois plus rapide (optimize essaie
        # plusieurs réglages par tuile) pour des fichiers à peine plus gros (~3 %)
        img.save(tmp, format="PNG", compress_level=6)
        os.replace(tmp, path)

    @staticmethod
    def _lonlat_to_pixel(lon, lat, zoom):
        lat_rad = np.radians(np.clip(lat, -MAX_LAT, MAX_LAT))
        n = 2 ** zoom
        x = (lon + 180.0) / 360.0 * n * TILE_SIZE
        y = (1.0 - np.log(np.tan(lat_rad) + 1.0 / np.cos(lat_rad)) / math.pi) / 2.0 * n * TILE_SIZE
        return x, y

    # ---------- consultation (admin) ----------

    def _read(self, sql, args=()):
        with self._read_lock:
            return self._read_conn.execute(sql, args).fetchall()

    def stats(self) -> dict:
        by_status = dict(self._read("SELECT ok, COUNT(*) FROM ingested_files GROUP BY ok"))
        return {
            "files": sum(by_status.values()),
            "files_ok": by_status.get(1, 0),
            "files_failed": by_status.get(0, 0),
            "files_legacy": by_status.get(2, 0),
            "segments": self._read("SELECT COUNT(*) FROM segments")[0][0],
            "points": self.point_count,
            "tiles": self.tile_count(),
            "data_bounds": self.data_bounds(),
        }

    def tile_count(self, max_age=30.0) -> int:
        at, count = self._tile_count_cache
        if time.monotonic() - at > max_age:
            count = sum(1 for _ in self.tiles_dir.rglob("*.png"))
            self._tile_count_cache = (time.monotonic(), count)
        return count

    def data_bounds(self):
        """[ouest, sud, est, nord] de l'essentiel des points (centiles 0,5-99,5 % pour
        ignorer les points GPS aberrants), ou None sans données."""
        pts = self.pts
        key, bounds = self._bounds_cache
        if key is pts:
            return bounds
        bounds = None
        if len(pts):
            lo_lon, hi_lon = np.percentile(pts.lon, [0.5, 99.5]) / COORD_SCALE
            lo_lat, hi_lat = np.percentile(pts.lat, [0.5, 99.5]) / COORD_SCALE
            bounds = [float(lo_lon), float(lo_lat), float(hi_lon), float(hi_lat)]
        self._bounds_cache = (pts, bounds)
        return bounds

    def list_files(self, query="", status="all", offset=0, limit=50) -> dict:
        where, args = [], []
        if query:
            where.append("path LIKE ?")
            args.append(f"%{query}%")
        codes = {v: k for k, v in FILE_STATUS.items()}
        if status in codes:
            where.append("ok = ?")
            args.append(codes[status])
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = self._read(f"SELECT COUNT(*) FROM ingested_files {clause}", args)[0][0]
        rows = self._read(
            f"SELECT path, ok, points, size, mtime, imported_at, error FROM ingested_files {clause} "
            "ORDER BY imported_at IS NULL, imported_at DESC, path LIMIT ? OFFSET ?",
            args + [limit, offset],
        )
        return {
            "total": total,
            "items": [
                {"path": p, "name": os.path.basename(p), "status": FILE_STATUS.get(ok, "ok"), "points": pts,
                 "size": size, "mtime": mtime, "imported_at": at, "error": err}
                for p, ok, pts, size, mtime, at, err in rows
            ],
        }

    def failed_files(self):
        return [r[0] for r in self._read("SELECT path FROM ingested_files WHERE ok = 0")]
