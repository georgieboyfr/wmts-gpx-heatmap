import os

import pytest
from garminconnect import GarminConnectAuthenticationError

from app.garmin_account import GarminAccount, GarminNotConnected

from .fake_garmin import MFA_CODE, PASSWORD, FakeGarmin


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(FakeGarmin, "needs_mfa", False)
    monkeypatch.setattr(FakeGarmin, "fail_token_login", False)
    return GarminAccount(tmp_path / "tokens", tmp_path / "state.json", garmin_class=lambda: FakeGarmin)


def test_login_without_mfa_and_without_remembering_password(account, tmp_path):
    assert account.status()["state"] == "disconnected"
    with pytest.raises(GarminNotConnected):
        account.client()
    assert account.start_login("a@b.c", PASSWORD, remember=False) == {"state": "connected"}
    st = account.status()
    assert st["state"] == "connected" and st["account_name"] == "Camille Test"
    assert st["token_present"] and not st["password_remembered"]
    assert not (tmp_path / "tokens" / "credentials.json").exists()


def test_login_with_mfa_and_remembered_password(account, tmp_path, monkeypatch):
    monkeypatch.setattr(FakeGarmin, "needs_mfa", True)
    assert account.start_login("a@b.c", PASSWORD, remember=True) == {"state": "mfa_pending"}
    assert account.status()["state"] == "mfa_pending"
    with pytest.raises(GarminConnectAuthenticationError):
        account.submit_mfa("000000")
    assert account.status()["state"] == "mfa_pending"  # on peut réessayer le code
    account.submit_mfa(MFA_CODE)
    st = account.status()
    assert st["state"] == "connected" and st["password_remembered"] and st["password_source"] == "admin"
    creds = tmp_path / "tokens" / "credentials.json"
    assert creds.exists()
    if os.name == "posix":
        assert oct(creds.stat().st_mode & 0o777) == "0o600"

    account.forget_password()
    assert not account.status()["password_remembered"] and account.status()["token_present"]


def test_wrong_password_is_reported(account):
    with pytest.raises(GarminConnectAuthenticationError):
        account.start_login("a@b.c", "faux", remember=False)
    assert account.status()["state"] == "disconnected"


def test_expired_session_needs_login_then_recovers(account, monkeypatch):
    account.start_login("a@b.c", PASSWORD, remember=False)
    account.invalidate()
    monkeypatch.setattr(FakeGarmin, "fail_token_login", True)  # jeton refusé, pas de mot de passe mémorisé
    with pytest.raises(GarminConnectAuthenticationError):
        account.client()
    assert account.status()["state"] == "needs_login"
    monkeypatch.setattr(FakeGarmin, "fail_token_login", False)
    account.start_login("a@b.c", PASSWORD, remember=False)
    assert account.status()["state"] == "connected" and account.status()["last_error"] is None


def test_mfa_requested_in_background_needs_login(account, monkeypatch):
    account.start_login("a@b.c", PASSWORD, remember=True)
    account.invalidate()
    monkeypatch.setattr(FakeGarmin, "fail_token_login", True)
    monkeypatch.setattr(FakeGarmin, "needs_mfa", True)
    with pytest.raises(Exception, match="MFA"):
        account.client()
    assert account.status()["state"] == "needs_login"


def test_remembered_password_allows_automatic_relogin(account, monkeypatch):
    account.start_login("a@b.c", PASSWORD, remember=True)
    account.invalidate()
    monkeypatch.setattr(FakeGarmin, "fail_token_login", True)
    assert account.client().get_full_name() == "Camille Test"  # reconnecté avec le mot de passe


def test_logout_removes_token_and_password(account, tmp_path):
    account.start_login("a@b.c", PASSWORD, remember=True)
    account.record_sync(3)
    account.logout()
    st = account.status()
    assert st["state"] == "disconnected" and not st["token_present"] and not st["password_remembered"]
    assert st["total_downloaded"] == 3  # l'historique de sync est conservé


def test_state_survives_restart(account, tmp_path):
    account.start_login("a@b.c", PASSWORD, remember=False)
    account.record_sync(5)
    again = GarminAccount(tmp_path / "tokens", tmp_path / "state.json", garmin_class=lambda: FakeGarmin)
    st = again.status()
    assert st["state"] == "connected" and st["last_sync_new"] == 5 and st["account_name"] == "Camille Test"
