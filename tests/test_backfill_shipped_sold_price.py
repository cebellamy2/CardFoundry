from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backfill_shipped_sold_price import backfill_sold_price
from models import Base, Batch, InventoryCard, OrderItem, PickAllocation, SalesOrder


def setup_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill.db'}")
    Base.metadata.create_all(engine)
    return engine


def add_shipped_unpriced_card(session, *, external_order_id, mtgjson_id="MTG-ALPHA",
                               order_status="shipped", source="manapool"):
    batch = Batch(batch_code=f"B-{external_order_id}")
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name="Alpha", mtgjson_id=mtgjson_id, language_id="EN",
        condition_id="LP", finish_id="NF", status="sold", sold_price=None,
    )
    session.add(card)
    session.flush()
    order = SalesOrder(external_order_id=external_order_id, source=source, status=order_status)
    session.add(order)
    session.flush()
    item = OrderItem(
        order_id=order.id, name="Alpha", mtgjson_id=mtgjson_id, language_id="EN",
        condition_id="LP", finish_id="NF", quantity=1, price_cents=None,
    )
    session.add(item)
    session.flush()
    allocation = PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id, status="shipped",
    )
    session.add(allocation)
    session.flush()
    return card, order, item


def remote_order_response(mtgjson_id="MTG-ALPHA", price_cents=1999):
    return {"order": {"items": [{
        "price_cents": price_cents,
        "product": {"single": {
            "mtgjson_id": mtgjson_id, "language_id": "EN",
            "condition_id": "LP", "finish_id": "NF",
        }},
    }]}}


def test_backfill_populates_sold_price_and_order_item_price_cents(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card, order, item = add_shipped_unpriced_card(session, external_order_id="mp-1")
        session.commit()
        card_id, item_id = card.id, item.id

    result = None
    with Session(engine) as session:
        result = backfill_sold_price(
            session, detail_loader=lambda order_id: remote_order_response(price_cents=1999),
        )
        session.commit()

    assert len(result["backfilled"]) == 1
    assert not result["unfetchable"]
    assert not result["unmatched"]
    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).sold_price == 19.99
        assert session.get(OrderItem, item_id).price_cents == 1999


def test_backfill_leaves_sold_price_blank_when_order_unfetchable(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card, order, item = add_shipped_unpriced_card(session, external_order_id="mp-gone")
        session.commit()
        card_id = card.id

    def failing_loader(order_id):
        raise RuntimeError("404 order not found")

    with Session(engine) as session:
        result = backfill_sold_price(session, detail_loader=failing_loader)
        session.commit()

    assert not result["backfilled"]
    assert result["unfetchable"] == [{"external_order_id": "mp-gone", "error": "404 order not found"}]
    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).sold_price is None


def test_backfill_leaves_sold_price_blank_when_line_not_matched_in_fresh_response(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card, order, item = add_shipped_unpriced_card(
            session, external_order_id="mp-2", mtgjson_id="MTG-ALPHA",
        )
        session.commit()
        card_id = card.id

    with Session(engine) as session:
        result = backfill_sold_price(
            session,
            detail_loader=lambda order_id: remote_order_response(mtgjson_id="MTG-OTHER"),
        )
        session.commit()

    assert not result["backfilled"]
    assert len(result["unmatched"]) == 1
    assert result["unmatched"][0]["card_id"] == card_id
    assert result["unmatched"][0]["name"] == "Alpha"
    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).sold_price is None


def test_backfill_ignores_cards_that_already_have_sold_price(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card, order, item = add_shipped_unpriced_card(session, external_order_id="mp-3")
        card.sold_price = 10.00
        session.commit()

    calls = []
    with Session(engine) as session:
        result = backfill_sold_price(
            session,
            detail_loader=lambda order_id: (calls.append(order_id), remote_order_response())[1],
        )
        session.commit()

    assert not result["backfilled"]
    assert calls == []


def test_backfill_groups_multiple_lines_from_the_same_order_into_one_fetch(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card1, order, item1 = add_shipped_unpriced_card(
            session, external_order_id="mp-multi", mtgjson_id="MTG-A",
        )
        batch = session.get(Batch, card1.batch_id)
        card2 = InventoryCard(
            batch_id=batch.id, name="Beta", mtgjson_id="MTG-B", language_id="EN",
            condition_id="LP", finish_id="NF", status="sold", sold_price=None,
        )
        session.add(card2)
        session.flush()
        item2 = OrderItem(
            order_id=order.id, name="Beta", mtgjson_id="MTG-B", language_id="EN",
            condition_id="LP", finish_id="NF", quantity=1, price_cents=None,
        )
        session.add(item2)
        session.flush()
        session.add(PickAllocation(
            order_item_id=item2.id, inventory_card_id=card2.id, batch_id=batch.id, status="shipped",
        ))
        session.commit()
        card1_id, card2_id = card1.id, card2.id

    calls = []

    def loader(order_id):
        calls.append(order_id)
        return {"order": {"items": [
            {"price_cents": 500, "product": {"single": {
                "mtgjson_id": "MTG-A", "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
            }}},
            {"price_cents": 700, "product": {"single": {
                "mtgjson_id": "MTG-B", "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
            }}},
        ]}}

    with Session(engine) as session:
        result = backfill_sold_price(session, detail_loader=loader)
        session.commit()

    assert calls == ["mp-multi"]
    assert len(result["backfilled"]) == 2
    with Session(engine) as session:
        assert session.get(InventoryCard, card1_id).sold_price == 5.00
        assert session.get(InventoryCard, card2_id).sold_price == 7.00
