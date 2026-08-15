import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import AppSetting, Base, Batch, InventoryCard, InventorySyncJob


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'reconciliation_routes.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def add_card(session, batch, imported_at):
    card = InventoryCard(
        batch_id=batch.id, name="Alpha", set_code="ONE", collector_number="1",
        mtgjson_id="MTG-ALPHA", language_id="EN", condition_id="LP", finish_id="NF",
        condition="near_mint", finish="normal", scryfall_id="sf-alpha", status="available",
        imported_at=imported_at,
    )
    session.add(card)
    session.flush()
    return card


def make_maintenance_job(session, card_id):
    mirror_preview = {
        "preview_timestamp": "2026-08-15T00:00:00Z",
        "local_snapshot_hash": "local-hash", "remote_snapshot_hash": "remote-hash",
        "rows": [{
            "category": "increase_quantity",
            "canonical_identity": {
                "mtgjson_id": "MTG-ALPHA", "language_id": "EN",
                "condition_id": "LP", "finish_id": "NF",
            },
            "local_contributing_card_ids": [card_id],
            "desired_quantity": 2,
            "current_remote_quantity": 1,
            "effective_as_of": "2026-08-01T00:00:00Z",
            "remote_product_id": "mp-product",
        }],
        "summary": {"categories": {"increase_quantity": 1}},
    }
    job = InventorySyncJob(
        status="completed", mode="maintenance_preview",
        snapshot_json=json.dumps(mirror_preview),
    )
    session.add(job)
    session.flush()
    return job


def test_reconciliation_preview_route_finds_traceable_increase_and_redirects(tmp_path, monkeypatch):
    from datetime import datetime

    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = Batch(batch_code="NEW")
        session.add(batch)
        session.flush()
        card = add_card(session, batch, datetime(2026, 8, 5))
        job = make_maintenance_job(session, card.id)
        session.commit()
        job_id = job.id

    client = TestClient(main.app)
    response = client.post(
        f"/inventory-sync/{job_id}/reconcile/preview", follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db) as session:
        new_job_id = int(response.headers["location"].rsplit("/", 1)[-1])
        new_job = session.get(InventorySyncJob, new_job_id)
        assert new_job.mode == "reconciliation_preview"
        preview = json.loads(new_job.snapshot_json)
        assert preview["summary"]["candidates"] == 1
        assert preview["rows"][0]["direction"] == "increase"

    detail_page = client.get(f"/inventory-sync/{new_job_id}")
    assert detail_page.status_code == 200
    assert "Quantity Reconciliation Preview" in detail_page.text


def test_reconciliation_preview_route_requires_maintenance_job(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/inventory-sync/999/reconcile/preview", follow_redirects=False)
    assert response.status_code == 404


def test_reconciliation_apply_route_rejects_wrong_confirmation(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job = InventorySyncJob(
            status="completed", mode="reconciliation_preview",
            snapshot_json=json.dumps({"rows": []}),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    client = TestClient(main.app)
    response = client.post(
        f"/inventory-sync/{job_id}/reconcile/apply",
        data={"confirmation": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_reconciliation_apply_route_writes_and_reports_response(tmp_path, monkeypatch):
    from datetime import datetime

    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(AppSetting(key=main.GO_LIVE_SETTING_KEY, value="2026-01-01T00:00:00Z"))
        baseline_batch = Batch(batch_code="OLD")
        session.add(baseline_batch)
        session.flush()
        add_card(session, baseline_batch, datetime(2026, 7, 1))
        batch = Batch(batch_code="NEW")
        session.add(batch)
        session.flush()
        card = add_card(session, batch, datetime(2026, 8, 5))
        session.flush()

        from inventory_reconciliation_service import extract_reconciliation_candidates
        row = {
            "category": "increase_quantity",
            "canonical_identity": {
                "mtgjson_id": "MTG-ALPHA", "language_id": "EN",
                "condition_id": "LP", "finish_id": "NF",
            },
            "local_contributing_card_ids": [card.id],
            "desired_quantity": 2,
            "current_remote_quantity": 1,
            "effective_as_of": "2026-08-01T00:00:00Z",
            "remote_product_id": "mp-product",
        }
        candidates, _ = extract_reconciliation_candidates(session, {"rows": [row]})
        preview = {"rows": [{**candidates[0], "status": "eligible"}]}
        job = InventorySyncJob(
            status="completed", mode="reconciliation_preview",
            snapshot_json=json.dumps(preview),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    monkeypatch.setattr(main, "get_seller_orders", lambda since: {"orders": []})
    monkeypatch.setattr(main, "get_seller_order", lambda order_id: {})
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity: [
        {"product_id": "mp-product", "quantity": 1},
    ])

    written = {}

    def fake_writer(updates):
        written["updates"] = updates
        # update_inventory_prices_by_product returns a LIST of one response
        # per chunk (chunked at 2000), not a single dict.
        return [{
            "inventory": [{
                "product_id": "mp-product", "quantity": updates[0]["quantity"],
                "product": {"single": {"name": "Alpha"}},
            }],
            "skipped": [],
        }]

    monkeypatch.setattr(main, "update_inventory_prices_by_product", fake_writer)

    client = TestClient(main.app)
    response = client.post(
        f"/inventory-sync/{job_id}/reconcile/apply",
        data={"confirmation": "RECONCILE QUANTITIES"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert written["updates"][0]["quantity"] == 2

    with Session(db) as session:
        apply_job_id = int(response.headers["location"].rsplit("/", 1)[-1])
        apply_job = session.get(InventorySyncJob, apply_job_id)
        assert apply_job.mode == "reconciliation_apply"
        result = json.loads(apply_job.snapshot_json)
        assert result["updates"][0]["quantity"] == 2

    detail_page = client.get(f"/inventory-sync/{apply_job_id}")
    assert detail_page.status_code == 200
    assert "Alpha" in detail_page.text


def test_reconciliation_apply_route_requires_go_live_setting(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job = InventorySyncJob(
            status="completed", mode="reconciliation_preview",
            snapshot_json=json.dumps({"rows": [{"status": "eligible", "direction": "decrease",
                                                   "canonical_identity": {}, "product_id": "x"}]}),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    client = TestClient(main.app)
    response = client.post(
        f"/inventory-sync/{job_id}/reconcile/apply",
        data={"confirmation": "RECONCILE QUANTITIES"},
        follow_redirects=False,
    )
    assert response.status_code == 400
