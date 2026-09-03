import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from fulfillment_exception_service import FulfillmentExceptionError, mark_fulfillment_exception
from fulfillment_substitution_service import confirm_substitution, find_substitution_candidates
from models import (
    Base, Batch, Consignor, FulfillmentException, FulfillmentExceptionEvent,
    ImportRecord, InventoryCard, InventoryChangeLog, OrderItem, PickAllocation,
    SalesOrder,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'substitution-service.db'}")
    Base.metadata.create_all(engine)
    return engine


def add_batch(session, *, code=None, is_consignment=False, consignor_id=None):
    batch = Batch(
        batch_code=code or f"BATCH-{session.query(Batch).count() + 1}",
        is_archived=False, is_consignment=is_consignment, consignor_id=consignor_id,
    )
    session.add(batch)
    session.flush()
    return batch


def add_consignor(session, name="Cam"):
    consignor = Consignor(name=name, is_active=True)
    session.add(consignor)
    session.flush()
    return consignor


IDENTITY = dict(
    name="Mirrorform", set_code="ECL", collector_number="308",
    scryfall_id="sf-mirrorform", mtgjson_id="mtg-mirrorform", language_id="EN",
)


def add_card(session, batch, *, condition_id="LP", finish_id="FO", status="available",
             imported_at=None, **overrides):
    values = dict(IDENTITY)
    values.update({
        "batch_id": batch.id, "condition_id": condition_id, "finish_id": finish_id,
        "condition": condition_id, "finish": "foil" if finish_id == "FO" else "normal",
        "status": status, "imported_at": imported_at or datetime.now(),
    })
    values.update(overrides)
    card = InventoryCard(**values)
    session.add(card)
    session.flush()
    return card


def seed_exception(session, *, exception_type="missing", batch=None,
                    condition_id="LP", finish_id="FO"):
    batch = batch or add_batch(session)
    card = add_card(session, batch, condition_id=condition_id, finish_id=finish_id, status="reserved")
    order = SalesOrder(external_order_id="remote-1", status="in_pick_wave")
    session.add(order)
    session.flush()
    item = OrderItem(
        order_id=order.id, name=card.name, set_code=card.set_code,
        collector_number=card.collector_number, scryfall_id=card.scryfall_id,
        mtgjson_id=card.mtgjson_id, language_id=card.language_id,
        condition_id=card.condition_id, finish_id=card.finish_id, quantity=1,
    )
    session.add(item)
    session.flush()
    allocation = PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id, status="allocated",
    )
    session.add(allocation)
    session.commit()
    exception = mark_fulfillment_exception(session, allocation.id, exception_type, "test note")
    session.commit()
    return order, item, card, allocation, exception


# --- find_substitution_candidates: the equivalence rule ----------------------

def test_exact_condition_candidate_is_offered(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session)
        other_batch = add_batch(session)
        candidate = add_card(session, other_batch, condition_id="LP", finish_id="FO")

        rows = find_substitution_candidates(session, exception.id)
        assert [row["card"].id for row in rows] == [candidate.id]


def test_better_condition_candidate_is_offered(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session, condition_id="LP")
        other_batch = add_batch(session)
        candidate = add_card(session, other_batch, condition_id="NM", finish_id="FO")

        rows = find_substitution_candidates(session, exception.id)
        assert [row["card"].id for row in rows] == [candidate.id]


def test_worse_condition_candidate_is_never_offered(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session, condition_id="LP")
        other_batch = add_batch(session)
        add_card(session, other_batch, condition_id="MP", finish_id="FO")
        add_card(session, other_batch, condition_id="HP", finish_id="FO")
        add_card(session, other_batch, condition_id="DMG", finish_id="FO")

        rows = find_substitution_candidates(session, exception.id)
        assert rows == []


def test_different_finish_candidate_is_never_offered(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session, finish_id="FO")
        other_batch = add_batch(session)
        add_card(session, other_batch, condition_id="LP", finish_id="NF")

        rows = find_substitution_candidates(session, exception.id)
        assert rows == []


def test_different_scryfall_id_candidate_is_never_offered(db):
    """Different printing entirely -- name/set/collector/language could
    all coincidentally match and it still must not qualify."""
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session)
        other_batch = add_batch(session)
        add_card(
            session, other_batch, condition_id="LP", finish_id="FO",
            scryfall_id="sf-different-printing",
        )

        rows = find_substitution_candidates(session, exception.id)
        assert rows == []


def test_unavailable_candidate_is_never_offered(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session)
        other_batch = add_batch(session)
        add_card(session, other_batch, condition_id="LP", finish_id="FO", status="sold")

        rows = find_substitution_candidates(session, exception.id)
        assert rows == []


def test_archived_batch_candidate_is_never_offered(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session)
        archived_batch = Batch(batch_code="ARCHIVED", is_archived=True)
        session.add(archived_batch)
        session.flush()
        add_card(session, archived_batch, condition_id="LP", finish_id="FO")

        rows = find_substitution_candidates(session, exception.id)
        assert rows == []


def test_inventory_mismatch_exception_gets_no_candidates(db):
    """v1 scope: substitution only applies to missing-card exceptions --
    the reused resolve_missing_inventory_exception hard-requires it."""
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session, exception_type="inventory_mismatch")
        other_batch = add_batch(session)
        add_card(session, other_batch, condition_id="LP", finish_id="FO")

        rows = find_substitution_candidates(session, exception.id)
        assert rows == []


def test_no_candidates_at_all_returns_empty_list_not_an_error(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session)
        rows = find_substitution_candidates(session, exception.id)
        assert rows == []


# --- ordering: exact match beats an upgrade, even when the upgrade is older -

def test_exact_condition_sorts_above_a_better_condition_even_when_older(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session, condition_id="LP")
        other_batch = add_batch(session)
        older_nm = add_card(
            session, other_batch, condition_id="NM", finish_id="FO",
            imported_at=datetime(2020, 1, 1),
        )
        newer_lp = add_card(
            session, other_batch, condition_id="LP", finish_id="FO",
            imported_at=datetime(2026, 1, 1),
        )

        rows = find_substitution_candidates(session, exception.id)
        assert [row["card"].id for row in rows] == [newer_lp.id, older_nm.id]


def test_within_a_condition_tier_oldest_imported_first(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session, condition_id="LP")
        other_batch = add_batch(session)
        newer = add_card(
            session, other_batch, condition_id="LP", finish_id="FO",
            imported_at=datetime(2026, 1, 1),
        )
        older = add_card(
            session, other_batch, condition_id="LP", finish_id="FO",
            imported_at=datetime(2020, 1, 1),
        )

        rows = find_substitution_candidates(session, exception.id)
        assert [row["card"].id for row in rows] == [older.id, newer.id]


# --- consignment flagging: offered always, flagged when attribution changes -

def test_candidate_from_different_consignor_is_flagged_and_named(db):
    with Session(db) as session:
        consignor_a = add_consignor(session, "Cam")
        consignor_b = add_consignor(session, "Riley")
        original_batch = add_batch(session, is_consignment=True, consignor_id=consignor_a.id)
        order, item, card, allocation, exception = seed_exception(session, batch=original_batch)
        other_batch = add_batch(session, is_consignment=True, consignor_id=consignor_b.id)
        candidate = add_card(session, other_batch, condition_id="LP", finish_id="FO")

        rows = find_substitution_candidates(session, exception.id)
        assert rows[0]["card"].id == candidate.id
        assert rows[0]["changes_consignment"] is True
        assert rows[0]["consignor_name"] == "Riley"


def test_candidate_from_same_consignor_is_not_flagged(db):
    with Session(db) as session:
        consignor = add_consignor(session, "Cam")
        original_batch = add_batch(session, is_consignment=True, consignor_id=consignor.id)
        order, item, card, allocation, exception = seed_exception(session, batch=original_batch)
        other_batch = add_batch(session, is_consignment=True, consignor_id=consignor.id)
        add_card(session, other_batch, condition_id="LP", finish_id="FO")

        rows = find_substitution_candidates(session, exception.id)
        assert rows[0]["changes_consignment"] is False


def test_house_stock_candidate_flagged_when_original_was_consigned(db):
    with Session(db) as session:
        consignor = add_consignor(session, "Cam")
        original_batch = add_batch(session, is_consignment=True, consignor_id=consignor.id)
        order, item, card, allocation, exception = seed_exception(session, batch=original_batch)
        house_batch = add_batch(session, is_consignment=False)
        add_card(session, house_batch, condition_id="LP", finish_id="FO")

        rows = find_substitution_candidates(session, exception.id)
        assert rows[0]["changes_consignment"] is True
        assert rows[0]["consignor_name"] is None


def test_consignment_candidate_offered_not_filtered_even_when_flagged(db):
    """The explicit requirement: never filter, only flag."""
    with Session(db) as session:
        consignor = add_consignor(session, "Cam")
        order, item, card, allocation, exception = seed_exception(session)
        consignment_batch = add_batch(session, is_consignment=True, consignor_id=consignor.id)
        candidate = add_card(session, consignment_batch, condition_id="LP", finish_id="FO")

        rows = find_substitution_candidates(session, exception.id)
        assert len(rows) == 1
        assert rows[0]["card"].id == candidate.id


# --- confirm_substitution: the state-changing transaction ---------------------

def test_successful_substitution_remove_outcome(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session)
        other_batch = add_batch(session)
        candidate = add_card(session, other_batch, condition_id="LP", finish_id="FO")
        candidate_id, card_id, exception_id, item_id, allocation_id = (
            candidate.id, card.id, exception.id, item.id, allocation.id,
        )

        result = confirm_substitution(session, exception_id, candidate_id, "remove", "test")
        assert result["original_card"].id == card_id
        assert result["substitute_card"].id == candidate_id
        session.commit()

    with Session(db) as session:
        candidate = session.get(InventoryCard, candidate_id)
        card = session.get(InventoryCard, card_id)
        exception = session.get(FulfillmentException, exception_id)
        allocations = session.query(PickAllocation).filter_by(order_item_id=item_id).all()

        assert candidate.status == "reserved"
        assert card.status == "removed"  # unchanged by "remove" -- already removed
        assert exception.inventory_resolution_state == "resolved"
        assert exception.submission_state == "not_required"
        assert exception.inventory_card_id == card_id  # never repointed

        new_allocs = [a for a in allocations if a.inventory_card_id == candidate_id]
        assert len(new_allocs) == 1
        assert new_allocs[0].status == "picked"
        exception_alloc = [a for a in allocations if a.id == allocation_id][0]
        assert exception_alloc.status == "exception"  # untouched, historical record


def test_successful_substitution_needs_review_outcome(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session)
        other_batch = add_batch(session)
        candidate = add_card(session, other_batch, condition_id="LP", finish_id="FO")
        card_id, exception_id = card.id, exception.id

        confirm_substitution(session, exception_id, candidate.id, "needs_review", "test")
        session.commit()

    with Session(db) as session:
        exception = session.get(FulfillmentException, exception_id)
        card = session.get(InventoryCard, card_id)
        assert exception.inventory_resolution_state == "unresolved"  # left exactly as-is
        assert exception.submission_state == "not_required"  # order still unblocked
        assert card.status == "removed"  # untouched


def test_substitution_records_a_fulfillment_exception_event(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session)
        other_batch = add_batch(session)
        candidate = add_card(session, other_batch, condition_id="LP", finish_id="FO")
        candidate_id, card_id = candidate.id, card.id

        confirm_substitution(session, exception.id, candidate_id, "needs_review")
        session.commit()

    with Session(db) as session:
        event = session.query(FulfillmentExceptionEvent).filter_by(
            event_type="fulfillment_exception_substituted",
        ).one()
        evidence = json.loads(event.evidence_json)
        assert evidence["substitute_inventory_card_id"] == candidate_id
        assert evidence["original_inventory_card_id"] == card_id


def test_substitution_audits_the_candidates_inventory_change(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session)
        other_batch = add_batch(session)
        candidate = add_card(session, other_batch, condition_id="LP", finish_id="FO")
        candidate_id = candidate.id

        confirm_substitution(session, exception.id, candidate_id, "needs_review")
        session.commit()

    with Session(db) as session:
        logs = session.query(InventoryChangeLog).filter_by(inventory_card_id=candidate_id).all()
        assert len(logs) == 1
        audit = json.loads(logs[0].change_summary)
        assert audit["previous_status"] == "available"
        assert audit["new_status"] == "reserved"


# --- confirm_substitution: guarded, re-validates fresh ------------------------

def test_wrong_exception_type_refused(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session, exception_type="inventory_mismatch")
        other_batch = add_batch(session)
        candidate = add_card(session, other_batch, condition_id="LP", finish_id="FO")

        with pytest.raises(FulfillmentExceptionError, match="missing-card"):
            confirm_substitution(session, exception.id, candidate.id, "needs_review")


def test_candidate_no_longer_available_refused(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session)
        other_batch = add_batch(session)
        candidate = add_card(session, other_batch, condition_id="LP", finish_id="FO", status="sold")

        with pytest.raises(FulfillmentExceptionError, match="no longer available"):
            confirm_substitution(session, exception.id, candidate.id, "needs_review")


def test_candidate_condition_downgraded_between_list_and_confirm_refused(db):
    """Simulates the race: candidate qualified when listed, but its
    condition was corrected downward before this confirm executed."""
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session, condition_id="LP")
        other_batch = add_batch(session)
        candidate = add_card(session, other_batch, condition_id="NM", finish_id="FO")
        session.commit()
        candidate.condition_id = "MP"  # now worse than ordered LP
        session.commit()

        with pytest.raises(FulfillmentExceptionError, match="no longer qualifies"):
            confirm_substitution(session, exception.id, candidate.id, "needs_review")


def test_already_resolved_exception_refused(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session)
        other_batch = add_batch(session)
        candidate1 = add_card(session, other_batch, condition_id="LP", finish_id="FO")
        confirm_substitution(session, exception.id, candidate1.id, "remove")
        session.commit()

        candidate2 = add_card(session, other_batch, condition_id="LP", finish_id="FO")
        with pytest.raises(FulfillmentExceptionError, match="already resolved"):
            confirm_substitution(session, exception.id, candidate2.id, "needs_review")


def test_candidate_cannot_be_the_original_card(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session)
        with pytest.raises(FulfillmentExceptionError, match="different card"):
            confirm_substitution(session, exception.id, card.id, "needs_review")


def test_invalid_outcome_refused(db):
    with Session(db) as session:
        order, item, card, allocation, exception = seed_exception(session)
        other_batch = add_batch(session)
        candidate = add_card(session, other_batch, condition_id="LP", finish_id="FO")
        with pytest.raises(FulfillmentExceptionError, match="valid outcome"):
            confirm_substitution(session, exception.id, candidate.id, "bogus")
