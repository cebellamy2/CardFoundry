from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard, InventoryChangeLog


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'bulk-card-actions.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_batch(db, code, *, is_archived=False):
    with Session(db) as session:
        batch = Batch(batch_code=code, is_archived=is_archived)
        session.add(batch)
        session.commit()
        session.refresh(batch)
        return batch


def make_card(db, batch_id, **overrides):
    with Session(db) as session:
        values = {
            "batch_id": batch_id, "name": "Alpha", "set_code": "SET",
            "collector_number": "1", "scryfall_id": "sf-1", "mtgjson_id": "mtg-1",
            "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
            "condition": "LP", "finish": "normal", "status": "available",
        }
        values.update(overrides)
        card = InventoryCard(**values)
        session.add(card)
        session.commit()
        session.refresh(card)
        return card


# --- /inventory-cards/bulk-move-batch ---

def test_bulk_move_batch_moves_all_available_cards(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    source = make_batch(db, "SOURCE")
    target = make_batch(db, "TARGET")
    card_a = make_card(db, source.id, name="Alpha", collector_number="1", scryfall_id="sf-a", mtgjson_id="mtg-a")
    card_b = make_card(db, source.id, name="Beta", collector_number="2", scryfall_id="sf-b", mtgjson_id="mtg-b")

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-move-batch",
        data={"card_ids": [card_a.id, card_b.id], "target_batch_id": str(target.id)},
    )
    assert response.status_code == 200
    assert "moved" in response.text

    with Session(db) as session:
        assert session.get(InventoryCard, card_a.id).batch_id == target.id
        assert session.get(InventoryCard, card_b.id).batch_id == target.id
        logs = session.query(InventoryChangeLog).all()
        assert len(logs) == 2
        assert "SOURCE" in logs[0].change_summary and "TARGET" in logs[0].change_summary


def test_bulk_move_batch_blocks_entirely_if_any_card_not_available(tmp_path, monkeypatch):
    """All-or-nothing: consignment status lives at the batch level, so a
    sold card moving batches would retroactively shift which consignor a
    past sale is attributed to. The whole move must be blocked, and the
    available card must NOT be moved either."""
    db = setup_db(tmp_path, monkeypatch)
    source = make_batch(db, "SOURCE")
    target = make_batch(db, "TARGET")
    available_card = make_card(db, source.id, name="Alpha", scryfall_id="sf-a", mtgjson_id="mtg-a")
    sold_card = make_card(
        db, source.id, name="Beta", collector_number="2", scryfall_id="sf-b",
        mtgjson_id="mtg-b", status="sold",
    )

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-move-batch",
        data={
            "card_ids": [available_card.id, sold_card.id],
            "target_batch_id": str(target.id),
        },
    )
    assert response.status_code == 409
    assert "Beta" in response.text
    assert "sold" in response.text

    with Session(db) as session:
        # Neither card moved -- true all-or-nothing.
        assert session.get(InventoryCard, available_card.id).batch_id == source.id
        assert session.get(InventoryCard, sold_card.id).batch_id == source.id


def test_bulk_move_batch_names_every_non_available_card(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    source = make_batch(db, "SOURCE")
    target = make_batch(db, "TARGET")
    sold = make_card(db, source.id, name="SoldCard", status="sold")
    removed = make_card(
        db, source.id, name="RemovedCard", collector_number="2",
        scryfall_id="sf-2", mtgjson_id="mtg-2", status="removed",
    )

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-move-batch",
        data={"card_ids": [sold.id, removed.id], "target_batch_id": str(target.id)},
    )
    assert response.status_code == 409
    assert "SoldCard" in response.text
    assert "RemovedCard" in response.text


def test_bulk_move_batch_rejects_archived_target(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    source = make_batch(db, "SOURCE")
    archived_target = make_batch(db, "OLD", is_archived=True)
    card = make_card(db, source.id)

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-move-batch",
        data={"card_ids": [card.id], "target_batch_id": str(archived_target.id)},
    )
    assert response.status_code == 400
    assert "archived" in response.text.lower()


def test_bulk_move_batch_requires_target_batch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    source = make_batch(db, "SOURCE")
    card = make_card(db, source.id)

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-move-batch",
        data={"card_ids": [card.id], "target_batch_id": ""},
    )
    assert response.status_code == 400


def test_bulk_move_batch_requires_at_least_one_card(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    target = make_batch(db, "TARGET")

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-move-batch",
        data={"card_ids": [], "target_batch_id": str(target.id)},
    )
    assert response.status_code == 400
    assert "No cards selected" in response.text


def test_bulk_move_batch_already_in_target_is_a_harmless_noop(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "SAME")
    card = make_card(db, batch.id)

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-move-batch",
        data={"card_ids": [card.id], "target_batch_id": str(batch.id)},
    )
    assert response.status_code == 200
    assert "unchanged" in response.text


# --- /inventory-cards/bulk-mark-unavailable ---

def test_bulk_mark_unavailable_transitions_available_cards(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "B1")
    card_a = make_card(db, batch.id, name="Alpha", scryfall_id="sf-a", mtgjson_id="mtg-a")
    card_b = make_card(db, batch.id, name="Beta", collector_number="2", scryfall_id="sf-b", mtgjson_id="mtg-b")

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-mark-unavailable",
        data={
            "card_ids": [card_a.id, card_b.id],
            "unsellable_reason": "damaged",
            "unsellable_note": "found in a wet box",
        },
    )
    assert response.status_code == 200
    with Session(db) as session:
        refreshed_a = session.get(InventoryCard, card_a.id)
        refreshed_b = session.get(InventoryCard, card_b.id)
        assert refreshed_a.status == "unsellable"
        assert refreshed_a.unsellable_reason == "damaged"
        assert refreshed_a.unsellable_note == "found in a wet box"
        assert refreshed_b.status == "unsellable"


def test_bulk_mark_unavailable_is_per_card_isolated_not_all_or_nothing(tmp_path, monkeypatch):
    """Unlike bulk-move, a card that's ineligible here (not currently
    available) doesn't block the rest of the selection -- it's reported
    as skipped, and every eligible card still succeeds."""
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "B1")
    available_card = make_card(db, batch.id, name="Alpha", scryfall_id="sf-a", mtgjson_id="mtg-a")
    sold_card = make_card(
        db, batch.id, name="Beta", collector_number="2", scryfall_id="sf-b",
        mtgjson_id="mtg-b", status="sold",
    )

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-mark-unavailable",
        data={
            "card_ids": [available_card.id, sold_card.id],
            "unsellable_reason": "damaged", "unsellable_note": "",
        },
    )
    assert response.status_code == 200
    with Session(db) as session:
        assert session.get(InventoryCard, available_card.id).status == "unsellable"
        assert session.get(InventoryCard, sold_card.id).status == "sold"
    assert "skipped" in response.text
    assert "Beta" in response.text


# --- /inventory-cards/bulk-mark-available ---

def test_bulk_mark_available_returns_unsellable_cards(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "B1")
    card = make_card(
        db, batch.id, status="unsellable", unsellable_reason="damaged",
        unsellable_note="old note",
    )

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-mark-available",
        data={"card_ids": [card.id]},
    )
    assert response.status_code == 200
    with Session(db) as session:
        refreshed = session.get(InventoryCard, card.id)
        assert refreshed.status == "available"
        assert refreshed.unsellable_reason is None
        assert refreshed.unsellable_note is None


def test_bulk_mark_available_skips_a_card_that_is_already_available(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "B1")
    card = make_card(db, batch.id, status="available")

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-mark-available",
        data={"card_ids": [card.id]},
    )
    assert response.status_code == 200
    assert "skipped" in response.text


# --- /inventory-cards/bulk-remove ---

def test_bulk_remove_removes_available_cards_with_shared_reason_and_note(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "B1")
    card_a = make_card(db, batch.id, name="Alpha", scryfall_id="sf-a", mtgjson_id="mtg-a")
    card_b = make_card(db, batch.id, name="Beta", collector_number="2", scryfall_id="sf-b", mtgjson_id="mtg-b")

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-remove",
        data={
            "card_ids": [card_a.id, card_b.id],
            "removal_reason": "scan_error",
            "removal_note": "duplicate scan of the same physical card",
        },
    )
    assert response.status_code == 200
    with Session(db) as session:
        refreshed_a = session.get(InventoryCard, card_a.id)
        refreshed_b = session.get(InventoryCard, card_b.id)
        assert refreshed_a.status == "removed"
        assert refreshed_a.removal_reason == "scan_error"
        assert refreshed_b.status == "removed"

        # Same audit shape as the single-card removal flow.
        logs = session.query(InventoryChangeLog).all()
        assert len(logs) == 2
        import json
        for log in logs:
            evidence = json.loads(log.change_summary)
            assert evidence["action_type"] == "inventory_removal"


def test_bulk_remove_is_per_card_isolated_not_all_or_nothing(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "B1")
    available_card = make_card(db, batch.id, name="Alpha", scryfall_id="sf-a", mtgjson_id="mtg-a")
    sold_card = make_card(
        db, batch.id, name="Beta", collector_number="2", scryfall_id="sf-b",
        mtgjson_id="mtg-b", status="sold",
    )

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-remove",
        data={
            "card_ids": [available_card.id, sold_card.id],
            "removal_reason": "scan_error", "removal_note": "duplicate",
        },
    )
    assert response.status_code == 200
    with Session(db) as session:
        assert session.get(InventoryCard, available_card.id).status == "removed"
        assert session.get(InventoryCard, sold_card.id).status == "sold"
    assert "skipped" in response.text


def test_bulk_remove_rejects_invalid_reason_per_card(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "B1")
    card = make_card(db, batch.id)

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-remove",
        data={
            "card_ids": [card.id],
            "removal_reason": "not_a_real_reason", "removal_note": "note",
        },
    )
    assert response.status_code == 200
    assert "skipped" in response.text
    with Session(db) as session:
        assert session.get(InventoryCard, card.id).status == "available"


# --- shared: missing cards, back_link safety ---

def test_bulk_action_reports_missing_card_id(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "B1")
    card = make_card(db, batch.id)

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-mark-unavailable",
        data={
            "card_ids": [card.id, 999999],
            "unsellable_reason": "damaged", "unsellable_note": "",
        },
    )
    assert response.status_code == 200
    assert "Not found" in response.text


def test_bulk_action_falls_back_to_inventory_for_unsafe_back_link(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "B1")
    card = make_card(db, batch.id, status="unsellable")

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-mark-available",
        data={"card_ids": [card.id], "back_link": "https://evil.example.com/steal"},
    )
    assert response.status_code == 200
    assert 'href="/inventory"' in response.text
    assert "evil.example.com" not in response.text


def test_bulk_action_preserves_a_safe_batch_back_link(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "B1")
    card = make_card(db, batch.id, status="unsellable")

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-mark-available",
        data={"card_ids": [card.id], "back_link": f"/batches/{batch.id}"},
    )
    assert response.status_code == 200
    assert f'href="/batches/{batch.id}"' in response.text


# --- UI rendering: /inventory and /batches/{id} ---

def test_inventory_page_renders_bulk_action_form_and_checkboxes(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "B1")
    target = make_batch(db, "B2")
    card = make_card(db, batch.id)

    client = TestClient(main.app)
    response = client.get("/inventory")
    assert response.status_code == 200
    assert 'id="bulk-card-action-form"' in response.text
    assert f'value="{card.id}"' in response.text
    assert 'form="bulk-card-action-form"' in response.text
    assert 'formaction="/inventory-cards/bulk-move-batch"' in response.text
    assert 'formaction="/inventory-cards/bulk-mark-unavailable"' in response.text
    assert 'formaction="/inventory-cards/bulk-mark-available"' in response.text
    assert 'formaction="/inventory-cards/bulk-remove"' in response.text
    assert f'<option value="{target.id}">B2</option>' in response.text


def test_inventory_bulk_move_dropdown_excludes_archived_batches(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_batch(db, "LIVE")
    archived = make_batch(db, "DEAD", is_archived=True)

    client = TestClient(main.app)
    response = client.get("/inventory")
    assert response.status_code == 200
    assert f'<option value="{archived.id}">DEAD</option>' not in response.text


def test_batch_detail_page_renders_bulk_action_form_and_checkboxes(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "B1")
    card = make_card(db, batch.id)

    client = TestClient(main.app)
    response = client.get(f"/batches/{batch.id}")
    assert response.status_code == 200
    assert 'id="bulk-card-action-form"' in response.text
    assert f'value="{card.id}"' in response.text
    assert f'name="back_link" value="/batches/{batch.id}"' in response.text
