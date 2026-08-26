import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import inventory_sync_workflow
from inventory_sync_workflow import (
    create_batch_scoped_mirror_preview,
    create_exceptions_review_preview,
    create_inventory_sync_preview,
)
from models import AppSetting, Base, Batch, InventoryCard, RemoteProductBinding


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'sync_workflow.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(database, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(inventory_sync_workflow, "engine", db)
    with Session(db) as session:
        session.add(AppSetting(key="manapool_go_live_at", value="2026-01-01T00:00:00Z"))
        session.commit()
    return db


def add_unresolved_card(session):
    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name="Alpha", mtgjson_id=None, language_id="EN",
        condition_id="LP", finish_id="NF", status="available",
    )
    session.add(card)
    session.flush()
    return card


def test_default_fails_closed_on_unresolved_identity(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_unresolved_card(session)
        session.commit()

    try:
        create_inventory_sync_preview(
            orders_loader=lambda since: {"orders": []},
            detail_loader=lambda order_id: {},
            inventory_loader=lambda min_quantity: [],
        )
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "canonical MTGJSON identity" in str(exc)


def test_permissive_mode_skips_and_reports_unresolved_identity(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_unresolved_card(session)
        session.commit()
        card_id = card.id

    preview = create_inventory_sync_preview(
        orders_loader=lambda since: {"orders": []},
        detail_loader=lambda order_id: {},
        inventory_loader=lambda min_quantity: [],
        fail_closed_on_unresolved=False,
    )
    assert preview["unresolved_card_ids"] == [card_id]


def test_confirmed_mtgjson_override_lists_the_card_instead_of_blocking_it(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_unresolved_card(session)
        session.add(RemoteProductBinding(
            provider="manapool", product_type="mtg_single", product_id="product-override",
            local_card_ids_json=json.dumps([card.id]),
            requested_identity_json="{}", scryfall_id="sf-alpha", mtgjson_id=None,
            language_id="EN", condition_id="LP", finish_id="NF", set_code="SET",
            collector_number="1", binding_status="validated", validated_at=datetime(2026, 8, 14),
            evidence_hash="override-hash", evidence_json="{}",
            mtgjson_override_confirmed_at=datetime(2026, 8, 19),
            mtgjson_override_note="Japanese foil, no MTGJSON documented",
        ))
        session.commit()

    preview = create_inventory_sync_preview(
        orders_loader=lambda since: {"orders": []},
        detail_loader=lambda order_id: {},
        inventory_loader=lambda min_quantity: [],
    )
    assert preview["unresolved_card_ids"] == []
    assert {row["category"] for row in preview["rows"]} == {"local_only_requires_listing"}


def add_resolved_card(session, batch_code, mtgjson_id):
    batch = Batch(batch_code=batch_code)
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name="Alpha", set_code="ONE", collector_number="1",
        scryfall_id=f"sf-{mtgjson_id}", mtgjson_id=mtgjson_id, language_id="EN",
        condition_id="LP", finish_id="NF", condition="LP", finish="normal",
        status="available",
    )
    session.add(card)
    session.flush()
    return batch, card


def test_batch_scoped_preview_only_includes_selected_batches(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch_a, card_a = add_resolved_card(session, "A1", "mtg-alpha")
        batch_b, card_b = add_resolved_card(session, "B1", "mtg-beta")
        session.commit()
        batch_a_id, batch_b_id = batch_a.id, batch_b.id
        card_a_id, card_b_id = card_a.id, card_b.id

    preview = create_batch_scoped_mirror_preview(
        [batch_a_id], inventory_loader=lambda min_quantity: [],
    )
    card_ids = {
        card_id
        for row in preview["rows"]
        for card_id in row.get("local_contributing_card_ids", [])
    }
    assert card_a_id in card_ids
    assert card_b_id not in card_ids
    assert preview["order_ingestion"] is None
    assert preview["scoped_batch_ids"] == [batch_a_id]


def test_batch_scoped_preview_requires_at_least_one_batch():
    with pytest.raises(ValueError, match="At least one batch"):
        create_batch_scoped_mirror_preview([])


def test_batch_scoped_preview_never_calls_an_orders_endpoint(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch, card = add_resolved_card(session, "A1", "mtg-alpha")
        session.commit()
        batch_id = batch.id

    # No orders_loader/detail_loader parameter exists on this function at
    # all -- if it tried to sync orders, this call would TypeError.
    preview = create_batch_scoped_mirror_preview(
        [batch_id], inventory_loader=lambda min_quantity: [],
    )
    assert preview["order_ingestion"] is None


def test_exceptions_review_preview_includes_every_batch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _batch_a, card_a = add_resolved_card(session, "A1", "mtg-alpha")
        _batch_b, card_b = add_resolved_card(session, "B1", "mtg-beta")
        session.commit()
        card_a_id, card_b_id = card_a.id, card_b.id

    preview = create_exceptions_review_preview(inventory_loader=lambda min_quantity: [])
    card_ids = {
        card_id
        for row in preview["rows"]
        for card_id in row.get("local_contributing_card_ids", [])
    }
    assert card_a_id in card_ids
    assert card_b_id in card_ids
    assert preview["order_ingestion"] is None


def test_exceptions_review_preview_never_calls_an_orders_endpoint(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_resolved_card(session, "A1", "mtg-alpha")
        session.commit()

    # No orders_loader/detail_loader parameter exists on this function at
    # all -- if it tried to sync orders, this call would TypeError.
    preview = create_exceptions_review_preview(inventory_loader=lambda min_quantity: [])
    assert preview["order_ingestion"] is None
