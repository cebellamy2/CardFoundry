from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from fulfillment_exception_resolution_service import resolve_missing_inventory_exception
from fulfillment_exception_service import mark_fulfillment_exception
from fulfillment_exception_submission_service import confirm_fulfillment_exception_submitted
from models import Base
from tests.test_fulfillment_exception_service import seed


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'exception-search.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def test_exception_filter_finds_removed_missing_and_quarantined_mismatch(
    tmp_path, monkeypatch,
):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        missing_order, _, _, missing_allocation = seed(session)
        mismatch_order, _, _, mismatch_allocation = seed(session)
        mark_fulfillment_exception(session, missing_allocation.id, "missing", "Not found")
        mark_fulfillment_exception(
            session, mismatch_allocation.id, "inventory_mismatch", "Wrong printing",
        )
        session.commit()
        missing_label = missing_order.external_order_id
        mismatch_label = mismatch_order.external_order_id

    response = TestClient(main.app).get(
        "/inventory?exception_status=exception_unresolved"
    )
    assert response.status_code == 200
    assert response.text.count("EXCEPTION UNRESOLVED") == 2
    assert "missing" in response.text
    assert "inventory_mismatch" in response.text
    assert missing_label in response.text
    assert mismatch_label in response.text

    with Session(db) as session:
        rows = session.query(main.InventoryCard).filter(
            main.InventoryCard.inventory_exception_state == "exception_unresolved"
        ).all()
        assert {row.status for row in rows} == {"removed", "unsellable"}


@pytest.mark.parametrize(
    "remote_state",
    ["awaiting", "resolved_refunded", "resolved_replaced", "review_required"],
)
def test_exception_filter_is_independent_of_submission_and_remote_resolution(
    tmp_path, monkeypatch,
    remote_state,
):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _, _, allocation = seed(session)
        exception = mark_fulfillment_exception(
            session, allocation.id, "missing", "Not found",
        )
        session.commit()
        exception = confirm_fulfillment_exception_submitted(
            session, exception.id, "Submitted manually",
        )
        exception.remote_resolution_state = remote_state
        session.commit()

    response = TestClient(main.app).get(
        "/inventory?exception_status=exception_unresolved"
    )
    assert response.status_code == 200
    assert "EXCEPTION UNRESOLVED" in response.text
    assert "Type: missing" in response.text


def test_resolved_projection_disappears_and_status_combines_deterministically(
    tmp_path, monkeypatch,
):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        missing_order, _, _, missing_allocation = seed(session)
        mismatch_order, _, _, mismatch_allocation = seed(session)
        missing_exception = mark_fulfillment_exception(
            session, missing_allocation.id, "missing", "Not found",
        )
        mark_fulfillment_exception(
            session, mismatch_allocation.id, "inventory_mismatch", "Wrong set",
        )
        resolve_missing_inventory_exception(
            session, missing_exception.id, "Confirmed absent",
        )
        session.commit()

    client = TestClient(main.app)
    unresolved = client.get("/inventory?exception_status=exception_unresolved")
    assert unresolved.status_code == 200
    assert "EXCEPTION UNRESOLVED" in unresolved.text
    assert "Type: missing" not in unresolved.text
    removed_only = client.get(
        "/inventory?exception_status=exception_unresolved&status=removed"
    )
    assert "EXCEPTION UNRESOLVED" not in removed_only.text
    assert "No cards found." in removed_only.text
    unsellable_only = client.get(
        "/inventory?exception_status=exception_unresolved&status=unsellable"
    )
    assert "Showing" in unsellable_only.text
    assert "EXCEPTION UNRESOLVED" in unsellable_only.text


def test_invalid_exception_filter_falls_back_without_changing_normal_search(
    tmp_path, monkeypatch,
):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _, _, card, _ = seed(session, card_status="available")
        available_id = card.id
        session.commit()

    client = TestClient(main.app)
    normal = client.get("/inventory?status=available")
    invalid = client.get(
        "/inventory?status=available&exception_status=not-a-state"
    )
    assert normal.status_code == invalid.status_code == 200
    assert f"Showing" in normal.text
    assert f"{available_id}" in normal.text
    assert "Exception unresolved" in invalid.text
    assert "EXCEPTION UNRESOLVED" not in invalid.text


def test_exception_filter_does_not_change_sellability_or_quantity(
    tmp_path, monkeypatch,
):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _, item, card, allocation = seed(session)
        mark_fulfillment_exception(session, allocation.id, "inventory_mismatch", "Wrong card")
        session.commit()
        quantity = item.quantity
        status = card.status

    response = TestClient(main.app).get(
        "/inventory?exception_status=exception_unresolved"
    )
    assert response.status_code == 200
    with Session(db) as session:
        card = session.get(type(card), card.id)
        item = session.get(type(item), item.id)
        assert card.status == status == "unsellable"
        assert item.quantity == quantity
        assert session.query(main.PickAllocation).count() == 1


def test_exception_details_are_loaded_with_search_query_not_n_plus_one(
    tmp_path, monkeypatch,
):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _, _, _, allocation = seed(session)
        mark_fulfillment_exception(session, allocation.id, "missing", "Not found")
        session.commit()

    statements = []

    def count_statement(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(db, "before_cursor_execute", count_statement)
    try:
        response = TestClient(main.app).get(
            "/inventory?exception_status=exception_unresolved"
        )
    finally:
        event.remove(db, "before_cursor_execute", count_statement)
    assert response.status_code == 200
    # 1 query for the exception-aware card search itself (what this test
    # guards against N+1 on) + 2 for page_start()'s own Mana Pool
    # sync-issue banner check (one per stuck-sync kind: shipped and
    # processing), which every page now issues by design -- not a search
    # N+1.
    assert len(statements) == 3
