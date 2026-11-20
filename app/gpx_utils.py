"""Extraction des traces d'un fichier GPX, groupées par segment continu
(tracks/routes) et par waypoints isolés.

On ne renvoie plus des points "à plat" : chaque segment garde ses points
dans l'ordre, pour pouvoir être dessiné comme une ligne continue plutôt que
d'accumuler point par point.
"""
import os

import gpxpy

MIN_YEAR, MAX_YEAR = 1980, 2200  # en dehors : date manifestement fausse, ignorée


def normalize_path(path) -> str:
    """Forme canonique d'un chemin, pour qu'un même fichier ait toujours la même clé
    en base, qu'il vienne de watchdog, d'un glob ou de la sync Garmin."""
    return os.path.normpath(os.path.abspath(str(path)))


def is_gpx(path) -> bool:
    return str(path).lower().endswith(".gpx")


def _read(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return gpxpy.parse(f)


def _activity_year(gpx) -> int:
    """Année de l'activité : celle du premier point horodaté, sinon la date des
    métadonnées du fichier. 0 si le fichier n'est pas daté."""
    times = (p.time for track in gpx.tracks for seg in track.segments for p in seg.points)
    times = (t for t in times if t is not None)
    first = next(times, None) or next((p.time for r in gpx.routes for p in r.points if p.time), None) or gpx.time
    if first is None or not MIN_YEAR <= first.year <= MAX_YEAR:
        return 0
    return first.year


def parse_gpx(path: str):
    """Retourne (segments, waypoints, année).
    segments : liste de listes de (lat, lon), une par trace/route continue
               (un segment d'un seul point est dessiné comme un point isolé).
    waypoints : liste de (lat, lon) isolés.
    année : année de l'activité, 0 si le fichier n'est pas daté.
    """
    gpx = _read(path)
    segments = []
    for track in gpx.tracks:
        for seg in track.segments:
            pts = [(p.latitude, p.longitude) for p in seg.points]
            if pts:
                segments.append(pts)
    for route in gpx.routes:
        pts = [(p.latitude, p.longitude) for p in route.points]
        if pts:
            segments.append(pts)

    waypoints = [(wp.latitude, wp.longitude) for wp in gpx.waypoints]
    return segments, waypoints, _activity_year(gpx)


def parse_gpx_segments(path: str):
    """Retourne (segments, waypoints), voir parse_gpx."""
    segments, waypoints, _ = parse_gpx(path)
    return segments, waypoints


def read_year(path: str) -> int:
    """Année de l'activité d'un fichier GPX (0 si non daté)."""
    return _activity_year(_read(path))
