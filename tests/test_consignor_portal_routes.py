from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from consignment_service import create_consignor_payout
from consignor_auth_service import set_consignor_portal_credentials
from models import Base, Batch, Consignor, InventoryCard


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'consignor-portal.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(database, "engine", db)
    return db


def make_consignor_with_login(db, *, name="Jane", username="jane@example.com", password="secretpw", is_active=True):
    with Session(db) as session:
        consignor = Consignor(name=name, is_active=is_active)
        session.add(consignor)
        session.flush()
        set_consignor_portal_credentials(session, consignor.id, username, password)
        session.commit()
        session.refresh(consignor)
        return consignor


def make_batch(db, code, *, is_consignment=True, consignor_id=None):
    with Session(db) as session:
        batch = Batch(batch_code=code, is_consignment=is_consignment, consignor_id=consignor_id)
        session.add(batch)
        session.commit()
        session.refresh(batch)
        return batch


def make_card(db, batch_id, **overrides):
    with Session(db) as session:
        values = {"batch_id": batch_id, "name": "Alpha", "status": "available"}
        values.update(overrides)
        card = InventoryCard(**values)
        session.add(card)
        session.commit()
        session.refresh(card)
        return card


def login(client, username, password):
    return client.post(
        "/portal/login", data={"username": username, "password": password},
        follow_redirects=False,
    )


# --- portal_login_form ---

def test_login_form_has_no_operator_nav(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/portal/login")
    assert response.status_code == 200
    assert "Consignor Portal" in response.text
    assert 'href="/admin"' not in response.text
    assert 'href="/orders"' not in response.text
    assert 'href="/inventory-sync"' not in response.text


# --- portal_login_submit ---

def test_login_wrong_password_returns_401(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_consignor_with_login(db)
    client = TestClient(main.app)
    response = login(client, "jane@example.com", "wrongpw")
    assert response.status_code == 401
    assert "Incorrect email or password" in response.text


def test_login_unknown_username_returns_401(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = login(client, "nobody@example.com", "whatever")
    assert response.status_code == 401


def test_login_success_sets_cookie_and_redirects(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_consignor_with_login(db)
    client = TestClient(main.app)
    response = login(client, "jane@example.com", "secretpw")
    assert response.status_code == 303
    assert response.headers["location"] == "/portal/"
    assert "consignor_session" in response.cookies


def test_login_is_case_insensitive_on_username(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_consignor_with_login(db, username="jane@example.com")
    client = TestClient(main.app)
    response = login(client, "Jane@Example.com", "secretpw")
    assert response.status_code == 303


def test_login_rejects_inactive_consignor(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_consignor_with_login(db, is_active=False)
    client = TestClient(main.app)
    response = login(client, "jane@example.com", "secretpw")
    assert response.status_code == 401


# --- portal_dashboard / access control ---

def test_dashboard_redirects_when_not_logged_in(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/portal/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/portal/login"


def test_dashboard_shows_own_cards_and_owed_total(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    make_card(db, batch.id, name="Brainstorm", consignment_value=4.0)
    make_card(
        db, batch.id, name="Lightning Bolt", status="sold", sold_price=10.0,
        consignment_amount_owed=8.0, consignment_payout_status="owed",
    )
    client = TestClient(main.app)
    login(client, "jane@example.com", "secretpw")
    response = client.get("/portal/")
    assert response.status_code == 200
    assert "Brainstorm" in response.text
    assert "Lightning Bolt" in response.text
    assert "Currently owed: <strong>$8.00</strong>" in response.text


def test_dashboard_shows_paid_status_for_a_paid_out_sold_card(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    make_card(
        db, batch.id, name="Lightning Bolt", status="sold", sold_price=10.0,
        consignment_amount_owed=8.0, consignment_payout_status="paid",
    )
    client = TestClient(main.app)
    login(client, "jane@example.com", "secretpw")
    response = client.get("/portal/")
    assert response.status_code == 200
    assert "<td>Paid</td>" in response.text
    assert "<td>sold</td>" not in response.text


def test_dashboard_shows_sold_status_for_a_still_owed_sold_card(tmp_path, monkeypatch):
    """A sold card whose payout hasn't gone through yet must still read
    "sold," not "Paid" -- only consignment_payout_status == "paid" flips
    the display."""
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    make_card(
        db, batch.id, name="Lightning Bolt", status="sold", sold_price=10.0,
        consignment_amount_owed=8.0, consignment_payout_status="owed",
    )
    client = TestClient(main.app)
    login(client, "jane@example.com", "secretpw")
    response = client.get("/portal/")
    assert response.status_code == 200
    assert "<td>sold</td>" in response.text
    assert "<td>Paid</td>" not in response.text


def _make_one_of_each_status(db, batch_id):
    make_card(db, batch_id, name="Brainstorm")  # available
    make_card(
        db, batch_id, name="Lightning Bolt", status="sold", sold_price=10.0,
        consignment_amount_owed=8.0, consignment_payout_status="owed",
    )
    make_card(
        db, batch_id, name="Ponder", status="sold", sold_price=5.0,
        consignment_amount_owed=4.0, consignment_payout_status="paid",
    )


def test_dashboard_status_filter_available_shows_only_available(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    _make_one_of_each_status(db, batch.id)
    client = TestClient(main.app)
    login(client, "jane@example.com", "secretpw")
    response = client.get("/portal/", params={"status": "available"})
    assert response.status_code == 200
    assert "Brainstorm" in response.text
    assert "Lightning Bolt" not in response.text
    assert "Ponder" not in response.text


def test_dashboard_status_filter_sold_excludes_paid(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    _make_one_of_each_status(db, batch.id)
    client = TestClient(main.app)
    login(client, "jane@example.com", "secretpw")
    response = client.get("/portal/", params={"status": "sold"})
    assert response.status_code == 200
    assert "Lightning Bolt" in response.text
    assert "Brainstorm" not in response.text
    assert "Ponder" not in response.text


def test_dashboard_status_filter_paid_excludes_owed_sold(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    _make_one_of_each_status(db, batch.id)
    client = TestClient(main.app)
    login(client, "jane@example.com", "secretpw")
    response = client.get("/portal/", params={"status": "paid"})
    assert response.status_code == 200
    assert "Ponder" in response.text
    assert "Brainstorm" not in response.text
    assert "Lightning Bolt" not in response.text


def test_dashboard_status_filter_blank_shows_everything(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    _make_one_of_each_status(db, batch.id)
    client = TestClient(main.app)
    login(client, "jane@example.com", "secretpw")
    response = client.get("/portal/")
    assert response.status_code == 200
    assert "Brainstorm" in response.text
    assert "Lightning Bolt" in response.text
    assert "Ponder" in response.text


def test_dashboard_status_filter_rejects_unknown_value(tmp_path, monkeypatch):
    """An unrecognized status value must fail open to "no filter," not
    crash or silently match nothing -- same convention as the Inventory
    Search status filter."""
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    _make_one_of_each_status(db, batch.id)
    client = TestClient(main.app)
    login(client, "jane@example.com", "secretpw")
    response = client.get("/portal/", params={"status": "removed; DROP TABLE"})
    assert response.status_code == 200
    assert "Brainstorm" in response.text
    assert "Lightning Bolt" in response.text
    assert "Ponder" in response.text


def test_dashboard_status_filter_owed_total_unaffected_by_filter(tmp_path, monkeypatch):
    """"Currently owed" is the consignor's true running total, not a
    count of the currently-filtered rows -- it must stay correct no
    matter which status filter is applied."""
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    _make_one_of_each_status(db, batch.id)
    client = TestClient(main.app)
    login(client, "jane@example.com", "secretpw")
    response = client.get("/portal/", params={"status": "available"})
    assert response.status_code == 200
    assert "Currently owed: <strong>$8.00</strong>" in response.text


def test_dashboard_status_filter_no_match_shows_filter_specific_empty_state(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    make_card(db, batch.id, name="Brainstorm")  # available only
    client = TestClient(main.app)
    login(client, "jane@example.com", "secretpw")
    response = client.get("/portal/", params={"status": "paid"})
    assert response.status_code == 200
    assert "No cards match this filter." in response.text
    assert "No cards on consignment yet." not in response.text


def test_dashboard_never_shows_another_consignors_cards(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    jane = make_consignor_with_login(db, name="Jane", username="jane@example.com")
    bob = make_consignor_with_login(db, name="Bob", username="bob@example.com", password="bobpw")
    jane_batch = make_batch(db, "JANE-1", consignor_id=jane.id)
    bob_batch = make_batch(db, "BOB-1", consignor_id=bob.id)
    make_card(db, jane_batch.id, name="Jane Card")
    make_card(db, bob_batch.id, name="Bob Card")

    client = TestClient(main.app)
    login(client, "bob@example.com", "bobpw")
    response = client.get("/portal/")
    assert response.status_code == 200
    assert "Bob Card" in response.text
    assert "Jane Card" not in response.text


def test_dashboard_shows_empty_state_with_no_cards(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_consignor_with_login(db)
    client = TestClient(main.app)
    login(client, "jane@example.com", "secretpw")
    response = client.get("/portal/")
    assert response.status_code == 200
    assert "No cards on consignment yet." in response.text


def test_dashboard_rejects_stale_cookie_after_logout(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_consignor_with_login(db)
    client = TestClient(main.app)
    login(client, "jane@example.com", "secretpw")
    client.post("/portal/logout")
    response = client.get("/portal/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/portal/login"


# --- portal_payout_history ---

def test_payout_history_redirects_when_not_logged_in(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/portal/payouts", follow_redirects=False)
    assert response.status_code == 303


def test_payout_history_shows_empty_state(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_consignor_with_login(db)
    client = TestClient(main.app)
    login(client, "jane@example.com", "secretpw")
    response = client.get("/portal/payouts")
    assert response.status_code == 200
    assert "No payouts recorded yet." in response.text


def test_payout_history_shows_recorded_payout(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    card = make_card(
        db, batch.id, name="Lightning Bolt", status="sold", sold_price=10.0,
        consignment_amount_owed=8.0, consignment_payout_status="owed",
    )
    from datetime import datetime
    create_consignor_payout(consignor.id, [card.id], "Cash App", "", datetime(2026, 8, 19))

    client = TestClient(main.app)
    login(client, "jane@example.com", "secretpw")
    response = client.get("/portal/payouts")
    assert response.status_code == 200
    assert "$8.00" in response.text
    assert "Cash App" in response.text


def test_payout_history_never_shows_another_consignors_payout(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    jane = make_consignor_with_login(db, name="Jane", username="jane@example.com")
    bob = make_consignor_with_login(db, name="Bob", username="bob@example.com", password="bobpw")
    bob_batch = make_batch(db, "BOB-1", consignor_id=bob.id)
    bob_card = make_card(
        db, bob_batch.id, status="sold", sold_price=10.0,
        consignment_amount_owed=8.0, consignment_payout_status="owed",
    )
    from datetime import datetime
    create_consignor_payout(bob.id, [bob_card.id], "Venmo", "", datetime(2026, 8, 19))

    client = TestClient(main.app)
    login(client, "jane@example.com", "secretpw")
    response = client.get("/portal/payouts")
    assert response.status_code == 200
    assert "No payouts recorded yet." in response.text
    assert "Venmo" not in response.text


# --- operator-side set_consignor_portal_login route ---

def test_set_portal_login_happy_path(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        consignor = Consignor(name="Jane")
        session.add(consignor)
        session.commit()
        session.refresh(consignor)
    client = TestClient(main.app)
    response = client.post(
        f"/consignors/{consignor.id}/portal-credentials",
        data={"portal_username": "jane@example.com", "portal_password": "pw123"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(db) as session:
        refreshed = session.get(Consignor, consignor.id)
        assert refreshed.portal_username == "jane@example.com"
        assert refreshed.portal_password_hash


def test_set_portal_login_missing_consignor_404s(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/consignors/999/portal-credentials",
        data={"portal_username": "x@example.com", "portal_password": "pw"},
    )
    assert response.status_code == 404


def test_set_portal_login_rejects_duplicate_username(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_consignor_with_login(db, name="Jane", username="jane@example.com")
    with Session(db) as session:
        bob = Consignor(name="Bob")
        session.add(bob)
        session.commit()
        session.refresh(bob)
    client = TestClient(main.app)
    response = client.post(
        f"/consignors/{bob.id}/portal-credentials",
        data={"portal_username": "jane@example.com", "portal_password": "pw"},
    )
    assert response.status_code == 400


# --- operator gate is unaffected ---

def test_operator_routes_unaffected_by_portal_exemption(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/consignors")
    assert response.status_code == 200
