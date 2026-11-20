"""
Dégradés de couleur prêts à l'emploi, choisis par leur identifiant : pour colorer les
couches par année sans avoir à régler chaque palier (l'éditeur complet reste disponible
pour la couche « toutes années »). Les noms affichés sont traduits côté admin.

Chaque dégradé suit la même rampe d'opacité que celui livré par défaut (transparent ->
opaque), pour qu'un passage isolé ressorte autant avec l'un qu'avec l'autre.
"""

PRESETS = {
    "ice": [  # bleu glacier, le dégradé par défaut de config.yaml
        [0.00, [10, 15, 40, 0]],
        [0.15, [15, 40, 95, 130]],
        [0.40, [25, 110, 210, 190]],
        [0.65, [80, 190, 255, 225]],
        [0.85, [190, 235, 255, 245]],
        [1.00, [255, 255, 255, 255]],
    ],
    "fire": [
        [0.00, [60, 0, 0, 0]],
        [0.15, [120, 10, 10, 140]],
        [0.40, [220, 40, 20, 200]],
        [0.65, [255, 130, 0, 230]],
        [0.85, [255, 220, 60, 245]],
        [1.00, [255, 255, 220, 255]],
    ],
    "orange": [
        [0.00, [60, 20, 0, 0]],
        [0.15, [140, 50, 0, 140]],
        [0.40, [252, 82, 0, 200]],
        [0.65, [255, 140, 40, 230]],
        [0.85, [255, 200, 120, 245]],
        [1.00, [255, 245, 220, 255]],
    ],
    "green": [
        [0.00, [0, 30, 10, 0]],
        [0.15, [0, 80, 40, 130]],
        [0.40, [20, 160, 70, 190]],
        [0.65, [90, 220, 90, 225]],
        [0.85, [200, 250, 150, 245]],
        [1.00, [255, 255, 230, 255]],
    ],
    "violet": [
        [0.00, [30, 0, 40, 0]],
        [0.15, [70, 10, 100, 130]],
        [0.40, [150, 30, 190, 190]],
        [0.65, [230, 70, 220, 225]],
        [0.85, [255, 170, 240, 245]],
        [1.00, [255, 240, 255, 255]],
    ],
    "viridis": [
        [0.00, [68, 1, 84, 0]],
        [0.15, [72, 40, 120, 150]],
        [0.35, [49, 104, 142, 200]],
        [0.55, [33, 145, 140, 225]],
        [0.75, [94, 201, 98, 240]],
        [1.00, [253, 231, 37, 255]],
    ],
    "white": [  # pour un fond de carte sombre
        [0.00, [255, 255, 255, 0]],
        [0.20, [255, 255, 255, 90]],
        [0.60, [255, 255, 255, 190]],
        [1.00, [255, 255, 255, 255]],
    ],
    "graphite": [  # pour un fond de carte clair
        [0.00, [20, 20, 30, 0]],
        [0.20, [20, 20, 30, 90]],
        [0.60, [20, 20, 30, 190]],
        [1.00, [0, 0, 0, 255]],
    ],
}


def validate_year_gradients(values) -> dict:
    """{année: identifiant de dégradé} ; une valeur vide (None, "") retire le choix de
    cette année, qui reprend alors le dégradé de la couche « toutes années »."""
    if not isinstance(values, dict):
        raise ValueError("year_gradients doit être un objet {année: dégradé}")
    out = {}
    for year, preset in values.items():
        y = str(year)
        if not (y.isdigit() and len(y) == 4):
            raise ValueError(f"année invalide : {year}")
        if preset in (None, ""):
            out[y] = None
        elif preset in PRESETS:
            out[y] = preset
        else:
            raise ValueError(f"dégradé inconnu : {preset} (au choix : {', '.join(PRESETS)})")
    return out
