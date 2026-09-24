"""cron_credentials -- the one place every scheduled job and the
pre-push hook gets its credential from.

The fallback is what makes Stage A safe to deploy in either order, so it
is pinned in both directions. So is the rule that nothing here ever
prints a secret: the original reason this project is rotating its shared
password at all is that one appeared in terminal output.
"""
import pytest

import cron_credentials
from cron_credentials import (
    LEGACY_PASSWORD_VAR,
    SERVICE_PASSWORD_VAR,
    MissingServiceCredential,
    service_auth,
    service_password,
)

NEW = "the-machines-own-service-secret"
OLD = "the-retiring-shared-password"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(SERVICE_PASSWORD_VAR, raising=False)
    monkeypatch.delenv(LEGACY_PASSWORD_VAR, raising=False)


def test_the_new_variable_is_preferred(monkeypatch):
    monkeypatch.setenv(SERVICE_PASSWORD_VAR, NEW)
    monkeypatch.setenv(LEGACY_PASSWORD_VAR, OLD)
    assert service_password() == (NEW, SERVICE_PASSWORD_VAR)


def test_it_falls_back_to_the_shared_password_until_the_new_one_is_set(monkeypatch):
    """This is the entire reason Stage A cannot break a cron: the code
    can ship before or after the Railway variable exists."""
    monkeypatch.setenv(LEGACY_PASSWORD_VAR, OLD)
    assert service_password() == (OLD, LEGACY_PASSWORD_VAR)


def test_an_empty_new_variable_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv(SERVICE_PASSWORD_VAR, "")
    monkeypatch.setenv(LEGACY_PASSWORD_VAR, OLD)
    assert service_password() == (OLD, LEGACY_PASSWORD_VAR)


def test_neither_variable_set_raises_a_message_naming_what_to_set():
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


def test_the_fallback_says_so_loudly_and_still_prints_no_value(monkeypatch, capsys):
    """This printed line is how each cron's rollout is verified per
    service: a tick still saying LEGACY has not picked the new secret up."""
    monkeypatch.setenv(LEGACY_PASSWORD_VAR, OLD)
    service_auth()
    out = capsys.readouterr().out
    assert "LEGACY fallback" in out
    assert SERVICE_PASSWORD_VAR in out
    assert OLD not in out


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
    assert f'os.environ["{LEGACY_PASSWORD_VAR}"]' not in body, module_name
    assert f'os.environ.get("{LEGACY_PASSWORD_VAR}"' not in body, module_name
