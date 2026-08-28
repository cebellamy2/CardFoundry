from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from models import Base, Batch, InventoryChangeLog, InventoryCard


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'inventory-card-edit.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(database, "engine", db)
    return db


def make_batch(db, code="A1"):
    with Session(db) as session:
        batch = Batch(batch_code=code)
        session.add(batch)
        session.commit()
        session.refresh(batch)
        return batch


def make_card(db, batch_id, **overrides):
    with Session(db) as session:
        values = {"batch_id": batch_id, "name": "Lightning Bolt", "status": "available"}
        values.update(overrides)
        card = InventoryCard(**values)
        session.add(card)
        session.commit()
        session.refresh(card)
        return card


BOLT_METADATA = {
    "sf-bolt": {
        "id": "sf-bolt", "name": "Lightning Bolt", "set": "lea", "collector_number": "161",
    },
}


def mock_scryfall(monkeypatch, data=BOLT_METADATA):
    monkeypatch.setattr(main, "fetch_scryfall_cards", lambda ids: {sid: data[sid] for sid in ids if sid in data})


def edit_form(**overrides):
    form = {
        "name": "Lightning Bolt", "set_code": "lea", "collector_number": "161",
        "scryfall_id": "sf-bolt", "batch_id": "1",
    }
    form.update(overrides)
    return form


def test_edit_with_no_scryfall_id_skips_validation_entirely(tmp_path, monkeypatch):
    """A legacy-imported card can legitimately have no scryfall_id at all
    -- this check isn't its concern."""
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db)
    card = make_card(db, batch.id, scryfall_id=None)
    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card.id}/edit",
        data={"name": "Renamed Card", "set_code": "", "collector_number": "", "batch_id": str(batch.id)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(db) as session:
        assert session.get(InventoryCard, card.id).name == "Renamed Card"


def test_edit_with_matching_scryfall_id_saves_cleanly(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db)
    card = make_card(db, batch.id, scryfall_id="sf-bolt", set_code="LEA", collector_number="161")
    mock_scryfall(monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card.id}/edit",
        data=edit_form(batch_id=str(batch.id)),
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(db) as session:
        refreshed = session.get(InventoryCard, card.id)
        assert refreshed.name == "Lightning Bolt"
        assert refreshed.scryfall_id == "sf-bolt"


def test_edit_name_typo_conflicting_with_scryfall_fails_closed(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db)
    card = make_card(db, batch.id, scryfall_id="sf-bolt")
    mock_scryfall(monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card.id}/edit",
        data=edit_form(name="Lightning Bols", batch_id=str(batch.id)),
    )
    assert response.status_code == 400
    assert "conflicts on: name" in response.text
    with Session(db) as session:
        assert session.get(InventoryCard, card.id).name == "Lightning Bolt"


def test_edit_set_code_conflicting_with_scryfall_fails_closed(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db)
    card = make_card(db, batch.id, scryfall_id="sf-bolt")
    mock_scryfall(monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card.id}/edit",
        data=edit_form(set_code="m10", batch_id=str(batch.id)),
    )
    assert response.status_code == 400
    assert "conflicts on: set" in response.text


def test_edit_collector_number_conflicting_with_scryfall_fails_closed(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db)
    card = make_card(db, batch.id, scryfall_id="sf-bolt")
    mock_scryfall(monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card.id}/edit",
        data=edit_form(collector_number="999", batch_id=str(batch.id)),
    )
    assert response.status_code == 400
    assert "conflicts on: collector" in response.text


def test_edit_unresolvable_scryfall_id_fails_closed(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db)
    card = make_card(db, batch.id, scryfall_id="sf-bolt")
    monkeypatch.setattr(main, "fetch_scryfall_cards", lambda ids: {})
    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card.id}/edit",
        data=edit_form(scryfall_id="sf-nonexistent", batch_id=str(batch.id)),
    )
    assert response.status_code == 400
    assert "No Scryfall printing found" in response.text


def test_edit_unreachable_scryfall_shows_readable_error(tmp_path, monkeypatch):
    import httpx

    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db)
    card = make_card(db, batch.id, scryfall_id="sf-bolt")

    def raise_unreachable(ids):
        raise httpx.ConnectError("connection failed")

    monkeypatch.setattr(main, "fetch_scryfall_cards", raise_unreachable)
    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card.id}/edit",
        data=edit_form(batch_id=str(batch.id)),
    )
    assert response.status_code == 502
    assert "unreachable" in response.text.lower()


def test_edit_dfc_front_face_only_name_is_accepted(tmp_path, monkeypatch):
    """Confirmed live against production before shipping: transform/MDFC
    cards are stored with just the front face's name, while Scryfall's
    top-level `name` for the same scryfall_id is the full "Front // Back"
    combined string -- that's a legitimate convention, not a conflict."""
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db)
    card = make_card(db, batch.id, name="Vanille, Cheerful l'Cie", scryfall_id="sf-vanille")
    monkeypatch.setattr(main, "fetch_scryfall_cards", lambda ids: {
        "sf-vanille": {
            "id": "sf-vanille",
            "name": "Vanille, Cheerful l'Cie // Ragnarok, Divine Deliverance",
            "set": "fin", "collector_number": "42",
        },
    })
    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card.id}/edit",
        data=edit_form(
            name="Vanille, Cheerful l'Cie", scryfall_id="sf-vanille",
            set_code="fin", collector_number="42", batch_id=str(batch.id),
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_edit_double_sided_token_collector_range_is_accepted(tmp_path, monkeypatch):
    """Confirmed live against production before shipping: a double-sided
    token is stored with a combined "18-22" collector-number range, while
    Scryfall's own record for the front face's scryfall_id alone reports
    just "18" -- also a legitimate convention, not a conflict."""
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db)
    card = make_card(
        db, batch.id, name="Orc Army // Food", scryfall_id="sf-orc-army",
        set_code="TLTR", collector_number="18-22",
    )
    monkeypatch.setattr(main, "fetch_scryfall_cards", lambda ids: {
        "sf-orc-army": {
            "id": "sf-orc-army", "name": "Orc Army", "set": "tltr", "collector_number": "18",
        },
    })
    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card.id}/edit",
        data=edit_form(
            name="Orc Army // Food", scryfall_id="sf-orc-army",
            set_code="TLTR", collector_number="18-22", batch_id=str(batch.id),
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_edit_unrelated_collector_number_still_fails_closed(tmp_path, monkeypatch):
    """The token-range leniency is a narrow startswith("<number>-") allowance
    -- it must not swallow a genuinely wrong collector number."""
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db)
    card = make_card(db, batch.id, scryfall_id="sf-bolt", collector_number="180")
    mock_scryfall(monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card.id}/edit",
        data=edit_form(collector_number="180", batch_id=str(batch.id)),
    )
    assert response.status_code == 400
    assert "conflicts on: collector" in response.text


def test_edit_conflicting_scryfall_id_leaves_nothing_changed_and_no_audit_entry(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db)
    card = make_card(db, batch.id, scryfall_id="sf-bolt")
    mock_scryfall(monkeypatch)
    client = TestClient(main.app)
    client.post(
        f"/inventory/{card.id}/edit",
        data=edit_form(name="Lightning Bols", batch_id=str(batch.id)),
    )
    with Session(db) as session:
        assert session.get(InventoryCard, card.id).name == "Lightning Bolt"
        assert session.query(InventoryChangeLog).count() == 0
