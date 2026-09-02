import json
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
import manapool_quantity_push_service as push_service
from models import Base, Batch, InventoryCard, RemoteProductBinding


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'quantity_push_routes.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(database, "engine", db)
    return db


def add_batch(session, code="B1", **overrides):
    values = {"batch_code": code}
    values.update(overrides)
    batch = Batch(**values)
    session.add(batch)
    session.flush()
    return batch


def add_card(session, batch, **overrides):
    values = {
        "batch_id": batch.id, "name": "Alpha", "mtgjson_id": "mtg-alpha",
        "language_id": "EN", "condition_id": "LP", "finish_id": "NF", "status": "available",
    }
    values.update(overrides)
    card = InventoryCard(**values)
    session.add(card)
    session.commit()
    session.refresh(card)
    return card


def add_binding(session, *, product_id="product-alpha", mtgjson_id="mtg-alpha",
                 finish_id="NF", evidence_hash=None, **overrides):
    values = {
        "provider": "manapool", "product_type": "mtg_single", "product_id": product_id,
        "local_card_ids_json": "[]", "requested_identity_json": json.dumps({"name": "Alpha"}),
        "scryfall_id": "sf-alpha", "mtgjson_id": mtgjson_id,
        "language_id": "EN", "condition_id": "LP", "finish_id": finish_id,
        "set_code": "ONE", "collector_number": "1",
        "binding_status": "validated", "validated_at": datetime.now(),
        "evidence_hash": evidence_hash or f"evidence-{product_id}", "evidence_json": "{}",
    }
    values.update(overrides)
    binding = RemoteProductBinding(**values)
    session.add(binding)
    session.commit()
    return binding


def stub_writer(monkeypatch, *, raises=None):
    calls = []

    def fake(updates):
        calls.append(updates)
        if raises:
            raise raises
        return [{"ok": True}]

    monkeypatch.setattr(push_service, "update_inventory_prices_by_product", fake)
    return calls


# --- single-card routes trigger a push ---------------------------------------

def test_manual_disposition_pushes_a_decrement(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        card = add_card(session, batch)
        add_card(session, batch)  # a second copy stays available
        add_binding(session)
        card_id = card.id
    calls = stub_writer(monkeypatch)

    from sellability_service import disposition_identity_hash
    with Session(db) as session:
        expected_hash = disposition_identity_hash(session.get(InventoryCard, card_id))

    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card_id}/disposition/confirm",
        data={
            "expected_status": "available", "expected_identity_hash": expected_hash,
            "disposition_type": "local_sale", "transaction_note": "sold at a table",
            "value": "5.00", "received_description": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(calls) == 1
    assert calls[0] == [{
        "product_type": "mtg_single", "product_id": "product-alpha",
        "price_cents": None, "quantity": 1,  # the one remaining available copy
    }]
    with Session(db) as session:
        assert session.get(InventoryCard, card_id).status == "sold"


def test_removal_pushes_a_decrement(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        card = add_card(session, batch)
        add_binding(session)
        card_id = card.id
    calls = stub_writer(monkeypatch)

    from sellability_service import disposition_identity_hash
    with Session(db) as session:
        expected_hash = disposition_identity_hash(session.get(InventoryCard, card_id))

    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card_id}/removal/confirm",
        data={
            "expected_status": "available", "expected_identity_hash": expected_hash,
            "removal_reason": "duplicate_record", "removal_note": "double-scanned",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(calls) == 1
    assert calls[0][0]["quantity"] == 0
    with Session(db) as session:
        assert session.get(InventoryCard, card_id).status == "removed"


def test_mark_unavailable_pushes_a_decrement(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        card = add_card(session, batch)
        add_binding(session)
        card_id = card.id
    calls = stub_writer(monkeypatch)

    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card_id}/sellability/confirm",
        data={
            "expected_status": "available", "target_status": "unsellable",
            "reason": "damaged", "note": "creased",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(calls) == 1
    assert calls[0][0]["quantity"] == 0


def test_mark_available_return_direction_never_pushes(tmp_path, monkeypatch):
    """The confirmed exclusion: relisting is a pricing decision, not
    wired to this feature at all."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        card = add_card(
            session, batch, status="unsellable",
            unsellable_reason="damaged", unsellable_note="creased",
        )
        add_binding(session)
        card_id = card.id
    calls = stub_writer(monkeypatch)

    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card_id}/sellability/confirm",
        data={"expected_status": "unsellable", "target_status": "available"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert calls == []


def test_local_transition_succeeds_even_when_push_fails(tmp_path, monkeypatch):
    """The non-negotiable failure behaviour: a Mana Pool failure never
    blocks or reverses the local write."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        card = add_card(session, batch)
        add_binding(session)
        card_id = card.id
    stub_writer(monkeypatch, raises=RuntimeError("Mana Pool is down"))

    from sellability_service import disposition_identity_hash
    with Session(db) as session:
        expected_hash = disposition_identity_hash(session.get(InventoryCard, card_id))

    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card_id}/disposition/confirm",
        data={
            "expected_status": "available", "expected_identity_hash": expected_hash,
            "disposition_type": "local_sale", "transaction_note": "sold locally",
            "value": "", "received_description": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303  # local write still succeeded
    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert card.status == "sold"
        binding = session.query(RemoteProductBinding).one()
        assert binding.last_quantity_push_failure_detail == "Mana Pool is down"


# --- bulk routes: write count matches identity count, not card count --------

def test_bulk_remove_pushes_once_per_identity_not_per_card(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        cards = [add_card(session, batch) for _ in range(4)]  # 4 cards, 1 identity
        add_binding(session)
        card_ids = [c.id for c in cards]
    calls = stub_writer(monkeypatch)

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-remove",
        data={
            "card_ids": card_ids, "removal_reason": "duplicate_record",
            "removal_note": "bulk cleanup",
        },
    )
    assert response.status_code == 200
    assert len(calls) == 1  # one POST call
    assert len(calls[0]) == 1  # containing exactly one update (one identity)
    assert calls[0][0]["quantity"] == 0


def test_bulk_remove_across_two_identities_pushes_two_updates_in_one_call(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        alpha_cards = [add_card(session, batch, name="Alpha") for _ in range(3)]
        beta_cards = [
            add_card(session, batch, name="Beta", mtgjson_id="mtg-beta") for _ in range(2)
        ]
        add_binding(session, product_id="product-alpha")
        add_binding(session, product_id="product-beta", mtgjson_id="mtg-beta", evidence_hash="evidence-beta")
        card_ids = [c.id for c in alpha_cards + beta_cards]  # 5 cards, 2 identities
    calls = stub_writer(monkeypatch)

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-remove",
        data={"card_ids": card_ids, "removal_reason": "duplicate_record", "removal_note": "cleanup"},
    )
    assert response.status_code == 200
    assert len(calls) == 1
    assert len(calls[0]) == 2
    quantities = {u["product_id"]: u["quantity"] for u in calls[0]}
    assert quantities == {"product-alpha": 0, "product-beta": 0}


def test_bulk_mark_unavailable_pushes_once_per_identity(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        cards = [add_card(session, batch) for _ in range(6)]
        add_binding(session)
        card_ids = [c.id for c in cards]
    calls = stub_writer(monkeypatch)

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-mark-unavailable",
        data={"card_ids": card_ids, "unsellable_reason": "damaged", "unsellable_note": "water damage"},
    )
    assert response.status_code == 200
    assert len(calls) == 1
    assert len(calls[0]) == 1
    assert calls[0][0]["quantity"] == 0


def test_bulk_mark_available_never_pushes(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        cards = [
            add_card(
                session, batch, status="unsellable",
                unsellable_reason="damaged", unsellable_note="x",
            )
            for _ in range(3)
        ]
        add_binding(session)
        card_ids = [c.id for c in cards]
    calls = stub_writer(monkeypatch)

    client = TestClient(main.app)
    response = client.post(
        "/inventory-cards/bulk-mark-available", data={"card_ids": card_ids},
    )
    assert response.status_code == 200
    assert calls == []


def test_decklist_personal_use_bulk_confirm_pushes_once_per_identity(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        cards = [add_card(session, batch) for _ in range(3)]
        add_binding(session)
        from sellability_service import disposition_identity_hash
        refs = [f"{c.id}:{disposition_identity_hash(c)}" for c in cards]
    calls = stub_writer(monkeypatch)

    client = TestClient(main.app)
    response = client.post(
        "/inventory/decklist-search/mark-personal-use/confirm",
        data={
            "decklist_text": "3 Alpha", "personal_use_note": "keeping for myself",
            "card_ref": refs,
        },
    )
    assert response.status_code == 200
    assert len(calls) == 1
    assert len(calls[0]) == 1
    assert calls[0][0]["quantity"] == 0


# --- sync-issues page + retry -------------------------------------------------

def test_stuck_quantity_push_shows_on_sync_issues_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        add_card(session, batch)
        binding = add_binding(session)
        binding.last_quantity_push_attempted_at = datetime.now()
        binding.last_quantity_push_failure_detail = "Mana Pool is down"
        session.commit()

    response = TestClient(main.app).get("/orders/shipment-sync-issues")
    assert response.status_code == 200
    assert "quantity decrease" in response.text
    assert "Mana Pool is down" in response.text
    assert "Alpha" in response.text


def test_healthy_binding_never_shows_on_sync_issues_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        add_card(session, batch)
        add_binding(session)  # never pushed at all

    response = TestClient(main.app).get("/orders/shipment-sync-issues")
    assert response.status_code == 200
    assert "quantity decrease" not in response.text


def test_retry_route_clears_failure_and_redirects(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        add_card(session, batch)
        binding = add_binding(session)
        binding.last_quantity_push_failure_detail = "previous failure"
        session.commit()
        binding_id = binding.id
    calls = stub_writer(monkeypatch)

    response = TestClient(main.app).post(
        f"/manapool/quantity-push/{binding_id}/retry", follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/orders/shipment-sync-issues"
    assert len(calls) == 1
    with Session(db) as session:
        binding = session.get(RemoteProductBinding, binding_id)
        assert binding.last_quantity_push_failure_detail is None

    follow_up = TestClient(main.app).get("/orders/shipment-sync-issues")
    assert "quantity decrease" not in follow_up.text


# --- Part 1: unresolvable pushes surface distinctly ---------------------------

def test_card_with_no_binding_marked_unavailable_local_transition_intact(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        card = add_card(session, batch, mtgjson_id="unbound-id")
        card_id = card.id
    calls = stub_writer(monkeypatch)

    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card_id}/sellability/confirm",
        data={
            "expected_status": "available", "target_status": "unsellable",
            "reason": "damaged", "note": "creased",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303  # local transition succeeded
    assert calls == []  # nothing to push -- no binding exists
    with Session(db) as session:
        assert session.get(InventoryCard, card_id).status == "unsellable"


def test_unbound_card_row_appears_on_sync_issues_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        card = add_card(session, batch, mtgjson_id="unbound-id")
        card_id = card.id
    stub_writer(monkeypatch)

    client = TestClient(main.app)
    client.post(
        f"/inventory/{card_id}/sellability/confirm",
        data={"expected_status": "available", "target_status": "unsellable", "reason": "damaged", "note": "x"},
    )

    response = TestClient(main.app).get("/orders/shipment-sync-issues")
    assert response.status_code == 200
    assert "quantity decrease &mdash; no binding" in response.text
    assert "No Mana Pool binding exists for this identity" in response.text
    assert "Alpha" in response.text


def test_unbound_row_is_distinguishable_from_a_push_failure_row(tmp_path, monkeypatch):
    """Both rows show under the general "quantity decrease" family but
    must not be identical: different label, and no Retry button on the
    unresolved row -- there is nothing to retry."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        bound_card = add_card(session, batch, name="Bound Card")
        unbound_card = add_card(session, batch, name="Unbound Card", mtgjson_id="unbound-id")
        add_binding(session)
        bound_id, unbound_id = bound_card.id, unbound_card.id
    stub_writer(monkeypatch, raises=RuntimeError("Mana Pool is down"))

    client = TestClient(main.app)
    client.post(
        f"/inventory/{bound_id}/sellability/confirm",
        data={"expected_status": "available", "target_status": "unsellable", "reason": "damaged", "note": "x"},
    )
    client.post(
        f"/inventory/{unbound_id}/sellability/confirm",
        data={"expected_status": "available", "target_status": "unsellable", "reason": "damaged", "note": "y"},
    )

    response = TestClient(main.app).get("/orders/shipment-sync-issues")
    text = response.text
    assert "quantity decrease</td>" in text  # the push-failure row
    assert "quantity decrease &mdash; no binding" in text  # the unresolved row
    # The push-failure row gets a retry form; the unresolved row must not.
    table_rows = text.split("<tr>")
    failure_row = next(r for r in table_rows if "Mana Pool is down" in r)
    unresolved_row = next(r for r in table_rows if "No Mana Pool binding exists" in r)
    assert "Retry Now" in failure_row
    assert "/manapool/quantity-push/" in failure_row
    assert "Retry Now" not in unresolved_row


def test_unresolved_row_disappears_after_binding_is_added_and_transition_reruns(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = add_batch(session)
        card = add_card(session, batch, mtgjson_id="now-bound-id", status="unsellable",
                         unsellable_reason="damaged", unsellable_note="x")
        card_id = card.id
    stub_writer(monkeypatch)

    # First: no binding, gets recorded as unresolved via a real transition.
    with Session(db) as session:
        from manapool_quantity_push_service import push_for_cards
        card = session.get(InventoryCard, card_id)
        push_for_cards(session, [card])
        session.commit()
    assert "quantity decrease &mdash; no binding" in TestClient(main.app).get("/orders/shipment-sync-issues").text

    # Now a binding exists (simulating backfill_remote_product_bindings.py).
    with Session(db) as session:
        add_binding(session, product_id="product-now-bound", mtgjson_id="now-bound-id")

    client = TestClient(main.app)
    client.post(
        f"/inventory/{card_id}/sellability/confirm",
        data={"expected_status": "unsellable", "target_status": "available"},
    )
    client.post(
        f"/inventory/{card_id}/sellability/confirm",
        data={"expected_status": "available", "target_status": "unsellable", "reason": "damaged", "note": "z"},
    )

    response = TestClient(main.app).get("/orders/shipment-sync-issues")
    assert "quantity decrease &mdash; no binding" not in response.text
