import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from fulfillment_exception_service import (
    FulfillmentExceptionError,
    mark_fulfillment_exception,
)
from models import (
    Base, Batch, FulfillmentException, FulfillmentExceptionEvent, ImportRecord,
    InventoryCard, InventoryChangeLog, OrderItem, PickAllocation, SalesOrder,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'exceptions-service.db'}")
    Base.metadata.create_all(engine)
    return engine


def seed(session, *, allocation_status="allocated", card_status="reserved", order_status="in_pick_wave", quantity=1):
    batch = Batch(batch_code=f"SERVICE-EXCEPTION-{session.query(Batch).count() + 1}", is_archived=False)
    session.add(batch); session.flush()
    record = ImportRecord(batch_id=batch.id, filename="x.csv", file_hash="x", card_count=1)
    session.add(record); session.flush()
    card = InventoryCard(
        batch_id=batch.id, import_id=record.id, name="Example", set_code="SET",
        collector_number="1", scryfall_id="sf", mtgjson_id="mtg",
        language_id="EN", condition_id="LP", finish_id="NF", condition="LP",
        finish="normal", status=card_status,
    )
    session.add(card); session.flush()
    order = SalesOrder(external_order_id="remote-1", status=order_status)
    session.add(order); session.flush()
    item = OrderItem(
        order_id=order.id, name=card.name, set_code=card.set_code,
        collector_number=card.collector_number, scryfall_id=card.scryfall_id,
        mtgjson_id=card.mtgjson_id, language_id=card.language_id,
        condition_id=card.condition_id, finish_id=card.finish_id, quantity=quantity,
    )
    session.add(item); session.flush()
    allocation = PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id,
        status=allocation_status,
    )
    session.add(allocation); session.commit()
    return order, item, card, allocation


def test_missing_transition_is_atomic_and_audited(db):
    with Session(db) as session:
        order, item, card, allocation = seed(session)
        exception = mark_fulfillment_exception(session, allocation.id, "missing")
        session.commit()
        session.refresh(card); session.refresh(allocation)
        assert exception.exception_type == "missing"
        assert allocation.status == "exception"
        assert card.status == "removed"
        assert card.inventory_exception_state == "exception_unresolved"
        assert card.removal_reason == "fulfillment_missing"
        assert exception.note.startswith("Fulfillment exception identified — ")
        assert session.query(FulfillmentExceptionEvent).count() == 1
        audit = json.loads(session.query(InventoryChangeLog).one().change_summary)
        assert audit["previous_status"] == "reserved"
        assert audit["new_status"] == "removed"
        assert audit["reason"] == "fulfillment_missing"


def test_picked_missing_and_inventory_mismatch_paths(db):
    with Session(db) as session:
        _, _, card, allocation = seed(session, allocation_status="picked")
        mark_fulfillment_exception(session, allocation.id, "missing", "Could not locate card")
        session.commit()
        assert card.status == "removed"

    with Session(db) as session:
        _, _, card, allocation = seed(session, allocation_status="allocated")
        mark_fulfillment_exception(session, allocation.id, "inventory_mismatch", "Wrong set")
        session.commit()
        assert card.status == "unsellable"
        assert card.unsellable_reason == "fulfillment_inventory_mismatch"
        assert card.inventory_exception_state == "exception_unresolved"
        assert session.query(InventoryChangeLog).count() == 2


def test_replay_is_idempotent_and_conflicting_type_fails(db):
    with Session(db) as session:
        _, _, _, allocation = seed(session)
        first = mark_fulfillment_exception(session, allocation.id, "missing", "note")
        session.commit()
        second = mark_fulfillment_exception(session, allocation.id, "missing", "different")
        assert second.id == first.id
        assert session.query(FulfillmentExceptionEvent).count() == 1
        assert session.query(InventoryChangeLog).count() == 1
        with pytest.raises(FulfillmentExceptionError):
            mark_fulfillment_exception(session, allocation.id, "inventory_mismatch")


@pytest.mark.parametrize("kwargs", [
    {"card_status": "available"},
    {"allocation_status": "released"},
    {"order_status": "shipped"},
])
def test_invalid_preconditions_make_no_mutations(db, kwargs):
    with Session(db) as session:
        _, _, card, allocation = seed(session, **kwargs)
        with pytest.raises(FulfillmentExceptionError):
            mark_fulfillment_exception(session, allocation.id, "missing")
        session.rollback()
        assert session.query(FulfillmentException).count() == 0
        assert session.query(FulfillmentExceptionEvent).count() == 0
        assert session.query(InventoryChangeLog).count() == 0
        assert card.status == kwargs.get("card_status", "reserved")


def test_projection_or_identity_conflicts_fail_and_multi_quantity_siblings_untouched(db):
    with Session(db) as session:
        _, item, card, allocation = seed(session, quantity=3)
        card.inventory_exception_state = "exception_unresolved"
        session.commit()
        with pytest.raises(FulfillmentExceptionError):
            mark_fulfillment_exception(session, allocation.id, "missing")

    with Session(db) as session:
        _, item, card, allocation = seed(session, quantity=3)
        sibling = InventoryCard(
            batch_id=card.batch_id, import_id=card.import_id, name="Sibling",
            set_code="SET", collector_number="2", scryfall_id="sf2", mtgjson_id="mtg2",
            language_id="EN", condition_id="LP", finish_id="NF", status="reserved",
        )
        session.add(sibling); session.flush()
        sibling_alloc = PickAllocation(
            order_item_id=item.id, inventory_card_id=sibling.id,
            batch_id=card.batch_id, status="allocated",
        )
        session.add(sibling_alloc); session.commit()
        mark_fulfillment_exception(session, allocation.id, "missing", "missing one")
        session.commit()
        assert item.quantity == 3
        assert sibling.status == "reserved"
        assert sibling_alloc.status == "allocated"
        assert session.query(PickAllocation).filter(
            PickAllocation.order_item_id == item.id,
        ).count() == 2


def test_identity_mismatch_and_existing_card_exception_fail_closed(db):
    with Session(db) as session:
        _, item, card, allocation = seed(session)
        item.scryfall_id = "different"
        session.commit()
        with pytest.raises(FulfillmentExceptionError, match="identities"):
            mark_fulfillment_exception(session, allocation.id, "missing")
        session.rollback()
        session.refresh(card)
        assert card.status == "reserved"
        assert session.query(FulfillmentException).count() == 0


def test_mid_transaction_failure_rolls_back_all_exception_mutations(db, monkeypatch):
    with Session(db) as session:
        _, _, card, allocation = seed(session)

        def fail(*args, **kwargs):
            raise RuntimeError("audit failure")

        monkeypatch.setattr("fulfillment_exception_service._audit_inventory", fail)
        with pytest.raises(RuntimeError, match="audit failure"):
            mark_fulfillment_exception(session, allocation.id, "missing")
        session.rollback()
        session.refresh(card); session.refresh(allocation)
        assert session.query(FulfillmentException).count() == 0
        assert session.query(FulfillmentExceptionEvent).count() == 0
        assert session.query(InventoryChangeLog).count() == 0
        assert card.status == "reserved"
        assert card.inventory_exception_state == "none"
        assert allocation.status == "allocated"
