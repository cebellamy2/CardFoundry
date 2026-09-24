"""operator_account.py -- the bootstrap/reset CLI.

This script is the only thing that ever handles an operator's password
in plaintext, and it is the permanent break-glass for when the shared
password is retired. Both of those make its OUTPUT the thing to pin: it
must print the username and whether it worked, and nothing else, ever.
"""
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import operator_account
import operator_auth_service
from models import Base, OperatorUser

SECRET = "a-genuinely-secret-password"
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'operator_account.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(operator_account, "engine", engine)
    return engine


@pytest.fixture
def interactive(monkeypatch):
    """Stand in for a real terminal: isatty true, getpass answering."""
    monkeypatch.setattr(operator_account.sys.stdin, "isatty", lambda: True)

    def answer(entries):
        it = iter(entries)
        monkeypatch.setattr(operator_account.getpass, "getpass", lambda _prompt: next(it))

    return answer


def test_it_creates_an_account_and_prints_the_username_not_the_password(db, interactive, capsys):
    interactive([SECRET, SECRET])
    operator_account.set_credentials("CeBellamy2@Gmail.com")
    out = capsys.readouterr().out
    assert "Created operator account: cebellamy2@gmail.com" in out
    assert SECRET not in out
    with Session(db) as session:
        user = session.query(OperatorUser).one()
        assert user.username == "cebellamy2@gmail.com"
        assert SECRET not in (user.password_hash + user.password_salt)
        assert operator_auth_service.verify_password(
            SECRET, user.password_hash, user.password_salt,
        )


def test_the_printed_output_contains_no_hash_or_salt_either(db, interactive, capsys):
    interactive([SECRET, SECRET])
    operator_account.set_credentials("cebellamy2@gmail.com")
    out = capsys.readouterr().out
    with Session(db) as session:
        user = session.query(OperatorUser).one()
    assert user.password_hash not in out
    assert user.password_salt not in out


def test_a_second_run_resets_rather_than_creating(db, interactive, capsys):
    interactive([SECRET, SECRET])
    operator_account.set_credentials("cebellamy2@gmail.com")
    capsys.readouterr()
    interactive(["a-replacement-password", "a-replacement-password"])
    operator_account.set_credentials("cebellamy2@gmail.com")
    assert "Reset operator account: cebellamy2@gmail.com" in capsys.readouterr().out
    with Session(db) as session:
        assert session.query(OperatorUser).count() == 1


def test_mismatched_entries_change_nothing(db, interactive, capsys):
    interactive([SECRET, "typed-it-differently"])
    with pytest.raises(SystemExit) as exit_info:
        operator_account.set_credentials("cebellamy2@gmail.com")
    assert "did not match" in str(exit_info.value)
    assert SECRET not in str(exit_info.value)
    with Session(db) as session:
        assert session.query(OperatorUser).count() == 0


def test_a_short_password_is_refused_before_the_second_prompt(db, interactive):
    interactive(["short"])  # only ONE entry available: a second call would raise
    with pytest.raises(SystemExit) as exit_info:
        operator_account.set_credentials("cebellamy2@gmail.com")
    assert "at least 12" in str(exit_info.value)
    with Session(db) as session:
        assert session.query(OperatorUser).count() == 0


def test_a_blank_username_is_refused(db, interactive):
    interactive([SECRET, SECRET])
    with pytest.raises(SystemExit):
        operator_account.set_credentials("   ")


def test_unlock_only_clears_a_lockout_without_touching_the_password(db, interactive):
    interactive([SECRET, SECRET])
    operator_account.set_credentials("cebellamy2@gmail.com")
    with Session(db) as session:
        user = session.query(OperatorUser).one()
        user.locked_until = datetime.now() + timedelta(minutes=15)
        user.failed_login_count = 5
        original_hash = user.password_hash
        session.commit()

    operator_account.unlock("cebellamy2@gmail.com")

    with Session(db) as session:
        user = session.query(OperatorUser).one()
        assert user.locked_until is None
        assert user.failed_login_count == 0
        assert user.password_hash == original_hash
        _, outcome = operator_auth_service.authenticate_operator(
            session, "cebellamy2@gmail.com", SECRET,
        )
        assert outcome == operator_auth_service.OUTCOME_OK


def test_unlocking_an_account_that_does_not_exist_says_so(db):
    with pytest.raises(SystemExit) as exit_info:
        operator_account.unlock("nobody@example.com")
    assert "No operator account" in str(exit_info.value)


def test_list_shows_the_lock_state_and_no_credentials(db, interactive, capsys):
    interactive([SECRET, SECRET])
    operator_account.set_credentials("cebellamy2@gmail.com")
    with Session(db) as session:
        session.query(OperatorUser).one().locked_until = datetime.now() + timedelta(minutes=15)
        session.commit()
    capsys.readouterr()
    operator_account.list_accounts()
    out = capsys.readouterr().out
    assert "cebellamy2@gmail.com" in out and "LOCKED" in out
    assert SECRET not in out


def test_list_on_an_empty_database_says_so(db, capsys):
    operator_account.list_accounts()
    assert "No operator accounts exist yet." in capsys.readouterr().out


def test_it_refuses_a_piped_password_rather_than_echoing_it(tmp_path):
    """The password must never arrive on stdin, in argv or in the
    environment. Driven as a real subprocess, because what is being
    pinned is what ends up in a Railway SSH scrollback."""
    result = subprocess.run(
        [sys.executable, "operator_account.py", "--username", "cebellamy2@gmail.com"],
        input=f"{SECRET}\n{SECRET}\n", capture_output=True, text=True, cwd=REPO,
        env={"PATH": "/usr/bin:/bin", "DATABASE_URL": f"sqlite:///{tmp_path / 'piped.db'}"},
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "non-interactive stdin" in combined
    assert SECRET not in combined


def test_the_password_is_not_accepted_as_an_argument_at_all(tmp_path):
    result = subprocess.run(
        [sys.executable, "operator_account.py", "--username", "a@b.com", "--password", SECRET],
        capture_output=True, text=True, cwd=REPO,
        env={"PATH": "/usr/bin:/bin", "DATABASE_URL": f"sqlite:///{tmp_path / 'argv.db'}"},
    )
    assert result.returncode != 0
    assert "unrecognized arguments" in (result.stdout + result.stderr)


def test_the_source_reads_no_password_from_the_environment():
    """Pins the absence of the thing, not its behaviour: there is no
    os.getenv anywhere in this script, so no future edit can quietly add
    a password-shaped environment variable without this failing."""
    source = (REPO / "operator_account.py").read_text()
    assert "os.getenv" not in source
    assert "os.environ" not in source
    # It does not even import os, so there is nothing to reach for.
    assert "\nimport os" not in source


def test_the_documented_command_uses_the_container_virtualenv_python():
    """v1.197.0 shipped this wrong: a `railway ssh` shell is NOT the
    Nixpacks start-command environment, so plain `python` there has none
    of the app's dependencies and the script dies on its first import.
    Pinned so the instruction cannot silently regress."""
    source = (REPO / "operator_account.py").read_text()
    assert "/opt/venv/bin/python" in source
    docs = (REPO / "docs" / "DEVELOPMENT.md").read_text()
    assert "/opt/venv/bin/python operator_account.py" in docs
