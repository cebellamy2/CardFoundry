"""Deploy-collision recovery: what the app does at startup about jobs the
previous container took with it, and the read-only readiness check the
deploy guard and cron scripts consult."""
import json
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from inventory_sync_service import LEASE_NAME, acquire_inventory_lease
from models import Base, InventorySyncLease, PricingJob
from restart_recovery_service import (
    INTERRUPTED_ERROR_PREFIX,
    deploy_readiness,
    recover_from_restart,
)


NOW = datetime(2026, 9, 10, 0, 1, 52)


def setup_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'restart_recovery.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def add_pricing_job(session, *, status, action="competitor_only_full_preview", response=None):
    job = PricingJob(
        action=action, status=status, request_json="{}",
        response_json=json.dumps(response) if response is not None else None,
    )
    session.add(job)
    session.flush()
    return job


# -- recover_from_restart ----------------------------------------------------

def test_in_flight_pricing_jobs_are_marked_failed_with_the_interruption_reason(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        running = add_pricing_job(session, status="running", response={"preview_only": True, "progress": {"stage": "optimizer"}})
        pending = add_pricing_job(session, status="pending")
        done = add_pricing_job(session, status="completed", response={"preview": {"summary": {}}})
        failed = add_pricing_job(session, status="failed", response={"error": "boom"})
        session.commit()
        ids = (running.id, pending.id, done.id, failed.id)

    with Session(db) as session:
        report = recover_from_restart(session, now=NOW)
        session.commit()

    assert report["failed_pricing_job_ids"] == [ids[0], ids[1]]
    assert report["cleared_lease"] is None
    with Session(db) as session:
        interrupted = session.get(PricingJob, ids[0])
        assert interrupted.status == "failed"
        stored = json.loads(interrupted.response_json)
        assert stored["error"].startswith(INTERRUPTED_ERROR_PREFIX)
        assert "2026-09-10T00:01:52" in stored["error"]
        assert stored["interrupted_at"] == NOW.isoformat()
        # The progress it had reached is kept alongside the error, not wiped.
        assert stored["progress"] == {"stage": "optimizer"}
        # A pending job with no response_json yet gets a fresh dict.
        assert json.loads(session.get(PricingJob, ids[1]).response_json)["error"].startswith(INTERRUPTED_ERROR_PREFIX)
        # Terminal rows are untouched.
        assert session.get(PricingJob, ids[2]).status == "completed"
        assert json.loads(session.get(PricingJob, ids[3]).response_json) == {"error": "boom"}


def test_leftover_lease_is_cleared_even_while_still_within_its_ttl(tmp_path, monkeypatch):
    """The lease's own TTL model would hold this for up to 15 minutes; on
    startup it provably belongs to a dead process (single-mount volume),
    so it goes now."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        acquire_inventory_lease(session, "dead-process-token", ttl_seconds=900)
        session.commit()

    with Session(db) as session:
        report = recover_from_restart(session, now=NOW)
        session.commit()

    assert report["failed_pricing_job_ids"] == []
    assert report["cleared_lease"]["owner_token"] == "dead-process-token"
    assert report["cleared_lease"]["was_still_valid"] is True or report["cleared_lease"]["was_still_valid"] is False
    with Session(db) as session:
        assert session.get(InventorySyncLease, LEASE_NAME) is None


def test_recovery_on_a_clean_database_changes_nothing(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_pricing_job(session, status="completed", response={})
        session.commit()
    with Session(db) as session:
        report = recover_from_restart(session, now=NOW)
    assert report == {"failed_pricing_job_ids": [], "cleared_lease": None}


# -- the startup hook actually runs it --------------------------------------

def test_startup_hook_runs_recovery(tmp_path, monkeypatch, capsys):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job = add_pricing_job(session, status="running", response={})
        acquire_inventory_lease(session, "dead", ttl_seconds=900)
        session.commit()
        job_id = job.id

    main.initialize_app_database()

    with Session(db) as session:
        assert session.get(PricingJob, job_id).status == "failed"
        assert session.get(InventorySyncLease, LEASE_NAME) is None
    out = capsys.readouterr().out
    assert "Startup recovery" in out and str(job_id) in out


def test_startup_hook_is_quiet_when_there_is_nothing_to_recover(tmp_path, monkeypatch, capsys):
    setup_db(tmp_path, monkeypatch)
    main.initialize_app_database()
    assert "Startup recovery" not in capsys.readouterr().out


# -- the cron script can recognise an interrupted preview on the page ----------

def test_interrupted_preview_page_shows_the_recognisable_reason(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job = add_pricing_job(session, status="running", response={"preview_only": True})
        session.commit()
        job_id = job.id
    with Session(db) as session:
        recover_from_restart(session, now=NOW)
        session.commit()

    page = TestClient(main.app).get(f"/pricing/full-competitor-preview/{job_id}")
    assert page.status_code == 200
    assert "Full Competitor-Only Preview Failed" in page.text
    assert INTERRUPTED_ERROR_PREFIX in page.text


# -- deploy readiness ----------------------------------------------------------

def test_readiness_is_ready_on_a_quiet_app(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_pricing_job(session, status="completed", response={})
        session.commit()
    with Session(db) as session:
        assert deploy_readiness(session, now=NOW) == {"ready": True, "reasons": [], "checked_at": "2026-09-10T00:01:52"}

    response = TestClient(main.app).get("/admin/deploy-readiness")
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.headers["cache-control"] == "no-store"


def test_readiness_is_503_while_a_pricing_job_is_in_flight(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job = add_pricing_job(session, status="running", response={})
        session.commit()
        job_id = job.id

    response = TestClient(main.app).get("/admin/deploy-readiness")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["reasons"] == [f"pricing job(s) in flight: {job_id} (competitor_only_full_preview, running)"]


def test_readiness_is_503_while_the_inventory_lease_is_held_but_not_after_it_expires(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        acquire_inventory_lease(session, "perform-sync", ttl_seconds=900)
        session.commit()
    with Session(db) as session:
        held = deploy_readiness(session)
        assert held["ready"] is False and held["reasons"][0].startswith("inventory lease held")
        expired = deploy_readiness(session, now=datetime.now() + timedelta(seconds=901))
        assert expired["ready"] is True


def test_readiness_route_is_behind_the_password_gate(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "correct-horse-battery-staple")
    assert TestClient(main.app).get("/admin/deploy-readiness").status_code == 401
