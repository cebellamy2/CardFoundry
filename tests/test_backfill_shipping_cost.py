from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backfill_shipping_cost import backfill_shipping_cost
from models import Base, SalesOrder


def make_session(tmp_path):
    db = create_engine(f"sqlite:///{tmp_path / 'backfill-shipping-cost.db'}")
    Base.metadata.create_all(db)
    return Session(db)


def test_backfills_orders_missing_shipping_cost(tmp_path):
    with make_session(tmp_path) as session:
        order = SalesOrder(
            external_order_id="remote-1", source="manapool", status="shipped",
        )
        session.add(order)
        session.commit()
        order_id = order.id

        def loader(external_order_id):
            return {"order": {"payment": {"shipping_cents": 250}}}

        result = backfill_shipping_cost(session, detail_loader=loader)
        session.commit()

        assert result["backfilled"] == [{
            "order_id": order_id, "external_order_id": "remote-1", "shipping_cents": 250,
        }]
        assert session.get(SalesOrder, order_id).shipping_cents == 250


def test_skips_orders_already_populated(tmp_path):
    with make_session(tmp_path) as session:
        session.add(SalesOrder(
            external_order_id="remote-2", source="manapool", status="shipped",
            shipping_cents=0,
        ))
        session.commit()

        result = backfill_shipping_cost(session, detail_loader=lambda _id: {"order": {}})
        assert result == {"backfilled": [], "unfetchable": [], "missing_field": []}


def test_records_unfetchable_orders_without_guessing(tmp_path):
    with make_session(tmp_path) as session:
        order = SalesOrder(
            external_order_id="remote-3", source="manapool", status="shipped",
        )
        session.add(order)
        session.commit()
        order_id = order.id

        def loader(external_order_id):
            raise RuntimeError("boom")

        result = backfill_shipping_cost(session, detail_loader=loader)
        assert result["backfilled"] == []
        assert result["unfetchable"] == [{
            "order_id": order_id, "external_order_id": "remote-3", "error": "boom",
        }]
