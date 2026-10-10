"""The one-time CON_RAU -> Raul retroactive consignment correction.

CON_RAU was named like every other consignor batch but was never linked,
and two of its cards had already sold with no payout tracked. The
batch-edit UI deliberately locks consignment status once any card has
sold, so this correction has to do both halves together -- the same shape
as retro_consign_cam_roc.py.

These tests exist because the script writes to production once and then
never again: they are the record of what it was supposed to do.
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import retro_consign_raul as rcr
from models import (
    Base, Batch, Consignor, ConsignorChangeLog, ImportRecord, InventoryCard,
    InventoryChangeLog,
)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'raul.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def seed(session, *, link_to=None, sold=((1.28, 6388), (0.65, 6391)), unsold=9):
    consignor = Consignor(id=16, name="Raul", payout_method="Cash App")
    session.add(consignor)
    session.flush()
    batch = Batch(batch_code="CON_RAU",
                  is_consignment=bool(link_to), consignor_id=link_to)
    session.add(batch)
    session.flush()
    rec = ImportRecord(batch_id=batch.id, filename="x.csv", file_hash="h",
                       card_count=1)
    session.add(rec)
    session.flush()
    n = 0
    for price, card_id in sold:
        n += 1
        session.add(InventoryCard(
            id=card_id, batch_id=batch.id, import_id=rec.id, name=f"Sold {n}",
            set_code="MOM", collector_number=str(n), language_id="EN",
            condition_id="NM", finish_id="NF", condition="NM", finish="normal",
            status="sold", sold_price=price))
    for i in range(unsold):
        session.add(InventoryCard(
            batch_id=batch.id, import_id=rec.id, name=f"Unsold {i}",
            set_code="MOM", collector_number=f"u{i}", language_id="EN",
            condition_id="NM", finish_id="NF", condition="NM", finish="normal",
            status="available"))
    session.commit()
    return consignor, batch


# --- the plan -------------------------------------------------------------

def test_the_plan_resolves_both_sold_cards_against_the_live_tiers(session):
    seed(session)
    plan = rcr.plan_retro_consignment(session)
    owed = {row["card_id"]: row["computed_owed"] for row in plan["cards_to_backfill"]}
    assert owed == {6388: 0.77, 6391: 0.00}
    assert plan["cards_to_backfill_total_owed"] == 0.77
    assert len(plan["unsold_cards"]) == 9
    assert plan["total_cards_in_batch"] == 11


def test_the_under_a_dollar_card_pays_nothing(session):
    """Operator decision 2026-09-23: the under-$1 tier is flat $0.00,
    because handling cost more than the payout."""
    seed(session)
    plan = rcr.plan_retro_consignment(session)
    cheap = next(r for r in plan["cards_to_backfill"] if r["card_id"] == 6391)
    assert cheap["sold_price"] == 0.65
    assert cheap["computed_owed"] == 0.00


def test_the_shipping_deduction_does_not_apply_to_either_card(session):
    """The $5.50 deduction sits only on the >$35 tier."""
    seed(session)
    plan = rcr.plan_retro_consignment(session)
    top = plan["tiers"][-1]
    assert top["deduction"] == 5.50 and top["max_price"] is None
    assert all(r["sold_price"] < 35.00 for r in plan["cards_to_backfill"])


def test_it_refuses_to_create_the_consignor(session):
    """Raul already exists; inventing a second one would be a duplicate."""
    batch = Batch(batch_code="CON_RAU")
    session.add(batch)
    session.commit()
    with pytest.raises(ValueError, match="refusing to create one"):
        rcr.plan_retro_consignment(session)


def test_it_refuses_a_batch_linked_to_a_different_consignor(session):
    other = Consignor(name="Someone Else")
    session.add(other)
    session.flush()
    seed(session, link_to=other.id)
    with pytest.raises(ValueError, match="different consignor"):
        rcr.plan_retro_consignment(session)


def test_it_refuses_a_card_that_already_carries_tracking(session):
    """A silent overwrite could erase or double-count real payout history."""
    _consignor, _batch = seed(session)
    card = session.get(InventoryCard, 6388)
    card.consignment_amount_owed = 99.0
    session.commit()
    with pytest.raises(ValueError, match="already carry consignment"):
        rcr.plan_retro_consignment(session)


# --- the apply ------------------------------------------------------------

def test_the_apply_links_the_batch_and_backfills_both_cards(session):
    seed(session)
    with session.begin_nested():
        rcr.apply_retro_consignment(session)
    session.commit()

    batch = session.query(Batch).filter_by(batch_code="CON_RAU").one()
    assert batch.is_consignment is True
    assert batch.consignor_id == 16
    assert session.get(InventoryCard, 6388).consignment_amount_owed == 0.77
    assert session.get(InventoryCard, 6388).consignment_payout_status == "owed"
    assert session.get(InventoryCard, 6391).consignment_amount_owed == 0.00
    assert session.get(InventoryCard, 6391).consignment_payout_status == "owed"


def test_no_consignor_payout_row_is_created(session):
    """"owed" means visible on the owed report, not paid."""
    from models import ConsignorPayout

    seed(session)
    with session.begin_nested():
        rcr.apply_retro_consignment(session)
    session.commit()
    assert session.query(ConsignorPayout).count() == 0


def test_the_nine_unsold_cards_are_left_completely_alone(session):
    seed(session)
    with session.begin_nested():
        rcr.apply_retro_consignment(session)
    session.commit()
    unsold = session.query(InventoryCard).filter(
        InventoryCard.status == "available").all()
    assert len(unsold) == 9
    assert all(c.consignment_amount_owed is None for c in unsold)
    assert all(c.consignment_payout_status is None for c in unsold)


# --- the audit the operator asked for -------------------------------------

def test_one_inventory_change_log_row_per_changed_card(session):
    seed(session)
    with session.begin_nested():
        plan = rcr.apply_retro_consignment(session)
    session.commit()

    rows = session.query(InventoryChangeLog).order_by(InventoryChangeLog.id).all()
    assert len(rows) == 2
    assert sorted(r.inventory_card_id for r in rows) == [6388, 6391]
    assert {r.actor for r in rows} == {"script:retro_consign_raul"}
    assert plan["audit_ids"]["inventory_change_logs"] == [r.id for r in rows]


def test_the_card_audit_records_before_after_and_the_tiers_used(session):
    seed(session)
    with session.begin_nested():
        rcr.apply_retro_consignment(session)
    session.commit()

    row = session.query(InventoryChangeLog).filter_by(inventory_card_id=6388).one()
    entry = json.loads(row.change_summary)
    assert entry["action_type"] == "consignment_retro_backfill"
    assert entry["before"] == {"consignment_amount_owed": None,
                               "consignment_payout_status": None}
    assert entry["after"] == {"consignment_amount_owed": 0.77,
                              "consignment_payout_status": "owed"}
    assert entry["sold_price"] == 1.28
    assert entry["consignor_name"] == "Raul"
    assert entry["batch_code"] == "CON_RAU"
    # The tier table is recorded so a later tier edit cannot make this
    # number look wrong in hindsight.
    assert entry["tiers_at_the_time"][0] == {"max_price": 1.0, "type": "flat", "value": 0.0}
    assert entry["reason"]


def test_the_batch_link_is_recorded_against_the_consignor(session):
    """There is no batch-level audit table, so the batch link lands on the
    consignor whose holdings actually changed."""
    seed(session)
    with session.begin_nested():
        plan = rcr.apply_retro_consignment(session)
    session.commit()

    rows = session.query(ConsignorChangeLog).filter_by(consignor_id=16).all()
    assert len(rows) == 1
    entry = json.loads(rows[0].change_summary)
    assert entry["action_type"] == "consignment_batch_linked"
    assert entry["before"] == {"is_consignment": False, "consignor_id": None}
    assert entry["after"] == {"is_consignment": True, "consignor_id": 16}
    assert entry["batch_code"] == "CON_RAU"
    assert entry["total_owed"] == 0.77
    assert sorted(entry["cards_backfilled"]) == [6388, 6391]
    assert entry["actor"] == "script:retro_consign_raul"
    assert plan["audit_ids"]["consignor_change_log"] == rows[0].id


def test_the_batch_link_entry_is_not_revertible_through_the_consignor_ui(session):
    """revert_consignor_change only accepts "consignor_updated", so this
    entry shows in the history and is refused rather than half-undone."""
    from consignment_service import ConsignorChangeError, revert_consignor_change

    seed(session)
    with session.begin_nested():
        rcr.apply_retro_consignment(session)
    session.commit()
    log = session.query(ConsignorChangeLog).filter_by(consignor_id=16).one()
    with pytest.raises(ConsignorChangeError, match="Only an edit entry"):
        revert_consignor_change(session, 16, log.id)


def test_the_owed_report_shows_the_total(session):
    from consignment_service import consignor_owed_report

    seed(session)
    with session.begin_nested():
        rcr.apply_retro_consignment(session)
    session.commit()
    report = [r for r in consignor_owed_report(session) if r["consignor"].id == 16]
    assert len(report) == 1
    assert round(report[0]["total_owed"], 2) == 0.77


# --- the dry run writes nothing -------------------------------------------

def test_the_plan_alone_writes_nothing(session):
    seed(session)
    rcr.plan_retro_consignment(session)
    session.commit()
    batch = session.query(Batch).filter_by(batch_code="CON_RAU").one()
    assert batch.is_consignment is False
    assert batch.consignor_id is None
    assert session.query(InventoryChangeLog).count() == 0
    assert session.query(ConsignorChangeLog).count() == 0
