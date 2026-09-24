"""Operator authentication -- password hashing, session lifecycle and
failed-attempt lockout for named operator logins.

A DELIBERATE COPY of consignor_auth_service, not a shared module
(operator decision, 2026-09-24). The two auth systems must stay
independently breakable: a bug in the consignor portal must never be
able to grant operator access, and a bug here must never be able to leak
a consignor's data. Sharing the code would couple exactly the two things
the existing design keeps apart -- consignor_auth_service's own module
docstring says so. The duplication is the point; if one of these needs
to change, the other should have to be changed deliberately.

No new external dependency, same as the consignor side: hashing is
stdlib hashlib.pbkdf2_hmac (salted PBKDF2-SHA256 at OWASP's current
iteration guidance) and sessions are opaque stdlib `secrets` tokens
looked up in a DB table -- no signed-cookie library, no stable signing
key to lose, and individual sessions can be revoked.

THIS MODULE ADDS AUTHENTICATION, IT DOES NOT REMOVE ANY. The shared
ADMIN_PASSWORD Basic gate in main.require_shared_password is untouched
by this slice and still protects every route; a valid operator session
is simply a second way through it. Retiring the shared password is a
separate, later change -- which is also why the bootstrap script
(operator_account.py) exists now rather than then: it is the
permanent break-glass for when Basic is gone.

LOCKOUT is per user row, 5 consecutive failures buying a 15-minute lock
that clears itself. During a lock, new sign-ins are refused even with
the correct password; sessions already issued are deliberately NOT
touched, so locking an account cannot sign the operator out of the
device he is holding.
"""

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from models import OperatorSession, OperatorUser

logger = logging.getLogger("cardfoundry")

PBKDF2_ITERATIONS = 310_000
SESSION_LIFETIME = timedelta(days=30)

# Operator decision (2026-09-24): 5 failures, 15 minutes, per username,
# self-clearing, and clearable immediately by the bootstrap script.
LOCKOUT_THRESHOLD = 5
LOCKOUT_DURATION = timedelta(minutes=15)

# authenticate_operator's second return value. Callers must render the
# SAME generic message for every non-"ok" outcome -- these values exist
# for the log line, never for the page.
OUTCOME_OK = "ok"
OUTCOME_UNKNOWN_USER = "unknown_user"
OUTCOME_BAD_PASSWORD = "bad_password"
OUTCOME_LOCKED = "locked"
# The attempt that TRIPPED the lock, as distinct from one refused by
# an already-standing lock. Same generic page, different log line.
OUTCOME_JUST_LOCKED = "just_locked"
OUTCOME_INACTIVE = "inactive"


def normalize_username(username: str | None) -> str:
    """Usernames are compared casefolded and trimmed, so the operator
    cannot lock himself out by capitalising his own email address on a
    phone keyboard."""
    return str(username or "").strip().lower()


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


def invalidate_operator_sessions(session: Session, operator_user_id: int) -> int:
    """Delete every open session for this user. Returns the count, for
    the caller's log line."""
    return (
        session.query(OperatorSession)
        .filter(OperatorSession.operator_user_id == operator_user_id)
        .delete(synchronize_session=False)
    )


def get_operator_user(session: Session, username: str) -> OperatorUser | None:
    cleaned = normalize_username(username)
    if not cleaned:
        return None
    return session.query(OperatorUser).filter(
        OperatorUser.username == cleaned,
    ).first()


def is_locked(user: OperatorUser, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return bool(user.locked_until and user.locked_until > now)


def clear_lockout(user: OperatorUser) -> None:
    user.failed_login_count = 0
    user.locked_until = None


def set_operator_credentials(
    session: Session, username: str, password: str,
) -> tuple[OperatorUser, bool]:
    """Create the user, or reset an existing one's password. Always
    clears any lockout, and always invalidates every open session for
    that user -- a password reset that left old sessions alive would
    make the reset useless as a response to a lost device.

    Returns (user, created).
    """
    cleaned = normalize_username(username)
    if not cleaned:
        raise ValueError("A username is required.")
    if not password:
        raise ValueError("A password is required.")
    password_hash, salt = hash_password(password)
    user = get_operator_user(session, cleaned)
    created = user is None
    if user is None:
        user = OperatorUser(username=cleaned)
        session.add(user)
    user.password_hash = password_hash
    user.password_salt = salt
    user.is_active = True
    user.updated_at = datetime.now()
    clear_lockout(user)
    session.flush()
    invalidated = invalidate_operator_sessions(session, user.id)
    session.flush()
    logger.info(
        "operator auth: credentials %s for %s (sessions invalidated: %d)",
        "created" if created else "reset", cleaned, invalidated,
    )
    return user, created


def authenticate_operator(
    session: Session, username: str, password: str,
) -> tuple[OperatorUser | None, str]:
    """Returns (user, outcome). A user is returned ONLY on OUTCOME_OK.

    Every failure path costs the caller the same generic message; the
    outcome string is for the `cardfoundry` log, so a real lockout is
    visible to the operator without the login page ever revealing which
    of the five things went wrong.
    """
    cleaned = normalize_username(username)
    user = get_operator_user(session, cleaned)
    if user is None:
        # No row means nothing to lock and nothing to count. An attacker
        # guessing usernames therefore cannot fill a table, and cannot
        # tell a real name from a made-up one either way.
        return None, OUTCOME_UNKNOWN_USER
    if is_locked(user):
        # Checked BEFORE the password so a correct password during a
        # lock is still refused, and so a locked account costs no
        # PBKDF2 work.
        return None, OUTCOME_LOCKED
    if user.locked_until:
        # The lock has expired; clear it now rather than leaving a stale
        # timestamp to be re-read on every future attempt.
        clear_lockout(user)
    if not user.is_active:
        return None, OUTCOME_INACTIVE
    if not password or not verify_password(password, user.password_hash, user.password_salt):
        user.failed_login_count = (user.failed_login_count or 0) + 1
        if user.failed_login_count >= LOCKOUT_THRESHOLD:
            user.locked_until = datetime.now() + LOCKOUT_DURATION
            # Reset the counter with the lock so that waiting out the
            # 15 minutes restores a full allowance, instead of leaving
            # the account one attempt from being locked forever.
            user.failed_login_count = 0
            session.flush()
            return None, OUTCOME_JUST_LOCKED
        session.flush()
        return None, OUTCOME_BAD_PASSWORD
    clear_lockout(user)
    session.flush()
    return user, OUTCOME_OK


def create_operator_session(session: Session, operator_user_id: int) -> OperatorSession:
    record = OperatorSession(
        operator_user_id=operator_user_id,
        token=secrets.token_urlsafe(32),
        expires_at=datetime.now() + SESSION_LIFETIME,
    )
    session.add(record)
    session.flush()
    return record


def validate_operator_session(session: Session, token: str) -> OperatorUser | None:
    """Never a no-op in any environment, unlike the shared-password gate
    this sits in front of. An expired or unknown token is simply not a
    session; the caller then falls through to whatever else the gate
    allows, which in this slice is still the Basic challenge."""
    if not token:
        return None
    record = session.query(OperatorSession).filter(
        OperatorSession.token == token,
    ).first()
    if not record or record.expires_at < datetime.now():
        return None
    user = session.get(OperatorUser, record.operator_user_id)
    if not user or not user.is_active:
        return None
    return user


def destroy_operator_session(session: Session, token: str) -> None:
    if not token:
        return
    session.query(OperatorSession).filter(OperatorSession.token == token).delete()


def log_sign_in_attempt(username: str, outcome: str) -> None:
    """USERNAME AND OUTCOME ONLY. Never the password, never the token,
    never the request headers -- this is the auth path and everything
    else in scope here is a secret by assumption."""
    cleaned = normalize_username(username) or "(blank)"
    if outcome == OUTCOME_OK:
        logger.info("operator auth: sign-in succeeded for %s", cleaned)
    elif outcome == OUTCOME_JUST_LOCKED:
        logger.warning(
            "operator auth: %s locked after %d consecutive failed sign-ins; "
            "new sign-ins refused for %d minutes",
            cleaned, LOCKOUT_THRESHOLD, int(LOCKOUT_DURATION.total_seconds() // 60),
        )
    elif outcome == OUTCOME_LOCKED:
        logger.warning(
            "operator auth: sign-in refused for %s -- account is locked", cleaned,
        )
    else:
        logger.warning(
            "operator auth: sign-in failed for %s (%s)", cleaned, outcome,
        )
