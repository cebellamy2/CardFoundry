"""The machines' own credential -- the only way a non-human reaches the
app.

Added in Stage A alongside the shared password; since Stage B (v2.0.0)
it is the sole machine credential and the shared password is gone. The
gate's own behaviour (exemptions, fail-closed, browser-vs-machine, the
retirement itself) lives in tests/test_auth_gate.py; this file owns the
credential.
"""
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
import operator_auth_service
from models import Base, OperatorSession

SERVICE = "the-machines-own-service-secret"
OPERATOR_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'service_credential.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(main, "SERVICE_PASSWORD", SERVICE)
    monkeypatch.setattr(main, "DEV_AUTH_DISABLED", False)
    monkeypatch.setattr(main, "COOKIE_SECURE", True)
    return engine


def make_client(**kwargs):
    # https because the operator session cookie is issued Secure whenever
    # COOKIE_SECURE is set -- see tests/test_operator_auth.py.
    return TestClient(main.app, base_url="https://testserver", **kwargs)


@pytest.fixture
def gate_log(caplog):
    """cardfoundry has propagate=False in production, so caplog is
    attached to the logger directly or every assertion passes vacuously."""
    logger = logging.getLogger("cardfoundry")
    caplog.set_level(logging.INFO, logger="cardfoundry")
    logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


# ---------------------------------------------------------------------
# The service credential works
# ---------------------------------------------------------------------

@pytest.mark.parametrize("username", ["cron", "hook"])
def test_a_service_username_with_the_service_secret_passes(db, username):
    assert make_client().get("/orders", auth=(username, SERVICE)).status_code == 200


def test_the_cron_shaped_request_each_scheduled_job_actually_sends(db):
    response = make_client().post(
        "/manapool/sync", auth=("cron", SERVICE), follow_redirects=False,
    )
    assert response.status_code != 401


def test_the_pre_push_hooks_readiness_probe_passes(db):
    response = make_client().get("/admin/deploy-readiness", auth=("hook", SERVICE))
    assert response.status_code in (200, 503), response.status_code


# ---------------------------------------------------------------------
# Every way of getting it wrong is refused, identically
# ---------------------------------------------------------------------

def refusals(client):
    return [
        client.get("/orders", auth=("cron", "the-wrong-secret")),
        client.get("/orders", auth=("nobody", SERVICE)),
        client.get("/orders", auth=("", SERVICE)),
        client.get("/orders", auth=("Cron", SERVICE)),   # case matters
        client.get("/orders", auth=("cron", "")),
        client.get("/orders"),
    ]


def test_a_wrong_secret_a_wrong_username_and_no_credential_are_all_refused(db):
    for response in refusals(make_client()):
        assert response.status_code == 401
        # No Basic challenge any more: there is no password a browser
        # prompt could usefully collect (see test_auth_gate).
        assert "WWW-Authenticate" not in response.headers


def test_every_refusal_is_byte_identical(db):
    """Nothing in the response may hint at which half was wrong."""
    responses = refusals(make_client())
    bodies = {r.content for r in responses}
    headers = {tuple(sorted(r.headers.items())) for r in responses}
    assert len(bodies) == 1, "refusals differ in body"
    assert len(headers) == 1, "refusals differ in headers"


def test_a_service_username_is_refused_when_no_service_secret_is_configured(db, monkeypatch):
    monkeypatch.setattr(main, "SERVICE_PASSWORD", None)
    assert make_client().get("/orders", auth=("cron", SERVICE)).status_code == 401
    # FAILS CLOSED. In Stage A the shared password rescued this caller,
    # which is what made that deploy safe. There is nothing behind it now,
    # and that is the intended end state.
    assert make_client().get("/orders", auth=("cron", "anything-else")).status_code == 401


def test_a_non_ascii_service_secret_attempt_fails_closed_rather_than_500ing(db):
    """compare_digest raises TypeError on non-ASCII str instead of
    returning False -- the same crash that took the whole app down on
    2026-08-17 through the shared-password compare."""
    response = make_client().get("/orders", auth=("cron", "wrong-pässwörd"))
    assert response.status_code == 401


def test_an_undecodable_basic_header_does_not_leak_into_the_service_check(db):
    response = make_client().get(
        "/orders", headers={"Authorization": "Basic !!!!not-base64!!!!"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------
# A machine is not a person
# ---------------------------------------------------------------------

def test_the_service_credential_cannot_sign_in_at_login(db):
    """It gets a machine THROUGH the gate. It is not an account."""
    client = make_client()
    response = client.post(
        "/login", data={"username": "cron", "password": SERVICE},
        auth=("cron", SERVICE), follow_redirects=False,
    )
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    with Session(db) as session:
        assert session.query(OperatorSession).count() == 0


def test_passing_the_gate_as_a_service_creates_no_session(db):
    client = make_client()
    client.get("/orders", auth=("cron", SERVICE))
    client.get("/inventory", auth=("hook", SERVICE))
    with Session(db) as session:
        assert session.query(OperatorSession).count() == 0
    assert main.OPERATOR_SESSION_COOKIE not in client.cookies


def test_an_operator_session_token_is_not_a_service_credential(db):
    with Session(db) as session:
        user, _ = operator_auth_service.set_operator_credentials(
            session, "cebellamy2@gmail.com", OPERATOR_PASSWORD,
        )
        token = operator_auth_service.create_operator_session(session, user.id).token
        session.commit()
    # The token is a real, valid session -- but presented as a Basic
    # password it is just a wrong string.
    assert make_client().get("/orders", auth=("cron", token)).status_code == 401
    # ...while the same token in its own cookie still works.
    assert make_client(
        cookies={main.OPERATOR_SESSION_COOKIE: token},
    ).get("/orders").status_code == 200


def test_the_service_secret_is_not_an_operator_password(db):
    with Session(db) as session:
        operator_auth_service.set_operator_credentials(
            session, "cebellamy2@gmail.com", OPERATOR_PASSWORD,
        )
        session.commit()
    response = make_client().post(
        "/login", data={"username": "cebellamy2@gmail.com", "password": SERVICE},
        auth=("cron", SERVICE), follow_redirects=False,
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------
# It coexists with the other way through
# ---------------------------------------------------------------------

def test_an_operator_session_still_passes(db):
    with Session(db) as session:
        user, _ = operator_auth_service.set_operator_credentials(
            session, "cebellamy2@gmail.com", OPERATOR_PASSWORD,
        )
        token = operator_auth_service.create_operator_session(session, user.id).token
        session.commit()
    client = make_client(cookies={main.OPERATOR_SESSION_COOKIE: token})
    assert client.get("/orders").status_code == 200


def test_the_two_original_exemptions_are_unchanged(db):
    client = make_client()
    assert client.get("/portal/login").status_code == 200
    assert client.get("/portalish-unrelated-route", headers={"Accept": "*/*"}).status_code == 401
    assert client.post("/webhooks/manapool/orders").status_code != 401


# ---------------------------------------------------------------------
# Logging: the rollout has to be verifiable from the app's own logs
# ---------------------------------------------------------------------

def test_a_service_acceptance_is_logged_with_the_caller_and_never_the_secret(db, gate_log):
    make_client().get("/orders", auth=("cron", SERVICE))
    assert "service credential accepted for 'cron' (GET /orders)" in gate_log.text
    assert SERVICE not in gate_log.text


def test_the_retiring_shared_password_warning_is_gone(db, gate_log):
    """That WARNING existed to answer "has every cron moved over yet?"
    during Stage A. All six had, which is what let Stage B ship -- so the
    line has no remaining job and must not still be emitted."""
    make_client().get("/orders", auth=("cron", "the-old-shared-password"))
    make_client().get("/orders", auth=("chris", "the-old-shared-password"))
    assert "RETIRING" not in gate_log.text
    import inspect
    assert "RETIRING" not in inspect.getsource(main)


def test_a_refused_attempt_never_logs_the_attempted_secret(db, gate_log):
    make_client().get("/orders", auth=("cron", "an-attempted-secret"))
    assert "an-attempted-secret" not in gate_log.text
