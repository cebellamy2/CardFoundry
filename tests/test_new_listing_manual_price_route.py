import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard, InventorySyncJob, ManualPriceOverride


IDENTITY = {
    "name": "Aang's Iceberg", "set_code": "PTLA", "collector_number": "5S",
    "scryfall_id": "sf-iceberg", "catalog_scryfall_id": "sf-iceberg",
    "language_id": "EN", "condition_id": "LP", "finish_id": "FO",
    "mtgjson_id": "MTGJSON-ICEBERG",
}


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'manual_price_route.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def seed_held_job(db, *, row_overrides=None):
    with Session(db) as session:
        batch = Batch(batch_code="B1")
        session.add(batch)
        session.flush()
        card = InventoryCard(
            batch_id=batch.id, name=IDENTITY["name"], set_code=IDENTITY["set_code"],
            collector_number=IDENTITY["collector_number"], scryfall_id=IDENTITY["scryfall_id"],
            language_id=IDENTITY["language_id"], condition_id=IDENTITY["condition_id"],
            finish_id=IDENTITY["finish_id"], mtgjson_id=IDENTITY["mtgjson_id"], status="available",
        )
        session.add(card)
        session.flush()
        row = {
            "key": [IDENTITY["mtgjson_id"], "EN", "LP", "FO"], "identity": IDENTITY,
            "card_ids": [card.id], "path": "scryfall_id", "desired_quantity": 1,
            "status": "hold", "price_classification": "hold_no_price_evidence",
            "reason": "No seller-excluded competitor satisfies this request",
            "target_price_cents": None, "competitor_inventory_id": None,
            "market_evidence": None, "evidence_hash": "held-row-hash",
        }
        row.update(row_overrides or {})
        job = InventorySyncJob(
            status="completed", mode="new_listing_preview",
            snapshot_json=json.dumps({
                "rows": [row],
                "summary": {"candidates": 1, "priced": 0, "held": 1, "excluded": 0},
            }),
        )
        session.add(job)
        session.commit()
        return job.id, card.id


def test_manual_price_link_appears_only_for_eligible_held_rows(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    job_id, _ = seed_held_job(db)
    client = TestClient(main.app)

    response = client.get(f"/inventory-sync/{job_id}")
    assert response.status_code == 200
    assert f"/inventory-sync/{job_id}/new-listings/manual-price/held-row-hash" in response.text
    assert "Set Manual Price" in response.text


def test_manual_price_link_absent_for_priced_rows(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    job_id, _ = seed_held_job(db, row_overrides={
        "status": "priced", "price_classification": "competitor_undercut",
        "target_price_cents": 199,
    })
    client = TestClient(main.app)

    response = client.get(f"/inventory-sync/{job_id}")
    assert "Set Manual Price" not in response.text


def test_manual_price_review_page_shows_identity(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    job_id, _ = seed_held_job(db)
    client = TestClient(main.app)

    response = client.get(f"/inventory-sync/{job_id}/new-listings/manual-price/held-row-hash")
    assert response.status_code == 200
    assert "Aang" in response.text
    assert "PTLA" in response.text
    assert 'name="expected_identity_hash"' in response.text


def test_manual_price_review_rejects_wrong_row_hash(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    job_id, _ = seed_held_job(db)
    client = TestClient(main.app)

    response = client.get(f"/inventory-sync/{job_id}/new-listings/manual-price/wrong-hash")
    assert response.status_code == 409


def test_manual_price_review_rejects_non_new_listing_job(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    job_id, _ = seed_held_job(db)
    with Session(db) as session:
        session.get(InventorySyncJob, job_id).mode = "maintenance_preview"
        session.commit()
    client = TestClient(main.app)

    response = client.get(f"/inventory-sync/{job_id}/new-listings/manual-price/held-row-hash")
    assert response.status_code == 409


def test_save_manual_price_creates_override(tmp_path, monkeypatch):
    from manual_price_override_service import identity_hash

    db = setup_db(tmp_path, monkeypatch)
    job_id, _ = seed_held_job(db)
    client = TestClient(main.app)

    response = client.post(
        f"/inventory-sync/{job_id}/new-listings/manual-price/held-row-hash",
        data={
            "manual_price_dollars": "1.25", "note": "No competitor or market evidence exists",
            "confirmation": "SET MANUAL INITIAL PRICE",
            "expected_identity_hash": identity_hash(IDENTITY),
        },
    )
    assert response.status_code == 200
    assert "$1.25" in response.text

    with Session(db) as session:
        override = session.query(ManualPriceOverride).one()
        assert override.manual_price_cents == 125
        assert override.identity_hash == identity_hash(IDENTITY)
        assert override.remote_product_binding_id is None
        assert override.status == "active"


def test_save_manual_price_rejects_wrong_confirmation(tmp_path, monkeypatch):
    from manual_price_override_service import identity_hash

    db = setup_db(tmp_path, monkeypatch)
    job_id, _ = seed_held_job(db)
    client = TestClient(main.app)

    response = client.post(
        f"/inventory-sync/{job_id}/new-listings/manual-price/held-row-hash",
        data={
            "manual_price_dollars": "1.25", "note": "note",
            "confirmation": "wrong phrase",
            "expected_identity_hash": identity_hash(IDENTITY),
        },
    )
    assert response.status_code == 400
    with Session(db) as session:
        assert session.query(ManualPriceOverride).count() == 0


def test_save_manual_price_rejects_stale_identity_hash(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    job_id, _ = seed_held_job(db)
    client = TestClient(main.app)

    response = client.post(
        f"/inventory-sync/{job_id}/new-listings/manual-price/held-row-hash",
        data={
            "manual_price_dollars": "1.25", "note": "note",
            "confirmation": "SET MANUAL INITIAL PRICE",
            "expected_identity_hash": "stale-hash",
        },
    )
    assert response.status_code == 409
    with Session(db) as session:
        assert session.query(ManualPriceOverride).count() == 0


def test_manual_override_lets_the_row_publish_on_a_fresh_preview_rebuild(tmp_path, monkeypatch):
    """End-to-end: a held candidate with no competitor and no market price
    gets a manual price, and a fresh preview rebuild (the operator's next
    step, per the saved-evidence page's own instruction) picks it up and
    marks it priced -- proving the override actually reaches publish
    eligibility, not just that a database row gets created."""
    from manual_price_override_service import identity_hash

    db = setup_db(tmp_path, monkeypatch)
    job_id, card_id = seed_held_job(db)

    # Point the maintenance-mode mirror preview this new-listing preview
    # would rebuild from at the same held candidate.
    with Session(db) as session:
        maintenance_job = InventorySyncJob(
            status="completed", mode="maintenance_preview",
            snapshot_json=json.dumps({
                "rows": [{
                    "category": "local_only_requires_listing",
                    "canonical_identity": {
                        "mtgjson_id": IDENTITY["mtgjson_id"], "language_id": "EN",
                        "condition_id": "LP", "finish_id": "FO",
                    },
                    "local_contributing_card_ids": [card_id],
                    "desired_quantity": 1,
                }],
            }),
        )
        session.add(maintenance_job)
        session.commit()
        maintenance_job_id = maintenance_job.id

    # No seller-excluded competitor and no market price -- the real
    # conditions that produced the original hold, so the fresh rebuild
    # naturally re-derives it and the override is what changes the outcome.
    monkeypatch.setattr(
        main, "optimize_exact_variant_batch_with_conflicts",
        lambda cart, seller_id: {"cart": [], "_conflicts": [{"item": {"index": 0}}]},
    )
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", lambda ids: {"data": []})
    monkeypatch.setattr(main, "get_inventory_listings_by_ids", lambda ids: [])

    client = TestClient(main.app)
    save_response = client.post(
        f"/inventory-sync/{job_id}/new-listings/manual-price/held-row-hash",
        data={
            "manual_price_dollars": "1.25", "note": "No competitor or market evidence exists",
            "confirmation": "SET MANUAL INITIAL PRICE",
            "expected_identity_hash": identity_hash(IDENTITY),
        },
    )
    assert save_response.status_code == 200

    rebuild_response = client.post(
        f"/inventory-sync/{maintenance_job_id}/new-listings/preview", follow_redirects=False,
    )
    assert rebuild_response.status_code == 303
    new_job_id = int(rebuild_response.headers["location"].rsplit("/", 1)[-1])

    with Session(db) as session:
        new_job = session.get(InventorySyncJob, new_job_id)
        rebuilt = json.loads(new_job.snapshot_json)
        row = rebuilt["rows"][0]
        assert row["status"] == "priced"
        assert row["price_classification"] == "manual_price_override"
        assert row["target_price_cents"] == 125
