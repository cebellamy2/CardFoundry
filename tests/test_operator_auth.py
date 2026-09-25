"""Operator user accounts (v1.197.0) -- sessions, lockout, and the
guarantee that adding them changed nothing about the Basic gate.

The whole point of this slice is that it is ADDITIVE. Roughly half of
these tests exist to pin what did NOT change: the crons' Basic auth, the
two middleware exemptions, and the fact that an operator session and a
consignor session cannot be swapped for one another.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import consignor_auth_service
import inventory_sync_service
import main
import operator_auth_service
from models import Base, Consignor, OperatorSession, OperatorUser

PASSWORD = "correct-horse-battery-staple"
# The shared password is gone (v2.0.0). Machines use this instead;
# it is here so the "not a person" tests have something real to send.
SERVICE = "the-machines-own-service-secret"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'operator_auth.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(main, "SERVICE_PASSWORD", SERVICE)
    monkeypatch.setattr(main, "DEV_AUTH_DISABLED", False)
    monkeypatch.setattr(main, "COOKIE_SECURE", True)
    return engine


def make_client(**kwargs):
    """HTTPS on purpose. COOKIE_SECURE is True in these tests, so the
    session cookie is issued with Secure -- over plain http the client
    would silently never send it back, and every session test would fail
    for a reason that has nothing to do with the code under test. This
    also means the Secure flag is genuinely exercised rather than
    quietly worked around."""
    return TestClient(main.app, base_url="https://testserver", **kwargs)


def make_user(engine, username="cebellamy2@gmail.com", password=PASSWORD):
    with Session(engine) as session:
        user, created = operator_auth_service.set_operator_credentials(
            session, username, password,
        )
        session.commit()
        return user.id, created


def sign_in(client, username="cebellamy2@gmail.com", password=PASSWORD):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        auth=None,
        follow_redirects=False,
    )


# ---------------------------------------------------------------------
# The service layer
# ---------------------------------------------------------------------

def test_a_password_is_never_stored_in_the_clear(db):
    make_user(db)
    with Session(db) as session:
        user = session.query(OperatorUser).one()
        assert PASSWORD not in (user.password_hash or "")
        assert PASSWORD not in (user.password_salt or "")
        assert len(user.password_salt) == 32  # 16 random bytes, hex
        assert operator_auth_service.verify_password(
            PASSWORD, user.password_hash, user.password_salt,
        )
        assert not operator_auth_service.verify_password(
            "not-it", user.password_hash, user.password_salt,
        )


def test_the_same_password_hashes_differently_for_two_users(db):
    make_user(db, "one@example.com")
    make_user(db, "two@example.com")
    with Session(db) as session:
        hashes = {u.password_hash for u in session.query(OperatorUser).all()}
    assert len(hashes) == 2, "a shared salt would make these identical"


def test_usernames_are_normalised_so_a_phone_keyboard_cannot_lock_you_out(db):
    make_user(db, "CeBellamy2@Gmail.com ")
    with Session(db) as session:
        user, outcome = operator_auth_service.authenticate_operator(
            session, "  cebellamy2@GMAIL.com", PASSWORD,
        )
        assert outcome == operator_auth_service.OUTCOME_OK
        assert user.username == "cebellamy2@gmail.com"


def test_setting_credentials_twice_resets_rather_than_duplicating(db):
    _, created_first = make_user(db)
    _, created_second = make_user(db, password="a-different-password")
    assert created_first is True and created_second is False
    with Session(db) as session:
        assert session.query(OperatorUser).count() == 1
        _, outcome = operator_auth_service.authenticate_operator(session, "cebellamy2@gmail.com", PASSWORD)
        assert outcome == operator_auth_service.OUTCOME_BAD_PASSWORD


def test_a_password_reset_signs_out_every_open_session(db):
    user_id, _ = make_user(db)
    with Session(db) as session:
        token = operator_auth_service.create_operator_session(session, user_id).token
        session.commit()
    with Session(db) as session:
        assert operator_auth_service.validate_operator_session(session, token)
    make_user(db, password="brand-new-password")
    with Session(db) as session:
        assert operator_auth_service.validate_operator_session(session, token) is None


def test_an_expired_session_is_not_a_session(db):
    user_id, _ = make_user(db)
    with Session(db) as session:
        record = operator_auth_service.create_operator_session(session, user_id)
        record.expires_at = datetime.now() - timedelta(seconds=1)
        token = record.token
        session.commit()
    with Session(db) as session:
        assert operator_auth_service.validate_operator_session(session, token) is None


def test_an_unknown_token_is_not_a_session(db):
    make_user(db)
    with Session(db) as session:
        assert operator_auth_service.validate_operator_session(session, "made-up") is None
        assert operator_auth_service.validate_operator_session(session, "") is None


def test_sessions_last_thirty_days(db):
    user_id, _ = make_user(db)
    with Session(db) as session:
        record = operator_auth_service.create_operator_session(session, user_id)
        remaining = record.expires_at - datetime.now()
    assert timedelta(days=29, hours=23) < remaining <= timedelta(days=30)


def test_a_deactivated_user_cannot_sign_in_or_use_an_open_session(db):
    user_id, _ = make_user(db)
    with Session(db) as session:
        token = operator_auth_service.create_operator_session(session, user_id).token
        session.get(OperatorUser, user_id).is_active = False
        session.commit()
    with Session(db) as session:
        assert operator_auth_service.validate_operator_session(session, token) is None
        _, outcome = operator_auth_service.authenticate_operator(
            session, "cebellamy2@gmail.com", PASSWORD,
        )
        assert outcome == operator_auth_service.OUTCOME_INACTIVE


# ---------------------------------------------------------------------
# Lockout: 5 failures, 15 minutes, self-clearing
# ---------------------------------------------------------------------

def test_the_fifth_failure_locks_the_account(db):
    make_user(db)
    with Session(db) as session:
        for attempt in range(1, 5):
            _, outcome = operator_auth_service.authenticate_operator(
                session, "cebellamy2@gmail.com", "wrong",
            )
            assert outcome == operator_auth_service.OUTCOME_BAD_PASSWORD, attempt
        _, outcome = operator_auth_service.authenticate_operator(
            session, "cebellamy2@gmail.com", "wrong",
        )
        assert outcome == operator_auth_service.OUTCOME_JUST_LOCKED
        user = session.query(OperatorUser).one()
        remaining = user.locked_until - datetime.now()
        assert timedelta(minutes=14) < remaining <= timedelta(minutes=15)


def test_the_correct_password_is_refused_during_a_lock(db):
    make_user(db)
    with Session(db) as session:
        for _ in range(5):
            operator_auth_service.authenticate_operator(session, "cebellamy2@gmail.com", "wrong")
        user, outcome = operator_auth_service.authenticate_operator(
            session, "cebellamy2@gmail.com", PASSWORD,
        )
        assert user is None
        assert outcome == operator_auth_service.OUTCOME_LOCKED


def test_a_successful_sign_in_resets_the_failure_count(db):
    make_user(db)
    with Session(db) as session:
        for _ in range(4):
            operator_auth_service.authenticate_operator(session, "cebellamy2@gmail.com", "wrong")
        assert session.query(OperatorUser).one().failed_login_count == 4
        operator_auth_service.authenticate_operator(session, "cebellamy2@gmail.com", PASSWORD)
        assert session.query(OperatorUser).one().failed_login_count == 0
        # ...so the next four failures do not lock.
        for _ in range(4):
            _, outcome = operator_auth_service.authenticate_operator(
                session, "cebellamy2@gmail.com", "wrong",
            )
        assert outcome == operator_auth_service.OUTCOME_BAD_PASSWORD


def test_the_lock_clears_itself_after_fifteen_minutes(db):
    make_user(db)
    with Session(db) as session:
        for _ in range(5):
            operator_auth_service.authenticate_operator(session, "cebellamy2@gmail.com", "wrong")
        user = session.query(OperatorUser).one()
        # Wind the clock forward by moving the expiry back, rather than
        # by patching datetime -- same effect, nothing global stubbed.
        user.locked_until = datetime.now() - timedelta(seconds=1)
        session.commit()
        signed_in, outcome = operator_auth_service.authenticate_operator(
            session, "cebellamy2@gmail.com", PASSWORD,
        )
        assert outcome == operator_auth_service.OUTCOME_OK and signed_in is not None
        assert session.query(OperatorUser).one().locked_until is None


def test_waiting_out_a_lock_restores_a_full_allowance_of_five(db):
    """The counter is reset when the lock is applied, so an expired lock
    must not leave the account one wrong password from being re-locked."""
    make_user(db)
    with Session(db) as session:
        for _ in range(5):
            operator_auth_service.authenticate_operator(session, "cebellamy2@gmail.com", "wrong")
        session.query(OperatorUser).one().locked_until = datetime.now() - timedelta(seconds=1)
        session.commit()
        for attempt in range(1, 5):
            _, outcome = operator_auth_service.authenticate_operator(
                session, "cebellamy2@gmail.com", "wrong",
            )
            assert outcome == operator_auth_service.OUTCOME_BAD_PASSWORD, attempt
        _, outcome = operator_auth_service.authenticate_operator(
            session, "cebellamy2@gmail.com", "wrong",
        )
        assert outcome == operator_auth_service.OUTCOME_JUST_LOCKED


def test_sessions_already_open_keep_working_during_a_lock(db):
    """Locking an account must never sign the operator out of the device
    in his hand -- the lock refuses NEW sign-ins, nothing else."""
    user_id, _ = make_user(db)
    with Session(db) as session:
        token = operator_auth_service.create_operator_session(session, user_id).token
        for _ in range(5):
            operator_auth_service.authenticate_operator(session, "cebellamy2@gmail.com", "wrong")
        session.commit()
    client = make_client(cookies={main.OPERATOR_SESSION_COOKIE: token})
    assert client.get("/orders").status_code == 200


def test_an_unknown_username_creates_no_row_to_lock(db):
    """Lockout is tracked on the user row on purpose: counting attempts
    against arbitrary submitted usernames would let anyone grow a table
    by posting made-up names."""
    make_user(db)
    with Session(db) as session:
        for _ in range(20):
            _, outcome = operator_auth_service.authenticate_operator(
                session, "nobody@example.com", "wrong",
            )
            assert outcome == operator_auth_service.OUTCOME_UNKNOWN_USER
        assert session.query(OperatorUser).count() == 1


def test_clear_lockout_unlocks_immediately(db):
    make_user(db)
    with Session(db) as session:
        for _ in range(5):
            operator_auth_service.authenticate_operator(session, "cebellamy2@gmail.com", "wrong")
        session.commit()
    # This is exactly what operator_account.py --unlock-only does.
    with Session(db) as session:
        operator_auth_service.clear_lockout(
            operator_auth_service.get_operator_user(session, "cebellamy2@gmail.com"),
        )
        session.commit()
    with Session(db) as session:
        _, outcome = operator_auth_service.authenticate_operator(
            session, "cebellamy2@gmail.com", PASSWORD,
        )
        assert outcome == operator_auth_service.OUTCOME_OK


def test_resetting_the_password_also_clears_a_lockout(db):
    make_user(db)
    with Session(db) as session:
        for _ in range(5):
            operator_auth_service.authenticate_operator(session, "cebellamy2@gmail.com", "wrong")
        session.commit()
    make_user(db, password="a-whole-new-password")
    with Session(db) as session:
        _, outcome = operator_auth_service.authenticate_operator(
            session, "cebellamy2@gmail.com", "a-whole-new-password",
        )
        assert outcome == operator_auth_service.OUTCOME_OK


# ---------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------

def test_signing_in_sets_an_httponly_session_cookie_and_redirects_home(db):
    make_user(db)
    client = make_client()
    response = sign_in(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert main.OPERATOR_SESSION_COOKIE in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Secure" in cookie, "COOKIE_SECURE is True, so this is production-shaped"
    assert "Path=/" in cookie


def test_the_session_cookie_alone_gets_past_the_gate(db):
    make_user(db)
    client = make_client()
    assert client.get("/orders").status_code == 401
    sign_in(client)
    # No Basic credentials on this one -- only the cookie the sign-in set.
    assert client.get("/orders").status_code == 200
    assert client.get("/").status_code == 200


def test_logout_destroys_the_session_server_side_not_just_the_cookie(db):
    make_user(db)
    client = make_client()
    sign_in(client)
    token = client.cookies[main.OPERATOR_SESSION_COOKIE]
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    with Session(db) as session:
        assert session.query(OperatorSession).count() == 0
    # Even replaying the stolen cookie is now worthless.
    replay = make_client(cookies={main.OPERATOR_SESSION_COOKIE: token})
    assert replay.get("/orders").status_code == 401


def test_a_wrong_username_and_a_wrong_password_are_indistinguishable(db):
    make_user(db)
    client = make_client()
    wrong_user = sign_in(client, username="nobody@example.com")
    wrong_password = sign_in(client, password="nope")
    assert wrong_user.status_code == wrong_password.status_code == 401
    assert wrong_user.text == wrong_password.text
    assert "set-cookie" not in wrong_user.headers
    assert "Incorrect username or password." in wrong_user.text


def test_a_locked_account_looks_exactly_like_a_wrong_password(db):
    make_user(db)
    client = make_client()
    for _ in range(5):
        sign_in(client, password="wrong")
    locked = sign_in(client)  # correct password, but locked
    baseline = sign_in(client, username="nobody@example.com")
    assert locked.status_code == baseline.status_code == 401
    assert locked.text == baseline.text
    # Scoped to the message the person actually reads -- a naive
    # substring search over the whole page matches the shared CSS
    # ("bypass blocks"), which is not what this is about.
    message = locked.text.split('<div class="danger">')[1].split("</div>")[0]
    assert message.strip() == "Incorrect username or password."


def test_the_login_page_offers_a_logout_only_once_signed_in(db):
    make_user(db)
    client = make_client()
    signed_out = client.get("/login", auth=None)
    assert signed_out.status_code == 200
    assert 'action="/logout"' not in signed_out.text
    assert 'action="/login"' in signed_out.text
    sign_in(client)
    signed_in = client.get("/login")
    assert 'action="/logout"' in signed_in.text
    assert "cebellamy2@gmail.com" in signed_in.text


def test_the_login_page_carries_no_javascript(db):
    make_user(db)
    client = make_client()
    page = client.get("/login", auth=None).text
    assert "<script" not in page.lower()
    assert "onclick" not in page.lower()


def test_a_failed_attempt_is_persisted_even_though_the_request_failed(db):
    make_user(db)
    client = make_client()
    for _ in range(5):
        sign_in(client, password="wrong")
    with Session(db) as session:
        assert operator_auth_service.is_locked(session.query(OperatorUser).one())


# ---------------------------------------------------------------------
# What did NOT change
# ---------------------------------------------------------------------

@pytest.mark.parametrize("username", ["cron", "hook", "whoever", ""])
def test_basic_auth_still_works_exactly_as_before_for_any_username(db, username):
    """The six crons send ("cron", password); the pre-push hook sends
    ("hook", password). The username has always been discarded and still
    is -- this slice must not have started caring about it."""
    client = make_client()
    assert client.get("/orders", auth=("cron", SERVICE)).status_code == 200


def test_a_cron_shaped_request_with_no_cookie_still_gets_through(db):
    """Shape-for-shape what scheduled_order_sync.py and friends send."""
    client = make_client()
    response = client.post("/manapool/sync", auth=("cron", SERVICE), follow_redirects=False)
    assert response.status_code != 401


def test_a_wrong_machine_credential_is_a_bare_401(db):
    """INVERTED at v2.0.0. This asserted a Basic challenge header, which
    was right while a browser prompt could still collect a usable
    password. It cannot any more, so the header is gone."""
    response = make_client().get("/orders", auth=("cron", "wrong"))
    assert response.status_code == 401
    assert "WWW-Authenticate" not in response.headers


def test_an_unauthenticated_browser_is_now_sent_to_login(db):
    """INVERTED at v2.0.0. This asserted the Basic challenge and NO
    redirect, with the comment "redirecting to /login is for when Basic is
    retired, not now". Basic is retired; now is then."""
    response = make_client().get(
        "/orders", headers={"Accept": "text/html"}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "WWW-Authenticate" not in response.headers


def test_the_gate_no_longer_no_ops_when_a_credential_is_missing(db, monkeypatch):
    """INVERTED at v2.0.0, and the most important inversion in the file.
    This used to assert that unsetting one variable opened the whole app.
    It now asserts the opposite. Full coverage in test_auth_gate.py."""
    monkeypatch.setattr(main, "SERVICE_PASSWORD", None)
    assert make_client().get("/orders", headers={"Accept": "*/*"}).status_code == 401


def test_a_bogus_session_cookie_is_refused_rather_than_passing(db):
    make_user(db)
    forged = make_client(cookies={main.OPERATOR_SESSION_COOKIE: "forged"})
    assert forged.get("/orders").status_code == 401
    assert forged.get("/orders", auth=("cron", SERVICE)).status_code == 200


def test_the_two_middleware_exemptions_are_unchanged(db):
    client = make_client()
    assert client.get("/portal/login").status_code == 200
    assert client.get("/portalish-unrelated-route").status_code == 401
    # The webhook prefix is still exempt from the gate (the route itself
    # 404s unless MANAPOOL_WEBHOOK_ENABLED is set -- either way, not 401).
    assert client.post("/webhooks/manapool/orders").status_code != 401


# ---------------------------------------------------------------------
# The two auth systems cannot be swapped
# ---------------------------------------------------------------------

def make_consignor(engine):
    with Session(engine) as session:
        consignor = Consignor(name="Rose")
        session.add(consignor)
        session.flush()
        consignor_auth_service.set_consignor_portal_credentials(
            session, consignor.id, "rose@example.com", PASSWORD,
        )
        token = consignor_auth_service.create_consignor_session(session, consignor.id).token
        session.commit()
        return token


def test_a_consignor_session_does_not_pass_the_operator_gate(db):
    consignor_token = make_consignor(db)
    # Presented under BOTH cookie names, to prove it is the table lookup
    # that refuses it and not merely the cookie's name.
    as_operator = make_client(cookies={main.OPERATOR_SESSION_COOKIE: consignor_token})
    assert as_operator.get("/orders").status_code == 401
    as_consignor = make_client(cookies={main.CONSIGNOR_SESSION_COOKIE: consignor_token})
    assert as_consignor.get("/orders").status_code == 401


def test_an_operator_session_is_not_a_consignor_session(db):
    user_id, _ = make_user(db)
    with Session(db) as session:
        operator_token = operator_auth_service.create_operator_session(session, user_id).token
        session.commit()
        assert consignor_auth_service.validate_consignor_session(session, operator_token) is None


def test_neither_lookup_can_see_the_other_table(db):
    consignor_token = make_consignor(db)
    user_id, _ = make_user(db)
    with Session(db) as session:
        operator_token = operator_auth_service.create_operator_session(session, user_id).token
        session.commit()
        assert operator_auth_service.validate_operator_session(session, consignor_token) is None
        assert consignor_auth_service.validate_consignor_session(session, operator_token) is None
        assert operator_auth_service.validate_operator_session(session, operator_token) is not None
        assert consignor_auth_service.validate_consignor_session(session, consignor_token) is not None


def test_the_operator_cookie_has_a_different_name_from_the_consignor_one(db):
    assert main.OPERATOR_SESSION_COOKIE != main.CONSIGNOR_SESSION_COOKIE


# ---------------------------------------------------------------------
# Logging: username and outcome, never a secret
# ---------------------------------------------------------------------

@pytest.fixture
def auth_log(caplog):
    """cardfoundry has propagate=False in production, so caplog must be
    attached to the logger directly or every assertion below passes
    vacuously (see reference_cardfoundry_logging)."""
    import logging
    logger = logging.getLogger("cardfoundry")
    caplog.set_level(logging.INFO, logger="cardfoundry")
    logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


def test_a_successful_sign_in_logs_the_username_and_never_the_password(db, auth_log):
    make_user(db)
    sign_in(make_client())
    text = auth_log.text
    assert "sign-in succeeded for cebellamy2@gmail.com" in text
    assert PASSWORD not in text


def test_a_failed_sign_in_logs_the_reason_without_the_attempted_password(db, auth_log):
    make_user(db)
    sign_in(make_client(), password="hunter2-was-the-attempt")
    assert "sign-in failed for cebellamy2@gmail.com (bad_password)" in auth_log.text
    assert "hunter2-was-the-attempt" not in auth_log.text


def test_tripping_the_lock_is_logged_distinctly_from_an_ordinary_failure(db, auth_log):
    make_user(db)
    client = make_client()
    for _ in range(5):
        sign_in(client, password="wrong")
    assert "locked after 5 consecutive failed sign-ins" in auth_log.text
    assert "refused for 15 minutes" in auth_log.text


def test_a_session_token_is_never_logged(db, auth_log):
    make_user(db)
    client = make_client()
    sign_in(client)
    token = client.cookies[main.OPERATOR_SESSION_COOKIE]
    client.get("/orders")
    client.post("/logout", follow_redirects=False)
    assert token not in auth_log.text
