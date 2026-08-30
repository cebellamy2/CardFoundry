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


# --- Phase 9: status-scope toggle --------------------------------------------

def test_status_scope_toggle_is_present_and_unchecked_by_default(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "1 Lightning Bolt"},
    )
    assert response.status_code == 200
    assert 'name="status_scope" value="extended"' in response.text
    assert 'name="status_scope" value="extended" checked' not in response.text


def test_status_scope_toggle_checked_when_extended_was_selected(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).post(
        "/inventory/decklist-search",
        data={"decklist": "1 Lightning Bolt", "status_scope": "extended"},
    )
    assert response.status_code == 200
    assert 'name="status_scope" value="extended" checked' in response.text


def test_default_scope_excludes_reserved_and_unsellable(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", status="reserved")

    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "1 Lightning Bolt"},
    )
    assert response.status_code == 200
    assert "Couldn't Find/Parse (1)" in response.text


def test_extended_scope_surfaces_reserved_and_unsellable_not_sold_or_removed(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", status="reserved")
        add_card(session, b1, name="Sol Ring", status="sold")

    response = TestClient(main.app).post(
        "/inventory/decklist-search",
        data={"decklist": "1 Lightning Bolt\n1 Sol Ring", "status_scope": "extended"},
    )
    assert response.status_code == 200
    assert "Fillable" in response.text  # Lightning Bolt (reserved) now found
    assert "Couldn't Find/Parse (1)" in response.text  # Sol Ring (sold) still excluded


def test_invalid_status_scope_falls_back_to_available(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", status="reserved")

    response = TestClient(main.app).post(
        "/inventory/decklist-search",
        data={"decklist": "1 Lightning Bolt", "status_scope": "bogus"},
    )
    assert response.status_code == 200
    assert "Couldn't Find/Parse (1)" in response.text


# --- Phase 9: paste-size cap --------------------------------------------------

def test_paste_over_500_lines_is_refused(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    decklist = "\n".join(f"1 Card {n}" for n in range(501))
    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": decklist},
    )
    assert response.status_code == 400
    assert "Decklist Too Long" in response.text


def test_paste_at_exactly_500_lines_is_accepted(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    decklist = "\n".join(f"1 Card {n}" for n in range(500))
    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": decklist},
    )
    assert response.status_code == 200


# --- Phase 9: bulk-action selection checkboxes -------------------------------

def test_results_include_bulk_action_group_checkboxes_and_form(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", finish_id="NF")

    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "1 Lightning Bolt"},
    )
    assert response.status_code == 200
    assert 'id="decklist-bulk-action-form"' in response.text
    assert 'name="group"' in response.text
    assert 'form="decklist-bulk-action-form"' in response.text
    assert 'formaction="/inventory/decklist-search/bulk-action/move-batch/preview"' in response.text
    assert 'formaction="/inventory/decklist-search/bulk-action/mark-unavailable/preview"' in response.text
    assert 'formaction="/inventory/decklist-search/bulk-action/mark-available/preview"' in response.text
    assert 'formaction="/inventory/decklist-search/bulk-action/remove/preview"' in response.text


def test_personal_use_button_still_present_alongside_new_checkbox(tmp_path, monkeypatch):
    """Additive-only: the shipped v1.60.0 personal-use flow must keep
    working untouched next to the new bulk-action selection."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", finish_id="NF")

    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "1 Lightning Bolt"},
    )
    assert response.status_code == 200
    assert 'name="mark"' in response.text
    assert "Mark for personal use" in response.text
    assert 'name="group"' in response.text


# --- Phase 9: bulk-action resolve/preview routes -----------------------------

def _group_value(name, set_code, collector_number, batch_id, foil, quantity):
    return "\x1f".join([
        name, set_code or "", collector_number or "", str(batch_id),
        "foil" if foil else "nonfoil", str(quantity),
    ])


def test_bulk_preview_no_selection_is_rejected(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    for path in [
        "/inventory/decklist-search/bulk-action/move-batch/preview",
        "/inventory/decklist-search/bulk-action/mark-unavailable/preview",
        "/inventory/decklist-search/bulk-action/mark-available/preview",
        "/inventory/decklist-search/bulk-action/remove/preview",
    ]:
        response = TestClient(main.app).post(path, data={})
        assert response.status_code == 400, path
        assert "No cards selected" in response.text


def test_bulk_preview_refuses_when_nothing_resolves(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        batch_id = b1.id

    response = TestClient(main.app).post(
        "/inventory/decklist-search/bulk-action/remove/preview",
        data={"group": [_group_value("Lightning Bolt", None, None, batch_id, False, 1)]},
    )
    assert response.status_code == 409
    assert "Nothing to Act On" in response.text


def test_bulk_move_batch_preview_shows_resolved_cards_and_confirm_form(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        source = add_batch(session, "SOURCE")
        target = add_batch(session, "TARGET")
        add_card(session, source, name="Lightning Bolt", finish_id="NF")
        source_id, target_id = source.id, target.id

    response = TestClient(main.app).post(
        "/inventory/decklist-search/bulk-action/move-batch/preview",
        data={
            "group": [_group_value("Lightning Bolt", None, None, source_id, False, 1)],
            "target_batch_id": str(target_id),
        },
    )
    assert response.status_code == 200
    assert "Confirm Bulk Move" in response.text
    assert "TARGET" in response.text
    assert 'name="card_ids"' in response.text
    assert 'action="/inventory-cards/bulk-move-batch"' in response.text


def test_bulk_move_batch_preview_requires_a_target(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", finish_id="NF")
        batch_id = b1.id

    response = TestClient(main.app).post(
        "/inventory/decklist-search/bulk-action/move-batch/preview",
        data={"group": [_group_value("Lightning Bolt", None, None, batch_id, False, 1)]},
    )
    assert response.status_code == 400
    assert "Select a target batch" in response.text


def test_bulk_move_batch_end_to_end_moves_the_card(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        source = add_batch(session, "SOURCE")
        target = add_batch(session, "TARGET")
        add_card(session, source, name="Lightning Bolt", finish_id="NF")
        card_id = session.query(InventoryCard).one().id
        source_id, target_id = source.id, target.id

    client = TestClient(main.app)
    preview = client.post(
        "/inventory/decklist-search/bulk-action/move-batch/preview",
        data={
            "group": [_group_value("Lightning Bolt", None, None, source_id, False, 1)],
            "target_batch_id": str(target_id),
        },
    )
    confirm = client.post(
        "/inventory-cards/bulk-move-batch",
        data={
            "card_ids": [str(card_id)], "target_batch_id": str(target_id),
            "back_link": "/inventory?mode=decklist",
        },
    )
    assert confirm.status_code == 200
    assert "Bulk Move Results" in confirm.text

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert card.batch_id == target_id


def test_bulk_mark_unavailable_preview_carries_reason_and_note_forward(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", finish_id="NF")
        batch_id = b1.id

    response = TestClient(main.app).post(
        "/inventory/decklist-search/bulk-action/mark-unavailable/preview",
        data={
            "group": [_group_value("Lightning Bolt", None, None, batch_id, False, 1)],
            "unsellable_reason": "damaged", "unsellable_note": "Water damage",
        },
    )
    assert response.status_code == 200
    assert "Confirm Bulk Mark Unavailable" in response.text
    assert 'value="damaged"' in response.text
    assert 'value="Water damage"' in response.text
    assert 'action="/inventory-cards/bulk-mark-unavailable"' in response.text


def test_bulk_mark_available_preview_and_confirm_end_to_end(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(
            session, b1, name="Lightning Bolt", finish_id="NF", status="unsellable",
            unsellable_reason="damaged", mtgjson_id="mtg-1", language_id="EN",
            condition_id="NM",
        )
        card_id = session.query(InventoryCard).one().id
        batch_id = b1.id

    client = TestClient(main.app)
    preview = client.post(
        "/inventory/decklist-search/bulk-action/mark-available/preview",
        data={
            "group": [_group_value("Lightning Bolt", None, None, batch_id, False, 1)],
            "status_scope": "extended",
        },
    )
    assert preview.status_code == 200
    assert "Confirm Bulk Mark Available" in preview.text

    confirm = client.post(
        "/inventory-cards/bulk-mark-available",
        data={"card_ids": [str(card_id)], "back_link": "/inventory?mode=decklist"},
    )
    assert confirm.status_code == 200

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert card.status == "available"


def test_bulk_remove_preview_and_confirm_end_to_end(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", finish_id="NF")
        card_id = session.query(InventoryCard).one().id
        batch_id = b1.id

    client = TestClient(main.app)
    preview = client.post(
        "/inventory/decklist-search/bulk-action/remove/preview",
        data={
            "group": [_group_value("Lightning Bolt", None, None, batch_id, False, 1)],
            "removal_reason": "duplicate_record", "removal_note": "Dupe entry",
        },
    )
    assert preview.status_code == 200
    assert "Confirm Bulk Remove" in preview.text

    confirm = client.post(
        "/inventory-cards/bulk-remove",
        data={
            "card_ids": [str(card_id)], "removal_reason": "duplicate_record",
            "removal_note": "Dupe entry", "back_link": "/inventory?mode=decklist",
        },
    )
    assert confirm.status_code == 200

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert card.status == "removed"
        assert card.removal_reason == "duplicate_record"


def test_bulk_preview_resolution_respects_extended_status_scope(tmp_path, monkeypatch):
    """A reserved card is only resolvable when the search that produced
    the selection used the extended scope -- proves the resolve route
    threads status_scope through to matching_available_cards_in_batch."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", finish_id="NF", status="reserved")
        batch_id = b1.id

    default_scope = TestClient(main.app).post(
        "/inventory/decklist-search/bulk-action/remove/preview",
        data={"group": [_group_value("Lightning Bolt", None, None, batch_id, False, 1)]},
    )
    assert default_scope.status_code == 409

    extended_scope = TestClient(main.app).post(
        "/inventory/decklist-search/bulk-action/remove/preview",
        data={
            "group": [_group_value("Lightning Bolt", None, None, batch_id, False, 1)],
            "status_scope": "extended",
        },
    )
    assert extended_scope.status_code == 200
    assert "Confirm Bulk Remove" in extended_scope.text


def test_bulk_preview_dedupes_duplicate_decklist_lines(tmp_path, monkeypatch):
    """Two decklist lines resolving to the same underlying card(s) --
    the judgment-call default is to dedupe server-side rather than pass
    the same InventoryCard id twice."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", finish_id="NF")
        card_id = session.query(InventoryCard).one().id
        batch_id = b1.id

    same_group = _group_value("Lightning Bolt", None, None, batch_id, False, 1)
    response = TestClient(main.app).post(
        "/inventory/decklist-search/bulk-action/remove/preview",
        data={"group": [same_group, same_group]},
    )
    assert response.status_code == 200
    assert response.text.count(f'name="card_ids" value="{card_id}"') == 1


def test_bulk_move_batch_all_or_nothing_guard_still_applies(tmp_path, monkeypatch):
    """The canonical bulk-move-batch route's own all-or-nothing guard
    (available cards only) is untouched -- confirming with a non-available
    resolved card must be blocked exactly like it already is for
    /inventory and /batches/{id}."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        target = add_batch(session, "TARGET")
        add_card(session, b1, name="Lightning Bolt", finish_id="NF", status="unsellable")
        card_id = session.query(InventoryCard).one().id
        target_id = target.id

    response = TestClient(main.app).post(
        "/inventory-cards/bulk-move-batch",
        data={
            "card_ids": [str(card_id)], "target_batch_id": str(target_id),
            "back_link": "/inventory?mode=decklist",
        },
    )
    assert response.status_code == 409
    assert "Move blocked" in response.text


# --- Phase 10: flag-and-nest printing display --------------------------------

def test_single_printing_line_renders_exactly_as_before_no_nested_row(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session)
        add_card(session, b1, name="Lightning Bolt", set_code="LEA", collector_number="161")

    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "1 Lightning Bolt"},
    )
    assert response.status_code == 200
    assert "decklist-printings-row" not in response.text
    assert "Printings found" not in response.text


def test_name_only_line_spanning_printings_shows_nested_breakdown(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        b1 = add_batch(session, "LEA-BATCH")
        b2 = add_batch(session, "M10-BATCH")
        add_card(session, b1, name="Lightning Bolt", set_code="LEA", collector_number="161")
        add_card(session, b2, name="Lightning Bolt", set_code="M10", collector_number="146")

    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "2 Lightning Bolt"},
    )
    assert response.status_code == 200
    assert 'class="decklist-printings-row"' in response.text
    assert "Printings found" in response.text
    assert "LEA-BATCH" in response.text
    assert "M10-BATCH" in response.text
    # Neither printing was specifically requested, so nothing is flagged.
    assert "Exact match" not in response.text


def test_exact_printing_line_flags_the_requested_printing_and_lists_the_other(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        requested = add_batch(session, "REQUESTED-BATCH")
        other = add_batch(session, "OTHER-BATCH")
        add_card(session, requested, name="Lightning Bolt", set_code="LEA", collector_number="161")
        add_card(session, other, name="Lightning Bolt", set_code="M10", collector_number="146")

    response = TestClient(main.app).post(
        "/inventory/decklist-search",
        data={"decklist": "1 Lightning Bolt (LEA) 161"},
    )
    assert response.status_code == 200
    assert "Fillable" in response.text  # on_hand/fillable still scoped to LEA #161 alone
    assert 'class="decklist-printings-row"' in response.text
    assert "OTHER-BATCH" in response.text  # the un-requested printing is still surfaced
    # Two "Exact match" occurrences: main row's Printing cell + the nested list entry.
    assert response.text.count("Exact match") == 2


def test_many_printing_line_caps_visible_and_uses_details_for_overflow(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        for n in range(8):
            batch = add_batch(session, f"BATCH-{n}")
            add_card(
                session, batch, name="Lightning Bolt", set_code=f"S{n}", collector_number="1",
            )

    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "8 Lightning Bolt"},
    )
    assert response.status_code == 200
    assert "<details>" in response.text
    assert "+3 more printing(s)" in response.text  # 8 printings, cap 5 -> 3 overflow


def test_selection_still_resolves_correctly_after_display_change(tmp_path, monkeypatch):
    """Checkboxes/selection are untouched by the display change -- a
    bulk-action selection on a multi-printing line must still resolve to
    exactly the cards in the LINE-level batch/finish it targets, same as
    before Phase 10."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        older = add_batch(session, "OLDER-BATCH")
        newer = add_batch(session, "NEWER-BATCH")
        from datetime import datetime
        add_card(
            session, older, name="Lightning Bolt", set_code="LEA", collector_number="161",
            finish_id="NF", imported_at=datetime(2020, 1, 1),
        )
        add_card(
            session, newer, name="Lightning Bolt", set_code="M10", collector_number="146",
            finish_id="NF", imported_at=datetime(2026, 1, 1),
        )
        older_card_id = (
            session.query(InventoryCard).filter_by(batch_id=older.id).one().id
        )

    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "2 Lightning Bolt"},
    )
    assert response.status_code == 200
    # Line-level checkbox still targets the OLDEST batch/finish overall,
    # unaffected by the new nested per-printing rows.
    group_value = "\x1f".join(["Lightning Bolt", "", "", str(older.id), "nonfoil", "2"])
    assert f'value="{group_value}"' in response.text

    preview = TestClient(main.app).post(
        "/inventory/decklist-search/bulk-action/remove/preview",
        data={
            "group": [group_value],
            "removal_reason": "duplicate_record", "removal_note": "test",
        },
    )
    assert preview.status_code == 200
    assert f'name="card_ids" value="{older_card_id}"' in preview.text
