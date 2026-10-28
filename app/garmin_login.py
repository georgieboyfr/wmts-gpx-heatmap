"""
Alternative en ligne de commande à la connexion depuis l'admin (onglet Garmin) :
établit la session Garmin Connect (gère le code MFA si activé) et génère le jeton
mis en cache, que le service recharge ensuite automatiquement.

Usage (depuis la racine du projet, avec les variables d'environnement définies) :

    docker compose run --rm wmts-gpx-heatmap python -m app.garmin_login

ou, si le conteneur tourne déjà :

    docker exec -it wmts-gpx-heatmap python -m app.garmin_login
"""
import os
import sys

from garminconnect import Garmin


def main():
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    token_store = os.environ.get("GARMIN_TOKEN_STORE", "/data/garmin_tokens")

    if not email or not password:
        print("GARMIN_EMAIL / GARMIN_PASSWORD non définis (voir .env).", file=sys.stderr)
        sys.exit(1)

    client = Garmin(email, password, prompt_mfa=lambda: input("Code MFA Garmin : "))
    client.login(token_store)
    print(f"Connexion réussie, jeton enregistré dans {token_store}")
    print("Le service peut maintenant se synchroniser sans interaction.")


if __name__ == "__main__":
    main()
