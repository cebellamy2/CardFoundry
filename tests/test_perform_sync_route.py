import json

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, InventorySyncJob


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'perform_sync.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def fake_mirror_preview(unresolved_card_ids=None):
    return {
        "preview_only": True,
        "maintenance_mode_required": True,
        "preview_timestamp": "2026-08-17T00:00:00Z",
        "local_snapshot_hash": "local-hash",
        "remote_snapshot_hash": "remote-hash",
        "rows": [],
        "unresolved_card_ids": unresolved_card_ids or [],
        "order_ingestion": {"imported": 0, "already_known": 0, "failed": []},
        "summary": {
            "categories": {}, "exact_quantity_writes": 0,
            "managed_remote_variants": 0, "unresolved_mappings": 0,
        },
    }


def fake_new_listing_preview():
    return {
        "rows": [],
        "summary": {"candidates": 0, "priced": 0, "held": 0, "excluded": 0},
    }


def fake_reconciliation_preview(candidates=0, increase=0, decrease=0, excluded=0):
    return {
        "preview_only": True,
        "preview_timestamp": "2026-08-23T00:00:00Z",
        "source_local_snapshot_hash": "local-hash",
        "source_remote_snapshot_hash": "remote-hash",
        "rows": [],
        "summary": {
            "candidates": candidates, "increase": increase,
            "decrease": decrease, "excluded": excluded,
        },
    }


def fake_reconciliation_apply_result(updates=None, excluded=None):
    return {
        "applied_at": "2026-08-23T00:05:00Z",
        "updates": updates or [],
        "responses": [],
        "excluded": excluded or [],
    }


def _stub_backfill_and_preview(monkeypatch):
    monkeypatch.setattr(
        main, "run_additive_mtgjson_backfill",
        lambda session, seller_loader, catalog_loader, operator_note=None: {
            "updated_inventory_cards": 0, "updated_bindings": 0, "skipped": [],
        },
    )
    monkeypatch.setattr(
        main, "create_inventory_sync_preview", lambda **kwargs: fake_mirror_preview(),
    )


def test_perform_sync_button_appears_on_inventory_sync_page(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory-sync")
    assert response.status_code == 200
    assert 'action="/inventory-sync/perform-sync"' in response.text
    assert "Perform Sync with Mana Pool" in response.text


def test_perform_sync_chains_backfill_preview_and_new_listings(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    calls = []

    def fake_backfill(session, seller_loader, catalog_loader, operator_note=None):
        calls.append("backfill")
        assert operator_note == "Automated via Perform Sync with Mana Pool"
        return {
            "updated_inventory_cards": 3, "updated_bindings": 3,
            "skipped": [{
                "inventory_card_id": 99, "current_identity": {"name": "Mystery Card"},
                "classification": "missing_documented_mtgjson",
                "reason": "Neither documented seller MTGJSON nor approved exact legacy catalog card_id is available.",
            }],
        }

    def fake_create_preview(**kwargs):
        calls.append("maintenance_preview")
        assert kwargs.get("fail_closed_on_unresolved") is False
        return fake_mirror_preview(unresolved_card_ids=[42])

    def fake_build_new_listing(session, mirror_preview, *args, **kwargs):
        calls.append("new_listing_preview")
        return fake_new_listing_preview()

    monkeypatch.setattr(main, "run_additive_mtgjson_backfill", fake_backfill)
    monkeypatch.setattr(main, "create_inventory_sync_preview", fake_create_preview)
    monkeypatch.setattr(main, "build_new_listing_preview", fake_build_new_listing)

    client = TestClient(main.app)
    response = client.post("/inventory-sync/perform-sync", follow_redirects=False)
    assert response.status_code == 303
    assert calls == ["backfill", "maintenance_preview", "new_listing_preview"]

    new_job_id = int(response.headers["location"].rsplit("/", 1)[-1])
    with Session(db) as session:
        jobs = session.query(InventorySyncJob).order_by(InventorySyncJob.id).all()
        assert [job.mode for job in jobs] == ["maintenance_preview", "new_listing_preview"]
        new_job = session.get(InventorySyncJob, new_job_id)
        assert new_job.mode == "new_listing_preview"
        stored = json.loads(new_job.snapshot_json)
        summary = stored["perform_sync_summary"]
        assert summary["backfilled_cards"] == 3
        assert summary["backfill_skipped"][0]["inventory_card_id"] == 99
        assert summary["still_unresolved"][0]["inventory_card_id"] == 42

    detail = client.get(f"/inventory-sync/{new_job_id}")
    assert detail.status_code == 200
    assert "Perform Sync Summary" in detail.text
    assert "backfilled" in detail.text.lower() or "3" in detail.text
    assert "Mystery Card" in detail.text
    assert "missing_documented_mtgjson" in detail.text


def test_perform_sync_writes_nothing_when_there_is_nothing_to_reconcile_or_publish(tmp_path, monkeypatch):
    """Perform Sync does write to Mana Pool now (quantity reconciliation,
    see test_perform_sync_applies_reconciliation_when_candidates_exist) --
    but only when there's actually something to reconcile, and new-listing
    publishing always requires its own separate confirm step regardless."""
    db = setup_db(tmp_path, monkeypatch)
    write_calls = []

    def fake_backfill(session, seller_loader, catalog_loader, operator_note=None):
        return {"updated_inventory_cards": 0, "updated_bindings": 0, "skipped": []}

    monkeypatch.setattr(main, "run_additive_mtgjson_backfill", fake_backfill)
    monkeypatch.setattr(
        main, "create_inventory_sync_preview", lambda **kwargs: fake_mirror_preview(),
    )
    monkeypatch.setattr(
        main, "build_new_listing_preview",
        lambda session, mirror_preview, *a, **k: fake_new_listing_preview(),
    )
    monkeypatch.setattr(
        main, "update_inventory_prices_by_product",
        lambda updates: write_calls.append(updates),
    )
    monkeypatch.setattr(
        main, "create_or_update_inventory_by_scryfall_id",
        lambda updates: write_calls.append(updates),
    )

    client = TestClient(main.app)
    response = client.post("/inventory-sync/perform-sync", follow_redirects=False)
    assert response.status_code == 303
    assert write_calls == []


def test_perform_sync_fails_closed_on_backfill_error(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)

    def failing_backfill(session, seller_loader, catalog_loader, operator_note=None):
        raise RuntimeError("Mana Pool seller inventory fetch failed")

    monkeypatch.setattr(main, "run_additive_mtgjson_backfill", failing_backfill)

    client = TestClient(main.app)
    response = client.post("/inventory-sync/perform-sync", follow_redirects=False)
    assert response.status_code == 409
    assert "Perform Sync failed closed" in response.text
    with Session(main.engine) as session:
        assert session.query(InventorySyncJob).count() == 0


def test_perform_sync_fails_closed_on_new_listing_preview_error(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)

    monkeypatch.setattr(
        main, "run_additive_mtgjson_backfill",
        lambda session, seller_loader, catalog_loader, operator_note=None: {
            "updated_inventory_cards": 0, "updated_bindings": 0, "skipped": [],
        },
    )
    monkeypatch.setattr(
        main, "create_inventory_sync_preview", lambda **kwargs: fake_mirror_preview(),
    )

    def failing_new_listing(session, mirror_preview, *args, **kwargs):
        raise RuntimeError("optimizer unavailable")

    monkeypatch.setattr(main, "build_new_listing_preview", failing_new_listing)

    client = TestClient(main.app)
    response = client.post("/inventory-sync/perform-sync", follow_redirects=False)
    assert response.status_code == 409
    assert "Perform Sync failed closed" in response.text
    # The maintenance preview job was already committed before the
    # new-listing step failed -- that's real, useful history, not
    # something to roll back.
    with Session(db) as session:
        jobs = session.query(InventorySyncJob).all()
        assert len(jobs) == 1
        assert jobs[0].mode == "maintenance_preview"


def test_perform_sync_shows_friendly_message_when_mana_pool_rate_limits_us(tmp_path, monkeypatch):
    """A 429 that survives manapool_service's own retry-with-backoff (i.e.
    Mana Pool is still rate-limiting us after several attempts) should
    read as an actionable, specific message -- not a raw exception dump --
    and should still make clear the backfill/maintenance steps already
    succeeded and were saved."""
    db = setup_db(tmp_path, monkeypatch)

    monkeypatch.setattr(
        main, "run_additive_mtgjson_backfill",
        lambda session, seller_loader, catalog_loader, operator_note=None: {
            "updated_inventory_cards": 0, "updated_bindings": 0, "skipped": [],
        },
    )
    monkeypatch.setattr(
        main, "create_inventory_sync_preview", lambda **kwargs: fake_mirror_preview(),
    )

    def rate_limited_new_listing(session, mirror_preview, *args, **kwargs):
        request = httpx.Request("POST", "https://manapool.com/api/v1/buyer/optimizer")
        response = httpx.Response(429, json={"status": 429, "message": "Rate limit exceeded"}, request=request)
        raise httpx.HTTPStatusError("429", request=request, response=response)

    monkeypatch.setattr(main, "build_new_listing_preview", rate_limited_new_listing)

    client = TestClient(main.app)
    response = client.post("/inventory-sync/perform-sync", follow_redirects=False)
    assert response.status_code == 409
    assert "still rate-limiting" in response.text
    assert "already" in response.text and "saved" in response.text
    # No raw exception text (e.g. "429 Too Many Requests") leaked into the page.
    assert "HTTPStatusError" not in response.text


def test_perform_sync_applies_reconciliation_when_candidates_exist(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    apply_calls = []
    _stub_backfill_and_preview(monkeypatch)
    monkeypatch.setattr(
        main, "build_reconciliation_preview",
        lambda session, mirror_preview: fake_reconciliation_preview(candidates=3, increase=2, decrease=1),
    )

    def fake_apply(session, preview, *args, **kwargs):
        apply_calls.append(preview)
        return fake_reconciliation_apply_result(
            updates=[{"product_id": "p1", "quantity": 5}, {"product_id": "p2", "quantity": 2}],
            excluded=[{"product_id": "p3"}],
        )

    monkeypatch.setattr(main, "apply_reconciliation_preview", fake_apply)
    monkeypatch.setattr(
        main, "build_new_listing_preview",
        lambda session, mirror_preview, *a, **k: fake_new_listing_preview(),
    )

    client = TestClient(main.app)
    response = client.post("/inventory-sync/perform-sync", follow_redirects=False)
    assert response.status_code == 303
    assert len(apply_calls) == 1

    new_job_id = int(response.headers["location"].rsplit("/", 1)[-1])
    with Session(db) as session:
        jobs = session.query(InventorySyncJob).order_by(InventorySyncJob.id).all()
        assert [job.mode for job in jobs] == [
            "maintenance_preview", "reconciliation_preview", "reconciliation_apply", "new_listing_preview",
        ]
        new_job = session.get(InventorySyncJob, new_job_id)
        stored = json.loads(new_job.snapshot_json)
        recon = stored["perform_sync_summary"]["reconciliation"]
        assert recon["candidates"] == 3
        assert recon["updated"] == 2
        assert recon["excluded"] == 1

    detail = client.get(f"/inventory-sync/{new_job_id}")
    assert "Quantity reconciliation" in detail.text
    assert "2</strong> Mana Pool listing(s) had their quantity" in detail.text


def test_perform_sync_skips_reconciliation_when_nothing_to_reconcile(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    _stub_backfill_and_preview(monkeypatch)
    monkeypatch.setattr(
        main, "build_reconciliation_preview",
        lambda session, mirror_preview: fake_reconciliation_preview(candidates=0),
    )

    def fail_apply(*args, **kwargs):
        raise AssertionError("apply_reconciliation_preview should not be called with zero candidates")

    monkeypatch.setattr(main, "apply_reconciliation_preview", fail_apply)
    monkeypatch.setattr(
        main, "build_new_listing_preview",
        lambda session, mirror_preview, *a, **k: fake_new_listing_preview(),
    )

    client = TestClient(main.app)
    response = client.post("/inventory-sync/perform-sync", follow_redirects=False)
    assert response.status_code == 303

    new_job_id = int(response.headers["location"].rsplit("/", 1)[-1])
    with Session(db) as session:
        jobs = session.query(InventorySyncJob).order_by(InventorySyncJob.id).all()
        assert [job.mode for job in jobs] == ["maintenance_preview", "new_listing_preview"]

    detail = client.get(f"/inventory-sync/{new_job_id}")
    assert "Nothing to reconcile" in detail.text


def test_perform_sync_fails_closed_on_reconciliation_error(tmp_path, monkeypatch):
    from inventory_reconciliation_service import InventoryReconciliationError

    db = setup_db(tmp_path, monkeypatch)
    _stub_backfill_and_preview(monkeypatch)
    monkeypatch.setattr(
        main, "build_reconciliation_preview",
        lambda session, mirror_preview: fake_reconciliation_preview(candidates=1),
    )

    def failing_apply(*args, **kwargs):
        raise InventoryReconciliationError("boom")

    monkeypatch.setattr(main, "apply_reconciliation_preview", failing_apply)

    client = TestClient(main.app)
    response = client.post("/inventory-sync/perform-sync", follow_redirects=False)
    assert response.status_code == 409
    assert "Perform Sync failed closed" in response.text

    with Session(db) as session:
        jobs = session.query(InventorySyncJob).order_by(InventorySyncJob.id).all()
        # maintenance_preview + reconciliation_preview committed before the
        # apply failure -- real, useful history, not rolled back.
        assert [job.mode for job in jobs] == ["maintenance_preview", "reconciliation_preview"]


def test_perform_sync_surfaces_deferred_orders_from_the_sync_cap(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(
        main, "run_additive_mtgjson_backfill",
        lambda session, seller_loader, catalog_loader, operator_note=None: {
            "updated_inventory_cards": 0, "updated_bindings": 0, "skipped": [],
        },
    )

    def fake_create_preview(**kwargs):
        preview = fake_mirror_preview()
        preview["order_ingestion"] = {
            "imported": 1, "already_known": 19, "failed": [], "deferred": 12,
        }
        return preview

    monkeypatch.setattr(main, "create_inventory_sync_preview", fake_create_preview)
    monkeypatch.setattr(
        main, "build_new_listing_preview",
        lambda session, mirror_preview, *a, **k: fake_new_listing_preview(),
    )

    client = TestClient(main.app)
    response = client.post("/inventory-sync/perform-sync", follow_redirects=False)
    assert response.status_code == 303
    new_job_id = int(response.headers["location"].rsplit("/", 1)[-1])

    detail = client.get(f"/inventory-sync/{new_job_id}")
    assert "Order sync" in detail.text
    assert "12</strong> order(s) deferred" in detail.text
    assert "click Perform Sync again" in detail.text
