import os
import subprocess
import sys

from sqlalchemy import create_engine, inspect

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
