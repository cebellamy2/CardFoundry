"""The targeted MTGJSON backfill for binding 6955.

WHY IT IS TARGETED. execute_mtgjson_backfill is the canonical path and
writes both card and binding, but it is driven by a preview whose candidates
are cards with a NULL mtgjson_id. Card 10797 already has one, so that path
cannot reach this binding -- only the binding's column is missing.

★ THE GUARD UNDER TEST is the three-way agreement: the binding's value must
be NULL, and the card's value must equal Mana Pool's catalog value. Any
disagreement means the premise is wrong, so nothing is written. The tests
below are mostly about the REFUSALS, because those are what make a
hand-made correction safe.
"""
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import backfill_binding_6955_mtgjson as script
from models import (Base, Batch, InventoryCard, InventoryChangeLog,
                    RemoteProductBinding)

MTGJSON = "943111c2-ded8-515b-90d5-947eb729e932"
PRODUCT = script.PRODUCT_ID


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'binding.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def setup_binding(session, *, card_mtgjson=MTGJSON, binding_mtgjson=None,
                  override_at=None, product_id=PRODUCT, extra_card_mtgjson=None):
    batch = Batch(batch_code="CON_RAN")
    session.add(batch)
    session.flush()
    cards = []
    for value in [card_mtgjson] + ([extra_card_mtgjson] if extra_card_mtgjson else []):
        card = InventoryCard(
            batch_id=batch.id, name="Dwarven Warriors", set_code="HOC",
            collector_number="93", mtgjson_id=value, language_id="DW",
            condition_id="NM", finish_id="NF", condition="near_mint",
            finish="normal", scryfall_id="sf-dw", status="available",
            current_price=8.80,
        )
        session.add(card)
        session.flush()
        cards.append(card)
    binding = RemoteProductBinding(
        id=script.BINDING_ID, provider="manapool", product_type="mtg_single",
        product_id=product_id, mtgjson_id=binding_mtgjson, scryfall_id="sf-dw",
        language_id="DW", condition_id="NM", finish_id="NF", set_code="HOC",
        collector_number="93", binding_status="validated",
        local_card_ids_json=json.dumps([c.id for c in cards]),
        requested_identity_json=json.dumps({"name": "Dwarven Warriors"}),
        evidence_hash="fixture", evidence_json="{}",
        validated_at=datetime(2026, 9, 20),
        mtgjson_override_confirmed_at=override_at,
    )
    session.add(binding)
    # COMMITTED, not just flushed: the script's dry run ends in a rollback,
    # and uncommitted fixture rows would be discarded with it -- which would
    # test the harness rather than the script. Production data is committed.
    session.commit()
    return binding, cards


def catalog(value=MTGJSON):
    return lambda product_id: value


# --- the happy path -----------------------------------------------------

def test_the_dry_run_verifies_but_writes_nothing(session):
    binding, _ = setup_binding(session)
    report = script.run(session, confirm=False, catalog_loader=catalog())
    assert report["mode"] == "DRY_RUN"
    assert report["old_mtgjson_id"] is None
    assert report["new_mtgjson_id"] == MTGJSON
    assert session.get(RemoteProductBinding, script.BINDING_ID).mtgjson_id is None
    assert session.query(InventoryChangeLog).count() == 0


def test_confirm_writes_exactly_one_binding_and_audits_it(session):
    setup_binding(session)
    report = script.run(session, confirm=True, catalog_loader=catalog())
    assert report["mode"] == "CONFIRMED"
    assert session.get(RemoteProductBinding, script.BINDING_ID).mtgjson_id == MTGJSON

    logs = session.query(InventoryChangeLog).all()
    assert len(logs) == 1, "one correction, one audit row"
    recorded = json.loads(logs[0].change_summary)
    assert recorded["action_type"] == "binding_mtgjson_backfill"
    # The OLD value is recorded, which is what makes the reversal a single
    # UPDATE rather than a guess.
    assert recorded["old_mtgjson_id"] is None
    assert recorded["new_mtgjson_id"] == MTGJSON
    assert recorded["binding_id"] == script.BINDING_ID


def test_no_inventory_card_is_modified(session):
    """Only the binding's column is missing; the card is already correct and
    must not be touched."""
    _, cards = setup_binding(session)
    before = cards[0].mtgjson_id
    script.run(session, confirm=True, catalog_loader=catalog())
    assert session.get(InventoryCard, cards[0].id).mtgjson_id == before


# --- the refusals, which are the point ----------------------------------

def test_it_refuses_an_override_confirmed_binding(session):
    """6742's NULL is an operator decision ("I have no idea", 2026-09-17)
    that the operator reaffirmed. A script that could overwrite one of those
    by being pointed at it is the wrong shape."""
    setup_binding(session, override_at=datetime(2026, 9, 17, 14, 3, 36))
    with pytest.raises(script.PremiseFailed, match="override-confirmed"):
        script.run(session, confirm=True, catalog_loader=catalog())
    assert session.get(RemoteProductBinding, script.BINDING_ID).mtgjson_id is None


def test_it_refuses_when_the_catalog_disagrees_with_the_card(session):
    setup_binding(session)
    with pytest.raises(script.PremiseFailed, match="does not match"):
        script.run(session, confirm=True,
                   catalog_loader=catalog("ffffffff-0000-0000-0000-000000000000"))
    assert session.get(RemoteProductBinding, script.BINDING_ID).mtgjson_id is None


def test_it_refuses_when_the_catalog_has_no_value(session):
    setup_binding(session)
    with pytest.raises(script.PremiseFailed, match="no MTGJSON id"):
        script.run(session, confirm=True, catalog_loader=catalog(None))


def test_it_refuses_when_the_binding_already_has_a_value(session):
    setup_binding(session, binding_mtgjson=MTGJSON)
    with pytest.raises(script.PremiseFailed, match="already has"):
        script.run(session, confirm=True, catalog_loader=catalog())


def test_it_refuses_when_the_cards_disagree_with_each_other(session):
    setup_binding(session, extra_card_mtgjson="00000000-1111-2222-3333-444444444444")
    with pytest.raises(script.PremiseFailed, match="do not agree"):
        script.run(session, confirm=True, catalog_loader=catalog())


def test_it_refuses_a_binding_on_a_different_product(session):
    setup_binding(session, product_id="some-other-product")
    with pytest.raises(script.PremiseFailed, match="not 44c8137c"):
        script.run(session, confirm=True, catalog_loader=catalog())


def test_it_refuses_when_the_binding_is_absent(session):
    with pytest.raises(script.PremiseFailed, match="does not exist"):
        script.run(session, confirm=True, catalog_loader=catalog())
