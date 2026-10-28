"""Extraction des traces d'un fichier GPX, groupées par segment continu
(tracks/routes) et par waypoints isolés.

On ne renvoie plus des points "à plat" : chaque segment garde ses points
dans l'ordre, pour pouvoir être dessiné comme une ligne continue plutôt que
d'accumuler point par point.
"""
import os

import gpxpy


def normalize_path(path) -> str:
    """Forme canonique d'un chemin, pour qu'un même fichier ait toujours la même clé
    en base, qu'il vienne de watchdog, d'un glob ou de la sync Garmin."""
    return os.path.normpath(os.path.abspath(str(path)))


def is_gpx(path) -> bool:
    return str(path).lower().endswith(".gpx")


def parse_gpx_segments(path: str):
    """Retourne (segments, waypoints).
    segments : liste de listes de (lat, lon), une par trace/route continue
               (un segment d'un seul point est dessiné comme un point isolé).
    waypoints : liste de (lat, lon) isolés.
    """
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        gpx = gpxpy.parse(f)

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
    return segments, waypoints
