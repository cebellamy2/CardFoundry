from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from correct_manapool_backfill_sync_markers import apply_correction, plan_correction
from models import Base, SalesOrder


def setup_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'markers.db'}")
    Base.metadata.create_all(engine)
    return engine


def test_plan_flags_backfilled_shipped_order_with_no_pick_pack_history(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        session.add(SalesOrder(
            external_order_id="order-1", source="manapool", status="shipped",
        ))
        session.commit()

        plan = plan_correction(session)
        assert len(plan["to_correct"]) == 1
        assert plan["to_correct"][0]["external_order_id"] == "order-1"


def test_plan_ignores_a_genuinely_live_processed_order(tmp_path):
    """An order that actually went through CardFoundry's own pack/ship flow
    has picked_at/packed_at set -- it must never be silently marked synced
    just because the push happens to be stuck; that's a real issue the
    operator still needs to see and retry."""
    from datetime import datetime

    engine = setup_db(tmp_path)
    with Session(engine) as session:
        session.add(SalesOrder(
            external_order_id="order-1", source="manapool", status="shipped",
            picked_at=datetime.now(), packed_at=datetime.now(),
        ))
        session.commit()

        plan = plan_correction(session)
        assert plan["to_correct"] == []


def test_plan_ignores_orders_already_synced_or_released(tmp_path):
    from datetime import datetime

    engine = setup_db(tmp_path)
    with Session(engine) as session:
        session.add(SalesOrder(
            external_order_id="already-synced", source="manapool", status="shipped",
            mana_pool_shipment_synced_at=datetime.now(),
        ))
        session.add(SalesOrder(
            external_order_id="already-released", source="manapool", status="shipped",
            mana_pool_shipment_released_at=datetime.now(),
        ))
        session.commit()

        plan = plan_correction(session)
        assert plan["to_correct"] == []


def test_plan_ignores_non_shipped_and_non_manapool_orders(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        session.add(SalesOrder(
            external_order_id="cancelled", source="manapool", status="cancelled",
        ))
        session.add(SalesOrder(
            external_order_id="sim-order", source="simulation", status="shipped",
        ))
        session.commit()

        plan = plan_correction(session)
        assert plan["to_correct"] == []


def test_apply_stamps_mana_pool_shipment_synced_at(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        order = SalesOrder(external_order_id="order-1", source="manapool", status="shipped")
        session.add(order)
        session.commit()
        order_id = order.id

        outcome = apply_correction(session)
        session.commit()

        assert outcome["corrected"] == 1
        corrected = session.get(SalesOrder, order_id)
        assert corrected.mana_pool_shipment_synced_at is not None


def test_apply_is_idempotent(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        session.add(SalesOrder(external_order_id="order-1", source="manapool", status="shipped"))
        session.commit()

        apply_correction(session)
        session.commit()

        plan = plan_correction(session)
        assert plan["to_correct"] == []
