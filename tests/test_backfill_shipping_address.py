from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backfill_shipping_address import backfill_shipping_address
from models import Base, SalesOrder


def make_session(tmp_path):
    db = create_engine(f"sqlite:///{tmp_path / 'backfill-shipping-address.db'}")
    Base.metadata.create_all(db)
    return Session(db)


def test_backfills_orders_missing_shipping_address(tmp_path):
    with make_session(tmp_path) as session:
        order = SalesOrder(
            external_order_id="remote-1", source="manapool", status="shipped",
        )
        session.add(order)
        session.commit()
        order_id = order.id

        def loader(external_order_id):
            return {"order": {"shipping_address": {
                "name": "Jane Doe", "line1": "123 Main St", "line2": None,
                "city": "Springfield", "state": "IL", "postal_code": "62704",
                "country": "US",
            }}}

        result = backfill_shipping_address(session, detail_loader=loader)
        session.commit()

        assert result["backfilled"] == [{"order_id": order_id, "external_order_id": "remote-1"}]
        refreshed = session.get(SalesOrder, order_id)
        assert refreshed.shipping_name == "Jane Doe"
        assert refreshed.shipping_line1 == "123 Main St"
        assert refreshed.shipping_city == "Springfield"


def test_skips_orders_already_populated(tmp_path):
    with make_session(tmp_path) as session:
        session.add(SalesOrder(
            external_order_id="remote-2", source="manapool", status="shipped",
            shipping_line1="Already set",
        ))
        session.commit()

        result = backfill_shipping_address(session, detail_loader=lambda _id: {"order": {}})
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

        result = backfill_shipping_address(session, detail_loader=loader)
        assert result["backfilled"] == []
        assert result["unfetchable"] == [{
            "order_id": order_id, "external_order_id": "remote-3", "error": "boom",
        }]
