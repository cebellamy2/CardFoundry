import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard, InventorySyncJob


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'exceptions.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def add_card(session, **overrides):
    batch = Batch(batch_code=overrides.pop("batch_code", "B1"))
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
    return card, batch


def fake_mirror_preview(rows=None, unresolved_card_ids=None):
    return {
        "rows": rows or [],
        "unresolved_card_ids": unresolved_card_ids or [],
        "order_ingestion": None,
    }


def test_exceptions_page_shows_never_published_and_lets_it_be_published(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card, _batch = add_card(session)
        session.commit()
        card_id = card.id

    row = {
        "category": "local_only_requires_listing",
        "name": "Alpha",
        "canonical_identity": {
            "mtgjson_id": "MTG-ALPHA", "language_id": "EN",
            "condition_id": "LP", "finish_id": "NF",
        },
        "local_contributing_card_ids": [card_id],
        "desired_quantity": 1,
    }
    monkeypatch.setattr(
        main, "create_exceptions_review_preview", lambda **kwargs: fake_mirror_preview([row]),
    )

    client = TestClient(main.app)
    response = client.get("/inventory-sync/exceptions")
    assert response.status_code == 200
    assert "Never Published on Mana Pool (1)" in response.text
    assert "MTG-ALPHA" in response.text
    assert "<td>Alpha</td>" in response.text
    assert 'name="mtgjson_id" value="MTG-ALPHA"' in response.text


def test_exceptions_page_shows_unresolved_and_ambiguous_and_mismatch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        unresolved_card, _batch = add_card(session, name="Unresolved Card", mtgjson_id=None)
        session.commit()
        unresolved_id = unresolved_card.id

    rows = [
        {
            "category": "ambiguous_identity",
            "name": "Local Name / Remote Name",
            "canonical_identity": {
                "mtgjson_id": "MTG-BETA", "language_id": "EN",
                "condition_id": "LP", "finish_id": "NF",
            },
            "local_contributing_card_ids": [unresolved_id],
            "desired_quantity": 1,
            "reason": "Cross-check metadata conflicts",
        },
        {
            "category": "increase_quantity",
            "name": "Gamma Card",
            "canonical_identity": {
                "mtgjson_id": "MTG-GAMMA", "language_id": "EN",
                "condition_id": "LP", "finish_id": "NF",
            },
            "local_contributing_card_ids": [],
            "desired_quantity": 3,
            "current_remote_quantity": 1,
            "remote_product_id": "product-gamma",
            "effective_as_of": "2026-08-01T00:00:00Z",
        },
    ]
    monkeypatch.setattr(
        main, "create_exceptions_review_preview",
        lambda **kwargs: fake_mirror_preview(rows, unresolved_card_ids=[unresolved_id]),
    )

    client = TestClient(main.app)
    response = client.get("/inventory-sync/exceptions")
    assert response.status_code == 200
    assert "No Canonical Identity (1)" in response.text
    assert "Unresolved Card" in response.text
    assert "Ambiguous Identity (1)" in response.text
    assert "Local Name / Remote Name" in response.text
    assert "Cross-check metadata conflicts" in response.text
    # the ambiguous row's Card(s) column links by name, not a bare ID
    assert f'<a href="/inventory/{unresolved_id}/edit">Unresolved Card (#{unresolved_id})</a>' in response.text
    assert "Quantity Mismatch Reconciliation Can't Auto-Fix (1)" in response.text
    assert "MTG-GAMMA" in response.text
    assert "Gamma Card" in response.text


def test_exceptions_page_links_from_inventory_sync(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory-sync")
    assert response.status_code == 200
    assert 'action="/inventory-sync/exceptions"' in response.text


def test_exceptions_publish_creates_scoped_maintenance_preview_job(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card, _batch = add_card(session)
        session.commit()
        card_id = card.id

    client = TestClient(main.app)
    response = client.post(
        "/inventory-sync/exceptions/publish",
        data={
            "mtgjson_id": "MTG-ALPHA", "language_id": "EN",
            "condition_id": "LP", "finish_id": "NF",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db) as session:
        job_id = int(response.headers["location"].rsplit("/", 1)[-1])
        job = session.get(InventorySyncJob, job_id)
        assert job.mode == "maintenance_preview"
        preview = json.loads(job.snapshot_json)
        assert preview["rows"][0]["local_contributing_card_ids"] == [card_id]
        assert preview["summary"]["categories"]["local_only_requires_listing"] == 1

    detail = client.get(f"/inventory-sync/{job_id}")
    assert detail.status_code == 200
    assert "Price New Listings" in detail.text


def test_exceptions_publish_refuses_when_nothing_left_to_publish(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).post(
        "/inventory-sync/exceptions/publish",
        data={
            "mtgjson_id": "MTG-NOTHING", "language_id": "EN",
            "condition_id": "LP", "finish_id": "NF",
        },
    )
    assert response.status_code == 409
    assert "Nothing to Publish" in response.text
