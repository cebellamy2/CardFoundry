"""Shopify ticket 5: the quantity push, and the promise around it.

What these pin, in the order that matters:

  1. A SHOPIFY FAILURE CANNOT TOUCH MANA POOL. Every way Shopify can go
     wrong -- an exception, an import failure, a timeout, a programming
     error -- is driven through the real Mana Pool push and asserted not
     to change what it did.
  2. IT IS OFF BY DEFAULT and the default fails closed.
  3. THE QUANTITY IS THE PUBLISH'S OWN RULE: a card the publish would
     not publish does not count toward what Shopify advertises.
  4. The write is a compare-and-swap, a stale compare is answered by
     READING AGAIN (not by retrying the same numbers), and a timeout is
     UNCERTAIN and never retried.
  5. Reconciliation names the dangerous direction -- Shopify advertising
     more than the shop holds -- separately from drift in general.

No test touches the network.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import manapool_quantity_push_service as manapool_push
import shopify_publish_service
import shopify_quantity_push_service as qty
import shopify_service
from fulfillment_exception_service import mark_fulfillment_exception
from local_price_writeback_service import set_card_price
from models import (
    AppSetting, AppSettingChangeLog, Base, Batch, ImportRecord, InventoryCard,
    OrderItem, PickAllocation, RemoteProductBinding, SalesOrder,
)
from shopify_binding_service import record_binding, retire_binding

LOCATION = "gid://shopify/Location/97412055105"
ALLOWED = "card-foundry-dev.myshopify.com"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shopify-qty.db'}")
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


def bind(session, card, *, sku=None, item="gid://shopify/InventoryItem/1"):
    return record_binding(
        session, sku=sku or f"CF-NEO-{card.collector_number}-EN-NM-NF",
        set_code=card.set_code, collector_number=card.collector_number,
        language_id=card.language_id, condition_id=card.condition_id,
        finish_id=card.finish_id,
        product_gid="gid://shopify/Product/1",
        variant_gid="gid://shopify/ProductVariant/1",
        inventory_item_gid=item,
    )


def stock(sku, available, *, item="gid://shopify/InventoryItem/1"):
    return {sku: {"variant_gid": "gid://shopify/ProductVariant/1",
                  "product_gid": "gid://shopify/Product/1",
                  "product_status": "DRAFT",
                  "inventory_item_gid": item,
                  "available_at_location": available}}


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


# --- 1. Mana Pool is untouchable -----------------------------------------


def _manapool_binding(session, card):
    binding = RemoteProductBinding(
        provider="manapool", product_type="mtg_single", product_id="mp-1",
        local_card_ids_json=json.dumps([card.id]),
        requested_identity_json="{}", scryfall_id=card.scryfall_id,
        mtgjson_id=card.mtgjson_id, language_id=card.language_id,
        condition_id=card.condition_id, finish_id=card.finish_id,
        set_code=card.set_code, collector_number=card.collector_number,
        binding_status="validated", validated_at=__import__("datetime").datetime.now(),
        evidence_hash=f"h-{card.id}", evidence_json="{}",
    )
    session.add(binding)
    session.flush()
    return binding


@pytest.mark.parametrize("boom", [
    RuntimeError("shopify exploded"),
    httpx.TimeoutException("too slow"),
    ImportError("no such module"),
    TypeError("a programming error in code written later"),
    KeyError("a dict key nobody checked"),
])
def test_a_shopify_failure_never_breaks_the_manapool_push(db, monkeypatch,
                                                           boom):
    """★ THE OPERATOR'S HARD REQUIREMENT. Mana Pool behaviour must not
    change at all, so EVERY way Shopify can fail is driven through the
    real push and asserted harmless -- including the ones nobody thought
    of, which is why the clause is bare."""
    pushed = []
    monkeypatch.setattr(manapool_push, "update_inventory_prices_by_product",
                        lambda updates: pushed.append(updates))

    def explode(session, cards):
        raise boom

    monkeypatch.setattr(qty, "push_for_cards", explode)
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch)
        _manapool_binding(session, card)
        card.status = "sold"
        session.flush()
        manapool_push.push_for_cards(session, [card])  # must not raise
        session.commit()
    assert len(pushed) == 1
    assert pushed[0][0]["product_id"] == "mp-1"
    assert pushed[0][0]["quantity"] == 0


def test_the_manapool_push_still_records_its_own_success(db, monkeypatch):
    """A Shopify hook that runs after must not disturb what Mana Pool
    already wrote on its binding."""
    monkeypatch.setattr(manapool_push, "update_inventory_prices_by_product",
                        lambda updates: None)
    monkeypatch.setattr(qty, "push_for_cards",
                        lambda session, cards: (_ for _ in ()).throw(
                            RuntimeError("shopify down")))
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch)
        binding = _manapool_binding(session, card)
        card.status = "sold"
        session.flush()
        manapool_push.push_for_cards(session, [card])
        session.commit()
        refreshed = session.get(RemoteProductBinding, binding.id)
        assert refreshed.last_quantity_push_attempted_at is not None
        assert refreshed.last_quantity_push_failure_detail is None


def test_the_shopify_hook_is_reached_from_the_manapool_push(db, monkeypatch):
    """The wiring itself: one hook, so all nine call sites are covered."""
    monkeypatch.setattr(manapool_push, "update_inventory_prices_by_product",
                        lambda updates: None)
    seen = []
    monkeypatch.setattr(qty, "push_for_cards",
                        lambda session, cards: seen.append(
                            [c.id for c in cards]) or {"pushed": 0})
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch)
        _manapool_binding(session, card)
        manapool_push.push_for_cards(session, [card])
    assert seen == [[card.id]]


# --- 2. off by default ---------------------------------------------------


def test_the_sync_is_off_with_no_setting_row(db):
    with Session(db) as session:
        assert qty.sync_enabled(session) is False


def test_the_hook_does_nothing_at_all_while_it_is_off(db, wired, monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover -- must not run
        raise AssertionError("pushed while the sync was off")

    monkeypatch.setattr(qty, "push_bindings", explode)
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch)
        bind(session, card)
        session.flush()
        outcome = qty.push_for_cards(session, [card])
    assert outcome["pushed"] == 0
    assert "off" in outcome["skipped"]


def test_a_nonsense_setting_value_reads_as_off(db):
    with Session(db) as session:
        session.add(AppSetting(key=qty.SETTING_KEY, value="maybe"))
        session.flush()
        assert qty.sync_enabled(session) is False


@pytest.mark.parametrize("stored,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("on", True), ("yes", True),
    ("false", False), ("0", False), ("off", False), ("no", False), ("", False),
])
def test_the_setting_reads_the_obvious_spellings(db, stored, expected):
    with Session(db) as session:
        session.add(AppSetting(key=qty.SETTING_KEY, value=stored))
        session.flush()
        assert qty.sync_enabled(session) is expected


def test_turning_the_sync_on_is_audited(db):
    with Session(db) as session:
        result = qty.set_sync_enabled(session, True)
        session.flush()
        assert result == {"before": False, "after": True, "changed": True}
        assert qty.sync_enabled(session) is True
        log = session.query(AppSettingChangeLog).one()
        entry = json.loads(log.change_summary)
        assert entry["setting"] == qty.SETTING_KEY
        assert entry["before"] is False and entry["after"] is True
        assert log.actor


def test_setting_it_to_what_it_already_is_writes_no_audit_row(db):
    with Session(db) as session:
        qty.set_sync_enabled(session, False)
        session.flush()
        assert session.query(AppSettingChangeLog).count() == 0


# --- 3. the quantity is the publish's rule -------------------------------


def test_the_quantity_counts_available_cards(db, wired):
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        add_card(session, batch, n=1)
        binding = bind(session, card)
        session.flush()
        assert qty.desired_quantity(session, binding) == 2


def test_a_sold_card_does_not_count(db, wired):
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        gone = add_card(session, batch, n=1)
        binding = bind(session, card)
        gone.status = "sold"
        session.flush()
        assert qty.desired_quantity(session, binding) == 1


def test_a_price_held_card_does_not_count(db, wired):
    """It would not be published, so it must not be advertised."""
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        binding = bind(session, card)
        card.price_pending_since = __import__("datetime").datetime.now()
        session.flush()
        assert qty.desired_quantity(session, binding) == 0


def test_a_card_in_an_archived_batch_does_not_count(db, wired):
    with Session(db) as session:
        batch = make_batch(session, is_archived=True)
        card = add_card(session, batch, n=1)
        binding = bind(session, card)
        session.flush()
        assert qty.desired_quantity(session, binding) == 0


def reserve(session, card, *, status="allocated"):
    """A real reservation, through the shapes the real services demand.

    Every field mark_fulfillment_exception's identity check compares,
    scryfall_id included -- the service refuses a line that does not
    match the physical card on all eight -- and the card has to read
    `reserved`, or it refuses that too.
    """
    order = SalesOrder(external_order_id=f"o-{card.id}",
                       external_label=f"L-{card.id}",
                       source="manapool", status="ready_to_pick")
    session.add(order)
    session.flush()
    item = OrderItem(order_id=order.id, name=card.name,
                     set_code=card.set_code,
                     collector_number=card.collector_number, quantity=1,
                     scryfall_id=card.scryfall_id,
                     mtgjson_id=card.mtgjson_id,
                     language_id=card.language_id,
                     condition_id=card.condition_id,
                     finish_id=card.finish_id, price_cents=500)
    session.add(item)
    session.flush()
    allocation = PickAllocation(order_item_id=item.id,
                                inventory_card_id=card.id,
                                batch_id=card.batch_id, status=status)
    session.add(allocation)
    card.status = "reserved"
    session.flush()
    return allocation


def test_a_card_with_an_open_exception_does_not_count(db, wired):
    """★ THROUGH THE REAL SERVICE. A hand-built exception row passes
    while the real path refuses, and it is the exception's own card
    projection -- the card returning to `available` carrying
    exception_unresolved -- that makes this interesting at all: by
    status alone the card looks sellable."""
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        binding = bind(session, card)
        allocation = reserve(session, card)
        mark_fulfillment_exception(session, allocation.id, "missing",
                                   note="not on the shelf")
        session.flush()
        assert card.inventory_exception_state == "exception_unresolved"
        assert qty.desired_quantity(session, binding) == 0


def test_an_available_card_held_by_an_active_allocation_does_not_count(db,
                                                                       wired):
    """The mirror's own invalid-state check: a card reading `available`
    while an allocation actively holds it is a quantity nothing
    downstream can trust."""
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        binding = bind(session, card)
        reserve(session, card)
        card.status = "available"
        session.flush()
        assert qty.desired_quantity(session, binding) == 0


def test_the_one_pass_and_scoped_counts_agree(db, wired):
    """Two scopes, one rule. If they ever disagree, the quantity on a
    product and the existence of that product are deciding separately."""
    with Session(db) as session:
        batch = make_batch(session)
        bindings = []
        for n in range(1, 40):
            card = add_card(session, batch, n=n)
            bindings.append(bind(session, card,
                                 sku=f"CF-NEO-{n}-EN-NM-NF"))
        add_card(session, batch, n=7)
        session.flush()
        one_pass = qty.desired_quantities(session, bindings)
        scoped = {b.sku: qty.desired_quantity(session, b) for b in bindings}
    assert len(bindings) > qty.ONE_PASS_THRESHOLD
    assert one_pass == scoped
    assert scoped["CF-NEO-7-EN-NM-NF"] == 2


def test_resolving_bindings_for_cards_is_one_query_per_chunk(db, wired):
    """The sale path runs inside a web request; it must not read the
    whole bindings table to find three rows."""
    with Session(db) as session:
        batch = make_batch(session)
        cards = []
        for n in range(1, 30):
            card = add_card(session, batch, n=n)
            bind(session, card, sku=f"CF-NEO-{n}-EN-NM-NF")
            cards.append(card)
        session.flush()
        few = _count_queries(db, lambda: qty.bindings_for_cards(
            session, cards[:2]))
        many = _count_queries(db, lambda: qty.bindings_for_cards(
            session, cards))
    assert len(few) == len(many) == 1


def test_a_retired_binding_is_not_pushed(db, wired):
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        binding = bind(session, card)
        retire_binding(session, binding, reason="printing corrected")
        session.flush()
        assert qty.bindings_for_cards(session, [card]) == []


# --- 4. the write --------------------------------------------------------


def test_nothing_is_sent_without_confirm(db, wired, monkeypatch):
    monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                        lambda skus, location_gid, client=None: stock(
                            "CF-NEO-1-EN-NM-NF", 5))

    def explode(*args, **kwargs):  # pragma: no cover -- must not run
        raise AssertionError("a dry run wrote to Shopify")

    monkeypatch.setattr(shopify_service, "set_inventory_quantities", explode)
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        binding = bind(session, card)
        session.flush()
        report = qty.push_bindings(session, [binding], confirm=False)
    assert "--confirm" in report["stopped_because"]
    assert report["would_change"] == [
        {"sku": "CF-NEO-1-EN-NM-NF", "shopify_has": 5, "local_says": 1}]
    assert report["changed"] == []


def test_the_write_is_a_compare_and_swap_on_what_was_just_read(db, wired,
                                                                monkeypatch):
    """changeFromQuantity is "mandatory: you must explicitly pass in a
    value"; passing the value just read is what turns a blind overwrite
    into a detectable race."""
    sent = []
    monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                        lambda skus, location_gid, client=None: stock(
                            "CF-NEO-1-EN-NM-NF", 5))

    def capture(quantities, **kwargs):
        sent.append((quantities, kwargs))
        return {"adjustment_group": {"createdAt": "now"}, "user_errors": [],
                "stale": []}

    monkeypatch.setattr(shopify_service, "set_inventory_quantities", capture)
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        binding = bind(session, card)
        session.flush()
        report = qty.push_bindings(session, [binding], confirm=True)
    quantities, kwargs = sent[0]
    assert quantities == [{
        "inventoryItemId": "gid://shopify/InventoryItem/1",
        "locationId": LOCATION, "quantity": 1, "changeFromQuantity": 5}]
    assert kwargs["reference_document_uri"].startswith(
        qty.REFERENCE_DOCUMENT_PREFIX)
    assert kwargs["idempotency_key"]
    assert report["changed"] == ["CF-NEO-1-EN-NM-NF"]


def test_a_variant_that_already_matches_is_not_written(db, wired,
                                                        monkeypatch):
    monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                        lambda skus, location_gid, client=None: stock(
                            "CF-NEO-1-EN-NM-NF", 1))

    def explode(*args, **kwargs):  # pragma: no cover -- must not run
        raise AssertionError("wrote a quantity that was already correct")

    monkeypatch.setattr(shopify_service, "set_inventory_quantities", explode)
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        binding = bind(session, card)
        session.flush()
        report = qty.push_bindings(session, [binding], confirm=True)
    assert report["already_correct"] == ["CF-NEO-1-EN-NM-NF"]
    assert "already matches" in report["stopped_because"]


def test_a_stale_compare_is_answered_by_reading_again(db, wired, monkeypatch):
    """Not by retrying the same numbers. Someone else moved the quantity,
    so the fresh value is what the next attempt compares against."""
    reads = []
    attempts = []

    def read(skus, location_gid, client=None):
        reads.append(list(skus))
        return stock("CF-NEO-1-EN-NM-NF", 5 if len(reads) == 1 else 9)

    def write(quantities, **kwargs):
        attempts.append(quantities[0]["changeFromQuantity"])
        if len(attempts) == 1:
            return {"adjustment_group": None, "stale": [
                {"code": "CHANGE_FROM_QUANTITY_STALE",
                 "field": ["quantities", "0", "changeFromQuantity"],
                 "message": "stale"}],
                "user_errors": [
                    {"code": "CHANGE_FROM_QUANTITY_STALE",
                     "field": ["quantities", "0", "changeFromQuantity"],
                     "message": "stale"}]}
        return {"adjustment_group": {}, "user_errors": [], "stale": []}

    monkeypatch.setattr(shopify_publish_service, "read_variant_stock", read)
    monkeypatch.setattr(shopify_service, "set_inventory_quantities", write)
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        binding = bind(session, card)
        session.flush()
        report = qty.push_bindings(session, [binding], confirm=True)
    assert attempts == [5, 9]
    assert len(reads) == 2
    assert report["changed"] == ["CF-NEO-1-EN-NM-NF"]
    assert report["stale_after_retry"] == []


def test_a_second_staleness_is_reported_not_raced(db, wired, monkeypatch):
    attempts = []

    def write(quantities, **kwargs):
        attempts.append(1)
        stale = [{"code": "CHANGE_FROM_QUANTITY_STALE", "field": [],
                  "message": "stale"}]
        return {"adjustment_group": None, "stale": stale,
                "user_errors": stale}

    monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                        lambda skus, location_gid, client=None: stock(
                            "CF-NEO-1-EN-NM-NF", 5))
    monkeypatch.setattr(shopify_service, "set_inventory_quantities", write)
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        binding = bind(session, card)
        session.flush()
        report = qty.push_bindings(session, [binding], confirm=True)
    assert len(attempts) == qty.STALE_RETRIES + 1
    assert report["stale_after_retry"] == ["CF-NEO-1-EN-NM-NF"]
    assert report["changed"] == []


def test_a_timed_out_write_is_uncertain_and_never_retried(db, wired,
                                                           monkeypatch):
    attempts = []

    def write(quantities, **kwargs):
        attempts.append(1)
        raise shopify_service.ShopifyWriteUncertain("timed out")

    monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                        lambda skus, location_gid, client=None: stock(
                            "CF-NEO-1-EN-NM-NF", 5))
    monkeypatch.setattr(shopify_service, "set_inventory_quantities", write)
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        binding = bind(session, card)
        session.flush()
        report = qty.push_bindings(session, [binding], confirm=True)
    assert len(attempts) == 1
    assert report["errors"][0]["uncertain"] is True
    assert report["changed"] == []


def test_a_binding_with_no_inventory_item_is_reported_unbound(db, wired):
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        binding = record_binding(
            session, sku="CF-NEO-1-EN-NM-NF", set_code="NEO",
            collector_number="1", language_id="EN", condition_id="NM",
            finish_id="NF", product_gid="gid://shopify/Product/1")
        session.flush()
        report = qty.push_bindings(session, [binding], confirm=False)
    assert report["unbound"] == ["CF-NEO-1-EN-NM-NF"]
    assert "no binding carries" in report["stopped_because"]


def test_a_variant_shopify_has_no_stock_record_for_is_absent_not_zero(db,
                                                                       wired,
                                                                       monkeypatch):
    monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                        lambda skus, location_gid, client=None: {})
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        binding = bind(session, card)
        session.flush()
        report = qty.push_bindings(session, [binding], confirm=False)
    assert report["absent_from_shopify"] == ["CF-NEO-1-EN-NM-NF"]
    assert report["would_change"] == []


def test_the_same_numbers_reuse_one_idempotency_key(wired):
    """"Shopify tracks idempotency keys for 24 hours", so a retry of the
    SAME numbers must not double-apply."""
    rows = [{"inventoryItemId": "i", "locationId": "l", "quantity": 1,
             "changeFromQuantity": 2}]
    assert (qty._idempotency_key("run-1", rows)
            == qty._idempotency_key("run-1", rows))


def test_different_numbers_get_a_different_key(wired):
    """"Changing any value--even a single quantity or ID--requires a new
    idempotency key"."""
    base = [{"inventoryItemId": "i", "locationId": "l", "quantity": 1,
             "changeFromQuantity": 2}]
    other = [{"inventoryItemId": "i", "locationId": "l", "quantity": 3,
              "changeFromQuantity": 2}]
    assert (qty._idempotency_key("run-1", base)
            != qty._idempotency_key("run-1", other))


# --- the client's own guards --------------------------------------------


def test_the_client_refuses_a_quantity_with_no_change_from(wired):
    authorization = shopify_service.authorize_writes(confirm=True,
                                                     reason="test")
    with pytest.raises(shopify_service.ShopifyError) as caught:
        shopify_service.set_inventory_quantities(
            [{"inventoryItemId": "i", "locationId": "l", "quantity": 1}],
            authorization=authorization)
    assert "changeFromQuantity" in str(caught.value)


def test_the_client_refuses_a_negative_quantity(wired):
    authorization = shopify_service.authorize_writes(confirm=True,
                                                     reason="test")
    with pytest.raises(shopify_service.ShopifyError):
        shopify_service.set_inventory_quantities(
            [{"inventoryItemId": "i", "locationId": "l", "quantity": -1,
              "changeFromQuantity": 0}],
            authorization=authorization)


def test_the_client_refuses_a_duplicate_item_location_pair(wired):
    """"The combination of inventoryItemId and locationId must be
    unique" -- caught here rather than by Shopify."""
    authorization = shopify_service.authorize_writes(confirm=True,
                                                     reason="test")
    row = {"inventoryItemId": "i", "locationId": "l", "quantity": 1,
           "changeFromQuantity": 0}
    with pytest.raises(shopify_service.ShopifyError) as caught:
        shopify_service.set_inventory_quantities(
            [row, dict(row)], authorization=authorization)
    assert "duplicate" in str(caught.value)


def test_the_inventory_mutation_cannot_go_out_through_the_read_client(wired):
    with pytest.raises(shopify_service.ShopifyError):
        shopify_service.execute(shopify_service.SET_QUANTITIES_MUTATION)


# --- 5. reconciliation ---------------------------------------------------


def test_reconciliation_names_the_oversell_direction_separately(db, wired,
                                                                 monkeypatch):
    """Shopify holding MORE than the shop does is the oversell. Shopify
    holding less is merely a lost sale, and the two are not the same
    problem."""
    def read(skus, location_gid, client=None):
        return {
            "CF-NEO-1-EN-NM-NF": {"available_at_location": 3,
                                  "product_status": "DRAFT",
                                  "inventory_item_gid": "i1"},
            "CF-NEO-2-EN-NM-NF": {"available_at_location": 0,
                                  "product_status": "DRAFT",
                                  "inventory_item_gid": "i2"},
            "CF-NEO-3-EN-NM-NF": {"available_at_location": 1,
                                  "product_status": "DRAFT",
                                  "inventory_item_gid": "i3"},
        }

    monkeypatch.setattr(shopify_publish_service, "read_variant_stock", read)
    with Session(db) as session:
        batch = make_batch(session)
        for n in (1, 2, 3):
            card = add_card(session, batch, n=n)
            bind(session, card, sku=f"CF-NEO-{n}-EN-NM-NF",
                 item=f"i{n}")
        session.flush()
        result = qty.reconcile(session)
    assert result["matched"] == ["CF-NEO-3-EN-NM-NF"]
    assert [row["sku"] for row in result["advertising_more_than_we_hold"]] == [
        "CF-NEO-1-EN-NM-NF"]
    assert len(result["different"]) == 2


def test_reconciliation_writes_nothing(db, wired, monkeypatch):
    monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                        lambda skus, location_gid, client=None: stock(
                            "CF-NEO-1-EN-NM-NF", 7))

    def explode(*args, **kwargs):  # pragma: no cover -- must not run
        raise AssertionError("reconciliation wrote to Shopify")

    monkeypatch.setattr(shopify_service, "set_inventory_quantities", explode)
    monkeypatch.setattr(shopify_service, "authorize_writes", explode)
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.flush()
        result = qty.reconcile(session)
    assert len(result["different"]) == 1


def test_reconciliation_reports_an_unbound_binding(db, wired, monkeypatch):
    monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                        lambda skus, location_gid, client=None: {})
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1)
        record_binding(
            session, sku="CF-NEO-1-EN-NM-NF", set_code="NEO",
            collector_number="1", language_id="EN", condition_id="NM",
            finish_id="NF", product_gid="gid://shopify/Product/1")
        session.flush()
        result = qty.reconcile(session)
    assert result["unbound"] == ["CF-NEO-1-EN-NM-NF"]


def test_the_canary_leads_with_the_worst_oversell():
    from sync_shopify_quantities import pick_canary

    report = {"would_change": [
        {"sku": "mild", "shopify_has": 2, "local_says": 1},
        {"sku": "worst", "shopify_has": 9, "local_says": 0},
        {"sku": "under", "shopify_has": 0, "local_says": 4},
    ]}
    picked = pick_canary(report, limit=2)
    assert [row["sku"] for row in picked] == ["worst", "mild"]


# --- 6. the narrow hook: paths Mana Pool's push never reached -----------


def _order_for(session, card, *, quantity=1):
    order = SalesOrder(external_order_id=f"o-{card.id}",
                       external_label=f"L-{card.id}",
                       source="manapool", status="pending")
    session.add(order)
    session.flush()
    session.add(OrderItem(
        order_id=order.id, name=card.name, set_code=card.set_code,
        collector_number=card.collector_number, quantity=quantity,
        scryfall_id=card.scryfall_id, mtgjson_id=card.mtgjson_id,
        language_id=card.language_id, condition_id=card.condition_id,
        finish_id=card.finish_id, price_cents=500))
    session.flush()
    return order


def test_a_real_allocation_lowers_shopify(db, wired, monkeypatch):
    """★ THE GAP THIS TICKET EXISTS FOR. A Mana Pool order reserving a
    card never reached push_for_cards, because Mana Pool does not need
    telling about its own sale -- so Shopify went on advertising it."""
    import order_service

    sent = []
    monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                        lambda skus, location_gid, client=None: stock(
                            "CF-NEO-1-EN-NM-NF", 1))
    monkeypatch.setattr(
        shopify_service, "set_inventory_quantities",
        lambda quantities, **kwargs: sent.append(quantities) or {
            "adjustment_group": {}, "user_errors": [], "stale": []})
    with Session(db) as session:
        qty.set_sync_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        order = _order_for(session, card)
        session.flush()
        order_service.allocate_order(session, order)
        session.flush()
        assert card.status == "reserved"
    assert sent == [[{
        "inventoryItemId": "gid://shopify/InventoryItem/1",
        "locationId": LOCATION, "quantity": 0, "changeFromQuantity": 1}]]


def test_an_allocation_still_works_when_shopify_is_broken(db, wired,
                                                           monkeypatch):
    """★ AN ALLOCATION MUST NEVER FAIL BECAUSE OF SHOPIFY. This is the
    order-sync path: a Mana Pool order landing must be allocated whether
    or not the second channel is reachable."""
    import order_service

    def explode(*args, **kwargs):
        raise RuntimeError("shopify is down")

    monkeypatch.setattr(shopify_publish_service, "read_variant_stock", explode)
    with Session(db) as session:
        qty.set_sync_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        order = _order_for(session, card)
        session.flush()
        result = order_service.allocate_order(session, order)  # must not raise
        session.commit()
        assert result["fully_matched"] is True
        assert card.status == "reserved"


@pytest.mark.parametrize("boom", [
    RuntimeError("down"), ImportError("gone"), TypeError("bug"),
    shopify_service.ShopifyWriteUncertain("timed out"),
])
def test_no_shopify_failure_can_break_an_allocation(db, wired, monkeypatch,
                                                     boom):
    import order_service

    def explode(session, cards, *, origin):
        raise boom

    monkeypatch.setattr(qty, "push_lower_only", explode)
    with Session(db) as session:
        qty.set_sync_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        order = _order_for(session, card)
        session.flush()
        order_service.allocate_order(session, order)
        session.commit()
        assert card.status == "reserved"


def test_the_hook_never_raises_a_quantity(db, wired, monkeypatch):
    """★ THE PROPERTY THAT MAKES IT SAFE INSIDE AN UNCOMMITTED
    TRANSACTION. Raising from uncommitted state could advertise stock a
    rollback then removes -- the oversell this whole feature prevents.
    Lowering cannot: the worst a rollback leaves is a lost sale."""
    monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                        lambda skus, location_gid, client=None: stock(
                            "CF-NEO-1-EN-NM-NF", 0))

    def explode(*args, **kwargs):  # pragma: no cover -- must not run
        raise AssertionError("the hook raised a quantity")

    monkeypatch.setattr(shopify_service, "set_inventory_quantities", explode)
    with Session(db) as session:
        qty.set_sync_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.flush()
        result = qty.push_lower_only(session, [card], origin="test")
    assert result["lowered"] == []
    assert result["deferred"] == [
        {"sku": "CF-NEO-1-EN-NM-NF", "shopify_has": 0, "local_says": 1}]


def test_the_hook_does_nothing_while_the_sync_is_off(db, wired, monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover -- must not run
        raise AssertionError("pushed while off")

    monkeypatch.setattr(shopify_publish_service, "read_variant_stock", explode)
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.flush()
        result = qty.push_lower_only(session, [card], origin="test")
    assert "off" in result["skipped"]


def test_marking_an_exception_lowers_shopify(db, wired, monkeypatch):
    """An exception makes the card unpublishable, and no route that
    reaches it calls push_for_cards."""
    sent = []
    monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                        lambda skus, location_gid, client=None: stock(
                            "CF-NEO-1-EN-NM-NF", 1))
    monkeypatch.setattr(
        shopify_service, "set_inventory_quantities",
        lambda quantities, **kwargs: sent.append(quantities) or {
            "adjustment_group": {}, "user_errors": [], "stale": []})
    with Session(db) as session:
        qty.set_sync_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        allocation = reserve(session, card)
        mark_fulfillment_exception(session, allocation.id, "missing",
                                   note="not on the shelf")
        session.flush()
    assert len(sent) == 1
    assert sent[0][0]["quantity"] == 0


def test_marking_an_exception_survives_a_shopify_failure(db, wired,
                                                          monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("shopify down")

    monkeypatch.setattr(shopify_publish_service, "read_variant_stock", explode)
    with Session(db) as session:
        qty.set_sync_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        allocation = reserve(session, card)
        exception = mark_fulfillment_exception(
            session, allocation.id, "missing", note="not on the shelf")
        session.commit()
        assert exception.id is not None
        assert card.inventory_exception_state == "exception_unresolved"


def test_a_release_defers_its_raise_rather_than_advertising_early(db, wired,
                                                                   monkeypatch):
    """A cancellation gives stock back. The hook logs the raise and
    leaves it to reconciliation rather than advertising stock from a
    transaction that has not committed."""
    import order_service

    monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                        lambda skus, location_gid, client=None: stock(
                            "CF-NEO-1-EN-NM-NF", 0))

    def explode(*args, **kwargs):  # pragma: no cover -- must not run
        raise AssertionError("a release raised a quantity on Shopify")

    monkeypatch.setattr(shopify_service, "set_inventory_quantities", explode)
    with Session(db) as session:
        qty.set_sync_enabled(session, True)
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        order = _order_for(session, card)
        session.flush()
        order_service.allocate_order(session, order)
        order.status = "ready_to_pick"
        session.flush()
        order_service.release_order(session, order, reason="test")
        session.commit()
        assert card.status == "available"


def test_the_hooks_cost_does_not_grow_with_the_catalogue(db, wired,
                                                         monkeypatch):
    """★ THE SCALING PROPERTY THAT MATTERS. The hook runs inside the
    order-sync path, so its cost may grow with the size of the ORDER --
    three scoped queries per distinct identity, which is the price of
    recomputing from local state -- but it must NOT grow with the size of
    the inventory. Ten thousand unrelated cards on the shelf must cost an
    allocation nothing."""
    import order_service

    monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                        lambda skus, location_gid, client=None: {})

    def allocate_one(session, n):
        batch = session.query(Batch).first()
        card = add_card(session, batch, n=n)
        bind(session, card, sku=f"CF-NEO-{n}-EN-NM-NF")
        order = _order_for(session, card)
        session.flush()
        return lambda: order_service.allocate_order(session, order)

    with Session(db) as session:
        qty.set_sync_enabled(session, True)
        make_batch(session)
        lean = _count_queries(db, allocate_one(session, 1))
        # Now bury it in unrelated stock -- 400 cards, none of them in
        # the order being allocated.
        batch = session.query(Batch).first()
        for n in range(100, 500):
            add_card(session, batch, n=n, price_cents=100)
        session.flush()
        fat = _count_queries(db, allocate_one(session, 2))
    assert len(fat) <= len(lean) + 2, (len(lean), len(fat))


def test_an_allocations_own_per_line_cost_is_what_it_was(db, wired,
                                                          monkeypatch):
    """And the hook's share of a multi-line order is bounded: three
    scoped queries per DISTINCT identity, not per card on the shelf."""
    import order_service

    monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                        lambda skus, location_gid, client=None: {})

    def build(session, label, count):
        batch = session.query(Batch).first()
        order = SalesOrder(external_order_id=f"o-{label}",
                           external_label=f"L-{label}", source="manapool",
                           status="pending")
        session.add(order)
        session.flush()
        for n in range(count):
            card = add_card(session, batch, n=f"{label}{n}")
            bind(session, card, sku=f"CF-NEO-{label}{n}-EN-NM-NF")
            session.add(OrderItem(
                order_id=order.id, name=card.name, set_code=card.set_code,
                collector_number=card.collector_number, quantity=1,
                scryfall_id=card.scryfall_id, mtgjson_id=card.mtgjson_id,
                language_id=card.language_id,
                condition_id=card.condition_id, finish_id=card.finish_id,
                price_cents=500))
        session.flush()
        return order

    with Session(db) as session:
        make_batch(session)
        one = build(session, "a", 1)
        five = build(session, "b", 5)
        qty.set_sync_enabled(session, False)
        session.flush()
        off_one = len(_count_queries(db, lambda: order_service.allocate_order(
            session, one)))
        off_five = len(_count_queries(db, lambda: order_service.allocate_order(
            session, five)))
        qty.set_sync_enabled(session, True)
        session.flush()
        two = build(session, "c", 1)
        six = build(session, "d", 5)
        on_one = len(_count_queries(db, lambda: order_service.allocate_order(
            session, two)))
        on_five = len(_count_queries(db, lambda: order_service.allocate_order(
            session, six)))
    # The hook's own cost, with the allocation's own work subtracted.
    hook_one = on_one - off_one
    hook_five = on_five - off_five
    assert hook_one <= 6, hook_one
    # Five identities, not five times the inventory: bounded by a small
    # constant per identity.
    assert hook_five <= hook_one + 5 * 4, (hook_one, hook_five)


# --- 7. the scheduled reconciliation (v2.94.0) ---------------------------


def _reconcile_with(monkeypatch, rows):
    monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                        lambda skus, location_gid, client=None: rows)


def test_the_scheduled_reconciliation_cannot_write_to_shopify(db, wired,
                                                               monkeypatch):
    """★ THE PROPERTY THAT MAKES A CRON SAFE. A job that could correct
    drift on its own could overwrite a quantity nobody reviewed."""
    def explode(*args, **kwargs):  # pragma: no cover -- must not run
        raise AssertionError("the reconciliation wrote to Shopify")

    monkeypatch.setattr(shopify_service, "authorize_writes", explode)
    monkeypatch.setattr(shopify_service, "set_inventory_quantities", explode)
    _reconcile_with(monkeypatch, stock("CF-NEO-1-EN-NM-NF", 3))
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.flush()
        outcome = qty.reconcile_and_record(session)
    assert outcome["ran"] is True
    assert outcome["stored"]["over_count"] == 1


def test_the_reconciliation_stamps_its_own_run(db, wired, monkeypatch):
    """It writes nothing else anywhere, so the stamp is the whole of its
    freshness signal -- the same reasoning VACUUM's route uses."""
    import scheduled_job_runs

    _reconcile_with(monkeypatch, stock("CF-NEO-1-EN-NM-NF", 1))
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.flush()
        assert scheduled_job_runs.last_success(
            session, scheduled_job_runs.SHOPIFY_RECONCILE) is None
        qty.reconcile_and_record(session)
        session.flush()
        assert scheduled_job_runs.last_success(
            session, scheduled_job_runs.SHOPIFY_RECONCILE) is not None


def test_a_refused_reconciliation_leaves_no_stamp(db, monkeypatch):
    """A run that could not reach Shopify must not look like one that
    found nothing."""
    import scheduled_job_runs

    monkeypatch.setattr(shopify_service, "SHOPIFY_LOCATION_ID", None)
    with Session(db) as session:
        outcome = qty.reconcile_and_record(session)
        session.flush()
        assert outcome["ran"] is False
        assert outcome["stopped_because"]
        assert scheduled_job_runs.last_success(
            session, scheduled_job_runs.SHOPIFY_RECONCILE) is None
        assert qty.last_reconcile_result(session) is None


def test_the_stored_result_counts_units_not_just_rows(db, wired, monkeypatch):
    """Nine variants one card over and one variant nine over are the same
    row count and very different problems."""
    _reconcile_with(monkeypatch, {
        "CF-NEO-1-EN-NM-NF": {"available_at_location": 9,
                              "product_status": "DRAFT",
                              "inventory_item_gid": "i1"}})
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.flush()
        stored = qty.reconcile_and_record(session)["stored"]
    assert stored["over_count"] == 1
    assert stored["over_units"] == 8
    assert stored["over_skus"] == ["CF-NEO-1-EN-NM-NF"]


def test_the_reconciliation_writes_no_setting_audit_row(db, wired,
                                                         monkeypatch):
    """It is a measurement that overwrites itself three times a day, not
    a policy someone chose. Auditing it would be hundreds of rows a year
    of a number nobody decided."""
    _reconcile_with(monkeypatch, stock("CF-NEO-1-EN-NM-NF", 1))
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.flush()
        before = session.query(AppSettingChangeLog).count()
        qty.reconcile_and_record(session)
        session.flush()
        assert session.query(AppSettingChangeLog).count() == before


# --- the Attention item --------------------------------------------------


def test_over_drift_raises_exactly_one_attention_item(db, wired, monkeypatch):
    import attention_service

    _reconcile_with(monkeypatch, {
        f"CF-NEO-{n}-EN-NM-NF": {"available_at_location": 2,
                                 "product_status": "DRAFT",
                                 "inventory_item_gid": f"i{n}"}
        for n in (1, 2, 3)})
    with Session(db) as session:
        batch = make_batch(session)
        for n in (1, 2, 3):
            card = add_card(session, batch, n=n)
            bind(session, card, sku=f"CF-NEO-{n}-EN-NM-NF", item=f"i{n}")
        session.flush()
        qty.reconcile_and_record(session)
        session.flush()
        items = attention_service._shopify_drift_items(session)
    assert len(items) == 1
    assert items[0].category == attention_service.CATEGORY_SHOPIFY_DRIFT
    assert "3 variants" in items[0].summary
    assert "CF-NEO-1-EN-NM-NF" in items[0].detail


def test_under_drift_raises_no_attention_item(db, wired, monkeypatch):
    """★ ONE DIRECTION ONLY. Shopify showing FEWER than local is a lost
    sale that corrects itself at the next reconciliation; an item for it
    would appear every time a cancellation gave stock back, which is
    wallpaper on a page the operator asked to keep quiet."""
    import attention_service

    _reconcile_with(monkeypatch, stock("CF-NEO-1-EN-NM-NF", 0))
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.flush()
        stored = qty.reconcile_and_record(session)["stored"]
        session.flush()
        assert stored["under_count"] == 1
        assert stored["over_count"] == 0
        assert attention_service._shopify_drift_items(session) == []


def test_a_clean_run_clears_the_item_with_nothing_to_retract(db, wired,
                                                              monkeypatch):
    """Auto-clearing by construction: the item exists only while the LAST
    stored run found over-drift."""
    import attention_service

    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.flush()
        _reconcile_with(monkeypatch, stock("CF-NEO-1-EN-NM-NF", 5))
        qty.reconcile_and_record(session)
        session.flush()
        assert len(attention_service._shopify_drift_items(session)) == 1
        _reconcile_with(monkeypatch, stock("CF-NEO-1-EN-NM-NF", 1))
        qty.reconcile_and_record(session)
        session.flush()
        assert attention_service._shopify_drift_items(session) == []


def test_the_items_bucket_is_the_skus_not_the_count(db, wired, monkeypatch):
    """A dismissal must hold while the same variants drift and come back
    the moment a different one does."""
    import attention_service

    with Session(db) as session:
        batch = make_batch(session)
        for n in (1, 2):
            card = add_card(session, batch, n=n)
            bind(session, card, sku=f"CF-NEO-{n}-EN-NM-NF", item=f"i{n}")
        session.flush()
        _reconcile_with(monkeypatch, {
            "CF-NEO-1-EN-NM-NF": {"available_at_location": 2,
                                  "product_status": "DRAFT",
                                  "inventory_item_gid": "i1"}})
        qty.reconcile_and_record(session)
        session.flush()
        first = attention_service._shopify_drift_items(session)[0]
        _reconcile_with(monkeypatch, {
            "CF-NEO-2-EN-NM-NF": {"available_at_location": 2,
                                  "product_status": "DRAFT",
                                  "inventory_item_gid": "i2"}})
        qty.reconcile_and_record(session)
        session.flush()
        second = attention_service._shopify_drift_items(session)[0]
    assert first.condition_hash != second.condition_hash


def test_a_never_run_reconciliation_raises_no_drift_item(db, wired):
    import attention_service

    with Session(db) as session:
        assert attention_service._shopify_drift_items(session) == []


def test_an_unparseable_stored_result_is_treated_as_unknown(db, wired):
    with Session(db) as session:
        session.add(AppSetting(key=qty.LAST_RECONCILE_SETTING_KEY,
                               value="not json"))
        session.flush()
        assert qty.last_reconcile_result(session) is None


def test_the_drift_badge_adds_no_query_to_a_page_load(db, wired, monkeypatch):
    """badge_count runs on every page in the app; its statement count is
    pinned, so one more category must not mean one more statement."""
    import attention_service

    _reconcile_with(monkeypatch, stock("CF-NEO-1-EN-NM-NF", 5))
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.flush()
        clean = len(_count_queries(db, lambda: qty.last_reconcile_result(
            session)))
        qty.reconcile_and_record(session)
        session.flush()
        drifted = len(_count_queries(db, lambda: qty.last_reconcile_result(
            session)))
    # One row, one statement, whether or not there is drift to report.
    assert clean == drifted == 1


def test_the_stopped_working_alarm_notices_a_cron_that_died(db, wired,
                                                             monkeypatch):
    """Every remaining gap between local stock and Shopify is caught by
    this job and nothing else, so a job that quietly stopped would leave
    the shop believing the channels agree."""
    import attention_service
    import scheduled_job_runs
    from datetime import datetime, timedelta

    now = datetime.now()
    with Session(db) as session:
        # A job is "unknown" until the app has been watching long enough
        # for silence to mean something -- the existing pattern, so a
        # fresh deploy does not alarm about a cron that has not had a
        # chance to run yet.
        scheduled_job_runs.ensure_watch_since(
            session, when=now - timedelta(days=7))
        session.commit()
        scheduled_job_runs.record_success(
            session, scheduled_job_runs.SHOPIFY_RECONCILE,
            when=now - timedelta(hours=1))
        session.commit()
        assert attention_service.shopify_reconcile_health(
            session, now=now)["state"] == "fresh"
        scheduled_job_runs.record_success(
            session, scheduled_job_runs.SHOPIFY_RECONCILE,
            when=now - timedelta(hours=40))
        session.commit()
        late = attention_service.shopify_reconcile_health(session, now=now)
        assert late["state"] not in attention_service.SCHEDULED_JOB_QUIET_STATES
        assert "three times a day" in late["reason"]


def test_the_drift_badge_reads_from_the_shared_dict(db, wired, monkeypatch):
    """★ THE N+1 THIS SHIPPED WITH FOR ONE SUITE RUN. The first version
    queried the stored result directly and put a seventh statement on
    every page in the app; the orders-list statement-count tests failed,
    which is what they are for. The key now rides along in
    scheduled_job_runs_records' single IN (...)."""
    import attention_service
    import scheduled_job_runs

    _reconcile_with(monkeypatch, stock("CF-NEO-1-EN-NM-NF", 5))
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        bind(session, card)
        session.flush()
        qty.reconcile_and_record(session)
        session.commit()
        records = attention_service.scheduled_job_runs_records(session, None)
        assert qty.LAST_RECONCILE_SETTING_KEY in records
        # Given the dict, reading the result costs NO query at all.
        statements = _count_queries(db, lambda: qty.last_reconcile_result(
            session, records=records))
        assert statements == []
        assert qty.last_reconcile_result(
            session, records=records)["over_count"] == 1


def test_the_admin_card_reports_the_numbers_the_run_actually_stored(
        db, wired, monkeypatch):
    """★ IT SAID "0 variant(s) checked" AFTER CHECKING 6,804 (ticket 7b).

    The /admin card read `ran_at` and `checked`; _store_reconcile_result
    writes `at` and `bindings`. Both misses fell through a `.get`
    default, so a missing key rendered exactly like a real zero -- a
    reconciliation that had just agreed on every one of 6,804 bindings
    looked like one that had checked nothing.

    Driven end to end on purpose: the REAL reconciliation writes the
    record, and then the REAL page is read. A test that asserted the
    stored dict's keys would have passed throughout, because the stored
    dict was never wrong.
    """
    import main

    monkeypatch.setattr(main, "engine", db)
    _reconcile_with(monkeypatch, {
        "CF-NEO-1-EN-NM-NF": {"available_at_location": 1,
                              "product_status": "DRAFT",
                              "inventory_item_gid": "i1"},
        "CF-NEO-2-EN-NM-NF": {"available_at_location": 1,
                              "product_status": "DRAFT",
                              "inventory_item_gid": "i2"},
    })
    with Session(db) as session:
        batch = make_batch(session)
        first = add_card(session, batch, n=1)
        second = add_card(session, batch, n=2)
        bind(session, first, sku="CF-NEO-1-EN-NM-NF")
        bind(session, second, sku="CF-NEO-2-EN-NM-NF",
             item="gid://shopify/InventoryItem/2")
        session.flush()
        stored = qty.reconcile_and_record(session)["stored"]
        session.commit()
    assert stored["bindings"] == 2 and stored["matched"] == 2

    html = TestClient(main.app).get("/admin").text
    assert "2 variant(s) checked, 2 matching" in html
    assert "0 variant(s) checked" not in html
    # The timestamp is the other half of the same bug: `ran_at` was
    # absent too, so the line opened with " -- ".
    assert stored["at"][:16].replace("T", " ") in html
