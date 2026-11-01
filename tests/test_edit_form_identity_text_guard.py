"""The edit form refuses a condition or finish it cannot read back.

★ THE TRAP THIS CLOSES, found 2026-11-01. Every page in the app
DISPLAYED LP as "Lightly Played" (main.CONDITION_LABELS), while the only
spellings normalized_condition_id reads back were the intake
vocabulary's ("Light Play"). The edit form takes condition as FREE TEXT
and derives the id straight from it, with no validation -- and
normalized_condition_id falls through to the cleaned string for input it
does not know. So an operator who typed the word the app had just shown
them saved condition_id = "LIGHTLY_PLAYED": a value no Mana Pool catalog
variant can match. The identity guard (v1.180.0) then did its job
correctly, zeroed the card's real LP listing, rebound to nothing, and the
card left the shop window with nothing shown to say so.

★ THE BAIT IS GONE; THE GUARD IS NOT. v2.82.0 made CONDITION_LABELS
derive from the intake vocabulary, so no page shows "Lightly Played" any
more and nobody will be led into typing it. These tests keep using it as
the rejected word on purpose: it is still a word this app cannot read
back, and the property under test is that ANY such word is refused
rather than written -- which is what has to hold the day a sixth label
or a new synonym arrives.

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
    # v2.83.0: the sentence is intake_defaults_service's now, shared with
    # the import refusal; the form adds the one clause only it can say.
    assert "Nothing was changed." in response.text


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


# --- the blank box: the other way the same form wiped an identity --------
# ★ FOUND 2026-11-01 while measuring the finish column for the refusal
# above, and PROVED before it was fixed. InventoryCard keeps condition and
# finish twice -- the operator's words, and the listing key -- and this
# form rendered the words while the POST derived the key from them. A card
# with a blank word beside a VALID key rendered an EMPTY box, so a plain
# Save (change the price, type nothing) submitted that blank, derived None
# from it, and wiped the key; the identity machinery then zeroed the
# card's real Mana Pool listing on the way out. Shown here as the shape
# plus the ending, so a regression cannot pass by fixing only the display.
#
# ★ THE SHAPE IS BUILT, NOT BORROWED, and deliberately. Re-measured
# read-only the same day: 814 cards hold a blank word beside a valid key,
# and all 814 are status='sold' -- which this route refuses with a 409
# before it reads a field -- while zero of the 11,471 available cards do.
# So an AVAILABLE card in that state does not exist in production today,
# and these tests construct one: the point of the fix is that it cannot
# hurt anyone if some future path ever makes one.

def available_blank_boxes(db):
    """The production shape, on a card this route will actually accept."""
    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        card.condition = None
        card.finish = None
        session.commit()


def get_edit_page():
    return TestClient(main.app).get("/inventory/1/edit")


def test_a_blank_box_shows_the_stored_listing_key(world):
    db = world["db"]
    available_blank_boxes(db)

    text = get_edit_page().text

    assert 'name="condition"' in text
    assert 'value="LP"' in text, "the condition box was left empty"
    assert 'value="FO"' in text, "the finish box was left empty"


def test_a_card_with_real_text_renders_exactly_as_before(world):
    """The fallback is a fallback: words win over the key."""
    text = get_edit_page().text

    assert 'value="Light Play"' in text
    assert 'value="foil"' in text
    assert 'value="LP"' not in text and 'value="FO"' not in text


def rendered_box(text: str, field: str) -> str:
    """What the form actually puts in one box, read off the page.

    Scraped rather than assumed, because the whole property here is that
    a plain Save submits WHAT THE FORM RENDERED -- a test that types the
    right answer itself would pass with the bug still in place.
    """
    marker = f'name="{field}"'
    assert marker in text, f"no {field} box on the page"
    tail = text[text.index(marker):]
    value = tail[tail.index('value="') + len('value="'):]
    return value[:value.index('"')]


def test_a_plain_save_no_longer_wipes_the_identity(world):
    """★ THE ENDING THIS FIXES, end to end: open the form, change only
    the price, type nothing, and submit exactly what the page rendered."""
    db = world["db"]
    available_blank_boxes(db)
    with_binding(db)
    before_binding = binding_state(db)

    page = get_edit_page().text
    response = save(
        condition=rendered_box(page, "condition"),
        finish=rendered_box(page, "finish"),
        current_price="7.00",
    )

    assert response.status_code == 303
    state = card_state(db)
    assert state["condition_id"] == "LP", "the listing key was wiped"
    assert state["finish_id"] == "FO", "the listing key was wiped"
    assert state["current_price"] == 7.0, "the edit itself did not land"
    assert binding_state(db) == before_binding, "the listing moved"
    assert world["pushes"] == [], "it told Mana Pool something"


def test_a_card_with_real_text_round_trips_its_own_words(world):
    """Same end-to-end path on a card that has words: submitting what the
    page rendered changes nothing and costs no remote write."""
    db = world["db"]
    with_binding(db)
    before_binding = binding_state(db)

    page = get_edit_page().text
    response = save(
        condition=rendered_box(page, "condition"),
        finish=rendered_box(page, "finish"),
    )

    assert response.status_code == 303
    assert card_state(db)["condition"] == "Light Play"
    assert card_state(db)["condition_id"] == "LP"
    assert binding_state(db) == before_binding
    assert world["pushes"] == []


def test_the_old_blank_submission_still_wipes_and_that_is_why_we_prefill(world):
    """The POST is unchanged and still derives the key from the words --
    a blank genuinely submitted is still a blank. The fix is that the form
    no longer RENDERS one, which this pins by contrast: submit the empty
    string the form used to produce and the ending is still there."""
    db = world["db"]
    available_blank_boxes(db)
    with_binding(db)

    response = save(condition="", finish="", current_price="7.00")

    assert response.status_code == 303
    assert card_state(db)["condition_id"] is None
    assert world["pushes"], (
        "this is the ending the prefill exists to keep the form away from; "
        "if it has stopped happening the POST changed and this test is stale"
    )


def test_what_the_form_prefills_is_something_the_guard_accepts(world):
    """★ THE TWO HALVES HAVE TO AGREE. v2.81.1 refuses a value it cannot
    read back, and this fills the box with a stored id -- so every id the
    form could prefill must pass the refusal, or the fix would hand the
    operator a page that refuses itself on Save."""
    for condition_id in identity.VALID_CONDITION_IDS:
        assert identity.condition_refusal(condition_id) is None, condition_id
    for finish_id in identity.VALID_FINISH_IDS:
        assert identity.finish_refusal(finish_id) is None, finish_id


def test_the_operator_accepted_consequence_is_what_happens(world):
    """Saving such a card writes the code into the words column (NULL ->
    'LP'), which the operator accepted on 2026-11-01. No backfill: only a
    card someone actually saves changes."""
    db = world["db"]
    available_blank_boxes(db)
    assert card_state(db)["condition"] is None

    assert save(condition="LP", finish="FO").status_code == 303

    assert card_state(db)["condition"] == "LP"
    assert card_state(db)["finish"] == "FO"


def test_a_sold_card_shows_the_key_too(world):
    """The only cards in this state today -- all 814 of them -- are sold.
    The POST refuses them with a 409, but the page still RENDERS, so this
    is the one place the fix is visible in production right now."""
    db = world["db"]
    available_blank_boxes(db)
    with Session(db) as session:
        session.get(InventoryCard, 1).status = "sold"
        session.commit()

    text = get_edit_page().text

    assert 'value="LP"' in text and 'value="FO"' in text
    assert "disabled" in text, "a sold card's form must stay read-only"
    assert save(condition="LP").status_code == 409


def test_nothing_is_invented_when_there_is_no_key_either(world):
    """Blank words AND no key: the box stays empty rather than guessing.
    Production holds none of these, and a placeholder is not a value."""
    db = world["db"]
    with Session(db) as session:
        card = session.get(InventoryCard, 1)
        card.condition = card.condition_id = None
        card.finish = card.finish_id = None
        session.commit()

    text = get_edit_page().text

    assert 'value=""' in text
    assert 'value="None"' not in text


@pytest.mark.parametrize("text_value,stored_id,expected", [
    ("Light Play", "LP", "Light Play"),
    (None, "LP", "LP"),
    ("", "NF", "NF"),
    ("   ", "NF", "NF"),
    (None, None, ""),
    ("", "", ""),
    (None, "   ", ""),
    ("foil", None, "foil"),
])
def test_the_fallback_rule_itself(text_value, stored_id, expected):
    """A unit on the rule, with no route and no database: the words win,
    the key is the fallback, and whitespace counts as absent."""
    assert identity.identity_box_value(text_value, stored_id) == expected
