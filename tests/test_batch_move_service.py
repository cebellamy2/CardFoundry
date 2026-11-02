"""batch_move_service: bulk-move cards between batches, available-only.

WHY. The rule is all-or-nothing: consignment status lives at the batch
level, so moving a sold card would retroactively shift which consignor a
past sale is attributed to, and moving a reserved one would pull it out
from under an order. One ineligible card blocks the whole move.

The logic was lifted out of main.py's bulk_move_cards_to_batch with no
behaviour change, so every rule is asserted twice: once against the
service directly, once through the real route and the page the operator
reads. tests/test_bulk_card_actions.py still covers the route unchanged.
"""
import re
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import batch_move_service
import inventory_sync_service
import main
from models import (Base, Batch, InventoryCard, InventoryChangeLog, OrderItem,
                    SalesOrder)
from order_service import allocate_order

KEY = ("MTG-ONE-1", "EN", "LP", "NF")
ROUTE = "/inventory-cards/bulk-move-batch"
BLOCKED_WARNING = (
    '<div class="warning">Only available cards can be bulk-moved between '
    "batches. The following selected cards are not eligible:<ul>"
)


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'batch-move.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_batch(db, code, *, is_archived=False):
    with Session(db) as session:
        batch = Batch(batch_code=code, is_archived=is_archived)
        session.add(batch)
        session.commit()
        return batch.id


def make_card(db, batch_id, name, **overrides):
    with Session(db) as session:
        values = dict(
            batch_id=batch_id, name=name, set_code="ONE",
            collector_number="1", mtgjson_id=KEY[0], language_id=KEY[1],
            condition_id=KEY[2], finish_id=KEY[3], status="available",
            imported_at=datetime.now(),
        )
        values.update(overrides)
        card = InventoryCard(**values)
        session.add(card)
        session.commit()
        return card.id


def reserve_card(db, card_id):
    """Reserve through the real allocator, so the card is in exactly the
    shape an order puts it in. The card must be the only available one
    matching KEY for allocate_order to pick it."""
    with Session(db) as session:
        name = session.get(InventoryCard, card_id).name
        order = SalesOrder(external_order_id=f"ord-{card_id}", status="short")
        session.add(order)
        session.flush()
        session.add(OrderItem(
            order_id=order.id, name=name, mtgjson_id=KEY[0],
            language_id=KEY[1], condition_id=KEY[2], finish_id=KEY[3],
            quantity=1, set_code="ONE", collector_number="1",
        ))
        session.flush()
        allocate_order(session, order)
        session.commit()
        assert session.get(InventoryCard, card_id).status == "reserved"


def batch_ids_and_logs(db, card_ids):
    with Session(db) as session:
        batches = [session.get(InventoryCard, cid).batch_id for cid in card_ids]
        logs = session.query(InventoryChangeLog).order_by(InventoryChangeLog.id).all()
        return batches, [(log.inventory_card_id, log.actor, log.change_summary) for log in logs]


# --- a sold or reserved card blocks the whole move ------------------------

def test_service_sold_card_blocks_everything_and_writes_nothing(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    source, target = make_batch(db, "SOURCE"), make_batch(db, "TARGET")
    ok = make_card(db, source, "Alpha", mtgjson_id="MTG-A")
    sold = make_card(db, source, "Beta", mtgjson_id="MTG-B", status="sold")

    with Session(db) as session:
        outcome = batch_move_service.move_cards_to_batch(
            session, [ok, sold], target, actor="operator:test",
        )
        assert outcome.status == batch_move_service.BLOCKED
        assert [c.id for c in outcome.non_available] == [sold]
        assert outcome.missing_ids == []
        assert outcome.results == []

    assert batch_ids_and_logs(db, [ok, sold]) == ([source, source], [])


def test_service_reserved_card_blocks_everything(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    source, target = make_batch(db, "SOURCE"), make_batch(db, "TARGET")
    reserved = make_card(db, source, "Reserved")
    reserve_card(db, reserved)
    ok = make_card(db, source, "Alpha", mtgjson_id="MTG-A")

    with Session(db) as session:
        outcome = batch_move_service.move_cards_to_batch(
            session, [ok, reserved], target, actor="operator:test",
        )
        assert outcome.status == batch_move_service.BLOCKED
        assert [c.id for c in outcome.non_available] == [reserved]

    assert batch_ids_and_logs(db, [ok, reserved]) == ([source, source], [])


def test_service_missing_card_id_blocks_everything(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    source, target = make_batch(db, "SOURCE"), make_batch(db, "TARGET")
    ok = make_card(db, source, "Alpha")

    with Session(db) as session:
        outcome = batch_move_service.move_cards_to_batch(
            session, [ok, 99999], target, actor="operator:test",
        )
        assert outcome.status == batch_move_service.BLOCKED
        assert outcome.missing_ids == [99999]

    assert batch_ids_and_logs(db, [ok]) == ([source], [])


def test_route_reserved_and_sold_cards_block_with_the_exact_warning(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    source, target = make_batch(db, "SOURCE"), make_batch(db, "TARGET")
    reserved = make_card(db, source, "ReservedCard")
    reserve_card(db, reserved)
    sold = make_card(db, source, "SoldCard", mtgjson_id="MTG-S", status="sold")
    ok = make_card(db, source, "AvailableCard", mtgjson_id="MTG-A")

    response = TestClient(main.app).post(ROUTE, data={
        "card_ids": [ok, reserved, sold, 99999], "target_batch_id": str(target),
    })

    assert response.status_code == 409
    assert re.search(r"<title>\s*Move Blocked\s*</title>", response.text)
    assert "<h1>Move blocked.</h1>" in response.text
    assert BLOCKED_WARNING in response.text
    assert "<li>ReservedCard (" in response.text
    assert "<li>SoldCard (" in response.text
    assert "<li>Card #99999 not found</li>" in response.text
    # The available card is not named: it is not the problem.
    assert "<li>AvailableCard" not in response.text
    assert batch_ids_and_logs(db, [ok, reserved, sold]) == ([source] * 3, [])


# --- target validation ----------------------------------------------------

def test_service_refuses_archived_and_missing_targets(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    source, archived = make_batch(db, "SOURCE"), make_batch(db, "OLD", is_archived=True)
    ok = make_card(db, source, "Alpha")

    with Session(db) as session:
        assert batch_move_service.move_cards_to_batch(
            session, [ok], archived, actor="operator:test",
        ).status == batch_move_service.TARGET_ARCHIVED
        assert batch_move_service.move_cards_to_batch(
            session, [ok], 99999, actor="operator:test",
        ).status == batch_move_service.TARGET_NOT_FOUND

    assert batch_ids_and_logs(db, [ok]) == ([source], [])


def test_route_refuses_archived_target(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    source, archived = make_batch(db, "SOURCE"), make_batch(db, "OLD", is_archived=True)
    ok = make_card(db, source, "Alpha")

    response = TestClient(main.app).post(ROUTE, data={
        "card_ids": [ok], "target_batch_id": str(archived),
    })

    assert response.status_code == 400
    assert "<h1>Cannot move cards into an archived batch.</h1>" in response.text
    assert batch_ids_and_logs(db, [ok]) == ([source], [])


def test_route_refuses_missing_target(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    ok = make_card(db, make_batch(db, "SOURCE"), "Alpha")

    response = TestClient(main.app).post(ROUTE, data={
        "card_ids": [ok], "target_batch_id": "99999",
    })

    assert response.status_code == 400
    assert "<h1>Target batch not found.</h1>" in response.text


# --- available cards move, one audit row each -----------------------------

def test_service_moves_available_cards_with_one_log_row_each(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    source, target = make_batch(db, "SOURCE"), make_batch(db, "TARGET")
    a = make_card(db, source, "Alpha", mtgjson_id="MTG-A")
    b = make_card(db, source, "Beta", mtgjson_id="MTG-B")

    with Session(db) as session:
        outcome = batch_move_service.move_cards_to_batch(
            # A repeated id is one card, moved once and logged once.
            session, [a, b, a], target, actor="operator:test",
        )
        assert outcome.status == batch_move_service.MOVED
        assert outcome.results == [
            {"outcome": "moved", "name": "Alpha", "reason": "SOURCE → TARGET"},
            {"outcome": "moved", "name": "Beta", "reason": "SOURCE → TARGET"},
        ]

    summary = "batch: 'SOURCE' -> 'TARGET' (bulk move)"
    assert batch_ids_and_logs(db, [a, b]) == (
        [target, target],
        [(a, "operator:test", summary), (b, "operator:test", summary)],
    )


def test_route_moves_available_cards_and_shows_results(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    source, target = make_batch(db, "SOURCE"), make_batch(db, "TARGET")
    a = make_card(db, source, "Alpha", mtgjson_id="MTG-A")
    b = make_card(db, source, "Beta", mtgjson_id="MTG-B")

    response = TestClient(main.app).post(ROUTE, data={
        "card_ids": [a, b], "target_batch_id": str(target),
    })

    assert response.status_code == 200
    assert "Bulk Move Results" in response.text
    assert response.text.count("<td>SOURCE → TARGET</td>") == 2
    batches, logs = batch_ids_and_logs(db, [a, b])
    assert batches == [target, target]
    assert [(cid, s) for cid, _actor, s in logs] == [
        (a, "batch: 'SOURCE' -> 'TARGET' (bulk move)"),
        (b, "batch: 'SOURCE' -> 'TARGET' (bulk move)"),
    ]
    # The route attributes the write to whoever is acting, never nothing.
    assert all(actor for _cid, actor, _s in logs)


# --- already in the target: unchanged, no audit row -----------------------

def test_service_card_already_in_target_is_unchanged_with_no_log(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    source, target = make_batch(db, "SOURCE"), make_batch(db, "TARGET")
    here = make_card(db, target, "Here", mtgjson_id="MTG-H")
    moving = make_card(db, source, "Moving", mtgjson_id="MTG-M")

    with Session(db) as session:
        outcome = batch_move_service.move_cards_to_batch(
            session, [here, moving], target, actor="operator:test",
        )
        assert outcome.results[0] == {
            "outcome": "unchanged", "name": "Here", "reason": "Already in this batch.",
        }
        assert outcome.results[1]["outcome"] == "moved"

    batches, logs = batch_ids_and_logs(db, [here, moving])
    assert batches == [target, target]
    assert [cid for cid, _a, _s in logs] == [moving]


def test_route_card_already_in_target_is_reported_unchanged(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    target = make_batch(db, "TARGET")
    here = make_card(db, target, "Here")

    response = TestClient(main.app).post(ROUTE, data={
        "card_ids": [here], "target_batch_id": str(target),
    })

    assert response.status_code == 200
    assert "<td>Already in this batch.</td>" in response.text
    assert batch_ids_and_logs(db, [here]) == ([target], [])
