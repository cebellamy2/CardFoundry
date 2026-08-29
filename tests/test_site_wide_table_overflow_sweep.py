"""Site-wide table overflow sweep, follow-up to UX/design-system epic
item 4.

Item 4 covered the six tables named in the original audit (Inventory
Search, Orders, Pick Waves, Pricing history, Inventory Sync history,
Consignors). Item 13 (Order Detail) found a real 462px overflow bug on
tables that were never on that list -- not deliberately deferred, just
missed, since item 4's scope came from what the audit happened to
notice rather than a systematic grep of every `<table` in the app.

This is that systematic sweep: every `<table` in main.py was mapped to
its route, and every one not already using `.data-table-scroll` was
measured with a real headless-Chromium render (Playwright) against
realistic long-value content (long double-faced-style card names, long
consignor/batch names, long free-text reasons) -- not estimated from
column counts. All 14 routes found to genuinely overflow (up to 504px
at 320px on Pick Wave detail alone) were fixed with the exact same
`.data-table-scroll` containment item 4 established -- no new pattern.

These tests check structure/markup only (same convention as
test_phase2_table_overflow.py) -- the real overflow measurement was
done live during development. Two known, out-of-scope residuals were
found and are NOT fixed here (this sweep's scope is table containment
only, not general page-level overflow): a native <select> listing long
consignor/batch names on Batch Detail and Batch Edit (not a table), and
a ~65px-at-320px-only residual on Pick Wave Detail traced to its
pre-existing (unmodified) closed <details> exception-report control,
consistent with this app's own documented <details>/shadow-DOM
rendering quirks. See the audit's own final report for both.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from consignor_auth_service import set_consignor_portal_credentials
from models import (
    Base, Batch, InventoryCard, InventoryChangeLog, ImportRecord,
    Consignor, ConsignorPayout, PickWave, PickWaveOrder, PickWaveEvent,
    SalesOrder, OrderItem, PickAllocation, FulfillmentException,
)


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'sweep.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def assert_scroll_wrapped(html, table_class_fragment='class="data-table'):
    assert '<div class="data-table-scroll">' in html or 'data-table-scroll no-print' in html
    assert table_class_fragment in html


# --- Consignors sub-pages -------------------------------------------------

def make_consignor_with_owed_card(session, *, name="Jane Doe"):
    batch = Batch(batch_code="CON1")
    session.add(batch)
    session.flush()
    consignor = Consignor(name=name, is_active=True)
    session.add(consignor)
    session.flush()
    batch.is_consignment = True
    batch.consignor_id = consignor.id
    card = InventoryCard(
        batch_id=batch.id, name="Sold Card", set_code="LEA", collector_number="1",
        status="sold", sold_price=100.0, consignment_amount_owed=90.0,
        consignment_payout_status="owed",
    )
    session.add(card)
    session.commit()
    return consignor, card


def test_consignor_edit_portal_mirror_tables_are_scroll_wrapped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        consignor, _ = make_consignor_with_owed_card(session)
        consignor_id = consignor.id
    response = TestClient(main.app).get(f"/consignors/{consignor_id}/edit")
    assert response.status_code == 200
    assert response.text.count('<div class="data-table-scroll">') == 2


def test_consignors_owed_report_is_scroll_wrapped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_consignor_with_owed_card(session)
    response = TestClient(main.app).get("/consignors/owed")
    assert response.status_code == 200
    assert_scroll_wrapped(response.text)


def test_consignor_pay_form_is_scroll_wrapped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        consignor, _ = make_consignor_with_owed_card(session)
        consignor_id = consignor.id
    response = TestClient(main.app).get(f"/consignors/{consignor_id}/pay")
    assert response.status_code == 200
    assert_scroll_wrapped(response.text)


def test_consignor_payout_preview_is_scroll_wrapped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        consignor, card = make_consignor_with_owed_card(session)
        consignor_id, card_id = consignor.id, card.id
    response = TestClient(main.app).post(
        f"/consignors/{consignor_id}/pay/preview",
        data={"card_ids": [str(card_id)], "method": "Cash", "note": "", "paid_at": "2026-08-01"},
    )
    assert response.status_code == 200
    assert_scroll_wrapped(response.text)


def test_consignor_payout_history_is_scroll_wrapped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        consignor, _ = make_consignor_with_owed_card(session)
        session.add(ConsignorPayout(consignor_id=consignor.id, amount=50.0, method="Cash"))
        session.commit()
        consignor_id = consignor.id
    response = TestClient(main.app).get(f"/consignors/{consignor_id}/payouts")
    assert response.status_code == 200
    assert_scroll_wrapped(response.text)


def test_consignor_portal_dashboard_is_scroll_wrapped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        consignor, _ = make_consignor_with_owed_card(session)
        set_consignor_portal_credentials(session, consignor.id, "jane@example.com", "password123")
        session.commit()
    client = TestClient(main.app)
    client.post("/portal/login", data={"username": "jane@example.com", "password": "password123"})
    response = client.get("/portal/")
    assert response.status_code == 200
    assert_scroll_wrapped(response.text)


# --- Batch detail / admin batches / archived batches ---------------------

def test_batch_detail_inventory_table_is_scroll_wrapped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = Batch(batch_code="A1")
        session.add(batch)
        session.flush()
        session.add(InventoryCard(
            batch_id=batch.id, name="Card", set_code="LEA", collector_number="1", status="available",
        ))
        session.commit()
        batch_id = batch.id
    response = TestClient(main.app).get(f"/batches/{batch_id}")
    assert response.status_code == 200
    assert_scroll_wrapped(response.text)


def test_admin_batches_table_is_scroll_wrapped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(Batch(batch_code="A1"))
        session.commit()
    response = TestClient(main.app).get("/admin/batches")
    assert response.status_code == 200
    assert_scroll_wrapped(response.text)


def test_archived_batches_table_is_scroll_wrapped_and_uses_shared_empty_class(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/batches/archived")
    assert response.status_code == 200
    assert_scroll_wrapped(response.text)
    assert 'class="data-table-empty"' in response.text


# --- Inventory card history / imports -------------------------------------

def test_card_history_table_is_scroll_wrapped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = Batch(batch_code="A1")
        session.add(batch)
        session.flush()
        card = InventoryCard(batch_id=batch.id, name="Card", status="available")
        session.add(card)
        session.flush()
        session.add(InventoryChangeLog(inventory_card_id=card.id, change_summary="Price changed"))
        session.commit()
        card_id = card.id
    response = TestClient(main.app).get(f"/inventory/{card_id}/history")
    assert response.status_code == 200
    assert_scroll_wrapped(response.text)


def test_import_history_table_is_scroll_wrapped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = Batch(batch_code="A1")
        session.add(batch)
        session.flush()
        session.add(ImportRecord(batch_id=batch.id, filename="x.csv", file_hash="h", card_count=1))
        session.commit()
    response = TestClient(main.app).get("/imports")
    assert response.status_code == 200
    assert_scroll_wrapped(response.text)


# --- Pick Wave detail: 4 tables -------------------------------------------

def make_wave_with_everything(session):
    batch = Batch(batch_code="A1")
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name="Card", set_code="LEA", collector_number="1",
        finish_id="NF", status="reserved",
    )
    session.add(card)
    session.flush()
    order = SalesOrder(external_order_id="o1", source="manapool", status="in_pick_wave")
    session.add(order)
    session.flush()
    item = OrderItem(order_id=order.id, name="Card", quantity=1)
    session.add(item)
    session.flush()
    allocation = PickAllocation(order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id, status="allocated")
    session.add(allocation)
    session.flush()
    wave = PickWave(label="Wave 1", status="active")
    session.add(wave)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
    session.add(PickWaveEvent(pick_wave_id=wave.id, event_type="reopened", note="test", evidence_json="{}"))
    session.add(FulfillmentException(
        sales_order_id=order.id, order_item_id=item.id, pick_allocation_id=allocation.id,
        inventory_card_id=card.id, exception_type="missing", submission_state="needs_submission",
        remote_resolution_state="awaiting", inventory_resolution_state="unresolved",
        note="test", remote_order_id=order.external_order_id,
    ))
    session.commit()
    return wave


def test_pick_wave_detail_all_four_tables_are_scroll_wrapped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave_with_everything(session)
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.status_code == 200
    # reopen history, picklist batch, exceptions, orders-in-wave
    assert response.text.count('<div class="data-table-scroll">') >= 3
    assert 'data-table-scroll no-print' in response.text


# --- Inventory Search decklist-mode results -------------------------------

def test_decklist_search_results_table_is_scroll_wrapped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = Batch(batch_code="A1")
        session.add(batch)
        session.flush()
        session.add(InventoryCard(
            batch_id=batch.id, name="Lightning Bolt", set_code="LEA", collector_number="1",
            finish_id="NF", status="available",
        ))
        session.commit()
    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "1 Lightning Bolt"},
    )
    assert response.status_code == 200
    assert_scroll_wrapped(response.text)


def test_decklist_not_found_section_is_scroll_wrapped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).post(
        "/inventory/decklist-search", data={"decklist": "1 Nonexistent Card Name"},
    )
    assert response.status_code == 200
    assert_scroll_wrapped(response.text)


# --- Inventory-sync exceptions: 4 tables ----------------------------------

def test_inventory_sync_exceptions_tables_are_scroll_wrapped(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory-sync/exceptions")
    assert response.status_code == 200
    assert response.text.count('<div class="data-table-scroll">') == 4


# --- Shipment sync issues --------------------------------------------------

def test_shipment_sync_issues_table_is_scroll_wrapped(tmp_path, monkeypatch):
    from datetime import datetime

    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(SalesOrder(
            external_order_id="stuck", source="manapool", status="shipped",
            tracking_number="1Z1", shipped_at=datetime(2026, 8, 15, 9, 0, 0),
            mana_pool_shipment_failure_detail="network down",
        ))
        session.commit()
    response = TestClient(main.app).get("/orders/shipment-sync-issues")
    assert response.status_code == 200
    assert_scroll_wrapped(response.text)


# --- Shared preview/correction detail tables (6 call sites, 1 helper) ----

def test_removal_preview_detail_table_is_scroll_wrapped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = Batch(batch_code="A1")
        session.add(batch)
        session.flush()
        card = InventoryCard(batch_id=batch.id, name="Card", status="available")
        session.add(card)
        session.commit()
        card_id = card.id
    response = TestClient(main.app).post(
        f"/inventory/{card_id}/removal/preview",
        data={"removal_reason": "personal_use", "removal_note": "test"},
    )
    assert response.status_code == 200
    assert_scroll_wrapped(response.text, table_class_fragment='class="data-table')
