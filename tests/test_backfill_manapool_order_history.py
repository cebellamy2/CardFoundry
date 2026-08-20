import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import backfill_manapool_order_history as backfill_module
import inventory_sync_service
from backfill_manapool_order_history import apply_backfill, plan_backfill
from models import Base, OrderItem, SalesOrder


def setup_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'orders.db'}")
    Base.metadata.create_all(engine)
    return engine


def make_summary(order_id, status="delivered"):
    return {
        "id": order_id, "created_at": "2025-12-31T20:59:30.288Z",
        "label": f"label-{order_id}", "total_cents": 1000,
        "shipping_method": "ground_advantage", "latest_fulfillment_status": status,
    }


def make_detail(order_id, status="delivered", price_cents=1000):
    return {
        "id": order_id, "created_at": "2025-12-31T20:59:30.288Z",
        "label": f"label-{order_id}", "shipping_method": "ground_advantage",
        "latest_fulfillment_status": status,
        "shipping_address": {
            "name": "Test Buyer", "line1": "1 Main St", "line2": None,
            "city": "Rockford", "state": "IL", "postal_code": "61108", "country": "US",
        },
        "payment": {"shipping_cents": 0},
        "fulfillments": [{"tracking_number": "9400111899223333333333"}],
        "items": [{
            "product": {"single": {
                "name": "Polluted Bonds", "set": "WOT", "number": "34",
                "scryfall_id": "abc-123", "mtgjson_id": "mtg-abc",
                "language_id": "EN", "condition_id": "LP", "finish_id": "FO",
            }},
            "quantity": 1, "price_cents": price_cents,
        }],
    }


def test_plan_skips_orders_already_synced_locally(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        session.add(SalesOrder(external_order_id="order-1", source="manapool", status="shipped"))
        session.commit()

        summaries = [make_summary("order-1"), make_summary("order-2")]
        loader_calls = []

        def loader(order_id):
            loader_calls.append(order_id)
            return {"order": make_detail(order_id)}

        plan = plan_backfill(session, loader, summaries=summaries)
        assert plan["skipped_existing"] == 1
        assert len(plan["to_import"]) == 1
        assert plan["to_import"][0]["remote_id"] == "order-2"
        assert loader_calls == ["order-2"]  # never fetches detail for a known order


def test_plan_skips_orders_with_no_fulfillment_status(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        summaries = [make_summary("order-1", status=None)]
        plan = plan_backfill(session, lambda oid: {"order": make_detail(oid)}, summaries=summaries)
        assert plan["skipped_unfulfilled"] == 1
        assert plan["to_import"] == []


def test_plan_maps_refunded_to_cancelled_status(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        summaries = [make_summary("order-1", status="refunded")]
        loader = lambda oid: {"order": make_detail(oid, status="refunded")}
        plan = plan_backfill(session, loader, summaries=summaries)
        assert len(plan["to_import"]) == 1
        assert plan["to_import"][0]["local_status"] == "cancelled"


def test_plan_maps_delivered_shipped_replaced_to_shipped_status(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        for i, status in enumerate(["delivered", "shipped", "replaced"]):
            summaries = [make_summary(f"order-{i}", status=status)]
            loader = lambda oid, status=status: {"order": make_detail(oid, status=status)}
            plan = plan_backfill(session, loader, summaries=summaries)
            assert plan["to_import"][0]["local_status"] == "shipped"


def test_apply_inserts_sales_order_and_items_without_allocation(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        summaries = [make_summary("order-1")]
        loader = lambda oid: {"order": make_detail(oid, price_cents=638)}
        plan = plan_backfill(session, loader, summaries=summaries)

        outcome = apply_backfill(session, plan)
        session.commit()

        assert outcome["imported"] == 1
        assert outcome["failed"] == []

        order = session.query(SalesOrder).filter(
            SalesOrder.external_order_id == "order-1",
        ).first()
        assert order is not None
        assert order.status == "shipped"
        assert order.source == "manapool"
        assert order.shipping_name == "Test Buyer"
        assert order.tracking_number == "9400111899223333333333"

        items = session.query(OrderItem).filter(OrderItem.order_id == order.id).all()
        assert len(items) == 1
        assert items[0].name == "Polluted Bonds"
        assert items[0].price_cents == 638

        # No PickAllocation should ever be created by this path -- it must
        # never try to reserve live/available inventory against a
        # historical order.
        from models import PickAllocation
        assert session.query(PickAllocation).count() == 0


def test_apply_is_safe_to_rerun_without_duplicating(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        summaries = [make_summary("order-1")]
        loader = lambda oid: {"order": make_detail(oid)}

        plan = plan_backfill(session, loader, summaries=summaries)
        apply_backfill(session, plan)
        session.commit()

        # Re-run: same order should now be skipped as already-existing.
        plan_again = plan_backfill(session, loader, summaries=summaries)
        assert plan_again["skipped_existing"] == 1
        assert plan_again["to_import"] == []


def test_plan_isolates_a_single_order_detail_failure(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        summaries = [make_summary("order-1"), make_summary("order-2")]

        def loader(order_id):
            if order_id == "order-1":
                raise RuntimeError("boom")
            return {"order": make_detail(order_id)}

        plan = plan_backfill(session, loader, summaries=summaries)
        assert len(plan["to_import"]) == 1
        assert plan["to_import"][0]["remote_id"] == "order-2"
        assert len(plan["detail_errors"]) == 1
        assert plan["detail_errors"][0]["order_id"] == "order-1"


def test_main_confirm_actually_commits_end_to_end(tmp_path, monkeypatch):
    """Regression: main()'s --confirm path wrapped apply_backfill (which
    already commits per order itself, deliberately, for isolation) in an
    outer `with session.begin():` -- the two fought over the same
    transaction boundary and main() crashed with "A transaction is already
    begun on this Session." on every real run. Caught only by actually
    running main() end-to-end against a snapshot copy before touching
    production, never by testing plan_backfill/apply_backfill directly."""
    engine = create_engine(f"sqlite:///{tmp_path / 'main_e2e.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(backfill_module, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(
        backfill_module, "list_all_order_summaries", lambda: [make_summary("order-1")],
    )
    monkeypatch.setattr(
        backfill_module, "get_seller_order", lambda oid: {"order": make_detail(oid)},
    )
    output_path = str(tmp_path / "report.json")
    monkeypatch.setattr(sys, "argv", ["backfill_manapool_order_history.py", "--confirm",
                                       "--output", output_path])

    backfill_module.main()

    with Session(engine) as session:
        order = session.query(SalesOrder).filter(
            SalesOrder.external_order_id == "order-1",
        ).first()
        assert order is not None
        assert order.status == "shipped"
