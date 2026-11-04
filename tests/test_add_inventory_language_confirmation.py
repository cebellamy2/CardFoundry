"""Add Inventory: confirming a non-English card on an English printing.

The operator's own case (2026-11-04 08:17 ET): FIC 207, Japanese, Lightly
Played, non-foil. Scryfall publishes that printing only in English, so the
explicit-language guard refused the add -- correctly, and with no way
forward on that screen. The buylist pile has had a per-line "Confirm
<language> on this English printing" button since Slice 4-3; a single add
had nothing, because there is no pile line to hang a confirmation on.

HOUSE RULE (operator, 2026-11-04 08:17 ET): a non-English card is stored
on the ENGLISH printing's scryfall_id with language_id = its real
language, which is how Mana Pool's catalog keys every language of one
printing. These tests assert that outcome -- the row that lands in
inventory -- not that a function was called.

THE GUARD IS NOT RELAXED. Every mismatch is still refused unless an
operator has clicked the button for that exact printing and language, in
that one request.
"""
import logging
import re
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import main
from models import InventoryCard
from production_import_service import language_override_fingerprint
from tests.test_inventory_add import make_batch, mock_scryfall, setup_db

FIC_207 = "sf-fic-207"


def english_fic_printing(ids):
    """What Scryfall really answers for FIC 207: an English card. There is
    no Japanese Scryfall object to resolve to, which is the whole reason
    the guard fires."""
    return {
        scryfall_id: {
            "id": scryfall_id, "name": "Sword of Fire and Ice", "set": "fic",
            "collector_number": "207", "lang": "en", "finishes": ["nonfoil"],
        }
        for scryfall_id in ids
    }


def fic_catalog_lookup(ids, languages=None):
    """Mana Pool's catalog, which carries BOTH languages under the one
    English printing -- the trap CLAUDE.md records, and the reason the
    house rule stores the English scryfall_id."""
    return {"meta": {"as_of": "catalog-v1"}, "data": [
        {
            "name": "Sword of Fire and Ice", "set_code": "FIC", "number": "207",
            "scryfall_id": scryfall_id,
            "variants": [
                {
                    "product_type": "mtg_single",
                    "product_id": f"product-{scryfall_id}-{language}-{condition}",
                    "language_id": language, "condition_id": condition,
                    "finish_id": "NF",
                }
                for language in ("EN", "JA")
                for condition in ("NM", "LP")
            ],
        }
        for scryfall_id in ids
    ]}


def japanese_form(**overrides):
    """Exactly what the operator submitted."""
    form = {
        "scryfall_id": FIC_207, "name": "Sword of Fire and Ice",
        "set_code": "fic", "collector_number": "207",
        "variant_finish": "nonfoil", "condition": "Light Play",
        "bought_price": "20.00", "asking_price": "40.00",
        "language": "JA", "mode": "existing",
    }
    form.update(overrides)
    return form


def _setup(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(
        monkeypatch, printing=None, cards_lookup=english_fic_printing,
        catalog_lookup=fic_catalog_lookup,
    )
    batch = make_batch(db, "JA1")
    return db, batch, TestClient(main.app)


def _confirm_form_values(html: str) -> dict:
    """Read the confirm button's form back off the page, exactly as a
    browser would re-post it -- never hand-built, so the test cannot pass
    with a form the operator could not actually submit."""
    block = html.split('name="confirm_language"')[0]
    form_html = block[block.rindex('<form method="post" action="/inventory/add/preview"'):]
    values = dict(re.findall(
        r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form_html,
    ))
    fingerprint = re.search(
        r'name="confirm_language" value="([^"]*)"', html,
    ).group(1)
    values["confirm_language"] = fingerprint
    return values


def _commit(client, preview_html) -> None:
    action = re.search(r'action="(/imports/\d+/confirm)"', preview_html)
    assert action, preview_html[:2000]
    response = client.post(action.group(1), follow_redirects=False)
    assert response.status_code == 303, response.text


# ---------------------------------------------------------------------
# ★ THE OPERATOR'S OWN CLICK, END TO END
# ---------------------------------------------------------------------

def test_the_japanese_card_is_refused_then_confirmed_into_inventory(tmp_path, monkeypatch):
    db, batch, client = _setup(tmp_path, monkeypatch)

    refused = client.post(
        "/inventory/add/preview",
        data=japanese_form(target_batch_id=str(batch.id)),
    )
    assert refused.status_code == 400
    assert "conflicts with Scryfall language EN" in refused.text
    assert "Confirm Japanese on this English printing" in refused.text
    # Nothing was added by being refused.
    with Session(db) as session:
        assert session.query(InventoryCard).count() == 0

    preview = client.post(
        "/inventory/add/preview", data=_confirm_form_values(refused.text),
    )
    assert preview.status_code == 200, preview.text[:2000]
    _commit(client, preview.text)

    with Session(db) as session:
        card = session.query(InventoryCard).one()
        # ★ THE HOUSE RULE, as a row: the English printing's scryfall_id,
        # the card's real language.
        assert card.scryfall_id == FIC_207
        assert card.language_id == "JA"
        assert card.condition_id == "LP"
        assert card.finish_id == "NF"
        assert card.batch_id == batch.id


def test_the_refusal_screen_tells_a_single_add_what_to_do(tmp_path, monkeypatch):
    """It used to tell every refused add to 'fix these lines in your CSV
    and re-upload the file'. A single add has no CSV."""
    _db, batch, client = _setup(tmp_path, monkeypatch)
    refused = client.post(
        "/inventory/add/preview",
        data=japanese_form(target_batch_id=str(batch.id)),
    )
    assert "re-upload the file" not in refused.text
    assert "Nothing was added" in refused.text
    # And no "CSV line(s)" column over a synthetic row number.
    assert "CSV line(s)" not in refused.text


# ---------------------------------------------------------------------
# ★ THE GUARD IS NOT RELAXED
# ---------------------------------------------------------------------

def test_a_mismatch_with_no_confirmation_still_refuses(tmp_path, monkeypatch):
    db, batch, client = _setup(tmp_path, monkeypatch)
    response = client.post(
        "/inventory/add/preview",
        data=japanese_form(target_batch_id=str(batch.id)),
    )
    assert response.status_code == 400
    assert "conflicts with Scryfall language EN" in response.text
    with Session(db) as session:
        assert session.query(InventoryCard).count() == 0


@pytest.mark.parametrize("field,value", [
    ("language", "DE"),
    ("scryfall_id", "sf-some-other-printing"),
])
def test_an_identity_change_voids_the_confirmation(tmp_path, monkeypatch, field, value):
    """The same invalidation rule the pile line's stored fingerprint uses,
    applied to a re-post: change the printing or the language and the
    fingerprint no longer describes what is being submitted, so it
    confirms nothing."""
    db, batch, client = _setup(tmp_path, monkeypatch)
    refused = client.post(
        "/inventory/add/preview",
        data=japanese_form(target_batch_id=str(batch.id)),
    )
    tampered = _confirm_form_values(refused.text)
    tampered[field] = value

    response = client.post("/inventory/add/preview", data=tampered)
    assert response.status_code == 400
    with Session(db) as session:
        assert session.query(InventoryCard).count() == 0


def test_a_confirmation_invented_for_another_card_does_nothing(tmp_path, monkeypatch):
    db, batch, client = _setup(tmp_path, monkeypatch)
    response = client.post(
        "/inventory/add/preview",
        data=japanese_form(
            target_batch_id=str(batch.id),
            confirm_language=language_override_fingerprint("sf-something-else", "JA"),
        ),
    )
    assert response.status_code == 400
    assert "conflicts with Scryfall language EN" in response.text
    with Session(db) as session:
        assert session.query(InventoryCard).count() == 0


@contextmanager
def _capturing(caplog, level):
    """★ caplog ALONE CANNOT SEE THIS LOGGER. main.py sets
    cardfoundry.propagate = False, so the records never reach the root
    handler caplog installs and an assertion on caplog.text would pass
    vacuously whatever the code did. Attach caplog's own handler, the same
    way tests/test_non_english_mirror_fold.py does."""
    cardfoundry = logging.getLogger("cardfoundry")
    caplog.set_level(level, logger="cardfoundry")
    cardfoundry.addHandler(caplog.handler)
    try:
        yield
    finally:
        cardfoundry.removeHandler(caplog.handler)


def test_an_ignored_confirmation_is_logged(tmp_path, monkeypatch, caplog):
    """Catch-and-continue logs. A confirmation that silently stopped
    counting is exactly what has to be visible afterwards."""
    _db, batch, client = _setup(tmp_path, monkeypatch)
    with _capturing(caplog, logging.WARNING):
        client.post(
            "/inventory/add/preview",
            data=japanese_form(
                target_batch_id=str(batch.id),
                confirm_language=language_override_fingerprint("sf-else", "JA"),
            ),
        )
    assert "no longer matches this submission" in caplog.text


def test_an_honoured_confirmation_is_logged(tmp_path, monkeypatch, caplog):
    """The other half: a confirmation that DID count says so by name, so
    an operator-vouched identity is readable after the fact."""
    _db, batch, client = _setup(tmp_path, monkeypatch)
    refused = client.post(
        "/inventory/add/preview",
        data=japanese_form(target_batch_id=str(batch.id)),
    )
    with _capturing(caplog, logging.INFO):
        client.post(
            "/inventory/add/preview", data=_confirm_form_values(refused.text),
        )
    assert "operator confirmed language JA on printing" in caplog.text


# ---------------------------------------------------------------------
# ★ THE BUTTON APPEARS ONLY WHERE IT IS THE WHOLE ANSWER
# ---------------------------------------------------------------------

def test_no_button_when_the_language_is_not_the_problem(tmp_path, monkeypatch):
    """A printing Scryfall does not know is held for a different reason.
    Confirming a language would not make that row addable, so offering the
    button would only teach the operator to click it."""
    _db, batch, client = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "fetch_scryfall_cards", lambda ids: {})
    response = client.post(
        "/inventory/add/preview",
        data=japanese_form(target_batch_id=str(batch.id)),
    )
    assert response.status_code == 400
    assert "Confirm Japanese" not in response.text


def test_no_button_on_a_clean_english_add(tmp_path, monkeypatch):
    _db, batch, client = _setup(tmp_path, monkeypatch)
    response = client.post(
        "/inventory/add/preview",
        data=japanese_form(target_batch_id=str(batch.id), language="EN"),
    )
    assert response.status_code == 200
    assert "on this English printing" not in response.text


def test_there_is_no_confirm_all_and_nothing_is_remembered(tmp_path, monkeypatch):
    """Per item, like the pile's. The next add starts from refused again."""
    db, batch, client = _setup(tmp_path, monkeypatch)
    first_refusal = client.post(
        "/inventory/add/preview",
        data=japanese_form(target_batch_id=str(batch.id)),
    )
    assert "confirm all" not in first_refusal.text.lower()
    preview = client.post(
        "/inventory/add/preview", data=_confirm_form_values(first_refusal.text),
    )
    _commit(client, preview.text)

    second = client.post(
        "/inventory/add/preview",
        data=japanese_form(target_batch_id=str(batch.id)),
    )
    assert second.status_code == 400
    assert "conflicts with Scryfall language EN" in second.text
    with Session(db) as session:
        assert session.query(InventoryCard).count() == 1


def test_no_new_javascript(tmp_path, monkeypatch):
    _db, batch, client = _setup(tmp_path, monkeypatch)
    refused = client.post(
        "/inventory/add/preview",
        data=japanese_form(target_batch_id=str(batch.id)),
    )
    offer = refused.text[refused.text.index("Is this card really"):]
    assert "<script" not in offer
    assert "onclick" not in offer
