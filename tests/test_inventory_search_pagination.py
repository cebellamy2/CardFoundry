from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'inventory_search.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def add_cards(session, count, *, batch_code="A1", status="available", name_prefix="Card"):
    batch = session.query(Batch).filter_by(batch_code=batch_code).one_or_none()
    if not batch:
        batch = Batch(batch_code=batch_code, is_archived=False)
        session.add(batch)
        session.flush()
    for i in range(count):
        session.add(InventoryCard(
            batch_id=batch.id, name=f"{name_prefix} {i:03d}",
            set_code="SET", collector_number=str(i), status=status,
        ))
    session.commit()


def test_bare_page_load_shows_all_inventory_by_default(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_cards(session, 3)
    response = TestClient(main.app).get("/inventory")
    assert response.status_code == 200
    assert "No cards found." not in response.text
    assert "Card 000" in response.text
    assert "Card 001" in response.text
    assert "Card 002" in response.text
    assert "All Inventory" in response.text


def test_bare_page_load_shows_every_status_not_just_available(tmp_path, monkeypatch):
    """Matches the existing behavior of a real search (no implicit status
    filter unless the operator picks one from the dropdown) and of the
    pre-existing "Show All Inventory" button -- not a new, narrower
    default."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_cards(session, 1, status="available", name_prefix="Avail")
        add_cards(session, 1, status="sold", name_prefix="Sold")
        add_cards(session, 1, status="removed", name_prefix="Removed")
    response = TestClient(main.app).get("/inventory")
    assert response.status_code == 200
    assert "Avail 000" in response.text
    assert "Sold 000" in response.text
    assert "Removed 000" in response.text


def test_explicit_search_still_works_unchanged(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_cards(session, 1, name_prefix="Bolt")
        add_cards(session, 1, name_prefix="Elves")
    response = TestClient(main.app).get("/inventory?q=Bolt")
    assert response.status_code == 200
    assert "Bolt 000" in response.text
    assert "Elves 000" not in response.text
    assert "Results" in response.text


def test_pagination_splits_results_across_pages(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "INVENTORY_SEARCH_PAGE_SIZE", 3)
    with Session(db) as session:
        add_cards(session, 7)
    client = TestClient(main.app)

    page1 = client.get("/inventory")
    assert page1.status_code == 200
    assert "Card 000" in page1.text and "Card 002" in page1.text
    assert "Card 003" not in page1.text
    assert "Showing" in page1.text and "1&ndash;3" in page1.text.replace("\n", "")
    assert "of" in page1.text and "7" in page1.text
    assert "Page 1 of 3" in page1.text

    page2 = client.get("/inventory?page=2")
    assert page2.status_code == 200
    assert "Card 003" in page2.text and "Card 005" in page2.text
    assert "Card 000" not in page2.text
    assert "Page 2 of 3" in page2.text

    page3 = client.get("/inventory?page=3")
    assert "Card 006" in page3.text
    assert "Page 3 of 3" in page3.text


def test_out_of_range_page_clamps_to_last_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "INVENTORY_SEARCH_PAGE_SIZE", 3)
    with Session(db) as session:
        add_cards(session, 7)
    response = TestClient(main.app).get("/inventory?page=999")
    assert response.status_code == 200
    assert "Page 3 of 3" in response.text
    assert "Card 006" in response.text


def test_zero_or_negative_page_clamps_to_first_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "INVENTORY_SEARCH_PAGE_SIZE", 3)
    with Session(db) as session:
        add_cards(session, 7)
    response = TestClient(main.app).get("/inventory?page=0")
    assert response.status_code == 200
    assert "Page 1 of 3" in response.text
    assert "Card 000" in response.text


def test_no_pagination_controls_when_everything_fits_on_one_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_cards(session, 2)
    response = TestClient(main.app).get("/inventory")
    assert response.status_code == 200
    assert "Page 1 of 1" not in response.text
    assert "Previous" not in response.text
    assert "Next" not in response.text


def test_sort_link_preserves_the_batch_filter(tmp_path, monkeypatch):
    """Regression: sort_link() previously dropped the batch filter when
    building its column-header links, silently losing it on any sort."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_cards(session, 1, batch_code="A1")
        add_cards(session, 1, batch_code="B2")
    response = TestClient(main.app).get("/inventory?batch=A1")
    assert response.status_code == 200
    assert "batch=A1" in response.text


def test_explicit_filter_shows_results_heading_not_all_inventory(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_cards(session, 1, batch_code="A1")
    response = TestClient(main.app).get("/inventory?batch=A1")
    assert response.status_code == 200
    assert "<h2>\n            Results\n        </h2>" in response.text \
        or "Results" in response.text
    assert "<h2>\n            All Inventory\n        </h2>" not in response.text
