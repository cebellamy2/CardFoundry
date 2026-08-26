import json
from datetime import datetime

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard, InventorySyncJob, RemoteProductBinding


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'new_listing_routes.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_maintenance_job(session, card_id):
    mirror_preview = {
        "preview_timestamp": "2026-08-15T00:00:00Z",
        "local_snapshot_hash": "local-hash", "remote_snapshot_hash": "remote-hash",
        "rows": [{
            "category": "local_only_requires_listing",
            "canonical_identity": {
                "mtgjson_id": "MTG-ALPHA", "language_id": "EN",
                "condition_id": "LP", "finish_id": "NF",
            },
            "local_contributing_card_ids": [card_id],
            "desired_quantity": 1,
            "reason": "Canonical local variant has no remote inventory record",
        }],
        "summary": {"categories": {"local_only_requires_listing": 1}},
    }
    job = InventorySyncJob(
        status="completed", mode="maintenance_preview",
        snapshot_json=json.dumps(mirror_preview),
    )
    session.add(job)
    session.flush()
    return job


def add_card(session, **overrides):
    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    values = {
        "batch_id": batch.id, "name": "Alpha", "set_code": "ONE", "collector_number": "1",
        "mtgjson_id": "MTG-ALPHA", "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
        "condition": "near_mint", "finish": "normal", "scryfall_id": "sf-alpha", "status": "available",
    }
    values.update(overrides)
    card = InventoryCard(**values)
    session.add(card)
    session.flush()
    return card


def test_new_listing_preview_route_prices_candidates_and_redirects(tmp_path, monkeypatch):
    """New-listing preview never calls the optimizer -- pricing falls
    through to the reviewed-inventory-price tier, using the card's own
    current_price."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, current_price=1.99)
        job = make_maintenance_job(session, card.id)
        session.commit()
        job_id = job.id

    def fail_if_called(cart, seller):
        raise AssertionError("the optimizer must never be called for first-time listing")

    monkeypatch.setattr(main, "optimize_exact_variant_batch_with_conflicts", fail_if_called)
    monkeypatch.setattr(main, "get_inventory_listings_by_ids", lambda ids: [])
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", lambda ids: {"data": []})

    client = TestClient(main.app)
    response = client.post(
        f"/inventory-sync/{job_id}/new-listings/preview", follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db) as session:
        new_job_id = int(response.headers["location"].rsplit("/", 1)[-1])
        new_job = session.get(InventorySyncJob, new_job_id)
        assert new_job.mode == "new_listing_preview"
        preview = json.loads(new_job.snapshot_json)
        assert preview["summary"]["priced"] == 1
        assert preview["rows"][0]["target_price_cents"] == 199
        assert preview["rows"][0]["price_source"] == "reviewed_inventory"


def test_new_listing_preview_route_shows_friendly_message_when_mana_pool_rate_limits_us(
    tmp_path, monkeypatch,
):
    """New-listing preview never calls the optimizer, but still calls the
    market-catalog endpoint for the market-price fallback tier -- a 429
    from there should get the same friendly treatment."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session)
        job = make_maintenance_job(session, card.id)
        session.commit()
        job_id = job.id

    def rate_limited_market_call(scryfall_ids):
        request = httpx.Request("GET", "https://manapool.com/api/v1/products/singles")
        response = httpx.Response(429, json={"status": 429, "message": "Rate limit exceeded"}, request=request)
        raise httpx.HTTPStatusError("429", request=request, response=response)

    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", rate_limited_market_call)

    client = TestClient(main.app)
    response = client.post(
        f"/inventory-sync/{job_id}/new-listings/preview", follow_redirects=False,
    )
    assert response.status_code == 409
    assert "still rate-limiting" in response.text
    assert "HTTPStatusError" not in response.text


def test_new_listing_preview_route_requires_maintenance_job(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/inventory-sync/999/new-listings/preview", follow_redirects=False)
    assert response.status_code == 404


def test_new_listing_apply_route_rejects_wrong_confirmation(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job = InventorySyncJob(
            status="completed", mode="new_listing_preview",
            snapshot_json=json.dumps({"rows": []}),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    client = TestClient(main.app)
    response = client.post(
        f"/inventory-sync/{job_id}/new-listings/apply",
        data={"confirmation": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_new_listing_apply_route_writes_and_reports_response(tmp_path, monkeypatch):
    """Apply never calls the optimizer -- fresh re-pricing at apply time
    lands on the same 199 via the reviewed-inventory-price tier (the
    card's own current_price), matching what was reviewed."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, current_price=1.99)
        priced_preview = {
            "rows": [{
                "key": ["MTG-ALPHA", "EN", "LP", "NF"],
                "identity": {
                    "name": "Alpha", "set_code": "ONE", "collector_number": "1",
                    "scryfall_id": "sf-alpha", "language_id": "EN",
                    "condition_id": "LP", "finish_id": "NF",
                },
                "desired_quantity": 1, "card_ids": [card.id], "path": "scryfall_id",
                "status": "priced", "target_price_cents": 199,
            }],
        }
        job = InventorySyncJob(
            status="completed", mode="new_listing_preview",
            snapshot_json=json.dumps(priced_preview),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity: [])
    monkeypatch.setattr(
        main, "create_or_update_inventory_by_scryfall_id",
        lambda updates: [{"inventory": [{"id": "inv-1", "quantity": 1, "price_cents": 199}], "skipped": []}],
    )

    def fail_if_called(cart, seller):
        raise AssertionError("the optimizer must never be called for first-time listing")

    monkeypatch.setattr(main, "optimize_exact_variant_batch_with_conflicts", fail_if_called)
    monkeypatch.setattr(main, "get_inventory_listings_by_ids", lambda ids: [])
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", lambda ids: {"data": []})

    client = TestClient(main.app)
    response = client.post(
        f"/inventory-sync/{job_id}/new-listings/apply",
        data={"confirmation": "PUBLISH NEW LISTINGS"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db) as session:
        apply_job_id = int(response.headers["location"].rsplit("/", 1)[-1])
        apply_job = session.get(InventorySyncJob, apply_job_id)
        assert apply_job.mode == "new_listing_apply"
        result = json.loads(apply_job.snapshot_json)
        assert result["responses"]["scryfall_id"][0]["inventory"][0]["id"] == "inv-1"

    detail_page = client.get(f"/inventory-sync/{apply_job_id}")
    assert detail_page.status_code == 200
    assert "inv-1" in detail_page.text


def test_new_listing_apply_shows_friendly_message_when_mana_pool_rate_limits_us(tmp_path, monkeypatch):
    """A 429 during apply's fresh re-price check (before any write) should
    read as an actionable, specific message -- not a raw exception dump
    crashing to a 500 -- and should make clear nothing was published."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session)
        priced_preview = {
            "rows": [{
                "key": ["MTG-ALPHA", "EN", "LP", "NF"],
                "identity": {
                    "name": "Alpha", "set_code": "ONE", "collector_number": "1",
                    "scryfall_id": "sf-alpha", "language_id": "EN",
                    "condition_id": "LP", "finish_id": "NF",
                },
                "desired_quantity": 1, "card_ids": [card.id], "path": "scryfall_id",
                "status": "priced", "target_price_cents": 199,
            }],
        }
        job = InventorySyncJob(
            status="completed", mode="new_listing_preview",
            snapshot_json=json.dumps(priced_preview),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity: [])

    def rate_limited_market_call(scryfall_ids):
        request = httpx.Request("GET", "https://manapool.com/api/v1/products/singles")
        response = httpx.Response(429, json={"status": 429, "message": "Rate limit exceeded"}, request=request)
        raise httpx.HTTPStatusError("429", request=request, response=response)

    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", rate_limited_market_call)

    client = TestClient(main.app)
    response = client.post(
        f"/inventory-sync/{job_id}/new-listings/apply",
        data={"confirmation": "PUBLISH NEW LISTINGS"},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert "still rate-limiting" in response.text
    assert "nothing was published" in response.text
    assert "HTTPStatusError" not in response.text

    with Session(db) as session:
        assert session.query(InventorySyncJob).filter_by(mode="new_listing_apply").count() == 0


def test_confirm_mtgjson_override_route_requires_a_note(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        binding = RemoteProductBinding(
            provider="manapool", product_type="mtg_single", product_id="product-override",
            local_card_ids_json=json.dumps([1]), requested_identity_json="{}",
            scryfall_id="sf-alpha", mtgjson_id=None, language_id="EN", condition_id="LP",
            finish_id="NF", set_code="ONE", collector_number="1", binding_status="validated",
            validated_at=datetime(2026, 8, 14), evidence_hash="override-hash", evidence_json="{}",
        )
        session.add(binding)
        session.commit()
        binding_id = binding.id

    client = TestClient(main.app)
    # A whitespace-only note (rather than a literal empty string) reliably
    # exercises the server's own blank-reason rejection here -- an empty
    # string value for the sole form field doesn't survive TestClient's
    # form encoding to reach the server at all.
    response = client.post(
        f"/remote-bindings/{binding_id}/confirm-mtgjson-override", data={"note": "   "},
    )
    assert response.status_code == 409
    assert "reason is required" in response.text

    response = client.post(
        f"/remote-bindings/{binding_id}/confirm-mtgjson-override",
        data={"note": "Japanese foil, no MTGJSON documented"},
    )
    assert response.status_code == 200
    assert "Override Confirmed" in response.text

    with Session(db) as session:
        reloaded = session.get(RemoteProductBinding, binding_id)
        assert reloaded.mtgjson_override_confirmed_at is not None
        assert reloaded.mtgjson_override_note == "Japanese foil, no MTGJSON documented"
