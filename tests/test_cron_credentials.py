"""cron_credentials -- the one place every scheduled job and the
pre-push hook gets its credential from.

Stage A read the service credential and fell back to the shared password,
which is what made that deploy safe in either order. The fallback is gone
now, so these tests pin the opposite: the retired password is NOT read.

Also pinned, and the reason any of this happened: nothing here ever prints
a secret. This project rotated its shared password because one appeared in
terminal output.
"""
import pytest

import cron_credentials
from cron_credentials import (
    SERVICE_PASSWORD_VAR,
    MissingServiceCredential,
    service_auth,
    service_password,
)

LEGACY_PASSWORD_VAR = "CARDFOUNDRY_ADMIN_PASSWORD"  # retired; must not be read

NEW = "the-machines-own-service-secret"
OLD = "the-retiring-shared-password"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(SERVICE_PASSWORD_VAR, raising=False)
    monkeypatch.delenv(LEGACY_PASSWORD_VAR, raising=False)


def test_the_service_variable_is_the_credential(monkeypatch):
    monkeypatch.setenv(SERVICE_PASSWORD_VAR, NEW)
    assert service_password() == NEW


def test_the_retired_shared_password_is_NOT_read_as_a_fallback(monkeypatch):
    """INVERTED at v2.0.0. Stage A deliberately fell back to this, which
    is what made that deploy safe in either order. All six crons were then
    confirmed on the service credential, and only then was the fallback
    removed -- a fallback nobody notices is still in use is a credential
    nobody knows they depend on."""
    monkeypatch.setenv(LEGACY_PASSWORD_VAR, OLD)
    with pytest.raises(MissingServiceCredential):
        service_password()


def test_an_empty_service_variable_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv(SERVICE_PASSWORD_VAR, "")
    monkeypatch.setenv(LEGACY_PASSWORD_VAR, OLD)
    with pytest.raises(MissingServiceCredential):
        service_password()


def test_an_unset_variable_raises_a_message_naming_what_to_set():
    with pytest.raises(MissingServiceCredential) as exc:
        service_password()
    assert SERVICE_PASSWORD_VAR in str(exc.value)


def test_service_auth_is_shaped_for_httpx(monkeypatch):
    monkeypatch.setenv(SERVICE_PASSWORD_VAR, NEW)
    assert service_auth() == ("cron", NEW)
    assert service_auth("hook") == ("hook", NEW)


def test_it_prints_the_variable_name_and_never_the_value(monkeypatch, capsys):
    monkeypatch.setenv(SERVICE_PASSWORD_VAR, NEW)
    service_auth()
    out = capsys.readouterr().out
    assert SERVICE_PASSWORD_VAR in out
    assert NEW not in out


def test_the_legacy_fallback_line_is_gone_from_the_source():
    """The "LEGACY fallback" line existed to show, per tick, which crons
    had not moved over yet. All six had, so it has no job left."""
    body = open(cron_credentials.__file__).read()
    # The printed marker and the variable itself -- not the word "legacy",
    # which the module docstring is free to use explaining the removal.
    assert "LEGACY fallback" not in body
    assert "CARDFOUNDRY_ADMIN_PASSWORD" not in body


def test_the_module_never_prints_a_password_anywhere_in_its_source():
    """Pins the absence: no print/log of the value itself, only names."""
    source = (cron_credentials.__file__)
    body = open(source).read()
    assert "print(password" not in body
    assert "%s\", password" not in body


@pytest.mark.parametrize("module_name", [
    "scheduled_order_sync", "scheduled_color_backfill", "scheduled_job_retention",
    "scheduled_vacuum", "scheduled_perform_sync", "scheduled_pricing_apply",
])
def test_every_scheduled_job_gets_its_credential_from_here(module_name):
    """No script may read the password variable directly any more -- one
    that did would silently stay on the shared password through Stage B
    and start failing the moment it was retired."""
    import importlib
    module = importlib.import_module(module_name)
    body = open(module.__file__).read()
    assert "service_auth()" in body, module_name
    assert LEGACY_PASSWORD_VAR not in body, module_name
