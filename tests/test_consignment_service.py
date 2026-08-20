from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from consignment_service import (
    DEFAULT_CONSIGNMENT_TIERS,
    apply_consignment_payout_if_consigned,
    consignor_owed_report,
    consignor_payout_history,
    correct_consignor_payout,
    get_consignment_tiers,
    payout_state_hash,
    record_consignor_payout,
    resolve_consignment_payout,
    set_consignment_tiers,
)
from models import Base, Batch, Consignor, ConsignorPayout, InventoryCard


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'consignment.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def add_card(session, batch, **overrides):
    values = {"batch_id": batch.id, "name": "Alpha"}
    values.update(overrides)
    card = InventoryCard(**values)
    session.add(card)
    session.flush()
    return card


def add_consignor_batch(session, *, consignor_name="Jane", is_active=True):
    consignor = Consignor(name=consignor_name, is_active=is_active)
    session.add(consignor)
    session.flush()
    batch = Batch(batch_code=f"BATCH-{consignor.id}", is_consignment=True, consignor_id=consignor.id)
    session.add(batch)
    session.flush()
    return consignor, batch


# --- resolve_consignment_payout tier boundaries ---

def test_flat_tier_applies_under_one_dollar():
    assert resolve_consignment_payout(DEFAULT_CONSIGNMENT_TIERS, 0.99) == 0.10


def test_flat_tier_applies_at_exactly_one_dollar():
    assert resolve_consignment_payout(DEFAULT_CONSIGNMENT_TIERS, 1.00) == 0.10


def test_second_tier_applies_just_above_one_dollar():
    assert resolve_consignment_payout(DEFAULT_CONSIGNMENT_TIERS, 1.01) == round(1.01 * 0.60, 2)


def test_second_tier_applies_at_upper_boundary():
    assert resolve_consignment_payout(DEFAULT_CONSIGNMENT_TIERS, 2.99) == round(2.99 * 0.60, 2)


def test_third_tier_applies_just_above_second_boundary():
    assert resolve_consignment_payout(DEFAULT_CONSIGNMENT_TIERS, 3.00) == round(3.00 * 0.65, 2)


def test_third_tier_applies_at_upper_boundary():
    assert resolve_consignment_payout(DEFAULT_CONSIGNMENT_TIERS, 4.99) == round(4.99 * 0.65, 2)


def test_catch_all_tier_applies_just_above_third_boundary():
    assert resolve_consignment_payout(DEFAULT_CONSIGNMENT_TIERS, 5.00) == round(5.00 * 0.80, 2)


def test_fourth_tier_applies_at_thirty_five_dollar_boundary_with_no_deduction():
    assert resolve_consignment_payout(DEFAULT_CONSIGNMENT_TIERS, 35.00) == round(35.00 * 0.80, 2)


def test_catch_all_tier_applies_just_above_fourth_boundary_with_shipping_deduction():
    assert resolve_consignment_payout(DEFAULT_CONSIGNMENT_TIERS, 35.01) == round(
        35.01 * 0.80 - 5.50, 2,
    )


def test_catch_all_tier_applies_to_high_prices_with_shipping_deduction():
    assert resolve_consignment_payout(DEFAULT_CONSIGNMENT_TIERS, 250.00) == round(
        250.00 * 0.80 - 5.50, 2,
    )


def test_percent_tier_without_deduction_field_is_unaffected():
    """A tier with no "deduction" key must behave exactly as before --
    the .get() default keeps every pre-existing tier (and any custom
    tier table an operator sets without this field) unchanged."""
    tiers = [{"max_price": None, "type": "percent", "value": 0.80}]
    assert resolve_consignment_payout(tiers, 250.00) == round(250.00 * 0.80, 2)


def test_no_catch_all_band_raises():
    with pytest.raises(ValueError):
        resolve_consignment_payout([{"max_price": 1.0, "type": "flat", "value": 0.10}], 5.00)


# --- get/set_consignment_tiers persistence ---

def test_get_consignment_tiers_defaults_when_unset(session):
    assert get_consignment_tiers(session) == DEFAULT_CONSIGNMENT_TIERS


def test_set_and_get_consignment_tiers_round_trips(session):
    custom = [{"max_price": None, "type": "percent", "value": 0.5}]
    set_consignment_tiers(session, custom)
    session.commit()
    assert get_consignment_tiers(session) == custom


def test_set_consignment_tiers_updates_existing_setting_in_place(session):
    set_consignment_tiers(session, [{"max_price": None, "type": "percent", "value": 0.5}])
    session.commit()
    set_consignment_tiers(session, [{"max_price": None, "type": "flat", "value": 1.0}])
    session.commit()
    assert get_consignment_tiers(session) == [{"max_price": None, "type": "flat", "value": 1.0}]


# --- apply_consignment_payout_if_consigned ---

def test_apply_payout_noop_for_non_consignment_batch(session):
    batch = Batch(batch_code="B1", is_consignment=False)
    session.add(batch)
    session.flush()
    card = add_card(session, batch, sold_price=10.0)
    apply_consignment_payout_if_consigned(session, card)
    assert card.consignment_amount_owed is None
    assert card.consignment_payout_status is None


def test_apply_payout_noop_when_sold_price_unset(session):
    _, batch = add_consignor_batch(session)
    card = add_card(session, batch, sold_price=None)
    apply_consignment_payout_if_consigned(session, card)
    assert card.consignment_amount_owed is None
    assert card.consignment_payout_status is None


def test_apply_payout_freezes_dollar_amount_and_sets_status(session):
    _, batch = add_consignor_batch(session)
    card = add_card(session, batch, sold_price=10.0)
    apply_consignment_payout_if_consigned(session, card)
    assert card.consignment_amount_owed == 8.0
    assert card.consignment_payout_status == "owed"


def test_apply_payout_is_frozen_against_later_tier_table_changes(session):
    _, batch = add_consignor_batch(session)
    card = add_card(session, batch, sold_price=10.0)
    apply_consignment_payout_if_consigned(session, card)
    assert card.consignment_amount_owed == 8.0

    set_consignment_tiers(session, [{"max_price": None, "type": "percent", "value": 0.1}])
    session.commit()
    assert card.consignment_amount_owed == 8.0


# --- consignor_owed_report ---

def test_owed_report_excludes_consignors_with_nothing_owed(session):
    session.add(Consignor(name="Jane"))
    session.commit()
    assert consignor_owed_report(session) == []


def test_owed_report_includes_inactive_consignors_with_balance(session):
    consignor, batch = add_consignor_batch(session, is_active=False)
    card = add_card(session, batch, sold_price=10.0)
    apply_consignment_payout_if_consigned(session, card)
    session.commit()

    report = consignor_owed_report(session)
    assert len(report) == 1
    assert report[0]["consignor"].id == consignor.id
    assert report[0]["total_owed"] == 8.0


def test_owed_report_excludes_cards_never_shipped(session):
    _, batch = add_consignor_batch(session)
    add_card(session, batch, sold_price=None)
    session.commit()
    assert consignor_owed_report(session) == []


def test_owed_report_sorts_by_total_owed_descending(session):
    _, small_batch = add_consignor_batch(session, consignor_name="Small")
    _, big_batch = add_consignor_batch(session, consignor_name="Big")
    small_card = add_card(session, small_batch, sold_price=1.50)
    big_card = add_card(session, big_batch, sold_price=100.0)
    apply_consignment_payout_if_consigned(session, small_card)
    apply_consignment_payout_if_consigned(session, big_card)
    session.commit()

    report = consignor_owed_report(session)
    assert [row["consignor"].name for row in report] == ["Big", "Small"]


def test_owed_report_groups_multiple_cards_per_consignor(session):
    _, batch = add_consignor_batch(session)
    card1 = add_card(session, batch, name="Card A", sold_price=10.0)
    card2 = add_card(session, batch, name="Card B", sold_price=20.0)
    apply_consignment_payout_if_consigned(session, card1)
    apply_consignment_payout_if_consigned(session, card2)
    session.commit()

    report = consignor_owed_report(session)
    assert len(report) == 1
    assert len(report[0]["cards"]) == 2
    assert report[0]["total_owed"] == 8.0 + 16.0


# --- record_consignor_payout ---

def test_record_payout_covers_only_selected_cards(session):
    consignor, batch = add_consignor_batch(session)
    card1 = add_card(session, batch, name="Card A", sold_price=10.0)
    card2 = add_card(session, batch, name="Card B", sold_price=20.0)
    apply_consignment_payout_if_consigned(session, card1)
    apply_consignment_payout_if_consigned(session, card2)
    session.commit()

    record_consignor_payout(
        session, consignor.id, [card1.id], "Cash App", "", datetime(2026, 1, 1),
    )
    session.commit()

    assert card1.consignment_payout_status == "paid"
    assert card2.consignment_payout_status == "owed"


def test_record_payout_computes_total_from_live_owed_amounts(session):
    consignor, batch = add_consignor_batch(session)
    card1 = add_card(session, batch, name="Card A", sold_price=10.0)
    card2 = add_card(session, batch, name="Card B", sold_price=20.0)
    apply_consignment_payout_if_consigned(session, card1)
    apply_consignment_payout_if_consigned(session, card2)
    session.commit()

    payout = record_consignor_payout(
        session, consignor.id, [card1.id, card2.id], "Cash App", "note", datetime(2026, 1, 1),
    )
    session.commit()

    assert payout.amount == 8.0 + 16.0
    assert payout.method == "Cash App"
    assert payout.note == "note"
    assert payout.paid_at == datetime(2026, 1, 1)


def test_record_payout_marks_covered_cards_paid_and_links_payout_id(session):
    consignor, batch = add_consignor_batch(session)
    card = add_card(session, batch, sold_price=10.0)
    apply_consignment_payout_if_consigned(session, card)
    session.commit()

    payout = record_consignor_payout(
        session, consignor.id, [card.id], "Cash App", "", datetime(2026, 1, 1),
    )
    session.commit()

    assert card.consignment_payout_status == "paid"
    assert card.consignment_payout_id == payout.id


def test_record_payout_rejects_unknown_consignor(session):
    with pytest.raises(ValueError, match="Consignor not found"):
        record_consignor_payout(session, 999, [1], "", "", datetime(2026, 1, 1))


def test_record_payout_rejects_empty_selection(session):
    consignor, _ = add_consignor_batch(session)
    with pytest.raises(ValueError, match="Select at least one"):
        record_consignor_payout(session, consignor.id, [], "", "", datetime(2026, 1, 1))


def test_record_payout_rejects_unknown_card_id(session):
    consignor, _ = add_consignor_batch(session)
    with pytest.raises(ValueError, match="not found"):
        record_consignor_payout(session, consignor.id, [999], "", "", datetime(2026, 1, 1))


def test_record_payout_rejects_card_not_belonging_to_consignor(session):
    consignor_a, batch_a = add_consignor_batch(session, consignor_name="A")
    consignor_b, batch_b = add_consignor_batch(session, consignor_name="B")
    card = add_card(session, batch_b, sold_price=10.0)
    apply_consignment_payout_if_consigned(session, card)
    session.commit()
    with pytest.raises(ValueError, match="does not belong to this consignor"):
        record_consignor_payout(session, consignor_a.id, [card.id], "", "", datetime(2026, 1, 1))


def test_record_payout_rejects_card_not_currently_owed(session):
    consignor, batch = add_consignor_batch(session)
    card = add_card(session, batch, sold_price=10.0)
    apply_consignment_payout_if_consigned(session, card)
    session.commit()
    record_consignor_payout(session, consignor.id, [card.id], "", "", datetime(2026, 1, 1))
    session.commit()

    with pytest.raises(ValueError, match="not currently owed"):
        record_consignor_payout(session, consignor.id, [card.id], "", "", datetime(2026, 1, 1))


def test_record_payout_deduplicates_repeated_card_ids(session):
    consignor, batch = add_consignor_batch(session)
    card = add_card(session, batch, sold_price=10.0)
    apply_consignment_payout_if_consigned(session, card)
    session.commit()

    payout = record_consignor_payout(
        session, consignor.id, [card.id, card.id], "", "", datetime(2026, 1, 1),
    )
    session.commit()
    assert payout.amount == 8.0


# --- consignor_payout_history ---

def test_payout_history_orders_most_recent_first(session):
    consignor, batch = add_consignor_batch(session)
    card1 = add_card(session, batch, sold_price=10.0)
    card2 = add_card(session, batch, name="Card B", sold_price=20.0)
    apply_consignment_payout_if_consigned(session, card1)
    apply_consignment_payout_if_consigned(session, card2)
    session.commit()

    record_consignor_payout(session, consignor.id, [card1.id], "", "", datetime(2026, 1, 1))
    session.commit()
    record_consignor_payout(session, consignor.id, [card2.id], "", "", datetime(2026, 6, 1))
    session.commit()

    history = consignor_payout_history(session, consignor.id)
    assert [row["payout"].paid_at for row in history] == [datetime(2026, 6, 1), datetime(2026, 1, 1)]


def test_payout_history_includes_covered_cards(session):
    consignor, batch = add_consignor_batch(session)
    card1 = add_card(session, batch, sold_price=10.0)
    card2 = add_card(session, batch, name="Card B", sold_price=20.0)
    apply_consignment_payout_if_consigned(session, card1)
    apply_consignment_payout_if_consigned(session, card2)
    session.commit()

    record_consignor_payout(
        session, consignor.id, [card1.id, card2.id], "", "", datetime(2026, 1, 1),
    )
    session.commit()

    history = consignor_payout_history(session, consignor.id)
    assert len(history) == 1
    assert {card.id for card in history[0]["cards"]} == {card1.id, card2.id}


def test_payout_history_empty_for_consignor_with_no_payouts(session):
    consignor, _ = add_consignor_batch(session)
    assert consignor_payout_history(session, consignor.id) == []


# --- correct_consignor_payout ---

def test_correct_payout_updates_fields_and_logs_before_after(session):
    consignor, batch = add_consignor_batch(session)
    card = add_card(session, batch, sold_price=10.0)
    apply_consignment_payout_if_consigned(session, card)
    session.commit()
    payout = record_consignor_payout(
        session, consignor.id, [card.id], "Cash App", "original note", datetime(2026, 1, 1),
    )
    session.commit()
    state = payout_state_hash(payout)

    corrected = correct_consignor_payout(
        session, payout.id, state, 6.0, "Venmo", "corrected note", datetime(2026, 1, 2),
        "Refund adjustment",
    )
    session.commit()

    assert corrected.amount == 6.0
    assert corrected.method == "Venmo"
    assert corrected.note == "corrected note"
    assert corrected.paid_at == datetime(2026, 1, 2)


def test_correct_payout_rejects_stale_state(session):
    consignor, batch = add_consignor_batch(session)
    card = add_card(session, batch, sold_price=10.0)
    apply_consignment_payout_if_consigned(session, card)
    session.commit()
    payout = record_consignor_payout(
        session, consignor.id, [card.id], "Cash App", "", datetime(2026, 1, 1),
    )
    session.commit()
    stale_hash = payout_state_hash(payout)
    payout.amount = 999.0
    session.commit()

    with pytest.raises(ValueError, match="changed after review"):
        correct_consignor_payout(
            session, payout.id, stale_hash, 6.0, "Venmo", "", datetime(2026, 1, 2), "reason",
        )


def test_correct_payout_requires_reason(session):
    consignor, batch = add_consignor_batch(session)
    card = add_card(session, batch, sold_price=10.0)
    apply_consignment_payout_if_consigned(session, card)
    session.commit()
    payout = record_consignor_payout(
        session, consignor.id, [card.id], "", "", datetime(2026, 1, 1),
    )
    session.commit()
    state = payout_state_hash(payout)

    with pytest.raises(ValueError, match="reason is required"):
        correct_consignor_payout(session, payout.id, state, 6.0, "", "", datetime(2026, 1, 2), "")


def test_correct_payout_rejects_negative_amount(session):
    consignor, batch = add_consignor_batch(session)
    card = add_card(session, batch, sold_price=10.0)
    apply_consignment_payout_if_consigned(session, card)
    session.commit()
    payout = record_consignor_payout(
        session, consignor.id, [card.id], "", "", datetime(2026, 1, 1),
    )
    session.commit()
    state = payout_state_hash(payout)

    with pytest.raises(ValueError, match="cannot be negative"):
        correct_consignor_payout(session, payout.id, state, -1.0, "", "", datetime(2026, 1, 2), "reason")


def test_correct_payout_missing_payout_raises(session):
    with pytest.raises(ValueError, match="Payout not found"):
        correct_consignor_payout(session, 999, "hash", 6.0, "", "", datetime(2026, 1, 2), "reason")


def test_payout_state_hash_changes_when_amount_changes(session):
    consignor, batch = add_consignor_batch(session)
    card = add_card(session, batch, sold_price=10.0)
    apply_consignment_payout_if_consigned(session, card)
    session.commit()
    payout = record_consignor_payout(
        session, consignor.id, [card.id], "", "", datetime(2026, 1, 1),
    )
    session.commit()
    original_hash = payout_state_hash(payout)
    payout.amount = 999.0
    assert payout_state_hash(payout) != original_hash
