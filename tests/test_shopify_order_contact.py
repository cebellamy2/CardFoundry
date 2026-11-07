"""Ticket 18: the buyer's name and phone on a Shopify order.

The operator's reason for this is narrow and worth keeping in view: see
who is picking an order up, and be able to text one of two friends whose
orders collided. So every test here drives the REAL intake path and then
reads the REAL rendered order page -- what the operator sees is the
thing under test, not whether a function was called.

★ ALL DATA IN HERE IS OBVIOUSLY FAKE. "Testy McFake" and +1555... are
not anybody, which matters more than usual in a file about personal
data.

★ AND THE PRIVACY RULE IS ITSELF TESTED, not just honoured: a name or a
phone must never reach a log line or an audit row. Two tests assert the
absence, because that is the half a future change would quietly break.

No test touches the network: shopify_service.execute is stubbed.
"""
import base64
import hashlib
import hmac
import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import inventory_sync_service
import main
import shopify_order_contact_service as contact
import shopify_order_poll_service as poll
import shopify_order_service as intake
import shopify_service
import shopify_webhook_service as hook
from local_price_writeback_service import set_card_price
from models import (
    Base, Batch, ImportRecord, InventoryCard, SalesOrder,
    ShopifyOrderContactChangeLog,
)
from shopify_binding_service import record_binding

ORDER_NUMERIC_ID = "7271829405761"
ORDER_GID = f"gid://shopify/Order/{ORDER_NUMERIC_ID}"

FAKE_FIRST = "Testy"
FAKE_LAST = "McFake"
FAKE_NAME = f"{FAKE_FIRST} {FAKE_LAST}"
FAKE_PHONE = "+15550100001"
FAKE_BILLING_NAME = "Billing Mcfake"
FAKE_BILLING_PHONE = "+15550100002"
FAKE_ORDER_PHONE = "+15550100003"

# ★ SHOPIFY'S REAL DENIAL, COPIED FROM THE PRODUCTION LOG (2026-11-06
# 21:53:47 UTC), not invented. v2.117.0's version of this test used a
# message I made up -- "ACCESS_DENIED: This app is not approved to
# access the Customer object." -- which matched the shipped
# case-sensitive check and so passed while the real path, with its
# capital A and no ACCESS_DENIED text at all, fell through to the
# transport wording on every hourly tick.
#
# Note what is and is not in here: "Access denied" is capitalised, and
# the ACCESS_DENIED code appears NOWHERE -- shopify_service logs the
# code on a separate line and raises only the message. If this string
# is ever "tidied", the bug comes back.
REAL_DENIAL_MESSAGE = (
    "Shopify returned errors: Access denied for customer field. "
    "Required access: `read_customers` access scope."
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'contact.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


@pytest.fixture
def logs(caplog):
    """The cardfoundry logger does not propagate; attach by hand."""
    logger = logging.getLogger("cardfoundry")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="cardfoundry")
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


def ready_shop(session):
    """One available, priced, Shopify-bound card -- through the real
    services, so the row shapes are the ones production makes."""
    batch = Batch(batch_code="A1")
    session.add(batch)
    session.flush()
    record = ImportRecord(batch_id=batch.id, filename="x.csv",
                          file_hash="h1", card_count=1)
    session.add(record)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, import_id=record.id, name="Barreling Attack",
        set_code="MIR", collector_number="1", scryfall_id="sf-1",
        mtgjson_id="mtg-1", language_id="EN", condition_id="LP",
        finish_id="NF", condition="LP", finish="normal", status="available",
    )
    session.add(card)
    session.flush()
    set_card_price(session, card, 65, source="seller_inventory_scan",
                   note="fixture")
    session.flush()
    record_binding(
        session, sku="CF-MIR-1-EN-LP-NF", set_code="MIR",
        collector_number="1", language_id="EN", condition_id="LP",
        finish_id="NF", mtgjson_id="mtg-1",
        product_gid="gid://shopify/Product/1",
        variant_gid="gid://shopify/ProductVariant/1",
        inventory_item_gid="gid://shopify/InventoryItem/1",
    )
    session.flush()
    return card


def order_payload():
    """A GraphQL order node, the shape the poll reads."""
    return {
        "id": ORDER_GID,
        "name": "#1001",
        "createdAt": "2026-11-06T16:25:49Z",
        "updatedAt": "2026-11-06T16:25:49Z",
        "cancelledAt": None,
        "email": "buyer@example.com",
        "lineItems": {"edges": [{"node": {
            "id": "gid://shopify/LineItem/1", "sku": "CF-MIR-1-EN-LP-NF",
            "quantity": 1, "name": "Barreling Attack",
            "discountedUnitPriceSet": {
                "shopMoney": {"amount": "0.65", "currencyCode": "USD"}},
        }}]},
    }


def contact_node(*, first=FAKE_FIRST, last=FAKE_LAST, phone=FAKE_PHONE,
                 billing_name=None, billing_phone=None,
                 order_phone=None, customer=True):
    """One Order node as the contact read sees it."""
    node = {"id": ORDER_GID, "phone": order_phone}
    if customer:
        node["customer"] = {
            "firstName": first, "lastName": last,
            "defaultPhoneNumber": ({"phoneNumber": phone} if phone
                                   else None),
        }
    else:
        node["customer"] = None
    node["billingAddress"] = (
        {"name": billing_name, "phone": billing_phone}
        if (billing_name or billing_phone) else None)
    return node


def ingest(session, *, contact_nodes=None, raises=None, monkeypatch=None):
    """Drive the REAL ingest, with the contact read stubbed."""
    calls = {"contact": 0}

    def execute(document, variables=None, *, client=None, **kwargs):
        if "CardFoundryShopifyOrderContact" in document:
            calls["contact"] += 1
            if raises is not None:
                raise raises
            return {"orders": {"nodes": list(contact_nodes or [])}}
        raise AssertionError(f"unexpected document: {document[:90]}")

    monkeypatch.setattr(shopify_service, "execute", execute)
    outcome = intake.ingest_one(session, order_payload(),
                                          initiated_by="test")
    session.commit()
    order = session.get(SalesOrder, outcome["order_id"])
    result = contact.record_order_contacts(session, [order], refresh=True,
                                           actor="shopify-poll")
    session.commit()
    return order, result, calls


def page(order_id):
    return TestClient(main.app).get(f"/orders/{order_id}").text


# --------------------------------------------------------------- case 1
def test_name_and_phone_present_reach_the_order_page(db, monkeypatch):
    """The operator sees who is collecting, and can tap the number."""
    with Session(db) as session:
        ready_shop(session)
        session.commit()
        order, result, _ = ingest(session, contact_nodes=[contact_node()],
                                  monkeypatch=monkeypatch)
        order_id = order.id
        assert result["stored"] == 1
        assert order.shopify_contact_name == FAKE_NAME
        assert order.shopify_contact_phone == FAKE_PHONE

    text = page(order_id)
    assert "Collected by" in text
    assert FAKE_NAME in text
    assert "Buyer phone" in text
    # A plain tel: link, no JavaScript -- the stated point of the phone.
    assert f'href="tel:{FAKE_PHONE}"' in text


def test_the_audit_row_records_the_capture_with_no_values(db, monkeypatch):
    """★ THE PRIVACY HALF. An append-only row saying it happened, and
    not one character of what it was."""
    with Session(db) as session:
        ready_shop(session)
        session.commit()
        order, _, _ = ingest(session, contact_nodes=[contact_node()],
                             monkeypatch=monkeypatch)
        rows = session.query(ShopifyOrderContactChangeLog).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.change_type == "captured"
        assert row.fields_changed == "name+phone"
        assert row.name_present is True
        assert row.phone_present is True
        assert row.actor == "shopify-poll"
        # The whole row, every text column, carries no value.
        blob = " ".join(str(getattr(row, col) or "") for col in
                        ("change_type", "fields_changed", "change_summary",
                         "actor", "external_order_id"))
        for secret in (FAKE_NAME, FAKE_FIRST, FAKE_LAST, FAKE_PHONE):
            assert secret not in blob


def test_no_name_or_phone_is_ever_logged(db, monkeypatch, logs):
    """★ THE OTHER PRIVACY HALF: the log counts, it does not quote."""
    with Session(db) as session:
        ready_shop(session)
        session.commit()
        ingest(session, contact_nodes=[contact_node()],
               monkeypatch=monkeypatch)
    written = "\n".join(record.getMessage() for record in logs.records)
    assert written  # the capture really did log something
    for secret in (FAKE_NAME, FAKE_FIRST, FAKE_LAST, FAKE_PHONE):
        assert secret not in written


# --------------------------------------------------------------- case 2
def test_phone_missing_stores_the_name_and_says_so_on_the_page(
        db, monkeypatch):
    """Missing is NULL, never an error, and the page says it plainly."""
    with Session(db) as session:
        ready_shop(session)
        session.commit()
        order, result, _ = ingest(
            session, contact_nodes=[contact_node(phone=None)],
            monkeypatch=monkeypatch)
        order_id = order.id
        assert result["stored"] == 1
        assert result["with_name"] == 1
        assert result["with_phone"] == 0
        assert order.shopify_contact_name == FAKE_NAME
        assert order.shopify_contact_phone is None

    text = page(order_id)
    assert FAKE_NAME in text
    assert "Buyer phone" in text
    assert "not recorded" in text
    assert 'href="tel:' not in text


def test_the_documented_fallbacks_are_used_in_the_operators_order(
        db, monkeypatch):
    """billingAddress.name for the name; billing then order for the phone."""
    with Session(db) as session:
        ready_shop(session)
        session.commit()
        order, _, _ = ingest(
            session,
            contact_nodes=[contact_node(
                customer=False, billing_name=FAKE_BILLING_NAME,
                billing_phone=FAKE_BILLING_PHONE,
                order_phone=FAKE_ORDER_PHONE)],
            monkeypatch=monkeypatch)
        assert order.shopify_contact_name == FAKE_BILLING_NAME
        # Billing wins over the order's own phone.
        assert order.shopify_contact_phone == FAKE_BILLING_PHONE

    with Session(db) as session:
        # And the order's own phone is the last resort.
        node = contact_node(customer=False, order_phone=FAKE_ORDER_PHONE)
        assert contact.contact_from_node(node)["phone"] == FAKE_ORDER_PHONE
        assert contact.contact_from_node(node)["name"] is None


# --------------------------------------------------------------- case 3
def test_scope_denied_leaves_the_order_intact_and_logs_once(
        db, monkeypatch, logs):
    """★ THE DEV-STORE CASE. No read_customers scope, so the read is
    refused -- and intake must not care. The order is ingested,
    allocated and visible; the contact is NULL; one counted log line."""
    denial = shopify_service.ShopifyError(REAL_DENIAL_MESSAGE)
    with Session(db) as session:
        ready_shop(session)
        session.commit()
        order, result, calls = ingest(session, raises=denial,
                                      monkeypatch=monkeypatch)
        order_id = order.id
        assert calls["contact"] == 1
        assert result["stored"] == 0
        assert result["denied"] is True
        assert order.shopify_contact_name is None
        assert order.shopify_contact_phone is None
        # ★ THE ORDER ITSELF IS UNTOUCHED BY THE REFUSAL -- it allocated
        # and is ready to pick. That is the whole point: the contact is
        # a nice-to-have bolted onto an order that must survive without
        # it, and "ready_to_pick" is the proof it did.
        assert order.status == "ready_to_pick"
        assert session.query(ShopifyOrderContactChangeLog).count() == 0

    denials = [r.getMessage() for r in logs.records
               if "buyer contact" in r.getMessage()]
    assert len(denials) == 1
    # ★ NAMED AS A SCOPE PROBLEM, which is the whole point: it is
    # permanent and fixable, and v2.117.0 called it a transport failure.
    assert "no read_customers scope" in denials[0]
    assert "the read failed" not in denials[0]
    # ★ BOTH NUMBERS in the one line a person reads.
    assert "1 order(s)" in denials[0]
    assert "1 carried an id" in denials[0]
    assert result["asked"] == 1
    assert result["asked_ids"] == 1

    text = page(order_id)
    assert "Collected by" in text
    assert "not recorded" in text


def test_a_transport_failure_is_not_reported_as_a_scope_problem(
        db, monkeypatch, logs):
    """A timeout and a denial are different things to an operator: one
    is fixable by granting a scope and the other is not."""
    with Session(db) as session:
        ready_shop(session)
        session.commit()
        _, result, _ = ingest(session,
                              raises=RuntimeError("connection reset"),
                              monkeypatch=monkeypatch)
        assert result["stored"] == 0
        assert result["denied"] is False
    messages = [r.getMessage() for r in logs.records
                if "buyer contact" in r.getMessage()]
    assert len(messages) == 1
    assert "the read failed" in messages[0]
    assert "read_customers" not in messages[0]
    assert "1 order(s)" in messages[0]
    assert "1 carried an id" in messages[0]


# ------------------------------------------------------- update on change
def test_a_changed_contact_is_updated_and_audited_as_changed(
        db, monkeypatch):
    """The operator asked for this: a contact edited in Shopify catches
    up, and the audit says WHICH field moved without saying to what."""
    with Session(db) as session:
        ready_shop(session)
        session.commit()
        order, _, _ = ingest(session,
                             contact_nodes=[contact_node(phone=None)],
                             monkeypatch=monkeypatch)
        order_id = order.id

        def execute(document, variables=None, *, client=None, **kwargs):
            return {"orders": {"nodes": [contact_node()]}}

        monkeypatch.setattr(shopify_service, "execute", execute)
        second = contact.record_order_contacts(session, [order],
                                               refresh=True)
        session.commit()
        assert second["stored"] == 1
        assert order.shopify_contact_phone == FAKE_PHONE
        rows = (session.query(ShopifyOrderContactChangeLog)
                .order_by(ShopifyOrderContactChangeLog.id).all())
        assert [r.change_type for r in rows] == ["captured", "changed"]
        assert rows[1].fields_changed == "phone"

    assert f'href="tel:{FAKE_PHONE}"' in page(order_id)


def test_a_second_sighting_with_the_same_contact_writes_nothing(
        db, monkeypatch):
    """Idempotent: no second audit row for an unchanged contact."""
    with Session(db) as session:
        ready_shop(session)
        session.commit()
        order, _, _ = ingest(session, contact_nodes=[contact_node()],
                             monkeypatch=monkeypatch)
        again = contact.record_order_contacts(session, [order], refresh=True)
        session.commit()
        assert again["stored"] == 0
        assert session.query(ShopifyOrderContactChangeLog).count() == 1


def test_without_refresh_an_order_that_has_a_contact_is_not_re_read(
        db, monkeypatch):
    """The ingest pass must not spend a read on an order already answered."""
    with Session(db) as session:
        ready_shop(session)
        session.commit()
        order, _, _ = ingest(session, contact_nodes=[contact_node()],
                             monkeypatch=monkeypatch)

        def refuse(document, variables=None, *, client=None, **kwargs):
            raise AssertionError("it asked Shopify again for a known contact")

        monkeypatch.setattr(shopify_service, "execute", refuse)
        result = contact.record_order_contacts(session, [order],
                                               refresh=False)
        assert result == {"asked": 0, "asked_ids": 0, "stored": 0,
                          "with_name": 0, "with_phone": 0}


def test_a_mana_pool_order_is_never_asked_about(db, monkeypatch):
    """Source-scoped: only Shopify orders have a Shopify contact."""
    with Session(db) as session:
        order = SalesOrder(external_order_id="mp-1", source="manapool",
                           status="new")
        session.add(order)
        session.commit()

        def refuse(document, variables=None, *, client=None, **kwargs):
            raise AssertionError("it asked Shopify about a Mana Pool order")

        monkeypatch.setattr(shopify_service, "execute", refuse)
        result = contact.record_order_contacts(session, [order],
                                               refresh=True)
        assert result["asked"] == 0
        order_id = order.id

    # And the page shows no contact rows at all for it.
    text = page(order_id)
    assert "Collected by" not in text


def test_the_poll_asks_about_contacts_and_reports_the_count(db, monkeypatch):
    """★ THROUGH THE REAL POLL, so the wiring is covered and not only
    the service. The poll must surface the count it stored."""
    with Session(db) as session:
        ready_shop(session)
        session.commit()

    documents = []

    def execute(document, variables=None, *, client=None, **kwargs):
        documents.append(document)
        if "CardFoundryShopifyOrders" in document:
            return {"orders": {
                "edges": [{"cursor": "c0", "node": order_payload()}],
                "pageInfo": {"hasNextPage": False},
            }}
        if "CardFoundryShopifyDeliveryMethod" in document:
            return {"shipping": {"nodes": []}, "pickup": {"nodes": []}}
        if "CardFoundryShopifyOrderContact" in document:
            return {"orders": {"nodes": [contact_node()]}}
        raise AssertionError(f"unexpected document: {document[:90]}")

    monkeypatch.setattr(shopify_service, "execute", execute)
    with Session(db) as session:
        # ★ THE REAL SETTING, not a monkeypatched predicate: the poll
        # imports intake_enabled into its own namespace, so patching the
        # attribute on shopify_order_service would not reach it -- the
        # test would pass while testing nothing.
        intake.set_intake_enabled(session, True)
        session.commit()
        outcome = poll.poll_shopify_orders(session)
        session.commit()

    assert any("CardFoundryShopifyOrderContact" in d for d in documents)
    assert outcome["buyer_contacts"]["stored"] == 1
    with Session(db) as session:
        order = (session.query(SalesOrder)
                 .filter(SalesOrder.source == "shopify").one())
        assert order.shopify_contact_name == FAKE_NAME
        order_id = order.id
    assert FAKE_NAME in page(order_id)


# ------------------------------------------------------- the webhook path
def sign(secret, body: bytes) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def webhook_body():
    """The snake_case JSON a webhook sends -- NOT the GraphQL shape."""
    return {
        "id": int(ORDER_NUMERIC_ID),
        "name": "#1001",
        "created_at": "2026-11-06T16:25:49Z",
        "line_items": [{"id": 1, "sku": "CF-MIR-1-EN-LP-NF", "quantity": 1,
                        "name": "Barreling Attack", "price": "0.65"}],
    }


def test_the_webhook_path_captures_the_contact_too(db, monkeypatch):
    """★ THE OTHER INTAKE PATH, end to end through the real receiver.

    The ticket asks for BOTH, and the poll test above covers one. This
    drives process_delivery, which is where the real webhook arrives --
    four seconds after a purchase -- and proves the contact lands from
    there as well, after the delivery is recorded and outside the lease.
    """
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "shhh")
    factory = sessionmaker(bind=db)
    with factory() as session:
        ready_shop(session)
        intake.set_intake_enabled(session, True)
        session.commit()

    documents = []

    def execute(document, variables=None, *, client=None, **kwargs):
        documents.append(document)
        if "CardFoundryShopifyOrderContact" in document:
            return {"orders": {"nodes": [contact_node()]}}
        if "CardFoundryShopifyDeliveryMethod" in document:
            return {"shipping": {"nodes": []}, "pickup": {"nodes": []}}
        raise AssertionError(f"unexpected document: {document[:90]}")

    monkeypatch.setattr(shopify_service, "execute", execute)

    body = json.dumps(webhook_body()).encode()
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
    assert outcome["outcome"] == "ingested"
    assert any("CardFoundryShopifyOrderContact" in d for d in documents)

    with factory() as session:
        order = (session.query(SalesOrder)
                 .filter(SalesOrder.source == "shopify").one())
        assert order.shopify_contact_name == FAKE_NAME
        assert order.shopify_contact_phone == FAKE_PHONE
        order_id = order.id
        assert session.query(ShopifyOrderContactChangeLog).count() == 1

    assert FAKE_NAME in page(order_id)


def test_a_webhook_contact_denial_does_not_fail_the_delivery(
        db, monkeypatch, logs):
    """★ THE DEV STORE, ON THE WEBHOOK PATH. The purchase is already
    ingested; a refused contact read must not turn a good delivery bad."""
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "shhh")
    factory = sessionmaker(bind=db)
    with factory() as session:
        ready_shop(session)
        intake.set_intake_enabled(session, True)
        session.commit()

    def execute(document, variables=None, *, client=None, **kwargs):
        if "CardFoundryShopifyOrderContact" in document:
            raise shopify_service.ShopifyError(REAL_DENIAL_MESSAGE)
        if "CardFoundryShopifyDeliveryMethod" in document:
            return {"shipping": {"nodes": []}, "pickup": {"nodes": []}}
        raise AssertionError(f"unexpected document: {document[:90]}")

    monkeypatch.setattr(shopify_service, "execute", execute)

    body = json.dumps(webhook_body()).encode()
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
    # ★ THE DELIVERY STILL SUCCEEDED. That is the assertion that matters.
    assert outcome["outcome"] == "ingested"
    with factory() as session:
        order = (session.query(SalesOrder)
                 .filter(SalesOrder.source == "shopify").one())
        assert order.shopify_contact_name is None
        assert order.status == "ready_to_pick"


# --------------------------------------------- Shopify's search index lag
def test_no_node_returned_writes_nothing_and_is_asked_again(
        db, monkeypatch):
    """★ THE WEBHOOK CAN BE AHEAD OF SHOPIFY'S SEARCH INDEX.

    This reader asks the `orders` SEARCH query by id, and the webhook
    reaches us about four seconds after the purchase --
    shopify_delivery_method_service's docstring already warns that the
    index is not promised to be current that fast. An order Shopify has
    not indexed yet comes back in NO node at all.

    That must write nothing. Writing an empty answer would be recording
    a timing artefact and then never asking again, because the columns
    would no longer be NULL -- the same trap store_unclassified=False
    exists to avoid for the delivery method. The contact stays NULL and
    the poll's bounded catch-up picks it up later, which this asserts by
    doing exactly that.
    """
    asked = {"count": 0}

    def execute(document, variables=None, *, client=None, **kwargs):
        if "CardFoundryShopifyOrderContact" in document:
            asked["count"] += 1
            # Shopify answered, and the order was not in the answer.
            return {"orders": {"nodes": []}}
        raise AssertionError(f"unexpected document: {document[:90]}")

    with Session(db) as session:
        ready_shop(session)
        session.commit()
        monkeypatch.setattr(shopify_service, "execute", execute)
        out = intake.ingest_one(session, order_payload(), initiated_by="test")
        session.commit()
        order = session.get(SalesOrder, out["order_id"])

        first = contact.record_order_contacts(session, [order], refresh=True)
        session.commit()
        # Asked, answered, and nothing stored -- NOT a failure.
        assert asked["count"] == 1
        assert first["asked"] == 1
        assert first["stored"] == 0
        assert "failed" not in first
        assert order.shopify_contact_name is None
        assert order.shopify_contact_phone is None
        assert session.query(ShopifyOrderContactChangeLog).count() == 0
        order_id = order.id

        # ★ AND IT IS ASKED AGAIN, because the columns are still NULL --
        # this time the index has caught up and the contact lands.
        def answered(document, variables=None, *, client=None, **kwargs):
            return {"orders": {"nodes": [contact_node()]}}

        monkeypatch.setattr(shopify_service, "execute", answered)
        second = contact.record_order_contacts(session, [order],
                                               refresh=False)
        session.commit()
        assert second["asked"] == 1
        assert second["stored"] == 1
        assert order.shopify_contact_name == FAKE_NAME

    assert FAKE_NAME in page(order_id)


def test_the_two_counts_differ_when_a_row_carries_no_askable_id(
        db, monkeypatch, logs):
    """★ THE PRODUCTION CASE, from the 21:53 UTC tick on v2.117.0: the
    warning said "2 order(s)" while the query it sent carried ONE id.

    Only a numeric id can go in an `id:` filter, so a Shopify order whose
    stored external id is not numeric contributes nothing to the read --
    correctly, and silently. But a line that reports only the order count
    invites the reader to think Shopify was asked about a row it never
    saw. Both numbers, and here they genuinely differ.
    """
    with Session(db) as session:
        ready_shop(session)
        session.commit()
        # One ordinary Shopify order with a numeric id...
        order, _, _ = ingest(session, contact_nodes=[contact_node()],
                             monkeypatch=monkeypatch)
        # ...and one whose external id is not numeric, so it cannot be
        # asked about at all.
        odd = SalesOrder(external_order_id="manual-no-number",
                         source="shopify", status="pending")
        session.add(odd)
        session.commit()

        def denied(document, variables=None, *, client=None, **kwargs):
            raise shopify_service.ShopifyError(REAL_DENIAL_MESSAGE)

        monkeypatch.setattr(shopify_service, "execute", denied)
        logs.clear()
        result = contact.record_order_contacts(session, [order, odd],
                                               refresh=True)
        assert result["asked"] == 2
        assert result["asked_ids"] == 1
        assert result["denied"] is True

    line = [r.getMessage() for r in logs.records
            if "buyer contact" in r.getMessage()]
    assert len(line) == 1
    assert "2 order(s)" in line[0]
    assert "1 carried an id" in line[0]
    assert "no read_customers scope" in line[0]
    # And still no values, with two orders in play.
    for secret in (FAKE_NAME, FAKE_FIRST, FAKE_LAST, FAKE_PHONE):
        assert secret not in line[0]


def test_askable_ids_filters_and_dedupes(db):
    """The one definition both callers use."""
    assert contact.askable_ids(
        ["7271829405761", "gid://shopify/Order/abc", None, "",
         "7271829405761", "manual-1"]) == ["7271829405761"]
    assert contact.askable_ids([]) == []
    assert contact.askable_ids(None) == []
