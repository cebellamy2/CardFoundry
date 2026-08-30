from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import json

from consignor_auth_service import (
    SESSION_LIFETIME,
    authenticate_consignor,
    create_consignor_session,
    destroy_consignor_session,
    hash_password,
    invalidate_consignor_sessions,
    set_consignor_portal_credentials,
    validate_consignor_session,
    verify_password,
)
from models import Base, Consignor, ConsignorCredentialChangeLog, ConsignorSession


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


# --- UX epic item 19, Section 22.5: credential change invalidates ---
# --- open sessions immediately, and writes a narrowly-scoped audit ---
# --- log entry (never a password or its hash).                    ---

def test_invalidate_consignor_sessions_deletes_all_and_returns_count(session):
    consignor = make_consignor(session)
    create_consignor_session(session, consignor.id)
    create_consignor_session(session, consignor.id)
    session.commit()
    deleted = invalidate_consignor_sessions(session, consignor.id)
    session.commit()
    assert deleted == 2
    assert session.query(ConsignorSession).filter_by(consignor_id=consignor.id).count() == 0


def test_invalidate_consignor_sessions_only_touches_that_consignor(session):
    jane = make_consignor(session, name="Jane")
    bob = make_consignor(session, name="Bob")
    create_consignor_session(session, jane.id)
    bob_session = create_consignor_session(session, bob.id)
    session.commit()
    invalidate_consignor_sessions(session, jane.id)
    session.commit()
    remaining = session.query(ConsignorSession).filter_by(consignor_id=bob.id).all()
    assert [s.id for s in remaining] == [bob_session.id]


def test_setting_credentials_invalidates_any_existing_open_session(session):
    consignor = make_consignor(session)
    set_consignor_portal_credentials(session, consignor.id, "jane@example.com", "firstpass")
    session.commit()
    live_token = create_consignor_session(session, consignor.id).token
    session.commit()
    assert validate_consignor_session(session, live_token) is not None

    set_consignor_portal_credentials(session, consignor.id, "jane@example.com", "secondpass")
    session.commit()

    assert validate_consignor_session(session, live_token) is None
    assert session.query(ConsignorSession).filter_by(consignor_id=consignor.id).count() == 0


def test_setting_credentials_writes_an_audit_log_entry(session):
    consignor = make_consignor(session)
    set_consignor_portal_credentials(session, consignor.id, "old@example.com", "firstpass")
    session.commit()
    create_consignor_session(session, consignor.id)
    session.commit()

    set_consignor_portal_credentials(session, consignor.id, "new@example.com", "secondpass")
    session.commit()

    logs = (
        session.query(ConsignorCredentialChangeLog)
        .filter_by(consignor_id=consignor.id)
        .order_by(ConsignorCredentialChangeLog.id)
        .all()
    )
    assert len(logs) == 2
    second = json.loads(logs[1].change_summary)
    assert second["action_type"] == "portal_credentials_set"
    assert second["previous_username"] == "old@example.com"
    assert second["new_username"] == "new@example.com"
    assert second["had_existing_credentials"] is True
    assert second["sessions_invalidated"] == 1


def test_audit_log_never_contains_password_or_hash(session):
    consignor = make_consignor(session)
    set_consignor_portal_credentials(session, consignor.id, "jane@example.com", "a-very-secret-password")
    session.commit()
    log = (
        session.query(ConsignorCredentialChangeLog)
        .filter_by(consignor_id=consignor.id)
        .one()
    )
    assert "a-very-secret-password" not in log.change_summary
    assert consignor.portal_password_hash not in log.change_summary
    assert "password" not in json.loads(log.change_summary)
    assert "password_hash" not in json.loads(log.change_summary)


def test_first_credential_set_shows_had_existing_credentials_false(session):
    consignor = make_consignor(session)
    set_consignor_portal_credentials(session, consignor.id, "jane@example.com", "firstpass")
    session.commit()
    log = (
        session.query(ConsignorCredentialChangeLog)
        .filter_by(consignor_id=consignor.id)
        .one()
    )
    entry = json.loads(log.change_summary)
    assert entry["had_existing_credentials"] is False
    assert entry["previous_username"] is None
    assert entry["sessions_invalidated"] == 0


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
