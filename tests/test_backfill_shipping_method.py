from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backfill_shipping_method import backfill_shipping_method
from models import Base, SalesOrder


def make_session(tmp_path):
    db = create_engine(f"sqlite:///{tmp_path / 'backfill-shipping-method.db'}")
    Base.metadata.create_all(db)
    return Session(db)


def test_backfills_orders_missing_shipping_method(tmp_path):
    with make_session(tmp_path) as session:
        order = SalesOrder(
            external_order_id="remote-1", source="manapool", status="shipped",
        )
        session.add(order)
        session.commit()
        order_id = order.id

        def loader(external_order_id):
            return {"order": {"shipping_method": "ground_advantage"}}

        result = backfill_shipping_method(session, detail_loader=loader)
        session.commit()

        assert result["backfilled"] == [{
            "order_id": order_id, "external_order_id": "remote-1",
            "shipping_method": "ground_advantage",
        }]
        assert session.get(SalesOrder, order_id).shipping_method == "ground_advantage"


def test_skips_non_manapool_orders_and_already_populated_orders(tmp_path):
    with make_session(tmp_path) as session:
        session.add(SalesOrder(
            external_order_id="local-1", source="simulation", status="shipped",
        ))
        session.add(SalesOrder(
            external_order_id="remote-2", source="manapool", status="shipped",
            shipping_method="first_class",
        ))
        session.commit()

        result = backfill_shipping_method(session, detail_loader=lambda _id: {"order": {}})
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

        result = backfill_shipping_method(session, detail_loader=loader)
        assert result["backfilled"] == []
        assert result["unfetchable"] == [{
            "order_id": order_id, "external_order_id": "remote-3", "error": "boom",
        }]
        assert session.get(SalesOrder, order_id).shipping_method is None
