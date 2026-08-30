import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, PricingJob


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'competitor_pricing_apply.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def change_row(product_id="p-1", competitor_inventory_id="comp-1", current_price=100,
                target_price=85, action="decrease"):
    return {
        "inventory_id": "inv-1", "product_id": product_id, "name": "Alpha",
        "set_code": "SET", "collector_number": "1", "language_id": "EN",
        "condition_id": "LP", "finish_id": "NF", "quantity": 1,
        "current_price": current_price, "competitor_inventory_id": competitor_inventory_id,
        "competitor_product_id": "comp-product-1", "competitor_price": 90,
        "competitor_condition": "LP", "competitor_language": "EN", "competitor_finish": "NF",
        "competitor_effective_as_of": "2026-08-16T00:00:00Z",
        "allowed_conditions": ["LP", "NM"], "target_price": target_price,
        "change_cents": target_price - current_price, "action": action,
        "validation_status": "passed", "validation_reason": "ok",
        "floor_applied": False, "price_classification": "competitor_undercut",
        "price_source": "competitor", "pricing_evidence_hash": "hash", "preview_only": True,
    }


def make_completed_preview_job(session, *, changes=None, increases=0, decreases=1):
    changes = changes if changes is not None else [change_row()]
    preview = {
        "changes": changes, "holds": [], "audit_rows": changes,
        "progress": {"stage": "complete"},
        "summary": {
            "seller_items": 1, "deduplicated_requests": 1, "optimizer_batches": 1,
            "optimizer_calls": 1, "optimizer_failures": 0, "optimizer_retries": 0,
            "listing_calls": 1, "changes": len(changes), "increases": increases,
            "decreases": decreases, "holds": 0, "skipped": 0, "floor_applied_count": 0,
            "total_change_cents": sum(c["change_cents"] for c in changes),
            "verified_increases": increases,
        },
    }
    job = PricingJob(
        external_job_id=None,
        action="competitor_only_full_preview",
        status="completed",
        request_json=json.dumps({"undercut_cents": 5, "floor_cents": 65, "preview_only": True}),
        response_json=json.dumps({"preview_only": True, "preview": preview}),
    )
    session.add(job)
    session.flush()
    return job


def fresh_competitor_listing(inventory_id="comp-1", product_id="comp-product-1", price_cents=90):
    return {
        "id": inventory_id, "product_id": product_id, "quantity": 1,
        "price_cents": price_cents, "effective_as_of": "2026-08-16T01:00:00Z",
        "product": {"single": {
            "name": "Alpha", "set": "SET", "number": "1", "language_id": "EN",
            "condition_id": "LP", "finish_id": "NF",
        }},
    }


def test_apply_route_rejects_wrong_confirmation(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job = make_completed_preview_job(session)
        session.commit()
        job_id = job.id

    client = TestClient(main.app)
    response = client.post(
        f"/pricing/full-competitor-preview/{job_id}/apply",
        data={"confirmation": "wrong"},
    )
    assert response.status_code == 400


def test_apply_route_requires_completed_preview(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/pricing/full-competitor-preview/999/apply",
        data={"confirmation": "APPLY COMPETITIVE PRICES"},
    )
    assert response.status_code == 404


def test_apply_route_writes_and_redirects_to_result(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job = make_completed_preview_job(session)
        session.commit()
        job_id = job.id

    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity: [
        {"product_id": "p-1", "quantity": 1, "product_type": "mtg_single",
         "product": {"single": {"mtgjson_id": "mtg-1", "language_id": "EN",
                                  "condition_id": "LP", "finish_id": "NF"}}},
    ])
    monkeypatch.setattr(
        main, "get_inventory_listings_by_ids",
        lambda ids: [fresh_competitor_listing()],
    )
    written = {}

    def fake_writer(updates):
        written["updates"] = updates
        return [{
            "inventory": [{
                "product_id": updates[0]["product_id"], "price_cents": updates[0]["price_cents"],
                "product": {"single": {"name": "Alpha"}},
            }],
            "skipped": [],
        }]

    monkeypatch.setattr(main, "update_inventory_prices_by_product", fake_writer)

    # sellable_remote_product_ids resolves via local InventoryCard matching;
    # stub it directly since this route test isn't exercising that machinery.
    monkeypatch.setattr(main, "sellable_remote_product_ids", lambda session, inv: {"p-1"})

    client = TestClient(main.app)
    response = client.post(
        f"/pricing/full-competitor-preview/{job_id}/apply",
        data={"confirmation": "APPLY COMPETITIVE PRICES"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert written["updates"] == [{
        "product_type": "mtg_single", "product_id": "p-1", "price_cents": 85, "quantity": None,
    }]

    apply_job_id = int(response.headers["location"].rsplit("/", 1)[-1])
    with Session(db) as session:
        apply_job = session.get(PricingJob, apply_job_id)
        assert apply_job.action == "competitor_only_full_apply"
        result = json.loads(apply_job.response_json)
        assert result["updates"] == written["updates"]

    detail = client.get(f"/pricing/full-competitor-apply/{apply_job_id}")
    assert detail.status_code == 200
    assert "Alpha" in detail.text
    assert "$0.85" in detail.text


def test_apply_route_excludes_stale_row_without_blocking_others(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job = make_completed_preview_job(session, changes=[
            change_row(product_id="p-1", competitor_inventory_id="comp-1", target_price=85),
            change_row(product_id="p-2", competitor_inventory_id="comp-2", target_price=45,
                       current_price=60),
        ], decreases=2)
        session.commit()
        job_id = job.id

    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity: [])
    monkeypatch.setattr(main, "sellable_remote_product_ids", lambda session, inv: {"p-1", "p-2"})
    # comp-2's listing is gone (sold out / delisted) by apply time; comp-1 is fine.
    monkeypatch.setattr(
        main, "get_inventory_listings_by_ids",
        lambda ids: [fresh_competitor_listing(inventory_id="comp-1")],
    )

    written = {}

    def fake_writer(updates):
        written["updates"] = updates
        return [{"inventory": [
            {"product_id": u["product_id"], "price_cents": u["price_cents"],
             "product": {"single": {"name": "Alpha"}}}
            for u in updates
        ], "skipped": []}]

    monkeypatch.setattr(main, "update_inventory_prices_by_product", fake_writer)

    client = TestClient(main.app)
    response = client.post(
        f"/pricing/full-competitor-preview/{job_id}/apply",
        data={"confirmation": "APPLY COMPETITIVE PRICES"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(written["updates"]) == 1
    assert written["updates"][0]["product_id"] == "p-1"

    apply_job_id = int(response.headers["location"].rsplit("/", 1)[-1])
    detail = client.get(f"/pricing/full-competitor-apply/{apply_job_id}")
    assert detail.status_code == 200
    assert "Not Applied (1)" in detail.text
    assert "Competitor listing no longer exists" in detail.text


def test_apply_route_refuses_when_nothing_left_to_apply(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job = make_completed_preview_job(session)
        session.commit()
        job_id = job.id

    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity: [])
    monkeypatch.setattr(main, "sellable_remote_product_ids", lambda session, inv: set())
    monkeypatch.setattr(main, "get_inventory_listings_by_ids", lambda ids: [fresh_competitor_listing()])

    client = TestClient(main.app)
    response = client.post(
        f"/pricing/full-competitor-preview/{job_id}/apply",
        data={"confirmation": "APPLY COMPETITIVE PRICES"},
    )
    assert response.status_code == 200
    # UX epic item 21: heading now matches the shared refused-correction
    # template's title-case convention ("Competitive Prices Not
    # Applied", not the old ad-hoc "Not applied.").
    assert "Competitive Prices Not Applied" in response.text
    assert "local sellability" in response.text
    assert "pricing basis changed since preview" in response.text
    assert 'href="/pricing/full-competitor-preview/' in response.text


def test_orphaned_pricing_flow_routes_are_gone(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    assert client.post("/pricing/preview", data={}).status_code == 404
    assert client.post("/pricing/apply", data={}).status_code == 404
