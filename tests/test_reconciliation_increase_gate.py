"""The increase gate: what may raise a Mana Pool listing's quantity.

Until 2026-09-17 the gate required every gap-explaining card to have been
imported AFTER the listing's own effective_as_of. That was a reasonable
proxy for "new stock Mana Pool has not seen" while listings were touched
rarely. Since v1.174.0 the bulk pricing job rewrites every listing three
times a day, so effective_as_of is always hours old while a real card's
imported_at is weeks old -- and the gate excluded 138 of 138 genuine
under-listings, $300.39 of stock permanently not offered.

The timestamp check is gone. The safety it was credited with lives
downstream: apply_reconciliation_preview re-reads Mana Pool fresh and
clamps to the fresh local desired count. These tests pin both halves --
that the gate no longer looks at the timestamp, and that every guard
which actually protects something still refuses its case.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_reconciliation_service as recon
from models import (
    Base, Batch, FulfillmentException, InventoryCard, OrderItem, PickAllocation,
    SalesOrder,
)

OLD_IMPORT = datetime(2026, 8, 1, 12, 0, 0)
LISTING_TOUCHED_TODAY = "2026-09-17T14:02:19.263Z"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recon.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Batch(id=1, batch_code="B1"))
        session.commit()
    return engine


def add_card(session, card_id, *, status="available", price=1.50,
             imported_at=OLD_IMPORT):
    session.add(InventoryCard(
        id=card_id, batch_id=1, name=f"Card {card_id}", status=status,
        mtgjson_id="mtg-a", language_id="EN", condition_id="LP", finish_id="NF",
        current_price=price, imported_at=imported_at,
    ))


def row(card_ids, *, desired=2, remote=1, effective_as_of=LISTING_TOUCHED_TODAY):
    return {
        "desired_quantity": desired,
        "current_remote_quantity": remote,
        "local_contributing_card_ids": list(card_ids),
        "effective_as_of": effective_as_of,
    }


# --- the removal itself ---------------------------------------------------

def test_a_gap_is_raisable_even_though_every_card_predates_the_listing(db):
    """THE regression this whole change exists for. The listing was
    touched by the pricing cron minutes ago; the card was imported six
    weeks earlier. Under the old gate this returned None, forever."""
    with Session(db) as session:
        add_card(session, 1)
        add_card(session, 2)
        session.commit()
        gap_cards, refusal = recon._raisable_gap(session, row([1, 2]))
    assert refusal is None
    assert len(gap_cards) == 1, "gap of 1 takes 1 card"


def test_the_gate_ignores_effective_as_of_entirely(db):
    """Same cards, wildly different timestamps -- and a missing one --
    must all behave identically. A future edit that reintroduces a
    timestamp comparison fails here."""
    with Session(db) as session:
        add_card(session, 1)
        add_card(session, 2)
        session.commit()
        outcomes = [
            recon._raisable_gap(session, row([1, 2], effective_as_of=value))
            for value in (
                LISTING_TOUCHED_TODAY,
                "2020-01-01T00:00:00.000Z",
                "2099-01-01T00:00:00.000Z",
                None,
                "",
            )
        ]
    assert all(refusal is None for _cards, refusal in outcomes)
    assert len({tuple(cards) for cards, _r in outcomes}) == 1


def test_a_missing_effective_as_of_no_longer_refuses(db):
    """It used to: `if not effective_dt: return None`."""
    with Session(db) as session:
        add_card(session, 1)
        add_card(session, 2)
        session.commit()
        gap_cards, refusal = recon._raisable_gap(
            session, row([1, 2], effective_as_of=None))
    assert refusal is None and gap_cards


# --- the guards that DO protect something ---------------------------------

def test_an_unpriced_card_refuses(db):
    """The guard that does the most work: 126 of the 138 stuck rows have
    one. Raising would put a card on sale with no price."""
    with Session(db) as session:
        add_card(session, 1, price=None)
        add_card(session, 2)
        session.commit()
        gap_cards, refusal = recon._raisable_gap(session, row([1, 2]))
    assert gap_cards is None
    assert refusal == "unpriced"
    assert "no price" in recon.GAP_REFUSALS[refusal]


def test_an_unavailable_card_refuses(db):
    with Session(db) as session:
        add_card(session, 1, status="sold")
        add_card(session, 2)
        session.commit()
        gap_cards, refusal = recon._raisable_gap(session, row([1, 2]))
    assert gap_cards is None and refusal == "unavailable"


def test_a_card_under_an_open_exception_refuses(db):
    """The operator has said this card is missing or wrong. Offering
    another unit of it is the phantom stock the exception prevents."""
    with Session(db) as session:
        add_card(session, 1)
        add_card(session, 2)
        order = SalesOrder(external_order_id="o1", source="manapool", status="picked")
        session.add(order)
        session.flush()
        item = OrderItem(order_id=order.id, name="Card 1", quantity=1,
                         mtgjson_id="mtg-a", language_id="EN",
                         condition_id="LP", finish_id="NF")
        session.add(item)
        session.flush()
        allocation = PickAllocation(order_item_id=item.id, inventory_card_id=1,
                                    batch_id=1, status="exception")
        session.add(allocation)
        session.flush()
        session.add(FulfillmentException(
            sales_order_id=order.id, order_item_id=item.id,
            pick_allocation_id=allocation.id, inventory_card_id=1,
            exception_type="missing", note="not on the shelf",
            submission_state="submitted", remote_resolution_state="awaiting",
            inventory_resolution_state="unresolved", remote_order_id="o1",
        ))
        session.commit()
        gap_cards, refusal = recon._raisable_gap(session, row([1, 2]))
    assert gap_cards is None and refusal == "open_exception"


def test_a_RESOLVED_exception_does_not_refuse(db):
    with Session(db) as session:
        add_card(session, 1)
        add_card(session, 2)
        order = SalesOrder(external_order_id="o2", source="manapool", status="shipped")
        session.add(order)
        session.flush()
        item = OrderItem(order_id=order.id, name="Card 1", quantity=1,
                         mtgjson_id="mtg-a", language_id="EN",
                         condition_id="LP", finish_id="NF")
        session.add(item)
        session.flush()
        allocation = PickAllocation(order_item_id=item.id, inventory_card_id=1,
                                    batch_id=1, status="exception")
        session.add(allocation)
        session.flush()
        session.add(FulfillmentException(
            sales_order_id=order.id, order_item_id=item.id,
            pick_allocation_id=allocation.id, inventory_card_id=1,
            exception_type="missing", note="found it",
            submission_state="submitted", remote_resolution_state="resolved_refunded",
            inventory_resolution_state="resolved", remote_order_id="o2",
        ))
        session.commit()
        _gap_cards, refusal = recon._raisable_gap(session, row([1, 2]))
    assert refusal is None


def test_fewer_cards_than_the_gap_refuses(db):
    with Session(db) as session:
        add_card(session, 1)
        session.commit()
        gap_cards, refusal = recon._raisable_gap(
            session, row([1], desired=5, remote=1))
    assert gap_cards is None and refusal == "not_enough_cards"


def test_no_gap_refuses(db):
    with Session(db) as session:
        add_card(session, 1)
        session.commit()
        gap_cards, refusal = recon._raisable_gap(
            session, row([1], desired=1, remote=1))
    assert gap_cards is None and refusal == "no_gap"


def test_newest_cards_are_chosen_when_there_are_more_than_the_gap(db):
    """If more cards back the identity than the gap needs, the most
    recently acquired are the ones Mana Pool is least likely to already
    be counting."""
    with Session(db) as session:
        add_card(session, 1, imported_at=OLD_IMPORT)
        add_card(session, 2, imported_at=OLD_IMPORT + timedelta(days=30))
        add_card(session, 3, imported_at=OLD_IMPORT + timedelta(days=10))
        session.commit()
        gap_cards, refusal = recon._raisable_gap(
            session, row([1, 2, 3], desired=3, remote=2))
    assert refusal is None
    assert gap_cards == [2], "newest first"


# --- the refusal is explicable --------------------------------------------

def test_every_refusal_code_has_plain_words(db):
    for code, text in recon.GAP_REFUSALS.items():
        assert text and not text.startswith(code)


def test_the_candidate_extractor_surfaces_the_refusal(db):
    """The old gate computed a reason and showed it nowhere -- which is
    how 138 rows sat unreconciled without anyone seeing why."""
    with Session(db) as session:
        add_card(session, 1, price=None)
        add_card(session, 2)
        session.commit()
        _candidates, excluded = recon.extract_reconciliation_candidates(session, {
            "rows": [{
                "category": "increase_quantity",
                "canonical_identity": {}, "name": "Card 1",
                "remote_product_id": "p1",
                **row([1, 2]),
            }],
        })
    assert len(excluded) == 1
    assert excluded[0]["refusal"] == "unpriced"
    assert "no price" in excluded[0]["reason"]
