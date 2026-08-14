import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, PickWaveOrder, SalesOrder


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'wave_routes.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_order(session, *, status="ready_to_pick"):
    order = SalesOrder(
        external_order_id=f"order-{session.query(SalesOrder).count() + 1}",
        status=status,
    )
    session.add(order)
    session.flush()
    return order


def test_orders_page_only_offers_checkboxes_for_ready_to_pick(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        ready = make_order(session)
        blocked = make_order(session, status="needs_review")
        session.commit()
        ready_id, blocked_id = ready.id, blocked.id

    client = TestClient(main.app)
    page = client.get("/orders")
    assert page.status_code == 200
    checkbox_pattern = re.compile(r'name="order_ids"\s+value="(\d+)"')
    checkbox_order_ids = {int(match) for match in checkbox_pattern.findall(page.text)}
    assert checkbox_order_ids == {ready_id}
    assert blocked_id not in checkbox_order_ids


def test_orders_page_status_filter_and_select_all(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        ready = make_order(session)
        make_order(session, status="needs_review")
        session.commit()
        ready_id = ready.id

    client = TestClient(main.app)
    filtered = client.get("/orders", params={"status": "needs_review"})
    assert filtered.status_code == 200
    assert f'value="{ready_id}"' not in filtered.text

    unchecked = client.get("/orders", params={"status": "ready_to_pick"})
    checkbox_pattern = re.compile(
        rf'value="{ready_id}"\s+form="create-wave-form"\s*(checked)?\s*>'
    )
    unchecked_match = checkbox_pattern.search(unchecked.text)
    assert unchecked_match and not unchecked_match.group(1)

    select_all = client.get(
        "/orders", params={"status": "ready_to_pick", "select_all_ready": "true"}
    )
    assert select_all.status_code == 200
    checked_match = checkbox_pattern.search(select_all.text)
    assert checked_match and checked_match.group(1) == "checked"


def test_create_wave_route_includes_only_selected_orders(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        selected = make_order(session)
        other_ready = make_order(session)
        session.commit()
        selected_id, other_id = selected.id, other_ready.id

    client = TestClient(main.app)
    response = client.post(
        "/pick-waves/create",
        data={"order_ids": [str(selected_id)], "label": "Route Wave"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db) as session:
        member_ids = {row.order_id for row in session.query(PickWaveOrder).all()}
        assert member_ids == {selected_id}
        assert session.get(SalesOrder, selected_id).status == "in_pick_wave"
        assert session.get(SalesOrder, other_id).status == "ready_to_pick"


def test_create_wave_route_fails_closed_on_ineligible_order(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        ready = make_order(session)
        not_ready = make_order(session, status="needs_review")
        session.commit()
        ready_id, not_ready_id = ready.id, not_ready.id

    client = TestClient(main.app)
    response = client.post(
        "/pick-waves/create",
        data={"order_ids": [str(ready_id), str(not_ready_id)]},
        follow_redirects=False,
    )
    assert response.status_code == 409

    with Session(db) as session:
        assert session.query(PickWaveOrder).count() == 0
        assert session.get(SalesOrder, ready_id).status == "ready_to_pick"


def test_remove_wave_order_route_returns_order_to_ready_to_pick(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_order(session)
        session.commit()
        order_id = order.id

    client = TestClient(main.app)
    created = client.post(
        "/pick-waves/create",
        data={"order_ids": [str(order_id)]},
        follow_redirects=False,
    )
    wave_id = int(created.headers["location"].rsplit("/", 1)[-1])

    removal = client.post(
        f"/pick-waves/{wave_id}/orders/{order_id}/remove",
        follow_redirects=False,
    )
    assert removal.status_code == 303

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "ready_to_pick"
        membership = session.query(PickWaveOrder).filter(PickWaveOrder.order_id == order_id).one()
        assert membership.status == "removed"

    wave_page = client.get(f"/pick-waves/{wave_id}")
    assert wave_page.status_code == 200
    assert f'/orders/{order_id}"' not in wave_page.text


def test_remove_wave_order_route_rejects_inactive_wave(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_order(session)
        session.commit()
        order_id = order.id

    client = TestClient(main.app)
    created = client.post(
        "/pick-waves/create",
        data={"order_ids": [str(order_id)]},
        follow_redirects=False,
    )
    wave_id = int(created.headers["location"].rsplit("/", 1)[-1])

    cancelled = client.post(f"/pick-waves/{wave_id}/cancel", follow_redirects=False)
    assert cancelled.status_code == 303

    removal = client.post(
        f"/pick-waves/{wave_id}/orders/{order_id}/remove",
        follow_redirects=False,
    )
    assert removal.status_code == 409
