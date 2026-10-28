"""
Réglages modifiables depuis l'admin, sans redémarrage.

config.yaml reste la source des valeurs par défaut ; les modifications faites dans
l'admin sont enregistrées comme des surcharges dans un fichier JSON à côté de la base
(donc dans /data, persistant). Les clés jamais modifiées dans l'admin continuent de
suivre config.yaml, et "Revenir à config.yaml" supprime simplement les surcharges.
"""
import copy
import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("settings")

RENDER_KEYS = ("min_zoom", "max_zoom", "line_width_px", "blur_sigma_px", "color_saturation", "contrast_boost")

GARMIN_DEFAULTS = {
    "enabled": False,
    "sync_interval_minutes": 60,
    "lookback_days_first_sync": 365,
    "ingest_batch_size": 0,
}
GARMIN_LIMITS = {  # clé -> (min, max)
    "sync_interval_minutes": (5, 7 * 24 * 60),
    "lookback_days_first_sync": (1, 100 * 365),
    "ingest_batch_size": (0, 10000),
}


def validate_garmin(values: dict) -> dict:
    out = {}
    for key, value in values.items():
        if key == "enabled":
            if not isinstance(value, bool):
                raise ValueError("garmin.enabled doit être true ou false")
        elif key in GARMIN_LIMITS:
            lo, hi = GARMIN_LIMITS[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"garmin.{key} doit être un entier entre {lo} et {hi}")
        else:
            raise ValueError(f"réglage Garmin inconnu : {key}")
        out[key] = value
    return out


class Settings:
    def __init__(self, base_config: dict, path):
        self.base = base_config
        self.path = Path(path)
        self._lock = threading.Lock()
        self._overrides = {}
        if self.path.exists():
            try:
                self._overrides = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                log.exception("Réglages illisibles (%s), valeurs de config.yaml utilisées", self.path)

    def effective(self) -> dict:
        """Configuration complète : config.yaml + surcharges de l'admin."""
        with self._lock:
            cfg = copy.deepcopy(self.base)
            ov = copy.deepcopy(self._overrides)
        cfg["render"] = {**cfg.get("render", {}), **ov.get("render", {})}
        if "color_gradient" in ov:
            cfg["color_gradient"] = ov["color_gradient"]
        cfg["garmin"] = {**GARMIN_DEFAULTS, **(cfg.get("garmin") or {}), **ov.get("garmin", {})}
        return cfg

    def overrides(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._overrides)

    def set_render(self, render: dict, gradient):
        """Enregistre les réglages de rendu (validés par l'appelant). Seules les valeurs qui
        diffèrent de config.yaml deviennent des surcharges."""
        base_render = self.base.get("render", {})
        diff = {k: render[k] for k in RENDER_KEYS if k in render and render[k] != base_render.get(k)}
        with self._lock:
            self._overrides.pop("render", None)
            self._overrides.pop("color_gradient", None)
            if diff:
                self._overrides["render"] = diff
            if gradient != self.base.get("color_gradient"):
                self._overrides["color_gradient"] = gradient
            self._save()

    def set_garmin(self, values: dict):
        """Comme pour le rendu, seules les valeurs qui diffèrent de config.yaml sont gardées."""
        values = validate_garmin(values)
        base = {**GARMIN_DEFAULTS, **(self.base.get("garmin") or {})}
        with self._lock:
            merged = {**self._overrides.get("garmin", {}), **values}
            diff = {k: v for k, v in merged.items() if v != base.get(k)}
            self._overrides.pop("garmin", None)
            if diff:
                self._overrides["garmin"] = diff
            self._save()

    def reset(self, section: str):
        with self._lock:
            self._overrides.pop(section, None)
            if section == "render":
                self._overrides.pop("color_gradient", None)
            self._save()

    def _save(self):
        # appelé avec self._lock ; écriture atomique
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._overrides, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
