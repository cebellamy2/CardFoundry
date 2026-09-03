from datetime import datetime
import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
import manapool_quantity_push_service as push_service
from models import (
    Base, Batch, Consignor, FulfillmentException, InventoryCard, OrderItem,
    PickAllocation, PickWave, PickWaveOrder, RemoteProductBinding, SalesOrder,
)


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'substitution-routes.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


IDENTITY = dict(
    name="Mirrorform", set_code="ECL", collector_number="308",
    scryfall_id="sf-mirrorform", mtgjson_id="mtg-mirrorform", language_id="EN",
)


def add_batch(session, code, **overrides):
    values = {"batch_code": code}
    values.update(overrides)
    batch = Batch(**values)
    session.add(batch)
    session.flush()
    return batch


def add_card(session, batch, *, condition_id="LP", finish_id="FO", status="available",
             imported_at=None, **overrides):
    values = dict(IDENTITY)
    values.update({
        "batch_id": batch.id, "condition_id": condition_id, "finish_id": finish_id,
        "condition": condition_id, "finish": "foil" if finish_id == "FO" else "normal",
        "status": status, "imported_at": imported_at or datetime.now(),
    })
    values.update(overrides)
    card = InventoryCard(**values)
    session.add(card)
    session.flush()
    return card


def add_binding(session, card, *, product_id=None, evidence_hash=None):
    binding = RemoteProductBinding(
        provider="manapool", product_type="mtg_single",
        product_id=product_id or f"product-{card.condition_id}-{card.id}",
        local_card_ids_json="[]", requested_identity_json=json.dumps({"name": card.name}),
        scryfall_id=card.scryfall_id, mtgjson_id=card.mtgjson_id,
        language_id=card.language_id, condition_id=card.condition_id, finish_id=card.finish_id,
        set_code=card.set_code, collector_number=card.collector_number,
        binding_status="validated", validated_at=datetime.now(),
        evidence_hash=evidence_hash or f"evidence-{card.id}", evidence_json="{}",
    )
    session.add(binding)
    session.commit()
    return binding


def make_wave_with_exception(session, *, exception_type="missing", batch_code="B1",
                              condition_id="LP", finish_id="FO"):
    order = SalesOrder(external_order_id=f"order-{session.query(SalesOrder).count() + 1}", status="in_pick_wave")
    session.add(order)
    session.flush()
    batch = add_batch(session, batch_code)
    card = add_card(session, batch, condition_id=condition_id, finish_id=finish_id, status="reserved")
    item = OrderItem(
        order_id=order.id, name=card.name, set_code=card.set_code,
        collector_number=card.collector_number, scryfall_id=card.scryfall_id,
        mtgjson_id=card.mtgjson_id, language_id=card.language_id,
        condition_id=card.condition_id, finish_id=card.finish_id, quantity=1,
    )
    session.add(item)
    session.flush()
    allocation = PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id, status="allocated",
    )
    session.add(allocation)
    wave = PickWave(label="Wave", status="active")
    session.add(wave)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
    session.commit()

    from fulfillment_exception_service import mark_fulfillment_exception
    exception = mark_fulfillment_exception(session, allocation.id, exception_type, "test note")
    session.commit()
    return wave, order, item, card, allocation, exception


def stub_writer(monkeypatch, *, raises=None):
    calls = []

    def fake(updates):
        calls.append(updates)
        if raises:
            raise raises
        return [{"ok": True}]

    monkeypatch.setattr(push_service, "update_inventory_prices_by_product", fake)
    return calls


# --- the disclosure appears/doesn't appear correctly --------------------------

def test_substitution_disclosure_appears_with_a_candidate(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order, item, card, allocation, exception = make_wave_with_exception(session)
        other_batch = add_batch(session, "B2")
        add_card(session, other_batch, condition_id="LP", finish_id="FO")
        session.commit()
        wave_id = wave.id

    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.status_code == 200
    assert "Substitute (1 candidate)" in response.text


def test_zero_candidates_shows_no_extra_ui(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order, item, card, allocation, exception = make_wave_with_exception(session)
        wave_id = wave.id

    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.status_code == 200
    assert "Substitute (" not in response.text
    # Today's plain exception form/table is still there, unaffected.
    assert "Fulfillment Exceptions" in response.text


def test_inventory_mismatch_exception_gets_no_substitution_ui(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order, item, card, allocation, exception = make_wave_with_exception(
            session, exception_type="inventory_mismatch",
        )
        other_batch = add_batch(session, "B2")
        add_card(session, other_batch, condition_id="LP", finish_id="FO")
        session.commit()
        wave_id = wave.id

    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert "Substitute (" not in response.text


# --- negative checks: disallowed candidates never appear as options ----------

def test_worse_condition_never_offered_in_rendered_html(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order, item, card, allocation, exception = make_wave_with_exception(session, condition_id="LP")
        other_batch = add_batch(session, "B2")
        add_card(session, other_batch, condition_id="MP", finish_id="FO")
        session.commit()
        wave_id = wave.id

    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert "Substitute (" not in response.text  # zero eligible candidates


def test_different_finish_never_offered_in_rendered_html(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order, item, card, allocation, exception = make_wave_with_exception(session, finish_id="FO")
        other_batch = add_batch(session, "B2")
        add_card(session, other_batch, condition_id="LP", finish_id="NF")
        session.commit()
        wave_id = wave.id

    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert "Substitute (" not in response.text


def test_different_language_never_offered_in_rendered_html(tmp_path, monkeypatch):
    """Different scryfall_id stands in for different language (v1.64.0:
    one scryfall_id pins exactly one language)."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order, item, card, allocation, exception = make_wave_with_exception(session)
        other_batch = add_batch(session, "B2")
        add_card(
            session, other_batch, condition_id="LP", finish_id="FO",
            scryfall_id="sf-mirrorform-japanese", language_id="JA",
        )
        session.commit()
        wave_id = wave.id

    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert "Substitute (" not in response.text


# --- ordering: exact match sorts above an upgrade, even when older ----------

def test_exact_condition_sorts_above_upgrade_even_when_older_in_html(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order, item, card, allocation, exception = make_wave_with_exception(session, condition_id="LP")
        other_batch = add_batch(session, "B2")
        add_card(
            session, other_batch, condition_id="NM", finish_id="FO",
            imported_at=datetime(2020, 1, 1),
        )
        add_card(
            session, other_batch, condition_id="LP", finish_id="FO",
            imported_at=datetime(2026, 1, 1),
        )
        session.commit()
        wave_id = wave.id

    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    text = response.text
    lp_pos = text.index("Lightly Played")
    nm_pos = text.index("Near Mint")
    assert lp_pos < nm_pos


# --- consignment flagging renders correctly -----------------------------------

def test_consignment_change_is_flagged_and_named_in_html(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order, item, card, allocation, exception = make_wave_with_exception(session)
        consignor = Consignor(name="Riley", is_active=True)
        session.add(consignor)
        session.flush()
        consignment_batch = add_batch(session, "B2", is_consignment=True, consignor_id=consignor.id)
        add_card(session, consignment_batch, condition_id="LP", finish_id="FO")
        session.commit()
        wave_id = wave.id

    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert "Changes consignment to Riley" in response.text


def test_same_consignor_candidate_not_flagged(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        consignor = Consignor(name="Cam", is_active=True)
        session.add(consignor)
        session.flush()
        wave, order, item, card, allocation, exception = make_wave_with_exception(session, batch_code="B1")
        original_batch = session.get(Batch, card.batch_id)
        original_batch.is_consignment = True
        original_batch.consignor_id = consignor.id
        session.commit()
        other_batch = add_batch(session, "B2", is_consignment=True, consignor_id=consignor.id)
        add_card(session, other_batch, condition_id="LP", finish_id="FO")
        session.commit()
        wave_id = wave.id

    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert "Changes consignment" not in response.text


# --- end-to-end confirm: push_for_cards, same-condition ----------------------

def test_end_to_end_same_condition_substitution_pushes_one_bucket(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order, item, card, allocation, exception = make_wave_with_exception(session, condition_id="LP")
        other_batch = add_batch(session, "B2")
        candidate = add_card(session, other_batch, condition_id="LP", finish_id="FO")
        add_binding(session, card, product_id="product-lp")
        # candidate resolves to the SAME binding (identical identity+condition)
        wave_id, exception_id, candidate_id = wave.id, exception.id, candidate.id
    calls = stub_writer(monkeypatch)

    client = TestClient(main.app)
    response = client.post(
        f"/pick-waves/{wave_id}/fulfillment-exceptions/{exception_id}/substitute",
        data={"candidate_card_id": candidate_id, "outcome": "remove", "note": "test"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(calls) == 1
    assert len(calls[0]) == 1  # one bucket -- original and substitute share the same LP identity
    assert calls[0][0]["product_id"] == "product-lp"

    with Session(db) as session:
        assert session.get(InventoryCard, candidate_id).status == "reserved"
        assert session.get(FulfillmentException, exception_id).submission_state == "not_required"


def test_end_to_end_cross_condition_substitution_pushes_two_buckets(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order, item, card, allocation, exception = make_wave_with_exception(session, condition_id="LP")
        other_batch = add_batch(session, "B2")
        candidate = add_card(session, other_batch, condition_id="NM", finish_id="FO")
        add_binding(session, card, product_id="product-lp")
        add_binding(session, candidate, product_id="product-nm", evidence_hash="evidence-nm")
        wave_id, exception_id, candidate_id = wave.id, exception.id, candidate.id
    calls = stub_writer(monkeypatch)

    client = TestClient(main.app)
    response = client.post(
        f"/pick-waves/{wave_id}/fulfillment-exceptions/{exception_id}/substitute",
        data={"candidate_card_id": candidate_id, "outcome": "remove", "note": "test"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(calls) == 1
    assert len(calls[0]) == 2  # two distinct Mana Pool listings: LP and NM
    product_ids = {u["product_id"] for u in calls[0]}
    assert product_ids == {"product-lp", "product-nm"}


def test_needs_review_outcome_end_to_end(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order, item, card, allocation, exception = make_wave_with_exception(session)
        other_batch = add_batch(session, "B2")
        candidate = add_card(session, other_batch, condition_id="LP", finish_id="FO")
        session.commit()
        wave_id, exception_id, candidate_id, card_id = wave.id, exception.id, candidate.id, card.id
    stub_writer(monkeypatch)

    client = TestClient(main.app)
    response = client.post(
        f"/pick-waves/{wave_id}/fulfillment-exceptions/{exception_id}/substitute",
        data={"candidate_card_id": candidate_id, "outcome": "needs_review", "note": "test"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(db) as session:
        exception = session.get(FulfillmentException, exception_id)
        assert exception.inventory_resolution_state == "unresolved"
        assert exception.submission_state == "not_required"
        assert session.get(InventoryCard, card_id).status == "removed"


# --- guard: wave membership / active state ------------------------------------

def test_substitute_refused_for_inactive_wave(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order, item, card, allocation, exception = make_wave_with_exception(session)
        other_batch = add_batch(session, "B2")
        candidate = add_card(session, other_batch, condition_id="LP", finish_id="FO")
        wave.status = "completed"
        session.commit()
        wave_id, exception_id, candidate_id = wave.id, exception.id, candidate.id

    response = TestClient(main.app).post(
        f"/pick-waves/{wave_id}/fulfillment-exceptions/{exception_id}/substitute",
        data={"candidate_card_id": candidate_id, "outcome": "remove"},
    )
    assert response.status_code == 409


def test_substitute_refused_for_exception_in_a_different_wave(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order, item, card, allocation, exception = make_wave_with_exception(session)
        other_wave = PickWave(label="Other Wave", status="active")
        session.add(other_wave)
        session.commit()
        other_wave_id, exception_id = other_wave.id, exception.id
        other_batch = add_batch(session, "B2")
        candidate = add_card(session, other_batch, condition_id="LP", finish_id="FO")
        session.commit()
        candidate_id = candidate.id

    response = TestClient(main.app).post(
        f"/pick-waves/{other_wave_id}/fulfillment-exceptions/{exception_id}/substitute",
        data={"candidate_card_id": candidate_id, "outcome": "remove"},
    )
    assert response.status_code == 409
