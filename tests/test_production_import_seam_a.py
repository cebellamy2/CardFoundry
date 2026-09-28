"""SEAM A -- Production Batch Import must refuse in v2.2.1 everything it
refuses in v2.2.0.

Written BEFORE the Scryfall stage was changed from raise-on-first-row to
collect-then-raise, and run green against the old code first, so the diff
is provably behaviour-preserving for the CSV path. The language guard in
production_import_service is SHARED with pile finalize; this file is what
stops the pile work loosening it.

Every assertion here is on the CSV import path, which never passes a
confirmation set and therefore can never benefit from one.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import Base, Batch, InventoryCard
from production_import_service import (
    ProductionImportError,
    build_production_import_preview,
)

# Copied verbatim from tests/test_production_import_service.py so both
# files feed the parser the same shape.
HEADERS = (
    "Location,Name,Set code,Collector number,Finish,Scryfall ID,"
    "Scan Order,Price (USD),Quantity,Language,Condition\n"
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'seam_a.db'}")
    Base.metadata.create_all(engine)
    return engine


def csv_bytes(rows):
    return (HEADERS + "\n".join(rows) + "\n").encode()


def catalog_lookup(_ids, languages=None):
    return {"meta": {}, "data": []}


def preview(session, contents, lookup):
    return build_production_import_preview(
        session, contents, "seam.csv", "SEAM_BATCH", "Shelf A", [],
        catalog_lookup, scryfall_lookup=lookup,
    )


# ---------------------------------------------------------------------
# Each of the four Scryfall-stage refusals, still refused
# ---------------------------------------------------------------------

def test_a_printing_scryfall_does_not_know_is_still_refused(db):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-missing,1,1.00,1,,"])
    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="Scryfall printing was not found"):
            preview(session, contents, lambda ids: {})
        assert session.query(Batch).count() == 0
        assert session.query(InventoryCard).count() == 0


def test_metadata_that_contradicts_the_csv_is_still_refused(db):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,"])

    def lookup(ids):
        # Scryfall says this id is a different set entirely.
        return {"sf-a": {"id": "sf-a", "name": "Alpha", "set": "two",
                         "collector_number": "1", "lang": "en"}}

    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="Scryfall printing metadata conflicts"):
            preview(session, contents, lookup)
        assert session.query(Batch).count() == 0


def test_an_unsupported_scryfall_language_is_still_refused(db):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,"])

    def lookup(ids):
        return {"sf-a": {"id": "sf-a", "name": "Alpha", "set": "one",
                         "collector_number": "1", "lang": "xx"}}

    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="unsupported Scryfall language xx"):
            preview(session, contents, lookup)
        assert session.query(Batch).count() == 0


def test_an_explicit_language_conflicting_with_the_printing_is_still_refused(db):
    """★ THE GUARD THE PILE WORK MUST NOT LOOSEN. A CSV import has no pile
    line behind it and so no confirmation to offer -- this must keep
    raising however the pile flow evolves."""
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-ja,1,1.00,1,EN,"])

    def lookup(ids):
        return {"sf-ja": {"id": "sf-ja", "name": "Alpha", "set": "one",
                          "collector_number": "1", "lang": "ja"}}

    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="explicit language EN conflicts"):
            preview(session, contents, lookup)
        assert session.query(Batch).count() == 0


def test_the_reverse_conflict_is_refused_too(db):
    """The Ozolith shape: an English printing tagged Japanese. Correct for
    Mana Pool, and still refused on the CSV path."""
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-en,1,1.00,1,JA,"])

    def lookup(ids):
        return {"sf-en": {"id": "sf-en", "name": "Alpha", "set": "one",
                          "collector_number": "1", "lang": "en"}}

    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="explicit language JA conflicts"):
            preview(session, contents, lookup)
        assert session.query(Batch).count() == 0


# ---------------------------------------------------------------------
# What is allowed stays allowed
# ---------------------------------------------------------------------

def test_no_explicit_language_still_inherits_the_printings_language(db):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-ja,1,1.00,1,,"])

    def lookup(ids):
        return {"sf-ja": {"id": "sf-ja", "name": "Alpha", "set": "one",
                          "collector_number": "1", "lang": "ja"}}

    with Session(db) as session:
        result = preview(session, contents, lookup)
    assert result["normalized_rows"][0]["language_id"] == "JA"


def test_an_explicit_language_that_agrees_is_accepted(db):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-ja,1,1.00,1,JA,"])

    def lookup(ids):
        return {"sf-ja": {"id": "sf-ja", "name": "Alpha", "set": "one",
                          "collector_number": "1", "lang": "ja"}}

    with Session(db) as session:
        result = preview(session, contents, lookup)
    assert result["normalized_rows"][0]["language_id"] == "JA"


# ---------------------------------------------------------------------
# ★ Two bad rows: still refused, and now BOTH are named
# ---------------------------------------------------------------------

def test_a_csv_with_two_bad_rows_is_still_refused(db):
    """★ THE ONE TEST HERE THAT FAILS BEFORE THE CHANGE, deliberately.
    Refusal is the pre-existing behaviour and must not move; naming BOTH
    rows in one pass is the new part."""
    contents = csv_bytes([
        "Shelf A,Alpha,ONE,1,normal,sf-ja,1,1.00,1,EN,",
        "Shelf A,Beta,ONE,2,normal,sf-ja2,1,1.00,1,EN,",
    ])

    def lookup(ids):
        return {
            "sf-ja": {"id": "sf-ja", "name": "Alpha", "set": "one",
                      "collector_number": "1", "lang": "ja"},
            "sf-ja2": {"id": "sf-ja2", "name": "Beta", "set": "one",
                       "collector_number": "2", "lang": "ja"},
        }

    with Session(db) as session:
        with pytest.raises(ProductionImportError) as exc:
            preview(session, contents, lookup)
        assert session.query(Batch).count() == 0
    # Row 2 was always named. Row 3 is the new part -- one pass, both rows.
    assert "Row 2" in str(exc.value)
    assert "Row 3" in str(exc.value)
