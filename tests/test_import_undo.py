import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import Base, Batch, InventoryCard, InventoryChangeLog, OrderItem, PickAllocation, SalesOrder
from sellability_service import remove_cards_by_import, remove_import_cards


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'import-undo.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        batch = Batch(batch_code="A1", is_archived=False)
        session.add(batch); session.flush()
        for card_id in (1, 2, 3):
            session.add(InventoryCard(
                id=card_id, batch_id=batch.id, import_id=42, name=f"Card {card_id}",
                set_code="SET", collector_number=str(card_id), scryfall_id=f"sf-{card_id}",
                mtgjson_id=f"mtg-{card_id}", language_id="EN", condition_id="LP",
                finish_id="NF", condition="LP", finish="normal", status="available",
            ))
        # A card from a DIFFERENT import -- must never be touched.
        session.add(InventoryCard(
            id=4, batch_id=batch.id, import_id=99, name="Other Import Card",
            set_code="SET", collector_number="4", scryfall_id="sf-4",
            mtgjson_id="mtg-4", language_id="EN", condition_id="LP",
            finish_id="NF", condition="LP", finish="normal", status="available",
        ))
        session.commit()
    return engine


def test_remove_cards_by_import_removes_every_available_card(db):
    with Session(db) as session, session.begin():
        result = remove_cards_by_import(session, 42, "Import undone: wrong batch")
        assert {row["card_id"] for row in result["removed"]} == {1, 2, 3}
        assert result["skipped"] == []
    with Session(db) as session:
        for card_id in (1, 2, 3):
            card = session.get(InventoryCard, card_id)
            assert card.status == "removed"
            assert card.removal_reason == "import_undone"
            assert card.removal_note == "Import undone: wrong batch"
        # The other import's card is untouched.
        assert session.get(InventoryCard, 4).status == "available"
        assert session.query(InventoryChangeLog).count() == 3


def test_remove_cards_by_import_skips_non_available_cards_but_removes_the_rest(db):
    with Session(db) as session:
        card = session.get(InventoryCard, 2)
        order = SalesOrder(external_order_id="o1", status="needs_review")
        session.add(order); session.flush()
        item = OrderItem(order_id=order.id, name="Card 2", quantity=1)
        session.add(item); session.flush()
        card.status = "reserved"
        session.add(PickAllocation(
            inventory_card_id=2, order_item_id=item.id, batch_id=card.batch_id, status="allocated",
        ))
        session.commit()

    with Session(db) as session, session.begin():
        result = remove_cards_by_import(session, 42, "Import undone")
        assert {row["card_id"] for row in result["removed"]} == {1, 3}
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["card_id"] == 2
        assert "not available" in result["skipped"][0]["reason"]

    with Session(db) as session:
        assert session.get(InventoryCard, 1).status == "removed"
        assert session.get(InventoryCard, 2).status == "reserved"
        assert session.get(InventoryCard, 3).status == "removed"


def test_remove_cards_by_import_no_op_for_unknown_import(db):
    with Session(db) as session, session.begin():
        result = remove_cards_by_import(session, 999, "note")
        assert result == {"removed": [], "skipped": []}


def test_remove_import_cards_lease_wrapper(db, monkeypatch):
    import database
    monkeypatch.setattr(database, "engine", db)
    import sellability_service
    monkeypatch.setattr(sellability_service, "inventory_sync_lease", __import__("contextlib").nullcontext)
    result = remove_import_cards(42, "Import undone via wrapper")
    assert len(result["removed"]) == 3
    with Session(db) as session:
        assert session.get(InventoryCard, 1).status == "removed"


def test_remove_import_cards_uses_inventory_lease(monkeypatch):
    import sellability_service
    from contextlib import contextmanager
    from inventory_sync_service import InventoryLeaseBusy
    @contextmanager
    def busy():
        raise InventoryLeaseBusy("busy")
        yield
    monkeypatch.setattr(sellability_service, "inventory_sync_lease", busy)
    with pytest.raises(InventoryLeaseBusy, match="busy"):
        remove_import_cards(42, "note")
