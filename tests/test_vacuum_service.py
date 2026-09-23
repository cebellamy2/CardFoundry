"""VACUUM reclaims freed pages, and fails safe when it cannot.

The failure path matters more than the happy path here. VACUUM takes an
EXCLUSIVE lock on the whole database for its duration, and this runs on
a cron 20 minutes before the 04:05 order sync -- so it has to stand
down rather than queue behind whatever holds the lock.
"""
import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import inventory_sync_service
import main
import vacuum_service
from models import Base
from vacuum_service import VacuumError, database_path, run_vacuum


@pytest.fixture
def cf_logs(caplog):
    """The cardfoundry logger does not propagate -- attach directly or
    every assertion here passes vacuously. See test_logging_visibility."""
    caplog.set_level(logging.DEBUG)
    vacuum_service.logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        vacuum_service.logger.removeHandler(caplog.handler)


def messages(caplog):
    return [r.getMessage() for r in caplog.records]


def bloated_db(tmp_path):
    """A database with real free pages: write a lot, then delete it."""
    path = tmp_path / "bloat.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE blob_rows (id INTEGER PRIMARY KEY, payload TEXT)")
    c.executemany("INSERT INTO blob_rows (payload) VALUES (?)",
                  [("x" * 20000,) for _ in range(300)])
    c.commit()
    c.execute("DELETE FROM blob_rows")
    c.commit()
    c.close()
    return path


# --- path parsing --------------------------------------------------------

def test_database_path_understands_the_urls_this_app_actually_uses():
    assert database_path("sqlite:///cardfoundry.db") == pytest.importorskip(
        "pathlib").Path("cardfoundry.db")
    assert str(database_path("sqlite:////data/cardfoundry.db")) == "/data/cardfoundry.db"
    # the read-only URI form used by every production inspection script
    assert str(database_path(
        "sqlite:///file:/data/cardfoundry.db?mode=ro&uri=true")) == "/data/cardfoundry.db"


def test_it_refuses_a_non_sqlite_database(cf_logs):
    with pytest.raises(VacuumError, match="non-SQLite"):
        database_path("postgresql://user@host/db")


def test_a_missing_file_fails_loudly_rather_than_creating_one(tmp_path, cf_logs):
    missing = tmp_path / "nope.db"
    with pytest.raises(VacuumError, match="does not exist"):
        run_vacuum(f"sqlite:///{missing}")
    assert not missing.exists(), "must not create the database it was asked to vacuum"
    assert any("refusing to run" in m for m in messages(cf_logs))


# --- the happy path ------------------------------------------------------

def test_vacuum_reclaims_space_and_reports_real_numbers(tmp_path, cf_logs):
    path = bloated_db(tmp_path)
    before = path.stat().st_size

    report = run_vacuum(f"sqlite:///{path}")

    after = path.stat().st_size
    assert after < before, "the file must actually shrink"
    assert report["bytes_before"] > report["bytes_after"]
    assert report["bytes_reclaimed"] > 0
    assert report["freelist_bytes_after"] < report["freelist_bytes_before"]
    assert report["seconds"] >= 0
    logged = messages(cf_logs)
    assert any("vacuum: starting" in m for m in logged)
    assert any("vacuum: completed" in m for m in logged)
    assert any("reclaimed" in m for m in logged)


def test_the_database_still_works_afterwards(tmp_path):
    path = bloated_db(tmp_path)
    run_vacuum(f"sqlite:///{path}")
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.execute("INSERT INTO blob_rows (payload) VALUES ('after')")
    c.commit()
    assert c.execute("SELECT COUNT(*) FROM blob_rows").fetchone()[0] == 1
    c.close()


# --- THE POINT: it fails rather than waits -------------------------------

def test_a_held_lock_fails_fast_and_loudly_without_retrying(tmp_path, cf_logs):
    """The behaviour the cron depends on. Another connection holds an
    exclusive lock; VACUUM must give up inside its short busy timeout,
    not queue behind it into the next cron tick."""
    path = bloated_db(tmp_path)
    size_before = path.stat().st_size

    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(VacuumError):
            run_vacuum(f"sqlite:///{path}", busy_timeout=0.2)
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert path.stat().st_size == size_before, "a refused VACUUM must change nothing"
    logged = messages(cf_logs)
    assert any("FAILED" in m and "database left untouched" in m for m in logged)
    assert any("next scheduled run will retry" in m for m in logged)


def test_the_failure_is_logged_at_ERROR_not_warning(tmp_path, cf_logs):
    """A skipped VACUUM is not routine -- it means something held the
    database at 03:45, which is supposed to be a quiet window."""
    path = bloated_db(tmp_path)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(VacuumError):
            run_vacuum(f"sqlite:///{path}", busy_timeout=0.2)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    levels = {r.levelname for r in cf_logs.records if "vacuum" in r.getMessage()}
    assert "ERROR" in levels


# --- the route -----------------------------------------------------------

def setup_app_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'route.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(main, "DATABASE_URL", f"sqlite:///{tmp_path / 'route.db'}")
    return db


def test_the_route_reports_a_greppable_summary_the_cron_can_parse(tmp_path, monkeypatch):
    setup_app_db(tmp_path, monkeypatch)
    response = TestClient(main.app).post("/admin/vacuum")
    assert response.status_code == 200
    assert "data-vacuum-summary=" in response.text
    assert "reclaimed_mb=" in response.text


def test_the_route_returns_409_not_500_when_the_lock_is_held(tmp_path, monkeypatch):
    """409 is a refusal, not a crash -- scheduled_vacuum distinguishes
    them and reports the refusal in its own words."""
    setup_app_db(tmp_path, monkeypatch)

    def refuse(*a, **k):
        raise VacuumError("database is locked")

    monkeypatch.setattr(main, "run_vacuum", refuse)
    response = TestClient(main.app).post("/admin/vacuum")
    assert response.status_code == 409
    assert "did not run" in response.text
    assert "Nothing was changed" in response.text


def test_the_admin_card_offers_it(tmp_path, monkeypatch):
    setup_app_db(tmp_path, monkeypatch)
    text = TestClient(main.app).get("/admin").text
    assert 'action="/admin/vacuum"' in text
    assert "Run VACUUM Now" in text


# --- the cron script -----------------------------------------------------

def test_the_cron_script_exits_non_zero_on_a_refusal():
    """Loudly, and without retrying into the 04:05 order sync."""
    import httpx
    import scheduled_vacuum

    def handler(request):
        return httpx.Response(409, text="<h1>VACUUM did not run.</h1>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert scheduled_vacuum.run_scheduled_vacuum(
        "http://app", "pw", client=client) == 1


def test_the_cron_script_exits_zero_and_echoes_the_summary():
    import httpx
    import scheduled_vacuum

    def handler(request):
        return httpx.Response(200, text='<span data-vacuum-summary="seconds=1.2 reclaimed_mb=110.0"></span>')

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert scheduled_vacuum.run_scheduled_vacuum(
        "http://app", "pw", client=client) == 0
