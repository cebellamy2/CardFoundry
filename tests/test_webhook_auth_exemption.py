"""The HMAC-authenticated webhook receivers get past the operator gate.

★ WHY THIS FILE EXISTS, AND WHY IT HAS TO TURN A FLAG OFF.

v2.97.0 shipped POST /webhooks/shopify/orders and did NOT add it to the
auth gate's exemption, so the live app answered Shopify 401 before the
route ran -- a receiver nothing could reach. Every other test in the
suite passed, because `main.DEV_AUTH_DISABLED` is on in local
development and the gate returns early for every request: the suite had
no coverage of that middleware at all. It was found by POSTing to the
live app.

So these tests turn DEV_AUTH_DISABLED off, which is the only way to
exercise the gate, and they assert the OUTCOME a marketplace sees: not
401. The route's own enable flag and HMAC check are what actually
protect it, and they have their own tests -- what is pinned here is that
a signed delivery is allowed to reach them.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base


@pytest.fixture
def gated(tmp_path, monkeypatch):
    """The gate as production runs it: dev opt-out off, a service secret
    configured, and no operator session."""
    db = create_engine(f"sqlite:///{tmp_path / 'gate.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(main, "DEV_AUTH_DISABLED", False)
    monkeypatch.setattr(main, "SERVICE_PASSWORD", "service-secret")
    return db


# --- the property that was broken ----------------------------------------

@pytest.mark.parametrize("path", [
    "/webhooks/shopify/orders",
    "/webhooks/manapool/order-created",
])
def test_a_webhook_receiver_is_never_refused_by_the_operator_gate(gated, path,
                                                                  monkeypatch):
    """★ THE ONE THAT MATTERS. A marketplace cannot hold an operator
    session, so a 401 here is a receiver nothing can reach."""
    monkeypatch.delenv("SHOPIFY_WEBHOOK_ENABLED", raising=False)
    monkeypatch.delenv("MANAPOOL_WEBHOOK_ENABLED", raising=False)
    response = TestClient(main.app).post(path, content=b"{}")
    assert response.status_code != 401, response.status_code
    # Disabled, which is what an un-flagged deployment should answer --
    # and it proves the ROUTE answered, not the gate.
    assert response.status_code == 404, response.status_code


def test_an_ordinary_page_is_still_refused_without_a_session(gated):
    """The exemption must not have opened anything else."""
    response = TestClient(main.app).get("/orders")
    assert response.status_code == 401


def test_the_exemption_is_prefixes_and_cannot_match_a_lookalike(gated):
    """Each entry keeps its trailing slash, so a path that merely starts
    with the same letters is not exempt."""
    assert all(prefix.endswith("/")
               for prefix in main.HMAC_AUTHENTICATED_WEBHOOK_PREFIXES)
    response = TestClient(main.app).post("/webhooks/shopifyX", content=b"{}")
    assert response.status_code == 401, response.status_code


def test_both_receivers_are_covered_by_the_one_list(gated):
    """Two marketplaces, one exemption. A third receiver added without
    its prefix would be the same bug again."""
    assert main.HMAC_AUTHENTICATED_WEBHOOK_PREFIXES == (
        "/webhooks/manapool/", "/webhooks/shopify/")


def test_a_webhook_request_is_attributed_to_the_webhook_actor(gated,
                                                              monkeypatch):
    """Set BEFORE the early return, because the receiver writes rows."""
    seen = []
    monkeypatch.setattr(main, "set_actor", lambda actor: seen.append(actor))
    monkeypatch.delenv("SHOPIFY_WEBHOOK_ENABLED", raising=False)
    TestClient(main.app).post("/webhooks/shopify/orders", content=b"{}")
    assert seen, "nothing was attributed"
    assert "webhook" in str(seen[0]).lower()
