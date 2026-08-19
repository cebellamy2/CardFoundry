from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from consignor_auth_service import (
    SESSION_LIFETIME,
    authenticate_consignor,
    create_consignor_session,
    destroy_consignor_session,
    hash_password,
    set_consignor_portal_credentials,
    validate_consignor_session,
    verify_password,
)
from models import Base, Consignor, ConsignorSession


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'consignor_auth.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def make_consignor(session, name="Jane", is_active=True):
    consignor = Consignor(name=name, is_active=is_active)
    session.add(consignor)
    session.flush()
    return consignor


# --- hash_password / verify_password ---

def test_hash_and_verify_round_trip():
    password_hash, salt = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", password_hash, salt) is True


def test_verify_rejects_wrong_password():
    password_hash, salt = hash_password("correct horse battery staple")
    assert verify_password("wrong password", password_hash, salt) is False


def test_hash_password_uses_a_fresh_salt_each_time():
    hash_a, salt_a = hash_password("same password")
    hash_b, salt_b = hash_password("same password")
    assert salt_a != salt_b
    assert hash_a != hash_b


def test_verify_rejects_missing_hash_or_salt():
    assert verify_password("anything", None, None) is False
    assert verify_password("anything", "somehash", None) is False
    assert verify_password("anything", None, "somesalt") is False


# --- set_consignor_portal_credentials ---

def test_set_credentials_happy_path(session):
    consignor = make_consignor(session)
    set_consignor_portal_credentials(session, consignor.id, "Jane@Example.com", "secretpw")
    session.commit()
    assert consignor.portal_username == "jane@example.com"
    assert consignor.portal_password_hash
    assert consignor.portal_password_salt


def test_set_credentials_rejects_missing_consignor(session):
    with pytest.raises(ValueError, match="Consignor not found"):
        set_consignor_portal_credentials(session, 999, "jane@example.com", "pw")


def test_set_credentials_rejects_blank_username(session):
    consignor = make_consignor(session)
    with pytest.raises(ValueError, match="username is required"):
        set_consignor_portal_credentials(session, consignor.id, "   ", "pw")


def test_set_credentials_rejects_blank_password(session):
    consignor = make_consignor(session)
    with pytest.raises(ValueError, match="password is required"):
        set_consignor_portal_credentials(session, consignor.id, "jane@example.com", "")


def test_set_credentials_rejects_duplicate_username(session):
    jane = make_consignor(session, name="Jane")
    bob = make_consignor(session, name="Bob")
    set_consignor_portal_credentials(session, jane.id, "shared@example.com", "pw")
    session.commit()
    with pytest.raises(ValueError, match="already in use"):
        set_consignor_portal_credentials(session, bob.id, "shared@example.com", "pw2")


def test_set_credentials_allows_same_consignor_to_keep_own_username(session):
    consignor = make_consignor(session)
    set_consignor_portal_credentials(session, consignor.id, "jane@example.com", "pw")
    session.commit()
    set_consignor_portal_credentials(session, consignor.id, "jane@example.com", "newpw")
    session.commit()
    assert consignor.portal_username == "jane@example.com"


# --- authenticate_consignor ---

def test_authenticate_happy_path(session):
    consignor = make_consignor(session)
    set_consignor_portal_credentials(session, consignor.id, "jane@example.com", "secretpw")
    session.commit()
    result = authenticate_consignor(session, "jane@example.com", "secretpw")
    assert result is not None
    assert result.id == consignor.id


def test_authenticate_is_case_insensitive_on_username(session):
    consignor = make_consignor(session)
    set_consignor_portal_credentials(session, consignor.id, "jane@example.com", "secretpw")
    session.commit()
    result = authenticate_consignor(session, "Jane@Example.com", "secretpw")
    assert result is not None
    assert result.id == consignor.id


def test_authenticate_rejects_wrong_password(session):
    consignor = make_consignor(session)
    set_consignor_portal_credentials(session, consignor.id, "jane@example.com", "secretpw")
    session.commit()
    assert authenticate_consignor(session, "jane@example.com", "wrongpw") is None


def test_authenticate_rejects_unknown_username(session):
    assert authenticate_consignor(session, "nobody@example.com", "pw") is None


def test_authenticate_rejects_inactive_consignor(session):
    consignor = make_consignor(session, is_active=False)
    set_consignor_portal_credentials(session, consignor.id, "jane@example.com", "secretpw")
    session.commit()
    assert authenticate_consignor(session, "jane@example.com", "secretpw") is None


def test_authenticate_rejects_blank_credentials(session):
    assert authenticate_consignor(session, "", "") is None
    assert authenticate_consignor(session, "jane@example.com", "") is None


# --- create_consignor_session / validate_consignor_session / destroy_consignor_session ---

def test_create_session_sets_expiry_thirty_days_out(session):
    consignor = make_consignor(session)
    session.commit()
    record = create_consignor_session(session, consignor.id)
    session.commit()
    delta = record.expires_at - datetime.now()
    assert timedelta(days=29, hours=23) < delta <= SESSION_LIFETIME


def test_validate_session_happy_path(session):
    consignor = make_consignor(session)
    session.commit()
    record = create_consignor_session(session, consignor.id)
    session.commit()
    result = validate_consignor_session(session, record.token)
    assert result is not None
    assert result.id == consignor.id


def test_validate_session_rejects_unknown_token(session):
    assert validate_consignor_session(session, "not-a-real-token") is None


def test_validate_session_rejects_blank_token(session):
    assert validate_consignor_session(session, "") is None


def test_validate_session_rejects_expired_token(session):
    consignor = make_consignor(session)
    session.flush()
    expired = ConsignorSession(
        consignor_id=consignor.id, token="expired-token",
        expires_at=datetime.now() - timedelta(days=1),
    )
    session.add(expired)
    session.commit()
    assert validate_consignor_session(session, "expired-token") is None


def test_validate_session_rejects_inactive_consignor(session):
    consignor = make_consignor(session)
    session.commit()
    record = create_consignor_session(session, consignor.id)
    session.commit()
    consignor.is_active = False
    session.commit()
    assert validate_consignor_session(session, record.token) is None


def test_destroy_session_removes_it(session):
    consignor = make_consignor(session)
    session.commit()
    record = create_consignor_session(session, consignor.id)
    session.commit()
    destroy_consignor_session(session, record.token)
    session.commit()
    assert validate_consignor_session(session, record.token) is None


def test_destroy_session_is_a_noop_for_unknown_token(session):
    destroy_consignor_session(session, "never-existed")
    session.commit()
