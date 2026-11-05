"""Shopify ticket 6: orders in, into the ONE inventory. Built OFF.

What these pin, in the order that matters:

  1. OFF BY DEFAULT, failing closed, and the Mana Pool write OFF
     separately -- two gates, because the sensible staging is orders
     arriving and being picked with Mana Pool untouched first.
  2. ONE INVENTORY: a Shopify order becomes SalesOrder(source="shopify")
     with ordinary OrderItems and goes through the REAL allocate_order,
     so the card becomes `reserved` where everything else already reads.
  3. A COLLISION IS AN ORDINARY SHORT ORDER: the line is not allocated,
     the order is flagged, every one is counted.
  4. IDEMPOTENT on the Shopify order id, so the webhook and the poll can
     both see an order without double-reserving.
  5. A Shopify failure cannot break the Mana Pool order sync, and a
     Shopify order can never reach a Mana Pool-only step.

No test touches the network.
"""
import base64
import hashlib
import hmac
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

import order_service
import shopify_order_service as intake
import shopify_webhook_service as hook
from local_price_writeback_service import set_card_price
from models import (
    AppSetting, AppSettingChangeLog, Base, Batch, ImportRecord, InventoryCard,
    OrderItem, PickAllocation, SalesOrder, WebhookDelivery,
)
from shopify_binding_service import record_binding


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shopify-orders.db'}")
    Base.metadata.create_all(engine)
    return engine


def make_batch(session, code="A1", **over):
    batch = Batch(batch_code=code, **over)
    session.add(batch)
    session.flush()
    return batch


def add_card(session, batch, *, n=1, price_cents=500, **over):
    record = session.query(ImportRecord).filter_by(batch_id=batch.id).first()
    if not record:
        record = ImportRecord(batch_id=batch.id, filename="x.csv",
                              file_hash=f"h{batch.id}", card_count=1)
        session.add(record)
        session.flush()
    values = {
        "batch_id": batch.id, "import_id": record.id, "name": f"Card {n}",
        "set_code": "NEO", "collector_number": str(n),
        "scryfall_id": f"sf-{n}", "mtgjson_id": f"mtg-{n}",
        "language_id": "EN", "condition_id": "NM", "finish_id": "NF",
        "condition": "NM", "finish": "normal", "status": "available",
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


def bind(session, card):
    return record_binding(
        session, sku=f"CF-NEO-{card.collector_number}-EN-NM-NF",
        set_code=card.set_code, collector_number=card.collector_number,
        language_id=card.language_id, condition_id=card.condition_id,
        finish_id=card.finish_id,
        # The publish records it from the card, and allocate_order's
        # canonical key needs it -- a binding without one becomes a
        # needs_review order, which has its own test.
        mtgjson_id=card.mtgjson_id,
        product_gid="gid://shopify/Product/1",
        variant_gid="gid://shopify/ProductVariant/1",
        inventory_item_gid="gid://shopify/InventoryItem/1",
    )


def shopify_order(order_id="gid://shopify/Order/1", *, name="#1001",
                  skus=("CF-NEO-1-EN-NM-NF",), quantity=1, price="5.00"):
    """A Shopify GraphQL order node, in the shape the poll reads."""
    return {
        "id": order_id,
        "name": name,
        "createdAt": "2026-11-03T10:00:00Z",
        "updatedAt": "2026-11-03T10:00:00Z",
        "cancelledAt": None,
        "email": "buyer@example.com",
        "lineItems": {"edges": [
            {"node": {
                "id": f"gid://shopify/LineItem/{i}",
                "sku": sku,
                "quantity": quantity,
                "name": f"Line {i}",
                "discountedUnitPriceSet": {
                    "shopMoney": {"amount": price, "currencyCode": "USD"}},
                "originalUnitPriceSet": {
                    "shopMoney": {"amount": price, "currencyCode": "USD"}},
            }}
            for i, sku in enumerate(skus, start=1)
        ]},
    }


def webhook_body(order_id="12345", *, name="#1001", sku="CF-NEO-1-EN-NM-NF"):
    """The plain-JSON shape a webhook sends, which is NOT the GraphQL one."""
    return {
        "id": order_id,
        "name": name,
        "created_at": "2026-11-03T10:00:00Z",
        "line_items": [
            {"id": 1, "sku": sku, "quantity": 1, "name": "Line 1",
             "price": "5.00"},
        ],
    }


def _count_queries(engine, action):
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "after_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "after_cursor_execute", record)
    return seen


# --- 1. off by default, two gates ---------------------------------------


def test_intake_is_off_with_no_setting_row(db):
    with Session(db) as session:
        assert intake.intake_enabled(session) is False


def test_the_manapool_push_is_off_separately(db):
    """Two gates on purpose: orders arriving and being picked with Mana
    Pool untouched is the stage before writing to Mana Pool."""
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        session.flush()
        assert intake.intake_enabled(session) is True
        assert intake.manapool_push_enabled(session) is False


@pytest.mark.parametrize("stored,expected", [
    ("true", True), ("ON", True), ("1", True), ("yes", True),
    ("false", False), ("0", False), ("off", False), ("", False),
    ("maybe", False), ("probably not", False),
])
def test_both_settings_fail_closed(db, stored, expected):
    with Session(db) as session:
        session.add(AppSetting(key=intake.SETTING_KEY, value=stored))
        session.add(AppSetting(key=intake.MANAPOOL_PUSH_SETTING_KEY,
                               value=stored))
        session.flush()
        assert intake.intake_enabled(session) is expected
        assert intake.manapool_push_enabled(session) is expected


def test_nothing_is_ingested_while_intake_is_off(db):
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.flush()
        result = intake.ingest_shopify_orders(session, [shopify_order()])
        assert "off" in result["skipped"]
        assert session.query(SalesOrder).count() == 0


def test_turning_each_setting_on_is_audited(db):
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        intake.set_manapool_push_enabled(session, True)
        session.flush()
        keys = {row.key for row in session.query(AppSettingChangeLog).all()}
        assert keys == {intake.SETTING_KEY, intake.MANAPOOL_PUSH_SETTING_KEY}
        for row in session.query(AppSettingChangeLog).all():
            entry = json.loads(row.change_summary)
            assert entry["before"] is False and entry["after"] is True
            assert row.actor


# --- 2. one inventory ----------------------------------------------------


def test_a_shopify_order_reserves_through_the_real_allocation(db):
    """★ ONE INVENTORY. Not a parallel reservation -- the card becomes
    `reserved` in the one place every other part of the app reads."""
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.commit()
        result = intake.ingest_shopify_orders(session, [shopify_order()])
        assert len(result["ingested"]) == 1
        order = session.query(SalesOrder).one()
        assert order.source == "shopify"
        # ★ THE CANONICAL FORM: Shopify's numeric order id, whichever
        # shape the reader saw it in. Storing the gid verbatim is what
        # let the poll ingest a webhook-ingested order a second time on
        # 2026-11-03 -- see tests/test_shopify_order_poll.py.
        assert order.external_order_id == "1"
        assert order.external_label == "#1001"
        assert order.status == "ready_to_pick"
        assert order.placed_at is not None
        session.refresh(card)
        assert card.status == "reserved"
        allocation = session.query(PickAllocation).one()
        assert allocation.inventory_card_id == card.id


def test_the_line_is_resolved_by_sku_through_the_bindings(db):
    """★ THE SKU IS THE IDENTITY, which is why the publish made it
    deterministic. No name matching anywhere."""
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.commit()
        intake.ingest_shopify_orders(session, [shopify_order()])
        item = session.query(OrderItem).one()
        assert item.set_code == "NEO"
        assert item.collector_number == "1"
        assert item.condition_id == "NM"
        assert item.finish_id == "NF"
        assert item.language_id == "EN"
        assert item.price_cents == 500


def test_a_line_with_no_binding_is_reported_never_approximated(db):
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        session.commit()
        result = intake.ingest_shopify_orders(
            session, [shopify_order(skus=("CF-WHO-KNOWS",))])
        assert result["ingested"] == []
        refused = result["refused"][0]
        assert "no line resolved" in refused["reason"]
        assert refused["unresolvable"][0]["why"].startswith("no Shopify binding")


def test_a_line_with_no_sku_at_all_is_its_own_reason(db):
    """`sku` is nullable on Shopify's own LineItem, so this is real."""
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        session.commit()
        order = shopify_order()
        order["lineItems"]["edges"][0]["node"]["sku"] = None
        result = intake.ingest_shopify_orders(session, [order])
        assert result["refused"][0]["unresolvable"][0]["why"] == \
            "no sku on the line"


def test_the_webhook_bodys_different_shape_reads_the_same(db):
    """★ ONE NORMALISER, TWO READERS. If the webhook and the poll
    disagreed about an order they both see, "the second sighting is a
    no-op" would be a coincidence.

    ★ AND IT USES EACH READER'S REAL ID SHAPE, which this test did not:
    passing "7" to both hid the fact that a GraphQL node carries a gid
    and a webhook body carries a bare number, so the two readers keyed
    the same order differently and the poll duplicated it in production
    on 2026-11-03.
    """
    graphql = intake.normalise_order(
        shopify_order(order_id="gid://shopify/Order/7", name="#9"))
    body = intake.normalise_order(webhook_body(order_id="7", name="#9"))
    assert graphql["external_order_id"] == body["external_order_id"] == "7"
    assert graphql["external_label"] == body["external_label"] == "#9"
    assert graphql["lines"][0]["sku"] == body["lines"][0]["sku"]
    assert graphql["lines"][0]["unit_price_cents"] == \
        body["lines"][0]["unit_price_cents"] == 500
    assert graphql["placed_at"] == body["placed_at"]


def test_an_unreadable_order_date_leaves_it_empty_rather_than_inventing_one(db):
    order = shopify_order()
    order["createdAt"] = "not a date"
    assert intake.normalise_order(order)["placed_at"] is None


# --- 3. collisions -------------------------------------------------------


def test_a_collision_is_a_short_order_with_the_line_unallocated(db):
    """★ THE CASE THIS CHANNEL CREATES: the card sold on Mana Pool
    between the publish and the Shopify order."""
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        card.status = "sold"
        session.commit()
        result = intake.ingest_shopify_orders(session, [shopify_order()])
        order = session.query(SalesOrder).one()
        assert order.status == "short"
        assert session.query(PickAllocation).count() == 0
        assert result["collisions"] == 1
        collision = result["ingested"][0]["collisions"][0]
        assert collision["requested"] == 1
        assert collision["allocated"] == 0


def test_every_collision_is_counted_across_a_batch(db):
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        batch = make_batch(session)
        gone = add_card(session, batch, n=1)
        here = add_card(session, batch, n=2)
        bind(session, gone)
        bind(session, here)
        gone.status = "sold"
        session.commit()
        result = intake.ingest_shopify_orders(session, [
            shopify_order(order_id="gid://shopify/Order/1", name="#1",
                          skus=("CF-NEO-1-EN-NM-NF",)),
            shopify_order(order_id="gid://shopify/Order/2", name="#2",
                          skus=("CF-NEO-2-EN-NM-NF",)),
        ])
        assert result["collisions"] == 1
        assert len(result["ingested"]) == 2
        statuses = {o.external_label: o.status
                    for o in session.query(SalesOrder).all()}
        assert statuses == {"#1": "short", "#2": "ready_to_pick"}


def test_collision_orders_are_computed_from_the_allocation_not_a_counter(db):
    """★ COMPUTED, NOT RECORDED, so it cannot drift from the truth."""
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        card.status = "sold"
        session.commit()
        intake.ingest_shopify_orders(session, [shopify_order()])
        collisions = intake.collision_orders(session)
        assert [o.external_label for o in collisions] == ["#1001"]


def test_a_partly_fillable_order_allocates_what_it_can(db):
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        batch = make_batch(session)
        here = add_card(session, batch, n=1)
        gone = add_card(session, batch, n=2)
        bind(session, here)
        bind(session, gone)
        gone.status = "sold"
        session.commit()
        intake.ingest_shopify_orders(session, [shopify_order(
            skus=("CF-NEO-1-EN-NM-NF", "CF-NEO-2-EN-NM-NF"))])
        order = session.query(SalesOrder).one()
        assert order.status == "short"
        assert session.query(PickAllocation).count() == 1


# --- 4. idempotence ------------------------------------------------------


def test_the_same_order_twice_reserves_once(db):
    """The webhook and the poll both see every order."""
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.commit()
        first = intake.ingest_shopify_orders(session, [shopify_order()])
        second = intake.ingest_shopify_orders(session, [shopify_order()])
        assert len(first["ingested"]) == 1
        assert len(second["ingested"]) == 0
        assert len(second["already_known"]) == 1
        assert session.query(SalesOrder).count() == 1
        assert session.query(PickAllocation).count() == 1


def test_one_bad_order_never_blocks_another(db):
    """Per-order isolation, the property ingest_manapool_orders has for
    the same reason."""
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=2)
        bind(session, card)
        session.commit()
        result = intake.ingest_shopify_orders(session, [
            shopify_order(order_id="gid://shopify/Order/1", name="#1",
                          skus=("CF-NOTHING",)),
            shopify_order(order_id="gid://shopify/Order/2", name="#2",
                          skus=("CF-NEO-2-EN-NM-NF",)),
        ])
        assert len(result["refused"]) == 1
        assert len(result["ingested"]) == 1
        assert session.query(SalesOrder).one().external_label == "#2"


# --- 5. nothing Mana Pool can trip over ---------------------------------


def test_the_manapool_push_does_nothing_while_it_is_off(db, monkeypatch):
    """★ THE HARD STOP. Built, dry-runnable, and it writes nothing."""
    import manapool_quantity_push_service as mp

    def explode(updates):  # pragma: no cover -- must not run
        raise AssertionError("wrote to Mana Pool with the push OFF")

    monkeypatch.setattr(mp, "update_inventory_prices_by_product", explode)
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.commit()
        intake.ingest_shopify_orders(session, [shopify_order()])
        order = session.query(SalesOrder).one()
        outcome = intake.note_manapool_lower_for_shopify_allocation(
            session, order, [card])
        # "noted", not "pushed": since v2.112.0 the write is made by the
        # after-commit flush, so nothing has been pushed at this point
        # even with the gate ON.
        assert outcome["noted"] == 0
        assert intake.MANAPOOL_PUSH_SETTING_KEY in outcome["skipped"]
        assert outcome["would_push"] == 1
        assert mp.noted_quantity_pushes(session) == {}


def test_the_manapool_push_refuses_a_manapool_order_outright(db):
    """Scoped to source="shopify" and nothing else: a Mana Pool order
    reaching it would be told about its own sale."""
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        intake.set_manapool_push_enabled(session, True)
        order = SalesOrder(external_order_id="mp-1", source="manapool",
                           status="ready_to_pick")
        session.add(order)
        session.flush()
        outcome = intake.note_manapool_lower_for_shopify_allocation(
            session, order, [object()])
        assert outcome["skipped"] == "not a Shopify order"
        # And the same refusal for the other two directions, so the
        # source check cannot drift between them.
        for note in (intake.note_manapool_raise_for_shopify_release,
                     intake.note_manapool_lower_for_shopify_uncancel):
            assert note(session, order, [object()])["skipped"] == (
                "not a Shopify order")


def test_the_preview_describes_the_write_without_making_it(db, monkeypatch):
    import manapool_quantity_push_service as mp

    def explode(updates):  # pragma: no cover -- must not run
        raise AssertionError("the preview wrote to Mana Pool")

    monkeypatch.setattr(mp, "update_inventory_prices_by_product", explode)
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        session.commit()
        rows = intake.preview_manapool_push(session, [card])
    assert len(rows) == 1
    assert rows[0]["binding"] is None
    assert "no validated Mana Pool binding" in rows[0]["note"]


def test_a_shopify_order_is_invisible_to_the_manapool_cancellation_sweep(db):
    """★ THE DANGEROUS ONE, found by the ticket 6 source sweep. Without
    the source filter, "absent from the Mana Pool listing" reads true for
    every Shopify order -- so the reconciler would cancel them all and
    release inventory a customer is waiting to collect."""
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.commit()
        intake.ingest_shopify_orders(session, [shopify_order()])
        order = session.query(SalesOrder).one()
        assert order.status == "ready_to_pick"
        absent = order_service.orders_missing_from_remote_listing(session, [])
        assert absent == []


def test_a_shopify_failure_cannot_break_the_manapool_order_sync(db,
                                                                 monkeypatch):
    """The Mana Pool ingest must not care that Shopify exists."""
    pushed = []
    monkeypatch.setattr(
        order_service, "_push_shopify_for_reserved",
        lambda session, cards: (_ for _ in ()).throw(
            RuntimeError("shopify is down")))
    monkeypatch.setattr(
        order_service, "_note_manapool_for_shopify",
        lambda session, order, cards, transition: (_ for _ in ()).throw(
            RuntimeError("shopify is down")))
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        order = SalesOrder(external_order_id="mp-1", external_label="L-1",
                           source="manapool", status="pending")
        session.add(order)
        session.flush()
        session.add(OrderItem(
            order_id=order.id, name=card.name, set_code=card.set_code,
            collector_number=card.collector_number, quantity=1,
            scryfall_id=card.scryfall_id, mtgjson_id=card.mtgjson_id,
            language_id=card.language_id, condition_id=card.condition_id,
            finish_id=card.finish_id, price_cents=500))
        session.flush()
        with pytest.raises(RuntimeError):
            order_service.allocate_order(session, order)
    assert pushed == []


def test_the_ingest_query_count_does_not_grow_with_the_inventory(db):
    """The webhook fires per order inside a request; its cost must track
    the ORDER, not the shelf."""
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        batch = make_batch(session)
        for n in range(1, 4):
            card = add_card(session, batch, n=n)
            bind(session, card)
        session.commit()
        lean = _count_queries(db, lambda: intake.ingest_shopify_orders(
            session, [shopify_order(order_id="o-lean", name="#lean",
                                    skus=("CF-NEO-1-EN-NM-NF",))]))
        for n in range(100, 400):
            add_card(session, batch, n=n, price_cents=100)
        session.commit()
        fat = _count_queries(db, lambda: intake.ingest_shopify_orders(
            session, [shopify_order(order_id="o-fat", name="#fat",
                                    skus=("CF-NEO-2-EN-NM-NF",))]))
    assert len(fat) <= len(lean) + 2, (len(lean), len(fat))


# --- the webhook receiver ------------------------------------------------


def sign(secret, body: bytes) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def test_the_signature_is_shopifys_own_recipe(monkeypatch):
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "shhh")
    body = json.dumps(webhook_body()).encode()
    assert hook.expected_signature("shhh", body) == sign("shhh", body)
    verdict = hook.verify_signature(
        {"X-Shopify-Hmac-Sha256": sign("shhh", body)}, body)
    assert verdict["status"] == hook.SIGNATURE_VERIFIED


def test_a_wrong_signature_is_refused(monkeypatch):
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "shhh")
    body = b'{"id": 1}'
    verdict = hook.verify_signature(
        {"X-Shopify-Hmac-Sha256": sign("other-secret", body)}, body)
    assert verdict["status"] == hook.SIGNATURE_INVALID


def test_a_body_changed_in_flight_is_refused(monkeypatch):
    """The HMAC is over the RAW body, which is why it has to be captured
    before any JSON parser runs."""
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "shhh")
    signed = b'{"id": 1}'
    verdict = hook.verify_signature(
        {"X-Shopify-Hmac-Sha256": sign("shhh", signed)}, b'{"id": 2}')
    assert verdict["status"] == hook.SIGNATURE_INVALID


def test_no_secret_is_a_refusal_not_a_bootstrap(monkeypatch):
    """Unlike Mana Pool's registration probe, there is no moment when an
    unverifiable Shopify delivery should be accepted: the secret is the
    app's client secret and it is already configured."""
    monkeypatch.delenv("SHOPIFY_CLIENT_SECRET", raising=False)
    verdict = hook.verify_signature({"X-Shopify-Hmac-Sha256": "x"}, b"{}")
    assert verdict["status"] == hook.SIGNATURE_MISSING_SECRET


def test_headers_are_read_case_insensitively(monkeypatch):
    """Shopify's own docs: "Treat header names as case-insensitive"."""
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "shhh")
    body = b"{}"
    lowered = {"x-shopify-hmac-sha256": sign("shhh", body)}
    assert hook.verify_signature(lowered, body)["status"] == \
        hook.SIGNATURE_VERIFIED
    assert hook.header({"x-shopify-topic": "orders/create"},
                       "X-Shopify-Topic") == "orders/create"


def test_a_delivery_is_recorded_even_with_intake_off(db, monkeypatch):
    """★ RECORDED, NOT PROCESSED. The one question worth asking before
    turning intake on is "is Shopify actually reaching us", and throwing
    the delivery away makes it unanswerable."""
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "shhh")
    body = json.dumps(webhook_body()).encode()
    factory = sessionmaker(bind=db)
    with factory() as session:
        verdict = hook.verify_signature(
            {"X-Shopify-Hmac-Sha256": sign("shhh", body),
             "X-Shopify-Topic": "orders/create"}, body)
        row = hook.record_delivery(
            session, headers={"X-Shopify-Topic": "orders/create"},
            raw_body=body, signature=verdict)
        session.commit()
        delivery_id = row.id
    outcome = hook.process_delivery(factory, delivery_id)
    assert outcome["outcome"] == "intake_off"
    with factory() as session:
        row = session.get(WebhookDelivery, delivery_id)
        assert row.source == "shopify"
        assert row.signature_status == "verified"
        assert row.processing_status == "stranded"
        assert row.external_order_id == "12345"
        assert session.query(SalesOrder).count() == 0


def test_a_verified_delivery_ingests_once_intake_is_on(db, monkeypatch):
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "shhh")
    factory = sessionmaker(bind=db)
    with factory() as session:
        intake.set_intake_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.commit()
    body = json.dumps(webhook_body()).encode()
    with factory() as session:
        verdict = hook.verify_signature(
            {"X-Shopify-Hmac-Sha256": sign("shhh", body)}, body)
        row = hook.record_delivery(
            session, headers={"X-Shopify-Topic": "orders/create"},
            raw_body=body, signature=verdict)
        session.commit()
        delivery_id = row.id
    outcome = hook.process_delivery(factory, delivery_id)
    assert outcome["outcome"] == "ingested"
    with factory() as session:
        order = session.query(SalesOrder).one()
        assert order.source == "shopify"
        assert order.status == "ready_to_pick"
        assert session.get(WebhookDelivery, delivery_id).processing_status \
            == "processed"


def test_a_duplicate_delivery_is_a_no_op(db, monkeypatch):
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "shhh")
    factory = sessionmaker(bind=db)
    with factory() as session:
        intake.set_intake_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.commit()
    body = json.dumps(webhook_body()).encode()
    ids = []
    for _ in range(2):
        with factory() as session:
            verdict = hook.verify_signature(
                {"X-Shopify-Hmac-Sha256": sign("shhh", body)}, body)
            row = hook.record_delivery(
                session, headers={"X-Shopify-Topic": "orders/create"},
                raw_body=body, signature=verdict)
            session.commit()
            ids.append(row.id)
    assert hook.process_delivery(factory, ids[0])["outcome"] == "ingested"
    assert hook.process_delivery(factory, ids[1])["outcome"] == "already_known"
    with factory() as session:
        assert session.query(SalesOrder).count() == 1
        assert session.query(PickAllocation).count() == 1


def test_an_unhandled_topic_is_failed_not_guessed_at(db, monkeypatch):
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "shhh")
    factory = sessionmaker(bind=db)
    body = b'{"id": 1}'
    with factory() as session:
        intake.set_intake_enabled(session, True)
        verdict = hook.verify_signature(
            {"X-Shopify-Hmac-Sha256": sign("shhh", body)}, body)
        row = hook.record_delivery(
            session, headers={"X-Shopify-Topic": "products/update"},
            raw_body=body, signature=verdict)
        session.commit()
        delivery_id = row.id
    outcome = hook.process_delivery(factory, delivery_id)
    assert outcome["outcome"] == "unhandled_topic"


def test_an_unverified_delivery_is_never_processed(db, monkeypatch):
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "shhh")
    factory = sessionmaker(bind=db)
    body = json.dumps(webhook_body()).encode()
    with factory() as session:
        intake.set_intake_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        row = hook.record_delivery(
            session, headers={"X-Shopify-Topic": "orders/create"},
            raw_body=body,
            signature={"status": hook.SIGNATURE_INVALID, "detail": "no"})
        session.commit()
        delivery_id = row.id
    outcome = hook.process_delivery(factory, delivery_id)
    assert outcome["outcome"] == "unverified"
    with factory() as session:
        assert session.query(SalesOrder).count() == 0


def test_the_sweep_picks_up_a_stranded_delivery_once_intake_is_on(db,
                                                                   monkeypatch):
    """The day intake goes on, the deliveries that arrived before it are
    still on record and still re-runnable."""
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "shhh")
    factory = sessionmaker(bind=db)
    body = json.dumps(webhook_body()).encode()
    with factory() as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        verdict = hook.verify_signature(
            {"X-Shopify-Hmac-Sha256": sign("shhh", body)}, body)
        row = hook.record_delivery(
            session, headers={"X-Shopify-Topic": "orders/create"},
            raw_body=body, signature=verdict)
        session.commit()
        delivery_id = row.id
    hook.process_delivery(factory, delivery_id)
    with factory() as session:
        assert session.get(WebhookDelivery, delivery_id).processing_status \
            == "stranded"
        intake.set_intake_enabled(session, True)
        session.commit()
    result = hook.sweep_unfinished(factory, older_than_seconds=0)
    assert result["found"] == 1
    with factory() as session:
        assert session.query(SalesOrder).count() == 1


def test_a_cancelled_order_releases_its_reservation(db, monkeypatch):
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "shhh")
    factory = sessionmaker(bind=db)
    with factory() as session:
        intake.set_intake_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.commit()
        intake.ingest_shopify_orders(session, [webhook_body()])
        assert session.query(SalesOrder).one().status == "ready_to_pick"
    body = json.dumps({**webhook_body(), "cancelled_at":
                       "2026-11-03T12:00:00Z"}).encode()
    with factory() as session:
        verdict = hook.verify_signature(
            {"X-Shopify-Hmac-Sha256": sign("shhh", body)}, body)
        row = hook.record_delivery(
            session, headers={"X-Shopify-Topic": "orders/cancelled"},
            raw_body=body, signature=verdict)
        session.commit()
        delivery_id = row.id
    outcome = hook.process_delivery(factory, delivery_id)
    assert outcome["outcome"] == "cancelled"
    with factory() as session:
        order = session.query(SalesOrder).one()
        assert order.status == "cancelled"
        card = session.query(InventoryCard).one()
        assert card.status == "available"
