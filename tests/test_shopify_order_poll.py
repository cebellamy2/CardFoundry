"""Shopify ticket 8: the poll's cursor, the cancellation it used to drop,
the order money it never read, and the delivery method it now asks about.

Every one of these is a bug that reached production on 2026-11-03, the
day the first real Shopify order was placed, and every one of them was
reachable only through the poll -- which had no tests at all until this
file. In the order they bit:

  1. THE SAME ORDER TWICE. A webhook body carries `id: 7271829405761`
     and a GraphQL node carries `id: "gid://shopify/Order/..."`, both
     were stored verbatim, and the poll ingested a SECOND local order
     for a purchase the webhook had already ingested -- reserving the
     card again and pushing Shopify's quantity back to zero for an order
     that was already cancelled.
  2. THE CURSOR WALKED BACKWARDS, one minute per empty tick.
  3. A CANCELLATION THE POLL COULD NOT ACT ON, although its whole
     `updated_at` filter exists to catch one.
  4. NO ORDER MONEY, so the Orders page showed an em dash for a Total.
  5. NOTHING NOTICED A SHOPIFY ORDER THAT CHOSE SHIPPING, in a phase
     that is pickup only.

No test touches the network: shopify_service.execute is stubbed, and the
stub refuses any document it was not told to expect.
"""
import logging
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import attention_service
import inventory_sync_service
import main
import shopify_delivery_method_service as delivery
import shopify_order_poll_service as poll
import shopify_order_service as intake
import shopify_service
from local_price_writeback_service import set_card_price
from models import (
    Base, Batch, ImportRecord, InventoryCard, OrderItem, PickAllocation,
    SalesOrder,
)
from shopify_binding_service import record_binding

ORDER_NUMERIC_ID = "7271829405761"
ORDER_GID = f"gid://shopify/Order/{ORDER_NUMERIC_ID}"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shopify-poll.db'}")
    Base.metadata.create_all(engine)
    return engine


def make_batch(session, code="A1"):
    batch = Batch(batch_code=code)
    session.add(batch)
    session.flush()
    return batch


def add_card(session, batch, *, n=1, price_cents=65):
    record = session.query(ImportRecord).filter_by(batch_id=batch.id).first()
    if not record:
        record = ImportRecord(batch_id=batch.id, filename="x.csv",
                              file_hash=f"h{batch.id}", card_count=1)
        session.add(record)
        session.flush()
    card = InventoryCard(
        batch_id=batch.id, import_id=record.id, name=f"Card {n}",
        set_code="MIR", collector_number=str(n), scryfall_id=f"sf-{n}",
        mtgjson_id=f"mtg-{n}", language_id="EN", condition_id="LP",
        finish_id="NF", condition="LP", finish="normal", status="available",
    )
    session.add(card)
    session.flush()
    set_card_price(session, card, price_cents,
                   source="seller_inventory_scan", note="fixture")
    session.flush()
    return card


def bind(session, card):
    return record_binding(
        session, sku=f"CF-MIR-{card.collector_number}-EN-LP-NF",
        set_code=card.set_code, collector_number=card.collector_number,
        language_id=card.language_id, condition_id=card.condition_id,
        finish_id=card.finish_id, mtgjson_id=card.mtgjson_id,
        product_gid="gid://shopify/Product/1",
        variant_gid="gid://shopify/ProductVariant/1",
        inventory_item_gid="gid://shopify/InventoryItem/1",
    )


def graphql_node(*, order_id=ORDER_GID, name="#1001", sku="CF-MIR-1-EN-LP-NF",
                 updated_at="2026-11-03T16:25:49Z", cancelled_at=None,
                 price="0.65", shipping="8.00", total="8.65"):
    """An order node in the shape read_orders_since reads."""
    node = {
        "id": order_id,
        "name": name,
        "createdAt": "2026-11-03T16:25:49Z",
        "updatedAt": updated_at,
        "cancelledAt": cancelled_at,
        "email": "buyer@example.com",
        "lineItems": {"edges": [{"node": {
            "id": "gid://shopify/LineItem/1", "sku": sku, "quantity": 1,
            "name": "Barreling Attack", "discountedUnitPriceSet": {
                "shopMoney": {"amount": price, "currencyCode": "USD"}},
        }}]},
    }
    if shipping is not None:
        node["totalShippingPriceSet"] = {
            "shopMoney": {"amount": shipping, "currencyCode": "USD"}}
    if total is not None:
        node["totalPriceSet"] = {
            "shopMoney": {"amount": total, "currencyCode": "USD"}}
    return node


def webhook_body(*, order_id=ORDER_NUMERIC_ID, name="#1001",
                 sku="CF-MIR-1-EN-LP-NF", shipping="8.00", total="8.65"):
    """The plain snake_case JSON a webhook sends, which is NOT the
    GraphQL shape -- including its MoneyBag's `shop_money`."""
    body = {
        "id": int(order_id),
        "name": name,
        "created_at": "2026-11-03T16:25:49Z",
        "line_items": [{"id": 1, "sku": sku, "quantity": 1,
                        "name": "Barreling Attack", "price": "0.65"}],
    }
    if shipping is not None:
        body["total_shipping_price_set"] = {
            "shop_money": {"amount": shipping, "currency_code": "USD"}}
    if total is not None:
        body["total_price_set"] = {
            "shop_money": {"amount": total, "currency_code": "USD"}}
    return body


class FakeShopify:
    """Answers the two documents the poll sends, and nothing else.

    A stub that answered anything would let a future caller add a live
    Shopify read to the poll without a test noticing.
    """

    def __init__(self, *, orders=(), pages=None, shipping=(), pickup=(),
                 delivery_raises=None, contacts=None, contact_raises=None):
        self.order_pages = list(pages) if pages is not None else [
            {"orders": list(orders), "has_next": False}]
        self.shipping = [str(value) for value in shipping]
        self.pickup = [str(value) for value in pickup]
        self.delivery_raises = delivery_raises
        # Ticket 18: the poll now also asks who is collecting. Keyed by
        # numeric order id; a missing key means Shopify returned no node.
        self.contacts = dict(contacts or {})
        self.contact_raises = contact_raises
        self.documents = []
        self.variables = []
        self.page_index = 0

    def execute(self, document, variables=None, *, client=None, **kwargs):
        self.documents.append(document)
        self.variables.append(dict(variables or {}))
        if "CardFoundryShopifyOrders" in document:
            page = self.order_pages[min(self.page_index,
                                        len(self.order_pages) - 1)]
            self.page_index += 1
            return {"orders": {
                "edges": [{"cursor": f"c{i}", "node": node}
                          for i, node in enumerate(page["orders"])],
                "pageInfo": {"hasNextPage": page["has_next"]},
            }}
        if "CardFoundryShopifyDeliveryMethod" in document:
            if self.delivery_raises is not None:
                raise self.delivery_raises
            return {
                "shipping": {"nodes": [
                    {"id": f"gid://shopify/Order/{v}"} for v in self.shipping]},
                "pickup": {"nodes": [
                    {"id": f"gid://shopify/Order/{v}"} for v in self.pickup]},
            }
        if "CardFoundryShopifyOrderContact" in document:
            if self.contact_raises is not None:
                raise self.contact_raises
            return {"orders": {"nodes": list(self.contacts.values())}}
        raise AssertionError(f"the poll sent an unexpected document: "
                             f"{document[:120]}")


@pytest.fixture
def logs(caplog):
    """★ THE cardfoundry LOGGER DOES NOT PROPAGATE, so caplog needs its
    handler attached by hand -- the same thing test_attention_scheduled_
    jobs does, for the same reason."""
    logger = logging.getLogger("cardfoundry")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger="cardfoundry")
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


@pytest.fixture
def fake(monkeypatch):
    """Installs a FakeShopify on shopify_service for every caller."""
    holder = {}

    def install(**kwargs):
        stub = FakeShopify(**kwargs)
        monkeypatch.setattr(shopify_service, "execute", stub.execute)
        holder["stub"] = stub
        return stub

    return install


def ready_shop(session, *, cards=1):
    intake.set_intake_enabled(session, True)
    batch = make_batch(session)
    made = []
    for n in range(1, cards + 1):
        card = add_card(session, batch, n=n)
        bind(session, card)
        made.append(card)
    session.commit()
    return made


# --- 1. one Shopify order is ONE local order ----------------------------


def test_the_webhook_and_the_poll_do_not_ingest_the_same_order_twice(db, fake):
    """★ THE LIVE BUG, 2026-11-03 16:54. The webhook ingested #1001 by
    its numeric id; the poll then ingested it AGAIN by its gid, reserved
    the card a second time, and lowered Shopify's quantity for a sale
    that had already been cancelled. One order, one reservation."""
    fake(orders=[graphql_node()])
    with Session(db) as session:
        (card,) = ready_shop(session)
        # The webhook's own shape first, exactly as the receiver ingests it.
        first = intake.ingest_shopify_orders(session, [webhook_body()])
        assert len(first["ingested"]) == 1
        # Then the poll, reading the SAME order as a GraphQL node.
        outcome = poll.poll_shopify_orders(session)
        session.commit()
        assert outcome["ingested"] == 0
        assert outcome["already_known"] == 1
        assert session.query(SalesOrder).count() == 1
        assert session.query(PickAllocation).count() == 1
        session.refresh(card)
        assert card.status == "reserved"


def test_the_id_is_stored_in_one_form_whichever_reader_saw_it(db, fake):
    fake(orders=[graphql_node()])
    with Session(db) as session:
        ready_shop(session)
        poll.poll_shopify_orders(session)
        session.commit()
        assert session.query(SalesOrder).one().external_order_id == \
            ORDER_NUMERIC_ID


@pytest.mark.parametrize("raw,expected", [
    (ORDER_GID, ORDER_NUMERIC_ID),
    (ORDER_NUMERIC_ID, ORDER_NUMERIC_ID),
    (int(ORDER_NUMERIC_ID), ORDER_NUMERIC_ID),
    ("gid://shopify/Order/5/", "5"),
    (None, ""),
    ("", ""),
])
def test_every_form_of_the_order_id_canonicalises_the_same(raw, expected):
    assert intake.canonical_external_order_id(raw) == expected


def test_a_row_stored_under_the_old_gid_key_is_still_found(db):
    """★ THE ROWS ALREADY IN PRODUCTION. One order is stored under its
    gid and must not be walked past, or the poll would ingest it again
    -- which is the bug, pointed the other way."""
    with Session(db) as session:
        ready_shop(session)
        session.add(SalesOrder(external_order_id=ORDER_GID,
                               external_label="#1001", source="shopify",
                               status="ready_to_pick"))
        session.commit()
        found = intake.existing_order(session, ORDER_NUMERIC_ID)
        assert found is not None
        assert found.external_order_id == ORDER_GID


def test_two_local_orders_for_one_shopify_order_are_reported_not_hidden(
        db, logs):
    """The EARLIEST is the real one -- it is the purchase -- and the
    duplicate is named in the log.

    ★ BOTH ROWS ARE OPEN HERE (v2.103.0). The warning counts only rows
    that still reserve something, because v2.101.0 counted every row and
    so went on firing hourly after the operator did what it asked. The
    settled case has its own test in test_shopify_quantity_raise.py."""
    with Session(db) as session:
        real = SalesOrder(external_order_id=ORDER_NUMERIC_ID,
                          external_label="#1001", source="shopify",
                          status="ready_to_pick")
        session.add(real)
        session.flush()
        session.add(SalesOrder(external_order_id=ORDER_GID,
                               external_label="#1001", source="shopify",
                               status="ready_to_pick"))
        session.commit()
        found = intake.existing_order(session, ORDER_GID)
        assert found.id == real.id
        assert "reserving stock more than once" in logs.text


def test_the_duplicate_production_actually_held_is_now_quiet(db, logs):
    """★ PRODUCTION'S OWN SHAPE, after the cleanup. Orders 5092
    (cancelled) and 5093 (open) both matched Shopify #1001; cancelling
    5093 left one cancelled row and one open row, and nothing double-
    reserves any more. The earliest is still returned."""
    with Session(db) as session:
        real = SalesOrder(external_order_id=ORDER_NUMERIC_ID,
                          external_label="#1001", source="shopify",
                          status="cancelled")
        session.add(real)
        session.flush()
        session.add(SalesOrder(external_order_id=ORDER_GID,
                               external_label="#1001", source="shopify",
                               status="cancelled"))
        session.commit()
        found = intake.existing_order(session, ORDER_GID)
        assert found.id == real.id
        assert "reserving stock more than once" not in logs.text


# --- 2. the cursor -------------------------------------------------------


def _window_minutes(outcome, started):
    since = datetime.strptime(outcome["since"], "%Y-%m-%dT%H:%M:%SZ")
    return (started - since).total_seconds() / 60.0


def test_two_empty_ticks_leave_the_read_window_the_same_width(db, fake):
    """★ THE TICKET'S OWN TEST. The window was one minute wide and grew
    by a minute every empty tick, because a complete walk that found
    nothing stored `cursor - overlap` as the new cursor."""
    fake(orders=[])
    first_at = datetime(2026, 11, 3, 12, 0, 0)
    second_at = datetime(2026, 11, 3, 13, 0, 0)
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        poll.set_poll_cursor(session, datetime(2026, 11, 3, 11, 0, 0))
        session.commit()
        first = poll.poll_shopify_orders(session, now=first_at)
        session.commit()
        second = poll.poll_shopify_orders(session, now=second_at)
        session.commit()
    # Two ticks an hour apart, so the honest window is the hour since the
    # last tick plus the overlap -- the SAME width both times. Under the
    # old arithmetic it was 61 minutes and then 62, and so on for ever.
    expected = 60 + poll.CURSOR_OVERLAP_MINUTES
    assert _window_minutes(first, first_at) == pytest.approx(expected)
    assert _window_minutes(second, second_at) == pytest.approx(expected)


def test_an_empty_complete_walk_advances_the_cursor_to_the_walk_start(db,
                                                                      fake):
    fake(orders=[])
    started = datetime(2026, 11, 3, 12, 0, 0)
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        poll.set_poll_cursor(session, datetime(2026, 11, 3, 11, 0, 0))
        session.commit()
        outcome = poll.poll_shopify_orders(session, now=started)
        session.commit()
        assert outcome["cursor_moved"] is True
        assert poll.poll_cursor(session) == started


def test_the_cursor_never_moves_backwards(db, fake):
    """A walk whose newest order predates the cursor says nothing about
    the gap between them, and moving back to it would widen the window
    by the difference -- the same failure, one tick at a time."""
    fake(orders=[graphql_node(updated_at="2026-11-03T09:00:00Z")])
    with Session(db) as session:
        ready_shop(session)
        cursor = datetime(2026, 11, 3, 11, 0, 0)
        poll.set_poll_cursor(session, cursor)
        session.commit()
        outcome = poll.poll_shopify_orders(
            session, now=datetime(2026, 11, 3, 12, 0, 0))
        session.commit()
        assert outcome["cursor_moved"] is False
        assert poll.poll_cursor(session) == cursor


def test_a_complete_walk_that_read_orders_advances_to_the_newest_seen(db,
                                                                     fake):
    fake(orders=[graphql_node(updated_at="2026-11-03T11:30:00Z")])
    with Session(db) as session:
        ready_shop(session)
        poll.set_poll_cursor(session, datetime(2026, 11, 3, 11, 0, 0))
        session.commit()
        poll.poll_shopify_orders(session, now=datetime(2026, 11, 3, 12, 0, 0))
        session.commit()
        assert poll.poll_cursor(session) == datetime(2026, 11, 3, 11, 30, 0)


def test_an_incomplete_walk_leaves_the_cursor_where_it_was(db, fake):
    """Unchanged, and pinned for the first time: a partial walk that
    moved the cursor would skip every order it did not reach."""
    fake(pages=[{"orders": [graphql_node()], "has_next": True}])
    with Session(db) as session:
        ready_shop(session)
        cursor = datetime(2026, 11, 3, 11, 0, 0)
        poll.set_poll_cursor(session, cursor)
        session.commit()
        outcome = poll.poll_shopify_orders(
            session, now=datetime(2026, 11, 3, 12, 0, 0),
            client=None)
        session.commit()
        assert outcome["complete"] is False
        assert outcome["cursor_moved"] is False
        assert poll.poll_cursor(session) == cursor


# --- 3. the cancellation the poll used to drop --------------------------


def test_the_poll_releases_an_order_cancelled_on_shopify(db, fake):
    """★ WHAT THE `updated_at` FILTER WAS FOR ALL ALONG. A missed
    orders/cancelled delivery left the cards reserved for a customer who
    was never coming, for ever."""
    fake(orders=[graphql_node(cancelled_at="2026-11-03T16:32:37Z",
                              updated_at="2026-11-03T16:32:37Z")])
    with Session(db) as session:
        (card,) = ready_shop(session)
        intake.ingest_shopify_orders(session, [webhook_body()])
        session.refresh(card)
        assert card.status == "reserved"
        outcome = poll.poll_shopify_orders(session)
        session.commit()
        assert outcome["released"] == 1
        assert outcome["ingested"] == 0
        order = session.query(SalesOrder).one()
        assert order.status == "cancelled"
        session.refresh(card)
        assert card.status == "available"


def test_seeing_the_same_cancellation_again_changes_nothing(db, fake):
    stub = fake(orders=[graphql_node(cancelled_at="2026-11-03T16:32:37Z",
                                     updated_at="2026-11-03T16:32:37Z")])
    with Session(db) as session:
        ready_shop(session)
        intake.ingest_shopify_orders(session, [webhook_body()])
        poll.poll_shopify_orders(session)
        session.commit()
        stub.page_index = 0
        again = poll.poll_shopify_orders(session)
        session.commit()
        assert again["released"] == 0
        assert again["already_known"] == 1
        assert session.query(SalesOrder).one().status == "cancelled"


def test_a_cancellation_never_releases_an_order_already_collected(db, fake):
    """The cards are with the customer. Writing "cancelled" over a
    collected order would release nothing and lie about the sale."""
    fake(orders=[graphql_node(cancelled_at="2026-11-03T16:32:37Z",
                              updated_at="2026-11-03T16:32:37Z")])
    with Session(db) as session:
        (card,) = ready_shop(session)
        intake.ingest_shopify_orders(session, [webhook_body()])
        order = session.query(SalesOrder).one()
        order.status = "picked_up"
        card.status = "sold"
        session.commit()
        outcome = poll.poll_shopify_orders(session)
        session.commit()
        assert outcome["released"] == 0
        assert outcome["refused"] == 1
        assert session.query(SalesOrder).one().status == "picked_up"
        session.refresh(card)
        assert card.status == "sold"


def test_the_webhook_and_the_poll_release_through_the_same_function(db):
    """One definition of what a Shopify cancellation releases. The
    webhook's own path is a wrapper around it."""
    with Session(db) as session:
        (card,) = ready_shop(session)
        intake.ingest_shopify_orders(session, [webhook_body()])
        order = session.query(SalesOrder).one()
        outcome = intake.release_cancelled_order(
            session, order, initiated_by="shopify-webhook")
        session.commit()
        assert outcome["outcome"] == "released"
        session.refresh(card)
        assert card.status == "available"


# --- 4. the order money -------------------------------------------------


def test_shipping_is_stored_from_both_payload_shapes(db, fake):
    fake(orders=[graphql_node()])
    with Session(db) as session:
        ready_shop(session, cards=2)
        intake.ingest_shopify_orders(session, [webhook_body()])
        assert session.query(SalesOrder).one().shipping_cents == 800
    with Session(db) as session:
        poll.poll_shopify_orders(session)
        session.commit()
        assert {o.shipping_cents for o in session.query(SalesOrder).all()} \
            == {800}


def test_an_order_with_no_shipping_figure_keeps_null_not_zero(db):
    """NULL means "Shopify did not tell us", and the page says so with an
    em dash. Zero would be a claim nobody made."""
    with Session(db) as session:
        ready_shop(session)
        intake.ingest_shopify_orders(
            session, [webhook_body(shipping=None, total=None)])
        assert session.query(SalesOrder).one().shipping_cents is None


def test_a_total_that_disagrees_with_subtotal_plus_shipping_is_logged(db,
                                                                     logs):
    """There is no column for tax or a discount, so the gap is named in
    the log rather than invented or silently shown."""
    with Session(db) as session:
        ready_shop(session)
        intake.ingest_shopify_orders(session, [webhook_body(total="9.65")])
        assert "Shopify's own total" in logs.text


def test_the_orders_page_shows_a_real_total_for_a_shopify_order(
        tmp_path, monkeypatch, db):
    """★ WHAT THE OPERATOR SEES. The Total column read "—" for #1001,
    because it is subtotal + shipping_cents and shipping_cents was NULL
    on every Shopify order ever ingested."""
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    with Session(db) as session:
        ready_shop(session)
        intake.ingest_shopify_orders(session, [webhook_body()])
        session.commit()
    response = TestClient(main.app).get("/orders?status=all")
    assert response.status_code == 200
    assert "$8.65" in response.text


# --- 5. the delivery method ---------------------------------------------


def test_an_order_that_chose_shipping_is_recorded_and_raises_one_item(db,
                                                                     fake):
    """★ PHASE 1 IS PICKUP ONLY. #1001 was checked out with an $8.00
    shipping line and CardFoundry called it a pickup anyway."""
    fake(orders=[graphql_node()], shipping=[ORDER_NUMERIC_ID])
    with Session(db) as session:
        ready_shop(session)
        poll.poll_shopify_orders(session)
        session.commit()
        order = session.query(SalesOrder).one()
        assert order.shopify_delivery_method == delivery.METHOD_SHIPPING
        items = attention_service._shopify_not_pickup_items(session)
        assert len(items) == 1
        assert "chose shipping" in items[0].summary
        assert items[0].href == f"/orders/{order.id}"
        assert attention_service._candidate_counts(session)[
            attention_service.CATEGORY_SHOPIFY_NOT_PICKUP] == 1


def test_the_attention_page_says_so_in_plain_words(tmp_path, monkeypatch, db,
                                                   fake):
    """★ WHAT THE OPERATOR SEES. Not that a collector returned an item --
    the sentence on the page they read."""
    fake(orders=[graphql_node()], shipping=[ORDER_NUMERIC_ID])
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    with Session(db) as session:
        ready_shop(session)
        poll.poll_shopify_orders(session)
        session.commit()
    response = TestClient(main.app).get("/orders/needs-attention")
    assert response.status_code == 200
    assert "chose shipping" in response.text
    assert "phase 1 is pickup only" in response.text


def test_an_order_that_chose_pickup_raises_nothing(db, fake):
    fake(orders=[graphql_node()], pickup=[ORDER_NUMERIC_ID])
    with Session(db) as session:
        ready_shop(session)
        poll.poll_shopify_orders(session)
        session.commit()
        assert session.query(SalesOrder).one().shopify_delivery_method == \
            delivery.METHOD_PICKUP
        assert attention_service._shopify_not_pickup_items(session) == []


def test_an_order_in_neither_bucket_is_unclassified_and_reported(db, fake):
    """★ THE UNCLASSIFIED BUCKET. Local delivery, a retail order, or a
    method Shopify adds later must not be assumed to be a pickup."""
    fake(orders=[graphql_node()])
    with Session(db) as session:
        ready_shop(session)
        poll.poll_shopify_orders(session)
        session.commit()
        assert session.query(SalesOrder).one().shopify_delivery_method == \
            delivery.METHOD_UNCLASSIFIED
        assert attention_service._shopify_not_pickup_items(session) == []


def test_the_webhook_never_records_an_order_shopify_has_not_indexed(db,
                                                                   fake):
    """★ FOUR SECONDS AFTER THE PURCHASE, "in neither bucket" probably
    means "not in the search index yet". Recording it would be an answer
    nobody gave, and the column would stop being NULL -- so nothing
    would ever ask again."""
    fake(orders=[])
    with Session(db) as session:
        ready_shop(session)
        intake.ingest_shopify_orders(session, [webhook_body()])
        order = session.query(SalesOrder).one()
        delivery.record_delivery_methods(session, [order],
                                         store_unclassified=False)
        session.commit()
        assert session.query(SalesOrder).one().shopify_delivery_method is None
        # And the poll, an hour later, is allowed to conclude it.
        poll.poll_shopify_orders(session)
        session.commit()
        assert session.query(SalesOrder).one().shopify_delivery_method == \
            delivery.METHOD_UNCLASSIFIED


def test_an_order_nobody_has_asked_about_raises_nothing(db):
    """NULL is "not asked yet". An alarm that fired on missing data would
    fire on every order written before this existed."""
    with Session(db) as session:
        ready_shop(session)
        intake.ingest_shopify_orders(session, [webhook_body()])
        session.commit()
        assert session.query(SalesOrder).one().shopify_delivery_method is None
        assert attention_service._shopify_not_pickup_items(session) == []


def test_the_item_clears_itself_when_the_order_is_cancelled(db, fake):
    fake(orders=[graphql_node()], shipping=[ORDER_NUMERIC_ID])
    with Session(db) as session:
        ready_shop(session)
        poll.poll_shopify_orders(session)
        session.commit()
        assert len(attention_service._shopify_not_pickup_items(session)) == 1
        order = session.query(SalesOrder).one()
        intake.release_cancelled_order(session, order,
                                       initiated_by="shopify-webhook")
        session.commit()
        assert attention_service._shopify_not_pickup_items(session) == []


def test_a_failed_delivery_method_read_leaves_null_and_never_raises(db, fake):
    """The poll's job is the orders. An unanswered question stays
    unanswered and is asked again next tick."""
    fake(orders=[graphql_node()],
         delivery_raises=shopify_service.ShopifyError("Shopify is down"))
    with Session(db) as session:
        ready_shop(session)
        outcome = poll.poll_shopify_orders(session)
        session.commit()
        assert outcome["ingested"] == 1
        assert outcome["delivery_methods"]["stored"] == 0
        assert session.query(SalesOrder).one().shopify_delivery_method is None


def test_the_delivery_method_is_asked_in_one_read_for_a_whole_batch(db, fake):
    """Two filters, two aliases, ONE document -- not one call per order."""
    stub = fake(orders=[graphql_node(),
                        graphql_node(order_id="gid://shopify/Order/2",
                                     name="#1002", sku="CF-MIR-2-EN-LP-NF")],
                shipping=[ORDER_NUMERIC_ID], pickup=["2"])
    with Session(db) as session:
        ready_shop(session, cards=2)
        poll.poll_shopify_orders(session)
        session.commit()
        methods = {o.external_label: o.shopify_delivery_method
                   for o in session.query(SalesOrder).all()}
        assert methods == {"#1001": delivery.METHOD_SHIPPING,
                           "#1002": delivery.METHOD_PICKUP}
    assert sum(1 for doc in stub.documents
               if "CardFoundryShopifyDeliveryMethod" in doc) == 1


def test_the_poll_asks_about_older_orders_that_have_no_method_yet(db, fake):
    """The orders already ingested before this existed are the ones that
    most need asking about -- #1001 among them. Bounded per tick."""
    fake(orders=[], shipping=[ORDER_NUMERIC_ID])
    with Session(db) as session:
        ready_shop(session)
        intake.ingest_shopify_orders(session, [webhook_body()])
        session.commit()
        poll.poll_shopify_orders(session)
        session.commit()
        assert session.query(SalesOrder).one().shopify_delivery_method == \
            delivery.METHOD_SHIPPING


def test_nothing_is_asked_of_shopify_while_intake_is_off(db, fake):
    stub = fake(orders=[graphql_node()])
    with Session(db) as session:
        make_batch(session)
        session.commit()
        outcome = poll.poll_shopify_orders(session)
        assert "off" in outcome["skipped"]
    assert stub.documents == []


def test_the_delivery_read_sends_shopifys_own_filter_values(db, fake):
    """`pick-up`, hyphenated, which is the search filter's spelling --
    NOT the GraphQL enum's PICK_UP. Mixing them matches nothing, silently.
    And the order is named by `id:`, not by its gid."""
    stub = fake(orders=[graphql_node()])
    with Session(db) as session:
        ready_shop(session)
        poll.poll_shopify_orders(session)
        session.commit()
    sent = [variables for document, variables
            in zip(stub.documents, stub.variables)
            if "CardFoundryShopifyDeliveryMethod" in document]
    assert len(sent) == 1
    assert sent[0]["shipping"] == \
        f"(id:{ORDER_NUMERIC_ID}) AND delivery_method:shipping"
    assert sent[0]["pickup"] == \
        f"(id:{ORDER_NUMERIC_ID}) AND delivery_method:pick-up"


def test_the_old_cursor_arithmetic_would_have_failed_this(db, fake):
    """A regression guard with teeth: five empty ticks in a row used to
    push the window five minutes wider. It stays one minute."""
    fake(orders=[])
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        poll.set_poll_cursor(session, datetime(2026, 11, 3, 10, 0, 0))
        session.commit()
        widths = []
        for hour in range(11, 16):
            at = datetime(2026, 11, 3, hour, 0, 0)
            outcome = poll.poll_shopify_orders(session, now=at)
            session.commit()
            widths.append(round(_window_minutes(outcome, at), 3))
        assert widths == [float(60 + poll.CURSOR_OVERLAP_MINUTES)] * 5
        assert poll.poll_cursor(session) == datetime(2026, 11, 3, 15, 0, 0)


def test_the_first_run_still_looks_back_a_bounded_window(db, fake):
    fake(orders=[])
    started = datetime(2026, 11, 3, 12, 0, 0)
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        session.commit()
        outcome = poll.poll_shopify_orders(session, now=started)
        session.commit()
    assert _window_minutes(outcome, started) == pytest.approx(
        poll.FIRST_RUN_LOOKBACK_HOURS * 60)
    assert timedelta(hours=poll.FIRST_RUN_LOOKBACK_HOURS) == timedelta(
        hours=24)
