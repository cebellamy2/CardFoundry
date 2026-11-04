"""Shopify ticket 9 (F5): a release RAISES Shopify's quantity.

Until v2.103.0 every path that took stock away pushed Shopify within a
second and every path that gave stock back pushed nothing at all. The
first real Shopify order proved what that costs: order #1001 was
cancelled, CardFoundry put the card back on the shelf, and Shopify went
on advertising zero of it. What restored the quantity that day was
Shopify's own `orderCancel(restock: true)`, not us -- so a release
CardFoundry starts by itself (a cancel here, an uncollected pickup, a
return to sellable) left the card available locally and invisible on
Shopify for ever.

What these pin, in the order that matters:

  1. A RAISE CAN NEVER COME FROM A ROLLED-BACK TRANSACTION. Two
     independent reasons, both tested: the note is discarded on
     rollback, AND the number is recomputed in a session of its own that
     cannot see uncommitted rows. The second is the one that matters --
     it means even a note that survived could only write what the
     database actually holds.
  2. THE REAL ROUTE RAISES. Driven through POST /orders/{id}/cancel on
     an order built by the real Shopify ingest, reading the rendered
     page and the write actually sent.
  3. A SHOPIFY FAILURE NEVER FAILS THE LOCAL CANCEL.
  4. THE CANCEL ROUTE'S QUERY COUNT IS PINNED, because the raise opens a
     second session and that is exactly the kind of thing that grows
     quietly.
  5. Q2: the duplicate-order warning counts only OPEN rows, so it goes
     quiet once the duplicate is cancelled instead of crying wolf hourly.
  6. Q3: the cancel confirm names the order's OWN marketplace.

No test touches the network: shopify_service's read and write are both
stubbed, and nothing here reaches a live endpoint.
"""
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import inventory_sync_service
import main
import manapool_quantity_push_service as manapool_push
import order_service
import shopify_order_service as intake
import shopify_publish_service
import shopify_quantity_push_service as qty
import shopify_service
from local_price_writeback_service import set_card_price
from models import (
    Base, Batch, ImportRecord, InventoryCard, PickAllocation, SalesOrder,
)
from shopify_binding_service import record_binding, retire_binding

LOCATION = "gid://shopify/Location/97412055105"
ALLOWED = "card-foundry-dev.myshopify.com"
SKU = "CF-MIR-1-EN-LP-NF"
ORDER_NUMERIC_ID = "7271829405761"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shopify-raise.db'}")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(shopify_service, "SHOPIFY_STORE_DOMAIN", ALLOWED)
    monkeypatch.setattr(shopify_service, "SHOPIFY_LOCATION_ID", "97412055105")
    monkeypatch.setattr(shopify_service, "SHOPIFY_CLIENT_ID", "id")
    monkeypatch.setattr(shopify_service, "SHOPIFY_CLIENT_SECRET", "secret")
    monkeypatch.setattr(shopify_service, "SHOPIFY_ACCESS_TOKEN", None)
    monkeypatch.setattr(shopify_service, "location_name",
                        lambda gid, client=None: "Shop location")
    return ALLOWED


class FakeShopifyStock:
    """Shopify's side of the push: one read, one write, both recorded.

    `available` is what Shopify says it has, and a successful write
    updates it -- so a second push in the same test sees the result of
    the first, which is how the "already correct" case gets exercised
    rather than asserted about.
    """

    def __init__(self, available=0):
        self.available = available
        self.reads = []
        self.writes = []
        self.explode = None

    def read(self, skus, location_gid, client=None):
        self.reads.append(sorted(skus))
        return {SKU: {"variant_gid": "gid://shopify/ProductVariant/1",
                      "product_gid": "gid://shopify/Product/1",
                      "product_status": "DRAFT",
                      "inventory_item_gid": "gid://shopify/InventoryItem/1",
                      "available_at_location": self.available}}

    def write(self, quantities, **kwargs):
        if self.explode is not None:
            raise self.explode
        self.writes.append([dict(row) for row in quantities])
        for row in quantities:
            self.available = row["quantity"]
        return {"adjustment_group": {"createdAt": "now"}, "user_errors": [],
                "stale": []}

    def install(self, monkeypatch):
        monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                            self.read)
        monkeypatch.setattr(shopify_service, "set_inventory_quantities",
                            self.write)
        return self


def make_card(session, *, n=1, code="A1", price_cents=65):
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
    card = InventoryCard(
        batch_id=batch.id, import_id=record.id, name=f"Card {n}",
        set_code="MIR", collector_number=str(n), scryfall_id=f"sf-{n}",
        mtgjson_id=f"mtg-{n}", language_id="EN", condition_id="LP",
        finish_id="NF", condition="LP", finish="normal", status="available",
    )
    session.add(card)
    session.flush()
    if price_cents is not None:
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


def webhook_body(*, order_id=ORDER_NUMERIC_ID, name="#1001", sku=SKU):
    return {
        "id": int(order_id), "name": name,
        "created_at": "2026-11-03T16:25:49Z",
        "line_items": [{"id": 1, "sku": sku, "quantity": 1,
                        "name": "Barreling Attack", "price": "0.65"}],
        "total_shipping_price_set": {
            "shop_money": {"amount": "8.00", "currency_code": "USD"}},
        "total_price_set": {
            "shop_money": {"amount": "8.65", "currency_code": "USD"}},
    }


def reserved_order(session, *, name="#1001", order_id=ORDER_NUMERIC_ID,
                   sku=SKU):
    """One Shopify order holding one card, built by the REAL ingest.

    Shopify quantity sync stays OFF through the ingest, so the card is
    reserved and Shopify is left advertising whatever the fixture says --
    which is exactly the state production was in: available 0, one card
    on the shelf, nothing to tell Shopify about it.
    """
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
    attaching to it by hand -- same as test_attention_scheduled_jobs."""
    import logging
    logger = logging.getLogger("cardfoundry")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="cardfoundry")
    yield caplog
    logger.removeHandler(caplog.handler)


# --- 1. a raise can never come from a rolled-back transaction ------------


def test_a_rolled_back_release_pushes_nothing(db, wired, monkeypatch):
    """★ THE PROPERTY THE WHOLE DESIGN EXISTS FOR. The note is discarded
    on rollback, so nothing is pushed at all."""
    shop = FakeShopifyStock(available=0).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        qty.set_sync_enabled(session, True)
        session.commit()

    with Session(db) as session:
        card = session.query(InventoryCard).one()
        card.status = "reserved"
        session.commit()

    with Session(db) as session:
        card = session.query(InventoryCard).one()
        card.status = "available"
        qty.note_raise(session, [card], origin="test-release")
        assert qty.noted_raises(session) == {SKU: "test-release"}
        session.rollback()
        assert qty.noted_raises(session) == {}
        # A later, unrelated commit on the same session must not revive it.
        session.commit()

    assert shop.writes == []
    assert shop.reads == []


def test_the_number_comes_from_committed_state_not_the_note(db, wired,
                                                            monkeypatch):
    """★ THE SECOND, STRONGER REASON. Even a note that somehow survived a
    rollback could only ever push what the database has COMMITTED: the
    flush opens its own session on the same bind, and a different
    connection cannot see another transaction's uncommitted rows.

    Here the caller's open transaction says the card is available and
    committed state still says reserved. The push must write 0, not 1."""
    shop = FakeShopifyStock(available=0).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        qty.set_sync_enabled(session, True)
        card.status = "reserved"
        session.commit()

    with Session(db) as session:
        card = session.query(InventoryCard).one()
        card.status = "available"
        session.flush()
        # Deliberately NOT committed. The push is called directly, the
        # way the after_commit flush calls it.
        result = qty.push_committed_for_skus(db, [SKU], origin="test-uncommitted")
        session.rollback()

    assert result["raised"] == []
    # Shopify already says 0 and committed state says 0, so there is
    # nothing to write -- the uncommitted 1 was invisible, which is the
    # whole point.
    assert shop.writes == []
    assert shop.available == 0


def test_a_committed_release_does_raise(db, wired, monkeypatch):
    """The same call, after the commit: 0 -> 1."""
    shop = FakeShopifyStock(available=0).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        qty.set_sync_enabled(session, True)
        card.status = "reserved"
        session.commit()
        card.status = "available"
        session.commit()

    result = qty.push_committed_for_skus(db, [SKU], origin="test-committed")
    assert [row["sku"] for row in result["raised"]] == [SKU]
    assert result["raised"][0] == {"sku": SKU, "shopify_has": 0,
                                   "local_says": 1}
    assert shop.available == 1
    assert len(shop.writes) == 1
    assert shop.writes[0][0]["quantity"] == 1
    # A compare-and-swap on what was just read, not a blind overwrite.
    assert shop.writes[0][0]["changeFromQuantity"] == 0


def test_the_flush_fires_on_commit_without_anybody_calling_it(db, wired,
                                                              monkeypatch):
    """The note is one line next to the status write; SQLAlchemy's own
    after_commit is what makes it impossible to forget."""
    shop = FakeShopifyStock(available=0).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        qty.set_sync_enabled(session, True)
        card.status = "reserved"
        session.commit()

    with Session(db) as session:
        card = session.query(InventoryCard).one()
        card.status = "available"
        qty.note_raise(session, [card], origin="test-auto")
        session.commit()

    assert shop.available == 1
    assert qty.noted_raises(session) == {}


def test_sync_off_pushes_nothing_at_all(db, wired, monkeypatch):
    """Off by default and it fails closed: not one Shopify call."""
    shop = FakeShopifyStock(available=0).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        card.status = "reserved"
        session.commit()
        card.status = "available"
        qty.note_raise(session, [card], origin="test-off")
        session.commit()
    assert shop.reads == []
    assert shop.writes == []


# --- 2. the real cancel route --------------------------------------------


def test_cancelling_a_shopify_order_raises_shopify(db, wired, client,
                                                    monkeypatch, logs):
    """★ THE PRODUCTION BUG, END TO END. Order #1001's shape exactly:
    Shopify advertising 0, one card reserved, the order cancelled from
    its own page. The card comes back and Shopify is told."""
    shop = FakeShopifyStock(available=0).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        order = reserved_order(session)
        order_id, card_id = order.id, card.id
        qty.set_sync_enabled(session, True)
        session.commit()

    response = client.post(f"/orders/{order_id}/cancel",
                           data={"cancel_reason": "other",
                                 "cancel_note": "duplicate row"},
                           follow_redirects=False)
    assert response.status_code == 303

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "cancelled"
        assert session.get(InventoryCard, card_id).status == "available"
    assert shop.available == 1
    assert [row["quantity"] for row in shop.writes[-1]] == [1]
    assert "RAISED" in logs.text


def test_a_not_collected_restock_raises_shopify(db, wired, client,
                                                 monkeypatch):
    """The uncollected-pickup path reaches the same release, so it gets
    the same raise without a second mechanism."""
    shop = FakeShopifyStock(available=0).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        order = reserved_order(session)
        order_id, card_id = order.id, card.id
        qty.set_sync_enabled(session, True)
        session.commit()

    with Session(db) as session:
        order = session.get(SalesOrder, order_id)
        card = session.get(InventoryCard, card_id)
        allocation = session.query(PickAllocation).filter_by(
            inventory_card_id=card_id).one()
        allocation.status = "shipped"
        card.status = "sold"
        order.status = "shipped"
        order.shipped_at = order.created_at
        session.commit()

    response = client.post(f"/orders/{order_id}/not-collected",
                           data={"cancel_note": "carrier never came"},
                           follow_redirects=False)
    assert response.status_code == 303
    with Session(db) as session:
        assert session.get(InventoryCard, card_id).status == "available"
    assert shop.available == 1


@pytest.mark.parametrize("boom", [
    RuntimeError("shopify exploded"),
    httpx.TimeoutException("too slow"),
    TypeError("a programming error in code written later"),
    KeyError("a dict key nobody checked"),
])
def test_a_shopify_failure_never_fails_the_cancel(db, wired, client,
                                                   monkeypatch, boom, logs):
    """The raise runs inside after_commit, so an exception there would
    come out of session.commit() and fail an action that has already
    succeeded on disk. Nothing may escape."""
    shop = FakeShopifyStock(available=0).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        order = reserved_order(session)
        order_id, card_id = order.id, card.id
        qty.set_sync_enabled(session, True)
        session.commit()
    shop.explode = boom

    response = client.post(f"/orders/{order_id}/cancel",
                           data={"cancel_reason": "other", "cancel_note": ""},
                           follow_redirects=False)
    assert response.status_code == 303
    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "cancelled"
        assert session.get(InventoryCard, card_id).status == "available"
    assert "swallowed" in logs.text


def test_a_cancel_with_no_live_shopify_binding_pushes_nothing(db, wired,
                                                               client,
                                                               monkeypatch):
    """A card Shopify no longer lists must not start making Shopify calls
    on every cancellation. Retired through the real service, because a
    retired binding is the only way a reserved Shopify order can end up
    with no listing -- the ingest refuses an unbound SKU outright."""
    shop = FakeShopifyStock(available=0).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        binding = bind(session, card)
        order = reserved_order(session)
        order_id = order.id
        qty.set_sync_enabled(session, True)
        retire_binding(session, binding, reason="test")
        session.commit()
    client.post(f"/orders/{order_id}/cancel",
                data={"cancel_reason": "other", "cancel_note": ""},
                follow_redirects=False)
    assert shop.reads == []
    assert shop.writes == []


# --- 3. the cancel route's query count -----------------------------------


def test_the_cancel_route_statement_count_is_pinned(db, wired, client,
                                                     monkeypatch):
    """★ THE RAISE OPENS A SECOND SESSION, which is exactly the kind of
    thing that grows quietly. Pinned with sync ON, so the count includes
    the flush's own reads."""
    FakeShopifyStock(available=0).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        order = reserved_order(session)
        order_id = order.id
        qty.set_sync_enabled(session, True)
        session.commit()

    statements = _count_statements(db, lambda: client.post(
        f"/orders/{order_id}/cancel",
        data={"cancel_reason": "other", "cancel_note": ""},
        follow_redirects=False))
    # Exact, not an upper bound: a bound that is comfortable today is a
    # bound that absorbs the next N+1 without saying anything.
    assert len(statements) == 25, len(statements)


def test_a_cancel_with_sync_off_costs_eight_fewer(db, wired, client,
                                                   monkeypatch):
    """With the push off, the whole mechanism is a dict lookup plus the
    one setting read the flush makes before it gives up -- so the eight
    statements the push itself costs are not paid at all."""
    FakeShopifyStock(available=0).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        order = reserved_order(session)
        order_id = order.id
        session.commit()

    statements = _count_statements(db, lambda: client.post(
        f"/orders/{order_id}/cancel",
        data={"cancel_reason": "other", "cancel_note": ""},
        follow_redirects=False))
    assert len(statements) == 17, len(statements)


# --- 4. the paths Mana Pool declines -------------------------------------


def test_a_card_manapool_has_never_listed_still_raises_shopify(
        db, wired, monkeypatch):
    """★ SHOPIFY DOES NOT SHARE MANA POOL'S REASONS. push_return_to_sellable
    skips a card Mana Pool has never listed, and that card reached
    Shopify's quantity push on NO path at all -- so returning it to
    sellable left Shopify advertising the old number for ever. The card
    here has a local price (so Shopify would publish it) and no Mana Pool
    binding, which is the commonest shape of the gap."""
    shop = FakeShopifyStock(available=0).install(monkeypatch)
    monkeypatch.setattr(manapool_push, "update_inventory_prices_by_product",
                        lambda updates: None)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        qty.set_sync_enabled(session, True)
        session.commit()
        outcomes = manapool_push.push_return_to_sellable(session, [card])
        session.commit()
    # Mana Pool declined it, and Shopify was told anyway.
    assert [row["outcome"] for row in outcomes] == ["never_listed"]
    assert shop.available == 1


# --- 5. Q2: the duplicate warning counts only OPEN rows ------------------


def _duplicate_rows(session, *, second_status="ready_to_pick"):
    canonical = intake.canonical_external_order_id(ORDER_NUMERIC_ID)
    first = session.query(SalesOrder).filter_by(
        external_order_id=canonical).one()
    second = SalesOrder(
        source=intake.SOURCE, external_order_id=intake.order_gid(canonical),
        external_label="#1001", status=second_status,
        created_at=first.created_at,
    )
    session.add(second)
    session.flush()
    return first, second


def test_two_open_rows_are_still_reported(db, wired, logs):
    with Session(db) as session:
        bind(session, make_card(session))
        reserved_order(session)
        _duplicate_rows(session)
        session.commit()
        found = intake.existing_order(session, ORDER_NUMERIC_ID)
    assert "2 OPEN local orders" in logs.text
    assert "reserving stock more than once" in logs.text
    assert found is not None


@pytest.mark.parametrize("settled", intake.SETTLED_STATUSES)
def test_a_settled_duplicate_is_silent(db, wired, logs, settled):
    """★ AN ALARM THAT SURVIVES ITS OWN CURE. v2.101.0 counted every row,
    so the moment the operator cancelled the duplicate the warning went
    on firing hourly, naming a row that no longer held anything."""
    with Session(db) as session:
        bind(session, make_card(session))
        reserved_order(session)
        _duplicate_rows(session, second_status=settled)
        session.commit()
        found = intake.existing_order(session, ORDER_NUMERIC_ID)
    assert "local orders" not in logs.text
    # Still found, and still the earliest row: the settled duplicate is
    # not reported, not hidden.
    assert found is not None
    assert found.status not in intake.SETTLED_STATUSES


def test_the_attention_page_and_the_warning_share_one_settled_set():
    """Two readers, one list of words, so they cannot drift."""
    import attention_service
    assert (attention_service._SHOPIFY_SETTLED_STATUSES
            is intake.SETTLED_STATUSES)


# --- 6. Q3: the confirm names the order's own marketplace ----------------


@pytest.mark.parametrize("source,expected,absent", [
    ("shopify", "Shopify is NOT told", "Mana Pool"),
    ("manapool", "Mana Pool is NOT told", "Shopify is NOT told"),
])
def test_the_cancel_confirm_names_the_right_marketplace(db, wired, client,
                                                         source, expected,
                                                         absent):
    """★ THE WORDING WART THE OPERATOR HIT. Every cancel confirm named
    Mana Pool, because until the Shopify channel existed every order came
    from there -- so cancelling a Shopify order sent them to the wrong
    shop to refund a buyer."""
    with Session(db) as session:
        bind(session, make_card(session))
        order = reserved_order(session)
        order.source = source
        order_id = order.id
        session.commit()

    page = client.get(f"/orders/{order_id}").text
    assert expected in page
    assert absent not in page.split("Reason for cancelling")[0][-600:]


def test_an_order_from_no_marketplace_names_none(db, wired, client):
    """Production holds no manual order today, which is exactly why the
    bucket has to be handled rather than assumed away."""
    with Session(db) as session:
        bind(session, make_card(session))
        order = reserved_order(session)
        order.source = "manual"
        order_id = order.id
        session.commit()
    page = client.get(f"/orders/{order_id}").text
    assert "came from no marketplace" in page


def test_the_marketplace_name_never_guesses():
    assert order_service.cancellation_marketplace(None) == \
        order_service.CANCEL_MARKETPLACE_NONE
    assert order_service.cancellation_marketplace_note(
        None, lead="X.").startswith("X. This order came from no marketplace")


# --- 7. the dry run of the raise -----------------------------------------


def test_the_dry_run_builds_the_real_payload_and_sends_nothing(db, wired,
                                                                monkeypatch):
    """★ A DRY RUN RUNS THE SAME CODE PATH AS THE WRITE. The only thing
    replaced is the outbound HTTP call, so authorize_writes runs, the
    idempotency key is built and the compare-and-swap value is the one
    Shopify just answered -- and the store hears nothing."""
    import dry_run_shopify_quantity_raise as preview

    shop = FakeShopifyStock(available=0).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        qty.set_sync_enabled(session, True)
        card.status = "reserved"
        session.commit()
        card.status = "available"
        session.commit()

    with preview.capture_shopify_writes() as sent:
        result = qty.push_committed_for_skus(db, [SKU], origin="preview")

    # Recorded in full...
    assert len(sent) == 1
    assert sent[0]["quantities"][0]["quantity"] == 1
    assert sent[0]["quantities"][0]["changeFromQuantity"] == 0
    assert sent[0]["idempotency_key"]
    assert sent[0]["reference_document_uri"].startswith(
        qty.REFERENCE_DOCUMENT_PREFIX)
    # ...and the store never heard it. The read DID happen, deliberately:
    # the whole question is what Shopify says it holds today.
    assert shop.writes == []
    assert shop.available == 0
    assert shop.reads == [[SKU]]
    assert [row["sku"] for row in result["raised"]] == [SKU]


def test_the_dry_run_restores_the_real_write_afterwards(wired, monkeypatch):
    """A preview that left the recorder in place would make the next
    apply in the same process silently do nothing."""
    import dry_run_shopify_quantity_raise as preview

    before = shopify_service.set_inventory_quantities
    with preview.capture_shopify_writes():
        assert shopify_service.set_inventory_quantities is not before
    assert shopify_service.set_inventory_quantities is before


def test_the_dry_run_restores_the_write_even_when_the_push_raises(wired):
    import dry_run_shopify_quantity_raise as preview

    before = shopify_service.set_inventory_quantities
    with pytest.raises(RuntimeError):
        with preview.capture_shopify_writes():
            raise RuntimeError("boom")
    assert shopify_service.set_inventory_quantities is before
