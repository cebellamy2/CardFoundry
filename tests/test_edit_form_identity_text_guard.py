"""The edit form refuses a condition or finish it cannot read back.

★ THE TRAP THIS CLOSES, found 2026-11-01. Every page in the app displays
LP as "Lightly Played" (main.CONDITION_LABELS), and the only spellings
normalized_condition_id reads back are the intake vocabulary's ("Light
Play"). The edit form takes condition as FREE TEXT and derives the id
straight from it, with no validation -- and normalized_condition_id falls
through to the cleaned string for input it does not know. So an operator
who typed the word the app had just shown them saved
condition_id = "LIGHTLY_PLAYED": a value no Mana Pool catalog variant can
match. The identity guard (v1.180.0) then did its job correctly, zeroed
the card's real LP listing, rebound to nothing, and the card left the
shop window with nothing shown to say so.

★ WHY A REFUSAL AND NOT A CORRECTION. Measured read-only in production
on 2026-11-01: zero of 16,155 cards hold a condition or finish value that
does not round-trip, and all 12,528 bindings key on a valid id. There is
no data to repair. Nothing is written here, which is why the fix needs no
audit row and no undo.

★ WHAT THESE ASSERT. The rendered page the operator actually gets, and
that NOTHING moved -- card, binding and listing row all read back
unchanged, and the Mana Pool pusher never called. The accepted spellings
tested are the ones production actually holds, counted the same day:
'Light Play' (8,625), 'LP' (3,151), 'NM' (3,303), 'mint' (243),
'Near Mint' (6), and blank (812, whose box renders empty -- refusing a
blank would lock every one of them out of being edited at all).
"""
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import identity_change_service as identity
import inventory_sync_service
import main
import manapool_quantity_push_service as push
from models import (
    Base, Batch, InventoryCard, InventoryChangeLog, InventoryListingStatus,
    RemoteProductBinding,
)


@pytest.fixture
def pushes(monkeypatch):
    """Every Mana Pool write this route would make, recorded not sent."""
    recorded = []
    monkeypatch.setattr(
        push, "update_inventory_prices_by_product",
        lambda updates: recorded.append(updates),
    )
    return recorded


@pytest.fixture
def world(tmp_path, monkeypatch, pushes):
    db = create_engine(f"sqlite:///{tmp_path / 'edit-guard.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(database, "engine", db)
    with Session(db) as session:
        session.add(Batch(id=1, batch_code="A1"))
        session.add(InventoryCard(
            id=1, batch_id=1, name="Lightning Bolt", set_code="LEA",
            collector_number="161", status="available", scryfall_id=None,
            mtgjson_id="mtg-lea-161", language_id="EN",
            condition="Light Play", condition_id="LP",
            finish="foil", finish_id="FO",
            current_price=4.5, price_usd=4.5,
        ))
        session.commit()
    return {"db": db, "pushes": pushes}


def with_binding(db):
    """A real validated binding and listing row, so "nothing moved" can
    be asserted about the listing and not only about the card."""
    with Session(db) as session:
        session.add(RemoteProductBinding(
            id=1, provider="manapool", product_type="mtg_single",
            product_id="product-lp", local_card_ids_json=json.dumps([1]),
            requested_identity_json="{}", scryfall_id="lea-161-en",
            set_code="LEA", collector_number="161", mtgjson_id="mtg-lea-161",
            language_id="EN", condition_id="LP", finish_id="FO",
            binding_status="validated", validated_at=datetime(2026, 10, 1),
            evidence_hash="h1", evidence_json="{}",
        ))
        session.add(InventoryListingStatus(
            inventory_card_id=1, listing_status="listed",
        ))
        session.commit()


def form(**overrides):
    data = {
        "name": "Lightning Bolt", "set_code": "LEA", "collector_number": "161",
        "scryfall_id": "", "batch_id": "1", "current_price": "4.50",
        "condition": "Light Play", "finish": "foil",
    }
    data.update(overrides)
    return data


def save(**overrides):
    return TestClient(main.app).post(
        "/inventory/1/edit", data=form(**overrides), follow_redirects=False,
    )


def card_state(db):
    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        return {
            "name": card.name, "condition": card.condition,
            "condition_id": card.condition_id, "finish": card.finish,
            "finish_id": card.finish_id, "batch_id": card.batch_id,
            "current_price": card.current_price,
        }


def binding_state(db):
    with Session(db) as session:
        binding = session.get(RemoteProductBinding, 1)
        row = session.get(InventoryListingStatus, 1)
        return {
            "product_id": binding.product_id,
            "condition_id": binding.condition_id,
            "finish_id": binding.finish_id,
            "members": binding.local_card_ids_json,
            "listing_status": row.listing_status if row else None,
        }


def change_log_count(db):
    with Session(db) as session:
        return session.query(InventoryChangeLog).count()


# --- the trap: the app's own display label ---------------------------------

def test_the_apps_own_display_label_is_refused(world):
    response = save(condition="Lightly Played")

    assert response.status_code == 400
    assert "Lightly Played" in response.text, "it must name what was typed"
    assert "is not one this shop recognises" in response.text
    assert "nothing was changed" in response.text


def test_the_refusal_page_lists_what_to_type_instead(world):
    """Plain words, and the spelling that works sits in the same list."""
    text = save(condition="Lightly Played").text

    for spelling in identity.ACCEPTED_CONDITION_SPELLINGS:
        assert spelling in text, f"{spelling} was not offered"
    for code in identity.VALID_CONDITION_IDS:
        assert code in text, f"the code {code} was not offered"
    assert "Light Play" in text, "the spelling that does work must be offered"


def test_a_refused_edit_writes_absolutely_nothing(world):
    """★ THE PROPERTY THAT MATTERS. Card, binding, listing row, change
    log and Mana Pool all exactly as they were."""
    db = world["db"]
    with_binding(db)
    before_card, before_binding = card_state(db), binding_state(db)
    before_log = change_log_count(db)

    response = save(
        condition="Lightly Played", name="Renamed In The Same Post",
        current_price="99.99", finish="normal",
    )

    assert response.status_code == 400
    assert card_state(db) == before_card, "the card moved"
    assert binding_state(db) == before_binding, "the listing identity moved"
    assert change_log_count(db) == before_log
    assert world["pushes"] == [], "it told Mana Pool something"


@pytest.mark.parametrize("typed", [
    "Lightly Played", "Moderately Played", "Heavily Played",
    "lightly played", "  Lightly Played  ",
])
def test_every_display_label_that_does_not_round_trip_is_refused(world, typed):
    """Three of the five displayed labels break; NM and DMG happen to
    round-trip, which is why this looked like a one-card curiosity."""
    assert save(condition=typed).status_code == 400
    assert card_state(world["db"])["condition_id"] == "LP"


@pytest.mark.parametrize("typed", ["banana", "sleeved", "LPX", "N M"])
def test_any_other_unreadable_condition_is_refused_too(world, typed):
    assert save(condition=typed).status_code == 400


# --- what production actually holds must still save -----------------------

@pytest.mark.parametrize("typed,expected_id", [
    ("Light Play", "LP"),        # 8,625 cards
    ("LP", "LP"),                # 3,151
    ("lp", "LP"),
    ("NM", "NM"),                # 3,303
    ("mint", "NM"),              # 243
    ("Near Mint", "NM"),         # 6
    ("Light Played", "LP"),      # 2, a legacy synonym
    ("light_played", "LP"),      # 1
    ("excellent", "LP"),         # 1
    ("Moderate Play", "MP"),     # 1
    ("Heavy Play", "HP"),        # 1
    ("MP", "MP"),
    ("DMG", "DMG"),
    ("Damaged", "DMG"),
])
def test_every_spelling_production_holds_still_saves(world, typed, expected_id):
    response = save(condition=typed)

    assert response.status_code == 303, response.text[:400]
    assert card_state(world["db"])["condition_id"] == expected_id
    assert card_state(world["db"])["condition"] == typed


def test_a_blank_condition_keeps_todays_behaviour(world):
    """812 cards hold a NULL condition beside a valid id, and their box
    renders EMPTY -- so refusing a blank would lock every one of them out
    of being edited at all. Same shape as the blank-price refusal beside
    it, which refuses REMOVING a price and never blocks an already
    unpriced card."""
    response = save(condition="")

    assert response.status_code == 303
    assert card_state(world["db"])["condition"] is None


def test_a_normal_edit_still_goes_through_untouched(world):
    response = save(name="Lightning Bolt (Retro)", current_price="6.25")

    assert response.status_code == 303
    state = card_state(world["db"])
    assert state["name"] == "Lightning Bolt (Retro)"
    assert state["current_price"] == 6.25
    assert state["condition_id"] == "LP" and state["finish_id"] == "FO"


# --- finish: the same guard on the same form ------------------------------

@pytest.mark.parametrize("typed", ["shiny", "gold", "FOIL-ETCHED", "rainbow"])
def test_an_unreadable_finish_is_refused(world, typed):
    response = save(finish=typed)

    assert response.status_code == 400
    assert typed in response.text
    assert card_state(world["db"])["finish_id"] == "FO"


def test_the_finish_refusal_offers_the_three_spellings(world):
    text = save(finish="shiny").text

    for spelling in identity.ACCEPTED_FINISH_SPELLINGS:
        assert spelling in text
    for code in identity.VALID_FINISH_IDS:
        assert code in text


@pytest.mark.parametrize("typed,expected_id", [
    ("normal", "NF"),            # 12,910 cards
    ("foil", "FO"),              # 2,009
    ("nonfoil", "NF"),           # 380
    ("etched", "EF"),            # 40
    ("Foil", "FO"),              # 1
    ("Normal", "NF"),            # 1
    ("Non-Foil", "NF"),
    ("Etched", "EF"),
    ("NF", "NF"),
    ("EF", "EF"),
])
def test_every_finish_spelling_production_holds_still_saves(world, typed,
                                                            expected_id):
    response = save(finish=typed)

    assert response.status_code == 303, response.text[:400]
    assert card_state(world["db"])["finish_id"] == expected_id


def test_a_blank_finish_keeps_todays_behaviour(world):
    """814 cards hold a NULL finish beside a valid id."""
    assert save(finish="").status_code == 303
    assert card_state(world["db"])["finish"] is None


def test_the_condition_is_reported_first_when_both_are_wrong(world):
    """One message, not two, and the first field on the form wins."""
    text = save(condition="Lightly Played", finish="shiny").text
    assert "Condition" in text
    assert "Finish “shiny”" not in text


# --- the rule lives in the service, not in the route ----------------------

def test_the_guard_is_the_services_and_derives_its_vocabulary(world):
    """★ NOT A THIRD VOCABULARY. The condition spellings ARE the intake
    list, and the finish ids are derived by running the declared
    spellings through the same normalizer the form uses -- so neither can
    drift from what actually decides."""
    from import_service import normalized_condition_id, normalized_finish_id
    from intake_defaults_service import (
        INTAKE_CONDITION_LABELS, VALID_CONDITION_IDS,
    )

    assert identity.ACCEPTED_CONDITION_SPELLINGS == tuple(INTAKE_CONDITION_LABELS)
    assert identity.VALID_CONDITION_IDS == tuple(VALID_CONDITION_IDS)
    for spelling in identity.ACCEPTED_CONDITION_SPELLINGS:
        assert normalized_condition_id(spelling) in identity.VALID_CONDITION_IDS
    for spelling in identity.ACCEPTED_FINISH_SPELLINGS:
        assert normalized_finish_id(spelling) in identity.VALID_FINISH_IDS
    assert identity.VALID_FINISH_IDS == ("NF", "FO", "EF")


def test_the_service_answers_without_a_route_or_a_database():
    """A unit on the rule itself, so a future caller can reuse it."""
    assert identity.condition_refusal("Lightly Played")
    assert identity.condition_refusal("Light Play") is None
    assert identity.condition_refusal("") is None
    assert identity.condition_refusal(None) is None
    assert identity.finish_refusal("shiny")
    assert identity.finish_refusal("foil") is None
    assert identity.identity_text_refusal("Light Play", "foil") is None
    assert identity.identity_text_refusal("Lightly Played", "foil")
    assert identity.identity_text_refusal("Light Play", "shiny")
