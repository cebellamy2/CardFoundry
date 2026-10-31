"""Parsing an intake CSV must not reach for the process-wide engine.

★ WHAT WENT WRONG, MEASURED 2026-10-31. `database.DATABASE_URL` defaults
to `sqlite:///./cardfoundry.db` -- a RELATIVE path, resolved against the
working directory. parse_production_csv resolved the shop's
intake-default condition through that engine on every parse, so the read
went to whatever database happened to sit in the cwd rather than to the
database the caller was working in. In the operator's checkout a real
development database answered it and the suite was green; in a clean
clone, in CI, or in a git worktree, SQLite created an empty file and 157
tests failed with "no such table: app_settings". The suite's result
depended on an untracked file, and those 157 tests read a live database
on every run.

Two fixes, and these tests pin both:
  * the setting is read from the CALLER'S SESSION when it has one, and
    only when a row actually lacks a condition;
  * tests/conftest.py points `database.engine` at an empty in-memory
    database for every test, so any future reach-through fails the same
    way everywhere instead of passing on one machine.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import database
import manabox_import_service as mis
import production_import_service as pis
from intake_defaults_service import set_default_condition_id
from models import AppSetting, Base

WITH_CONDITION = ("Location,Name,Set code,Collector number,Finish,Scryfall ID,"
                  "Scan Order,Price (USD),Quantity,Language,Condition,MTGJSON ID\n")
WITHOUT_CONDITION = ("Location,Name,Set code,Collector number,Finish,Scryfall ID,"
                     "Scan Order,Price (USD),Quantity,Language,MTGJSON ID\n")


def graded_csv(condition="Near Mint"):
    return (WITH_CONDITION
            + f"Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,{condition},mtg-a\n").encode()


def ungraded_csv():
    """No Condition COLUMN at all -- the warning path reads the setting."""
    return (WITHOUT_CONDITION
            + "Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,mtg-a\n").encode()


@pytest.fixture
def shop(tmp_path):
    """A shop whose intake default is HP, and a session on its database."""
    engine = create_engine(f"sqlite:///{tmp_path / 'shop.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        set_default_condition_id(session, "HP")
        session.commit()
        yield session


def test_the_module_engine_is_isolated_for_every_test():
    """conftest's guard, pinned. If this fails the guard has been lost."""
    with Session(database.engine) as session:
        with pytest.raises(OperationalError):
            session.query(AppSetting).first()


def test_a_fully_graded_csv_never_needs_a_database():
    """Every row graded, so the setting is never consulted -- and with
    the module engine isolated, consulting it would raise."""
    parsed = pis.parse_production_csv(graded_csv())
    assert parsed["physical_rows"][0]["condition_id"] == "NM"
    assert not parsed["errors"], parsed["errors"]


def test_a_blank_condition_reads_the_setting_from_the_callers_session(shop):
    parsed = pis.parse_production_csv(graded_csv(condition=""), session=shop)
    assert parsed["physical_rows"][0]["condition_id"] == "HP"


def test_a_missing_condition_column_reads_it_from_the_session_too(shop):
    parsed = pis.parse_production_csv(ungraded_csv(), session=shop)
    assert parsed["physical_rows"][0]["condition_id"] == "HP"
    assert any("defaults to HP" in warning for warning in parsed["warnings"]), \
        parsed["warnings"]


def test_an_explicitly_passed_default_still_wins_and_reads_nothing(shop):
    """A caller that knows better -- a one-off script -- still says so,
    and the setting is not read at all."""
    parsed = pis.parse_production_csv(
        graded_csv(condition=""), default_condition="DMG", session=shop,
    )
    assert parsed["physical_rows"][0]["condition_id"] == "DMG"


def test_without_a_session_the_module_engine_is_still_the_fallback():
    """The remaining path, stated rather than left implicit.

    A script with no session of its own still gets the shop's setting
    through `database.engine`. Under test that engine is isolated and
    empty, so the read raises -- which is the point: the fallback is
    visible, and no test can take it by accident.
    """
    with pytest.raises(OperationalError):
        pis.parse_production_csv(graded_csv(condition=""))


def test_the_manabox_adapter_passes_the_session_through(shop):
    manabox = (
        "Name,Set code,Collector number,Scryfall ID,Quantity,Foil,"
        "Language,Purchase price\n"
        "Alpha,ONE,1,sf-a,1,normal,en,1.00\n"
    ).encode()
    rows = mis.parse_manabox_csv(manabox, session=shop)["rows"]
    assert rows, "the adapter produced no rows"
    # The LABEL, not the code: PendingPileLine.condition holds the human
    # word, which is this adapter's own documented behaviour.
    assert rows[0]["condition"] == "Heavy Play"


def test_the_preview_builder_reads_the_setting_from_its_own_session(shop):
    """The outcome the operator sees: a staged import of an ungraded file
    takes the shop's default, read from the database it is importing
    into."""
    def catalog_lookup(ids, languages=None):
        return {"meta": {"as_of": "catalog-v1"}, "data": [{
            "name": "Alpha", "set_code": "ONE", "number": "1",
            "scryfall_id": scryfall_id,
            "variants": [{
                "product_type": "mtg_single", "product_id": "product-a",
                "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
            }],
        } for scryfall_id in ids]}

    preview = pis.build_production_import_preview(
        shop, ungraded_csv(), "next.csv", "NEXT", "Shelf A", [], catalog_lookup,
    )
    staged = preview["normalized_rows"] or preview["physical_rows"]
    assert staged[0]["condition_id"] == "HP", staged[0]
