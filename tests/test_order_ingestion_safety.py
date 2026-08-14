import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from inventory_sync_service import (
    InventoryLeaseBusy,
    acquire_inventory_lease,
    release_inventory_lease,
)
from inventory_mirror_service import build_inventory_mirror_preview
from models import (
    Base, Batch, InventoryCard, OrderItem, PickAllocation, RemoteProductBinding,
    SalesOrder,
)
from order_service import (
    InventoryAllocationError,
    approve_reserved_order,
    desired_sellable_quantities,
    ingest_manapool_orders,
    mark_packed,
    mark_picked,
    mark_shipped,
    release_order,
)


KEY = ("MTG-ALPHA", "EN", "LP", "NF")


@pytest.fixture
def session(tmp_path):
    test_engine = create_engine(f"sqlite:///{tmp_path / 'orders.db'}")
    Base.metadata.create_all(test_engine)
    with Session(test_engine) as value:
        yield value


def add_card(session, *, batch=None, status="available", **overrides):
    if batch is None:
        batch = Batch(batch_code=f"batch-{datetime.now().timestamp()}")
        session.add(batch)
        session.flush()
    values = {
        "batch_id": batch.id,
        "name": "Alpha",
        "set_code": "ONE",
        "collector_number": "1",
        "mtgjson_id": KEY[0],
        "language_id": KEY[1],
        "condition_id": KEY[2],
        "finish_id": KEY[3],
        "condition": "near_mint",
        "finish": "normal",
        "status": status,
    }
    values.update(overrides)
    card = InventoryCard(**values)
    session.add(card)
    session.flush()
    return card


def remote_detail(quantity=1, **single_overrides):
    single = {
        "name": "Alpha",
        "set": "ONE",
        "number": "1",
        "scryfall_id": "scryfall-alpha",
        "mtgjson_id": KEY[0],
        "language_id": KEY[1],
        "condition_id": KEY[2],
        "finish_id": KEY[3],
    }
    single.update(single_overrides)
    return {"order": {
        "label": "Order One",
        "latest_fulfillment_status": "paid",
        "items": [{
            "quantity": quantity,
            "product": {"tcgplayer_sku": 123, "single": single},
        }],
    }}


def ingest(session, detail=None):
    return ingest_manapool_orders(
        session,
        [{"id": "remote-1", "latest_fulfillment_status": "paid"}],
        lambda remote_id: detail or remote_detail(),
    )


def test_live_import_immediately_reserves_but_remains_needs_review(session):
    card = add_card(session)
    result = ingest(session)
    session.flush()
    order = session.query(SalesOrder).one()
    item = session.query(OrderItem).one()
    allocation = session.query(PickAllocation).one()
    assert result == {"imported": 1, "already_known": 0}
    assert order.status == "needs_review"
    assert card.status == "reserved"
    assert allocation.status == "allocated"
    assert (item.mtgjson_id, item.language_id, item.condition_id, item.finish_id) == KEY


def test_unsellable_card_never_satisfies_order_allocation(session):
    held = add_card(session, status="unsellable")
    available = add_card(session)
    ingest(session)
    session.flush()
    assert held.status == "unsellable"
    assert available.status == "reserved"
    assert session.query(PickAllocation).one().inventory_card_id == available.id


def test_manually_sold_card_never_satisfies_order_allocation(session):
    sold = add_card(session, status="sold", disposition_type="local_sale", disposition_note="Local")
    available = add_card(session)
    ingest(session)
    session.flush()
    assert sold.status == "sold"
    assert available.status == "reserved"


def test_removed_card_never_satisfies_order_allocation(session):
    removed = add_card(session, status="removed", removal_reason="duplicate_record", removal_note="Bad count")
    available = add_card(session)
    ingest(session)
    session.flush()
    assert removed.status == "removed"
    assert available.status == "reserved"


def test_allocation_matches_every_canonical_dimension_and_excludes_archived(session):
    active = Batch(batch_code="active")
    archived = Batch(batch_code="archived", is_archived=True)
    session.add_all([active, archived])
    session.flush()
    exact = add_card(session, batch=active)
    wrong_language = add_card(session, batch=active, language_id="JA")
    wrong_condition = add_card(session, batch=active, condition_id="NM")
    wrong_finish = add_card(session, batch=active, finish_id="FO")
    wrong_printing = add_card(session, batch=active, mtgjson_id="OTHER")
    archived_exact = add_card(session, batch=archived)
    ingest(session)
    session.flush()
    assert exact.status == "reserved"
    assert all(card.status == "available" for card in (
        wrong_language, wrong_condition, wrong_finish, wrong_printing, archived_exact,
    ))


def test_insufficient_exact_inventory_fails_closed(session):
    add_card(session)
    with pytest.raises(InventoryAllocationError, match="needed 2, found 1"):
        ingest(session, remote_detail(quantity=2))
    session.rollback()
    assert session.query(PickAllocation).count() == 0


def test_validated_binding_alone_does_not_satisfy_exact_order_allocation(session):
    card = add_card(session, mtgjson_id=None, scryfall_id="scryfall-alpha")
    session.add(RemoteProductBinding(
        provider="manapool", product_type="mtg_single", product_id="product-alpha",
        local_card_ids_json=json.dumps([card.id]),
        requested_identity_json=json.dumps({
            "name":"Alpha", "set_code":"ONE", "collector_number":"1",
            "scryfall_id":"scryfall-alpha", "language_id":"EN",
            "condition_id":"LP", "finish_id":"NF",
        }),
        scryfall_id="scryfall-alpha", language_id="EN", condition_id="LP",
        finish_id="NF", set_code="ONE", collector_number="1",
        binding_status="validated", validated_at=datetime.now(),
        evidence_hash="binding-evidence", evidence_json="{}",
    ))
    session.flush()
    with pytest.raises(InventoryAllocationError, match="needed 1, found 0"):
        ingest(session)


def test_available_null_mtgjson_is_rejected_by_inventory_sync_preflight(session):
    card = add_card(session, mtgjson_id=None, scryfall_id="scryfall-alpha")
    with pytest.raises(ValueError, match=rf"MTGJSON identity: {card.id}"):
        build_inventory_mirror_preview(
            [card], {card.batch_id: session.get(Batch, card.batch_id)}, [], [],
        )


def test_ambiguous_cross_check_metadata_fails_closed(session):
    add_card(session)
    add_card(session, name="Not Alpha")
    with pytest.raises(InventoryAllocationError, match="Ambiguous"):
        ingest(session)


def test_unknown_inventory_status_fails_closed(session):
    add_card(session, status="mystery")
    with pytest.raises(InventoryAllocationError, match="Unknown inventory status"):
        desired_sellable_quantities(session)


def test_order_lifecycle_and_release_are_idempotent(session):
    card = add_card(session)
    ingest(session)
    order = session.query(SalesOrder).one()
    approve_reserved_order(session, order)
    assert card.status == "reserved"
    mark_picked(session, order)
    assert card.status == "reserved"
    mark_packed(session, order)
    assert card.status == "reserved"
    release_order(session, order)
    release_order(session, order)
    assert card.status == "available"
    assert session.query(PickAllocation).one().status == "released"


def test_shipping_changes_reserved_card_to_sold(session):
    card = add_card(session)
    ingest(session)
    order = session.query(SalesOrder).one()
    approve_reserved_order(session, order)
    mark_picked(session, order)
    mark_packed(session, order)
    mark_shipped(session, order, None)
    assert card.status == "sold"
    assert session.query(PickAllocation).one().status == "shipped"


def test_desired_quantity_uses_status_once_without_double_subtraction(session):
    cards = [add_card(session) for _ in range(5)]
    ingest(session)
    quantities = desired_sellable_quantities(session)
    assert quantities[KEY] == 4
    assert sum(card.status == "reserved" for card in cards) == 1


def test_available_card_with_active_allocation_fails_closed(session):
    card = add_card(session)
    order = SalesOrder(external_order_id="bad", status="needs_review")
    session.add(order)
    session.flush()
    item = OrderItem(
        order_id=order.id, name="Alpha", mtgjson_id=KEY[0], language_id=KEY[1],
        condition_id=KEY[2], finish_id=KEY[3], quantity=1,
    )
    session.add(item)
    session.flush()
    session.add(PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=card.batch_id,
        status="allocated",
    ))
    session.flush()
    with pytest.raises(InventoryAllocationError, match="active allocation"):
        desired_sellable_quantities(session)


def test_existing_order_status_refresh_never_releases_allocation(session):
    card = add_card(session)
    ingest(session)
    result = ingest_manapool_orders(
        session,
        [{"id": "remote-1", "latest_fulfillment_status": "processing"}],
        lambda remote_id: remote_detail(),
    )
    assert result == {"imported": 0, "already_known": 1}
    assert card.status == "reserved"
    assert session.query(PickAllocation).one().status == "allocated"


def test_database_lease_rejects_collision(session):
    acquire_inventory_lease(session, "owner-one")
    with pytest.raises(InventoryLeaseBusy):
        acquire_inventory_lease(session, "owner-two")
    release_inventory_lease(session, "owner-one")
    acquire_inventory_lease(session, "owner-two")
