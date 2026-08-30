import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import (
    Base, Batch, InventoryCard, OrderItem, PickAllocation, PickWave,
    PickWaveOrder, SalesOrder,
)


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


def make_order_with_active_allocation(session, *, source="manapool"):
    order = SalesOrder(
        external_order_id=f"order-{session.query(SalesOrder).count() + 1}",
        source=source, status="in_pick_wave",
    )
    session.add(order)
    session.flush()
    batch = Batch(batch_code=f"B-{order.id}")
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name="Alpha", mtgjson_id="MTG-ALPHA", language_id="EN",
        condition_id="LP", finish_id="NF", status="reserved",
    )
    session.add(card)
    session.flush()
    item = OrderItem(order_id=order.id, name="Alpha", quantity=1)
    session.add(item)
    session.flush()
    session.add(PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id, status="allocated",
    ))
    session.flush()
    return order


def make_active_wave(session, orders):
    wave = PickWave(label="Wave", status="active")
    session.add(wave)
    session.flush()
    for order in orders:
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
    session.flush()
    return wave


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
    checkbox_pattern = re.compile(
        rf'value="{ready_id}"\s+form="create-wave-form"\s+aria-label="[^"]*"\s*(checked)?\s*>'
    )

    filtered = client.get("/orders", params={"status": "needs_review"})
    assert filtered.status_code == 200
    assert checkbox_pattern.search(filtered.text) is None

    unchecked = client.get("/orders", params={"status": "ready_to_pick"})
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


def test_complete_wave_route_pushes_processing_for_manapool_orders(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_order_with_active_allocation(session, source="manapool")
        wave = make_active_wave(session, [order])
        session.commit()
        wave_id, order_id, external_id = wave.id, order.id, order.external_order_id

    calls = []

    def fake_update(order_id_arg, status, tracking_number=None, tracking_company=None, tracking_url=None):
        calls.append({"order_id": order_id_arg, "status": status})
        return {"fulfillment": {"status": "processing"}}

    monkeypatch.setattr(main, "update_seller_order_fulfillment", fake_update)

    client = TestClient(main.app)
    response = client.post(f"/pick-waves/{wave_id}/complete", follow_redirects=False)
    assert response.status_code == 303

    assert calls == [{"order_id": external_id, "status": "processing"}]
    with Session(db) as session:
        refreshed = session.get(SalesOrder, order_id)
        assert refreshed.status == "picked"
        assert refreshed.mana_pool_processing_synced_at is not None
        assert refreshed.mana_pool_processing_failure_detail is None


def test_complete_wave_route_skips_processing_push_for_non_manapool_orders(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_order_with_active_allocation(session, source="simulation")
        wave = make_active_wave(session, [order])
        session.commit()
        wave_id = wave.id

    calls = []
    monkeypatch.setattr(
        main, "update_seller_order_fulfillment", lambda *a, **k: calls.append(1),
    )

    client = TestClient(main.app)
    response = client.post(f"/pick-waves/{wave_id}/complete", follow_redirects=False)
    assert response.status_code == 303
    assert calls == []


def test_complete_wave_route_is_batch_isolated_across_orders(tmp_path, monkeypatch):
    """One order's push failure must not block another order's push in the
    same wave-completion request."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        failing_order = make_order_with_active_allocation(session, source="manapool")
        succeeding_order = make_order_with_active_allocation(session, source="manapool")
        wave = make_active_wave(session, [failing_order, succeeding_order])
        session.commit()
        wave_id = wave.id
        failing_external_id = failing_order.external_order_id
        failing_id, succeeding_id = failing_order.id, succeeding_order.id

    import httpx

    def flaky_update(order_id_arg, status, tracking_number=None, tracking_company=None, tracking_url=None):
        if order_id_arg == failing_external_id:
            raise httpx.HTTPError("network down")
        return {"fulfillment": {"status": "processing"}}

    monkeypatch.setattr(main, "update_seller_order_fulfillment", flaky_update)

    client = TestClient(main.app)
    response = client.post(f"/pick-waves/{wave_id}/complete", follow_redirects=False)
    assert response.status_code == 303

    with Session(db) as session:
        failing = session.get(SalesOrder, failing_id)
        succeeding = session.get(SalesOrder, succeeding_id)
        assert failing.status == "picked"
        assert failing.mana_pool_processing_synced_at is None
        assert failing.mana_pool_processing_failure_detail == "network down"
        assert succeeding.status == "picked"
        assert succeeding.mana_pool_processing_synced_at is not None


def test_retry_processing_sync_route_succeeds_and_clears_failure_detail(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_order_with_active_allocation(session, source="manapool")
        order.status = "picked"
        order.mana_pool_processing_failure_detail = "first failure"
        session.commit()
        order_id, external_id = order.id, order.external_order_id

    calls = []

    def fake_update(order_id_arg, status, tracking_number=None, tracking_company=None, tracking_url=None):
        calls.append(order_id_arg)
        return {"fulfillment": {"status": "processing"}}

    monkeypatch.setattr(main, "update_seller_order_fulfillment", fake_update)

    client = TestClient(main.app)
    response = client.post(
        f"/orders/{order_id}/retry-processing-sync", follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/orders/shipment-sync-issues"
    assert calls == [external_id]

    with Session(db) as session:
        refreshed = session.get(SalesOrder, order_id)
        assert refreshed.mana_pool_processing_synced_at is not None
        assert refreshed.mana_pool_processing_failure_detail is None


def test_retry_processing_sync_route_is_a_noop_for_an_already_synced_order(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_order_with_active_allocation(session, source="manapool")
        order.status = "picked"
        order.mana_pool_processing_synced_at = order.created_at
        session.commit()
        order_id = order.id

    calls = []
    monkeypatch.setattr(
        main, "update_seller_order_fulfillment", lambda *a, **k: calls.append(1),
    )

    client = TestClient(main.app)
    response = client.post(
        f"/orders/{order_id}/retry-processing-sync", follow_redirects=False,
    )
    assert response.status_code == 303
    assert calls == []


def test_shipment_sync_issues_page_lists_both_processing_and_shipped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        picked_order = make_order_with_active_allocation(session, source="manapool")
        picked_order.status = "picked"
        picked_order.mana_pool_processing_failure_detail = "processing push failed"
        shipped_order = SalesOrder(
            external_order_id="shipped-order", source="manapool", status="shipped",
        )
        session.add(shipped_order)
        session.flush()
        shipped_order.mana_pool_shipment_failure_detail = "shipped push failed"
        session.commit()

    client = TestClient(main.app)
    response = client.get("/orders/shipment-sync-issues")
    assert response.status_code == 200
    assert "processing push failed" in response.text
    assert "shipped push failed" in response.text
    assert "picked &rarr; processing" in response.text
    assert "packed &rarr; shipped" in response.text
    assert "retry-processing-sync" in response.text
    assert "retry-shipment-sync" in response.text


def test_banner_counts_processing_and_shipped_stuck_orders_together(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        picked_order = make_order_with_active_allocation(session, source="manapool")
        picked_order.status = "picked"
        session.commit()

    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert "1 order failed to sync to Mana Pool" in response.text
