import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from models import Base, Batch, Consignor, InventoryCard


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'inventory-add.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(database, "engine", db)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    # confirm_import writes a real audit JSON file via Path("audits") --
    # redirect it into tmp_path so tests never pollute the real repo's
    # audits/ directory, same pattern test_production_import_service.py
    # already uses.
    monkeypatch.setattr(main, "Path", lambda value: tmp_path / value)
    return db


BOLT_PRINTING = {
    "id": "sf-bolt", "name": "Lightning Bolt", "set": "lea",
    "collector_number": "161", "finishes": ["nonfoil", "foil"],
}


def bolt_scryfall_cards(ids):
    return {
        scryfall_id: {
            "id": scryfall_id, "name": "Lightning Bolt", "set": "lea",
            "collector_number": "161", "lang": "en",
        }
        for scryfall_id in ids
    }


def bolt_catalog_lookup(ids, languages=None):
    return {"meta": {"as_of": "catalog-v1"}, "data": [
        {
            "name": "Lightning Bolt", "set_code": "LEA", "number": "161",
            "scryfall_id": scryfall_id,
            "variants": [
                {
                    "product_type": "mtg_single", "product_id": f"product-{scryfall_id}-nf",
                    "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
                },
                {
                    "product_type": "mtg_single", "product_id": f"product-{scryfall_id}-fo",
                    "language_id": "EN", "condition_id": "LP", "finish_id": "FO",
                },
            ],
        }
        for scryfall_id in ids
    ]}


def mock_scryfall(monkeypatch, printing=BOLT_PRINTING, cards_lookup=bolt_scryfall_cards,
                   catalog_lookup=bolt_catalog_lookup):
    monkeypatch.setattr(main, "fetch_scryfall_printing", lambda set_code, number: printing)
    monkeypatch.setattr(main, "fetch_scryfall_cards", cards_lookup)
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", catalog_lookup)


def make_batch(db, code, *, is_archived=False, is_consignment=False, consignor_id=None):
    with Session(db) as session:
        batch = Batch(
            batch_code=code, is_archived=is_archived,
            is_consignment=is_consignment, consignor_id=consignor_id,
        )
        session.add(batch)
        session.commit()
        session.refresh(batch)
        return batch


def valid_preview_form(**overrides):
    form = {
        "scryfall_id": "sf-bolt", "name": "Lightning Bolt", "set_code": "lea",
        "collector_number": "161", "variant_finish": "nonfoil",
        "condition": "Near Mint", "bought_price": "5.00", "asking_price": "8.00",
        "language": "EN", "mode": "existing",
    }
    form.update(overrides)
    return form


# --- GET /inventory/add ---

def test_add_inventory_page_renders_single_card_search_form(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add")
    assert response.status_code == 200
    assert "Add a Single Card" in response.text
    assert 'action="/inventory/add/search"' in response.text
    assert 'name="set_code"' in response.text
    assert 'name="collector_number"' in response.text
    assert "Add Inventory" in response.text


# --- POST /inventory/add/search ---

def test_search_unknown_printing_shows_inline_error_and_keeps_typed_values(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch, printing=None)
    client = TestClient(main.app)
    response = client.post(
        "/inventory/add/search", data={"set_code": "zzz", "collector_number": "999"},
    )
    assert response.status_code == 200
    assert "No printing found" in response.text
    assert 'value="zzz"' in response.text
    assert 'value="999"' in response.text


def test_search_unreachable_scryfall_shows_readable_error(tmp_path, monkeypatch):
    import httpx

    setup_db(tmp_path, monkeypatch)

    def raise_unreachable(set_code, number):
        raise httpx.ConnectError("connection failed")

    monkeypatch.setattr(main, "fetch_scryfall_printing", raise_unreachable)
    client = TestClient(main.app)
    response = client.post(
        "/inventory/add/search", data={"set_code": "lea", "collector_number": "161"},
    )
    assert response.status_code == 502
    assert "unreachable" in response.text.lower()


def test_search_found_printing_shows_one_row_per_finish(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/inventory/add/search", data={"set_code": "lea", "collector_number": "161"},
    )
    assert response.status_code == 200
    assert 'value="nonfoil"' in response.text
    assert 'value="foil"' in response.text
    assert 'action="/inventory/add/preview"' in response.text
    assert "Lightning Bolt" in response.text


def test_search_single_finish_printing_shows_just_that_one_row(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch, printing={**BOLT_PRINTING, "finishes": ["nonfoil"]})
    client = TestClient(main.app)
    response = client.post(
        "/inventory/add/search", data={"set_code": "lea", "collector_number": "161"},
    )
    assert response.status_code == 200
    assert 'value="nonfoil"' in response.text
    assert 'value="foil"' not in response.text


def test_search_batch_dropdown_labels_consignment_batches(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    with Session(db) as session:
        consignor = Consignor(name="Jane")
        session.add(consignor)
        session.flush()
        session.add(Batch(batch_code="CONS1", is_consignment=True, consignor_id=consignor.id))
        session.commit()
    client = TestClient(main.app)
    response = client.post(
        "/inventory/add/search", data={"set_code": "lea", "collector_number": "161"},
    )
    assert response.status_code == 200
    assert "CONS1 (Consignment: Jane)" in response.text


# --- POST /inventory/add/preview ---

def test_preview_zero_variants_selected_is_an_inline_error(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    client = TestClient(main.app)
    form = valid_preview_form()
    del form["variant_finish"]
    response = client.post("/inventory/add/preview", data=form)
    assert response.status_code == 400
    assert "exactly one" in response.text.lower()


def test_preview_multiple_variants_selected_is_an_inline_error(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    client = TestClient(main.app)
    form = valid_preview_form()
    form["variant_finish"] = ["nonfoil", "foil"]
    response = client.post("/inventory/add/preview", data=form)
    assert response.status_code == 400
    assert "exactly one" in response.text.lower()


def test_preview_to_existing_batch_creates_pending_import(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A5")
    client = TestClient(main.app)
    response = client.post(
        "/inventory/add/preview",
        data=valid_preview_form(target_batch_id=str(batch.id)),
    )
    assert response.status_code == 200
    assert "Production Import Preview" in response.text
    assert 'action="/imports/' in response.text
    with Session(db) as session:
        from models import PendingImport
        pending = session.query(PendingImport).one()
        assert pending.batch_id == batch.id


def test_preview_to_nonempty_existing_batch_is_allowed(tmp_path, monkeypatch):
    """The whole point of allow_nonempty_target -- single-card add must
    work against a batch that already has cards, unlike CSV import."""
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A5")
    with Session(db) as session:
        session.add(InventoryCard(
            batch_id=batch.id, name="Already Here", mtgjson_id="mtg-x",
            language_id="EN", condition_id="LP", finish_id="NF", status="available",
        ))
        session.commit()
    client = TestClient(main.app)
    response = client.post(
        "/inventory/add/preview",
        data=valid_preview_form(target_batch_id=str(batch.id)),
    )
    assert response.status_code == 200
    assert "Production Import Preview" in response.text


def test_preview_requires_a_batch_when_mode_is_existing(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/inventory/add/preview",
        data=valid_preview_form(mode="existing", target_batch_id=""),
    )
    assert response.status_code == 400
    assert "Choose a batch" in response.text


def test_preview_to_new_consignment_batch_requires_consignor(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/inventory/add/preview",
        data=valid_preview_form(
            mode="new", batch_code="NEWB", is_consignment="true",
        ),
    )
    assert response.status_code == 400
    assert "consignor is required" in response.text.lower()


# --- Full end-to-end: preview -> confirm ---

def test_full_add_to_existing_batch_creates_the_card(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A5")
    client = TestClient(main.app)

    preview_response = client.post(
        "/inventory/add/preview",
        data=valid_preview_form(target_batch_id=str(batch.id)),
    )
    assert preview_response.status_code == 200
    confirm_action = re.search(r'action="(/imports/\d+/confirm)"', preview_response.text)
    assert confirm_action

    confirm_response = client.post(confirm_action.group(1), follow_redirects=False)
    assert confirm_response.status_code == 303
    assert confirm_response.headers["location"] == f"/inventory/add?target_batch_id={batch.id}"

    with Session(db) as session:
        card = session.query(InventoryCard).filter_by(batch_id=batch.id).one()
        assert card.name == "Lightning Bolt"
        assert card.finish == "normal"
        assert card.condition_id == "LP"
        assert card.bought_in_price == 5.0
        assert card.current_price == 8.0
        assert card.price_usd == 8.0
        assert card.language_id == "EN"
        assert card.status == "available"


def test_full_add_creates_a_new_batch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    client = TestClient(main.app)

    preview_response = client.post(
        "/inventory/add/preview",
        data=valid_preview_form(mode="new", batch_code="FRESH1"),
    )
    assert preview_response.status_code == 200
    confirm_action = re.search(r'action="(/imports/\d+/confirm)"', preview_response.text)
    confirm_response = client.post(confirm_action.group(1), follow_redirects=False)
    assert confirm_response.status_code == 303

    with Session(db) as session:
        batch = session.query(Batch).filter_by(batch_code="FRESH1").one()
        assert session.query(InventoryCard).filter_by(batch_id=batch.id).count() == 1


def test_full_add_second_card_to_same_batch_after_redirect(tmp_path, monkeypatch):
    """The redirect-back-with-batch-kept flow: adding a second card into
    the same (now nonempty) batch must also work end to end."""
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A5")
    client = TestClient(main.app)

    for _ in range(2):
        preview_response = client.post(
            "/inventory/add/preview",
            data=valid_preview_form(target_batch_id=str(batch.id)),
        )
        assert preview_response.status_code == 200
        confirm_action = re.search(r'action="(/imports/\d+/confirm)"', preview_response.text)
        confirm_response = client.post(confirm_action.group(1), follow_redirects=False)
        assert confirm_response.status_code == 303
        assert confirm_response.headers["location"] == f"/inventory/add?target_batch_id={batch.id}"

    with Session(db) as session:
        assert session.query(InventoryCard).filter_by(batch_id=batch.id).count() == 2


def test_full_add_into_consignment_batch_sets_consignment_value(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    with Session(db) as session:
        consignor = Consignor(name="Jane")
        session.add(consignor)
        session.flush()
        consignor_id = consignor.id
    batch = make_batch(db, "CONS1", is_consignment=True, consignor_id=consignor_id)
    client = TestClient(main.app)

    preview_response = client.post(
        "/inventory/add/preview",
        data=valid_preview_form(target_batch_id=str(batch.id)),
    )
    confirm_action = re.search(r'action="(/imports/\d+/confirm)"', preview_response.text)
    client.post(confirm_action.group(1), follow_redirects=False)

    with Session(db) as session:
        card = session.query(InventoryCard).filter_by(batch_id=batch.id).one()
        assert card.consignment_value == 8.0


def test_full_add_new_consignment_batch_with_consignor(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    with Session(db) as session:
        consignor = Consignor(name="Bob")
        session.add(consignor)
        session.commit()
        session.refresh(consignor)
        consignor_id = consignor.id
    client = TestClient(main.app)

    preview_response = client.post(
        "/inventory/add/preview",
        data=valid_preview_form(
            mode="new", batch_code="NEWCONS", is_consignment="true",
            consignor_id=str(consignor_id),
        ),
    )
    assert preview_response.status_code == 200
    confirm_action = re.search(r'action="(/imports/\d+/confirm)"', preview_response.text)
    client.post(confirm_action.group(1), follow_redirects=False)

    with Session(db) as session:
        batch = session.query(Batch).filter_by(batch_code="NEWCONS").one()
        assert batch.is_consignment is True
        assert batch.consignor_id == consignor_id
        card = session.query(InventoryCard).filter_by(batch_id=batch.id).one()
        assert card.consignment_value == 8.0


def test_full_add_foil_variant_stores_foil_finish(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A5")
    client = TestClient(main.app)

    preview_response = client.post(
        "/inventory/add/preview",
        data=valid_preview_form(target_batch_id=str(batch.id), variant_finish="foil"),
    )
    confirm_action = re.search(r'action="(/imports/\d+/confirm)"', preview_response.text)
    client.post(confirm_action.group(1), follow_redirects=False)

    with Session(db) as session:
        card = session.query(InventoryCard).filter_by(batch_id=batch.id).one()
        assert card.finish == "foil"
        assert card.finish_id == "FO"


# --- Language: an untouched dropdown must not fight the card's own
# Scryfall-confirmed language (found live: "Row 2: explicit language EN
# conflicts with Scryfall language DW" on a single-print Dwarvish promo,
# because the dropdown always sent "EN" whether or not the operator ever
# touched it). ---------------------------------------------------------

DWARVEN_PRINTING = {
    "id": "sf-dwarven", "name": "Dwarven Warriors", "set": "hoc",
    "collector_number": "93", "finishes": ["nonfoil"],
}


def dwarven_scryfall_cards(ids):
    return {
        scryfall_id: {
            "id": scryfall_id, "name": "Dwarven Warriors", "set": "hoc",
            "collector_number": "93", "lang": "dw",
        }
        for scryfall_id in ids
    }


def dwarven_catalog_lookup(ids, languages=None):
    return {"meta": {"as_of": "catalog-v1"}, "data": [
        {
            "name": "Dwarven Warriors", "set_code": "HOC", "number": "93",
            "scryfall_id": scryfall_id,
            "variants": [{
                "product_type": "mtg_single", "product_id": f"product-{scryfall_id}-nf",
                "language_id": "DW", "condition_id": "LP", "finish_id": "NF",
            }],
        }
        for scryfall_id in ids
    ]}


def test_search_default_language_option_is_auto_detect_not_english(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/inventory/add/search", data={"set_code": "lea", "collector_number": "161"},
    )
    assert response.status_code == 200
    assert '<option value="" selected>Auto-detect from card</option>' in response.text
    assert '<option value="EN">English</option>' in response.text


def test_full_add_omitted_language_defers_to_scryfall_detected_language(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(
        monkeypatch, printing=DWARVEN_PRINTING,
        cards_lookup=dwarven_scryfall_cards, catalog_lookup=dwarven_catalog_lookup,
    )
    batch = make_batch(db, "A6")
    client = TestClient(main.app)

    form = valid_preview_form(
        scryfall_id="sf-dwarven", name="Dwarven Warriors", set_code="hoc",
        collector_number="93", target_batch_id=str(batch.id),
    )
    del form["language"]  # matches an untouched Auto-detect dropdown

    preview_response = client.post("/inventory/add/preview", data=form)
    assert preview_response.status_code == 200
    confirm_action = re.search(r'action="(/imports/\d+/confirm)"', preview_response.text)
    assert confirm_action, preview_response.text

    confirm_response = client.post(confirm_action.group(1), follow_redirects=False)
    assert confirm_response.status_code == 303

    with Session(db) as session:
        card = session.query(InventoryCard).filter_by(batch_id=batch.id).one()
        assert card.language_id == "DW"


def test_preview_explicit_language_conflicting_with_scryfall_still_fails_closed(tmp_path, monkeypatch):
    """The safety check itself must still catch a genuine mismatch -- this
    only removes the false positive an untouched dropdown default caused."""
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(
        monkeypatch, printing=DWARVEN_PRINTING,
        cards_lookup=dwarven_scryfall_cards, catalog_lookup=dwarven_catalog_lookup,
    )
    batch = make_batch(db, "A7")
    client = TestClient(main.app)
    form = valid_preview_form(
        scryfall_id="sf-dwarven", name="Dwarven Warriors", set_code="hoc",
        collector_number="93", language="EN", target_batch_id=str(batch.id),
    )
    response = client.post("/inventory/add/preview", data=form)
    assert response.status_code == 400
    assert "conflict" in response.text.lower()


def test_csv_import_route_unaffected_by_single_card_add_changes(tmp_path, monkeypatch):
    """A real CSV import into an already-nonempty batch must still be
    refused -- allow_nonempty_target must never leak into the CSV path."""
    import io
    from starlette.datastructures import UploadFile

    db = setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", bolt_catalog_lookup)
    monkeypatch.setattr(main, "fetch_scryfall_cards", bolt_scryfall_cards)
    batch = make_batch(db, "A5")
    with Session(db) as session:
        session.add(InventoryCard(
            batch_id=batch.id, name="Already Here", mtgjson_id="mtg-x",
            language_id="EN", condition_id="LP", finish_id="NF", status="available",
        ))
        session.commit()

    csv_text = (
        "Name,Set code,Collector number,Finish,Scryfall ID,Condition,Language,Quantity,Price (USD)\n"
        "Lightning Bolt,lea,161,normal,sf-bolt,Near Mint,EN,1,8.00\n"
    )
    client = TestClient(main.app)
    response = client.post(
        "/imports/production-preview",
        data={"mode": "existing", "source_location": "Shelf A", "target_batch_id": str(batch.id)},
        files={"file": ("cards.csv", csv_text.encode(), "text/csv")},
    )
    assert response.status_code == 400
    assert "already has cards" in response.text
