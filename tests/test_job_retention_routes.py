"""Job-JSON retention, as seen through the app: history summaries stay
readable on trimmed rows, every job detail page renders the trimmed
state instead of an empty table, every derive/apply path refuses a
trimmed preview with an explicit 409, and the /admin sweep works (dry
run and real) -- plus the cron script that drives it."""
import json
from datetime import datetime, timedelta

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
import scheduled_job_retention
from job_retention_service import compact_inventory_sync_job, compact_pricing_job
from models import AppSetting, Base, InventorySyncJob, PricingJob


NOW = datetime(2026, 9, 30, 3, 15, 0)


def setup_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'retention_routes.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def trimmed_sync_job(session, mode, blob, *, days_old=20):
    compact = compact_inventory_sync_job(mode, blob, now=NOW, days=14, original_bytes=1234)
    job = InventorySyncJob(
        mode=mode, status="completed", snapshot_json=json.dumps(compact),
        created_at=NOW - timedelta(days=days_old),
    )
    session.add(job)
    session.flush()
    return job


def trimmed_pricing_job(session, action, blob, *, days_old=20):
    compact = compact_pricing_job(action, blob, now=NOW, days=14, original_bytes=1234)
    job = PricingJob(
        action=action, status="completed", request_json=json.dumps({"triggered_by": "scheduled"}),
        response_json=json.dumps(compact), created_at=NOW - timedelta(days=days_old),
    )
    session.add(job)
    session.flush()
    return job


MAINTENANCE = {
    "triggered_by": "scheduled", "summary": {"categories": {"hold_equal": 5, "increase_quantity": 2}},
    "rows": [{"category": "hold_equal"}] * 7, "local_snapshot_hash": "l", "remote_snapshot_hash": "r",
}
RECON_APPLY = {"source_job_id": 3, "updates": [{"product_id": "p"}] * 4, "excluded": [{}] * 2, "responses": []}
NEW_LISTING_PREVIEW = {
    "source_job_id": 1, "summary": {"candidates": 3, "priced": 2, "held": 1, "excluded": 0},
    "rows": [{"status": "priced", "evidence_hash": "h1"}] * 3,
    "perform_sync_summary": {"backfilled_cards": 0, "still_unresolved": [{}], "backfill_skipped": []},
}
COMPETITOR_PREVIEW = {
    "preview": {"changes": [{"product_id": "p-1", "current_price": 100}], "holds": [],
                "summary": {"increases": 0, "decreases": 1, "holds": 0}},
    "progress": {"stage": "complete"},
}
COMPETITOR_APPLY = {"source_job_id": 5, "updates": [{"product_id": "p-1"}], "repriced": [], "excluded": [{}]}


# -- history summaries -----------------------------------------------------

def test_history_summaries_read_counts_and_flag_trimmed_rows():
    class Row:
        pass

    sync = Row(); sync.mode = "reconciliation_apply"
    sync.snapshot_json = json.dumps(compact_inventory_sync_job("reconciliation_apply", RECON_APPLY, now=NOW, days=14, original_bytes=1))
    assert main._sync_job_items_summary(sync) == "4 updated / 2 excluded · trimmed 2026-09-30"

    listing = Row(); listing.mode = "new_listing_apply"
    listing.snapshot_json = json.dumps(compact_inventory_sync_job(
        "new_listing_apply", {"scryfall_updates": [1, 2], "product_updates": [3]}, now=NOW, days=14, original_bytes=1))
    assert main._sync_job_items_summary(listing) == "2 via scryfall / 1 via product ID · trimmed 2026-09-30"

    pricing = Row(); pricing.action = "competitor_only_full_apply"
    pricing.response_json = json.dumps(compact_pricing_job("competitor_only_full_apply", COMPETITOR_APPLY, now=NOW, days=14, original_bytes=1))
    assert main._pricing_job_items_summary(pricing) == "1 applied / 0 repriced / 1 excluded · trimmed 2026-09-30"

    preview = Row(); preview.action = "competitor_only_full_preview"
    preview.response_json = json.dumps(compact_pricing_job("competitor_only_full_preview", COMPETITOR_PREVIEW, now=NOW, days=14, original_bytes=1))
    assert main._pricing_job_items_summary(preview) == "0 up / 1 down / 0 held · trimmed 2026-09-30"

    # Untrimmed rows read exactly as before -- no suffix, arrays counted directly.
    live = Row(); live.mode = "reconciliation_apply"; live.snapshot_json = json.dumps(RECON_APPLY)
    assert main._sync_job_items_summary(live) == "4 updated / 2 excluded"


def test_inventory_sync_history_table_shows_the_trimmed_marker(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        trimmed_sync_job(session, "maintenance_preview", MAINTENANCE)
        session.commit()
    response = TestClient(main.app).get("/inventory-sync")
    assert response.status_code == 200
    assert "7 row(s) · trimmed 2026-09-30" in response.text


# -- detail pages render the trimmed state ---------------------------------

def test_inventory_sync_detail_renders_trimmed_state_for_every_mode(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        ids = {
            "maintenance_preview": trimmed_sync_job(session, "maintenance_preview", MAINTENANCE).id,
            "new_listing_preview": trimmed_sync_job(session, "new_listing_preview", NEW_LISTING_PREVIEW).id,
            "new_listing_apply": trimmed_sync_job(session, "new_listing_apply", {"scryfall_updates": [1], "product_updates": []}).id,
            "reconciliation_preview": trimmed_sync_job(session, "reconciliation_preview", {"rows": [{}], "summary": {"candidates": 1, "increase": 1, "decrease": 0, "excluded": 0}}).id,
            "reconciliation_apply": trimmed_sync_job(session, "reconciliation_apply", RECON_APPLY).id,
            "clean_rebuild_preview": trimmed_sync_job(session, "clean_rebuild_preview", {"summary": {"ready": True}, "initial_price_rows": [{}]}).id,
        }
        session.commit()
    client = TestClient(main.app)
    for mode, job_id in ids.items():
        response = client.get(f"/inventory-sync/{job_id}")
        assert response.status_code == 200, mode
        assert "trimmed on 2026-09-30 after the 14-day retention window" in response.text, mode
        assert "Row counts at trim time" in response.text, mode
        # No action forms on a trimmed job -- nothing here can be applied.
        assert "PUBLISH NEW LISTINGS" not in response.text and "RECONCILE QUANTITIES" not in response.text, mode
        assert "<td>0</td>" not in response.text or "counts" in response.text  # sanity: page is the trimmed page


def test_competitor_pricing_detail_pages_render_trimmed_state(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        preview_id = trimmed_pricing_job(session, "competitor_only_full_preview", COMPETITOR_PREVIEW).id
        apply_id = trimmed_pricing_job(session, "competitor_only_full_apply", COMPETITOR_APPLY).id
        revert_id = trimmed_pricing_job(session, "competitor_only_full_revert", {"source_apply_job_id": apply_id, "reverts": [{}], "responses": [], "excluded": []}).id
        session.commit()
    client = TestClient(main.app)
    for path in (
        f"/pricing/full-competitor-preview/{preview_id}",
        f"/pricing/full-competitor-apply/{apply_id}",
        f"/pricing/full-competitor-revert/{revert_id}",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "trimmed on 2026-09-30 after the 14-day retention window" in response.text, path
        assert "APPLY COMPETITIVE PRICES" not in response.text and "Revert to Previous Price" not in response.text, path


# -- derive/apply from a trimmed preview: explicit 409 ----------------------

def test_deriving_or_applying_from_a_trimmed_preview_is_refused_with_409(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        maintenance_id = trimmed_sync_job(session, "maintenance_preview", MAINTENANCE).id
        listing_id = trimmed_sync_job(session, "new_listing_preview", NEW_LISTING_PREVIEW).id
        recon_id = trimmed_sync_job(session, "reconciliation_preview", {"rows": [{"status": "eligible"}], "summary": {"candidates": 1}}).id
        session.add(AppSetting(key=main.GO_LIVE_SETTING_KEY, value="2026-01-01T00:00:00Z"))
        competitor_id = trimmed_pricing_job(session, "competitor_only_full_preview", COMPETITOR_PREVIEW).id
        session.commit()
    client = TestClient(main.app)
    attempts = [
        client.post(f"/inventory-sync/{maintenance_id}/new-listings/preview"),
        client.post(f"/inventory-sync/{maintenance_id}/reconcile/preview"),
        client.post(f"/inventory-sync/{listing_id}/new-listings/apply", data={"confirmation": main.NEW_LISTING_CONFIRMATION}),
        client.post(f"/inventory-sync/{recon_id}/reconcile/apply", data={"confirmation": main.RECONCILE_CONFIRMATION}),
        client.post(f"/pricing/full-competitor-preview/{competitor_id}/apply", data={"confirmation": main.COMPETITOR_PRICE_APPLY_CONFIRMATION}),
    ]
    for response in attempts:
        assert response.status_code == 409, response.request.url
        assert "Preview Was Trimmed" in response.text
        assert "Run a fresh preview" in response.text


def test_manual_price_hold_lookup_on_a_trimmed_preview_does_not_crash(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        listing_id = trimmed_sync_job(session, "new_listing_preview", NEW_LISTING_PREVIEW).id
        session.commit()
    response = TestClient(main.app).get(f"/inventory-sync/{listing_id}/new-listings/manual-price/h1")
    assert response.status_code in (400, 404, 409)


# -- the sweep route and admin card -------------------------------------------

def _old_rows(session):
    # The route sweeps against the real clock, so age these relative to it.
    old = datetime.now() - timedelta(days=20)
    session.add(InventorySyncJob(mode="maintenance_preview", status="completed", snapshot_json=json.dumps(MAINTENANCE), created_at=old))
    session.add(PricingJob(action="competitor_only_full_preview", status="completed", request_json="{}", response_json=json.dumps(COMPETITOR_PREVIEW), created_at=old))
    session.add(InventorySyncJob(mode="maintenance_preview", status="completed", snapshot_json=json.dumps(MAINTENANCE)))  # young, untouched


def test_sweep_route_dry_run_reports_without_writing(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _old_rows(session)
        session.commit()
    response = TestClient(main.app).post("/admin/job-retention/sweep", data={"dry_run": "1"})
    assert response.status_code == 200
    assert "Job Retention Dry Run" in response.text
    assert "Nothing was written" in response.text
    assert 'data-sweep-summary="dry_run=True retention_days=14 sync_trimmed=1 pricing_trimmed=1' in response.text
    with Session(db) as session:
        assert not any("_trimmed_at" in job.snapshot_json for job in session.query(InventorySyncJob))
        assert session.query(AppSetting).filter(AppSetting.key == "job_retention_last_sweep_json").count() == 0


def test_sweep_route_trims_records_last_sweep_and_admin_card_shows_it(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _old_rows(session)
        session.commit()
    client = TestClient(main.app)
    response = client.post("/admin/job-retention/sweep", data={"dry_run": ""})
    assert response.status_code == 200
    assert "Job Retention Sweep Complete" in response.text
    assert "manual <code>VACUUM</code>" in response.text
    with Session(db) as session:
        jobs = session.query(InventorySyncJob).order_by(InventorySyncJob.id).all()
        assert "_trimmed_at" in jobs[0].snapshot_json and "_trimmed_at" not in jobs[1].snapshot_json
        assert "_trimmed_at" in session.query(PricingJob).one().response_json

    admin = client.get("/admin")
    assert admin.status_code == 200
    assert "Job Retention" in admin.text
    assert "Preview Sweep (dry run)" in admin.text and "Run Sweep Now" in admin.text
    assert "1 sync job(s) + 1 pricing job(s) trimmed" in admin.text


def test_admin_card_before_any_sweep_says_never_run(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    admin = TestClient(main.app).get("/admin")
    assert "Last sweep: <strong>never run</strong>" in admin.text
    assert "older than 14 days" in admin.text


def test_sweep_route_is_behind_the_password_gate(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "correct-horse-battery-staple")
    response = TestClient(main.app).post("/admin/job-retention/sweep", data={"dry_run": "1"})
    assert response.status_code == 401


# -- the cron script -------------------------------------------------------------

def test_scheduled_script_posts_a_real_sweep_and_prints_the_summary(capsys):
    calls = []

    def handler(request):
        calls.append((request.url.path, request.headers.get("authorization"), request.content))
        return httpx.Response(200, text='<span hidden data-sweep-summary="dry_run=False sync_trimmed=2"></span><h1>ok</h1>')

    client = httpx.Client(transport=httpx.MockTransport(handler))
    code = scheduled_job_retention.run_job_retention_sweep("https://example.test/", "pw", client=client)
    assert code == 0
    assert calls[0][0] == "/admin/job-retention/sweep"
    assert calls[0][1].startswith("Basic ")
    assert b"dry_run=" in calls[0][2] and b"dry_run=1" not in calls[0][2]
    out = capsys.readouterr().out
    assert "POST /admin/job-retention/sweep -> 200" in out
    assert "sync_trimmed=2" in out


def test_scheduled_script_exits_nonzero_on_failure(capsys):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    assert scheduled_job_retention.run_job_retention_sweep("https://example.test", "pw", client=client) == 1
    assert "boom" in capsys.readouterr().out
