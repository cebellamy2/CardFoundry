"""Expand all / Collapse all on the order page's batch sections.

★ WHY THE ORDER PAGE AND NOT ITS ITEMS TABLE. The operator asked for the
pair "every place the details appear". On /orders/{id} the items table is
NOT a disclosure -- it is always expanded, and has been since before any of
this -- so there is nothing there for a button to open. What DOES collapse
on that page is the per-batch pick list, rendered with the same
details.pick-batch sections the pick wave uses, and it had no way to open
them all at once: an operator working one order did by hand what the wave
page has done with a button since the item-15 redesign.

So the toolbar was extracted into pick_batch_toolbar and given to both,
rather than copied.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import (
    Base, Batch, ImportRecord, InventoryCard, OrderItem, PickAllocation,
    SalesOrder,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'order-toolbar.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def order_with_allocations(session, *, batch_codes=("A2", "A10")):
    order = SalesOrder(external_order_id="o-1", external_label="L-1",
                       source="manapool", status="ready_to_pick")
    session.add(order)
    session.flush()
    for n, code in enumerate(batch_codes):
        batch = session.query(Batch).filter_by(batch_code=code).one_or_none()
        if not batch:
            batch = Batch(batch_code=code)
            session.add(batch)
            session.flush()
        rec = ImportRecord(batch_id=batch.id, filename="x.csv",
                           file_hash=f"h-{code}", card_count=1)
        session.add(rec)
        session.flush()
        card = InventoryCard(
            batch_id=batch.id, import_id=rec.id, name=f"Brainstorm {n}",
            set_code="ICE", collector_number=str(n + 1),
            scryfall_id=f"sf-{n}", mtgjson_id=f"mtg-{n}", language_id="EN",
            condition_id="LP", finish_id="NF", condition="LP",
            finish="normal", status="reserved",
        )
        session.add(card)
        session.flush()
        item = OrderItem(
            order_id=order.id, name=card.name, set_code="ICE",
            collector_number=str(n + 1), finish="normal",
            scryfall_id=card.scryfall_id, language_id="EN",
            condition_id="LP", finish_id="NF", quantity=1, price_cents=150,
        )
        session.add(item)
        session.flush()
        session.add(PickAllocation(
            order_item_id=item.id, inventory_card_id=card.id,
            batch_id=batch.id, status="allocated",
        ))
    session.commit()
    return order


def test_the_order_page_offers_expand_and_collapse_all_batches(db):
    with Session(db) as session:
        order = order_with_allocations(session)
        order_id = order.id
    html = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "Expand all batches" in html
    assert "Collapse all batches" in html
    assert html.count("details.pick-batch") == 2        # one per button


def test_no_toolbar_when_the_order_has_no_allocations(db):
    """Nothing to control, so no control."""
    with Session(db) as session:
        order = SalesOrder(external_order_id="o-empty", source="manapool",
                           status="new")
        session.add(order)
        session.commit()
        order_id = order.id
    html = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "No inventory allocated yet." in html
    assert "Expand all batches" not in html


def test_the_items_table_is_still_always_expanded(db):
    """It is not a disclosure and never was -- the toolbar is for the batch
    sections, not for this."""
    with Session(db) as session:
        order = order_with_allocations(session)
        order_id = order.id
    html = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "<th>Requested</th>" in html
    assert "<th>Allocated</th>" in html
    assert "Brainstorm 0" in html


def test_the_wave_and_the_order_page_share_one_toolbar_helper():
    """One helper, so the two pages cannot drift into different wording or
    different selectors."""
    from main import PICK_BATCH_DISCLOSURE_CLASS, pick_batch_toolbar

    html = pick_batch_toolbar()
    assert f"details.{PICK_BATCH_DISCLOSURE_CLASS}" in html
    assert "d.open = true" in html and "d.open = false" in html
    assert html.count("onclick=") == 2
    assert "<script" not in html


def test_the_wave_picklist_tab_still_has_it(db):
    from models import PickWave, PickWaveOrder

    with Session(db) as session:
        order = order_with_allocations(session)
        wave = PickWave(label="Wave 1", status="active")
        session.add(wave)
        session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id))
        order.status = "in_pick_wave"
        session.commit()
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "Expand all batches" in html
    assert "Collapse all batches" in html
