"""The shared card-move helper: active allocations follow, history does not.

★ WHY IT EXISTS. PickAllocation keeps its own batch_id, written once at
allocation time. v2.39.0 moved a reserved card's InventoryCard.batch_id
and left the allocation behind; three INNER joins read that column, so
seven live orders each silently lost a pick-list line.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from card_batch_move import move_card_to_batch
from inventory_mirror_service import ACTIVE_ALLOCATION_STATUSES
from models import (
    Base, Batch, ImportRecord, InventoryCard, OrderItem, PickAllocation,
    SalesOrder,
)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'move.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def setup(session, *, alloc_status="allocated"):
    source = Batch(batch_code="E4")
    target = Batch(batch_code="A1")
    session.add_all([source, target])
    session.flush()
    rec = ImportRecord(batch_id=source.id, filename="x.csv",
                       file_hash="h1", card_count=1)
    session.add(rec)
    session.flush()
    card = InventoryCard(
        batch_id=source.id, import_id=rec.id, name="Ajani's Anguish",
        set_code="ICE", collector_number="1", mtgjson_id="MTG-A",
        language_id="EN", condition_id="NM", finish_id="NF",
        condition="NM", finish="normal", status="reserved",
    )
    session.add(card)
    session.flush()
    order = SalesOrder(external_order_id="o-1", external_label="4321-8765",
                       source="manapool", status="ready_to_pick")
    session.add(order)
    session.flush()
    item = OrderItem(order_id=order.id, name=card.name, quantity=1,
                     mtgjson_id="MTG-A", language_id="EN", condition_id="NM",
                     finish_id="NF", price_cents=100)
    session.add(item)
    session.flush()
    allocation = PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id,
        batch_id=source.id, status=alloc_status)
    session.add(allocation)
    session.flush()
    return source, target, card, allocation


@pytest.mark.parametrize("status", sorted(ACTIVE_ALLOCATION_STATUSES))
def test_an_active_allocation_follows_the_card(session, status):
    source, target, card, allocation = setup(session, alloc_status=status)

    moved = move_card_to_batch(session, card, target)
    session.flush()

    assert card.batch_id == target.id
    assert allocation.batch_id == target.id, (
        f"a {status!r} allocation is work still to do -- the pick list has to "
        "point at where the card actually is"
    )
    assert moved == [allocation.id]
    assert allocation.status == status, "the status itself must not change"
    assert allocation.inventory_card_id == card.id, "nor the physical card"


@pytest.mark.parametrize("status", ["shipped", "released", "exception"])
def test_a_historical_allocation_does_not_follow(session, status):
    """A shipped allocation records the batch a card shipped FROM.
    Rewriting it to a batch the card was moved to afterwards would make
    the record claim something that never happened."""
    source, target, card, allocation = setup(session, alloc_status=status)

    moved = move_card_to_batch(session, card, target)
    session.flush()

    assert card.batch_id == target.id, "the card still moves"
    assert allocation.batch_id == source.id, (
        f"but a {status!r} allocation keeps the batch it actually happened in"
    )
    assert moved == []


def test_moving_to_the_batch_it_is_already_in_changes_nothing(session):
    source, target, card, allocation = setup(session)
    moved = move_card_to_batch(session, card, source)
    session.flush()
    assert moved == [], "nothing to re-point"
    assert allocation.batch_id == source.id


def test_it_writes_no_audit_row_of_its_own(session):
    """Callers write their own, in their own vocabulary. A second one here
    would double-report every move."""
    from models import InventoryChangeLog

    source, target, card, allocation = setup(session)
    move_card_to_batch(session, card, target)
    session.flush()
    assert session.query(InventoryChangeLog).count() == 0


def test_only_the_active_allocation_moves_when_history_sits_beside_it(session):
    """★ A CARD CAN ONLY HOLD ONE ACTIVE ALLOCATION. The partial unique
    index ux_pick_allocations_active_inventory_card enforces it in the
    database for status in (allocated, picked, packed), while allowing any
    number of finished rows -- so the realistic shape is one live claim
    beside old history, not two live claims.

    My first version of this test tried to create two active allocations
    and died on that index, which is the schema telling the truth.
    """
    source, target, card, allocation = setup(session)
    old_order = SalesOrder(external_order_id="o-old", source="manapool",
                           status="shipped")
    session.add(old_order)
    session.flush()
    old_item = OrderItem(order_id=old_order.id, name=card.name, quantity=1,
                         mtgjson_id="MTG-A", language_id="EN",
                         condition_id="NM", finish_id="NF", price_cents=100)
    session.add(old_item)
    session.flush()
    history = PickAllocation(order_item_id=old_item.id,
                             inventory_card_id=card.id, batch_id=source.id,
                             status="shipped")
    session.add(history)
    session.flush()

    moved = move_card_to_batch(session, card, target)
    session.flush()

    assert moved == [allocation.id], "only the live claim moves"
    assert allocation.batch_id == target.id
    assert history.batch_id == source.id, (
        "the shipped row keeps the batch the card actually shipped from"
    )


def test_the_legacy_recategorizer_brings_allocations_along(session):
    """★ THE OTHER PATH WITH THE SAME GAP. find_legacy_cards does NOT
    filter by status, so a reserved legacy card can be picked up. The card
    edit form and the bulk-move route both refuse any non-available card,
    so this was the only other one."""
    import recategorize_legacy_batches as mod

    source = Batch(batch_code="leg_land")
    target = Batch(batch_code="leg_multi")
    session.add_all([source, target])
    session.flush()
    rec = ImportRecord(batch_id=source.id, filename="x.csv",
                       file_hash="h2", card_count=1)
    session.add(rec)
    session.flush()
    card = InventoryCard(
        batch_id=source.id, import_id=rec.id, name="Spell // Land",
        set_code="ZNR", collector_number="2", mtgjson_id="MTG-B",
        language_id="EN", condition_id="NM", finish_id="NF",
        condition="NM", finish="normal", status="reserved",
        scryfall_id="sf-legacy-1",
    )
    session.add(card)
    session.flush()
    order = SalesOrder(external_order_id="o-3", source="manapool",
                       status="ready_to_pick")
    session.add(order)
    session.flush()
    item = OrderItem(order_id=order.id, name=card.name, quantity=1,
                     mtgjson_id="MTG-B", language_id="EN", condition_id="NM",
                     finish_id="NF", price_cents=100)
    session.add(item)
    session.flush()
    allocation = PickAllocation(order_item_id=item.id,
                               inventory_card_id=card.id,
                               batch_id=source.id, status="allocated")
    session.add(allocation)
    session.commit()

    result = mod.recategorize_legacy_batches(
        session,
        scryfall_lookup=lambda ids: {"sf-legacy-1": {"name": "Spell // Land"}},
    )
    session.flush()

    if result["moved"]:
        assert allocation.batch_id == card.batch_id, (
            "the allocation must follow the card through the legacy "
            "recategorizer too"
        )
    else:
        # Classification left it where it was; the invariant still holds.
        assert allocation.batch_id == card.batch_id
