"""Create or reset an operator account, and clear its lockout.

Run it in the production container:

    railway ssh --service CardFoundry
    cd /app && PYTHONPATH=/app /opt/venv/bin/python operator_account.py \
        --username you@example.com

USE /opt/venv/bin/python, NOT PLAIN `python`. v1.197.0 shipped this
instruction wrong. Railway's Nixpacks image puts the application's
dependencies in a virtualenv at /opt/venv and activates it for the
service's START COMMAND -- but a `railway ssh` shell is not that
environment. There, `python` resolves to the bare Nix interpreter with no
fastapi and no sqlalchemy, and this script dies on its first import.
Verified in the live container, 2026-09-24.

THE PASSWORD IS NEVER AN ARGUMENT AND NEVER AN ENVIRONMENT VARIABLE.
It is read twice from a hidden interactive prompt (getpass) and
confirmed. A password passed on a command line ends up in the shell
history, in `ps` output and in Railway's own logs; a password in the
environment ends up in every child process and in a crash dump. This
script prints the username and whether it worked, and nothing else --
never the password, never the hash, never the salt, never a token.

IT IS THE BREAK-GLASS. Since Slice 2 Stage B (v2.0.0) there is no
shared password and no other way back in: if every operator credential is
lost, this is the recovery path -- SSH into the container and reset the
account. That is why it lives here rather than behind a route; a route to
reset a password is only reachable by someone who can already get in.

Every password run:
  * creates the user if there is none, or resets the password if there is
  * clears any standing lockout immediately
  * invalidates every session already open for that user, so a reset is
    a real response to a lost or shared device and not just a new password

DEACTIVATE RATHER THAN DELETE. --deactivate switches an account off and
signs it out; the row stays and --reactivate is the way back, so an
account switched off by mistake costs nothing. There is deliberately no
delete: the row is the only record that the account ever existed.
Deactivating the LAST active account is refused, because since v2.0.0 a
named account is the only way a human gets in. Note that a later
--username password run reactivates the account it resets -- that is the
break-glass behaving as intended, not a way around the switch.

Usage (inside the container, always via /opt/venv/bin/python):
    /opt/venv/bin/python operator_account.py --username you@example.com
    /opt/venv/bin/python operator_account.py --username you@example.com --unlock-only
    /opt/venv/bin/python operator_account.py --username them@example.com --deactivate
    /opt/venv/bin/python operator_account.py --username them@example.com --reactivate
    /opt/venv/bin/python operator_account.py --username them@example.com --deactivate --dry-run
    /opt/venv/bin/python operator_account.py --list
"""

import argparse
import getpass
import sys

from sqlalchemy.orm import Session

import operator_auth_service
from database import engine, initialize_database
from models import OperatorUser

MINIMUM_PASSWORD_LENGTH = 12


def _prompt_for_password() -> str:
    """Hidden, twice, no echo, no default, no argument fallback.

    getpass falls back to a VISIBLE prompt (with a warning) when the
    terminal cannot hide input -- a piped or non-tty stdin, for example.
    Refusing outright is the right call for a password entry: silently
    echoing it into a Railway SSH scrollback is the exact failure this
    whole script exists to avoid.
    """
    if not sys.stdin.isatty():
        raise SystemExit(
            "Refusing to read a password from a non-interactive stdin. "
            "Run this in an interactive shell (railway ssh), so the "
            "prompt can hide what you type."
        )
    first = getpass.getpass("New password (hidden): ")
    if len(first) < MINIMUM_PASSWORD_LENGTH:
        raise SystemExit(
            f"Refusing: the password must be at least {MINIMUM_PASSWORD_LENGTH} "
            "characters. Nothing was changed."
        )
    second = getpass.getpass("Confirm password (hidden): ")
    if first != second:
        raise SystemExit("Refusing: the two entries did not match. Nothing was changed.")
    return first


def list_accounts() -> None:
    with Session(engine) as session:
        users = session.query(OperatorUser).order_by(OperatorUser.username).all()
        if not users:
            print("No operator accounts exist yet.")
            return
        for user in users:
            state = "active" if user.is_active else "INACTIVE"
            if operator_auth_service.is_locked(user):
                state += f", LOCKED until {user.locked_until:%Y-%m-%d %H:%M}"
            print(f"{user.username}  ({state})")


def unlock(username: str) -> None:
    with Session(engine) as session:
        user = operator_auth_service.get_operator_user(session, username)
        if not user:
            raise SystemExit(f"No operator account exists for {username!r}. Nothing was changed.")
        operator_auth_service.clear_lockout(user)
        session.commit()
    print(f"Lockout cleared for {operator_auth_service.normalize_username(username)}.")


def set_active(username: str, *, active: bool, dry_run: bool) -> None:
    """A thin flag over the service call -- the rule about the last
    active account, the session sign-out and the log line all live in
    operator_auth_service, where the rest of the account lifecycle is.

    The dry run is the real call, rolled back: it reports exactly what
    the apply would do, including the refusals, because it runs the
    same code rather than a second opinion about it.
    """
    word = "Reactivated" if active else "Deactivated"
    with Session(engine) as session:
        try:
            user, changed = operator_auth_service.set_operator_active(
                session, username, active=active,
            )
        except ValueError as exc:
            raise SystemExit(f"{exc} Nothing was changed.")
        if not changed:
            session.rollback()
            state = "active" if active else "inactive"
            print(f"{user.username} is already {state}. Nothing was changed.")
            return
        remaining = operator_auth_service.active_operator_count(session)
        if dry_run:
            session.rollback()
            print(f"DRY RUN -- would have {word.lower()} {user.username}.")
            print(f"Active operator accounts afterwards would be: {remaining}.")
            print("Nothing was written.")
            return
        session.commit()
        print(f"{word} operator account: {user.username}")
        if not active:
            print("Every session open for that account was signed out.")
        print(f"Active operator accounts now: {remaining}.")


def set_credentials(username: str) -> None:
    cleaned = operator_auth_service.normalize_username(username)
    if not cleaned:
        raise SystemExit("Refusing: --username is required and cannot be blank.")
    password = _prompt_for_password()
    with Session(engine) as session:
        _, created = operator_auth_service.set_operator_credentials(
            session, cleaned, password,
        )
        session.commit()
    # The username and the outcome. Nothing else.
    print(f"{'Created' if created else 'Reset'} operator account: {cleaned}")
    print("Any lockout was cleared and every previously open session was signed out.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--username", help="Operator username (an email address is fine).")
    parser.add_argument(
        "--unlock-only", action="store_true",
        help="Clear a lockout without touching the password.",
    )
    parser.add_argument("--list", action="store_true", help="List operator accounts.")
    parser.add_argument(
        "--deactivate", action="store_true",
        help="Switch the account off and sign it out. Reversible with --reactivate.",
    )
    parser.add_argument(
        "--reactivate", action="store_true",
        help="Switch a deactivated account back on.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="With --deactivate/--reactivate: report what would change, write nothing.",
    )
    args = parser.parse_args()
    if args.deactivate and args.reactivate:
        parser.error("--deactivate and --reactivate are opposites; pass one.")

    # Safe on an existing database: create_all only adds missing tables.
    initialize_database()

    if args.list:
        list_accounts()
        return
    if not args.username:
        parser.error("--username is required (or use --list).")
    if args.unlock_only:
        unlock(args.username)
        return
    if args.deactivate or args.reactivate:
        set_active(args.username, active=args.reactivate, dry_run=args.dry_run)
        return
    set_credentials(args.username)


if __name__ == "__main__":
    main()
