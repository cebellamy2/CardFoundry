"""The shop's intake defaults are a setting, not a constant.

Operator, 2026-10-11: "so that when we sell this product different stores
can apply their own philosophy." The condition a card gets when nobody
typed one is a grading policy, so it belongs to the shop, not the code.

The property that matters most: AN EXPLICIT CONDITION ALWAYS WINS. Every
test below that supplies one asserts the setting did not override it.
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
from intake_defaults_service import (
    DEFAULT_CONDITION_SETTING_KEY, FALLBACK_CONDITION_ID, IntakeDefaultError,
    condition_label_for, default_condition_id, default_condition_label,
    set_default_condition_id, setting_history,
)
from models import AppSetting, AppSettingChangeLog, Base


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'intake.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'routes.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    import database
    monkeypatch.setattr(database, "engine", engine)
    return TestClient(main.app), engine



# Same CSV shape the production-import tests use, so these exercise the
# real parser rather than a reduced one.
_HEADERS = ("Location,Name,Set code,Collector number,Finish,Scryfall ID,"
            "Scan Order,Price (USD),Quantity,Language,Condition,MTGJSON ID\n")


def _csv(condition=""):
    row = f"Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,{condition},mtg-a"
    return (_HEADERS + row + "\n").encode()


# --- the setting ----------------------------------------------------------

def test_an_unset_database_behaves_as_LP(session):
    assert default_condition_id(session) == "LP"
    assert FALLBACK_CONDITION_ID == "LP"


def test_a_stored_value_is_used(session):
    set_default_condition_id(session, "MP")
    session.commit()
    assert default_condition_id(session) == "MP"


def test_a_lowercase_value_is_accepted_and_normalised(session):
    set_default_condition_id(session, " nm ")
    session.commit()
    assert default_condition_id(session) == "NM"


@pytest.mark.parametrize("bad", ["", "ZZZ", "Near Mint", "lp!", None])
def test_an_invalid_value_is_refused_and_nothing_is_written(session, bad):
    with pytest.raises(IntakeDefaultError):
        set_default_condition_id(session, bad)
    session.rollback()
    assert session.query(AppSetting).count() == 0
    assert session.query(AppSettingChangeLog).count() == 0


def test_a_nonsense_stored_value_falls_back_rather_than_poisoning_intake(session):
    """A condition id no Mana Pool listing can be keyed on must never reach
    a card. Written directly, bypassing the validator, the way a bad
    manual edit or a future migration could."""
    session.add(AppSetting(key=DEFAULT_CONDITION_SETTING_KEY, value="GARBAGE"))
    session.commit()
    assert default_condition_id(session) == "LP"


# --- the audit ------------------------------------------------------------

def test_a_change_is_audited_with_before_and_after(session):
    set_default_condition_id(session, "NM")
    session.commit()
    rows = session.query(AppSettingChangeLog).all()
    assert len(rows) == 1
    entry = json.loads(rows[0].change_summary)
    assert entry["action_type"] == "app_setting_changed"
    assert entry["before"] == "LP"
    assert entry["after"] == "NM"
    assert entry["setting"] == DEFAULT_CONDITION_SETTING_KEY
    assert rows[0].actor


def test_setting_it_to_what_it_already_is_writes_no_audit_row(session):
    """AppSetting keeps only the current value, so history must mean a real
    change, not every save click."""
    set_default_condition_id(session, "LP")
    session.commit()
    assert session.query(AppSettingChangeLog).count() == 0


def test_history_is_newest_first(session):
    for value in ("NM", "MP", "LP"):
        set_default_condition_id(session, value)
    session.commit()
    history = setting_history(session, DEFAULT_CONDITION_SETTING_KEY)
    assert [row["entry"]["after"] for row in history] == ["LP", "MP", "NM"]


# --- label derivation -----------------------------------------------------

def test_the_dropdown_label_is_derived_not_hardcoded(session):
    """Two label vocabularies exist in this app -- the add-card dropdown
    says "Light Play" while CONDITION_LABELS says "Lightly Played" -- so
    the label is derived by round-tripping the dropdown's own list."""
    labels = main._ADD_CARD_CONDITIONS
    assert condition_label_for("LP", labels) == "Light Play"
    assert condition_label_for("NM", labels) == "Near Mint"
    assert condition_label_for("ZZZ", labels) == ""
    set_default_condition_id(session, "NM")
    session.commit()
    assert default_condition_label(session, labels) == "Near Mint"


# --- the screen -----------------------------------------------------------

def test_the_admin_page_links_to_it_and_shows_the_current_value(client):
    http, _engine = client
    admin = http.get("/admin").text
    assert "/admin/intake-defaults" in admin
    assert "Intake Defaults" in admin


def test_saving_changes_the_default_and_records_history(client):
    http, engine = client
    assert 'value="LP" selected' in http.get("/admin/intake-defaults").text

    assert http.post("/admin/intake-defaults", data={"condition_id": "NM"},
                     follow_redirects=False).status_code == 303
    page = http.get("/admin/intake-defaults").text
    assert 'value="NM" selected' in page

    with Session(engine) as s:
        assert default_condition_id(s) == "NM"
        assert s.query(AppSettingChangeLog).count() == 1


def test_the_screen_refuses_an_invalid_condition(client):
    http, engine = client
    assert http.post("/admin/intake-defaults",
                     data={"condition_id": "ZZZ"}).status_code == 400
    with Session(engine) as s:
        assert default_condition_id(s) == "LP"


# --- every intake default reads the setting -------------------------------

def test_the_add_card_form_preselects_the_configured_default(client):
    """★ THE ONE THAT WAS WRONG. This form pre-selected "Near Mint"
    regardless of the shop's policy."""
    http, engine = client
    with Session(engine) as s:
        set_default_condition_id(s, "LP")
        s.commit()
    assert main._intake_default_condition_label() == "Light Play"

    with Session(engine) as s:
        set_default_condition_id(s, "NM")
        s.commit()
    assert main._intake_default_condition_label() == "Near Mint"


def test_the_card_edit_placeholder_follows_the_setting(client):
    http, engine = client
    with Session(engine) as s:
        set_default_condition_id(s, "MP")
        s.commit()
    assert main._intake_default_condition_code() == "MP"


def test_the_csv_importer_uses_the_setting_when_no_condition_is_given(client):
    http, engine = client
    import production_import_service as pis

    with Session(engine) as s:
        set_default_condition_id(s, "HP")
        s.commit()
    parsed = pis.parse_production_csv(_csv())
    assert parsed["physical_rows"], parsed
    assert parsed["physical_rows"][0]["condition_id"] == "HP"


def test_an_explicit_condition_in_the_csv_is_never_overridden(client):
    """★ THE REQUIREMENT. The setting fills a gap; it never overrules."""
    http, engine = client
    import production_import_service as pis

    with Session(engine) as s:
        set_default_condition_id(s, "HP")
        s.commit()
    parsed = pis.parse_production_csv(_csv("Near Mint"))
    assert parsed["physical_rows"][0]["condition_id"] == "NM"


def test_an_explicitly_passed_default_still_wins_over_the_setting(client):
    """A caller that knows better -- a one-off migration script -- can
    still say so."""
    http, engine = client
    import production_import_service as pis

    with Session(engine) as s:
        set_default_condition_id(s, "HP")
        s.commit()
    parsed = pis.parse_production_csv(_csv(), default_condition="DMG")
    assert parsed["physical_rows"][0]["condition_id"] == "DMG"


def test_the_consignment_sheet_importer_reads_the_setting(client):
    http, engine = client
    import import_consignment_sheets as ics

    with Session(engine) as s:
        set_default_condition_id(s, "MP")
        s.commit()
    assert ics._default_condition_id() == "MP"
    with Session(engine) as s:
        assert ics._default_condition_id(s) == "MP"


def test_no_intake_default_is_still_hardcoded(client):
    """Pinned by reading the source: a future edit must not reintroduce a
    literal condition default at any of the wired call sites."""
    source = open("main.py").read()
    assert 'or "Near Mint")' not in source
    assert 'or "Light Play")' not in source
    assert 'placeholder="NM"' not in source
    assert open("production_import_service.py").read().count('default_condition="LP"') == 0
