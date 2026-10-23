"""Deactivating an operator account -- the reversible answer to an
account that should not have existed.

Driven two ways on purpose: through the real /login route, because what
matters is that a deactivated person cannot get in rather than that a
column changed; and through the CLI's own entry points, because the
script is the break-glass and its REFUSALS are the part worth pinning.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
import operator_account
import operator_auth_service
from models import Base, OperatorSession, OperatorUser

PASSWORD = "correct-horse-battery-staple"
OTHER_PASSWORD = "a-second-entirely-different-password"
SERVICE = "the-machines-own-service-secret"
CHRIS = "cebellamy2@gmail.com"
STRAY = "your_username"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'operator_deactivation.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(operator_account, "engine", engine)
    monkeypatch.setattr(main, "SERVICE_PASSWORD", SERVICE)
    monkeypatch.setattr(main, "DEV_AUTH_DISABLED", False)
    monkeypatch.setattr(main, "COOKIE_SECURE", True)
    return engine


def make_client(**kwargs):
    # HTTPS: COOKIE_SECURE is True here, so over plain http the client
    # would never send the session cookie back.
    return TestClient(main.app, base_url="https://testserver", **kwargs)


def make_user(engine, username, password=PASSWORD):
    with Session(engine) as session:
        user, _ = operator_auth_service.set_operator_credentials(
            session, username, password, allow_create=True,
        )
        session.commit()
        return user.id


def both_accounts(engine):
    """The production shape this was built for: Chris plus the stray."""
    return make_user(engine, CHRIS), make_user(engine, STRAY, OTHER_PASSWORD)


def sign_in(client, username, password):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        auth=None,
        follow_redirects=False,
    )


# ---------------------------------------------------------------------
# What the person at the login page gets
# ---------------------------------------------------------------------

def test_a_deactivated_account_cannot_sign_in(db):
    both_accounts(db)
    with Session(db) as session:
        operator_auth_service.set_operator_active(session, STRAY, active=False)
        session.commit()

    client = make_client()
    response = sign_in(client, STRAY, OTHER_PASSWORD)

    assert response.status_code == 401
    assert "Incorrect username or password." in response.text
    assert main.OPERATOR_SESSION_COOKIE not in response.cookies


def test_the_refusal_does_not_reveal_that_the_account_is_deactivated(db):
    """Byte-identical to a wrong password. An inactive account must not
    be distinguishable from a non-existent one at the login page."""
    both_accounts(db)
    with Session(db) as session:
        operator_auth_service.set_operator_active(session, STRAY, active=False)
        session.commit()

    client = make_client()
    deactivated = sign_in(client, STRAY, OTHER_PASSWORD)
    unknown = sign_in(client, "nobody@example.com", OTHER_PASSWORD)

    assert deactivated.status_code == unknown.status_code == 401
    assert deactivated.text == unknown.text


def test_reactivating_lets_the_same_password_back_in(db):
    both_accounts(db)
    with Session(db) as session:
        operator_auth_service.set_operator_active(session, STRAY, active=False)
        session.commit()
    with Session(db) as session:
        operator_auth_service.set_operator_active(session, STRAY, active=True)
        session.commit()

    client = make_client()
    response = sign_in(client, STRAY, OTHER_PASSWORD)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.cookies.get(main.OPERATOR_SESSION_COOKIE)


def test_deactivating_does_not_touch_the_other_account(db):
    both_accounts(db)
    with Session(db) as session:
        operator_auth_service.set_operator_active(session, STRAY, active=False)
        session.commit()

    client = make_client()
    assert sign_in(client, CHRIS, PASSWORD).status_code == 303


def test_an_open_session_stops_working_the_moment_the_account_is_off(db):
    """The real reason this is not just a column: a signed-in browser
    must lose access, not keep it until the cookie expires."""
    both_accounts(db)
    client = make_client()
    assert sign_in(client, STRAY, OTHER_PASSWORD).status_code == 303
    # /login is the page that names who is signed in, so it reads the
    # session rather than merely being let through by it.
    assert f"You are signed in as <strong>{STRAY}</strong>" in client.get(
        "/login", auth=None,
    ).text
    assert client.get("/inventory", auth=None, follow_redirects=False).status_code == 200

    with Session(db) as session:
        operator_auth_service.set_operator_active(session, STRAY, active=False)
        session.commit()

    # Same cookie, still unexpired -- the browser is simply no longer
    # anybody. The gate fails closed (v2.0.0), so an app page is refused
    # outright rather than redirected.
    after = client.get("/login", auth=None)
    assert "You are signed in as" not in after.text
    assert client.get("/inventory", auth=None, follow_redirects=False).status_code == 401


def test_deactivating_deletes_that_accounts_session_rows(db):
    chris_id, stray_id = both_accounts(db)
    stray = make_client()
    chris = make_client()
    sign_in(stray, STRAY, OTHER_PASSWORD)
    sign_in(chris, CHRIS, PASSWORD)

    with Session(db) as session:
        operator_auth_service.set_operator_active(session, STRAY, active=False)
        session.commit()

    with Session(db) as session:
        owners = [r.operator_user_id for r in session.query(OperatorSession).all()]
    assert stray_id not in owners
    assert owners == [chris_id]


# ---------------------------------------------------------------------
# The rule that stops a lockout
# ---------------------------------------------------------------------

def test_the_last_active_operator_cannot_be_deactivated(db):
    make_user(db, CHRIS)
    with Session(db) as session:
        with pytest.raises(ValueError) as refusal:
            operator_auth_service.set_operator_active(session, CHRIS, active=False)
        session.rollback()

    assert "only active operator account" in str(refusal.value)
    client = make_client()
    assert sign_in(client, CHRIS, PASSWORD).status_code == 303


def test_the_last_remaining_active_one_is_refused_even_with_inactive_rows_around(db):
    """Two rows exist, but one is already off -- the count that matters
    is of ACTIVE accounts, not of accounts."""
    both_accounts(db)
    with Session(db) as session:
        operator_auth_service.set_operator_active(session, STRAY, active=False)
        session.commit()
    with Session(db) as session:
        with pytest.raises(ValueError, match="only active operator account"):
            operator_auth_service.set_operator_active(session, CHRIS, active=False)
        session.rollback()

    client = make_client()
    assert sign_in(client, CHRIS, PASSWORD).status_code == 303


def test_an_unknown_username_is_refused_rather_than_created(db):
    make_user(db, CHRIS)
    with Session(db) as session:
        with pytest.raises(ValueError, match="No operator account exists"):
            operator_auth_service.set_operator_active(
                session, "nobody@example.com", active=False,
            )
        session.rollback()
    with Session(db) as session:
        assert session.query(OperatorUser).count() == 1


def test_switching_to_the_state_it_is_already_in_changes_nothing(db):
    both_accounts(db)
    with Session(db) as session:
        user, changed = operator_auth_service.set_operator_active(
            session, STRAY, active=True,
        )
        assert changed is False
        assert user.is_active is True
        session.rollback()


# ---------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------

def test_the_cli_deactivates_and_says_what_it_did(db, capsys):
    both_accounts(db)
    operator_account.set_active(STRAY, active=False, dry_run=False)
    out = capsys.readouterr().out

    assert f"Deactivated operator account: {STRAY}" in out
    assert "signed out" in out
    assert "Active operator accounts now: 1." in out
    with Session(db) as session:
        assert session.query(OperatorUser).filter_by(username=STRAY).one().is_active is False


def test_the_cli_dry_run_writes_nothing(db, capsys):
    both_accounts(db)
    operator_account.set_active(STRAY, active=False, dry_run=True)
    out = capsys.readouterr().out

    assert "DRY RUN" in out
    assert "Nothing was written." in out
    with Session(db) as session:
        assert session.query(OperatorUser).filter_by(username=STRAY).one().is_active is True
        assert session.query(OperatorSession).count() == 0


def test_the_cli_dry_run_reports_the_same_refusal_the_apply_would(db, capsys):
    make_user(db, CHRIS)
    with pytest.raises(SystemExit) as exit_info:
        operator_account.set_active(CHRIS, active=False, dry_run=True)
    assert "only active operator account" in str(exit_info.value)
    assert "Nothing was changed." in str(exit_info.value)


def test_the_cli_reactivates(db, capsys):
    both_accounts(db)
    operator_account.set_active(STRAY, active=False, dry_run=False)
    capsys.readouterr()
    operator_account.set_active(STRAY, active=True, dry_run=False)
    out = capsys.readouterr().out

    assert f"Reactivated operator account: {STRAY}" in out
    assert "Active operator accounts now: 2." in out
    client = make_client()
    assert sign_in(client, STRAY, OTHER_PASSWORD).status_code == 303


def test_the_cli_is_idempotent_and_says_so(db, capsys):
    both_accounts(db)
    operator_account.set_active(STRAY, active=False, dry_run=False)
    capsys.readouterr()
    operator_account.set_active(STRAY, active=False, dry_run=False)
    out = capsys.readouterr().out

    assert "is already inactive" in out
    assert "Nothing was changed." in out


def test_the_cli_refuses_an_unknown_username(db):
    make_user(db, CHRIS)
    with pytest.raises(SystemExit, match="No operator account exists"):
        operator_account.set_active("nobody@example.com", active=False, dry_run=False)


def test_the_list_shows_the_deactivated_account_rather_than_hiding_it(db, capsys):
    both_accounts(db)
    operator_account.set_active(STRAY, active=False, dry_run=False)
    capsys.readouterr()
    operator_account.list_accounts()
    out = capsys.readouterr().out

    assert f"{CHRIS}  (active)" in out
    assert f"{STRAY}  (INACTIVE)" in out


def test_deactivate_and_reactivate_together_are_refused_by_the_parser(db, monkeypatch, capsys):
    monkeypatch.setattr(
        operator_account.sys, "argv",
        ["operator_account.py", "--username", STRAY, "--deactivate", "--reactivate"],
    )
    monkeypatch.setattr(operator_account, "initialize_database", lambda: None)
    with pytest.raises(SystemExit):
        operator_account.main()
    assert "opposites" in capsys.readouterr().err


def test_no_password_prompt_is_reached_by_the_deactivate_path(db, monkeypatch):
    """The switch must never ask for a password -- it would be asking
    for a secret it has no use for."""
    both_accounts(db)

    def explode(_prompt):
        raise AssertionError("the deactivate path asked for a password")

    monkeypatch.setattr(operator_account.getpass, "getpass", explode)
    monkeypatch.setattr(operator_account, "initialize_database", lambda: None)
    monkeypatch.setattr(
        operator_account.sys, "argv",
        ["operator_account.py", "--username", STRAY, "--deactivate"],
    )
    operator_account.main()
    with Session(db) as session:
        assert session.query(OperatorUser).filter_by(username=STRAY).one().is_active is False
