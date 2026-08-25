"""Extraction des traces d'un fichier GPX, groupées par segment continu
(tracks/routes) et par waypoints isolés.

On ne renvoie plus des points "à plat" : chaque segment garde ses points
dans l'ordre, pour pouvoir être dessiné comme une ligne continue plutôt que
d'accumuler point par point.
"""
import gpxpy


def parse_gpx_segments(path: str):
    """Retourne (segments, waypoints).
    segments : liste de listes de (lat, lon), une par trace/route continue.
    waypoints : liste de (lat, lon) isolés.
    """
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        gpx = gpxpy.parse(f)

    segments = []
    for track in gpx.tracks:
        for seg in track.segments:
            pts = [(p.latitude, p.longitude) for p in seg.points]
            if len(pts) >= 2:
                segments.append(pts)
            elif len(pts) == 1:
                segments.append(pts)  # traité comme un point isolé au rendu
    for route in gpx.routes:
        pts = [(p.latitude, p.longitude) for p in route.points]
        if pts:
            segments.append(pts)

    waypoints = [(wp.latitude, wp.longitude) for wp in gpx.waypoints]
    return segments, waypoints
