"""An import refuses a condition this app cannot read back.

★ THE DEFECT (operator decision, 2026-11-01). `normalized_condition_id`
falls through to the cleaned string for a word it does not recognise, so
a CSV saying "Lightly Played" produced `condition_id = "LIGHTLY_PLAYED"`
-- truthy, so the parser's own missing-identity check waved it through --
and the card was created keyed on a condition no Mana Pool catalog
variant can match. The edit form has refused that since v2.81.1; the
import paths stored it as junk.

★ THIS REPLACES v2.76.0's "an unrecognised grade clamps to LP and logs"
for batch intake. The rest of that decision stands and is asserted here:
a RECOGNISED grade is still taken in at LP or worse, and a hand-picked
dropdown value is still kept.

★ THE WHOLE FILE IS REFUSED, not the offending row. Half an import is
the thing this flow cannot undo cleanly, so
build_production_import_preview raises on any row error and confirm
rebuilds the preview from the stored CSV -- one shared refusal reaching
every importer that parses a CSV, rather than one per importer.

★ IT REJECTS NOTHING THAT WAS EVER IMPORTED, measured read-only the same
day. All 12 stored import CSVs (558 rows between them) hold only
recognised spellings -- 'Light Play', 'NM', 'near_mint', 'LP', 'MP',
'Near Mint' -- and of 16,155 inventory cards, zero hold a condition word
that would be refused. Prevention, not a cull.
"""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import main
from intake_defaults_service import (
    ACCEPTED_CONDITION_SPELLINGS, VALID_CONDITION_IDS,
    condition_refusal_reason,
)
from models import InventoryCard
from tests.test_batch_intake_condition_ceiling import batch_csv
from tests.test_inventory_add import make_batch, mock_scryfall, setup_db


def upload(client, batch_id, condition):
    return client.post(
        "/imports/production-preview",
        data={"mode": "existing", "source_location": "Shelf A",
              "target_batch_id": str(batch_id)},
        files={"file": ("cards.csv", batch_csv(condition), "text/csv")},
    )


def multi_row_csv(*conditions) -> bytes:
    header = ("Name,Set code,Collector number,Finish,Scryfall ID,Condition,"
              "Language,Quantity,Price (USD)\n")
    rows = "".join(
        f"Lightning Bolt,lea,161,normal,sf-bolt,{condition},EN,1,8.00\n"
        for condition in conditions
    )
    return (header + rows).encode()


def upload_rows(client, batch_id, *conditions):
    return client.post(
        "/imports/production-preview",
        data={"mode": "existing", "source_location": "Shelf A",
              "target_batch_id": str(batch_id)},
        files={"file": ("cards.csv", multi_row_csv(*conditions), "text/csv")},
    )


def cards(db, batch_id):
    with Session(db) as session:
        return session.query(InventoryCard).filter_by(batch_id=batch_id).all()


# --- the preview refuses, and names the line and the value ---------------

@pytest.mark.parametrize("condition", [
    "Lightly Played", "Moderately Played", "Heavily Played",
    "banana", "Mint-ish", "NM/M",
])
def test_an_unreadable_condition_stops_the_preview(tmp_path, monkeypatch,
                                                   condition):
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A1")

    response = upload(TestClient(main.app), batch.id, condition)

    assert condition in response.text, "it must name the value typed"
    assert "is not one this shop recognises" in response.text
    assert cards(db, batch.id) == [], "a refused file imported a card"


def test_the_refusal_names_the_csv_line(tmp_path, monkeypatch):
    """A 600-row file needs a line number, not just a verdict."""
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A1")

    # Row 2 is the first data line, so the bad one here is row 4.
    response = upload_rows(TestClient(main.app), batch.id,
                           "Near Mint", "Light Play", "Lightly Played")

    assert "Row 4" in response.text, response.text[:600]


def test_every_bad_line_is_reported_not_just_the_first(tmp_path, monkeypatch):
    """One pass through the file, so the operator fixes it once."""
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A1")

    response = upload_rows(TestClient(main.app), batch.id,
                           "Lightly Played", "Light Play", "banana")

    assert "Row 2" in response.text and "Row 4" in response.text
    assert "Lightly Played" in response.text and "banana" in response.text


def test_the_refusal_lists_what_to_use_instead(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A1")

    text = upload(TestClient(main.app), batch.id, "Lightly Played").text

    for spelling in ACCEPTED_CONDITION_SPELLINGS:
        assert spelling in text, spelling
    for code in VALID_CONDITION_IDS:
        assert code in text, code


def test_one_bad_row_refuses_the_whole_file(tmp_path, monkeypatch):
    """★ NOTHING HALF-IMPORTED. Two good rows and one bad one import
    nothing at all, and no confirm button is offered."""
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A1")

    response = upload_rows(TestClient(main.app), batch.id,
                           "Near Mint", "Lightly Played", "Light Play")

    assert cards(db, batch.id) == []
    assert not re.search(r'action="(/imports/\d+/confirm)"', response.text), (
        "a refused file must not offer a confirm button"
    )


def test_there_is_nothing_to_confirm_after_a_refusal(tmp_path, monkeypatch):
    """Confirm rebuilds the preview from the stored CSV, so even a
    hand-made POST cannot get past it."""
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    good = upload(client, batch.id, "Light Play")
    action = re.search(r'action="(/imports/\d+/confirm)"', good.text)
    assert action, "the good upload must offer a confirm"
    pending_path = action.group(1)

    # A second upload that IS refused leaves nothing new to confirm, and
    # the first one still confirms on its own terms.
    upload(client, batch.id, "Lightly Played")
    confirmed = client.post(pending_path, follow_redirects=False)

    assert confirmed.status_code in (200, 303)
    imported = cards(db, batch.id)
    assert len(imported) == 1, "the refused file must not have added a card"
    assert imported[0].condition_id == "LP"


# --- everything production actually holds still imports -----------------

@pytest.mark.parametrize("condition,expected_id", [
    ("Light Play", "LP"),      # 294 of the 558 stored CSV rows
    ("NM", "LP"),              # 77 -- recognised, then clamped by the ceiling
    ("near_mint", "LP"),       # 74
    ("LP", "LP"),              # 19
    ("Near Mint", "LP"),       # 3
    ("mint", "LP"),            # a legacy synonym
    ("light_played", "LP"),
    ("excellent", "LP"),
])
def test_every_spelling_the_stored_csvs_hold_still_imports(
        tmp_path, monkeypatch, condition, expected_id):
    """LP or better lands on LP -- the v2.76.0 ceiling, unchanged."""
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    preview = upload(client, batch.id, condition)
    action = re.search(r'action="(/imports/\d+/confirm)"', preview.text)
    assert action, preview.text[:600]
    client.post(action.group(1), follow_redirects=False)

    imported = cards(db, batch.id)
    assert len(imported) == 1
    assert imported[0].condition_id == expected_id


@pytest.mark.parametrize("condition,expected_id", [
    ("MP", "MP"), ("Moderate Play", "MP"),
    ("HP", "HP"), ("Heavy Play", "HP"),
    ("DMG", "DMG"), ("Damaged", "DMG"),
    ("good", "MP"), ("played", "HP"), ("poor", "DMG"),
])
def test_a_grade_worse_than_LP_is_still_left_exactly_as_the_file_had_it(
        tmp_path, monkeypatch, condition, expected_id):
    """The other half of v2.76.0, which this release does not touch."""
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    preview = upload(client, batch.id, condition)
    action = re.search(r'action="(/imports/\d+/confirm)"', preview.text)
    assert action, preview.text[:600]
    client.post(action.group(1), follow_redirects=False)

    assert cards(db, batch.id)[0].condition_id == expected_id


def test_a_file_with_no_condition_column_still_uses_the_shop_default(
        tmp_path, monkeypatch):
    """A blank is not a refusal: every caller has its own answer for an
    absent value, and refusing one would turn a missing column into a
    failed import."""
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    csv_bytes = (
        "Name,Set code,Collector number,Finish,Scryfall ID,Language,"
        "Quantity,Price (USD)\n"
        "Lightning Bolt,lea,161,normal,sf-bolt,EN,1,8.00\n"
    ).encode()

    preview = client.post(
        "/imports/production-preview",
        data={"mode": "existing", "source_location": "Shelf A",
              "target_batch_id": str(batch.id)},
        files={"file": ("cards.csv", csv_bytes, "text/csv")},
    )
    action = re.search(r'action="(/imports/\d+/confirm)"', preview.text)
    assert action, preview.text[:600]
    client.post(action.group(1), follow_redirects=False)

    assert cards(db, batch.id)[0].condition_id == "LP"


# --- the rule is one rule -----------------------------------------------

def test_the_import_and_the_form_refuse_the_same_words():
    """★ ONE SENTENCE, ONE ACCEPTANCE TEST. Two refusals written twice
    are two refusals that drift, so identity_change_service's form
    refusal and the import refusal both come from
    intake_defaults_service."""
    from identity_change_service import condition_refusal

    for word in ("Lightly Played", "banana", "Mint-ish", "NM/M"):
        assert condition_refusal_reason(word), word
        assert condition_refusal(word), word
    for word in ("Light Play", "NM", "mint", "light_played", "", None):
        assert condition_refusal_reason(word) is None, word
        assert condition_refusal(word) is None, word


def test_the_form_adds_the_one_clause_only_it_can_say():
    from identity_change_service import condition_refusal

    shared = condition_refusal_reason("banana")
    form = condition_refusal("banana")
    assert form.startswith(shared)
    assert form.endswith("Nothing was changed.")


# --- the legacy migration, which writes cards without the CSV parser ----

def legacy_csv(condition: str) -> str:
    return (
        "Name,Set code,Collector number,Foil,Condition,Quantity,"
        "Scryfall ID,Purchase price\n"
        f"Lightning Bolt,lea,161,normal,{condition},1,sf-bolt,1.00\n"
    )


def legacy_plan(monkeypatch, condition: str):
    """build_legacy_plan with Scryfall stubbed, no network."""
    import legacy_import_service as legacy

    monkeypatch.setattr(
        legacy, "fetch_scryfall_cards",
        lambda ids: ({i: {"id": i, "name": "Lightning Bolt", "set": "lea",
                          "collector_number": "161", "rarity": "common",
                          "type_line": "Instant", "colors": ["R"],
                          "prices": {}} for i in ids}, []),
    )
    monkeypatch.setattr(legacy, "_existing_copy_counts", lambda session: {})
    return legacy, legacy.build_legacy_plan(None, legacy_csv(condition))


def test_the_legacy_migration_flags_an_unreadable_condition(monkeypatch):
    """★ THIS PATH DOES NOT GO THROUGH THE CSV PARSER. It writes
    InventoryCard.condition_id straight from the file's own word, so the
    refusal is applied here too -- flagged by LINE NUMBER in the plan, the
    same shape the file already uses for unresolved rows."""
    _, plan = legacy_plan(monkeypatch, "Lightly Played")

    rows = plan["unreadable_condition_rows"]
    assert len(rows) == 1
    assert rows[0]["row_number"] == 2
    assert rows[0]["condition"] == "Lightly Played"
    assert "is not one this shop recognises" in rows[0]["reason"]
    assert plan["unreadable_condition_total"] == 1


def test_the_legacy_migration_refuses_to_import_while_any_remain(monkeypatch):
    legacy, plan = legacy_plan(monkeypatch, "Lightly Played")

    with pytest.raises(ValueError) as caught:
        legacy.import_legacy_plan(None, plan, "legacy.csv", "hash")

    assert "does not recognise" in str(caught.value)
    assert "Row 2" in str(caught.value)


@pytest.mark.parametrize("condition", ["Light Play", "NM", "mint", "Damaged"])
def test_a_legacy_file_the_app_can_read_is_not_flagged(monkeypatch, condition):
    _, plan = legacy_plan(monkeypatch, condition)

    assert plan["unreadable_condition_rows"] == []
    assert plan["unreadable_condition_total"] == 0


def test_a_legacy_plan_written_before_this_check_still_imports(monkeypatch):
    """Absent is read as "none" -- the same answer a clean file gives.
    The preview is rebuilt from the CSV on every load, so a stored plan
    old enough to lack the key cannot carry a bad row."""
    legacy, plan = legacy_plan(monkeypatch, "Light Play")
    del plan["unreadable_condition_rows"]
    del plan["unreadable_condition_total"]

    # It gets past the guard; the rest of the import is not this test's
    # business, so only the guard is asserted.
    try:
        legacy.import_legacy_plan(None, plan, "legacy.csv", "hash")
    except ValueError as exc:
        assert "does not recognise" not in str(exc)
    except Exception:
        pass
