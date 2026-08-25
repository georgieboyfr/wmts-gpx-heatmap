"""
Coeur du système :
- stocke les traces issues des GPX ingérés (SQLite -> tableaux numpy en mémoire),
  groupées par segment continu (une trace = une polyligne)
- pour un nouveau GPX, ne recalcule que les tuiles impactées (par zoom)
- rendu = chaque segment est dessiné comme UNE ligne (donc 1 passage = 1 "unité",
  même si le tracé se recoupe lui-même), les segments s'additionnent, puis un
  flou gaussien est appliqué une fois par tuile pour l'effet "heatmap"
"""
import logging
import math
import threading
from pathlib import Path

import mercantile
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from . import gpx_utils
import sqlite3

log = logging.getLogger("heatmap")

PAD = 32  # marge (px) autour de chaque tuile pour éviter les coutures aux bords (ligne + flou)

_lock = threading.Lock()


class HeatmapEngine:
    def __init__(self, config: dict):
        self.cfg = config
        self.tiles_dir = Path(config["paths"]["tiles"])
        self.db_path = Path(config["paths"]["db"])
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.tiles_dir.mkdir(parents=True, exist_ok=True)

        self.min_zoom = config["render"]["min_zoom"]
        self.max_zoom = config["render"]["max_zoom"]
        self.line_width_px = config["render"].get("line_width_px", 3)
        self.blur_sigma_px = config["render"].get("blur_sigma_px", 4)
        self.vmax = config["render"].get("color_saturation", 6.0)
        self.contrast_boost = config["render"].get("contrast_boost", 9.0)

        self._init_db()
        self._load_points()
        self._lut = self._make_lut(config["color_gradient"])

    # ---------- stockage ----------

    def _init_db(self):
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS points (seg_id INTEGER, seq INTEGER, lat REAL, lon REAL)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_points_seg ON points(seg_id, seq)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS ingested_files (path TEXT PRIMARY KEY)"
        )
        self.conn.commit()

    def _load_points(self):
        cur = self.conn.execute("SELECT seg_id, seq, lat, lon FROM points ORDER BY seg_id, seq")
        rows = cur.fetchall()
        if rows:
            arr = np.array(rows, dtype=np.float64)
            self.seg = arr[:, 0].astype(np.int64)
            self.seq = arr[:, 1].astype(np.int64)
            self.lat = arr[:, 2]
            self.lon = arr[:, 3]
        else:
            self.seg = np.empty((0,), dtype=np.int64)
            self.seq = np.empty((0,), dtype=np.int64)
            self.lat = np.empty((0,), dtype=np.float64)
            self.lon = np.empty((0,), dtype=np.float64)
        self._next_seg_id = int(self.seg.max()) + 1 if len(self.seg) else 0

    def already_ingested(self, path: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM ingested_files WHERE path=?", (path,))
        return cur.fetchone() is not None

    # ---------- ingestion ----------

    def ingest_gpx(self, path: str):
        if self.already_ingested(path):
            return

        try:
            segments, waypoints = gpx_utils.parse_gpx_segments(path)
        except Exception:
            log.exception("GPX illisible/invalide, ignoré : %s", path)
            self.conn.execute("INSERT OR IGNORE INTO ingested_files(path) VALUES (?)", (path,))
            self.conn.commit()
            return

        rows = []  # (seg_id, seq, lat, lon)
        for seg_pts in segments:
            sid = self._next_seg_id
            self._next_seg_id += 1
            for i, (lat, lon) in enumerate(seg_pts):
                rows.append((sid, i, lat, lon))
        for lat, lon in waypoints:
            sid = self._next_seg_id
            self._next_seg_id += 1
            rows.append((sid, 0, lat, lon))

        if not rows:
            self.conn.execute("INSERT OR IGNORE INTO ingested_files(path) VALUES (?)", (path,))
            self.conn.commit()
            return

        with _lock:
            self.conn.executemany(
                "INSERT INTO points(seg_id, seq, lat, lon) VALUES (?,?,?,?)", rows
            )
            self.conn.execute("INSERT OR IGNORE INTO ingested_files(path) VALUES (?)", (path,))
            self.conn.commit()

            arr = np.array(rows, dtype=np.float64)
            new_seg = arr[:, 0].astype(np.int64)
            new_lat = arr[:, 2]
            new_lon = arr[:, 3]

            self.seg = np.concatenate([self.seg, new_seg])
            self.seq = np.concatenate([self.seq, arr[:, 1].astype(np.int64)])
            self.lat = np.concatenate([self.lat, new_lat])
            self.lon = np.concatenate([self.lon, new_lon])

            self._rerender_affected(new_lat, new_lon, new_seg)

    def rerender_all(self):
        """Recalcule toutes les tuiles à partir de zéro (après un changement de style/config).
        Vide d'abord entièrement le dossier des tuiles (au cas où la plage de zoom ou la
        zone couverte aurait changé depuis le dernier rendu), avant de tout regénérer."""
        import shutil
        with _lock:
            shutil.rmtree(self.tiles_dir, ignore_errors=True)
            self.tiles_dir.mkdir(parents=True, exist_ok=True)
            if len(self.lat) == 0:
                return
            self._rerender_affected(self.lat, self.lon, self.seg)

    def bulk_ingest(self, paths):
        """Import groupé : insère TOUS les fichiers en base avant de calculer quoi que ce soit,
        puis ne rend chaque tuile impactée qu'UNE seule fois pour l'ensemble du lot, au lieu de
        la redessiner à chaque fichier. Très largement plus rapide pour un import initial massif
        (ex: des centaines de GPX d'un coup) que d'appeler ingest_gpx() en boucle."""
        rows = []
        newly_ingested_paths = []
        skipped = 0
        for path in paths:
            if self.already_ingested(path):
                continue
            try:
                segments, waypoints = gpx_utils.parse_gpx_segments(path)
            except Exception:
                log.exception("GPX illisible/invalide, ignoré : %s", path)
                newly_ingested_paths.append(path)  # marqué comme traité pour ne pas le retenter en boucle
                skipped += 1
                continue
            for seg_pts in segments:
                sid = self._next_seg_id
                self._next_seg_id += 1
                for i, (lat, lon) in enumerate(seg_pts):
                    rows.append((sid, i, lat, lon))
            for lat, lon in waypoints:
                sid = self._next_seg_id
                self._next_seg_id += 1
                rows.append((sid, 0, lat, lon))
            newly_ingested_paths.append(path)

        if skipped:
            log.warning("%d fichier(s) GPX invalide(s) ignoré(s) lors de cet import groupé", skipped)

        if not rows:
            with _lock:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO ingested_files(path) VALUES (?)",
                    [(p,) for p in newly_ingested_paths],
                )
                self.conn.commit()
            return

        with _lock:
            self.conn.executemany(
                "INSERT INTO points(seg_id, seq, lat, lon) VALUES (?,?,?,?)", rows
            )
            self.conn.executemany(
                "INSERT OR IGNORE INTO ingested_files(path) VALUES (?)",
                [(p,) for p in newly_ingested_paths],
            )
            self.conn.commit()

            arr = np.array(rows, dtype=np.float64)
            new_seg = arr[:, 0].astype(np.int64)
            new_lat = arr[:, 2]
            new_lon = arr[:, 3]

            self.seg = np.concatenate([self.seg, new_seg])
            self.seq = np.concatenate([self.seq, arr[:, 1].astype(np.int64)])
            self.lat = np.concatenate([self.lat, new_lat])
            self.lon = np.concatenate([self.lon, new_lon])

            # une seule passe de rendu sur l'ensemble des tuiles impactées par TOUT le lot
            self._rerender_affected(new_lat, new_lon, new_seg)

    # ---------- rendu ----------

    def _rerender_affected(self, lat_new, lon_new, seg_new):
        """Calcule l'ensemble des tuiles à (re)générer comme l'UNION des empreintes de
        chaque trace individuelle (pas la bbox globale du lot). Important pour un import
        groupé de traces géographiquement dispersées (ex: plusieurs pays) : une bbox
        globale engloberait d'immenses zones vides et ferait exploser le nombre de
        tuiles à haut zoom."""
        if len(seg_new) == 0:
            return

        # les tableaux sont dans l'ordre d'insertion (par seg_id croissant) -> les runs
        # d'un même seg_id sont contigus, pas besoin de trier
        change_idx = np.where(np.diff(seg_new) != 0)[0] + 1
        groups = np.split(np.arange(len(seg_new)), change_idx)

        tiles_by_zoom = set()
        for z in range(self.min_zoom, self.max_zoom + 1):
            deg_per_px = 360.0 / (2 ** z * 256)
            margin_px = self.line_width_px + 3 * self.blur_sigma_px + PAD
            buffer_deg = deg_per_px * margin_px

            for g in groups:
                if len(g) == 0:
                    continue
                lat_g = lat_new[g]
                lon_g = lon_new[g]
                west, east = float(np.min(lon_g)), float(np.max(lon_g))
                south, north = float(np.min(lat_g)), float(np.max(lat_g))

                w, s, e, n = west - buffer_deg, south - buffer_deg, east + buffer_deg, north + buffer_deg
                w, e = max(w, -180), min(e, 180)
                s, n = max(s, -85.05), min(n, 85.05)

                for tile in mercantile.tiles(w, s, e, n, [z]):
                    tiles_by_zoom.add((z, tile.x, tile.y))

        for z, x, y in tiles_by_zoom:
            self._render_tile(z, x, y)

    def _render_tile(self, z, x, y):
        deg_per_px = 360.0 / (2 ** z * 256)
        margin_px = self.line_width_px + 3 * self.blur_sigma_px + PAD
        buf = deg_per_px * margin_px
        bounds = mercantile.bounds(x, y, z)
        w, s, e, n = bounds.west - buf, bounds.south - buf, bounds.east + buf, bounds.north + buf

        mask = (self.lon >= w) & (self.lon <= e) & (self.lat >= s) & (self.lat <= n)
        if not np.any(mask):
            out = self.tiles_dir / str(z) / str(x) / f"{y}.png"
            if out.exists():
                out.unlink()
            return

        lat_sel = self.lat[mask]
        lon_sel = self.lon[mask]
        seg_sel = self.seg[mask]
        seq_sel = self.seq[mask]

        size = 256
        canvas_size = size + 2 * PAD
        canvas = np.zeros((canvas_size, canvas_size), dtype=np.float32)
        origin_x = x * 256 - PAD
        origin_y = y * 256 - PAD

        px_all, py_all = self._lonlat_to_pixel(lon_sel, lat_sel, z)
        lx_all = px_all - origin_x
        ly_all = py_all - origin_y

        # regroupe les points contigus par segment (les tableaux sont triés par seg_id, seq :
        # le masque de bbox préserve cet ordre, donc les runs restent contigus)
        change_idx = np.where(np.diff(seg_sel) != 0)[0] + 1
        seg_groups = np.split(np.arange(len(seg_sel)), change_idx)

        # à l'intérieur d'un même segment, un saut dans "seq" veut dire que la trace est
        # sortie de la zone visible de cette tuile puis y est revenue plus tard (boucle,
        # aller-retour...) : il ne faut PAS relier ces deux portions par une ligne, sous
        # peine de créer une fausse ligne droite qui ne correspond à aucune trace réelle.
        groups = []
        for g in seg_groups:
            if len(g) <= 1:
                groups.append(g)
                continue
            seq_g = seq_sel[g]
            gap_idx = np.where(np.diff(seq_g) != 1)[0] + 1
            groups.extend(np.split(g, gap_idx))

        for g in groups:
            if len(g) == 0:
                continue
            xs = lx_all[g]
            ys = ly_all[g]

            temp = Image.new("L", (canvas_size, canvas_size), 0)
            draw = ImageDraw.Draw(temp)
            if len(g) == 1:
                r = self.line_width_px / 2
                draw.ellipse([xs[0] - r, ys[0] - r, xs[0] + r, ys[0] + r], fill=255)
            else:
                coords = list(zip(xs.tolist(), ys.tolist()))
                draw.line(coords, fill=255, width=self.line_width_px, joint="curve")

            # une trace ne contribue jamais plus d'1 "unité" à un pixel, même si
            # elle se recoupe elle-même (aller-retour) -> un passage = un passage
            canvas += (np.asarray(temp, dtype=np.float32) > 0).astype(np.float32)

        if self.blur_sigma_px > 0:
            canvas = ndimage.gaussian_filter(canvas, sigma=self.blur_sigma_px)

        cropped = canvas[PAD:PAD + size, PAD:PAD + size]
        img = self._colorize(cropped)

        out_dir = self.tiles_dir / str(z) / str(x)
        out_dir.mkdir(parents=True, exist_ok=True)
        img.save(out_dir / f"{y}.png", format="PNG", optimize=True)

    def get_tile_path(self, z, x, y):
        p = self.tiles_dir / str(z) / str(x) / f"{y}.png"
        return p if p.exists() else None

    # ---------- maths / rendu bas niveau ----------

    @staticmethod
    def _lonlat_to_pixel(lon, lat, zoom):
        lat_rad = np.radians(lat)
        n = 2 ** zoom
        x = (lon + 180.0) / 360.0 * n * 256
        y = (1.0 - np.log(np.tan(lat_rad) + 1.0 / np.cos(lat_rad)) / math.pi) / 2.0 * n * 256
        return x, y

    @staticmethod
    def _make_lut(gradient_stops, n=256):
        stops = sorted(gradient_stops, key=lambda s: s[0])
        fracs = np.array([s[0] for s in stops])
        colors = np.array([s[1] for s in stops], dtype=np.float64)
        lut = np.zeros((n, 4), dtype=np.uint8)
        xs = np.linspace(0, 1, n)
        for c in range(4):
            lut[:, c] = np.clip(np.interp(xs, fracs, colors[:, c]), 0, 255).astype(np.uint8)
        return lut

    def _colorize(self, intensity: np.ndarray) -> Image.Image:
        norm = np.clip(intensity / self.vmax, 0, 1)
        # compression logarithmique : booste la visibilité des faibles densités (ex: un seul
        # passage) sans changer color_saturation. Plus contrast_boost est élevé, plus un
        # passage isolé ressort par rapport à un fond quasi-transparent.
        k = self.contrast_boost
        norm = np.log1p(norm * k) / np.log1p(k)
        idx = (norm * 255).astype(np.uint8)
        rgba = self._lut[idx]
        return Image.fromarray(rgba, mode="RGBA")
