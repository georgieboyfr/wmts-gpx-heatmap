"""Faux client garminconnect : connexion par identifiants (avec ou sans MFA), jeton
enregistré sur disque, rechargement depuis le jeton."""
import json
import os

from garminconnect import GarminConnectAuthenticationError

PASSWORD = "bon-mot-de-passe"
MFA_CODE = "123456"


class FakeGarmin:
    needs_mfa = False
    fail_token_login = False

    def __init__(self, email=None, password=None, return_on_mfa=False, prompt_mfa=None, **kwargs):
        self.email, self.password = email, password
        self.return_on_mfa, self.prompt_mfa = return_on_mfa, prompt_mfa
        self.full_name = None
        self.client = self  # garminconnect expose le client bas niveau (dump) sous .client

    def _token_file(self, store):
        return os.path.join(store, "garmin_tokens.json")

    def login(self, tokenstore=None):
        if tokenstore and os.path.exists(self._token_file(tokenstore)) and not FakeGarmin.fail_token_login:
            self.full_name = "Camille Test"
            return None, None
        if self.password != PASSWORD:
            raise GarminConnectAuthenticationError("Authentication failed (401 Unauthorized)")
        if FakeGarmin.needs_mfa:
            if self.return_on_mfa:
                return "needs_mfa", {"cookies": "x"}
            self.prompt_mfa()  # en tâche de fond : lève une erreur
        self.full_name = "Camille Test"
        if tokenstore:
            self.dump(tokenstore)
        return None, None

    def resume_login(self, client_state, code):
        if code != MFA_CODE:
            raise GarminConnectAuthenticationError("code MFA invalide")
        self.full_name = "Camille Test"
        return None, None

    def dump(self, path):
        os.makedirs(path, exist_ok=True)
        with open(self._token_file(path), "w") as f:
            json.dump({"token": "t"}, f)

    def get_full_name(self):
        return self.full_name

    def get_activities(self, start, limit):
        return []
