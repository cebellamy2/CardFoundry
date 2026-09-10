import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from job_retention_service import (
    JOB_RETENTION_DAYS,
    LAST_SWEEP_SETTING_KEY,
    RETENTION_DAYS_SETTING_KEY,
    compact_inventory_sync_job,
    compact_pricing_job,
    is_trimmed,
    last_sweep,
    record_last_sweep,
    retention_days,
    sweep_job_retention,
    trimmed_notice,
)
from models import (
    AppSetting,
    Base,
    CleanRebuildExecution,
    FloorCorrectionExecution,
    InventorySyncJob,
    PricingJob,
)


NOW = datetime(2026, 9, 30, 3, 15, 0)
OLD = NOW - timedelta(days=20)
YOUNG = NOW - timedelta(days=3)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'retention.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def maintenance_blob(rows=3):
    return {
        "preview_only": True, "preview_timestamp": "2026-09-10T02:31:00+00:00",
        "triggered_by": "scheduled",
        "local_snapshot_hash": "L" * 8, "remote_snapshot_hash": "R" * 8,
        "unresolved_card_ids": [1, 2],
        "order_ingestion": {"synced": 4, "deferred": 0},
        "summary": {"categories": {"hold_equal": rows, "increase_quantity": 1}, "exact_quantity_writes": 1},
        "rows": [{"category": "hold_equal", "canonical_identity": {"mtgjson_id": f"m{i}"},
                  "name": f"Card {i}", "desired_quantity": 1, "current_remote_quantity": 1}
                 for i in range(rows)],
    }


def competitor_preview_blob():
    return {
        "preview_only": True,
        "progress": {"stage": "complete", "optimizer_calls": 3},
        "preview": {
            "changes": [
                {"product_id": "p-1", "current_price": 100, "target_price": 85, "action": "decrease"},
                {"product_id": "p-2", "current_price": 250, "target_price": 260, "action": "increase"},
                {"product_id": None, "current_price": 5, "target_price": 5, "action": "hold"},
            ],
            "holds": [{"product_id": "p-3"}], "skipped": [], "audit_rows": [1, 2, 3, 4],
            "summary": {"increases": 1, "decreases": 1, "holds": 1, "optimizer_calls": 3},
        },
    }


def add_sync_job(session, *, mode, blob, created_at):
    job = InventorySyncJob(mode=mode, status="completed", snapshot_json=json.dumps(blob), created_at=created_at)
    session.add(job)
    session.flush()
    return job


def add_pricing_job(session, *, action, blob, created_at, request=None):
    job = PricingJob(
        action=action, status="completed", created_at=created_at,
        request_json=json.dumps(request or {"triggered_by": "scheduled"}),
        response_json=json.dumps(blob),
    )
    session.add(job)
    session.flush()
    return job


# -- compaction: what survives, per type -----------------------------------

def test_maintenance_preview_keeps_hashes_summary_counts_and_drops_rows():
    blob = maintenance_blob(rows=3)
    out = compact_inventory_sync_job("maintenance_preview", blob, now=NOW, days=14, original_bytes=9_000_000)

    assert out["_trimmed_at"] == NOW.isoformat()
    assert out["_original_bytes"] == 9_000_000 and out["_retention_days"] == 14
    assert out["triggered_by"] == "scheduled" and out["preview_timestamp"] == blob["preview_timestamp"]
    assert out["summary"] == blob["summary"]
    assert out["local_snapshot_hash"] == "L" * 8 and out["remote_snapshot_hash"] == "R" * 8
    assert out["order_ingestion"] == {"synced": 4, "deferred": 0}
    assert out["counts"] == {"rows": 3, "unresolved_card_ids": 2}
    assert "rows" not in out and "unresolved_card_ids" not in out
    assert is_trimmed(out) and not is_trimmed(blob)


def test_new_listing_preview_keeps_perform_sync_summary_with_its_row_lists_replaced_by_counts():
    blob = {
        "source_job_id": 41, "summary": {"candidates": 2, "priced": 1, "held": 1, "excluded": 0},
        "rows": [{"status": "priced"}, {"status": "hold"}],
        "perform_sync_summary": {
            "backfilled_cards": 3, "scope": "all",
            "still_unresolved": [{"inventory_card_id": 7}, {"inventory_card_id": 8}],
            "backfill_skipped": [{"inventory_card_id": 9}],
            "reconciliation": {"candidates": 0}, "order_sync": {"synced": 1},
        },
    }
    out = compact_inventory_sync_job("new_listing_preview", blob, now=NOW, days=14, original_bytes=10)

    assert out["source_job_id"] == 41 and out["summary"] == blob["summary"]
    assert out["counts"] == {"rows": 2}
    pss = out["perform_sync_summary"]
    assert pss["backfilled_cards"] == 3 and pss["reconciliation"] == {"candidates": 0}
    assert "still_unresolved" not in pss and "backfill_skipped" not in pss
    assert pss["counts"] == {"still_unresolved": 2, "backfill_skipped": 1}


def test_clean_rebuild_preview_keeps_the_scalars_the_executor_and_verifier_read():
    blob = {
        "summary": {"ready": True}, "blank_payloads": [1, 2], "republish_payloads": [3],
        "initial_price_rows": [{"binding_id": 1}], "exclusions": [],
        "local_snapshot_hash": "l", "remote_snapshot_hash": "r",
        "expected_resulting_seller_state_hash": "e", "final_local_database_hash": "f",
        "execution_pricing_seal_version": 2, "pricing_floor_cents": 65, "pricing_undercut_cents": 5,
        "excluded_status_counts": {"sold": 4}, "local_evidence": {"big": "x" * 100},
    }
    out = compact_inventory_sync_job("clean_rebuild_preview", blob, now=NOW, days=14, original_bytes=10)

    for key in ("local_snapshot_hash", "remote_snapshot_hash", "expected_resulting_seller_state_hash",
                "final_local_database_hash", "execution_pricing_seal_version",
                "pricing_floor_cents", "pricing_undercut_cents", "excluded_status_counts"):
        assert out[key] == blob[key]
    assert out["counts"] == {"initial_price_rows": 1, "exclusions": 0, "blank_payloads": 2, "republish_payloads": 1}
    assert "local_evidence" not in out and "blank_payloads" not in out


def test_competitor_preview_keeps_prior_prices_summary_progress_and_counts():
    out = compact_pricing_job("competitor_only_full_preview", competitor_preview_blob(), now=NOW, days=14, original_bytes=7_000_000)

    assert out["prior_prices"] == {"p-1": 100, "p-2": 250}
    assert out["preview"]["summary"] == {"increases": 1, "decreases": 1, "holds": 1, "optimizer_calls": 3}
    assert out["preview"]["counts"] == {"changes": 3, "holds": 1, "skipped": 0, "audit_rows": 4}
    assert out["progress"]["stage"] == "complete"
    assert "changes" not in out["preview"] and "changes" not in out


def test_competitor_apply_keeps_source_link_previous_prices_are_not_needed_but_counts_are():
    blob = {
        "source_job_id": 9, "triggered_by": "scheduled",
        "updates": [{"product_id": "p-1", "price_cents": 85}], "repriced": [], "excluded": [{"product_id": "p-2"}],
        "responses": [{"inventory": [], "skipped": []}], "previous_prices": {"p-1": 100},
    }
    out = compact_pricing_job("competitor_only_full_apply", blob, now=NOW, days=14, original_bytes=10)

    assert out["source_job_id"] == 9 and out["triggered_by"] == "scheduled"
    assert out["counts"] == {"updates": 1, "excluded": 1, "repriced": 0, "responses": 1}
    assert "updates" not in out


def test_trimmed_notice_names_the_date_and_window():
    out = compact_inventory_sync_job("maintenance_preview", maintenance_blob(), now=NOW, days=14, original_bytes=1)
    text = trimmed_notice(out)
    assert "2026-09-30" in text and "14-day" in text


# -- the sweep --------------------------------------------------------------

def test_sweep_trims_only_rows_older_than_the_window_and_leaves_young_rows_byte_identical(session):
    old_sync = add_sync_job(session, mode="maintenance_preview", blob=maintenance_blob(), created_at=OLD)
    young_sync = add_sync_job(session, mode="maintenance_preview", blob=maintenance_blob(), created_at=YOUNG)
    old_pricing = add_pricing_job(session, action="competitor_only_full_preview", blob=competitor_preview_blob(), created_at=OLD)
    young_pricing = add_pricing_job(session, action="competitor_only_full_preview", blob=competitor_preview_blob(), created_at=YOUNG)
    session.commit()
    young_sync_raw, young_pricing_raw = young_sync.snapshot_json, young_pricing.response_json

    report = sweep_job_retention(session, now=NOW)

    assert report["retention_days"] == JOB_RETENTION_DAYS and report["dry_run"] is False
    assert report["inventory_sync_jobs"]["trimmed"] == [old_sync.id]
    assert report["pricing_jobs"]["trimmed"] == [old_pricing.id]
    assert report["inventory_sync_jobs"]["bytes_after"] < report["inventory_sync_jobs"]["bytes_before"]
    session.expire_all()
    assert is_trimmed(json.loads(session.get(InventorySyncJob, old_sync.id).snapshot_json))
    assert is_trimmed(json.loads(session.get(PricingJob, old_pricing.id).response_json))
    assert session.get(InventorySyncJob, young_sync.id).snapshot_json == young_sync_raw
    assert session.get(PricingJob, young_pricing.id).response_json == young_pricing_raw
    # Rows stay; only the payload changed.
    assert session.query(InventorySyncJob).count() == 2 and session.query(PricingJob).count() == 2


def test_sweep_is_idempotent(session):
    old_sync = add_sync_job(session, mode="maintenance_preview", blob=maintenance_blob(), created_at=OLD)
    session.commit()
    sweep_job_retention(session, now=NOW)
    session.expire_all()
    first = session.get(InventorySyncJob, old_sync.id).snapshot_json

    report = sweep_job_retention(session, now=NOW + timedelta(days=1))

    assert report["inventory_sync_jobs"]["trimmed"] == []
    session.expire_all()
    assert session.get(InventorySyncJob, old_sync.id).snapshot_json == first


def test_sweep_dry_run_reports_but_writes_nothing(session):
    old_sync = add_sync_job(session, mode="maintenance_preview", blob=maintenance_blob(), created_at=OLD)
    session.commit()
    raw = old_sync.snapshot_json

    report = sweep_job_retention(session, now=NOW, dry_run=True)

    assert report["dry_run"] is True
    assert report["inventory_sync_jobs"]["trimmed"] == [old_sync.id]
    session.expire_all()
    assert session.get(InventorySyncJob, old_sync.id).snapshot_json == raw


def test_sweep_exempts_previews_referenced_by_active_executions(session):
    old_rebuild = add_sync_job(session, mode="clean_rebuild_preview", blob={"summary": {"ready": True}, "blank_payloads": [1]}, created_at=OLD)
    done_rebuild = add_sync_job(session, mode="clean_rebuild_preview", blob={"summary": {"ready": True}, "blank_payloads": [1]}, created_at=OLD)
    old_floor = add_pricing_job(session, action="floor_correction_preview", blob={"targets": [1], "payloads": [1]}, created_at=OLD)
    session.add(CleanRebuildExecution(
        execution_id="exec-active", preview_job_id=old_rebuild.id, status="prepared", current_phase="prepared",
        preview_evidence_json="{}", expected_seller_state_hash="", confirmation_hash="",
        store_off_evidence_json="{}", local_snapshot_evidence_json="{}", remote_prewrite_snapshot_json="{}",
    ))
    session.add(CleanRebuildExecution(
        execution_id="exec-done", preview_job_id=done_rebuild.id, status="completed", current_phase="completed",
        preview_evidence_json="{}", expected_seller_state_hash="", confirmation_hash="",
        store_off_evidence_json="{}", local_snapshot_evidence_json="{}", remote_prewrite_snapshot_json="{}",
    ))
    session.add(FloorCorrectionExecution(
        execution_id="floor-active", preview_job_id=old_floor.id, status="applying", current_phase="applying",
        preview_hash="", confirmation_hash="", store_off_evidence_json="{}", remote_prewrite_snapshot_json="{}",
    ))
    session.commit()

    report = sweep_job_retention(session, now=NOW)

    assert report["inventory_sync_jobs"]["exempt"] == [old_rebuild.id]
    assert report["inventory_sync_jobs"]["trimmed"] == [done_rebuild.id]
    assert report["pricing_jobs"]["exempt"] == [old_floor.id]
    assert report["pricing_jobs"]["trimmed"] == []
    session.expire_all()
    assert "blank_payloads" in json.loads(session.get(InventorySyncJob, old_rebuild.id).snapshot_json)
    assert "targets" in json.loads(session.get(PricingJob, old_floor.id).response_json)


def test_sweep_exempts_the_source_preview_of_a_recent_competitor_apply(session):
    """Rule 5b, belt-and-suspenders beside previous_prices: a preview older
    than the window that a YOUNG apply still points at keeps its rows."""
    old_preview = add_pricing_job(session, action="competitor_only_full_preview", blob=competitor_preview_blob(), created_at=OLD)
    orphan_preview = add_pricing_job(session, action="competitor_only_full_preview", blob=competitor_preview_blob(), created_at=OLD)
    add_pricing_job(
        session, action="competitor_only_full_apply", created_at=YOUNG,
        blob={"source_job_id": old_preview.id, "updates": [], "excluded": [], "repriced": []},
    )
    session.commit()

    report = sweep_job_retention(session, now=NOW)

    assert report["pricing_jobs"]["exempt"] == [old_preview.id]
    assert report["pricing_jobs"]["trimmed"] == [orphan_preview.id]


def test_retention_window_can_be_overridden_by_app_setting(session):
    session.add(AppSetting(key=RETENTION_DAYS_SETTING_KEY, value="30"))
    twenty_days_old = add_sync_job(session, mode="maintenance_preview", blob=maintenance_blob(), created_at=OLD)
    session.commit()

    assert retention_days(session) == 30
    report = sweep_job_retention(session, now=NOW)
    assert report["retention_days"] == 30
    assert report["inventory_sync_jobs"]["trimmed"] == []
    session.expire_all()
    assert not is_trimmed(json.loads(session.get(InventorySyncJob, twenty_days_old.id).snapshot_json))


def test_sweep_skips_unparseable_json_without_failing(session):
    broken = InventorySyncJob(mode="maintenance_preview", status="completed", snapshot_json="{not json", created_at=OLD)
    session.add(broken)
    old_sync = add_sync_job(session, mode="maintenance_preview", blob=maintenance_blob(), created_at=OLD)
    session.commit()

    report = sweep_job_retention(session, now=NOW)

    assert report["inventory_sync_jobs"]["skipped_unparseable"] == [broken.id]
    assert report["inventory_sync_jobs"]["trimmed"] == [old_sync.id]


def test_record_and_read_last_sweep_keeps_counts_not_id_lists(session):
    add_sync_job(session, mode="maintenance_preview", blob=maintenance_blob(), created_at=OLD)
    session.commit()
    report = sweep_job_retention(session, now=NOW)
    record_last_sweep(session, report)
    session.commit()

    stored = last_sweep(session)
    assert stored["ran_at"] == NOW.isoformat()
    assert stored["inventory_sync_jobs"]["trimmed"] == 1
    assert isinstance(stored["inventory_sync_jobs"]["bytes_before"], int)
    assert session.query(AppSetting).filter(AppSetting.key == LAST_SWEEP_SETTING_KEY).count() == 1
