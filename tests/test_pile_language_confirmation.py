"""Slice 4-3: the operator's explicit confirmation that a non-English
language is correct on an English printing.

THE GUARD IS NOT RELAXED. production_import_service still refuses every
explicit-vs-printing language mismatch. This records the one exception a
human has looked at, per line, for that printing only.

The confirmation is a FINGERPRINT, not a boolean, which is what makes the
invalidation matrix below fall out of one comparison instead of needing
code that remembers to clear a flag.
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import main
from models import Batch, InventoryCard, PendingPile, PendingPileLine
from production_import_service import (
    ProductionImportError,
    build_production_import_preview,
    language_override_fingerprint,
)
from tests.test_admin_piles import make_line, make_pile, setup_db

HEADERS = (
    "Location,Name,Set code,Collector number,Finish,Scryfall ID,"
    "Scan Order,Price (USD),Quantity,Language,Condition\n"
)


def catalog_lookup(_ids, languages=None):
    return {"meta": {}, "data": []}


def en_printing(ids):
    return {scryfall_id: {
        "id": scryfall_id, "name": "The Ozolith", "set": "iko",
        "collector_number": "237", "lang": "en", "finishes": ["nonfoil"],
    } for scryfall_id in ids}


# ---------------------------------------------------------------------
# The fingerprint itself
# ---------------------------------------------------------------------

def test_the_fingerprint_is_printing_and_language():
    assert language_override_fingerprint("ABC", "ja") == "abc|JA"
    assert language_override_fingerprint(" abc ", " Ja ") == "abc|JA"


def test_a_different_printing_or_language_is_a_different_fingerprint():
    base = language_override_fingerprint("abc", "JA")
    assert language_override_fingerprint("xyz", "JA") != base
    assert language_override_fingerprint("abc", "DE") != base


# ---------------------------------------------------------------------
# ★ The guard honours ONLY a confirmed fingerprint
# ---------------------------------------------------------------------

def _preview(session, confirmed=None):
    contents = (
        HEADERS + "Shelf A,The Ozolith,IKO,237,normal,sf-oz,1,1.00,1,JA,\n"
    ).encode()
    return build_production_import_preview(
        session, contents, "oz.csv", "OZ_BATCH", "Shelf A", [],
        catalog_lookup, scryfall_lookup=en_printing,
        confirmed_language_overrides=confirmed,
    )


def test_without_a_confirmation_the_conflict_still_refuses(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="explicit language JA conflicts"):
            _preview(session)


def test_the_matching_confirmation_lets_it_through(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        result = _preview(session, {language_override_fingerprint("sf-oz", "JA")})
    assert result["normalized_rows"][0]["language_id"] == "JA"


def test_a_confirmation_for_a_different_printing_does_not_help(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="explicit language JA conflicts"):
            _preview(session, {language_override_fingerprint("sf-other", "JA")})


def test_a_confirmation_for_a_different_language_does_not_help(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="explicit language JA conflicts"):
            _preview(session, {language_override_fingerprint("sf-oz", "DE")})


def test_the_default_is_no_confirmations(tmp_path, monkeypatch):
    """★ SEAM A. A caller that says nothing gets today's strict behaviour.
    Production Batch Import is exactly that caller."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="explicit language JA conflicts"):
            _preview(session, None)


def test_the_csv_import_route_passes_no_confirmations():
    """Asserted against the source, so a later edit cannot quietly add a
    default that weakens the CSV path."""
    import inspect
    import production_import_service as svc
    source = inspect.getsource(svc.build_production_import_preview)
    assert "confirmed_language_overrides = frozenset(confirmed_language_overrides or ())" in source
    # Only the pile staging helper supplies them.
    main_source = inspect.getsource(main)
    assert main_source.count("confirmed_language_overrides=") == 1


# ---------------------------------------------------------------------
# ★ THE INVALIDATION MATRIX
# ---------------------------------------------------------------------

def _confirmed_line(db, monkeypatch, language="JA"):
    pile = make_pile(db, "PILE-CONF", is_owned=True)
    line = make_line(
        db, pile.id, scryfall_id="sf-oz", name="The Ozolith", set_code="iko",
        collector_number="237", price_cents=500, offer_cents=350, line_status="pending",
    )
    with Session(db) as session:
        session.get(PendingPileLine, line.id).language = language
        session.commit()
    client = TestClient(main.app)
    response = client.post(
        f"/admin/piles/{pile.id}/lines/{line.id}/confirm-language",
        follow_redirects=False,
    )
    assert response.status_code == 303
    return pile, line, client


def _is_confirmed(db, line_id) -> bool:
    with Session(db) as session:
        line = session.get(PendingPileLine, line_id)
        return bool(main._confirmed_language_overrides([line]))


def test_confirming_records_the_current_fingerprint(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    _pile, line, _client = _confirmed_line(db, monkeypatch)
    with Session(db) as session:
        stored = session.get(PendingPileLine, line.id)
        assert stored.language_override_confirmed_for == "sf-oz|JA"
        assert stored.language_override_confirmed_at is not None
    assert _is_confirmed(db, line.id) is True


def test_a_language_edit_VOIDS_the_confirmation(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile, line, client = _confirmed_line(db, monkeypatch)
    client.post(
        f"/admin/piles/{pile.id}/lines/{line.id}/identity",
        data={"finish": "nonfoil", "condition": "Near Mint", "language": "DE"},
        follow_redirects=False,
    )
    assert _is_confirmed(db, line.id) is False


def test_a_printing_edit_VOIDS_the_confirmation(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    _pile, line, _client = _confirmed_line(db, monkeypatch)
    with Session(db) as session:
        # Whatever changes the printing -- the picker's select route does
        # exactly this -- moves the fingerprint.
        session.get(PendingPileLine, line.id).scryfall_id = "sf-different"
        session.commit()
    assert _is_confirmed(db, line.id) is False


@pytest.mark.parametrize("field,value", [
    ("finish", "foil"),
    ("condition", "Moderate Play"),
])
def test_a_finish_or_condition_edit_does_NOT_void_it(tmp_path, monkeypatch, field, value):
    """Correctly: neither bears on the language conflict."""
    db = setup_db(tmp_path, monkeypatch)
    pile, line, client = _confirmed_line(db, monkeypatch)
    data = {"finish": "nonfoil", "condition": "Near Mint", "language": "JA"}
    data[field] = value
    client.post(
        f"/admin/piles/{pile.id}/lines/{line.id}/identity",
        data=data, follow_redirects=False,
    )
    assert _is_confirmed(db, line.id) is True


# ---------------------------------------------------------------------
# Per line, no cross-pile memory, and the route's own guards
# ---------------------------------------------------------------------

def test_confirmation_is_per_line_not_per_identity(tmp_path, monkeypatch):
    """★ Two copies of the same card are two judgements. Confirming one
    must not confirm the other."""
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-TWO", is_owned=True)
    first = make_line(db, pile.id, scryfall_id="sf-oz", name="The Ozolith",
                      set_code="iko", collector_number="237", price_cents=500,
                      offer_cents=350, line_status="pending")
    second = make_line(db, pile.id, scryfall_id="sf-oz", name="The Ozolith",
                       set_code="iko", collector_number="237", price_cents=500,
                       offer_cents=350, line_status="pending")
    with Session(db) as session:
        for line_id in (first.id, second.id):
            session.get(PendingPileLine, line_id).language = "JA"
        session.commit()
    client = TestClient(main.app)
    client.post(f"/admin/piles/{pile.id}/lines/{first.id}/confirm-language",
                follow_redirects=False)
    with Session(db) as session:
        assert session.get(PendingPileLine, first.id).language_override_confirmed_for
        assert session.get(PendingPileLine, second.id).language_override_confirmed_for is None


def test_there_is_no_confirm_all_control():
    import inspect
    assert "confirm-all-language" not in inspect.getsource(main)


def test_a_finalized_pile_refuses_a_confirmation(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-LOCKED", is_owned=True)
    line = make_line(db, pile.id, price_cents=500, offer_cents=350, line_status="pending")
    with Session(db) as session:
        session.get(PendingPile, pile.id).status = "finalized"
        session.commit()
    client = TestClient(main.app)
    response = client.post(
        f"/admin/piles/{pile.id}/lines/{line.id}/confirm-language",
        follow_redirects=False,
    )
    assert response.status_code == 400
    with Session(db) as session:
        assert session.get(PendingPileLine, line.id).language_override_confirmed_for is None


def test_a_line_from_another_pile_is_refused(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile_a = make_pile(db, "PILE-A", is_owned=True)
    pile_b = make_pile(db, "PILE-B", is_owned=True)
    line_b = make_line(db, pile_b.id, price_cents=500, offer_cents=350, line_status="pending")
    client = TestClient(main.app)
    response = client.post(
        f"/admin/piles/{pile_a.id}/lines/{line_b.id}/confirm-language",
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_the_columns_are_added_by_an_additive_migration():
    import pathlib
    body = (pathlib.Path(main.__file__).parent / "database.py").read_text()
    assert '"language_override_confirmed_at": "DATETIME"' in body
    assert '"language_override_confirmed_for": "VARCHAR"' in body
