"""
Compte Garmin Connect : connexion depuis l'admin (code MFA compris), statut, jeton
de session, et choix de mémoriser ou non le mot de passe.

- Connexion : identifiants saisis dans l'admin. Si Garmin demande un code MFA, la
  connexion est mise en attente (quelques minutes) jusqu'à la saisie du code.
- Une fois connecté, seul le jeton de session est indispensable : il est enregistré
  dans le dossier des jetons et renouvelé automatiquement par la librairie.
- Mot de passe, au choix de l'utilisateur :
  (a) non mémorisé : il ne sert qu'à obtenir le jeton. Si la session expire un jour,
      il faudra se reconnecter depuis l'admin ;
  (b) mémorisé : enregistré en clair (fichier lisible du seul utilisateur du service)
      dans le dossier des jetons, pour pouvoir se reconnecter seul si la session
      expire (sauf si Garmin redemande un code MFA).
  Les variables d'environnement GARMIN_EMAIL / GARMIN_PASSWORD, si elles sont
  définies, jouent le rôle de (b).
"""
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("garmin_account")

MFA_TIMEOUT_S = 5 * 60
TOKEN_FILE = "garmin_tokens.json"  # nom utilisé par garminconnect dans le dossier des jetons
CREDENTIALS_FILE = "credentials.json"


class GarminNotConnected(Exception):
    pass


def _garmin_class():
    from garminconnect import Garmin
    return Garmin


def _is_auth_error(exc) -> bool:
    try:
        from garminconnect import GarminConnectAuthenticationError
        if isinstance(exc, GarminConnectAuthenticationError):
            return True
    except ImportError:
        pass
    return isinstance(exc, MfaRequired)


class MfaRequired(Exception):
    pass


def _no_interactive_mfa():
    # en tâche de fond, personne ne peut saisir de code : il faut se reconnecter depuis l'admin
    raise MfaRequired("Garmin demande un code MFA : reconnecte le compte depuis l'admin.")


def _write_private(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


class GarminAccount:
    def __init__(self, token_store, state_path, env_email=None, env_password=None, garmin_class=None):
        self.token_store = str(token_store)
        self.state_path = Path(state_path)
        self.env_credentials = (env_email, env_password) if env_email and env_password else None
        self._garmin_class = garmin_class or _garmin_class
        self._lock = threading.RLock()
        self._client = None
        self._pending = None  # connexion en attente du code MFA
        self._state = {}
        if self.state_path.exists():
            try:
                self._state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                log.exception("État Garmin illisible (%s), ignoré", self.state_path)

    # ---------- fichiers ----------

    @property
    def _token_path(self) -> Path:
        p = Path(self.token_store)
        return p if p.suffix == ".json" else p / TOKEN_FILE

    @property
    def _credentials_path(self) -> Path:
        p = Path(self.token_store)
        return (p.parent if p.suffix == ".json" else p) / CREDENTIALS_FILE

    def _stored_credentials(self):
        try:
            data = json.loads(self._credentials_path.read_text(encoding="utf-8"))
            return data["email"], data["password"]
        except FileNotFoundError:
            return None
        except Exception:
            log.exception("Identifiants Garmin mémorisés illisibles")
            return None

    def _credentials(self):
        return self._stored_credentials() or self.env_credentials

    def _update_state(self, **values):
        with self._lock:
            self._state.update(values)
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.state_path.with_name(self.state_path.name + ".tmp")
                tmp.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self.state_path)
            except Exception:
                log.exception("Impossible d'enregistrer l'état Garmin")

    # ---------- statut ----------

    def is_configured(self) -> bool:
        return self._token_path.exists() or self._credentials() is not None

    def status(self) -> dict:
        with self._lock:
            pending = self._pending if self._pending and self._pending["expires_at"] > time.time() else None
            st = dict(self._state)
            if pending:
                state = "mfa_pending"
            elif not self.is_configured():
                state = "disconnected"
            elif st.get("auth_failed"):
                state = "needs_login"
            else:
                state = "connected"
            stored = self._stored_credentials()
            return {
                "state": state,
                "account_name": st.get("account_name"),
                "email": st.get("email"),
                "token_present": self._token_path.exists(),
                "password_remembered": stored is not None or self.env_credentials is not None,
                "password_source": "admin" if stored else ("env" if self.env_credentials else None),
                "mfa_expires_at": pending["expires_at"] if pending else None,
                "connected_at": st.get("connected_at"),
                "last_sync_at": st.get("last_sync_at"),
                "last_sync_new": st.get("last_sync_new"),
                "last_error": st.get("last_error"),
                "last_error_at": st.get("last_error_at"),
                "total_downloaded": st.get("total_downloaded", 0),
            }

    # ---------- connexion depuis l'admin ----------

    def start_login(self, email: str, password: str, remember: bool) -> dict:
        """Démarre une connexion. Retourne {"state": "connected"} ou {"state": "mfa_pending"}."""
        if not email or not password:
            raise ValueError("Email et mot de passe requis")
        Garmin = self._garmin_class()
        with self._lock:
            self._pending = None
            client = Garmin(email, password, return_on_mfa=True)
            status, client_state = client.login()  # sans jeton : connexion par identifiants
            if status == "needs_mfa":
                self._pending = {"client": client, "state": client_state, "email": email,
                                 "password": password if remember else None,
                                 "expires_at": time.time() + MFA_TIMEOUT_S}
                log.info("Connexion Garmin : code MFA demandé")
                return {"state": "mfa_pending"}
            self._finish_login(client, email, password if remember else None, profile_loaded=False)
            return {"state": "connected"}

    def submit_mfa(self, code: str) -> dict:
        with self._lock:
            pending = self._pending
            if not pending or pending["expires_at"] < time.time():
                self._pending = None
                raise ValueError("Aucune connexion en attente de code MFA (ou délai dépassé) : recommence.")
            pending["client"].resume_login(pending["state"], code.strip())
            self._pending = None
            self._finish_login(pending["client"], pending["email"], pending["password"], profile_loaded=True)
            return {"state": "connected"}

    def cancel_login(self):
        with self._lock:
            self._pending = None

    def _finish_login(self, client, email, password_to_remember, profile_loaded):
        # en mode return_on_mfa, la librairie n'enregistre pas le jeton elle-même
        Path(self.token_store).mkdir(parents=True, exist_ok=True)
        client.client.dump(self.token_store)
        if not profile_loaded:
            # recharge depuis le jeton tout juste enregistré : vérifie qu'il fonctionne
            # et charge le profil (nom du compte)
            client = self._garmin_class()()
            client.login(self.token_store)
        if password_to_remember:
            _write_private(self._credentials_path, {"email": email, "password": password_to_remember})
        else:
            self._credentials_path.unlink(missing_ok=True)
        self._client = client
        self._update_state(account_name=client.get_full_name(), email=email, connected_at=time.time(),
                           auth_failed=False, last_error=None, last_error_at=None)
        log.info("Compte Garmin connecté : %s", client.get_full_name() or email)

    def logout(self):
        """Oublie le jeton de session et le mot de passe mémorisé."""
        with self._lock:
            self._client = None
            self._pending = None
            self._token_path.unlink(missing_ok=True)
            self._credentials_path.unlink(missing_ok=True)
            self._update_state(account_name=None, email=None, connected_at=None, auth_failed=False,
                               last_error=None, last_error_at=None)
        log.info("Compte Garmin déconnecté")

    def forget_password(self):
        with self._lock:
            self._credentials_path.unlink(missing_ok=True)
        log.info("Mot de passe Garmin oublié (le jeton de session est conservé)")

    # ---------- utilisation par la sync ----------

    def client(self):
        """Client connecté, réutilisé d'une sync à l'autre ; reconnexion depuis le jeton
        (ou le mot de passe mémorisé) si besoin."""
        with self._lock:
            if self._client is not None:
                return self._client
            if not self.is_configured():
                raise GarminNotConnected("Compte Garmin non connecté : connecte-le depuis l'admin.")
            creds = self._credentials() or (None, None)
            client = self._garmin_class()(creds[0], creds[1], prompt_mfa=_no_interactive_mfa)
            try:
                client.login(self.token_store)
            except Exception as e:
                self.record_error(e)
                raise
            self._client = client
            self._update_state(account_name=client.get_full_name() or self._state.get("account_name"),
                               auth_failed=False)
            return client

    def invalidate(self):
        """À appeler après une erreur d'API : la prochaine sync repartira d'une connexion neuve."""
        with self._lock:
            self._client = None

    def record_error(self, exc):
        auth = _is_auth_error(exc)
        if auth:
            self._client = None
        self._update_state(last_error=str(exc) or exc.__class__.__name__, last_error_at=time.time(),
                           auth_failed=auth or self._state.get("auth_failed", False))

    def record_sync(self, new_files: int):
        self._update_state(last_sync_at=time.time(), last_sync_new=new_files, last_error=None,
                           last_error_at=None, auth_failed=False,
                           total_downloaded=self._state.get("total_downloaded", 0) + new_files)
