import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from fulfillment_exception_reconciliation_service import (
    reconcile_remote_fulfillment_exceptions,
)
from fulfillment_exception_service import mark_fulfillment_exception
from fulfillment_exception_submission_service import (
    confirm_fulfillment_exception_submitted,
)
from models import (
    Base,
    Batch,
    FulfillmentException,
    FulfillmentExceptionEvent,
    InventoryCard,
    OrderItem,
    PickAllocation,
    SalesOrder,
)
from order_service import ingest_manapool_orders
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'exception-reconciliation.db'}")
    Base.metadata.create_all(engine)
    return engine


def submit(session, allocation, exception_type="missing"):
    exception = mark_fulfillment_exception(
        session, allocation.id, exception_type, "Card unavailable",
    )
    session.flush()
    confirm_fulfillment_exception_submitted(session, exception.id, "Reported in ManaPool")
    session.flush()
    return exception


def remote_line(item, outcome):
    return {
        "quantity": 1,
        "fulfillment_status": outcome,
        "product": {"single": {
            "name": item.name,
            "set": item.set_code,
            "number": item.collector_number,
            "scryfall_id": item.scryfall_id,
            "mtgjson_id": item.mtgjson_id,
            "language_id": item.language_id,
            "condition_id": item.condition_id,
            "finish_id": item.finish_id,
        }},
    }


def remote_order(*, items=None, status=None):
    value = {"id": "remote-1", "items": items or []}
    if status:
        value["latest_fulfillment_status"] = status
    return value


def test_line_refunded_and_replaced_resolve_only_submitted_exception(db):
    with Session(db) as session:
        order, item, _, allocation = seed(session)
        refunded = submit(session, allocation)
        result = reconcile_remote_fulfillment_exceptions(
            session, order, remote_order(items=[remote_line(item, "refunded")]),
        )
        assert result["resolved"] == 1
        assert refunded.remote_resolution_state == "resolved_refunded"
        assert session.query(FulfillmentExceptionEvent).filter_by(
            event_type="fulfillment_exception_remote_refunded"
        ).count() == 1

    with Session(db) as session:
        _, item, _, allocation = seed(session)
        replaced = submit(session, allocation)
        order = session.get(SalesOrder, replaced.sales_order_id)
        result = reconcile_remote_fulfillment_exceptions(
            session, order, remote_order(items=[remote_line(item, "replaced")]),
        )
        assert result["resolved"] == 1
        assert replaced.remote_resolution_state == "resolved_replaced"


@pytest.mark.parametrize("outcome, expected", [
    ("refunded", "resolved_refunded"),
    ("replaced", "resolved_replaced"),
])
def test_single_submitted_exception_uses_order_level_fallback(db, outcome, expected):
    with Session(db) as session:
        order, _, _, allocation = seed(session)
        exception = submit(session, allocation)
        result = reconcile_remote_fulfillment_exceptions(
            session, order, remote_order(status=outcome),
        )
        assert result == {"resolved": 1, "review_required": 0, "ignored": 0}
        assert exception.remote_resolution_state == expected
        assert exception.remote_order_id == "remote-1"
        assert exception.remote_evidence_hash
        assert json.loads(exception.remote_evidence_json)["matching_strategy"] == (
            "single_submitted_order_fallback"
        )


def add_sibling(session, order, batch, *, name="Second"):
    card = InventoryCard(
        batch_id=batch.id, import_id=None, name=name, set_code="SET",
        collector_number="2", scryfall_id="sf-2", mtgjson_id="mtg-2",
        language_id="EN", condition_id="LP", finish_id="NF", status="reserved",
    )
    session.add(card)
    session.flush()
    item = OrderItem(
        order_id=order.id, name=name, set_code="SET", collector_number="2",
        scryfall_id="sf-2", mtgjson_id="mtg-2", language_id="EN",
        condition_id="LP", finish_id="NF", quantity=1,
    )
    session.add(item)
    session.flush()
    allocation = PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id,
        batch_id=batch.id, status="allocated",
    )
    session.add(allocation)
    session.flush()
    return item, card, allocation


@pytest.mark.parametrize("outcome", ["refunded", "replaced"])
def test_multiple_order_level_candidates_are_review_required(db, outcome):
    with Session(db) as session:
        order, _, card, allocation = seed(session)
        first = submit(session, allocation)
        batch = session.get(Batch, card.batch_id)
        second_item, _, second_allocation = add_sibling(session, order, batch)
        second = submit(session, second_allocation)
        result = reconcile_remote_fulfillment_exceptions(
            session, order, remote_order(status=outcome),
        )
        assert result["review_required"] == 2
        assert first.remote_resolution_state == "review_required"
        assert second.remote_resolution_state == "review_required"
        assert session.query(FulfillmentExceptionEvent).filter_by(
            event_type="fulfillment_exception_remote_review_required"
        ).count() == 2


def test_line_evidence_identifies_one_of_multiple_candidates(db):
    with Session(db) as session:
        order, _, card, allocation = seed(session)
        first = submit(session, allocation)
        batch = session.get(Batch, card.batch_id)
        second_item, _, second_allocation = add_sibling(session, order, batch)
        second = submit(session, second_allocation)
        result = reconcile_remote_fulfillment_exceptions(
            session, order, remote_order(items=[remote_line(second_item, "refunded")]),
        )
        assert result["resolved"] == 1
        assert second.remote_resolution_state == "resolved_refunded"
        assert first.remote_resolution_state == "awaiting"


def test_needs_submission_is_not_auto_resolved(db):
    with Session(db) as session:
        order, item, _, allocation = seed(session)
        exception = mark_fulfillment_exception(session, allocation.id, "missing", "Not found")
        result = reconcile_remote_fulfillment_exceptions(
            session, order, remote_order(items=[remote_line(item, "refunded")], status="refunded"),
        )
        assert result == {"resolved": 0, "review_required": 0, "ignored": 1}
        assert exception.remote_resolution_state == "awaiting"


@pytest.mark.parametrize("exception_type, expected_status", [
    ("missing", "removed"),
    ("inventory_mismatch", "unsellable"),
])
@pytest.mark.parametrize("outcome", ["refunded", "replaced"])
def test_reconciliation_does_not_change_inventory_or_create_replacement(
    db, exception_type, expected_status, outcome,
):
    with Session(db) as session:
        order, item, card, allocation = seed(session)
        exception = submit(session, allocation, exception_type)
        card_status = card.status
        allocation_status = allocation.status
        result = reconcile_remote_fulfillment_exceptions(
            session, order, remote_order(status=outcome),
        )
        assert result["resolved"] == 1
        assert card.status == card_status == expected_status
        assert allocation.status == allocation_status == "exception"
        assert session.query(PickAllocation).count() == 1
        assert item.quantity == 1


def test_reconciliation_does_not_change_already_corrected_mismatch_inventory(db):
    with Session(db) as session:
        order, _, card, allocation = seed(session)
        exception = submit(session, allocation, "inventory_mismatch")
        card.status = "available"
        card.inventory_exception_state = "none"
        exception.inventory_resolution_state = "resolved"
        session.commit()
        reconcile_remote_fulfillment_exceptions(
            session, order, remote_order(status="refunded"),
        )
        assert card.status == "available"
        assert card.inventory_exception_state == "none"
        assert exception.inventory_resolution_state == "resolved"


@pytest.mark.parametrize("remote_state", ["resolved_refunded", "resolved_replaced", "review_required"])
def test_reconciled_states_do_not_block_progression(db, remote_state):
    with Session(db) as session:
        order, _, _, allocation = seed(session)
        exception = submit(session, allocation)
        exception.remote_resolution_state = remote_state
        session.commit()
        assert exception.submission_state == "submitted"


def test_identical_evidence_is_idempotent_and_conflicting_outcome_is_reviewed(db):
    with Session(db) as session:
        order, item, _, allocation = seed(session)
        exception = submit(session, allocation)
        detail = remote_order(items=[remote_line(item, "refunded")])
        first = reconcile_remote_fulfillment_exceptions(session, order, detail)
        session.commit()
        events = session.query(FulfillmentExceptionEvent).count()
        digest = exception.remote_evidence_hash
        second = reconcile_remote_fulfillment_exceptions(session, order, detail)
        assert first["resolved"] == 1
        assert second["resolved"] == 0
        assert session.query(FulfillmentExceptionEvent).count() == events
        assert exception.remote_evidence_hash == digest
        conflict = reconcile_remote_fulfillment_exceptions(
            session, order, remote_order(items=[remote_line(item, "replaced")]),
        )
        assert conflict["review_required"] == 1
        assert exception.remote_resolution_state == "review_required"


def test_order_sync_reconciles_read_only_detail_without_reallocation(db):
    with Session(db) as session:
        order, item, card, allocation = seed(session)
        order.source = "manapool"
        exception = submit(session, allocation)
        before_allocations = session.query(PickAllocation).count()
        detail = remote_order(items=[remote_line(item, "refunded")])
        result = ingest_manapool_orders(
            session, [{"id": "remote-1"}], lambda _: detail,
        )
        assert result == {"imported": 0, "already_known": 1}
        assert exception.remote_resolution_state == "resolved_refunded"
        assert session.query(PickAllocation).count() == before_allocations
        assert card.status == "removed"
