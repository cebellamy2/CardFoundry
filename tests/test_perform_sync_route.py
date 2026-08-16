import json

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


def test_perform_sync_never_writes_to_mana_pool(tmp_path, monkeypatch):
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
