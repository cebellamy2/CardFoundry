from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backfill_consignor_setup import (
    CONSIGNOR_BATCHES,
    apply_consignor_setup,
    plan_consignor_setup,
)
from models import Base, Batch, Consignor, InventoryCard


def setup_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'consignor_setup.db'}")
    Base.metadata.create_all(engine)
    return engine


def make_unlinked_batch(session, batch_code, *, sold_cards=None):
    batch = Batch(batch_code=batch_code, is_consignment=False, consignor_id=None)
    session.add(batch)
    session.flush()
    for name, sold_price in (sold_cards or []):
        session.add(InventoryCard(
            batch_id=batch.id, name=name, status="sold", sold_price=sold_price,
        ))
    session.add(InventoryCard(batch_id=batch.id, name="Still Available", status="available"))
    session.flush()
    return batch


def test_plan_identifies_all_batches_needing_setup(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        for _, batch_code in CONSIGNOR_BATCHES:
            make_unlinked_batch(session, batch_code)
        session.commit()

        plan = plan_consignor_setup(session)
        assert set(plan["consignors_to_create"]) == {name for name, _ in CONSIGNOR_BATCHES}
        assert set(plan["batches_to_link"]) == {code for _, code in CONSIGNOR_BATCHES}
        assert plan["batches_missing"] == []
        assert plan["consignors_existing"] == []
        assert plan["batches_already_linked"] == []


def test_plan_computes_owed_from_real_sold_price(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        make_unlinked_batch(session, "CON_CON", sold_cards=[("Card A", 10.0)])
        session.commit()

        plan = plan_consignor_setup(session)
        backfill = [row for row in plan["cards_to_backfill"] if row["batch"] == "CON_CON"]
        assert len(backfill) == 1
        assert backfill[0]["sold_price"] == 10.0
        assert backfill[0]["computed_owed"] == 8.0


def test_plan_skips_missing_batches_gracefully(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        session.commit()
        plan = plan_consignor_setup(session)
        assert set(plan["batches_missing"]) == {code for _, code in CONSIGNOR_BATCHES}
        assert plan["cards_to_backfill"] == []


def test_plan_recognizes_already_linked_batch_and_tracked_card(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        consignor = Consignor(name="Connor")
        session.add(consignor)
        session.flush()
        batch = Batch(
            batch_code="CON_CON", is_consignment=True, consignor_id=consignor.id,
        )
        session.add(batch)
        session.flush()
        session.add(InventoryCard(
            batch_id=batch.id, name="Already Tracked", status="sold",
            sold_price=10.0, consignment_amount_owed=8.0,
            consignment_payout_status="owed",
        ))
        session.commit()

        plan = plan_consignor_setup(session)
        assert "Connor" in plan["consignors_existing"]
        assert "CON_CON" in plan["batches_already_linked"]
        assert "CON_CON" not in plan["batches_to_link"]
        assert plan["cards_to_backfill"] == []
        assert len(plan["cards_already_tracked"]) == 1


def test_dry_run_writes_nothing(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        make_unlinked_batch(session, "CON_CON", sold_cards=[("Card A", 10.0)])
        session.commit()

        plan_consignor_setup(session)
        session.rollback()

    with Session(engine) as session:
        assert session.query(Consignor).count() == 0
        batch = session.query(Batch).filter_by(batch_code="CON_CON").one()
        assert batch.is_consignment is False
        assert batch.consignor_id is None
        card = session.query(InventoryCard).filter_by(name="Card A").one()
        assert card.consignment_amount_owed is None


def test_apply_creates_consignors_links_batches_and_backfills_owed(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        make_unlinked_batch(session, "CON_CON", sold_cards=[("Card A", 10.0)])
        make_unlinked_batch(session, "CON_KEV1", sold_cards=[("Card B", 20.0)])
        session.commit()

        apply_consignor_setup(session)
        session.commit()

    with Session(engine) as session:
        connor = session.query(Consignor).filter_by(name="Connor").one()
        kevin = session.query(Consignor).filter_by(name="Kevin").one()

        con_batch = session.query(Batch).filter_by(batch_code="CON_CON").one()
        assert con_batch.is_consignment is True
        assert con_batch.consignor_id == connor.id

        kev_batch = session.query(Batch).filter_by(batch_code="CON_KEV1").one()
        assert kev_batch.is_consignment is True
        assert kev_batch.consignor_id == kevin.id

        card_a = session.query(InventoryCard).filter_by(name="Card A").one()
        assert card_a.consignment_amount_owed == 8.0
        assert card_a.consignment_payout_status == "owed"

        card_b = session.query(InventoryCard).filter_by(name="Card B").one()
        assert card_b.consignment_amount_owed == 16.0
        assert card_b.consignment_payout_status == "owed"


def test_apply_does_not_touch_still_available_cards(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        make_unlinked_batch(session, "CON_CON", sold_cards=[("Card A", 10.0)])
        session.commit()

        apply_consignor_setup(session)
        session.commit()

    with Session(engine) as session:
        still_available = session.query(InventoryCard).filter_by(name="Still Available").one()
        assert still_available.consignment_amount_owed is None
        assert still_available.status == "available"


def test_apply_is_idempotent(tmp_path):
    """Running twice must not create duplicate consignors or double-count owed."""
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        make_unlinked_batch(session, "CON_CON", sold_cards=[("Card A", 10.0)])
        session.commit()

        apply_consignor_setup(session)
        session.commit()

    with Session(engine) as session:
        apply_consignor_setup(session)
        session.commit()

    with Session(engine) as session:
        assert session.query(Consignor).filter_by(name="Connor").count() == 1
        card_a = session.query(InventoryCard).filter_by(name="Card A").one()
        assert card_a.consignment_amount_owed == 8.0
