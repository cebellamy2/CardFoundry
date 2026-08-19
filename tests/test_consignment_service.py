import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from consignment_service import (
    DEFAULT_CONSIGNMENT_TIERS,
    apply_consignment_payout_if_consigned,
    consignor_owed_report,
    get_consignment_tiers,
    resolve_consignment_payout,
    set_consignment_tiers,
)
from models import Base, Batch, Consignor, InventoryCard


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


def test_catch_all_tier_applies_to_high_prices():
    assert resolve_consignment_payout(DEFAULT_CONSIGNMENT_TIERS, 250.00) == round(250.00 * 0.80, 2)


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
