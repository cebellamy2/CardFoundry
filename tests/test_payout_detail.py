"""One payout's detail, reachable from BOTH payout lists, identical content.

Operator request, 2026-09-28: "I need the payouts in the consignor screen
to be a clickable link showing what cards were paid in that payout with the
notes that were entered at that time" and "I want both the consignor and the
user to be able to click that link and get the same info."

So the content is produced by ONE renderer, _payout_detail_content_html, and
the two routes wrap it in their own page chrome. The tests below assert the
two bodies are byte-identical over that shared region, which is what stops
them drifting.

THE NOTE "AT THAT TIME". correct_consignor_payout edits payout.note IN
PLACE; the original survives only inside the first correction's `before`
blob. payout_detail reconstructs it from there when a correction exists, and
otherwise the current note IS the original. A note changed by anything other
than that function (a direct script or SQL edit) leaves no log and cannot be
detected -- see payout_detail's own docstring.
"""
import json
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from consignment_service import payout_detail
from consignor_auth_service import set_consignor_portal_credentials
from models import (Base, Batch, Consignor, ConsignorPayout,
                    ConsignorPayoutChangeLog, InventoryCard, OrderItem,
                    PickAllocation, SalesOrder)


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'payout-detail.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(database, "engine", db)
    return db


def make_consignor(db, *, name="Jane", username=None, password="secretpw"):
    with Session(db) as session:
        consignor = Consignor(name=name, is_active=True)
        session.add(consignor)
        session.flush()
        if username:
            set_consignor_portal_credentials(session, consignor.id, username, password)
        session.commit()
        session.refresh(consignor)
        return consignor


def make_payout(db, consignor_id, *, amount=10.0, method="PayPal",
                note="Paid in full", paid_at=None):
    with Session(db) as session:
        payout = ConsignorPayout(
            consignor_id=consignor_id, amount=amount, method=method, note=note,
            paid_at=paid_at or datetime(2026, 9, 14),
        )
        session.add(payout)
        session.commit()
        session.refresh(payout)
        return payout


def make_paid_card(db, consignor_id, payout_id, *, name="Alpha", owed=10.0,
                   sold_price=20.0, shipped_at=datetime(2026, 9, 1),
                   set_code="ONE", collector_number="1", condition_id="NM",
                   finish_id="NF", language_id="EN"):
    """A sold, paid consignment card linked to `payout_id`, with a shipped
    allocation so its sale date resolves the same way the portal's does."""
    with Session(db) as session:
        batch = Batch(batch_code=f"CON-{consignor_id}-{name}-{payout_id}",
                      is_consignment=True, consignor_id=consignor_id)
        session.add(batch)
        session.flush()
        card = InventoryCard(
            batch_id=batch.id, name=name, status="sold", set_code=set_code,
            collector_number=collector_number, condition_id=condition_id,
            finish_id=finish_id, language_id=language_id,
            sold_price=sold_price, consignment_amount_owed=owed,
            consignment_payout_status="paid", consignment_payout_id=payout_id,
        )
        session.add(card)
        order = SalesOrder(external_order_id=f"ord-{name}-{payout_id}",
                           status="shipped", shipped_at=shipped_at)
        session.add(order)
        session.flush()
        item = OrderItem(order_id=order.id, name=name, quantity=1)
        session.add(item)
        session.flush()
        session.add(PickAllocation(
            order_item_id=item.id, inventory_card_id=card.id,
            batch_id=batch.id, status="shipped",
        ))
        session.commit()
        session.refresh(card)
        return card


def add_correction(db, payout_id, *, before, after, reason, created_at=None):
    with Session(db) as session:
        session.add(ConsignorPayoutChangeLog(
            consignor_payout_id=payout_id,
            change_summary=json.dumps({
                "action_type": "payout_correction", "before": before,
                "after": after, "correction_reason": reason,
            }, sort_keys=True),
            created_at=created_at or datetime(2026, 9, 20),
        ))
        session.commit()


def login(client, username, password="secretpw"):
    return client.post("/portal/login",
                       data={"username": username, "password": password},
                       follow_redirects=False)


def shared_region(body):
    """The part both pages render from the one shared renderer."""
    start = body.index("<h2>Payout</h2>")
    end = body.index("</table>", body.index("Paid for this card"))
    return body[start:end]


# --- the same content in both places ------------------------------------

def test_operator_and_portal_render_identical_detail_content(tmp_path, monkeypatch):
    """★ The whole point of the shared renderer."""
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, username="jane@example.com")
    payout = make_payout(db, consignor.id, amount=30.0, note="Cash at the shop")
    make_paid_card(db, consignor.id, payout.id, name="Alpha", owed=10.0)
    make_paid_card(db, consignor.id, payout.id, name="Beta", owed=20.0)

    operator = TestClient(main.app).get(f"/consignors/payouts/{payout.id}")
    assert operator.status_code == 200

    portal_client = TestClient(main.app)
    login(portal_client, "jane@example.com")
    portal = portal_client.get(f"/portal/payouts/{payout.id}")
    assert portal.status_code == 200

    assert shared_region(operator.text) == shared_region(portal.text)
    for expected in ("Cash at the shop", "Alpha", "Beta", "$30.00",
                     "$10.00", "$20.00", "ONE", "#1", "NM / NF / EN"):
        assert expected in operator.text
        assert expected in portal.text


def test_both_lists_link_to_the_detail_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, username="jane@example.com")
    payout = make_payout(db, consignor.id)
    make_paid_card(db, consignor.id, payout.id)

    operator = TestClient(main.app).get(f"/consignors/{consignor.id}/payouts")
    assert f'href="/consignors/payouts/{payout.id}"' in operator.text
    # The existing correction link is untouched.
    assert f'href="/consignors/payouts/{payout.id}/edit"' in operator.text

    portal_client = TestClient(main.app)
    login(portal_client, "jane@example.com")
    portal = portal_client.get("/portal/payouts")
    assert f'href="/portal/payouts/{payout.id}"' in portal.text


def test_portal_preview_mirror_links_where_the_operator_can_follow(tmp_path, monkeypatch):
    """The operator's Portal Preview section shows the consignor's table.
    Its link must reach the OPERATOR route -- a /portal/ link would only
    bounce him to the consignor login."""
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db)
    payout = make_payout(db, consignor.id)
    make_paid_card(db, consignor.id, payout.id)

    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    assert response.status_code == 200
    assert f'href="/consignors/payouts/{payout.id}"' in response.text
    assert f'href="/portal/payouts/{payout.id}"' not in response.text


# --- the cards listed, and the total ------------------------------------

def test_cards_listed_are_exactly_the_payouts_cards(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db)
    payout = make_payout(db, consignor.id, amount=10.0)
    other_payout = make_payout(db, consignor.id, amount=99.0, note="Other")
    make_paid_card(db, consignor.id, payout.id, name="Mine", owed=10.0)
    make_paid_card(db, consignor.id, other_payout.id, name="NotMine", owed=99.0)

    response = TestClient(main.app).get(f"/consignors/payouts/{payout.id}")
    assert "Mine" in response.text
    assert "NotMine" not in response.text
    assert "Cards paid in this payout (1)" in response.text


def test_total_reconciles_and_says_so(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db)
    payout = make_payout(db, consignor.id, amount=30.0)
    make_paid_card(db, consignor.id, payout.id, name="Alpha", owed=12.5)
    make_paid_card(db, consignor.id, payout.id, name="Beta", owed=17.5)

    response = TestClient(main.app).get(f"/consignors/payouts/{payout.id}")
    assert "$30.00" in response.text
    assert "matches the payout amount" in response.text
    assert "do not reconcile" not in response.text


def test_a_mismatch_is_stated_on_the_page_not_hidden(tmp_path, monkeypatch):
    """★ A consignor comparing their own arithmetic deserves to see the
    discrepancy, not a total quietly rewritten to match."""
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, username="jane@example.com")
    payout = make_payout(db, consignor.id, amount=30.0)
    make_paid_card(db, consignor.id, payout.id, name="Alpha", owed=12.0)

    operator = TestClient(main.app).get(f"/consignors/payouts/{payout.id}")
    assert "do not reconcile" in operator.text
    assert "$18.00 less than" in operator.text

    # The consignor sees the same admission, not a cleaned-up page.
    portal_client = TestClient(main.app)
    login(portal_client, "jane@example.com")
    portal = portal_client.get(f"/portal/payouts/{payout.id}")
    assert "do not reconcile" in portal.text
    assert shared_region(operator.text) == shared_region(portal.text)


def test_a_payout_with_no_cards_says_so(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db)
    payout = make_payout(db, consignor.id, amount=5.0)

    response = TestClient(main.app).get(f"/consignors/payouts/{payout.id}")
    assert response.status_code == 200
    assert "No cards are linked to this payout." in response.text
    assert "do not reconcile" in response.text


def test_a_payout_with_no_note_says_so(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db)
    payout = make_payout(db, consignor.id, note=None)

    response = TestClient(main.app).get(f"/consignors/payouts/{payout.id}")
    assert "No note was entered with this payout." in response.text


# --- correction history, and the original note --------------------------

def test_correction_history_shows_and_the_original_note_is_preserved(tmp_path, monkeypatch):
    """★ The note displayed is the one entered AT THE TIME, never the
    corrected one, and the correction is listed separately beneath -- on
    BOTH surfaces."""
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, username="jane@example.com")
    payout = make_payout(db, consignor.id, amount=25.0, note="Corrected note")
    make_paid_card(db, consignor.id, payout.id, owed=25.0)
    add_correction(
        db, payout.id,
        before={"amount": 20.0, "method": "PayPal", "note": "Original note",
                "paid_at": "2026-09-14T00:00:00"},
        after={"amount": 25.0, "method": "PayPal", "note": "Corrected note",
               "paid_at": "2026-09-14T00:00:00"},
        reason="Undercounted one card",
    )

    operator = TestClient(main.app).get(f"/consignors/payouts/{payout.id}")
    portal_client = TestClient(main.app)
    login(portal_client, "jane@example.com")
    portal = portal_client.get(f"/portal/payouts/{payout.id}")

    for response in (operator, portal):
        assert response.status_code == 200
        body = response.text
        assert "Original note" in body, "the note as entered at the time"
        assert "Corrections after this payout was recorded" in body
        assert "Undercounted one card" in body
        # The before -> after transition itself, both halves present.
        assert "20.0" in body
        assert "$25.00" in body, "the corrected amount is the payout amount now"
        assert "Amount:" in body

    assert shared_region(operator.text) == shared_region(portal.text)


def test_the_earliest_correction_supplies_the_original_note(tmp_path, monkeypatch):
    """Two corrections: the FIRST one's `before` is the original, not the
    second's."""
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db)
    payout = make_payout(db, consignor.id, note="Third note")
    add_correction(
        db, payout.id, before={"note": "First note"}, after={"note": "Second note"},
        reason="First fix", created_at=datetime(2026, 9, 18),
    )
    add_correction(
        db, payout.id, before={"note": "Second note"}, after={"note": "Third note"},
        reason="Second fix", created_at=datetime(2026, 9, 19),
    )

    with Session(main.engine) as session:
        view = payout_detail(session, payout.id)
    assert view["note_at_record_time"] == "First note"
    assert view["note_at_record_time_is_certain"] is True

    response = TestClient(main.app).get(f"/consignors/payouts/{payout.id}")
    assert "First note" in response.text
    assert "First fix" in response.text and "Second fix" in response.text


def test_an_unreadable_correction_is_surfaced_not_hidden(tmp_path, monkeypatch):
    """An unreadable audit row is itself information -- skipping it would
    make a corrected payout look untouched."""
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db)
    payout = make_payout(db, consignor.id, note="Current note")
    with Session(db) as session:
        session.add(ConsignorPayoutChangeLog(
            consignor_payout_id=payout.id, change_summary="{not json",
        ))
        session.commit()

    with Session(main.engine) as session:
        view = payout_detail(session, payout.id)
    assert view["was_corrected"] is True
    assert view["note_at_record_time_is_certain"] is False

    response = TestClient(main.app).get(f"/consignors/payouts/{payout.id}")
    assert "could not be read" in response.text
    assert "Current note" in response.text


def test_no_corrections_means_the_current_note_is_the_original(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db)
    payout = make_payout(db, consignor.id, note="As entered")

    with Session(main.engine) as session:
        view = payout_detail(session, payout.id)
    assert view["note_at_record_time"] == "As entered"
    assert view["was_corrected"] is False

    response = TestClient(main.app).get(f"/consignors/payouts/{payout.id}")
    assert "As entered" in response.text
    assert "Corrections after this payout was recorded" not in response.text


# --- ★ PORTAL ACCESS CONTROL --------------------------------------------

def test_a_consignor_can_open_their_own_payout(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, username="jane@example.com")
    payout = make_payout(db, consignor.id)
    make_paid_card(db, consignor.id, payout.id)

    client = TestClient(main.app)
    login(client, "jane@example.com")
    assert client.get(f"/portal/payouts/{payout.id}").status_code == 200


def test_another_consignors_payout_and_a_nonexistent_id_are_INDISTINGUISHABLE(
    tmp_path, monkeypatch,
):
    """★ Same status, same body. This page must not reveal which payout ids
    exist or how many payouts anyone else has."""
    db = setup_db(tmp_path, monkeypatch)
    jane = make_consignor(db, name="Jane", username="jane@example.com")
    bob = make_consignor(db, name="Bob", username="bob@example.com")
    bobs_payout = make_payout(db, bob.id, amount=500.0, note="Bob's private note")
    make_paid_card(db, bob.id, bobs_payout.id, name="BobsCard", owed=500.0)

    client = TestClient(main.app)
    login(client, "jane@example.com")

    forbidden = client.get(f"/portal/payouts/{bobs_payout.id}")
    missing = client.get("/portal/payouts/99999")

    assert forbidden.status_code == missing.status_code == 404
    assert forbidden.text == missing.text
    assert "Bob" not in forbidden.text
    assert "BobsCard" not in forbidden.text
    assert "500.00" not in forbidden.text
    assert "private note" not in forbidden.text


def test_the_portal_detail_requires_a_consignor_session(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, username="jane@example.com")
    payout = make_payout(db, consignor.id)

    response = TestClient(main.app).get(
        f"/portal/payouts/{payout.id}", follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/portal/login"


def test_an_operator_session_does_NOT_pass_as_a_consignor(tmp_path, monkeypatch):
    """Operator auth and consignor auth stay isolated. The operator gate is
    open in tests (conftest sets DEV_AUTH_DISABLED), which is exactly the
    condition under which a leak would show: the portal route must still
    demand a CONSIGNOR session."""
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, username="jane@example.com")
    payout = make_payout(db, consignor.id)

    client = TestClient(main.app)
    # Prove this client is accepted as an operator...
    assert client.get(f"/consignors/payouts/{payout.id}").status_code == 200
    # ...and still gets no consignor access.
    response = client.get(f"/portal/payouts/{payout.id}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/portal/login"


def test_a_consignor_session_does_NOT_grant_the_operator_route(tmp_path, monkeypatch):
    """The reverse direction. The operator route is reachable here only
    because the test gate is open; what matters is that signing in as a
    consignor is not what opened it -- the consignor cookie carries no
    operator authority, and the operator route is not under /portal/."""
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, username="jane@example.com")
    payout = make_payout(db, consignor.id)

    client = TestClient(main.app)
    login(client, "jane@example.com")
    monkeypatch.setattr(main, "DEV_AUTH_DISABLED", False)
    monkeypatch.setattr(main, "SERVICE_PASSWORD", "")

    response = client.get(f"/consignors/payouts/{payout.id}")
    assert response.status_code == 401, \
        "a consignor session must not open an operator page"


def test_the_operator_detail_route_is_not_under_portal(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    paths = {
        route.path for route in main.app.routes
        if getattr(route, "path", "").endswith("/payouts/{payout_id}")
    }
    assert "/consignors/payouts/{payout_id}" in paths
    assert "/portal/payouts/{payout_id}" in paths


# --- nothing else moved --------------------------------------------------

def test_a_nonexistent_payout_on_the_operator_route_is_404(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    assert TestClient(main.app).get("/consignors/payouts/4242").status_code == 404


def test_the_existing_correction_form_still_renders(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db)
    payout = make_payout(db, consignor.id)

    response = TestClient(main.app).get(f"/consignors/payouts/{payout.id}/edit")
    assert response.status_code == 200
    assert f"/consignors/payouts/{payout.id}/correction/preview" in response.text


def test_the_detail_pages_are_read_only(tmp_path, monkeypatch):
    """No new write paths: no forms and no buttons on either page."""
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, username="jane@example.com")
    payout = make_payout(db, consignor.id)
    make_paid_card(db, consignor.id, payout.id)

    portal_client = TestClient(main.app)
    login(portal_client, "jane@example.com")
    portal = portal_client.get(f"/portal/payouts/{payout.id}")
    operator = TestClient(main.app).get(f"/consignors/payouts/{payout.id}")

    # Scoped to the shared content: the surrounding page chrome (portal nav,
    # operator nav) legitimately contains its own controls, and this is a
    # statement about the detail content, not about the layout.
    for body in (portal.text, operator.text):
        content = shared_region(body)
        assert "<form" not in content
        assert "<button" not in content
        assert "<input" not in content
    # And the portal page adds no form of its own outside that region either.
    assert "<form" not in portal.text.split("<h1>Payout Detail</h1>")[1]
