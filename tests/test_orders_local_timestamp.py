from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import main
from models import SalesOrder
from tests.test_orders_page_ui import setup_db


def make_order(session, *, created_at=None):
    order = SalesOrder(
        external_order_id="mp-1", status="ready_to_pick",
        created_at=created_at or datetime(2026, 9, 9, 8, 31, 15),
    )
    session.add(order)
    session.flush()
    return order


def test_created_column_renders_utc_labeled_span_with_data_attribute(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, created_at=datetime(2026, 9, 9, 8, 31, 15))
        session.commit()

    response = TestClient(main.app).get("/orders")
    assert response.status_code == 200
    assert 'class="local-timestamp" data-utc="2026-09-09T08:31:15Z"' in response.text
    # Server-rendered fallback, honestly labeled UTC (this is what a
    # JS-disabled browser is left with -- must never look like local time).
    assert "Sep 9, 2026 8:31 AM UTC</span>" in response.text


def test_local_timestamp_script_present_exactly_once(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session)
        session.commit()

    response = TestClient(main.app).get("/orders")
    assert response.status_code == 200
    assert response.text.count("local-timestamp[data-utc]") == 1


def test_local_timestamp_script_not_present_on_unrelated_pages(tmp_path, monkeypatch):
    """Scoped to the Orders page's Created column only, per the ticket --
    not a sweep of every _format_timestamp() call site in the app."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session)
        session.commit()
        order_id = session.query(SalesOrder).one().id

    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert "local-timestamp" not in response.text
