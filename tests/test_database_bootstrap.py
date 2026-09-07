import os
import subprocess
import sqlite3
import sys

from sqlalchemy import create_engine, inspect

import database
from database import initialize_database


def test_importing_models_and_app_does_not_initialize_default_database(tmp_path):
    repo = os.path.dirname(os.path.dirname(__file__))
    env = os.environ.copy()
    env["PYTHONPATH"] = repo
    result = subprocess.run(
        [sys.executable, "-c", "import models; import main"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    default_path = tmp_path / "cardfoundry.db"
    if default_path.exists():
        target = create_engine(f"sqlite:///{default_path}")
        assert inspect(target).get_table_names() == []


def test_database_initialization_is_explicit_and_supports_temporary_bind(tmp_path):
    target = create_engine(f"sqlite:///{tmp_path / 'temporary.db'}")
    initialize_database(bind=target)
    tables = set(inspect(target).get_table_names())
    assert {"inventory_cards", "fulfillment_exceptions", "fulfillment_exception_events"} <= tables


def test_fresh_database_has_pending_pile_tables_and_target_pile_id(tmp_path):
    """CF-BUY-002: pending_piles/pending_pile_lines are brand-new tables
    -- Base.metadata.create_all() (already called by initialize_database
    for any bind) creates them with no migration code needed, unlike the
    additive target_pile_id column on the pre-existing scan_capture_jobs
    table (see the next test for that one)."""
    target = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    initialize_database(bind=target)
    tables = set(inspect(target).get_table_names())
    assert {"pending_piles", "pending_pile_lines"} <= tables
    columns = {col["name"] for col in inspect(target).get_columns("scan_capture_jobs")}
    assert "target_pile_id" in columns


def test_upgrade_existing_database_adds_target_pile_id_to_old_schema(tmp_path, monkeypatch):
    """Dry-run against a simulated pre-CF-BUY-002 database: an existing
    scan_capture_jobs table missing target_pile_id (and the two new
    tables not existing at all yet) must upgrade cleanly through the
    REAL upgrade_existing_database() path -- add_missing_columns only
    runs against database.engine (not an arbitrary bind), so this has
    to monkeypatch that module attribute, same convention
    tests/test_scan_chute.py's own setup_db() already uses."""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE scan_capture_jobs (id INTEGER PRIMARY KEY, status VARCHAR)")
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{db_path}")
    monkeypatch.setattr(database, "engine", engine)
    initialize_database()

    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"pending_piles", "pending_pile_lines"} <= tables
    columns = {row[1] for row in conn.execute("PRAGMA table_info(scan_capture_jobs)")}
    assert "target_pile_id" in columns
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()
