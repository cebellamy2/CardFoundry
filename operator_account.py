"""Reset an operator account's password, create one, or clear a lockout.

Run it in the production container:

    railway ssh --service CardFoundry
    cd /app && PYTHONPATH=/app /opt/venv/bin/python operator_account.py \
        --username <the-real-username>

EVERY USERNAME BELOW IS WRITTEN AS <a-placeholder-in-angle-brackets>,
never as a plausible-looking address, because on 2026-10-23 a line from
this very docstring was pasted and run as-is and the script cheerfully
created a live operator account called `your_username`. A placeholder
that is valid shell is a trap; one in angle brackets is a syntax error
in most shells and an obvious blank everywhere else.

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

AN UNKNOWN USERNAME IS REFUSED, NOT CREATED. Creating is opt-in, behind
--create, because the old create-on-unknown behaviour turned a typo into
a real privileged login and printed what looked like success. The
refusal prints the exact command to retry with, so the extra step costs
one paste -- which matters, because this is the break-glass and the
moment it is needed is the worst moment to be fighting an argument
parser. --create works with an EMPTY operator_users table; it is the
bootstrap.

Every password run:
  * resets the password of an existing user, or creates one with --create
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
    /opt/venv/bin/python operator_account.py --list
    /opt/venv/bin/python operator_account.py --username <username>
    /opt/venv/bin/python operator_account.py --username <username> --create
    /opt/venv/bin/python operator_account.py --username <username> --unlock-only
    /opt/venv/bin/python operator_account.py --username <username> --deactivate
    /opt/venv/bin/python operator_account.py --username <username> --reactivate
    /opt/venv/bin/python operator_account.py --username <username> --deactivate --dry-run
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
            print("0 operator account(s), 0 active.")
            return
        for user in users:
            state = "active" if user.is_active else "INACTIVE"
            if operator_auth_service.is_locked(user):
                state += f", LOCKED until {user.locked_until:%Y-%m-%d %H:%M}"
            print(f"{user.username}  ({state})")
        # The count is the cheap check that a reset reset something
        # rather than adding something: "2 accounts" after a run that
        # should have touched one is the whole tell.
        active = operator_auth_service.active_operator_count(session)
        print(f"{len(users)} operator account(s), {active} active.")


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


def set_credentials(username: str, *, create: bool = False) -> None:
    """Resolve whether this is a reset or a create BEFORE prompting.

    The service refuses the wrong one either way -- it is the write path,
    so the rule lives there -- but asking it first means a mistyped name
    never reaches the password prompt. Collecting a secret for an account
    that is not going to be touched is the one thing this script exists
    to avoid.
    """
    cleaned = operator_auth_service.normalize_username(username)
    if not cleaned:
        raise SystemExit("Refusing: --username is required and cannot be blank.")
    with Session(engine) as session:
        exists = operator_auth_service.get_operator_user(session, cleaned) is not None
    if not exists and not create:
        raise SystemExit(
            f"Refusing: no operator account exists for {cleaned!r}, and creating "
            "one is not the default.\n"
            "Nothing was changed and no password was asked for.\n"
            "If you meant to CREATE it, run:\n"
            f"    /opt/venv/bin/python operator_account.py --username {cleaned} --create\n"
            "If you meant to reset an existing account, check the spelling with "
            "--list."
        )
    if exists and create:
        raise SystemExit(
            f"Refusing: an operator account already exists for {cleaned!r}.\n"
            "Nothing was changed and no password was asked for.\n"
            "To reset its password, run the same command without --create."
        )

    password = _prompt_for_password()
    with Session(engine) as session:
        try:
            _, created = operator_auth_service.set_operator_credentials(
                session, cleaned, password, allow_create=create,
            )
        except ValueError as exc:
            # Only reachable if the row changed under us between the
            # check above and here. The service is the authority.
            raise SystemExit(f"{exc} Nothing was changed.")
        session.commit()
        total = session.query(OperatorUser).count()
        active = operator_auth_service.active_operator_count(session)
    # The username and the outcome. Nothing else.
    print(f"{'Created' if created else 'Reset'} operator account: {cleaned}")
    print("Any lockout was cleared and every previously open session was signed out.")
    print(f"{total} operator account(s), {active} active.")


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
    parser.add_argument(
        "--create", action="store_true",
        help="Create the account. Required for a username that does not exist yet.",
    )
    args = parser.parse_args()
    if args.deactivate and args.reactivate:
        parser.error("--deactivate and --reactivate are opposites; pass one.")
    if args.create and (args.deactivate or args.reactivate or args.unlock_only):
        parser.error(
            "--create sets a password on a new account; it cannot be combined "
            "with --deactivate, --reactivate or --unlock-only."
        )

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
    set_credentials(args.username, create=args.create)


if __name__ == "__main__":
    main()
