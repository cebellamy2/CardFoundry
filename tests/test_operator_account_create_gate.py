"""Creating an operator account is opt-in, behind --create.

The case these pin happened in production on 2026-10-23: a line from
this script's own usage block was pasted and run as-is, and an unknown
username did not fail -- it produced a real, active, privileged login
with a password somebody had typed, and printed what looked like
success. The only way to catch it was reading `operator_users`
afterwards.

So the tests that matter most are the NEGATIVE ones: nothing written,
non-zero exit, and above all no password prompt reached, because
collecting a secret for an account that is not going to be touched is
the one thing this script exists to avoid.

Driven through the real CLI entry point (`main`, with argv) as well as
the service, since the argument parsing is half of what went wrong.
"""
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import operator_account
import operator_auth_service
from models import Base, OperatorUser

SECRET = "a-genuinely-secret-password"
OTHER = "a-second-entirely-different-password"
CHRIS = "cebellamy2@gmail.com"
PLACEHOLDER = "your_username"
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'create_gate.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(operator_account, "engine", engine)
    return engine


@pytest.fixture
def interactive(monkeypatch):
    """A real terminal: isatty true, getpass answering in order."""
    monkeypatch.setattr(operator_account.sys.stdin, "isatty", lambda: True)

    def answer(entries):
        it = iter(entries)
        monkeypatch.setattr(operator_account.getpass, "getpass", lambda _p: next(it))

    return answer


@pytest.fixture
def no_prompt(monkeypatch):
    """Explodes if anything asks for a password. Several tests below
    exist only to prove this is never reached."""
    def explode(_prompt):
        raise AssertionError("a password was asked for on a path that must refuse")

    monkeypatch.setattr(operator_account.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(operator_account.getpass, "getpass", explode)


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr(operator_account, "initialize_database", lambda: None)
    monkeypatch.setattr(operator_account.sys, "argv", ["operator_account.py", *argv])
    operator_account.main()


def seed(engine, username=CHRIS, password=SECRET):
    with Session(engine) as session:
        operator_auth_service.set_operator_credentials(
            session, username, password, allow_create=True,
        )
        session.commit()


# ---------------------------------------------------------------------
# The refusal, and what it costs
# ---------------------------------------------------------------------

def test_an_unknown_username_is_refused_and_writes_nothing(db, no_prompt, monkeypatch):
    seed(db)
    with pytest.raises(SystemExit) as exit_info:
        run_cli(monkeypatch, "--username", PLACEHOLDER)

    assert "no operator account exists" in str(exit_info.value)
    with Session(db) as session:
        assert session.query(OperatorUser).count() == 1
        assert session.query(OperatorUser).filter_by(username=PLACEHOLDER).count() == 0


def test_the_refusal_prints_the_exact_command_to_retry_with(db, no_prompt, monkeypatch):
    seed(db)
    with pytest.raises(SystemExit) as exit_info:
        run_cli(monkeypatch, "--username", PLACEHOLDER)

    message = str(exit_info.value)
    assert f"operator_account.py --username {PLACEHOLDER} --create" in message
    assert "--list" in message


def test_no_password_is_asked_for_before_the_refusal(db, no_prompt, monkeypatch):
    """`no_prompt` turns any getpass call into a failure. If this passes,
    a mistyped username never costs the operator a typed secret."""
    seed(db)
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "--username", "chirs@example.com")


def test_the_refusal_exits_non_zero_as_a_real_process(tmp_path):
    """A subprocess, because the exit status is what a script or a
    scrollback actually sees."""
    result = subprocess.run(
        [sys.executable, "operator_account.py", "--username", PLACEHOLDER],
        input="", capture_output=True, text=True, cwd=REPO,
        env={"PATH": "/usr/bin:/bin",
             "DATABASE_URL": f"sqlite:///{tmp_path / 'exit.db'}"},
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "no operator account exists" in combined
    assert f"--username {PLACEHOLDER} --create" in combined


# ---------------------------------------------------------------------
# --create: the break-glass still works
# ---------------------------------------------------------------------

def test_create_works_on_a_completely_empty_table(db, interactive, capsys, monkeypatch):
    """The scenario the script exists for: nobody can get in, and there
    may be no rows at all."""
    interactive([SECRET, SECRET])
    run_cli(monkeypatch, "--username", CHRIS, "--create")
    out = capsys.readouterr().out

    assert f"Created operator account: {CHRIS}" in out
    assert SECRET not in out
    with Session(db) as session:
        user = session.query(OperatorUser).one()
        assert user.username == CHRIS
        assert user.is_active is True
        assert operator_auth_service.verify_password(
            SECRET, user.password_hash, user.password_salt,
        )


def test_create_normalises_the_username_the_same_way(db, interactive, monkeypatch):
    interactive([SECRET, SECRET])
    run_cli(monkeypatch, "--username", "CeBellamy2@Gmail.COM", "--create")
    with Session(db) as session:
        assert session.query(OperatorUser).one().username == CHRIS


def test_create_on_an_existing_username_is_refused(db, no_prompt, monkeypatch):
    seed(db)
    with pytest.raises(SystemExit) as exit_info:
        run_cli(monkeypatch, "--username", CHRIS, "--create")

    message = str(exit_info.value)
    assert "already exists" in message
    assert "without --create" in message


def test_a_refused_create_does_not_touch_the_existing_password(db, no_prompt, monkeypatch):
    seed(db)
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "--username", CHRIS, "--create")

    with Session(db) as session:
        user = session.query(OperatorUser).filter_by(username=CHRIS).one()
        assert operator_auth_service.verify_password(
            SECRET, user.password_hash, user.password_salt,
        )


def test_resetting_an_existing_account_is_unchanged(db, interactive, capsys, monkeypatch):
    seed(db)
    interactive([OTHER, OTHER])
    run_cli(monkeypatch, "--username", CHRIS)
    out = capsys.readouterr().out

    assert f"Reset operator account: {CHRIS}" in out
    assert "signed out" in out
    assert OTHER not in out
    with Session(db) as session:
        user = session.query(OperatorUser).one()
        assert operator_auth_service.verify_password(
            OTHER, user.password_hash, user.password_salt,
        )


def test_create_cannot_be_combined_with_the_switches_that_need_a_row(db, monkeypatch, capsys):
    for flag in ("--deactivate", "--reactivate", "--unlock-only"):
        with pytest.raises(SystemExit):
            run_cli(monkeypatch, "--username", CHRIS, "--create", flag)
        assert "cannot be combined" in capsys.readouterr().err


# ---------------------------------------------------------------------
# The service, which is where the rule lives
# ---------------------------------------------------------------------

def test_the_service_refuses_an_unknown_username_by_default(db):
    with Session(db) as session:
        with pytest.raises(operator_auth_service.UnknownOperatorUser):
            operator_auth_service.set_operator_credentials(session, PLACEHOLDER, SECRET)
        session.rollback()
    with Session(db) as session:
        assert session.query(OperatorUser).count() == 0


def test_the_service_refuses_a_create_over_an_existing_account(db):
    seed(db)
    with Session(db) as session:
        with pytest.raises(operator_auth_service.OperatorUserExists):
            operator_auth_service.set_operator_credentials(
                session, CHRIS, OTHER, allow_create=True,
            )
        session.rollback()


def test_both_refusals_are_value_errors(db):
    """Subclassed so existing `except ValueError` callers keep working."""
    assert issubclass(operator_auth_service.UnknownOperatorUser, ValueError)
    assert issubclass(operator_auth_service.OperatorUserExists, ValueError)


def test_the_service_refuses_before_hashing_anything(db, monkeypatch):
    """Not a micro-optimisation: hashing is the only place a password is
    handled, and a refused call must not handle one at all."""
    def explode(_password):
        raise AssertionError("the refused path hashed a password")

    monkeypatch.setattr(operator_auth_service, "hash_password", explode)
    with Session(db) as session:
        with pytest.raises(operator_auth_service.UnknownOperatorUser):
            operator_auth_service.set_operator_credentials(session, PLACEHOLDER, SECRET)


# ---------------------------------------------------------------------
# --list counts: the cheap check that a reset reset something
# ---------------------------------------------------------------------

def test_list_prints_the_total_and_the_active_count(db, capsys):
    seed(db)
    seed(db, PLACEHOLDER, OTHER)
    with Session(db) as session:
        operator_auth_service.set_operator_active(session, PLACEHOLDER, active=False)
        session.commit()
    capsys.readouterr()

    operator_account.list_accounts()
    out = capsys.readouterr().out

    assert "2 operator account(s), 1 active." in out
    assert f"{CHRIS}  (active)" in out
    assert f"{PLACEHOLDER}  (INACTIVE)" in out


def test_list_on_an_empty_table_counts_zero(db, capsys):
    operator_account.list_accounts()
    out = capsys.readouterr().out

    assert "No operator accounts exist yet." in out
    assert "0 operator account(s), 0 active." in out


def test_a_create_reports_the_new_count(db, interactive, capsys, monkeypatch):
    """The tell that would have caught 2026-10-23 at the terminal:
    '2 operator account(s)' after a run that should have touched one."""
    seed(db)
    interactive([OTHER, OTHER])
    capsys.readouterr()
    run_cli(monkeypatch, "--username", "second@example.com", "--create")

    assert "2 operator account(s), 2 active." in capsys.readouterr().out


# ---------------------------------------------------------------------
# The docstring that got pasted
# ---------------------------------------------------------------------

def test_the_usage_block_contains_no_runnable_placeholder_username():
    """Every example username is <in angle brackets>. A placeholder that
    is valid shell is what made the original mistake possible."""
    doc = operator_account.__doc__
    # Only the runnable lines -- prose may say "--username" mid-sentence.
    commands = [
        line for line in doc.splitlines()
        if "operator_account.py" in line and "--username" in line
    ]
    assert commands
    for line in commands:
        name = line.split("--username", 1)[1].strip().split()[0]
        assert name.startswith("<") and name.endswith(">"), line
    # And no example address anywhere in the docstring, runnable or not:
    # the line that got pasted was itself just an example.
    assert "@example.com" not in doc
    assert "@gmail.com" not in doc
