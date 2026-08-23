"""Starting a full competitor preview while one is already running would
point a second ~264-batch optimizer fan-out at the same rate-limited Mana
Pool account. Seen live: the scheduled pricing cron opened preview job 22
while an earlier preview's optimizer calls were still in flight.
"""

import json
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
from models import Base, PricingJob


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'preview_overlap.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    return db


def make_preview_job(session, *, status, created_at=None, action="competitor_only_full_preview"):
    job = PricingJob(
        external_job_id=None,
        action=action,
        status=status,
        request_json=json.dumps({"undercut_cents": 5, "floor_cents": 65}),
        response_json=json.dumps({"preview_only": True}),
        created_at=created_at or datetime.now(),
    )
    session.add(job)
    session.commit()
    return job.id


def start_preview(monkeypatch):
    """POST the start route without letting the real preview run."""
    started = []
    monkeypatch.setattr(main, "_run_full_competitor_preview", started.append)
    client = TestClient(main.app)
    response = client.post(
        "/pricing/full-competitor-preview",
        data={"undercut_dollars": "0.05", "floor_dollars": "0.65"},
        follow_redirects=False,
    )
    return response, started


def test_start_redirects_to_the_preview_already_running(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        running_id = make_preview_job(session, status="pending")

    response, started = start_preview(monkeypatch)

    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/pricing/full-competitor-preview/{running_id}"
    )
    # No second fan-out was queued, and no second job row was created.
    assert started == []
    with Session(db) as session:
        assert session.query(PricingJob).count() == 1


def test_a_second_start_does_not_stack_a_competing_run(tmp_path, monkeypatch):
    """What the cron actually does: POST, follow the redirect, poll. It must
    end up on the running preview rather than opening a rival one."""
    setup_db(tmp_path, monkeypatch)

    first, started_first = start_preview(monkeypatch)
    first_id = first.headers["location"].rsplit("/", 1)[-1]
    second, started_second = start_preview(monkeypatch)

    assert len(started_first) == 1
    assert started_second == []
    assert second.headers["location"].rsplit("/", 1)[-1] == first_id


def test_completed_preview_does_not_block_a_new_one(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        done_id = make_preview_job(session, status="completed")

    response, started = start_preview(monkeypatch)

    assert response.status_code == 303
    assert response.headers["location"] != (
        f"/pricing/full-competitor-preview/{done_id}"
    )
    assert len(started) == 1


def test_failed_preview_does_not_block_a_new_one(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_preview_job(session, status="failed")

    response, started = start_preview(monkeypatch)

    assert response.status_code == 303
    assert len(started) == 1


def test_abandoned_pending_preview_stops_blocking_once_stale(tmp_path, monkeypatch):
    """An app restart mid-run leaves a job pending forever with no task
    behind it. Without the staleness cutoff that would block every later
    preview, including the cron's."""
    db = setup_db(tmp_path, monkeypatch)
    abandoned = datetime.now() - main.FULL_COMPETITOR_PREVIEW_STALE_AFTER - timedelta(minutes=1)
    with Session(db) as session:
        stale_id = make_preview_job(session, status="pending", created_at=abandoned)

    response, started = start_preview(monkeypatch)

    assert response.headers["location"] != (
        f"/pricing/full-competitor-preview/{stale_id}"
    )
    assert len(started) == 1


def test_a_pending_job_of_another_action_does_not_block(tmp_path, monkeypatch):
    """Only full competitor previews contend for this fan-out."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        other_id = make_preview_job(session, status="pending", action="bulk_price_apply")

    response, started = start_preview(monkeypatch)

    assert response.headers["location"] != (
        f"/pricing/full-competitor-preview/{other_id}"
    )
    assert len(started) == 1
