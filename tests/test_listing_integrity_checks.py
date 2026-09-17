"""Standing checks that a Mana Pool listing and local stock still agree.

The check that produced the ticket these were built for walked
RemoteProductBinding.local_card_ids_json and reported 14 orphans. Eleven
were false positives: membership is bookkeeping that goes stale, while
the function doing the actual writing -- _desired_quantity_for_binding --
counts available cards by four-key IDENTITY and ignores membership
whenever the binding has an mtgjson_id.

So the first test here is the one that matters: a stale membership list
with a real card behind the identity is NOT an over-listing, and must not
be reported as one. A check that disagrees with the writer cries wolf
until nobody reads it.
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import json

import listing_integrity_service as integrity
import main
from models import Base, Batch, InventoryCard, RemoteProductBinding

MTG = "mtg-aaa"


def listing(product_id, quantity, *, price=54995, condition="LP",
            finish="FO", language="EN", name="Ranger-Captain of Eos",
            mtgjson=MTG):
    return {
        "product_id": product_id,
        "quantity": quantity,
        "price_cents": price,
        "product": {"single": {
            "name": name, "set": "FCA", "number": "2", "mtgjson_id": mtgjson,
            "language_id": language, "condition_id": condition, "finish_id": finish,
        }},
    }


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'integrity.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    with Session(engine) as session:
        session.add(Batch(id=1, batch_code="B1"))
        session.commit()
    return engine


def add_card(session, card_id, *, status="available", condition="LP",
             finish="FO", language="EN", mtgjson=MTG, name="Ranger-Captain of Eos"):
    session.add(InventoryCard(
        id=card_id, batch_id=1, name=name, set_code="FCA", collector_number="2",
        status=status, mtgjson_id=mtgjson, language_id=language,
        condition_id=condition, finish_id=finish,
    ))


def add_binding(session, binding_id, product_id, card_ids, *, condition="LP",
                finish="FO", language="EN", mtgjson=MTG):
    session.add(RemoteProductBinding(
        id=binding_id, provider="manapool", product_type="mtg_single",
        product_id=product_id, local_card_ids_json=json.dumps(card_ids),
        requested_identity_json="{}", scryfall_id=f"sf-{binding_id}",
        set_code="FCA", collector_number="2", mtgjson_id=mtgjson,
        language_id=language, condition_id=condition, finish_id=finish,
        binding_status="validated", validated_at=datetime(2026, 9, 1),
        evidence_hash=f"h{binding_id}", evidence_json="{}",
    ))


# --- the false positive this exists to prevent ----------------------------

def test_a_stale_membership_list_is_NOT_an_over_listing(db):
    """The exact shape of 11 of the 14 reported "orphans": the binding
    lists a card that is gone, but another card of the same identity IS
    available, so the listing is correctly backed and the quantity the
    system would write matches what Mana Pool has."""
    with Session(db) as session:
        add_card(session, 1, status="removed")      # the one in the list
        add_card(session, 2, status="available")    # the one actually backing it
        add_binding(session, 10, "product-lp", [1])
        session.commit()
        rows = integrity.over_listed_rows(session, [listing("product-lp", 1)])
    assert rows == [], "membership is stale, but the listing is correct"


def test_a_genuinely_empty_listing_IS_reported(db):
    with Session(db) as session:
        add_card(session, 1, status="removed")
        add_binding(session, 10, "product-lp", [1])
        session.commit()
        rows = integrity.over_listed_rows(session, [listing("product-lp", 1)])
    assert len(rows) == 1
    assert rows[0]["sellable_quantity"] == 0
    assert rows[0]["reason"] == "nothing sellable is behind this listing"


def test_listing_more_than_is_sellable_is_reported_with_both_numbers(db):
    with Session(db) as session:
        add_card(session, 1, status="available")
        add_binding(session, 10, "product-lp", [1])
        session.commit()
        rows = integrity.over_listed_rows(session, [listing("product-lp", 3)])
    assert rows[0]["listed_quantity"] == 3
    assert rows[0]["sellable_quantity"] == 1
    assert "3 listed but only 1 sellable" in rows[0]["reason"]


def test_UNDER_listing_is_deliberately_not_reported(db):
    """128 rows were under-listed when this shipped. That is lost sales,
    not oversell, and folding it in would bury the dangerous direction."""
    with Session(db) as session:
        add_card(session, 1, status="available")
        add_card(session, 2, status="available")
        add_binding(session, 10, "product-lp", [1, 2])
        session.commit()
        rows = integrity.over_listed_rows(session, [listing("product-lp", 1)])
    assert rows == []


def test_a_zero_quantity_listing_is_not_reported(db):
    with Session(db) as session:
        add_card(session, 1, status="removed")
        add_binding(session, 10, "product-lp", [1])
        session.commit()
        rows = integrity.over_listed_rows(session, [listing("product-lp", 0)])
    assert rows == []


# --- listings with no binding at all --------------------------------------

def test_an_unbound_listing_with_no_matching_card_is_reported(db):
    """Dwarven Warriors' shape: live at quantity 2, no binding, and no
    card of that identity anywhere. The worst case of the class -- nothing
    local can even address it."""
    with Session(db) as session:
        rows = integrity.over_listed_rows(session, [listing(
            "product-dw", 2, price=1295, condition="NM", finish="NF",
            language="DW", name="Dwarven Warriors", mtgjson="mtg-dw")])
    assert len(rows) == 1
    assert rows[0]["binding_id"] is None
    assert rows[0]["reason"] == "no binding and no matching card in inventory"


def test_an_unbound_listing_WITH_a_matching_card_is_not_reported(db):
    """Smothering Abomination's shape. A real available card is behind it;
    it needs a binding, not a takedown. Reporting it as an over-listing
    would invite zeroing stock we hold."""
    with Session(db) as session:
        add_card(session, 1, status="available", mtgjson="mtg-sa",
                 condition="LP", finish="FO", name="Smothering Abomination")
        session.commit()
        rows = integrity.over_listed_rows(session, [listing(
            "product-sa", 1, price=164, mtgjson="mtg-sa",
            name="Smothering Abomination")])
    assert rows == []


def test_rows_are_highest_value_first(db):
    with Session(db) as session:
        rows = integrity.over_listed_rows(session, [
            listing("cheap", 1, price=15, mtgjson="m1", name="Cheap"),
            listing("dear", 1, price=54995, mtgjson="m2", name="Dear"),
        ])
    assert [r["name"] for r in rows] == ["Dear", "Cheap"]


# --- identity drift -------------------------------------------------------

def test_a_card_on_a_binding_with_a_different_condition_is_drift(db):
    with Session(db) as session:
        add_card(session, 1, status="available", condition="LP")
        add_binding(session, 10, "product-hp", [1], condition="HP")
        session.commit()
        rows = integrity.identity_drift_rows(session)
    assert len(rows) == 1
    assert rows[0]["differs_on"] == ["condition_id"]
    assert "different condition" in rows[0]["reason"]


def test_a_matching_card_is_not_drift(db):
    with Session(db) as session:
        add_card(session, 1, status="available", condition="LP")
        add_binding(session, 10, "product-lp", [1], condition="LP")
        session.commit()
        assert integrity.identity_drift_rows(session) == []


def test_a_removed_cards_stale_membership_is_not_reported(db):
    """Clearing it would be rewriting history, and it costs nothing:
    the quantity rule never counted it."""
    with Session(db) as session:
        add_card(session, 1, status="removed", condition="LP")
        add_binding(session, 10, "product-hp", [1], condition="HP")
        session.commit()
        assert integrity.identity_drift_rows(session) == []


def test_case_differences_are_not_drift(db):
    with Session(db) as session:
        add_card(session, 1, status="available", mtgjson=MTG.upper())
        add_binding(session, 10, "product-lp", [1], mtgjson=MTG.lower())
        session.commit()
        assert integrity.identity_drift_rows(session) == []


# --- logging --------------------------------------------------------------

def test_both_counts_are_logged_with_greppable_markers(db, caplog):
    import logging

    logger = logging.getLogger("cardfoundry")
    handler = caplog.handler
    logger.addHandler(handler)          # propagate=False; caplog sees nothing otherwise
    logger.setLevel(logging.INFO)
    try:
        with Session(db) as session:
            add_card(session, 1, status="removed")
            add_binding(session, 10, "product-lp", [1])
            # A DIFFERENT card entirely for the drift half -- giving it the
            # same identity as binding 10 would make it back that listing
            # and the over-listed count would correctly drop to zero.
            add_card(session, 2, status="available", condition="LP", mtgjson="mtg-bbb")
            add_binding(session, 11, "product-hp", [2], condition="HP", mtgjson="mtg-bbb")
            session.commit()
            result = integrity.log_listing_integrity(
                session, [listing("product-lp", 1)])
    finally:
        logger.removeHandler(handler)

    assert len(result["over_listed"]) == 1
    assert len(result["identity_drift"]) == 1
    assert integrity.OVER_LISTED_MARKER in caplog.text
    assert integrity.IDENTITY_DRIFT_MARKER in caplog.text
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 2, "non-zero counts must be WARNING, not INFO"


def test_clean_counts_log_at_info_not_warning(db, caplog):
    import logging

    logger = logging.getLogger("cardfoundry")
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.INFO)
    try:
        with Session(db) as session:
            integrity.log_listing_integrity(session, [])
    finally:
        logger.removeHandler(caplog.handler)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# --- display --------------------------------------------------------------

def test_nothing_renders_when_both_checks_are_clean(db):
    assert main._listing_integrity_section([], []) == ""


def test_the_attention_page_shows_drift_when_present(db):
    with Session(db) as session:
        add_card(session, 1, status="available", condition="LP")
        add_binding(session, 10, "product-hp", [1], condition="HP")
        session.commit()
    text = TestClient(main.app).get("/orders/shipment-sync-issues").text
    assert "Cards attached to the wrong listing (1)" in text
    assert "Ranger-Captain of Eos" in text
    assert "different condition" in text


def test_the_attention_page_stays_quiet_when_clean(db):
    text = TestClient(main.app).get("/orders/shipment-sync-issues").text
    assert "Cards attached to the wrong listing" not in text
    assert "Listings advertising more than we can sell" not in text


def test_the_over_listed_table_leads_with_value(db):
    section = main._listing_integrity_section([
        {"name": "Dear", "set_code": "X", "collector_number": "1",
         "identity": "EN/LP/FO", "listed_quantity": 1, "sellable_quantity": 0,
         "price_cents": 54995, "reason": "nothing sellable is behind this listing"},
    ], [])
    assert "Listings advertising more than we can sell (1)" in section
    assert "$549.95" in section
    assert "2026-09-07 oversell" in section
