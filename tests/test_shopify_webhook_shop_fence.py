"""The receiver only acts on deliveries from the shop it is configured for.

★ WHY THIS FILE EXISTS. The receiver has read X-Shopify-Shop-Domain into
a constant since it shipped and never compared it with anything
(SHOP_DOMAIN_HEADER had exactly one occurrence in the repository: its
own definition). That was invisible while one app was installed on one
store. The cutover to the real shop makes it matter: for a while the
dev shop still holds order subscriptions pointing at this receiver, and
two shops carrying the same app both pass the HMAC. Without this fence
the only thing standing between a dev-shop order and a real card
reservation is the two shops happening to have different app secrets --
true today, and nothing in the code says it must stay true.

★ WHY IT IS ITS OWN FILE. The fence is one property with two halves --
the verdict, and what the rest of the receiver then refuses to do with
it -- and both halves belong next to each other. The receiver's other
route tests live in test_shopify_pickup.py for historical reasons; this
is not more of those.

Everything here drives the real receiver: the real route, the real
verify_signature, the real process_delivery and the real sweeper. No
test touches the network, and the only "shops" involved are fake
hostnames.
"""
import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import inventory_sync_service
import main
import shopify_order_service as intake
import shopify_service
import shopify_webhook_service as hook
from models import Base, SalesOrder, WebhookDelivery

from tests.test_shopify_order_intake import sign, webhook_body

# The two shops in this file. Neither is real, deliberately: a fixture
# naming the operator's live shop would pass for the wrong reason the day
# somebody changes the live shop.
OURS = "cf-test-shop.myshopify.com"
THEIRS = "someone-elses-shop.myshopify.com"

SECRET = "s3cret"

# ★ SHOPIFY'S OWN HEADER, SPELLED THE WAY SHOPIFY SPELLS IT. Shopify
# documents sending X-Shopify-Shop-Domain on every webhook and tells
# readers to "treat header names as case-insensitive", so both spellings
# are exercised below rather than one being assumed to imply the other.
SHOP_HEADER = "X-Shopify-Shop-Domain"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'shop-fence.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


@pytest.fixture
def logs(caplog):
    """The cardfoundry logger does not propagate (main.py sets an explicit
    handler so it survives uvicorn), so caplog has to be attached by
    hand -- the same fixture test_shopify_order_contact.py uses."""
    logger = logging.getLogger("cardfoundry")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="cardfoundry")
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


@pytest.fixture
def receiver(monkeypatch):
    """The receiver as production runs it: enabled, a secret, and a
    configured shop."""
    monkeypatch.setenv(hook.ENABLED_ENV, "true")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", OURS)


def post(body: bytes, *, shop: str | None, secret: str = SECRET,
         header_name: str = SHOP_HEADER):
    headers = {"X-Shopify-Topic": "orders/create",
               "X-Shopify-Hmac-Sha256": sign(secret, body),
               "X-Shopify-Webhook-Id": "wh-1"}
    if shop is not None:
        headers[header_name] = shop
    return TestClient(main.app).post("/webhooks/shopify/orders",
                                     content=body, headers=headers)


# --- the property that matters -------------------------------------------

def test_an_authentic_delivery_from_another_shop_reserves_nothing(db, receiver,
                                                                  logs):
    """★ THE WHOLE POINT. Intake is ON, the body is signed with OUR app's
    secret, and it still must not become an order -- because it came from
    a shop this app is not pointed at."""
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        session.commit()

    body = json.dumps(webhook_body(order_id="99001")).encode()
    response = post(body, shop=THEIRS)

    assert response.status_code == 401
    assert response.json()["status"] == hook.SIGNATURE_WRONG_SHOP
    with Session(db) as session:
        # Recorded, because an operator asking "is something else
        # pointed at us" needs the row to answer with.
        row = session.query(WebhookDelivery).one()
        assert row.signature_status == hook.SIGNATURE_WRONG_SHOP
        assert row.external_order_id == "99001"
        # ★ AND NOTHING WAS RESERVED. If the fence failed, intake being
        # ON means this body would have produced a SalesOrder.
        assert session.query(SalesOrder).count() == 0
    # The refusal is a single counted line that names both shops and no
    # secret.
    refusals = [r.getMessage() for r in logs.records
                if "refused" in r.getMessage()]
    assert len(refusals) == 1
    assert THEIRS in refusals[0] and OURS in refusals[0]
    assert SECRET not in refusals[0]


def test_the_same_body_from_our_own_shop_is_accepted(db, receiver):
    """The fence has to let the real thing through, or it is just an
    outage with a reason attached."""
    body = json.dumps(webhook_body(order_id="99002")).encode()
    response = post(body, shop=OURS)
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    with Session(db) as session:
        assert session.query(WebhookDelivery).one().signature_status == \
            hook.SIGNATURE_VERIFIED


def test_a_wrong_shop_row_is_never_processed_and_never_swept(db, receiver):
    """★ THE SECOND HALF OF THE FENCE. The verdict alone would be
    cosmetic if a retry or the sweeper picked the row up later, so both
    paths are driven here rather than reasoned about: process_delivery
    refuses on the stored verdict, and the sweeper's own query does not
    return the row at all."""
    body = json.dumps(webhook_body(order_id="99003")).encode()
    assert post(body, shop=THEIRS).status_code == 401
    factory = sessionmaker(bind=db)
    with factory() as session:
        delivery_id = session.query(WebhookDelivery).one().id

    outcome = hook.process_delivery(factory, delivery_id)
    assert outcome["outcome"] == "unverified"
    with factory() as session:
        row = session.get(WebhookDelivery, delivery_id)
        assert row.processing_status == "failed"
        assert hook.SIGNATURE_WRONG_SHOP in row.last_error

    # ★ AND THE SWEEPER NEVER EVEN CONSIDERS IT: its query asks for
    # signature_status == verified, so a wrong_shop row is not an
    # unfinished delivery waiting for a retry -- it is a closed one.
    swept = hook.sweep_unfinished(factory, older_than_seconds=0)
    assert swept["found"] == 0
    with factory() as session:
        assert session.query(SalesOrder).count() == 0


# --- the order of the two checks -----------------------------------------

def test_a_forged_shop_header_cannot_soften_a_signature_failure(receiver):
    """★ THE HEADER IS UNAUTHENTICATED, SO IT IS JUDGED SECOND. Anyone
    can type a shop domain; nobody can type our HMAC. If the shop were
    checked first, an attacker could choose which of the two verdicts
    our audit row recorded -- so a badly signed body is invalid_signature
    whatever its header claims."""
    body = b'{"id": 1}'
    verdict = hook.verify_signature(
        {"X-Shopify-Hmac-Sha256": sign("not-our-secret", body),
         SHOP_HEADER: THEIRS}, body)
    assert verdict["status"] == hook.SIGNATURE_INVALID

    # And the same forged header on a body signed with our own secret is
    # the one case that IS wrong_shop.
    verdict = hook.verify_signature(
        {"X-Shopify-Hmac-Sha256": sign(SECRET, body),
         SHOP_HEADER: THEIRS}, body)
    assert verdict["status"] == hook.SIGNATURE_WRONG_SHOP


# --- comparing hosts -----------------------------------------------------

@pytest.mark.parametrize("sent", [
    OURS,
    OURS.upper(),
    f"https://{OURS}",
    f"https://{OURS}/",
    f"  {OURS}  ",
])
def test_our_own_shop_is_recognised_however_it_is_written(receiver, sent):
    """The value compared here is the same variable store_domain() reads,
    and that one tolerates a scheme and a trailing slash -- so a pasted
    URL must not read as a different shop."""
    body = b"{}"
    verdict = hook.verify_signature(
        {"X-Shopify-Hmac-Sha256": sign(SECRET, body), SHOP_HEADER: sent},
        body)
    assert verdict["status"] == hook.SIGNATURE_VERIFIED


def test_the_shop_header_is_read_case_insensitively(receiver):
    """Shopify's own instruction about header names, and the reason the
    receiver has a `header` helper at all."""
    body = b"{}"
    verdict = hook.verify_signature(
        {"X-Shopify-Hmac-Sha256": sign(SECRET, body),
         "x-shopify-shop-domain": THEIRS}, body)
    assert verdict["status"] == hook.SIGNATURE_WRONG_SHOP


# --- what cannot be judged ------------------------------------------------

def test_a_delivery_with_no_shop_header_is_accepted_and_says_so(receiver,
                                                                logs):
    """★ ACCEPTED, DELIBERATELY. Shopify documents sending the header, so
    its absence is a Shopify change rather than an attack -- and refusing
    would stop order intake over a header nobody needed yesterday. The
    HMAC has already passed, so this is no weaker than the behaviour
    before the fence existed. It is logged, so an unjudged delivery is
    never mistaken for a checked one."""
    body = b"{}"
    verdict = hook.verify_signature(
        {"X-Shopify-Hmac-Sha256": sign(SECRET, body)}, body)
    assert verdict["status"] == hook.SIGNATURE_VERIFIED
    assert any("the sending shop was not checked" in r.getMessage()
               and "no shop-domain header" in r.getMessage()
               for r in logs.records)


def test_an_unconfigured_app_cannot_refuse_on_a_shop_it_does_not_know(
        monkeypatch, logs):
    """★ A FENCE NEEDS TWO POSTS. With SHOPIFY_STORE_DOMAIN unset there
    is nothing to compare against, and that is every test worktree -- the
    exact condition that broke six tests in test_shopify_pickup.py once
    before. So it is accepted and logged, never refused."""
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", SECRET)
    monkeypatch.delenv("SHOPIFY_STORE_DOMAIN", raising=False)
    body = b"{}"
    verdict = hook.verify_signature(
        {"X-Shopify-Hmac-Sha256": sign(SECRET, body),
         SHOP_HEADER: THEIRS}, body)
    assert verdict["status"] == hook.SIGNATURE_VERIFIED
    assert any("SHOPIFY_STORE_DOMAIN is not configured" in r.getMessage()
               for r in logs.records)


# --- the write allow-list it travels with --------------------------------

def test_both_stores_may_be_written_to_and_a_third_may_not(monkeypatch):
    """★ THE ALLOW-LIST IS AN AUTHORIZATION FENCE, NOT A ROUTER. Listing
    two stores does not mean two stores get written to: which one does is
    SHOPIFY_STORE_DOMAIN, and authorize_writes only asks whether that
    value is permitted."""
    for domain in shopify_service.ALLOWED_WRITE_DOMAINS:
        monkeypatch.setattr(shopify_service, "SHOPIFY_STORE_DOMAIN", domain)
        authorization = shopify_service.authorize_writes(
            confirm=True, reason="test: an allow-listed store")
        assert authorization.domain == domain

    monkeypatch.setattr(shopify_service, "SHOPIFY_STORE_DOMAIN",
                        "not-ours.myshopify.com")
    with pytest.raises(shopify_service.ShopifyWriteRefused) as refusal:
        shopify_service.authorize_writes(confirm=True, reason="test: a third")
    assert "not on the Shopify write allow-list" in str(refusal.value)


def test_the_real_shop_is_on_the_write_allow_list():
    """Pinned because the cutover depends on it and because the dev store
    is expected to LEAVE this list later -- the day it does, nothing
    should quietly take the real shop with it."""
    assert "1qtxz2-fz.myshopify.com" in shopify_service.ALLOWED_WRITE_DOMAINS
    assert len(shopify_service.ALLOWED_WRITE_DOMAINS) == 2
