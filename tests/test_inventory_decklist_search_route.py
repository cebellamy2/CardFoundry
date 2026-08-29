from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard, InventoryChangeLog


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'inventory_decklist.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def add_batch(session, code="B1"):
    batch = Batch(batch_code=code)
    session.add(batch)
    session.flush()
    return batch


def add_card(session, batch, **overrides):
    values = {"batch_id": batch.id, "name": "Lightning Bolt", "status": "available"}
    values.update(overrides)
    session.add(InventoryCard(**values))
    session.commit()


def test_single_mode_is_the_default(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory")
    assert response.status_code == 200
    assert 'name="q"' in response.text
    assert 'name="decklist"' not in response.text


def test_decklist_mode_shows_the_textarea_not_the_single_search_input(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory?mode=decklist")
    assert response.status_code == 200
    assert 'name="decklist"' in response.text
    assert 'placeholder="Lightning Bolt"' not in response.text


def test_mode_toggle_is_present_on_both_views(tmp_path, monkeypatch):
    """Real tabs (UX epic item 9), not a <select>+"Switch" button."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    single = client.get("/inventory")
    decklist = client.get("/inventory?mode=decklist")
    assert 'nav class="tabs"' in single.text
    assert 'href="/inventory?mode=single" class="tab active"' in single.text
    assert 'href="/inventory?mode=decklist" class="tab"' in single.text
    assert 'nav class="tabs"' in decklist.text
    assert 'href="/inventory?mode=decklist" class="tab active"' in decklist.text
    assert 'href="/inventory?mode=single" class="tab"' in decklist.text


def test_decklist_search_shows_fillable_and_short_results(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt")
        add_card(session, b1, name="Lightning Bolt")
        add_card(session, b1, name="Sol Ring", set_code="LEA", collector_number="233")

    response = TestClient(main.app).post(
        "/inventory/decklist-search",
        data={"decklist": "2 Lightning Bolt\n1 Sol Ring (LEA) 233\n4 Black Lotus"},
    )
    assert response.status_code == 200
    assert "Lightning Bolt" in response.text
    assert "Sol Ring" in response.text
    assert "Fillable" in response.text
    assert "Couldn&#x27;t Find/Parse (1)" in response.text or "Black Lotus" in response.text


def test_decklist_search_reports_shortfall(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt")

    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "4 Lightning Bolt"},
    )
    assert response.status_code == 200
    assert "Short" in response.text


def test_decklist_search_one_bad_line_does_not_block_the_rest(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt")

    response = TestClient(main.app).post(
        "/inventory/decklist-search",
        data={"decklist": "1 Lightning Bolt\nnot a valid line at all!!!"},
    )
    assert response.status_code == 200
    assert "Lightning Bolt" in response.text
    assert "not a valid line at all" in response.text


def test_decklist_search_ignores_unavailable_inventory(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", status="sold")

    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "1 Lightning Bolt"},
    )
    assert response.status_code == 200
    assert "No matching sellable card found" in response.text or "Couldn" in response.text


def test_decklist_search_repopulates_the_textarea_with_submitted_text(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "4 Lightning Bolt"},
    )
    assert response.status_code == 200
    assert "4 Lightning Bolt" in response.text


def test_decklist_search_never_writes_anything(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt")

    TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "1 Lightning Bolt"},
    )

    with Session(db) as session:
        card = session.query(InventoryCard).filter_by(name="Lightning Bolt").one()
        assert card.status == "available"


def test_results_show_nonfoil_and_foil_batch_links(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        nonfoil_batch = add_batch(session, "NF-BATCH")
        foil_batch = add_batch(session, "FO-BATCH")
        add_card(session, nonfoil_batch, name="Lightning Bolt", finish_id="NF")
        add_card(session, foil_batch, name="Lightning Bolt", finish_id="FO")
        nonfoil_batch_id, foil_batch_id = nonfoil_batch.id, foil_batch.id

    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "1 Lightning Bolt"},
    )
    assert response.status_code == 200
    assert f'<a href="/batches/{nonfoil_batch_id}">NF-BATCH</a>' in response.text
    assert f'<a href="/batches/{foil_batch_id}">FO-BATCH</a>' in response.text


def test_missing_finish_batch_renders_as_a_dash_not_blank_column(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", finish_id="NF")

    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "1 Lightning Bolt"},
    )
    assert response.status_code == 200
    assert "&mdash;" in response.text


def test_results_include_a_required_personal_use_note_and_mark_buttons(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", finish_id="NF")

    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "1 Lightning Bolt"},
    )
    assert response.status_code == 200
    assert 'name="personal_use_note"' in response.text
    assert "required" in response.text
    assert 'name="mark"' in response.text
    assert "Mark for personal use" in response.text


def _mark_value(name, set_code, collector_number, batch_id, foil, quantity):
    return "\x1f".join([
        name, set_code or "", collector_number or "", str(batch_id),
        "foil" if foil else "nonfoil", str(quantity),
    ])


def test_preview_requires_a_note(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", finish_id="NF")
        batch_id = b1.id

    response = TestClient(main.app).post(
        "/inventory/decklist-search/mark-personal-use/preview",
        data={
            "decklist_text": "1 Lightning Bolt", "personal_use_note": "  ",
            "mark": _mark_value("Lightning Bolt", None, None, batch_id, False, 1),
        },
    )
    assert response.status_code == 400
    assert "note is required" in response.text


def test_preview_shows_matched_cards_and_confirm_form(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", finish_id="NF")
        batch_id = b1.id

    response = TestClient(main.app).post(
        "/inventory/decklist-search/mark-personal-use/preview",
        data={
            "decklist_text": "1 Lightning Bolt", "personal_use_note": "Taking one home",
            "mark": _mark_value("Lightning Bolt", None, None, batch_id, False, 1),
        },
    )
    assert response.status_code == 200
    assert "Confirm Mark for Personal Use" in response.text
    assert "Taking one home" in response.text
    assert 'name="card_ref"' in response.text
    assert 'action="/inventory/decklist-search/mark-personal-use/confirm"' in response.text


def test_preview_reports_shortfall_without_blocking(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", finish_id="NF")
        batch_id = b1.id

    response = TestClient(main.app).post(
        "/inventory/decklist-search/mark-personal-use/preview",
        data={
            "decklist_text": "3 Lightning Bolt", "personal_use_note": "Note",
            "mark": _mark_value("Lightning Bolt", None, None, batch_id, False, 3),
        },
    )
    assert response.status_code == 200
    assert "Only 1 of the requested 3" in response.text
    assert "Confirm Mark for Personal Use" in response.text


def test_preview_refuses_when_nothing_available(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        session.commit()
        batch_id = b1.id

    response = TestClient(main.app).post(
        "/inventory/decklist-search/mark-personal-use/preview",
        data={
            "decklist_text": "1 Lightning Bolt", "personal_use_note": "Note",
            "mark": _mark_value("Lightning Bolt", None, None, batch_id, False, 1),
        },
    )
    assert response.status_code == 409
    assert "Nothing Available" in response.text


def test_confirm_marks_cards_removed_with_personal_use_reason_and_note(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", finish_id="NF")
        card_id = session.query(InventoryCard).one().id
        batch_id = b1.id

    client = TestClient(main.app)
    preview = client.post(
        "/inventory/decklist-search/mark-personal-use/preview",
        data={
            "decklist_text": "1 Lightning Bolt", "personal_use_note": "Taking one home",
            "mark": _mark_value("Lightning Bolt", None, None, batch_id, False, 1),
        },
    )
    ref = preview.text.split('name="card_ref" value="')[1].split('"')[0]

    response = client.post(
        "/inventory/decklist-search/mark-personal-use/confirm",
        data={
            "decklist_text": "1 Lightning Bolt", "personal_use_note": "Taking one home",
            "card_ref": [ref],
        },
    )
    assert response.status_code == 200
    assert "Marked 1 card(s) for personal use" in response.text

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert card.status == "removed"
        assert card.removal_reason == "personal_use"
        assert card.removal_note == "Taking one home"
        log = session.query(InventoryChangeLog).filter_by(inventory_card_id=card_id).one()
        assert "inventory_removal" in log.change_summary


def test_confirm_re_renders_decklist_results_with_updated_on_hand(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", finish_id="NF")
        add_card(session, b1, name="Lightning Bolt", finish_id="NF")
        batch_id = b1.id

    client = TestClient(main.app)
    preview = client.post(
        "/inventory/decklist-search/mark-personal-use/preview",
        data={
            "decklist_text": "2 Lightning Bolt", "personal_use_note": "Note",
            "mark": _mark_value("Lightning Bolt", None, None, batch_id, False, 1),
        },
    )
    ref = preview.text.split('name="card_ref" value="')[1].split('"')[0]

    response = client.post(
        "/inventory/decklist-search/mark-personal-use/confirm",
        data={"decklist_text": "2 Lightning Bolt", "personal_use_note": "Note", "card_ref": [ref]},
    )
    assert response.status_code == 200
    assert "2 Lightning Bolt" in response.text
    assert "Short" in response.text


def test_confirm_reports_stale_selection_without_crashing(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", finish_id="NF")

    response = TestClient(main.app).post(
        "/inventory/decklist-search/mark-personal-use/confirm",
        data={
            "decklist_text": "1 Lightning Bolt", "personal_use_note": "Note",
            "card_ref": ["99999:bogus-hash"],
        },
    )
    assert response.status_code == 200
    assert "Card #99999" in response.text
    assert "Inventory card not found" in response.text
