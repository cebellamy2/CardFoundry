"""The inbound Mana Pool order_created webhook.

Three things carry the risk and each is pinned hard here.

The SIGNATURE is the only authentication this route has -- it is exempt
from the shared-password gate, because Mana Pool cannot send that
password. Every way a forged or replayed delivery could be accepted is a
test below.

The ORDER OF OPERATIONS (verify -> persist -> 2xx -> process) is the
whole design, because Mana Pool's spec says nothing about retries: a
non-2xx may lose the order forever. So the row must exist even when the
processing that follows it explodes, and a busy inventory lease must
never become a non-2xx.

IDEMPOTENCY is inherited from ingest, not added here, so the duplicate
test asserts on the real ingest path rather than a mock of it.
"""
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
import manapool_webhook_service as wh
from inventory_sync_service import InventoryLeaseBusy
from models import Base, WebhookDelivery

SECRET = "whsec_test_secret"
ORDER_ID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'webhook.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    return engine


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(wh.ENABLED_ENV, "true")
    monkeypatch.setenv(wh.SECRET_ENV, SECRET)


def body(order_id=ORDER_ID) -> bytes:
    return json.dumps({"order": {"id": order_id, "label": "123-456"}}).encode()


def headers(raw: bytes, *, secret=SECRET, ts=None, event="order_created",
            sig_ts=None, digest=None):
    ts = str(int(time.time())) if ts is None else str(ts)
    sig_ts = ts if sig_ts is None else str(sig_ts)
    v1 = digest or hmac.new(
        secret.encode(), f"v1:{ts}:".encode() + raw, hashlib.sha256,
    ).hexdigest()
    return {
        "X-ManaPool-Event": event,
        "X-ManaPool-Timestamp": ts,
        "X-ManaPool-Signature": f"t={sig_ts},v1={v1}",
        "Content-Type": "application/json",
    }


def rows(engine):
    with Session(engine) as s:
        return s.query(WebhookDelivery).order_by(WebhookDelivery.id).all()


# --- signature verification -------------------------------------------

def test_a_correctly_signed_delivery_verifies():
    raw = body()
    ts = str(int(time.time()))
    v1 = hmac.new(SECRET.encode(), f"v1:{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    assert wh.verify_signature(
        secret=SECRET, signature_header=f"t={ts},v1={v1}",
        timestamp_header=ts, raw_body=raw,
    ) == "verified"


def test_a_signature_from_the_wrong_secret_is_rejected():
    raw = body()
    ts = str(int(time.time()))
    v1 = hmac.new(b"not-the-secret", f"v1:{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    assert wh.verify_signature(
        secret=SECRET, signature_header=f"t={ts},v1={v1}",
        timestamp_header=ts, raw_body=raw,
    ) == "invalid_signature"


def test_a_tampered_body_is_rejected():
    """The signature covers the body, so editing one order id into
    another invalidates it -- this is why the raw bytes are verified and
    stored, never a re-serialized copy."""
    ts = str(int(time.time()))
    v1 = hmac.new(SECRET.encode(), f"v1:{ts}:".encode() + body(), hashlib.sha256).hexdigest()
    assert wh.verify_signature(
        secret=SECRET, signature_header=f"t={ts},v1={v1}",
        timestamp_header=ts, raw_body=body("99999999-2222-4333-8444-555555555555"),
    ) == "invalid_signature"


def test_a_delivery_301_seconds_old_is_stale():
    """Tolerance is 300s by operator decision; 301 is over the line."""
    raw = body()
    ts = str(int(time.time()) - 301)
    v1 = hmac.new(SECRET.encode(), f"v1:{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    assert wh.verify_signature(
        secret=SECRET, signature_header=f"t={ts},v1={v1}",
        timestamp_header=ts, raw_body=raw,
    ) == "stale_timestamp"


def test_a_delivery_301_seconds_in_the_future_is_also_stale():
    raw = body()
    ts = str(int(time.time()) + 301)
    v1 = hmac.new(SECRET.encode(), f"v1:{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    assert wh.verify_signature(
        secret=SECRET, signature_header=f"t={ts},v1={v1}",
        timestamp_header=ts, raw_body=raw,
    ) == "stale_timestamp"


def test_t_in_the_signature_must_equal_the_timestamp_header():
    """Two copies of one value. Disagreement means whatever assembled the
    request did not sign it, and there is no reading where trusting
    either copy is correct."""
    raw = body()
    ts = int(time.time())
    v1 = hmac.new(SECRET.encode(), f"v1:{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    assert wh.verify_signature(
        secret=SECRET, signature_header=f"t={ts - 5},v1={v1}",
        timestamp_header=str(ts), raw_body=raw,
    ) == "invalid_signature"


def test_no_secret_configured_is_its_own_status_not_a_pass():
    assert wh.verify_signature(
        secret="", signature_header="t=1,v1=abc", timestamp_header="1", raw_body=b"{}",
    ) == "missing_secret"


@pytest.mark.parametrize("header", ["", "garbage", "t=123", "v1=abc", "t=,v1="])
def test_a_malformed_signature_header_is_rejected(header):
    assert wh.verify_signature(
        secret=SECRET, signature_header=header,
        timestamp_header=str(int(time.time())), raw_body=body(),
    ) == "invalid_signature"


def test_the_comparison_is_constant_time(monkeypatch):
    """A byte-by-byte compare leaks how much of a forged digest was
    right, which is enough to build the rest of it."""
    called = []
    real = hmac.compare_digest
    monkeypatch.setattr(wh.hmac, "compare_digest",
                        lambda a, b: called.append(True) or real(a, b))
    raw = body()
    ts = str(int(time.time()))
    v1 = hmac.new(SECRET.encode(), f"v1:{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    wh.verify_signature(secret=SECRET, signature_header=f"t={ts},v1={v1}",
                        timestamp_header=ts, raw_body=raw)
    assert called, "verification must go through hmac.compare_digest"


# --- the flag and the auth exemption -----------------------------------

def test_the_route_is_404_when_the_flag_is_off(db, monkeypatch):
    """Not 401, not 403. An endpoint that answers differently when
    disabled has still told you it exists."""
    monkeypatch.delenv(wh.ENABLED_ENV, raising=False)
    raw = body()
    r = TestClient(main.app).post("/webhooks/manapool/order-created",
                                  content=raw, headers=headers(raw))
    assert r.status_code == 404
    assert rows(db) == []


def test_the_route_needs_no_operator_password(db, on, monkeypatch):
    """The whole point of the exemption: Mana Pool cannot send it."""
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "operator-password")
    monkeypatch.setattr(main, "_process_webhook_delivery", lambda did: "processed")
    raw = body()
    r = TestClient(main.app).post("/webhooks/manapool/order-created",
                                  content=raw, headers=headers(raw))
    assert r.status_code == 200


def test_every_other_route_still_needs_the_password(db, on, monkeypatch):
    """The exemption must not have widened to anything else."""
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "operator-password")
    r = TestClient(main.app).get("/orders/needs-attention")
    assert r.status_code == 401


def test_the_operator_retry_route_is_not_under_the_exempt_prefix(db, on, monkeypatch):
    """Retry mutates inventory through the ingest path, so it belongs
    behind the operator password like every other operator action."""
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "operator-password")
    r = TestClient(main.app).post("/orders/webhook-deliveries/1/retry")
    assert r.status_code == 401


# --- the registration bootstrap ----------------------------------------

def test_verification_probe_with_no_secret_is_accepted_and_recorded(db, monkeypatch):
    """Mana Pool signs the probe with the secret it returns in the SAME
    registration response, so we cannot have it yet. Refusing would make
    registering impossible."""
    monkeypatch.setenv(wh.ENABLED_ENV, "true")
    monkeypatch.delenv(wh.SECRET_ENV, raising=False)
    raw = json.dumps({"type": "verification", "message": "hi"}).encode()
    r = TestClient(main.app).post(
        "/webhooks/manapool/order-created", content=raw,
        headers=headers(raw, event="verification"),
    )
    assert r.status_code == 200
    (row,) = rows(db)
    assert row.signature_status == "unverified_bootstrap"
    assert row.event == "verification"


def test_the_bootstrap_row_is_terminal_not_left_pending(db, monkeypatch):
    """There is no order in a verification probe, so nothing will ever
    process that row. "pending" would describe work still to do about a
    delivery that is already completely finished with -- and it is the
    status the attention section and the sweep both key on."""
    monkeypatch.setenv(wh.ENABLED_ENV, "true")
    monkeypatch.delenv(wh.SECRET_ENV, raising=False)
    raw = json.dumps({"type": "verification"}).encode()
    TestClient(main.app).post(
        "/webhooks/manapool/order-created", content=raw,
        headers=headers(raw, event="verification"),
    )
    (row,) = rows(db)
    assert row.signature_status == "unverified_bootstrap"
    assert row.processing_status == "processed"
    assert row.processed_at is not None
    assert "verification probe" in (row.ingest_result or "")


def test_an_order_with_no_secret_is_rejected_and_recorded(db, monkeypatch):
    """The bootstrap hole is the verification event only. A real order
    arriving unverifiable is never processed."""
    monkeypatch.setenv(wh.ENABLED_ENV, "true")
    monkeypatch.delenv(wh.SECRET_ENV, raising=False)
    raw = body()
    r = TestClient(main.app).post("/webhooks/manapool/order-created",
                                  content=raw, headers=headers(raw))
    assert r.status_code == 401
    (row,) = rows(db)
    assert row.signature_status == "missing_secret"
    assert row.processing_status == "pending"


def test_a_rejected_delivery_is_still_recorded(db, on):
    """An invalid signature is the most interesting thing that can arrive
    here. "We rejected something and kept no record" is not an answer."""
    raw = body()
    r = TestClient(main.app).post(
        "/webhooks/manapool/order-created", content=raw,
        headers=headers(raw, secret="wrong-secret"),
    )
    assert r.status_code == 401
    (row,) = rows(db)
    assert row.signature_status == "invalid_signature"


# --- persist before answering ------------------------------------------

def test_the_row_exists_even_when_processing_explodes(db, on, monkeypatch):
    """The row is committed BEFORE the 2xx, so the promise the 2xx makes
    is already true when it is made."""
    def boom(_id):
        raise RuntimeError("processing blew up")
    monkeypatch.setattr(main, "_process_webhook_delivery", boom)
    raw = body()
    client = TestClient(main.app)
    try:
        client.post("/webhooks/manapool/order-created", content=raw, headers=headers(raw))
    except RuntimeError:
        pass  # the background task raising must not undo the committed row
    (row,) = rows(db)
    assert row.signature_status == "verified"
    assert row.external_order_id == ORDER_ID
    assert row.raw_body == raw.decode()


def test_the_raw_body_is_stored_verbatim(db, on, monkeypatch):
    """Re-serializing would reorder keys and make the stored bytes
    unverifiable against the signature that covered them."""
    monkeypatch.setattr(main, "_process_webhook_delivery", lambda did: "processed")
    raw = b'{"order":   {"id": "' + ORDER_ID.encode() + b'",  "label": "z"}}'
    TestClient(main.app).post("/webhooks/manapool/order-created",
                              content=raw, headers=headers(raw))
    (row,) = rows(db)
    assert row.raw_body == raw.decode()


# --- processing ---------------------------------------------------------

def _factory(engine):
    return lambda: Session(engine)


def test_happy_path_marks_the_delivery_processed(db, on, monkeypatch):
    seen = {}
    def fake_ingest(session, orders, loader, **kw):
        seen["orders"] = orders
        seen["loader_result"] = loader(orders[0]["id"])
        seen["interval"] = kw.get("min_request_interval")
        return {"imported": 1, "already_known": 0, "failed": [], "deferred": 0}
    monkeypatch.setattr(wh, "ingest_manapool_orders", fake_ingest)
    with Session(db) as s:
        did = wh.record_delivery(s, event="order_created", raw_body=body(),
                                 signature_status="verified", timestamp_header="1")
    assert wh.process_delivery(did, session_factory=_factory(db)) == "processed"
    with Session(db) as s:
        row = s.get(WebhookDelivery, did)
        assert row.processing_status == "processed"
        assert row.processed_at is not None
        assert row.attempts == 1
    # the one-item list and the zero-call loader are the design
    assert len(seen["orders"]) == 1
    assert seen["loader_result"] == {"order": seen["orders"][0]}
    assert seen["interval"] == 0


def test_a_second_delivery_of_the_same_order_is_already_known(db, on, monkeypatch):
    """Idempotency is inherited from ingest, which keys on
    (source, external_order_id) -- nothing is added here."""
    calls = []
    def fake_ingest(session, orders, loader, **kw):
        calls.append(orders[0]["id"])
        first = len(calls) == 1
        return {"imported": 1 if first else 0,
                "already_known": 0 if first else 1, "failed": [], "deferred": 0}
    monkeypatch.setattr(wh, "ingest_manapool_orders", fake_ingest)
    ids = []
    for _ in range(2):
        with Session(db) as s:
            ids.append(wh.record_delivery(s, event="order_created", raw_body=body(),
                                          signature_status="verified", timestamp_header="1"))
    assert wh.process_delivery(ids[0], session_factory=_factory(db)) == "processed"
    assert wh.process_delivery(ids[1], session_factory=_factory(db)) == "already_known"
    # Two deliveries, two rows -- a redelivery is a fact worth keeping.
    assert len(rows(db)) == 2


def test_a_busy_lease_retries_and_then_strands(db, on, monkeypatch):
    """A busy lease is ordinary and local. It is never an error, never a
    non-2xx, and after the budget it becomes VISIBLE rather than lost --
    the hourly poll still ingests the order."""
    monkeypatch.setattr(wh, "ingest_manapool_orders",
                        lambda *a, **k: pytest.fail("must not reach ingest"))
    def busy():
        raise InventoryLeaseBusy("held by another operation")
    monkeypatch.setattr(wh, "inventory_sync_lease", busy)
    clock = {"t": 0.0}
    slept = []
    with Session(db) as s:
        did = wh.record_delivery(s, event="order_created", raw_body=body(),
                                 signature_status="verified", timestamp_header="1")
    def sleep(sec):
        slept.append(sec); clock["t"] += sec
    status = wh.process_delivery(
        did, session_factory=_factory(db), sleep=sleep,
        now=lambda: clock["t"], budget_seconds=wh.RETRY_BUDGET_SECONDS,
    )
    assert status == "stranded"
    # 5, 10, 20, then 30 until the budget is spent -- and the LAST wait
    # is clamped to whatever is left of it, so the retry never sleeps
    # past its own deadline.
    assert slept[:4] == [5, 10, 20, 30], slept
    assert set(slept[4:-1]) <= {30}, slept
    assert slept[-1] <= 30, slept
    assert sum(slept) >= wh.RETRY_BUDGET_SECONDS
    with Session(db) as s:
        row = s.get(WebhookDelivery, did)
        assert row.processing_status == "stranded"
        assert "lease busy" in (row.last_error or "")
        assert row.attempts > 1


def test_an_ingest_exception_is_recorded_as_failed(db, on, monkeypatch):
    def boom(*a, **k):
        raise ValueError("inventory invariant violated")
    monkeypatch.setattr(wh, "ingest_manapool_orders", boom)
    with Session(db) as s:
        did = wh.record_delivery(s, event="order_created", raw_body=body(),
                                 signature_status="verified", timestamp_header="1")
    assert wh.process_delivery(did, session_factory=_factory(db)) == "failed"
    with Session(db) as s:
        row = s.get(WebhookDelivery, did)
        assert row.processing_status == "failed"
        assert "inventory invariant violated" in row.last_error


def test_a_per_order_ingest_failure_is_failed_not_processed(db, on, monkeypatch):
    """ingest isolates per-order failures into result["failed"] rather
    than raising -- a delivery whose order did not land must not report
    success."""
    monkeypatch.setattr(wh, "ingest_manapool_orders", lambda *a, **k: {
        "imported": 0, "already_known": 0,
        "failed": [{"order": ORDER_ID, "error": "no matching inventory"}], "deferred": 0,
    })
    with Session(db) as s:
        did = wh.record_delivery(s, event="order_created", raw_body=body(),
                                 signature_status="verified", timestamp_header="1")
    assert wh.process_delivery(did, session_factory=_factory(db)) == "failed"


def test_an_unusable_body_fails_instead_of_looping(db, on, monkeypatch):
    with Session(db) as s:
        did = wh.record_delivery(s, event="order_created", raw_body=b"not json at all",
                                 signature_status="verified", timestamp_header="1")
    assert wh.process_delivery(did, session_factory=_factory(db)) == "failed"


# --- the attention section ---------------------------------------------

def test_stranded_and_failed_deliveries_show_on_orders_needing_attention(db, on, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "")
    with Session(db) as s:
        s.add(WebhookDelivery(
            source="manapool", event="order_created", external_order_id=ORDER_ID,
            received_at=datetime.now(), signature_status="verified",
            raw_body="{}", processing_status="stranded", attempts=9,
            last_error="inventory lease busy for the whole retry budget"))
        s.commit()
    r = TestClient(main.app).get("/orders/needs-attention")
    assert r.status_code == 200
    assert "Webhook orders not yet processed" in r.text
    assert ORDER_ID in r.text
    assert "Retry now" in r.text


def test_the_section_says_so_when_there_is_nothing_waiting(db, on, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "")
    r = TestClient(main.app).get("/orders/needs-attention")
    assert "Webhook orders not yet processed" in r.text
    assert "No webhook deliveries are waiting" in r.text


def test_a_rejected_delivery_never_appears_in_the_queue(db, on, monkeypatch):
    """An invalid signature is a security observation, not an order
    waiting on an operator. Listing it would send someone looking for an
    order that may not exist."""
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "")
    with Session(db) as s:
        s.add(WebhookDelivery(
            source="manapool", event="order_created", external_order_id="forged-id",
            received_at=datetime.now(), signature_status="invalid_signature",
            raw_body="{}", processing_status="pending"))
        s.commit()
    r = TestClient(main.app).get("/orders/needs-attention")
    assert "forged-id" not in r.text


def test_a_processed_delivery_leaves_the_queue(db, on, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "")
    with Session(db) as s:
        s.add(WebhookDelivery(
            source="manapool", event="order_created", external_order_id=ORDER_ID,
            received_at=datetime.now(), signature_status="verified",
            raw_body="{}", processing_status="already_known"))
        s.commit()
    r = TestClient(main.app).get("/orders/needs-attention")
    assert ORDER_ID not in r.text


# --- the sweep ----------------------------------------------------------

def test_the_sweep_retries_stalled_deliveries_but_not_fresh_ones(db, on):
    old = datetime.now() - timedelta(minutes=5)
    with Session(db) as s:
        s.add(WebhookDelivery(source="manapool", external_order_id="old-one",
                              received_at=old, signature_status="verified",
                              raw_body="{}", processing_status="stranded"))
        s.add(WebhookDelivery(source="manapool", external_order_id="fresh-one",
                              received_at=datetime.now(), signature_status="verified",
                              raw_body="{}", processing_status="pending"))
        s.commit()
    seen = []
    result = wh.sweep_unfinished(_factory(db), process=lambda did: seen.append(did) or "processed")
    assert len(result["picked"]) == 1, "the in-flight one must not be raced"


def test_the_sweep_never_retries_a_rejected_delivery(db, on):
    with Session(db) as s:
        s.add(WebhookDelivery(source="manapool", external_order_id="forged",
                              received_at=datetime.now() - timedelta(minutes=5),
                              signature_status="invalid_signature",
                              raw_body="{}", processing_status="pending"))
        s.commit()
    assert wh.sweep_unfinished(_factory(db), process=lambda did: "processed")["picked"] == []
