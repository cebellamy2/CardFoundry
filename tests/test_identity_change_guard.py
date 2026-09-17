"""Changing a card's listing identity must take the old listing down first.

Printing, condition, finish and language are the four fields a Mana Pool
listing is keyed on. Until v1.180.0 both paths that change them told Mana
Pool nothing, leaving the old listing live at the old product_id with its
quantity intact and nothing backing it. That is the 2026-09-07 incident's
exact shape: v1.119.0's condition backfill orphaned 1,924 listings and six
real orders arrived against them.

The property under test is an ordering one, and ordering is what makes it
correct: resolve the old bindings BEFORE the identity moves (afterwards an
identity lookup finds the NEW binding), push AFTER it moves (the push
recounts from local state, so the card being gone is what makes the number
drop), detach LAST (detaching first destroys the product_id there was
something to push to).
"""
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import identity_change_service as identity
import manapool_quantity_push_service as push
from models import (
    Base, Batch, InventoryCard, InventoryChangeLog, InventoryListingStatus,
    RemoteProductBinding,
)


@pytest.fixture
def pushes(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        push, "update_inventory_prices_by_product",
        lambda updates: recorded.append(updates),
    )
    return recorded


@pytest.fixture
def failing_push(monkeypatch):
    def boom(_updates):
        raise RuntimeError("Mana Pool 503")

    monkeypatch.setattr(push, "update_inventory_prices_by_product", boom)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'identity.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Batch(id=1, batch_code="B1"))
        session.add(InventoryCard(
            id=1, batch_id=1, name="Snapcaster Mage", set_code="FIC",
            collector_number="469", status="available",
            mtgjson_id="mtg-fic-469", language_id="EN",
            condition_id="LP", finish_id="FO",
        ))
        session.add(RemoteProductBinding(
            id=1, provider="manapool", product_type="mtg_single",
            product_id="product-en", local_card_ids_json=json.dumps([1]),
            requested_identity_json="{}", scryfall_id="fic-469-en",
            set_code="FIC", collector_number="469", mtgjson_id="mtg-fic-469",
            language_id="EN", condition_id="LP", finish_id="FO",
            binding_status="validated", validated_at=datetime(2026, 9, 1),
            evidence_hash="h1", evidence_json="{}",
        ))
        session.add(InventoryListingStatus(
            inventory_card_id=1, listing_status="listed",
        ))
        session.commit()
    return engine


# --- the predicate --------------------------------------------------------

def test_only_the_four_mana_pool_keys_count_as_an_identity_change(db):
    """A re-labelled printing whose mtgjson identity has not moved is the
    same listing, and must not pay for a remote write."""
    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        before = identity.identity_snapshot(card)
        card.set_code = "fic"
        card.collector_number = "469a"
        card.scryfall_id = "something-else"
        assert identity.identity_would_change_from(before, card) is False

        card.language_id = "JA"
        assert identity.identity_would_change_from(before, card) is True


@pytest.mark.parametrize("field,value", [
    ("language_id", "JA"),
    ("condition_id", "NM"),
    ("finish_id", "NF"),
    ("mtgjson_id", "mtg-other"),
])
def test_each_listing_key_counts(db, field, value):
    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        before = identity.identity_snapshot(card)
        setattr(card, field, value)
        assert identity.identity_would_change_from(before, card) is True


def test_case_differences_are_not_an_identity_change(db):
    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        before = identity.identity_snapshot(card)
        card.language_id = "en"
        assert identity.identity_would_change_from(before, card) is False


# --- the ordering that makes it correct -----------------------------------

def test_the_old_binding_is_resolved_before_the_identity_moves(db):
    """Afterwards an identity lookup finds the NEW binding. Pushing that
    one would set the new listing's quantity and leave the old listing
    exactly as it was -- the orphan, plus a wasted write."""
    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        before = identity.bindings_to_retire(session, card)
        assert [b.product_id for b in before] == ["product-en"]

        card.language_id = "JA"
        session.flush()
        after = identity.bindings_to_retire(session, card)
        assert [b.product_id for b in after] == ["product-en"], (
            "still found only via local_card_ids_json, NOT via identity"
        )


def test_the_push_writes_the_reduced_quantity_not_the_old_one(db, pushes):
    """No arithmetic of our own: the push recounts sellable cards under the
    old identity, and the card having moved is what makes it drop."""
    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        bindings = identity.bindings_to_retire(session, card)
        card.language_id = "JA"
        result = identity.retire_old_listings(session, bindings, card.id)

    assert len(pushes) == 1
    assert pushes[0] == [{
        "product_type": "mtg_single", "product_id": "product-en",
        "price_cents": None, "quantity": 0,
    }]
    assert result["pushed"][0]["quantity_written"] == 0


def test_a_sibling_card_keeps_the_old_listing_alive(db, pushes):
    """Two cards on one listing: moving one must reduce to 1, not to 0."""
    with Session(db) as session:
        session.add(InventoryCard(
            id=2, batch_id=1, name="Snapcaster Mage", set_code="FIC",
            collector_number="469", status="available",
            mtgjson_id="mtg-fic-469", language_id="EN",
            condition_id="LP", finish_id="FO",
        ))
        binding = session.get(RemoteProductBinding, 1)
        binding.local_card_ids_json = json.dumps([1, 2])
        session.commit()

    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        bindings = identity.bindings_to_retire(session, card)
        card.language_id = "JA"
        identity.retire_old_listings(session, bindings, card.id)

    assert pushes[0][0]["quantity"] == 1, "the sibling still backs it"


def test_an_unlisted_card_makes_no_mana_pool_call_at_all(db, pushes):
    with Session(db) as session:
        session.query(RemoteProductBinding).delete()
        session.commit()
    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        bindings = identity.bindings_to_retire(session, card)
        card.language_id = "JA"
        result = identity.retire_old_listings(session, bindings, card.id)

    assert pushes == []
    assert result == {"pushed": [], "bindings": 0}


def test_a_failed_remote_write_raises_rather_than_being_recorded(db, failing_push):
    """_push_bindings deliberately swallows failures -- it follows a local
    change that has already committed. Here the local change has NOT
    committed and must not, so this one raises."""
    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        bindings = identity.bindings_to_retire(session, card)
        card.language_id = "JA"
        with pytest.raises(push.QuantityPushFailed, match="Mana Pool 503"):
            identity.retire_old_listings(session, bindings, card.id)


def test_the_listing_status_cache_is_cleared_not_set_to_not_listed(db):
    """After a correction the honest answer is "not known until the next
    reconciliation". not_listed would be a claim."""
    with Session(db) as session:
        assert session.get(InventoryListingStatus, 1).listing_status == "listed"
        identity.clear_listing_status(session, 1)
        session.commit()
    with Session(db) as session:
        assert session.get(InventoryListingStatus, 1) is None


# --- through the real correction path -------------------------------------

def _correction_fixture(db):
    """Minimal reviewed/current pair for apply_printing_correction."""
    from printing_correction_service import _card_snapshot

    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        before = _card_snapshot(card)
        after = dict(before)
        after.update({
            "language_id": "JA", "mtgjson_id": "mtg-sld-808",
            "set_code": "SLD", "collector_number": "808",
            "scryfall_id": "sld-808-ja", "name": "Snapcaster Mage",
            "color": "U", "flavor_name": None,
            "catalog_scryfall_id": "sld-808-ja",
        })
        reviewed = {
            "evidence_hash": "e1", "card_before": before, "card_after": after,
            "resolution": {"product_id": "product-ja",
                           "source_type": "validated_new_product_binding"},
            "catalog_as_of": None,
        }
        return reviewed, dict(reviewed)


def test_correcting_a_listed_card_zeroes_the_old_listing_then_rebinds(db, pushes):
    from printing_correction_service import apply_printing_correction

    reviewed, current = _correction_fixture(db)
    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        result = apply_printing_correction(session, card, reviewed, current)
        session.commit()

    assert pushes == [[{
        "product_type": "mtg_single", "product_id": "product-en",
        "price_cents": None, "quantity": 0,
    }]], "the OLD listing was zeroed, exactly once"
    assert result["retired_listings"][0]["product_id"] == "product-en"

    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        assert card.language_id == "JA"
        assert card.mtgjson_id == "mtg-sld-808"
        assert session.get(InventoryListingStatus, 1) is None, "cache cleared"
        bindings = session.query(RemoteProductBinding).all()
        assert [b.product_id for b in bindings] == ["product-ja"], (
            "old binding deleted (last card), new one created"
        )
        log = session.query(InventoryChangeLog).one()
        stored = json.loads(log.change_summary.split("printing correction: ", 1)[1])
        assert stored["before"]["language_id"] == "EN"
        assert stored["after"]["language_id"] == "JA"
        assert stored["retired_listings"][0]["quantity_written"] == 0


def test_a_failed_push_aborts_the_correction_and_changes_nothing(db, failing_push):
    from printing_correction_service import apply_printing_correction

    reviewed, current = _correction_fixture(db)
    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        with pytest.raises(push.QuantityPushFailed):
            with session.begin_nested():
                apply_printing_correction(session, card, reviewed, current)
        session.commit()

    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        assert card.language_id == "EN", "untouched"
        assert card.mtgjson_id == "mtg-fic-469"
        assert card.set_code == "FIC"
        binding = session.query(RemoteProductBinding).one()
        assert binding.product_id == "product-en", "still bound to the old listing"
        assert json.loads(binding.local_card_ids_json) == [1]
        assert session.get(InventoryListingStatus, 1).listing_status == "listed"
        assert session.query(InventoryChangeLog).count() == 0


def test_an_unlisted_card_corrects_with_no_mana_pool_call(db, pushes):
    from printing_correction_service import apply_printing_correction

    with Session(db) as session:
        session.query(RemoteProductBinding).delete()
        session.query(InventoryListingStatus).delete()
        session.commit()

    reviewed, current = _correction_fixture(db)
    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        apply_printing_correction(session, card, reviewed, current)
        session.commit()

    assert pushes == [], "nothing was listed, so nothing to take down"
    with Session(db) as session:
        assert session.get(InventoryCard, 1).language_id == "JA"


def test_a_correction_that_does_not_move_the_identity_makes_no_call(db, pushes):
    """Re-labelling set/collector/scryfall without moving the mtgjson
    identity is the same listing."""
    from printing_correction_service import _card_snapshot, apply_printing_correction

    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        before = _card_snapshot(card)
        after = dict(before)
        after.update({
            "set_code": "fic", "collector_number": "469",
            "scryfall_id": "relabelled", "name": "Snapcaster Mage",
            "color": "U", "flavor_name": None, "catalog_scryfall_id": "relabelled",
        })
        reviewed = {
            "evidence_hash": "e1", "card_before": before, "card_after": after,
            "resolution": {"product_id": "product-en",
                           "source_type": "validated_new_product_binding"},
            "catalog_as_of": None,
        }
        apply_printing_correction(session, card, reviewed, dict(reviewed))
        session.commit()

    assert pushes == []
