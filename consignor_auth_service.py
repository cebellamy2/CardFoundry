"""Consignor portal authentication -- password hashing and session
lifecycle for consignor logins.

Deliberately independent of the operator's shared-password gate
(main.require_shared_password / ADMIN_PASSWORD): a bug here can leak a
consignor's own data to another consignor at worst, never grant access
to operator routes, because those routes don't call into any of this.

No new external dependency: password hashing uses stdlib
hashlib.pbkdf2_hmac (salted, OWASP's current PBKDF2-SHA256 iteration
guidance), and sessions are opaque random tokens (stdlib secrets)
looked up in a DB table -- no signed-cookie library needed.

Credential lifecycle is fully manual per the shop owner's choice: an
operator sets a consignor's portal username/password directly (see
main.py's /consignors/{id}/portal-credentials route); there is no
self-service reset flow.
"""

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from models import Consignor, ConsignorCredentialChangeLog, ConsignorSession


PBKDF2_ITERATIONS = 310_000
SESSION_LIFETIME = timedelta(days=30)


def hash_password(password: str) -> tuple[str, str]:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS,
    )
    return digest.hex(), salt


def verify_password(password: str, password_hash: str | None, salt: str | None) -> bool:
    if not password_hash or not salt:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS,
    )
    return hmac.compare_digest(candidate.hex(), password_hash)


def invalidate_consignor_sessions(session: Session, consignor_id: int) -> int:
    """Delete every open session for this consignor. Returns the count
    deleted, for the credential-change audit log below -- not otherwise
    used."""
    deleted = (
        session.query(ConsignorSession)
        .filter(ConsignorSession.consignor_id == consignor_id)
        .delete(synchronize_session=False)
    )
    return deleted


def set_consignor_portal_credentials(
    session: Session, consignor_id: int, username: str, password: str,
) -> Consignor:
    """Operator-driven reset -- always sets both username and password
    together, no partial update. Simplest possible lifecycle for a
    handful of manually-managed consignors.

    UX epic item 19 (Section 22.5, operator-resolved 2026-08-29): a
    changed password must immediately invalidate any session already
    open, rather than letting it ride out the existing 30-day
    ConsignorSession expiry -- previously a real gap, closed here.
    Every call also writes a narrowly-scoped audit record (the one
    exception this item is explicitly authorized to add) that records
    the username change and session-invalidation count, never a
    password or its hash.
    """
    consignor = session.get(Consignor, consignor_id)
    if not consignor:
        raise ValueError("Consignor not found.")
    cleaned_username = str(username or "").strip().lower()
    if not cleaned_username:
        raise ValueError("Portal username is required.")
    if not password:
        raise ValueError("Portal password is required.")
    conflict = session.query(Consignor).filter(
        Consignor.portal_username == cleaned_username,
        Consignor.id != consignor_id,
    ).first()
    if conflict:
        raise ValueError("That portal username is already in use by another consignor.")
    previous_username = consignor.portal_username
    had_existing_credentials = bool(consignor.portal_password_hash)
    password_hash, salt = hash_password(password)
    consignor.portal_username = cleaned_username
    consignor.portal_password_hash = password_hash
    consignor.portal_password_salt = salt
    sessions_invalidated = invalidate_consignor_sessions(session, consignor_id)
    session.add(ConsignorCredentialChangeLog(
        consignor_id=consignor_id,
        change_summary=json.dumps({
            "action_type": "portal_credentials_set",
            "previous_username": previous_username,
            "new_username": cleaned_username,
            "had_existing_credentials": had_existing_credentials,
            "sessions_invalidated": sessions_invalidated,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, sort_keys=True),
    ))
    session.flush()
    return consignor


def authenticate_consignor(session: Session, username: str, password: str) -> Consignor | None:
    cleaned_username = str(username or "").strip().lower()
    if not cleaned_username or not password:
        return None
    consignor = session.query(Consignor).filter(
        Consignor.portal_username == cleaned_username,
    ).first()
    if not consignor or not consignor.is_active:
        return None
    if not verify_password(password, consignor.portal_password_hash, consignor.portal_password_salt):
        return None
    return consignor


def create_consignor_session(session: Session, consignor_id: int) -> ConsignorSession:
    record = ConsignorSession(
        consignor_id=consignor_id,
        token=secrets.token_urlsafe(32),
        expires_at=datetime.now() + SESSION_LIFETIME,
    )
    session.add(record)
    session.flush()
    return record


def validate_consignor_session(session: Session, token: str) -> Consignor | None:
    """Never a no-op, in any environment -- unlike the operator's shared
    password gate (which no-ops when ADMIN_PASSWORD is unset for local
    dev convenience), a third party's session is always checked for
    real."""
    if not token:
        return None
    record = session.query(ConsignorSession).filter(
        ConsignorSession.token == token,
    ).first()
    if not record or record.expires_at < datetime.now():
        return None
    consignor = session.get(Consignor, record.consignor_id)
    if not consignor or not consignor.is_active:
        return None
    return consignor


def destroy_consignor_session(session: Session, token: str) -> None:
    if not token:
        return
    session.query(ConsignorSession).filter(ConsignorSession.token == token).delete()
