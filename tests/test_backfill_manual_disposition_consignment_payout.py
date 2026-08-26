from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backfill_manual_disposition_consignment_payout import apply_backfill, plan_backfill
from models import Base, Batch, Consignor, InventoryCard


def setup_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill.db'}")
    Base.metadata.create_all(engine)
    return engine


def make_card(session, batch, **kwargs):
    defaults = dict(batch_id=batch.id, name="Test Card", status="sold")
    defaults.update(kwargs)
    card = InventoryCard(**defaults)
    session.add(card)
    session.flush()
    return card


def make_consignment_batch(session, code="CON_JANE"):
    consignor = Consignor(name="Jane")
    session.add(consignor)
    session.flush()
    batch = Batch(batch_code=code, is_consignment=True, consignor_id=consignor.id)
    session.add(batch)
    session.flush()
    return batch


def test_plan_finds_manually_disposed_consigned_card_missing_payout(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        batch = make_consignment_batch(session)
        make_card(
            session, batch, sold_price=10.00, disposition_type="local_sale",
            consignment_amount_owed=None,
        )
        session.commit()

        plan = plan_backfill(session)
        assert plan["found"] == 1
        row = plan["backfills"][0]
        assert row["sold_price"] == 10.00
        assert row["resolved_owed"] == 8.00
        assert plan["total_owed"] == 8.00


def test_plan_ignores_card_already_paid_out_or_queued(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        batch = make_consignment_batch(session)
        make_card(
            session, batch, sold_price=10.00, disposition_type="local_sale",
            consignment_amount_owed=8.00, consignment_payout_status="owed",
        )
        session.commit()

        plan = plan_backfill(session)
        assert plan["found"] == 0


def test_plan_ignores_card_sold_via_mark_shipped_not_manual_disposition(tmp_path):
    """A real Mana Pool sale (mark_shipped) never sets disposition_type --
    it's already handled by apply_consignment_payout_if_consigned at ship
    time and should never be touched by this backfill."""
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        batch = make_consignment_batch(session)
        make_card(
            session, batch, sold_price=10.00, disposition_type=None,
            consignment_amount_owed=None,
        )
        session.commit()

        plan = plan_backfill(session)
        assert plan["found"] == 0


def test_plan_ignores_non_consignment_batch(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        batch = Batch(batch_code="PROD", is_consignment=False)
        session.add(batch)
        session.flush()
        make_card(
            session, batch, sold_price=10.00, disposition_type="local_sale",
            consignment_amount_owed=None,
        )
        session.commit()

        plan = plan_backfill(session)
        assert plan["found"] == 0


def test_apply_writes_resolved_owed_and_payout_status(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        batch = make_consignment_batch(session)
        card = make_card(
            session, batch, sold_price=10.00, disposition_type="local_sale",
            consignment_amount_owed=None,
        )
        session.commit()
        card_id = card.id

    with Session(engine) as session:
        with session.begin():
            plan = apply_backfill(session)
        assert plan["found"] == 1

    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        assert card.consignment_amount_owed == 8.00
        assert card.consignment_payout_status == "owed"


def test_dry_run_plan_does_not_write(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        batch = make_consignment_batch(session)
        card = make_card(
            session, batch, sold_price=10.00, disposition_type="local_sale",
            consignment_amount_owed=None,
        )
        session.commit()
        card_id = card.id

        plan_backfill(session)
        session.commit()

    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        assert card.consignment_amount_owed is None
        assert card.consignment_payout_status is None
