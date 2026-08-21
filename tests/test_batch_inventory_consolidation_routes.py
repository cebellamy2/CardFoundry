import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard


HEADERS = "Location,Name,Set code,Collector number,Finish,Scryfall ID,Scan Order,Price (USD),Quantity,Language,Condition\n"


def csv_bytes(rows):
    headers = HEADERS.rstrip("\n") + ",MTGJSON ID\n"
    canonical_rows = []
    for row in rows:
        scryfall_id = row.split(",")[5]
        canonical_rows.append(f"{row},mtg-{scryfall_id.split('-')[-1]}")
    return (headers + "\n".join(canonical_rows) + "\n").encode()


def catalog_lookup(ids, languages=None):
    return {"meta": {"as_of": "catalog-v1"}, "data": [
        {
            "name": "Alpha", "set_code": "ONE", "number": "1", "scryfall_id": scryfall_id,
            "variants": [{
                "product_type": "mtg_single", "product_id": f"product-{scryfall_id}",
                "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
            }],
        }
        for scryfall_id in ids
    ]}


def scryfall_lookup(ids):
    return {
        scryfall_id: {
            "id": scryfall_id, "name": "Alpha", "set": "one",
            "collector_number": "1", "lang": "en",
        }
        for scryfall_id in ids
    }


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'batch-consolidation.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", catalog_lookup)
    monkeypatch.setattr(main, "fetch_scryfall_cards", scryfall_lookup)
    return db


def make_batch(session, code, *, with_card=False):
    batch = Batch(batch_code=code, is_archived=False)
    session.add(batch)
    session.flush()
    if with_card:
        session.add(InventoryCard(
            batch_id=batch.id, name="Existing Card", mtgjson_id="mtg-existing",
            language_id="EN", condition_id="LP", finish_id="NF", status="available",
        ))
    session.commit()
    session.refresh(batch)
    return batch


def test_home_redirects_to_inventory(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/inventory"


def test_admin_page_links_to_batches(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/admin")
    assert response.status_code == 200
    assert 'href="/admin/batches"' in response.text


def test_admin_batches_page_shows_metrics_and_batch_list(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_batch(session, "A3")
    client = TestClient(main.app)
    response = client.get("/admin/batches")
    assert response.status_code == 200
    assert "Not For Sale" in response.text
    assert "Total Owned" in response.text
    assert "A3" in response.text
    assert 'href="/batches/import"' in response.text
    assert 'href="/batches/new"' in response.text


def test_import_csv_form_lists_only_empty_batches(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        empty = make_batch(session, "EMPTY1")
        make_batch(session, "FULL1", with_card=True)
    client = TestClient(main.app)
    response = client.get("/batches/import")
    assert response.status_code == 200
    assert "EMPTY1" in response.text
    assert "FULL1" not in response.text


def test_import_csv_form_preselects_target_batch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        empty = make_batch(session, "EMPTY1")
        empty_id = empty.id
    client = TestClient(main.app)
    response = client.get(f"/batches/import?target_batch_id={empty_id}")
    assert response.status_code == 200
    existing_radio = re.search(r'value="existing"[^>]*>', response.text, re.S)
    assert existing_radio and "checked" in existing_radio.group(0)
    new_radio = re.search(r'value="new"[^>]*>', response.text, re.S)
    assert new_radio and "checked" not in new_radio.group(0)
    assert f'<option value="{empty_id}" selected>EMPTY1</option>' in response.text


def test_import_csv_form_offers_consignment_option_for_new_batch(tmp_path, monkeypatch):
    from models import Consignor

    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(Consignor(name="Jane", is_active=True))
        session.commit()
    client = TestClient(main.app)
    response = client.get("/batches/import")
    assert response.status_code == 200
    assert 'name="is_consignment"' in response.text
    assert 'name="consignor_id"' in response.text
    assert "Jane" in response.text


def test_new_batch_form_renders(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/batches/new")
    assert response.status_code == 200
    assert 'action="/batches"' in response.text


def test_create_batch_redirects_to_its_own_detail_page(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/batches", data={"batch_code": "A9"}, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert re.match(r"^/batches/\d+$", location)


def _add_card(session, batch_id, **overrides):
    from models import InventoryCard

    values = {
        "batch_id": batch_id, "name": "Card", "mtgjson_id": f"mtg-{overrides.get('name', 'x')}",
        "language_id": "EN", "condition_id": "LP", "finish_id": "NF", "status": "available",
    }
    values.update(overrides)
    session.add(InventoryCard(**values))
    session.commit()


def test_batch_detail_shows_edit_form_with_current_values(tmp_path, monkeypatch):
    from models import Consignor

    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        consignor = Consignor(name="Jane", is_active=True)
        session.add(consignor)
        session.flush()
        batch = Batch(batch_code="A3", is_consignment=True, consignor_id=consignor.id)
        session.add(batch)
        session.commit()
        batch_id = batch.id
        consignor_id = consignor.id
    client = TestClient(main.app)
    response = client.get(f"/batches/{batch_id}")
    assert response.status_code == 200
    assert f'action="/batches/{batch_id}/edit"' in response.text
    assert 'value="A3"' in response.text
    assert "checked" in response.text
    assert f'<option value="{consignor_id}" selected>Jane</option>' in response.text


def test_edit_batch_renames(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = make_batch(session, "OLD1")
        batch_id = batch.id
    client = TestClient(main.app)
    response = client.post(
        f"/batches/{batch_id}/edit",
        data={"batch_code": "NEW1"}, follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(db) as session:
        assert session.get(Batch, batch_id).batch_code == "NEW1"


def test_edit_batch_rejects_duplicate_name(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_batch(session, "TAKEN")
        batch = make_batch(session, "MINE")
        batch_id = batch.id
    client = TestClient(main.app)
    response = client.post(f"/batches/{batch_id}/edit", data={"batch_code": "TAKEN"})
    assert response.status_code == 400
    with Session(db) as session:
        assert session.get(Batch, batch_id).batch_code == "MINE"


def test_edit_batch_sets_consignment_status(tmp_path, monkeypatch):
    from models import Consignor

    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        consignor = Consignor(name="Jane", is_active=True)
        session.add(consignor)
        session.flush()
        batch = make_batch(session, "A3")
        batch_id = batch.id
        consignor_id = consignor.id
    client = TestClient(main.app)
    response = client.post(
        f"/batches/{batch_id}/edit",
        data={"batch_code": "A3", "is_consignment": "true", "consignor_id": str(consignor_id)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(db) as session:
        updated = session.get(Batch, batch_id)
        assert updated.is_consignment is True
        assert updated.consignor_id == consignor_id


def test_edit_batch_clears_consignment_status(tmp_path, monkeypatch):
    from models import Consignor

    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        consignor = Consignor(name="Jane", is_active=True)
        session.add(consignor)
        session.flush()
        batch = Batch(batch_code="A3", is_consignment=True, consignor_id=consignor.id)
        session.add(batch)
        session.commit()
        batch_id = batch.id
    client = TestClient(main.app)
    response = client.post(
        f"/batches/{batch_id}/edit",
        data={"batch_code": "A3"}, follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(db) as session:
        updated = session.get(Batch, batch_id)
        assert updated.is_consignment is False
        assert updated.consignor_id is None


def test_edit_batch_requires_consignor_when_marking_consignment(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = make_batch(session, "A3")
        batch_id = batch.id
    client = TestClient(main.app)
    response = client.post(
        f"/batches/{batch_id}/edit",
        data={"batch_code": "A3", "is_consignment": "true"},
    )
    assert response.status_code == 400
    with Session(db) as session:
        assert session.get(Batch, batch_id).is_consignment is False


def test_edit_batch_rejects_unknown_consignor(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = make_batch(session, "A3")
        batch_id = batch.id
    client = TestClient(main.app)
    response = client.post(
        f"/batches/{batch_id}/edit",
        data={"batch_code": "A3", "is_consignment": "true", "consignor_id": "999999"},
    )
    assert response.status_code == 404


def test_edit_batch_ignores_consignment_change_when_sold_cards_exist(tmp_path, monkeypatch):
    """Once any card in the batch has actually sold, consignment status
    and consignor are locked -- a submitted change must be silently
    dropped (not applied, not an error), while the rename still goes
    through."""
    from models import Consignor

    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        original_consignor = Consignor(name="Jane", is_active=True)
        other_consignor = Consignor(name="Bob", is_active=True)
        session.add_all([original_consignor, other_consignor])
        session.flush()
        batch = Batch(
            batch_code="A3", is_consignment=True, consignor_id=original_consignor.id,
        )
        session.add(batch)
        session.flush()
        _add_card(session, batch.id, name="sold-card", status="sold")
        batch_id = batch.id
        original_consignor_id = original_consignor.id
        other_consignor_id = other_consignor.id

    client = TestClient(main.app)
    response = client.post(
        f"/batches/{batch_id}/edit",
        data={
            "batch_code": "A3RENAMED", "is_consignment": "",
            "consignor_id": str(other_consignor_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(db) as session:
        updated = session.get(Batch, batch_id)
        assert updated.batch_code == "A3RENAMED"
        assert updated.is_consignment is True
        assert updated.consignor_id == original_consignor_id


def test_batch_detail_disables_consignment_fields_when_sold_cards_exist(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = make_batch(session, "A3")
        _add_card(session, batch.id, name="sold-card", status="sold")
        batch_id = batch.id
    client = TestClient(main.app)
    response = client.get(f"/batches/{batch_id}")
    assert response.status_code == 200
    assert "already-sold cards" in response.text
    checkbox = re.search(r'<input type="checkbox" name="is_consignment"[^>]*>', response.text)
    assert checkbox and "disabled" in checkbox.group(0)
    select_tag = re.search(r'<select name="consignor_id"[^>]*>', response.text)
    assert select_tag and "disabled" in select_tag.group(0)


def test_batch_detail_shows_import_prompt_when_empty(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        empty = make_batch(session, "EMPTY1")
        empty_id = empty.id
    client = TestClient(main.app)
    response = client.get(f"/batches/{empty_id}")
    assert response.status_code == 200
    assert "This batch has no cards yet" in response.text
    assert f"/batches/import?target_batch_id={empty_id}" in response.text


def test_batch_detail_omits_import_prompt_when_not_empty(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        full = make_batch(session, "FULL1", with_card=True)
        full_id = full.id
    client = TestClient(main.app)
    response = client.get(f"/batches/{full_id}")
    assert response.status_code == 200
    assert "This batch has no cards yet" not in response.text


def test_inventory_search_batch_column_is_a_link(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = make_batch(session, "A3")
        session.add(InventoryCard(
            batch_id=batch.id, name="Alpha", mtgjson_id="mtg-a",
            language_id="EN", condition_id="LP", finish_id="NF", status="available",
        ))
        session.commit()
        batch_id = batch.id
    client = TestClient(main.app)
    response = client.get("/inventory", params={"q": "Alpha"})
    assert response.status_code == 200
    assert f'<a href="/batches/{batch_id}">A3</a>' in response.text


def test_inventory_search_filters_by_batch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch_a = make_batch(session, "A3")
        batch_b = make_batch(session, "B1")
        session.add(InventoryCard(
            batch_id=batch_a.id, name="Alpha", mtgjson_id="mtg-a",
            language_id="EN", condition_id="LP", finish_id="NF", status="available",
        ))
        session.add(InventoryCard(
            batch_id=batch_b.id, name="Beta", mtgjson_id="mtg-b",
            language_id="EN", condition_id="LP", finish_id="NF", status="available",
        ))
        session.commit()
    client = TestClient(main.app)
    response = client.get("/inventory", params={"batch": "A3"})
    assert response.status_code == 200
    assert "Alpha" in response.text
    assert "Beta" not in response.text


def test_inventory_search_shows_import_and_create_links(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory")
    assert response.status_code == 200
    assert 'href="/batches/import"' in response.text
    assert 'href="/batches/new"' in response.text


def test_end_to_end_csv_import_into_existing_empty_batch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "Path", lambda value: tmp_path / value)
    with Session(db) as session:
        empty = make_batch(session, "A3")
        empty_id = empty.id

    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,2,,"])
    client = TestClient(main.app)

    preview_response = client.post(
        "/imports/production-preview",
        data={
            "mode": "existing", "batch_code": "", "source_location": "Shelf A",
            "target_batch_id": str(empty_id),
        },
        files={"file": ("next.csv", contents, "text/csv")},
    )
    assert preview_response.status_code == 200
    assert "Target batch (existing, empty)" in preview_response.text
    assert "A3" in preview_response.text

    with Session(db) as session:
        pending = session.query(main.PendingImport).one()
        assert pending.batch_id == empty_id
        pending_id = pending.id

    confirm_response = client.post(f"/imports/{pending_id}/confirm")
    assert confirm_response.status_code == 200
    assert "Production Import Completed" in confirm_response.text

    with Session(db) as session:
        assert session.query(Batch).count() == 1
        assert session.query(Batch).one().id == empty_id
        assert session.query(InventoryCard).filter_by(batch_id=empty_id).count() == 2


def test_import_existing_mode_without_a_selection_is_refused(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,"])
    client = TestClient(main.app)
    response = client.post(
        "/imports/production-preview",
        data={"mode": "existing", "batch_code": "", "source_location": "Shelf A"},
        files={"file": ("next.csv", contents, "text/csv")},
    )
    assert response.status_code == 400
    assert "Choose an empty batch" in response.text
