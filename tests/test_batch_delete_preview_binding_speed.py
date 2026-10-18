"""The delete-batch preview gathers bindings in ONE pass, and reports
exactly what it reported before.

THE PROBLEM, measured in production on 2026-10-11: the preview took about
158 seconds on a 374-card batch. It called bindings_backing_card once per
card, and that function re-reads and re-parses EVERY manapool binding
each time -- nearly 12,000 of them -- so the same scan ran 374 times.

The delete path had already solved this. Both now share
batch_delete_service.binding_membership_index.

★ THE OUTPUT MUST BE IDENTICAL, not merely equivalent. The rows are
sorted by set code and collector number, and two bindings can share both
(the same printing in another condition or finish), so insertion order is
what breaks those ties. These tests compare against the old per-card
algorithm row for row, in order.
"""
import json
import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import batch_delete_preview as bdp
import database
import main
from batch_delete_service import binding_membership_index
from manapool_quantity_push_service import bindings_backing_card
from models import Base, Batch, InventoryCard, RemoteProductBinding


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'preview-speed.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    return engine


def build(session, *, cards=12, bindings=60):
    """Cards and bindings that deliberately COLLIDE on set and collector
    number, so a tie-break by insertion order is actually exercised."""
    batch = Batch(batch_code="BIG", is_archived=False)
    session.add(batch)
    session.flush()
    made = []
    for i in range(cards):
        card = InventoryCard(
            batch_id=batch.id, name=f"Card {i}", status="available",
            condition="Near Mint", finish="normal", current_price=1.0,
            set_code=f"s{i % 3:02d}", collector_number=str(i % 2),
            scryfall_id=f"sf{i}", mtgjson_id=f"mtg{i}",
            language_id="EN", condition_id="NM", finish_id="NF",
        )
        session.add(card)
        made.append(card)
    session.flush()
    held = [c.id for c in made]
    for b in range(bindings):
        members = held[(b % 4)::5] if b % 3 == 0 else []
        session.add(RemoteProductBinding(
            provider="manapool", product_type="mtg_single", product_id=f"p{b}",
            local_card_ids_json=json.dumps(members),
            requested_identity_json=json.dumps({"name": f"Card {b % cards}"}),
            scryfall_id=f"sf{b % cards}", mtgjson_id=f"mtg{b % cards}",
            set_code=f"s{b % 3:02d}", collector_number=str(b % 2),
            language_id="EN", condition_id="NM" if b % 2 else "LP",
            finish_id="NF" if b % 3 else "FO",
            binding_status="validated",
            validated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            evidence_hash=f"h{b}", evidence_json="{}",
        ))
    session.commit()
    return made


def old_gather(session, cards):
    """The per-card algorithm this replaced, kept here as the oracle."""
    bindings = {}
    for card in cards:
        for binding in bindings_backing_card(session, card):
            bindings[binding.id] = binding
    return bindings


# --- identical output ----------------------------------------------------

def test_it_finds_exactly_the_same_bindings_in_the_same_order(db):
    with Session(db) as session:
        cards = build(session)
        old = old_gather(session, cards)
        new = bdp._bindings_backing_cards(session, cards)
    assert list(new.keys()) == list(old.keys()), (
        "same bindings AND the same insertion order -- the order breaks "
        "ties between rows sharing a set and collector number"
    )


def test_the_rendered_rows_are_identical(db):
    """Row for row, field for field, through the real _binding_rows."""
    with Session(db) as session:
        cards = build(session)
        new_rows = bdp._binding_rows(session, cards, [])

    with Session(db) as session:
        cards = session.query(InventoryCard).order_by(InventoryCard.id).all()
        bindings = old_gather(session, cards)
        live = bdp._live_quantities([])
        before = {bid: bdp._desired_quantity_for_binding(session, b)
                  for bid, b in bindings.items()}
        savepoint = session.begin_nested()
        try:
            for card in cards:
                session.delete(card)
            session.flush()
            after = {bid: bdp._desired_quantity_for_binding(session, b)
                     for bid, b in bindings.items()}
        finally:
            savepoint.rollback()
        old_rows = []
        for bid, binding in bindings.items():
            old_rows.append({
                "binding_id": bid, "product_id": binding.product_id,
                "name": bdp._binding_display_name(binding),
                "set_code": binding.set_code,
                "collector_number": binding.collector_number,
                "condition_id": binding.condition_id,
                "finish_id": binding.finish_id,
                "language_id": binding.language_id,
                "live_quantity": live.get(str(binding.product_id)),
                "quantity_before": before.get(bid), "quantity_after": after.get(bid),
            })
        old_rows.sort(key=lambda r: (r["set_code"] or "", r["collector_number"] or ""))

    assert new_rows == old_rows
    assert [r["binding_id"] for r in new_rows] == [r["binding_id"] for r in old_rows]
    assert sum(r["quantity_before"] or 0 for r in new_rows) == \
        sum(r["quantity_before"] or 0 for r in old_rows)
    assert sum(r["quantity_after"] or 0 for r in new_rows) == \
        sum(r["quantity_after"] or 0 for r in old_rows)


# --- the N+1 is gone -----------------------------------------------------

def count_binding_scans(engine, fn):
    """How many times the WHOLE binding table is read.

    A full pass filters on provider alone. The per-card identity lookup
    filters on provider AND binding status AND the identity columns, so
    the extra conditions are what tells the two apart -- that lookup is
    by design and the delete path makes the same one.
    """
    scans = []

    def record(conn, cursor, statement, parameters, context, executemany):
        text = " ".join(statement.lower().split())
        if "from remote_product_bindings" not in text or "where" not in text:
            return
        where = text.split("where", 1)[1]
        if " and " not in where:
            scans.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(scans)


def test_the_binding_table_is_read_once_not_once_per_card(db):
    """★ THE WHOLE POINT. One pass, however many cards there are."""
    with Session(db) as session:
        cards = build(session, cards=12)
        new_scans = count_binding_scans(
            db, lambda: bdp._bindings_backing_cards(session, cards))
        old_scans = count_binding_scans(
            db, lambda: old_gather(session, cards))

    assert new_scans == 1, f"expected a single pass, got {new_scans}"
    assert old_scans >= len(cards), (
        "the old algorithm should scan once per card -- if it does not, "
        "this test is no longer measuring the thing it was written for"
    )


def test_the_pass_does_not_grow_with_the_card_count(db):
    with Session(db) as session:
        cards = build(session, cards=30, bindings=80)
        scans = count_binding_scans(
            db, lambda: bdp._bindings_backing_cards(session, cards))
    assert scans == 1, f"30 cards should still be one pass, got {scans}"


# --- the shared index itself ---------------------------------------------

def test_the_shared_index_reads_every_binding_once(db):
    with Session(db) as session:
        build(session, cards=5, bindings=20)
        bindings, membership = binding_membership_index(session)
    assert len(bindings) == 20
    assert len(membership) == 20
    assert all(isinstance(v, list) for v in membership.values())


def test_an_unreadable_membership_list_is_logged_not_fatal(db, caplog):
    """A catch-and-continue, so it logs."""
    with Session(db) as session:
        build(session, cards=3, bindings=3)
        broken = session.query(RemoteProductBinding).first()
        broken.local_card_ids_json = "{not json"
        broken_id = broken.id
        session.commit()

    # the cardfoundry logger does not propagate, so attach the capture
    # handler to it directly rather than relying on the root logger
    app_logger = logging.getLogger("cardfoundry")
    app_logger.addHandler(caplog.handler)
    try:
        with Session(db) as session:
            with caplog.at_level(logging.WARNING, logger="cardfoundry"):
                bindings, membership = binding_membership_index(session)
    finally:
        app_logger.removeHandler(caplog.handler)

    assert len(bindings) == 3
    assert membership[broken_id] == []
    assert any("unreadable local_card_ids_json" in r.getMessage()
               for r in caplog.records), "a catch-and-continue must log"
