from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, SalesOrder


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'orders-pagination.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_orders(session, count, *, status="cancelled", prefix="order"):
    for index in range(count):
        session.add(SalesOrder(
            external_order_id=f"{prefix}-{index:04d}", status=status,
        ))
    session.commit()


def test_page_size_is_100(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_orders(session, 150)
    response = TestClient(main.app).get("/orders?status=cancelled")
    assert response.status_code == 200
    assert "Showing" in response.text
    assert "<strong>1&ndash;100</strong>" in response.text
    assert "<strong>150</strong>" in response.text


def test_page_2_shows_the_remainder(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_orders(session, 150)
    response = TestClient(main.app).get("/orders?status=cancelled&page=2")
    assert response.status_code == 200
    assert "<strong>101&ndash;150</strong>" in response.text
    assert "Page 2 of 2" in response.text


def test_no_pagination_controls_when_everything_fits_on_one_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_orders(session, 5)
    response = TestClient(main.app).get("/orders?status=cancelled")
    assert response.status_code == 200
    assert "Page 1 of 1" not in response.text
    assert "◀ Previous" not in response.text


def test_page_1_previous_is_not_a_link(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_orders(session, 150)
    response = TestClient(main.app).get("/orders?status=cancelled")
    assert response.status_code == 200
    assert '<span class="muted">◀ Previous</span>' in response.text
    assert 'page=2">Next' in response.text


def test_last_page_next_is_not_a_link(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_orders(session, 150)
    response = TestClient(main.app).get("/orders?status=cancelled&page=2")
    assert response.status_code == 200
    assert '<span class="muted">Next ▶</span>' in response.text
    assert 'page=1">◀ Previous' in response.text


def test_page_links_preserve_the_status_filter(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_orders(session, 150, status="shipped")
    response = TestClient(main.app).get("/orders?status=shipped")
    assert response.status_code == 200
    assert 'href="/orders?status=shipped&page=2"' in response.text


def test_out_of_range_page_clamps_to_last_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_orders(session, 150)
    response = TestClient(main.app).get("/orders?status=cancelled&page=99")
    assert response.status_code == 200
    assert "Page 2 of 2" in response.text


def test_page_zero_or_negative_clamps_to_page_one(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_orders(session, 150)
    response = TestClient(main.app).get("/orders?status=cancelled&page=0")
    assert response.status_code == 200
    assert "Page 1 of 2" in response.text


def test_status_all_is_also_paginated(tmp_path, monkeypatch):
    """The original unbounded page -- this is the exact scenario the
    audit flagged: production has 3,965 orders on status=all with no
    limit at all."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_orders(session, 250, status="cancelled")
    response = TestClient(main.app).get("/orders?status=all")
    assert response.status_code == 200
    assert "Page 1 of 3" in response.text
    assert response.text.count('<tr class="tracking-required">') + response.text.count("<tr>") <= 101  # header + <=100 rows


def test_a_non_all_status_filter_is_also_paginated(tmp_path, monkeypatch):
    """The audit asked whether any filter besides "all" is also
    unbounded -- confirmed yes (nothing limited the query before this
    fix, regardless of status), fixed uniformly for every filter."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_orders(session, 150, status="shipped")
    response = TestClient(main.app).get("/orders?status=shipped")
    assert response.status_code == 200
    assert "<strong>1&ndash;100</strong>" in response.text
    assert "<strong>150</strong>" in response.text
    assert "Page 1 of 2" in response.text


def test_priority_ordering_is_preserved_across_pages(tmp_path, monkeypatch):
    """Regression guard: pagination moved from a Python-level re-sort to
    a SQL-level ORDER BY case() -- confirm the same priority grouping
    (needs_review before short before ready_to_pick before ... before
    cancelled) still holds, now expressed in SQL."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_orders(session, 60, status="cancelled", prefix="cancelled")
        make_orders(session, 60, status="ready_to_pick", prefix="ready")
    response_page1 = TestClient(main.app).get("/orders?status=all&page=1")
    response_page2 = TestClient(main.app).get("/orders?status=all&page=2")
    # All 60 ready_to_pick orders (higher priority) must appear before
    # any cancelled order -- so page 1 (first 100) is all ready_to_pick
    # plus the first 40 cancelled; page 2 has the remaining 20 cancelled.
    assert "ready-0000" in response_page1.text
    assert "ready-0059" in response_page1.text
    # Each ready_to_pick order's identifier appears twice: once as the
    # visible order link text, once in its bulk-select checkbox's
    # aria-label (added by the item 22 accessibility pass).
    assert response_page1.text.count("ready-") == 60 * 2
    assert response_page2.text.count("ready-") == 0
    assert "cancelled-0000" in response_page2.text or "cancelled-0000" in response_page1.text


def test_select_all_ready_button_caps_its_claimed_count_at_page_size(tmp_path, monkeypatch):
    """Regression guard: clicking "Select all N Ready to Pick" navigates
    to page 1 of that filter and pre-checks whatever's rendered there --
    if N exceeds the page size, the button must not claim more than page
    1 will actually select."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_orders(session, 150, status="ready_to_pick", prefix="ready")
    response = TestClient(main.app).get("/orders")
    assert response.status_code == 200
    assert "Select all 100 Ready to Pick order(s)" in response.text
    assert "Select all 150 Ready to Pick order(s)" not in response.text
