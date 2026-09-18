"""Storing, locally, the price a card is actually selling at.

Before v1.185.0 nothing automatic had ever written
InventoryCard.current_price -- production's inventory_price_history held
five rows in the application's whole history, all of them 'manual'. The
cost was 5,883 available cards with no local price, every one of them
unable to be raised by reconciliation or returned to sale by the
quantity push, because both guards read that field.

Three things carry the risk and each is pinned here: the floor (the
stored number is the buyer-facing one, by operator decision), the
audit-row volume (three runs a day over ~6,000 listings, so a no-op must
write NOTHING), and the identity match (the export's five-field key,
which must never reach across two printings).
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import local_price_writeback_service as wb
from models import (
    Base, Batch, InventoryCard, InventoryChangeLog, InventoryPriceHistory,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'wb.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Batch(id=1, batch_code="B1"))
        session.commit()
    return engine


def add_card(session, card_id, *, price=None, status="available",
             set_code="DDG", collector_number="61", language_id="EN",
             condition_id="LP", finish_id="NF", pending=None):
    session.add(InventoryCard(
        id=card_id, batch_id=1, name=f"Card {card_id}", status=status,
        set_code=set_code, collector_number=collector_number,
        language_id=language_id, condition_id=condition_id, finish_id=finish_id,
        mtgjson_id="mtg-a", current_price=price, price_usd=9.99,
        price_pending_since=pending,
    ))


def export_row(*, new="$0.15", status="success", set_code="DDG",
               collector_number="61", language="EN", condition="LP", finish="NF"):
    return {
        "Item": "Thunder Dragon", "New": new, "Status": status,
        "Set Code": set_code, "Collector Number": collector_number,
        "Language": language, "Condition": condition, "Finish": finish,
    }


# --- the floor: what number actually gets stored -------------------------

def test_a_sub_floor_price_is_stored_at_the_floor():
    """Operator decision 2026-09-18: local current_price is the
    BUYER-FACING price. Mana Pool stores the raw $0.15 and applies the
    store minimum at serve time, so $0.65 is what the buyer pays and
    $0.65 is what the card is worth locally."""
    assert wb.floored_cents(15) == 65


def test_a_price_above_the_floor_is_stored_as_is():
    assert wb.floored_cents(12998) == 12998


@pytest.mark.parametrize("value", [None, "", "abc", 0, -5])
def test_nothing_usable_is_never_mistaken_for_a_free_card(value):
    assert wb.floored_cents(value) is None


def test_the_floor_is_the_same_number_the_config_panel_shows():
    """One definition, imported rather than repeated -- the panel and the
    write-back must never give two different answers to "the floor is"."""
    import main
    assert main.PRICING_LOCKED_FLOOR_CENTS == wb.PRICING_FLOOR_CENTS == 65


# --- the write, and its audit rows ---------------------------------------

def test_an_unpriced_card_gets_the_floored_price_and_two_audit_rows(db):
    with Session(db) as session:
        add_card(session, 1, price=None)
        session.commit()
        counts = wb.write_back_bulk_export(session, [export_row(new="$0.15")])
        session.commit()

        card = session.get(InventoryCard, 1)
        assert card.current_price == 0.65
        assert counts["cards_updated"] == 1
        history = session.query(InventoryPriceHistory).all()
        assert len(history) == 1
        assert history[0].old_price is None
        assert history[0].new_price == 0.65
        assert history[0].source == "bulk_market_job"
        assert session.query(InventoryChangeLog).count() == 1


def test_price_usd_is_never_touched(db):
    """It is the import-time record of what the card was adopted at --
    on legacy stock the only surviving evidence of the Mana Pool listing
    price at import."""
    with Session(db) as session:
        add_card(session, 1, price=None)
        session.commit()
        wb.write_back_bulk_export(session, [export_row(new="$4.00")])
        session.commit()
        assert session.get(InventoryCard, 1).price_usd == 9.99


def test_a_no_op_writes_nothing_at_all(db):
    """The bulk job runs three times a day over ~6,000 listings and most
    prices do not move. An audit row per listing per run would be ~18,000
    rows a day saying nothing happened, burying the few that mean
    something."""
    with Session(db) as session:
        add_card(session, 1, price=0.65)
        session.commit()
        counts = wb.write_back_bulk_export(session, [export_row(new="$0.15")])
        session.commit()

        assert counts["cards_unchanged"] == 1
        assert counts["cards_updated"] == 0
        assert session.query(InventoryPriceHistory).count() == 0
        assert session.query(InventoryChangeLog).count() == 0


def test_the_no_op_comparison_is_in_cents_not_floats(db):
    """1.0 and 1.00 are the same price. A float comparison that says
    otherwise would rewrite the whole catalogue every run."""
    with Session(db) as session:
        add_card(session, 1, price=1.0)
        session.commit()
        counts = wb.write_back_bulk_export(session, [export_row(new="$1.00")])
        assert counts["cards_unchanged"] == 1


def test_a_real_move_is_written_and_audited(db):
    with Session(db) as session:
        add_card(session, 1, price=4.00)
        session.commit()
        counts = wb.write_back_bulk_export(session, [export_row(new="$2.50")])
        session.commit()

        assert counts["cards_updated"] == 1
        assert session.get(InventoryCard, 1).current_price == 2.50
        assert session.query(InventoryPriceHistory).one().old_price == 4.00


# --- what is deliberately left alone -------------------------------------

def test_a_card_on_an_operator_hold_is_skipped_and_counted(db):
    """price_pending_since is an explicit 'do not list this yet'
    (CF-SCAN-025). Pricing it would quietly overrule the operator, so it
    is skipped -- and counted, never silently."""
    with Session(db) as session:
        add_card(session, 1, price=None, pending=datetime(2026, 9, 1))
        session.commit()
        counts = wb.write_back_bulk_export(session, [export_row()])
        session.commit()

        assert counts["cards_skipped_hold"] == 1
        assert counts["cards_updated"] == 0
        assert session.get(InventoryCard, 1).current_price is None


@pytest.mark.parametrize("status", ["sold", "removed", "reserved", "unsellable"])
def test_only_available_cards_are_priced(db, status):
    with Session(db) as session:
        add_card(session, 1, price=None, status=status)
        session.commit()
        counts = wb.write_back_bulk_export(session, [export_row()])
        assert counts["cards_updated"] == 0
        assert counts["rows_unmatched"] == 1


@pytest.mark.parametrize("status", ["skipped", "failed"])
def test_a_row_mana_pool_did_not_price_is_not_considered(db, status):
    """Its price did not move remotely, so there is nothing of it to
    record locally."""
    with Session(db) as session:
        add_card(session, 1, price=None)
        session.commit()
        counts = wb.write_back_bulk_export(session, [export_row(status=status)])
        assert counts["rows_considered"] == 0
        assert session.get(InventoryCard, 1).current_price is None


def test_a_row_with_an_unreadable_price_is_counted_not_guessed(db):
    with Session(db) as session:
        add_card(session, 1, price=None)
        session.commit()
        counts = wb.write_back_bulk_export(session, [export_row(new="")])
        assert counts["rows_no_price"] == 1
        assert session.get(InventoryCard, 1).current_price is None


# --- the identity match --------------------------------------------------

def test_every_available_card_of_the_identity_is_priced_not_just_one(db):
    """Mana Pool lists per identity, not per card. Scope is every
    available matching card: an unlisted one is stock the next
    reconciliation raises into that same listing at that same price."""
    with Session(db) as session:
        add_card(session, 1, price=None)
        add_card(session, 2, price=None)
        add_card(session, 3, price=None)
        session.commit()
        counts = wb.write_back_bulk_export(session, [export_row(new="$3.00")])
        session.commit()

        assert counts["cards_updated"] == 3
        assert all(session.get(InventoryCard, i).current_price == 3.00
                   for i in (1, 2, 3))


@pytest.mark.parametrize("differing", [
    {"condition_id": "NM"},
    {"finish_id": "FO"},
    {"language_id": "JA"},
    {"set_code": "DMU"},
    {"collector_number": "62"},
])
def test_a_card_differing_on_any_one_of_the_five_fields_is_not_priced(db, differing):
    """The export's key is the whole five-field tuple. Matching on less
    would cross-price two different printings -- verified against
    production that zero tuples span more than one mtgjson_id, which is
    what makes this key safe in the first place."""
    with Session(db) as session:
        add_card(session, 1, price=None, **differing)
        session.commit()
        counts = wb.write_back_bulk_export(session, [export_row(new="$3.00")])
        assert counts["rows_unmatched"] == 1
        assert session.get(InventoryCard, 1).current_price is None


def test_the_match_ignores_case_and_surrounding_space(db):
    with Session(db) as session:
        add_card(session, 1, price=None, set_code="ddg", condition_id="lp")
        session.commit()
        counts = wb.write_back_bulk_export(
            session, [export_row(set_code=" DDG ", condition="LP", new="$3.00")],
        )
        assert counts["cards_updated"] == 1


def test_nothing_is_written_when_the_export_is_empty(db):
    with Session(db) as session:
        add_card(session, 1, price=None)
        session.commit()
        counts = wb.write_back_bulk_export(session, [])
        assert counts["cards_updated"] == 0
        assert session.get(InventoryCard, 1).current_price is None


# --- publish write-back --------------------------------------------------

def test_publishing_stores_the_price_it_published_at(db):
    """Before v1.185.0 a card went live on Mana Pool at a real computed
    price and stayed locally unpriced -- which its own guards then read
    as 'never priced', blocking the raise and the return-to-sale push for
    a card that is on sale right now."""
    with Session(db) as session:
        add_card(session, 1, price=None)
        session.commit()
        counts = wb.write_back_published_prices(
            session, [{"target_price_cents": 250, "reconfirmed_card_ids": [1]}],
        )
        session.commit()

        assert counts["cards_updated"] == 1
        assert session.get(InventoryCard, 1).current_price == 2.50
        assert session.query(InventoryPriceHistory).one().source == "new_listing_publish"


def test_a_published_price_is_floored_too(db):
    with Session(db) as session:
        add_card(session, 1, price=None)
        session.commit()
        wb.write_back_published_prices(
            session, [{"target_price_cents": 20, "reconfirmed_card_ids": [1]}],
        )
        session.commit()
        assert session.get(InventoryCard, 1).current_price == 0.65


def test_publishing_does_not_overrule_an_operator_hold(db):
    with Session(db) as session:
        add_card(session, 1, price=None, pending=datetime(2026, 9, 1))
        session.commit()
        counts = wb.write_back_published_prices(
            session, [{"target_price_cents": 250, "reconfirmed_card_ids": [1]}],
        )
        assert counts["cards_skipped_hold"] == 1
        assert session.get(InventoryCard, 1).current_price is None


def test_a_published_row_with_no_cards_writes_nothing(db):
    with Session(db) as session:
        counts = wb.write_back_published_prices(
            session, [{"target_price_cents": 250, "reconfirmed_card_ids": []}],
        )
        assert counts == {"cards_updated": 0, "cards_unchanged": 0, "cards_skipped_hold": 0}
