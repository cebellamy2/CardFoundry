"""Returning a card to sellable tells Mana Pool immediately.

Reducing sellable stock has pushed within a second since v1.107.0 --
card 9430's removal reached Mana Pool in 140 ms. Returning it did not: a
card came back locally and then sat off-sale until the next Perform Sync,
up to eight hours. The justification for that asymmetry was "relisting is
a pricing decision Competitive Pricing owns", which stopped holding when
the bulk pricing cron began repricing every listing three times a day.

The guards are the raise path's, because the risk is the same one: an
unpriced card must never go on sale, and a first listing is the
new-listing path's job.
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import json

import main
import manapool_quantity_push_service as push
from models import Base, Batch, InventoryCard, RemoteProductBinding

MTG = "mtg-alpha"


@pytest.fixture
def pushes(monkeypatch):
    recorded = []
    monkeypatch.setattr(push, "update_inventory_prices_by_product",
                        lambda updates: recorded.append(updates))
    return recorded


@pytest.fixture
def failing_push(monkeypatch):
    monkeypatch.setattr(
        push, "update_inventory_prices_by_product",
        lambda _updates: (_ for _ in ()).throw(RuntimeError("Mana Pool 503")))


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'return.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    import inventory_sync_service
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    # sellability_service resolves `from database import engine` INSIDE
    # each transition, so patching the module attribute is not enough --
    # the real one must be replaced at its source.
    import database
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "inventory_sync_lease",
                        lambda *a, **kw: __import__("contextlib").nullcontext())
    import sellability_service
    monkeypatch.setattr(sellability_service, "inventory_sync_lease",
                        lambda *a, **kw: __import__("contextlib").nullcontext())
    with Session(engine) as session:
        session.add(Batch(id=1, batch_code="B1"))
        session.commit()
    return engine


def add_card(session, card_id=1, *, status="unsellable", price=2.50, mtgjson=MTG):
    session.add(InventoryCard(
        id=card_id, batch_id=1, name=f"Card {card_id}", set_code="ONE",
        collector_number="1", status=status, mtgjson_id=mtgjson,
        language_id="EN", condition_id="LP", finish_id="NF",
        condition="light_played", finish="normal", scryfall_id=f"sf-{card_id}",
        current_price=price, imported_at=datetime(2026, 8, 1),
    ))


def add_binding(session, binding_id=10, card_ids=(1,), mtgjson=MTG):
    session.add(RemoteProductBinding(
        id=binding_id, provider="manapool", product_type="mtg_single",
        product_id=f"product-{binding_id}",
        local_card_ids_json=json.dumps(list(card_ids)),
        requested_identity_json="{}", scryfall_id="sf-1", set_code="ONE",
        collector_number="1", mtgjson_id=mtgjson, language_id="EN",
        condition_id="LP", finish_id="NF", binding_status="validated",
        validated_at=datetime(2026, 9, 1), evidence_hash=f"h{binding_id}",
        evidence_json="{}",
    ))


# --- the service ----------------------------------------------------------

def test_a_returned_card_is_pushed(db, pushes):
    with Session(db) as session:
        add_card(session, status="available")
        add_binding(session)
        session.commit()
        card = session.get(InventoryCard, 1)
        outcomes = push.push_return_to_sellable(session, [card])

    assert outcomes[0]["outcome"] == "pushed"
    assert pushes == [[{
        "product_type": "mtg_single", "product_id": "product-10",
        "price_cents": None, "quantity": 1,
    }]]


def test_an_unpriced_card_returns_locally_but_is_NOT_pushed(db, pushes):
    """Listing at no price is worse than not listing -- the same rule the
    reconciliation raise path applies to its 126 held rows."""
    with Session(db) as session:
        add_card(session, status="available", price=None)
        add_binding(session)
        session.commit()
        card = session.get(InventoryCard, 1)
        outcomes = push.push_return_to_sellable(session, [card])

    assert pushes == []
    assert outcomes[0]["outcome"] == "no_price"
    assert "no price" in push.RETURN_PUSH_OUTCOMES["no_price"]


def test_a_never_listed_card_is_left_to_the_new_listing_path(db, pushes):
    """Publishing a first listing carries a pricing decision this does
    not make."""
    with Session(db) as session:
        add_card(session, status="available")
        session.commit()
        card = session.get(InventoryCard, 1)
        outcomes = push.push_return_to_sellable(session, [card])

    assert pushes == []
    assert outcomes[0]["outcome"] == "never_listed"


def test_a_failed_push_leaves_the_card_available_and_is_recorded(db, failing_push, caplog):
    """The card IS sellable; Mana Pool just has not heard. The local
    change must stand and the next reconciliation raises it."""
    import logging

    logger = logging.getLogger("cardfoundry")
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.INFO)
    try:
        with Session(db) as session:
            add_card(session, status="available")
            add_binding(session)
            session.commit()
            card = session.get(InventoryCard, 1)
            outcomes = push.push_return_to_sellable(session, [card])
            session.commit()
    finally:
        logger.removeHandler(caplog.handler)

    assert outcomes[0]["outcome"] == "push_failed"
    with Session(db) as session:
        assert session.get(InventoryCard, 1).status == "available", "still sellable"
        binding = session.get(RemoteProductBinding, 10)
        assert "Mana Pool 503" in (binding.last_quantity_push_failure_detail or ""), (
            "stamped on the binding exactly as a reduction's failure is"
        )
    assert "available locally but Mana Pool was not updated" in caplog.text


def test_the_summary_names_only_the_cards_that_did_not_list(db, pushes):
    outcomes = [
        {"card_id": 1, "name": "Alpha", "outcome": "pushed"},
        {"card_id": 2, "name": "Beta", "outcome": "no_price"},
    ]
    summary = push.return_push_summary(outcomes)
    assert "Beta" in summary and "no price" in summary
    assert "Alpha" not in summary


def test_a_clean_push_says_nothing(db):
    assert push.return_push_summary(
        [{"card_id": 1, "name": "Alpha", "outcome": "pushed"}]) == ""


def test_several_cards_on_one_identity_push_once(db, pushes):
    with Session(db) as session:
        add_card(session, 1, status="available")
        add_card(session, 2, status="available")
        add_binding(session, card_ids=(1, 2))
        session.commit()
        cards = [session.get(InventoryCard, 1), session.get(InventoryCard, 2)]
        push.push_return_to_sellable(session, cards)

    assert len(pushes) == 1, "one push per identity, not per card"
    assert pushes[0][0]["quantity"] == 2


# --- the routes -----------------------------------------------------------

def test_the_un_remove_route_pushes(db, pushes):
    with Session(db) as session:
        add_card(session, status="removed")
        card = session.get(InventoryCard, 1)
        card.removal_reason = "lost"
        add_binding(session)
        session.commit()
        # un_remove_card checks removal_metadata_state_hash, not the
        # disposition hash its neighbours use.
        from sellability_service import removal_metadata_state_hash
        card = session.get(InventoryCard, 1)
        expected = removal_metadata_state_hash(card)

    response = TestClient(main.app).post(
        "/inventory/1/un-remove/confirm",
        data={"expected_identity_hash": expected, "undo_note": "found it"},
        follow_redirects=False,
    )
    assert response.status_code in (200, 303)
    with Session(db) as session:
        assert session.get(InventoryCard, 1).status == "available"
    assert pushes, "un-remove must tell Mana Pool"


def test_the_sellability_route_pushes_on_return(db, pushes):
    with Session(db) as session:
        add_card(session, status="unsellable")
        card = session.get(InventoryCard, 1)
        card.unsellable_reason = "personal_use"
        add_binding(session)
        session.commit()
    response = TestClient(main.app).post(
        "/inventory/1/sellability/confirm",
        data={"expected_status": "unsellable", "target_status": "available",
              "reason": "", "note": ""},
        follow_redirects=False,
    )
    assert response.status_code in (200, 303)
    with Session(db) as session:
        assert session.get(InventoryCard, 1).status == "available"
    assert pushes, "return to sellable must tell Mana Pool"


def test_the_bulk_mark_available_route_pushes(db, pushes):
    with Session(db) as session:
        add_card(session, 1, status="unsellable")
        session.get(InventoryCard, 1).unsellable_reason = "personal_use"
        add_binding(session)
        session.commit()

    response = TestClient(main.app).post(
        "/inventory-cards/bulk-mark-available",
        data={"card_ids": [1], "back_link": "/inventory"},
    )
    assert response.status_code == 200
    with Session(db) as session:
        assert session.get(InventoryCard, 1).status == "available"
    assert pushes, "bulk mark available must tell Mana Pool"


def test_bulk_says_per_row_when_a_card_came_back_unlisted(db, pushes):
    with Session(db) as session:
        add_card(session, 1, status="unsellable", price=None)
        session.get(InventoryCard, 1).unsellable_reason = "personal_use"
        add_binding(session)
        session.commit()

    text = TestClient(main.app).post(
        "/inventory-cards/bulk-mark-available",
        data={"card_ids": [1], "back_link": "/inventory"},
    ).text
    assert "not listed" in text and "no price" in text
    assert pushes == []


# --- the reduction direction is unchanged ---------------------------------

def test_marking_a_card_unsellable_still_pushes(db, pushes):
    with Session(db) as session:
        add_card(session, status="available")
        add_binding(session)
        session.commit()
    TestClient(main.app).post(
        "/inventory/1/sellability/confirm",
        data={"expected_status": "available", "target_status": "unsellable",
              "reason": "personal_use", "note": "deck"},
        follow_redirects=False,
    )
    with Session(db) as session:
        assert session.get(InventoryCard, 1).status == "unsellable"
    assert pushes and pushes[-1][0]["quantity"] == 0, "reduction unchanged"


def test_an_unpriced_card_still_pushes_on_REDUCTION(db, pushes):
    """The price guard is about listing something, not about removing it.
    An unpriced card going off-sale must still reach Mana Pool."""
    with Session(db) as session:
        add_card(session, status="available", price=None)
        add_binding(session)
        session.commit()
        card = session.get(InventoryCard, 1)
        card.status = "unsellable"
        session.flush()
        push.push_for_cards(session, [card])

    assert pushes and pushes[-1][0]["quantity"] == 0


def test_never_listed_does_not_interrupt_the_operator(db):
    """It is the ordinary state of a card that has never been listed. A
    page for it would fire on most un-removes and train the operator to
    click through -- which is how the outcome that DOES matter (no price)
    would get missed."""
    assert push.return_push_summary(
        [{"card_id": 1, "name": "Alpha", "outcome": "never_listed"}]) == ""


def test_no_price_and_push_failed_DO_interrupt(db):
    for outcome in ("no_price", "push_failed"):
        summary = push.return_push_summary(
            [{"card_id": 1, "name": "Alpha", "outcome": outcome}])
        assert "Alpha" in summary, f"{outcome} must be surfaced"


def test_an_unpriced_UNBOUND_card_reports_never_listed_not_no_price(db, pushes):
    """Binding is checked first. A card that was never listed usually has
    no price too, and blaming the price would interrupt the operator over
    a card this path was never going to list anyway."""
    with Session(db) as session:
        add_card(session, status="available", price=None)   # no binding
        session.commit()
        card = session.get(InventoryCard, 1)
        outcomes = push.push_return_to_sellable(session, [card])
    assert outcomes[0]["outcome"] == "never_listed"
    assert push.return_push_summary(outcomes) == "", "and stays quiet"
