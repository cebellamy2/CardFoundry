from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from correct_consignment_shipping_deduction import apply_correction, plan_correction
from models import Base, Batch, Consignor, InventoryCard


def setup_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'correction.db'}")
    Base.metadata.create_all(engine)
    return engine


def make_card(session, batch, **kwargs):
    defaults = dict(
        batch_id=batch.id, name="Test Card", status="sold",
        consignment_payout_status="owed",
    )
    defaults.update(kwargs)
    card = InventoryCard(**defaults)
    session.add(card)
    session.flush()
    return card


def test_plan_flags_card_over_threshold_for_correction(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        consignor = Consignor(name="Luca")
        session.add(consignor)
        session.flush()
        batch = Batch(batch_code="CON_LUC", is_consignment=True, consignor_id=consignor.id)
        session.add(batch)
        session.flush()
        make_card(session, batch, sold_price=68.00, consignment_amount_owed=54.40)
        session.commit()

        plan = plan_correction(session)
        assert plan["checked"] == 1
        assert plan["unchanged"] == 0
        assert len(plan["corrections"]) == 1
        correction = plan["corrections"][0]
        assert correction["old_owed"] == 54.40
        assert correction["new_owed"] == round(68.00 * 0.80 - 5.50, 2)


def test_plan_leaves_card_under_threshold_unchanged(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        consignor = Consignor(name="Luca")
        session.add(consignor)
        session.flush()
        batch = Batch(batch_code="CON_LUC", is_consignment=True, consignor_id=consignor.id)
        session.add(batch)
        session.flush()
        make_card(session, batch, sold_price=10.00, consignment_amount_owed=8.00)
        session.commit()

        plan = plan_correction(session)
        assert plan["checked"] == 1
        assert plan["unchanged"] == 1
        assert plan["corrections"] == []


def test_plan_never_touches_already_paid_cards(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        consignor = Consignor(name="Luca")
        session.add(consignor)
        session.flush()
        batch = Batch(batch_code="CON_LUC", is_consignment=True, consignor_id=consignor.id)
        session.add(batch)
        session.flush()
        make_card(
            session, batch, sold_price=68.00, consignment_amount_owed=54.40,
            consignment_payout_status="paid",
        )
        session.commit()

        plan = plan_correction(session)
        assert plan["checked"] == 0
        assert plan["corrections"] == []


def test_apply_writes_the_corrected_amount(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        consignor = Consignor(name="Luca")
        session.add(consignor)
        session.flush()
        batch = Batch(batch_code="CON_LUC", is_consignment=True, consignor_id=consignor.id)
        session.add(batch)
        session.flush()
        card = make_card(session, batch, sold_price=68.00, consignment_amount_owed=54.40)
        session.commit()
        card_id = card.id

        apply_correction(session)
        session.commit()

        corrected = session.get(InventoryCard, card_id)
        assert corrected.consignment_amount_owed == round(68.00 * 0.80 - 5.50, 2)


def test_apply_is_idempotent(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        consignor = Consignor(name="Luca")
        session.add(consignor)
        session.flush()
        batch = Batch(batch_code="CON_LUC", is_consignment=True, consignor_id=consignor.id)
        session.add(batch)
        session.flush()
        make_card(session, batch, sold_price=68.00, consignment_amount_owed=54.40)
        session.commit()

        apply_correction(session)
        session.commit()

        plan = plan_correction(session)
        assert plan["unchanged"] == 1
        assert plan["corrections"] == []
