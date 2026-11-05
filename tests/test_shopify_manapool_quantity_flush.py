"""Ticket 14: a cancelled Shopify order RAISES Mana Pool, and every Mana
Pool quantity write a Shopify order causes is made after the commit.

Two gaps, found by ticket 12's production preview and made real by the
operator turning the push gate on at 2026-11-05 07:33 ET:

  A. release_order never contacted Mana Pool at all. With the gate on, a
     Shopify order lowered Mana Pool in seconds and a cancellation left
     the card advertised at ZERO until a Perform Sync reconciliation
     happened to pick the gap up -- up to eight hours, and only when the
     gap was batch-traceable. The same asymmetry v1.184.0 removed from
     every other return-to-sellable path in the app.
  B. The lower was written from INSIDE allocate_order, before
     ingest_shopify_orders committed -- and that function rolls back per
     refused order, so a write could describe a reservation that never
     happened.

What these pin, in the order that matters:

  1. NOTHING IS WRITTEN FROM AN UNCOMMITTED TRANSACTION, in either
     direction. Proved twice over: the note is discarded on rollback,
     AND the write is made from a session of its own that cannot see
     uncommitted rows -- asserted by reading the card's committed status
     from inside the Mana Pool write itself.
  2. THE REAL ROUTES. Driven through POST /orders/{id}/cancel and
     /orders/{id}/uncancel on an order built by the real Shopify
     ingest, asserting the body actually sent to Mana Pool.
  3. ONE GATE STOPS ALL THREE DIRECTIONS.
  4. THE COLLISION IS UNCHANGED: Mana Pool hears exactly one message,
     and only when Shopify wins the card.
  5. A MANA POOL FAILURE NEVER FAILS THE LOCAL TRANSITION, and leaves
     its detail on the binding where the sync-issues page reads it.
  6. The cancel route's statement count is pinned with the gate ON,
     because the flush opens a session of its own.

No test touches the network: update_inventory_prices_by_product is
replaced by a recorder in every test, and the Shopify quantity sync
stays off, so nothing here reaches a live endpoint.
"""
import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import inventory_sync_service
import main
import manapool_quantity_push_service as mp
import order_service
import shopify_order_service as intake
import shopify_service
from local_price_writeback_service import set_card_price
from models import (
    Base, Batch, ImportRecord, InventoryCard, OrderItem, PickAllocation,
    RemoteProductBinding, SalesOrder,
)
from shopify_binding_service import record_binding

ALLOWED = "card-foundry-dev.myshopify.com"
SKU = "CF-NEO-1-EN-NM-NF"
ORDER_NUMERIC_ID = "7271829405761"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mp-flush.db'}")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def wired(monkeypatch):
    """Shopify configured but its quantity sync left OFF, so the only
    channel these tests exercise is Mana Pool."""
    monkeypatch.setattr(shopify_service, "SHOPIFY_STORE_DOMAIN", ALLOWED)
    monkeypatch.setattr(shopify_service, "SHOPIFY_LOCATION_ID", "97412055105")
    monkeypatch.setattr(shopify_service, "SHOPIFY_CLIENT_ID", "id")
    monkeypatch.setattr(shopify_service, "SHOPIFY_CLIENT_SECRET", "secret")
    monkeypatch.setattr(shopify_service, "SHOPIFY_ACCESS_TOKEN", None)
    monkeypatch.setattr(shopify_service, "location_name",
                        lambda gid, client=None: "Shop location")
    return ALLOWED


class ManaPool:
    """Mana Pool's side of the write: every body recorded, and the
    committed state at the moment of the write recorded with it.

    ★ `seen_status` IS THE WHOLE POINT OF FIX B. Each write opens a
    session of its own on the same engine and reads the card back. A
    write made from inside the caller's transaction sees the card as the
    transaction left it; a write made after the commit sees what the
    database actually holds. The difference is not something this can be
    argued about -- it is recorded per call.
    """

    def __init__(self, engine, card_id=None):
        self.engine = engine
        self.card_id = card_id
        self.calls = []
        self.seen_status = []
        self.explode = None

    def install(self, monkeypatch):
        monkeypatch.setattr(mp, "update_inventory_prices_by_product", self.write)
        return self

    def write(self, updates):
        self.calls.append([dict(row) for row in updates])
        if self.card_id is not None:
            with Session(self.engine) as fresh:
                card = fresh.get(InventoryCard, self.card_id)
                self.seen_status.append(card.status if card else None)
        if self.explode is not None:
            raise self.explode
        return [{"updated": len(updates)}]

    @property
    def quantities(self):
        """Every (product_id, quantity) written, call order preserved."""
        return [(row["product_id"], row["quantity"])
                for call in self.calls for row in call]


def make_card(session, *, n=1, code="A1", price_cents=65, **over):
    batch = session.query(Batch).filter_by(batch_code=code).first()
    if batch is None:
        batch = Batch(batch_code=code)
        session.add(batch)
        session.flush()
    record = session.query(ImportRecord).filter_by(batch_id=batch.id).first()
    if record is None:
        record = ImportRecord(batch_id=batch.id, filename="x.csv",
                              file_hash=f"h{batch.id}", card_count=1)
        session.add(record)
        session.flush()
    values = {
        "batch_id": batch.id, "import_id": record.id, "name": f"Card {n}",
        "set_code": "NEO", "collector_number": str(n), "scryfall_id": f"sf-{n}",
        "mtgjson_id": f"mtg-{n}", "language_id": "EN", "condition_id": "NM",
        "finish_id": "NF", "condition": "NM", "finish": "normal",
        "status": "available",
    }
    values.update(over)
    card = InventoryCard(**values)
    session.add(card)
    session.flush()
    if price_cents is not None:
        set_card_price(session, card, price_cents,
                       source="seller_inventory_scan", note="fixture")
        session.flush()
    return card


def shopify_bind(session, card):
    """The Shopify binding, so the real ingest can resolve the line's SKU."""
    return record_binding(
        session, sku=f"CF-NEO-{card.collector_number}-EN-NM-NF",
        set_code=card.set_code, collector_number=card.collector_number,
        language_id=card.language_id, condition_id=card.condition_id,
        finish_id=card.finish_id, mtgjson_id=card.mtgjson_id,
        product_gid="gid://shopify/Product/1",
        variant_gid="gid://shopify/ProductVariant/1",
        inventory_item_gid="gid://shopify/InventoryItem/1",
    )


def manapool_bind(session, card, *, binding_id=1, product_id=None):
    """A validated Mana Pool binding for this card's identity.

    ★ HAND-BUILT, which CLAUDE.md asks tests not to do. There is no one
    service that creates a RemoteProductBinding -- six call sites each
    build their own row -- so there is nothing canonical to build it
    through. Same shape test_return_to_sellable_push.py already uses, and
    the fields that matter here are the four identity columns the real
    resolution matches on.
    """
    binding = RemoteProductBinding(
        id=binding_id, provider="manapool", product_type="mtg_single",
        product_id=product_id or f"product-{binding_id}",
        local_card_ids_json=json.dumps([card.id]),
        requested_identity_json="{}", scryfall_id=card.scryfall_id,
        set_code=card.set_code, collector_number=card.collector_number,
        mtgjson_id=card.mtgjson_id, language_id=card.language_id,
        condition_id=card.condition_id, finish_id=card.finish_id,
        binding_status="validated", validated_at=datetime(2026, 9, 1),
        evidence_hash=f"h{binding_id}", evidence_json="{}",
    )
    session.add(binding)
    session.flush()
    return binding


def webhook_body(*, order_id=ORDER_NUMERIC_ID, name="#1001", sku=SKU):
    return {
        "id": int(order_id), "name": name,
        "created_at": "2026-11-03T16:25:49Z",
        "line_items": [{"id": 1, "sku": sku, "quantity": 1,
                        "name": "Card 1", "price": "0.65"}],
        "total_shipping_price_set": {
            "shop_money": {"amount": "8.00", "currency_code": "USD"}},
        "total_price_set": {
            "shop_money": {"amount": "8.65", "currency_code": "USD"}},
    }


def reserved_order(session, *, name="#1001", order_id=ORDER_NUMERIC_ID,
                   sku=SKU):
    """One Shopify order holding one card, built by the REAL ingest."""
    intake.set_intake_enabled(session, True)
    outcome = intake.ingest_one(session, webhook_body(order_id=order_id,
                                                      name=name, sku=sku),
                                initiated_by="shopify-webhook")
    assert outcome["outcome"] == "ingested", outcome
    session.commit()
    return session.query(SalesOrder).filter_by(
        external_order_id=intake.canonical_external_order_id(order_id)).one()


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(order_service, "engine", db, raising=False)
    monkeypatch.setattr(inventory_sync_service, "engine", db, raising=False)
    return TestClient(main.app)


def _count_statements(engine, action):
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "after_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "after_cursor_execute", record)
    return seen


@pytest.fixture
def logs(caplog):
    """The cardfoundry logger does not propagate, so caplog needs
    attaching to it by hand."""
    import logging
    logger = logging.getLogger("cardfoundry")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="cardfoundry")
    yield caplog
    logger.removeHandler(caplog.handler)


# --- 1. nothing is written from an uncommitted transaction ---------------


def test_a_rolled_back_release_tells_manapool_nothing(db, wired, monkeypatch):
    """★ FIX A'S SAFETY PROPERTY. A release that never commits gave no
    stock back, so there is nothing to advertise."""
    manapool = ManaPool(db).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        shopify_bind(session, card)
        manapool_bind(session, card)
        intake.set_manapool_push_enabled(session, True)
        order = reserved_order(session)
        # reserved_order commits, so the LOWER has already been flushed.
        # What this test is about is everything after that point.
        assert manapool.quantities == [("product-1", 0)]

        order_service.release_order(session, order)
        assert mp.noted_quantity_pushes(session)  # noted, not yet written
        session.rollback()

    assert manapool.quantities == [("product-1", 0)]
    with Session(db) as session:
        assert session.get(InventoryCard, 1).status == "reserved"


def test_a_rolled_back_allocation_tells_manapool_nothing(db, wired,
                                                          monkeypatch):
    """★ FIX B. ingest_shopify_orders rolls back per refused order, and
    before v2.112.0 the lower was written from inside that transaction --
    so a refused order could tell Mana Pool a card was gone and then
    keep it on the shelf."""
    manapool = ManaPool(db).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        shopify_bind(session, card)
        manapool_bind(session, card)
        intake.set_intake_enabled(session, True)
        intake.set_manapool_push_enabled(session, True)
        session.commit()

        order = SalesOrder(external_order_id="shopify-x", external_label="#x",
                           source="shopify", status="pending")
        session.add(order)
        session.flush()
        session.add(OrderItem(
            order_id=order.id, name=card.name, set_code=card.set_code,
            collector_number=card.collector_number, quantity=1,
            scryfall_id=card.scryfall_id, mtgjson_id=card.mtgjson_id,
            language_id=card.language_id, condition_id=card.condition_id,
            finish_id=card.finish_id, price_cents=65))
        session.flush()
        order_service.allocate_order(session, order)
        assert mp.noted_quantity_pushes(session)
        session.rollback()

    assert manapool.calls == []
    with Session(db) as session:
        assert session.get(InventoryCard, 1).status == "available"


def test_one_refused_order_in_a_batch_does_not_write_for_the_other(
        db, wired, monkeypatch):
    """The real per-order isolation, end to end: one order is refused and
    rolled back, the other is ingested and committed, and Mana Pool hears
    about exactly one card."""
    manapool = ManaPool(db).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session, n=1)
        shopify_bind(session, card)
        manapool_bind(session, card)
        intake.set_intake_enabled(session, True)
        intake.set_manapool_push_enabled(session, True)
        session.commit()
        result = intake.ingest_shopify_orders(session, [
            {"id": "gid://shopify/Order/1", "name": "#bad",
             "createdAt": "2026-11-03T10:00:00Z",
             "updatedAt": "2026-11-03T10:00:00Z", "cancelledAt": None,
             "email": "b@example.com",
             "lineItems": {"edges": [{"node": {
                 "id": "gid://shopify/LineItem/1", "sku": "CF-NOTHING",
                 "quantity": 1, "name": "nope",
                 "discountedUnitPriceSet": {
                     "shopMoney": {"amount": "1.00", "currencyCode": "USD"}},
                 "originalUnitPriceSet": {
                     "shopMoney": {"amount": "1.00", "currencyCode": "USD"}},
             }}]}},
            {"id": "gid://shopify/Order/2", "name": "#good",
             "createdAt": "2026-11-03T10:00:00Z",
             "updatedAt": "2026-11-03T10:00:00Z", "cancelledAt": None,
             "email": "b@example.com",
             "lineItems": {"edges": [{"node": {
                 "id": "gid://shopify/LineItem/1", "sku": SKU,
                 "quantity": 1, "name": "Card 1",
                 "discountedUnitPriceSet": {
                     "shopMoney": {"amount": "0.65", "currencyCode": "USD"}},
                 "originalUnitPriceSet": {
                     "shopMoney": {"amount": "0.65", "currencyCode": "USD"}},
             }}]}},
        ])
    assert len(result["refused"]) == 1 and len(result["ingested"]) == 1
    assert manapool.quantities == [("product-1", 0)]


def test_the_write_is_made_from_committed_state(db, wired, monkeypatch):
    """★ THE PROPERTY THAT MATTERS MOST, and the one a discarded note
    alone does not give you. The write reads the card back in a session
    of its own: it sees `reserved` for an allocation and `available` for
    a release, which is only possible after each commit."""
    manapool = ManaPool(db, card_id=1).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        shopify_bind(session, card)
        manapool_bind(session, card)
        intake.set_manapool_push_enabled(session, True)
        order = reserved_order(session)          # commits -> lower flushes
        order_service.release_order(session, order)
        session.commit()                          # -> raise flushes

    assert manapool.seen_status == ["reserved", "available"]
    assert manapool.quantities == [("product-1", 0), ("product-1", 1)]


# --- 2. the real routes --------------------------------------------------


def test_the_cancel_route_raises_manapool(db, wired, client, monkeypatch,
                                          logs):
    """★ GAP A, CLOSED, THROUGH THE ROUTE THE OPERATOR CLICKS."""
    manapool = ManaPool(db, card_id=1).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        shopify_bind(session, card)
        manapool_bind(session, card)
        intake.set_manapool_push_enabled(session, True)
        order = reserved_order(session)
        order_id = order.id
    assert manapool.quantities == [("product-1", 0)]

    response = client.post(f"/orders/{order_id}/cancel",
                           data={"cancel_reason": "other", "cancel_note": ""},
                           follow_redirects=False)
    assert response.status_code in (302, 303), response.status_code

    assert manapool.quantities[-1] == ("product-1", 1)
    assert manapool.seen_status[-1] == "available"
    assert "after commit" in logs.text
    with Session(db) as session:
        assert session.get(InventoryCard, 1).status == "available"


def test_the_uncancel_route_lowers_manapool_again(db, wired, client,
                                                  monkeypatch):
    """★ THE HOLE THE RAISE OPENS, closed in the same release. Undo the
    cancellation and the card is spoken for again, so leaving Mana Pool
    at the raised number would be an oversell."""
    manapool = ManaPool(db, card_id=1).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        shopify_bind(session, card)
        manapool_bind(session, card)
        intake.set_manapool_push_enabled(session, True)
        order = reserved_order(session)
        order_id = order.id
    client.post(f"/orders/{order_id}/cancel",
                data={"cancel_reason": "other", "cancel_note": ""},
                follow_redirects=False)
    assert manapool.quantities[-1] == ("product-1", 1)

    response = client.post(f"/orders/{order_id}/uncancel",
                           follow_redirects=False)
    # This route renders its own confirmation page rather than
    # redirecting, so the status alone would also pass for the refusal
    # page -- read the page.
    assert response.status_code == 200, response.status_code
    assert "Order Uncancelled" in response.text
    assert manapool.quantities[-1] == ("product-1", 0)
    assert manapool.seen_status[-1] == "reserved"


def test_the_write_is_quantity_only(db, wired, monkeypatch):
    """price_cents null -- documented as "leaves the existing value
    unchanged" in the live spec -- so a quantity write can never move a
    price, in either direction."""
    manapool = ManaPool(db).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        shopify_bind(session, card)
        manapool_bind(session, card)
        intake.set_manapool_push_enabled(session, True)
        order = reserved_order(session)
        order_service.release_order(session, order)
        session.commit()
    assert len(manapool.calls) == 2
    for call in manapool.calls:
        for row in call:
            assert row["price_cents"] is None
            assert row["product_type"] == "mtg_single"
            assert set(row) == {"product_type", "product_id", "price_cents",
                                "quantity"}


def test_two_copies_are_one_write_of_the_whole_count(db, wired, monkeypatch):
    """One item per distinct binding, holding the new total -- not one
    per card and not a decrement."""
    manapool = ManaPool(db).install(monkeypatch)
    with Session(db) as session:
        first = make_card(session, n=1)
        second = make_card(session, n=1)      # same identity, second copy
        shopify_bind(session, first)
        manapool_bind(session, first)
        intake.set_manapool_push_enabled(session, True)
        order = reserved_order(session)
        assert session.get(InventoryCard, second.id).status == "available"
    # Two copies, one reserved: the surviving one is what Mana Pool is told.
    assert manapool.quantities == [("product-1", 1)]


# --- 3. one gate stops all three directions ------------------------------


def test_the_gate_off_writes_nothing_in_any_direction(db, wired, client,
                                                      monkeypatch, logs):
    """★ THE HARD STOP, still the hard stop. Allocation, cancellation and
    uncancel all go quiet together."""
    def explode(updates):  # pragma: no cover -- must not run
        raise AssertionError("wrote to Mana Pool with the push OFF")

    monkeypatch.setattr(mp, "update_inventory_prices_by_product", explode)
    with Session(db) as session:
        card = make_card(session)
        shopify_bind(session, card)
        manapool_bind(session, card)
        assert intake.manapool_push_enabled(session) is False
        order = reserved_order(session)
        order_id = order.id
    client.post(f"/orders/{order_id}/cancel",
                data={"cancel_reason": "other", "cancel_note": ""},
                follow_redirects=False)
    client.post(f"/orders/{order_id}/uncancel", follow_redirects=False)
    assert "the Mana Pool push is OFF" in logs.text
    with Session(db) as session:
        assert session.get(InventoryCard, 1).status == "reserved"


def test_a_manapool_orders_cancellation_tells_manapool_nothing(
        db, wired, monkeypatch):
    """Scoped to source="shopify". A Mana Pool order cancelled on Mana
    Pool is theirs to restock; telling them about it is a write nobody
    asked for."""
    manapool = ManaPool(db).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        manapool_bind(session, card)
        intake.set_manapool_push_enabled(session, True)
        order = SalesOrder(external_order_id="mp-1", external_label="L-1",
                           source="manapool", status="pending")
        session.add(order)
        session.flush()
        session.add(OrderItem(
            order_id=order.id, name=card.name, set_code=card.set_code,
            collector_number=card.collector_number, quantity=1,
            scryfall_id=card.scryfall_id, mtgjson_id=card.mtgjson_id,
            language_id=card.language_id, condition_id=card.condition_id,
            finish_id=card.finish_id, price_cents=65))
        session.flush()
        order_service.allocate_order(session, order)
        session.commit()
        order_service.release_order(session, order)
        session.commit()
    assert manapool.calls == []


# --- 4. the collision, unchanged ----------------------------------------


def _manapool_order(session, card, *, label="L-1"):
    order = SalesOrder(external_order_id=f"mp-{label}", external_label=label,
                       source="manapool", status="pending")
    session.add(order)
    session.flush()
    session.add(OrderItem(
        order_id=order.id, name=card.name, set_code=card.set_code,
        collector_number=card.collector_number, quantity=1,
        scryfall_id=card.scryfall_id, mtgjson_id=card.mtgjson_id,
        language_id=card.language_id, condition_id=card.condition_id,
        finish_id=card.finish_id, price_cents=65))
    session.flush()
    return order


def test_shopify_wins_the_last_copy_and_manapool_hears_once(db, wired,
                                                             monkeypatch):
    manapool = ManaPool(db).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        shopify_bind(session, card)
        manapool_bind(session, card)
        intake.set_manapool_push_enabled(session, True)
        shopify = reserved_order(session)
        assert shopify.status == "ready_to_pick"
        rival = _manapool_order(session, card)
        result = order_service.allocate_order(session, rival)
        session.commit()
    assert result["fully_matched"] is False
    assert manapool.quantities == [("product-1", 0)]


def test_manapool_wins_the_last_copy_and_hears_nothing(db, wired, monkeypatch):
    """The sale happened THERE, so their own quantity dropped by itself --
    and the Shopify order that lost allocates nothing, so there is no
    card to push."""
    manapool = ManaPool(db).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        shopify_bind(session, card)
        manapool_bind(session, card)
        intake.set_intake_enabled(session, True)
        intake.set_manapool_push_enabled(session, True)
        mine = _manapool_order(session, card)
        order_service.allocate_order(session, mine)
        session.commit()
        outcome = intake.ingest_one(session, webhook_body(),
                                    initiated_by="shopify-webhook")
        session.commit()
    assert outcome["outcome"] == "ingested"
    with Session(db) as session:
        loser = session.query(SalesOrder).filter_by(source="shopify").one()
        assert loser.status == "short"
    assert manapool.calls == []


# --- 5. a Mana Pool failure never fails the local transition ------------


def test_a_failed_write_leaves_the_cancel_standing_and_says_why(
        db, wired, client, monkeypatch, logs):
    manapool = ManaPool(db, card_id=1).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        shopify_bind(session, card)
        manapool_bind(session, card)
        intake.set_manapool_push_enabled(session, True)
        order = reserved_order(session)
        order_id = order.id
    manapool.explode = httpx.ConnectError("mana pool is down")

    response = client.post(f"/orders/{order_id}/cancel",
                           data={"cancel_reason": "other", "cancel_note": ""},
                           follow_redirects=False)
    assert response.status_code in (302, 303)
    with Session(db) as session:
        assert session.get(InventoryCard, 1).status == "available"
        order = session.get(SalesOrder, order_id)
        assert order.status == "cancelled"
        binding = session.get(RemoteProductBinding, 1)
        assert "mana pool is down" in (
            binding.last_quantity_push_failure_detail or "")
        assert binding.last_quantity_push_attempted_at is not None
    assert "mana pool quantity push failed" in logs.text


def test_a_card_with_no_price_is_not_advertised_on_the_raise(db, wired,
                                                             monkeypatch):
    """push_return_to_sellable's own policy, reached through the release:
    listing at no price is worse than not listing."""
    manapool = ManaPool(db).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session, price_cents=None)
        shopify_bind(session, card)
        manapool_bind(session, card)
        intake.set_manapool_push_enabled(session, True)
        order = reserved_order(session)
        assert manapool.quantities == [("product-1", 0)]
        order_service.release_order(session, order)
        session.commit()
    # The lower happened; the raise declined, so Mana Pool was not asked
    # to advertise a card with no price.
    assert manapool.quantities == [("product-1", 0)]


# --- 6. the cost ---------------------------------------------------------


# Measured, not estimated. 26 of these are the cancel route itself (the
# number test_shopify_quantity_raise pins with both pushes off, plus the
# Mana Pool gate read); the remaining 6 are the flush's own session --
# its gate read, the card, the binding resolution, the fresh recount and
# the binding stamp it commits.
PINNED_CANCEL_STATEMENTS = 32


def test_the_cancel_route_statement_count_is_pinned_with_the_gate_on(
        db, wired, client, monkeypatch):
    """★ THE FLUSH OPENS A SESSION OF ITS OWN, which is exactly the kind
    of thing that grows quietly. Exact, not an upper bound."""
    ManaPool(db).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        shopify_bind(session, card)
        manapool_bind(session, card)
        intake.set_manapool_push_enabled(session, True)
        order = reserved_order(session)
        order_id = order.id

    statements = _count_statements(db, lambda: client.post(
        f"/orders/{order_id}/cancel",
        data={"cancel_reason": "other", "cancel_note": ""},
        follow_redirects=False))
    assert len(statements) == PINNED_CANCEL_STATEMENTS, len(statements)
