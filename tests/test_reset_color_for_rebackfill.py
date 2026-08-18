from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import Base, Batch, InventoryCard, OrderItem, SalesOrder
from reset_color_for_rebackfill import reset_color


def setup_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reset_color.db'}")
    Base.metadata.create_all(engine)
    return engine


def test_resets_populated_colors_to_null_on_both_tables(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        batch = Batch(batch_code="B1")
        session.add(batch); session.flush()
        card = InventoryCard(
            batch_id=batch.id, name="Aang, Swift Savior", scryfall_id="sf-aang",
            color="", status="available",
        )
        session.add(card); session.flush()
        order = SalesOrder(external_order_id="o-1", source="manapool", status="shipped")
        session.add(order); session.flush()
        item = OrderItem(
            order_id=order.id, name="Orzhov Signet", scryfall_id="sf-orzhov",
            color="BW", quantity=1,
        )
        session.add(item); session.flush()
        card_id, item_id = card.id, item.id

        result = reset_color(session)
        session.commit()

    assert result == {"reset_cards": 1, "reset_items": 1}
    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).color is None
        assert session.get(OrderItem, item_id).color is None


def test_leaves_already_null_rows_alone_and_reports_zero(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        batch = Batch(batch_code="B1")
        session.add(batch); session.flush()
        session.add(InventoryCard(
            batch_id=batch.id, name="Forest", scryfall_id="sf-forest",
            color=None, status="available",
        ))
        session.commit()

        result = reset_color(session)
        session.commit()

    assert result == {"reset_cards": 0, "reset_items": 0}
