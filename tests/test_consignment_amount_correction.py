"""Audited, operator-authorised correction of a consigned card's amount owed.

apply_consignment_payout_if_consigned deliberately FREEZES the resolved
amount at sale time so a later tier-table edit never retroactively
changes what an already-sold card paid out. That freeze is correct and
stays. This is the explicit override for when the frozen number has to
change anyway -- it takes a named card set and a reason, never a query.

Built for the 2026-09-23 retirement of the $0.10 under-$1 tier: 107
unpaid cards, $10.70.
"""
import json
import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import consignment_service
from consignment_service import (CONSIGNMENT_AMOUNT_CORRECTION_ACTION,
                                 ConsignmentCorrectionError,
                                 apply_consignment_payout_if_consigned,
                                 correct_consignment_amounts,
                                 undo_consignment_amount_correction)
from models import (Base, Batch, Consignor, ConsignorPayout, InventoryCard,
                    InventoryChangeLog)

REASON = "Retired $0.10 under-$1 consignment tier; operator decision 2026-09-23"


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'correct.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def cf_logs(caplog):
    caplog.set_level(logging.DEBUG)
    consignment_service.logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        consignment_service.logger.removeHandler(caplog.handler)


def consigned_batch(session, code="CON_X"):
    c = Consignor(name=f"Owner {code}")
    session.add(c)
    session.flush()
    b = Batch(batch_code=code, is_consignment=True, consignor_id=c.id)
    session.add(b)
    session.flush()
    return b


def card(session, batch=None, *, sold_price=0.65, owed=0.10, status="owed",
         payout_id=None, name="Cheap Card"):
    batch = batch or consigned_batch(session)
    c = InventoryCard(
        batch_id=batch.id, name=name, status="sold", sold_price=sold_price,
        consignment_amount_owed=owed, consignment_payout_status=status,
        consignment_payout_id=payout_id,
    )
    session.add(c)
    session.flush()
    return c


# --- the guards ---------------------------------------------------------

def test_an_empty_card_set_is_refused_there_is_no_all_mode(session):
    with pytest.raises(ConsignmentCorrectionError, match="never operates on an implicit set"):
        correct_consignment_amounts(session, [], 0.00, REASON)


def test_an_empty_reason_is_refused(session):
    c = card(session)
    for reason in ("", "   ", None):
        with pytest.raises(ConsignmentCorrectionError, match="reason is required"):
            correct_consignment_amounts(session, [c.id], 0.00, reason)


def test_a_negative_amount_is_refused(session):
    c = card(session)
    with pytest.raises(ConsignmentCorrectionError, match="cannot be negative"):
        correct_consignment_amounts(session, [c.id], -1.00, REASON)


def test_a_paid_card_is_refused(session):
    """Paid money is corrected only through the payout-correction route."""
    c = card(session, status="paid")
    with pytest.raises(ConsignmentCorrectionError, match="already paid"):
        correct_consignment_amounts(session, [c.id], 0.00, REASON)
    assert c.consignment_amount_owed == 0.10


def test_a_card_attached_to_a_payout_record_is_refused(session):
    payout = ConsignorPayout(consignor_id=1, amount=5.00)
    session.add(payout)
    session.flush()
    c = card(session, payout_id=payout.id)
    with pytest.raises(ConsignmentCorrectionError, match="payout-correction route"):
        correct_consignment_amounts(session, [c.id], 0.00, REASON)


def test_a_card_not_in_a_consignment_batch_is_refused(session):
    plain = Batch(batch_code="A1", is_consignment=False)
    session.add(plain)
    session.flush()
    c = card(session, batch=plain)
    with pytest.raises(ConsignmentCorrectionError, match="not in a consignment batch"):
        correct_consignment_amounts(session, [c.id], 0.00, REASON)


def test_a_missing_card_is_refused(session):
    with pytest.raises(ConsignmentCorrectionError, match="not found"):
        correct_consignment_amounts(session, [999999], 0.00, REASON)


def test_it_is_all_or_nothing_one_bad_card_writes_nothing(session, cf_logs):
    batch = consigned_batch(session)
    good_a = card(session, batch)
    good_b = card(session, batch)
    bad = card(session, batch, status="paid")

    with pytest.raises(ConsignmentCorrectionError):
        correct_consignment_amounts(session, [good_a.id, bad.id, good_b.id], 0.00, REASON)

    assert good_a.consignment_amount_owed == 0.10, "a refused batch writes nothing"
    assert good_b.consignment_amount_owed == 0.10
    assert session.query(InventoryChangeLog).count() == 0
    assert any("REFUSED, nothing written" in r.getMessage() for r in cf_logs.records)


# --- dry run ------------------------------------------------------------

def test_a_dry_run_writes_nothing_and_returns_before_after(session):
    batch = consigned_batch(session)
    a, b = card(session, batch), card(session, batch)

    report = correct_consignment_amounts(
        session, [a.id, b.id], 0.00, REASON, dry_run=True)

    assert report["dry_run"] is True
    assert report["count"] == 2
    assert report["total_before"] == 0.20
    assert report["total_after"] == 0.00
    assert {c["before"] for c in report["cards"]} == {0.10}
    assert {c["after"] for c in report["cards"]} == {0.00}
    assert a.consignment_amount_owed == 0.10, "a dry run must write nothing"
    assert session.query(InventoryChangeLog).count() == 0


# --- the correction itself ---------------------------------------------

def test_it_zeroes_the_amount_and_audits_one_row_per_card(session):
    batch = consigned_batch(session)
    a, b = card(session, batch), card(session, batch)

    report = correct_consignment_amounts(session, [a.id, b.id], 0.00, REASON)

    assert a.consignment_amount_owed == 0.00
    assert b.consignment_amount_owed == 0.00
    assert report["count"] == 2 and report["total_before"] == 0.20

    logs = session.query(InventoryChangeLog).all()
    assert len(logs) == 2, "one audit row per card"
    summary = json.loads(logs[0].change_summary)
    assert summary["action_type"] == CONSIGNMENT_AMOUNT_CORRECTION_ACTION
    assert summary["before"]["consignment_amount_owed"] == 0.10
    assert summary["after"]["consignment_amount_owed"] == 0.00
    assert summary["correction_reason"] == REASON
    assert summary["operator_authorised"] is True


def test_the_payout_status_is_left_alone(session):
    """The correction changes the amount, not whether it is owed."""
    c = card(session)
    correct_consignment_amounts(session, [c.id], 0.00, REASON)
    assert c.consignment_payout_status == "owed"
    assert c.consignment_payout_id is None


def test_duplicate_ids_are_corrected_once(session):
    c = card(session)
    report = correct_consignment_amounts(session, [c.id, c.id, c.id], 0.00, REASON)
    assert report["count"] == 1
    assert session.query(InventoryChangeLog).count() == 1


# --- undo ---------------------------------------------------------------

def test_the_undo_round_trip_restores_the_exact_prior_amounts(session):
    batch = consigned_batch(session)
    a = card(session, batch, owed=0.10)
    b = card(session, batch, owed=0.25)

    correct_consignment_amounts(session, [a.id, b.id], 0.00, REASON)
    assert (a.consignment_amount_owed, b.consignment_amount_owed) == (0.00, 0.00)

    undo = undo_consignment_amount_correction(
        session, "undoing the 2026-09-23 tier correction",
        correction_reason=REASON)

    assert undo["count"] == 2
    assert a.consignment_amount_owed == 0.10, "exact prior amount, not a guess"
    assert b.consignment_amount_owed == 0.25


def test_the_undo_is_itself_audited_and_destroys_nothing(session):
    c = card(session)
    correct_consignment_amounts(session, [c.id], 0.00, REASON)
    undo_consignment_amount_correction(session, "reverting", correction_reason=REASON)

    logs = session.query(InventoryChangeLog).order_by(InventoryChangeLog.id).all()
    assert len(logs) == 2, "the original correction row survives the undo"
    kinds = [json.loads(l.change_summary)["action_type"] for l in logs]
    assert kinds == [CONSIGNMENT_AMOUNT_CORRECTION_ACTION,
                     "consignment_amount_correction_undo"]
    undo_summary = json.loads(logs[1].change_summary)
    assert undo_summary["before"]["consignment_amount_owed"] == 0.00
    assert undo_summary["after"]["consignment_amount_owed"] == 0.10
    assert undo_summary["undoes_action"] == CONSIGNMENT_AMOUNT_CORRECTION_ACTION


def test_a_dry_run_undo_writes_nothing(session):
    c = card(session)
    correct_consignment_amounts(session, [c.id], 0.00, REASON)
    report = undo_consignment_amount_correction(
        session, "checking", correction_reason=REASON, dry_run=True)
    assert report["dry_run"] is True and report["count"] == 1
    assert c.consignment_amount_owed == 0.00, "dry run must not restore"


def test_undoing_with_nothing_to_undo_is_refused(session):
    with pytest.raises(ConsignmentCorrectionError, match="No matching correction"):
        undo_consignment_amount_correction(session, "nothing here")


def test_undoing_unwinds_one_step_at_a_time(session):
    """A card corrected twice must return to its immediately previous
    value, not jump to its oldest."""
    c = card(session, owed=0.10)
    correct_consignment_amounts(session, [c.id], 0.05, "first correction")
    correct_consignment_amounts(session, [c.id], 0.00, "second correction")

    undo_consignment_amount_correction(
        session, "undo the second", correction_reason="second correction")
    assert c.consignment_amount_owed == 0.05


# --- the sale-time freeze is untouched ----------------------------------

def test_the_sale_time_freeze_still_works_unchanged(session):
    batch = consigned_batch(session)
    c = InventoryCard(batch_id=batch.id, name="Sold Card", status="sold",
                      sold_price=10.00)
    session.add(c)
    session.flush()
    apply_consignment_payout_if_consigned(session, c)
    assert c.consignment_amount_owed == 8.00       # 80% band
    assert c.consignment_payout_status == "owed"


def test_a_later_re_resolution_does_not_quietly_revert_a_correction(session):
    """apply_consignment_payout_if_consigned only fires for a card being
    settled at sale. It is not run again over corrected history, so a
    correction cannot be silently undone by the pricing engine."""
    c = card(session, sold_price=0.65, owed=0.10)
    correct_consignment_amounts(session, [c.id], 0.00, REASON)
    assert c.consignment_amount_owed == 0.00

    # Even if it WERE re-run, the retired tier now resolves to 0.00 too.
    apply_consignment_payout_if_consigned(session, c)
    assert c.consignment_amount_owed == 0.00
