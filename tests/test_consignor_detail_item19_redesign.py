"""UX/design-system epic, item 19: Consignor Detail/Portal/Payouts
redesign & credential safety.

Verified current state first, per the item's own instruction: confirmed
passwords are never displayed or retained anywhere -- the edit page only
ever renders a blank <input type="password"> for setting a NEW one,
never echoes an existing password or its hash. Confirmed the shared
safety/confirmation pattern (_confirm_message + native confirm()) was
already wired to the portal-credentials form before this item -- the
native browser confirm() dialog also already satisfies Section 14's
focus-trap requirement for free (it's not a custom modal), so no new
work was needed there specifically.

This is the one item in the epic with two real, explicitly authorized
functional changes (Section 22.5, resolved 2026-08-29): a credential
change now immediately invalidates any open ConsignorSession (verified
end-to-end in test_consignor_auth_service.py, not re-tested here), and
writes a narrowly-scoped ConsignorCredentialChangeLog entry that never
stores a password or its hash. Everything else here is presentation:
sections (Profile/Portal Access/Inventory/Balance/Payouts/Portal
Preview), an operator-facing Inventory section with real status badges
(item 6) kept deliberately separate from the untouched Portal Preview
mirror (_portal_card_rows/_portal_payout_rows, shared with the real
/portal/* routes -- out of scope, not part of this page's redesign),
payout actions styled with item 18's report/financial button treatment,
and payout-method display reusing item 18's normalization.
"""
import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from consignor_auth_service import create_consignor_session, set_consignor_portal_credentials
from models import (
    Base, Batch, Consignor, ConsignorCredentialChangeLog, ConsignorPayout,
    ConsignorSession, InventoryCard,
)


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'consignor-detail-item19.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(database, "engine", db)
    return db


def make_consignor(db, name="Jane", **overrides):
    with Session(db) as session:
        consignor = Consignor(name=name, **overrides)
        session.add(consignor)
        session.commit()
        session.refresh(consignor)
        return consignor


def make_card(db, batch_id, **overrides):
    with Session(db) as session:
        values = {"batch_id": batch_id, "name": "Alpha", "status": "available"}
        values.update(overrides)
        card = InventoryCard(**values)
        session.add(card)
        session.commit()
        session.refresh(card)
        return card


def make_batch(db, code, *, consignor_id):
    with Session(db) as session:
        batch = Batch(batch_code=code, is_consignment=True, consignor_id=consignor_id)
        session.add(batch)
        session.commit()
        session.refresh(batch)
        return batch


# --- passwords never displayed or retained --------------------------------

def test_password_input_always_blank_never_echoes_existing(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db)
    with Session(db) as session:
        set_consignor_portal_credentials(session, consignor.id, "jane@example.com", "supersecret123")
        session.commit()
    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    assert "supersecret123" not in response.text
    assert '<input type="password" name="portal_password" required autocomplete="new-password">' in response.text


def test_password_hash_and_salt_never_appear_on_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db)
    with Session(db) as session:
        set_consignor_portal_credentials(session, consignor.id, "jane@example.com", "supersecret123")
        session.commit()
        stored_hash = session.get(Consignor, consignor.id).portal_password_hash
    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    assert stored_hash not in response.text


# --- credential change: sensitive-operation framing ------------------------

def test_credential_form_has_native_confirm_dialog(tmp_path, monkeypatch):
    consignor = make_consignor(setup_db(tmp_path, monkeypatch))
    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    form_start = response.text.index(f'action="/consignors/{consignor.id}/portal-credentials"')
    form_end = response.text.index("</form>", form_start)
    form = response.text[form_start:form_end]
    assert 'onsubmit="return confirm(' in form


def test_confirm_dialog_mentions_immediate_session_invalidation(tmp_path, monkeypatch):
    consignor = make_consignor(setup_db(tmp_path, monkeypatch))
    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    assert "signed out" in response.text
    assert "not just after it eventually expires" in response.text


def test_portal_access_section_has_sensitive_warning_banner(tmp_path, monkeypatch):
    consignor = make_consignor(setup_db(tmp_path, monkeypatch))
    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    assert 'class="outcome-banner outcome-banner-warning"' in response.text
    assert "treat this like resetting anyone else&#x27;s password" in response.text.lower() or \
        "treat this like resetting anyone else's password" in response.text.lower()


def test_set_portal_login_button_is_primary_not_alarming(tmp_path, monkeypatch):
    # Deliberate call: this is the intended, correct outcome of the
    # form (like Pricing's Apply), not a destructive action -- the
    # sensitivity is conveyed by the warning banner and confirm()
    # dialog, not by styling the routine credential-set button red.
    consignor = make_consignor(setup_db(tmp_path, monkeypatch))
    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    idx = response.text.index("Set Portal Login")
    snippet = response.text[max(0, idx - 200):idx]
    assert 'class="btn-primary"' in snippet


# --- sections: Profile / Portal Access / Inventory / Payouts / Portal Preview

def test_page_has_all_six_named_sections(tmp_path, monkeypatch):
    consignor = make_consignor(setup_db(tmp_path, monkeypatch))
    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    for heading in ("Profile", "Portal Access", "Inventory", "Payouts", "Portal Preview"):
        assert f"<h2>{heading}</h2>" in response.text
    # Balance is the top summary card, not a separate <h2> -- confirmed
    # deliberately, see test_balance_summary_at_top_of_page below.


def test_balance_summary_at_top_of_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, payout_method="Venmo")
    batch = make_batch(db, "CON_jane", consignor_id=consignor.id)
    make_card(db, batch.id, name="Owed Card", status="sold", consignment_value=5,
              sold_price=10, consignment_amount_owed=5, consignment_payout_status="owed")
    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    summary_idx = response.text.index('class="order-summary-card"')
    profile_idx = response.text.index("<h2>Profile</h2>")
    assert summary_idx < profile_idx
    summary = response.text[summary_idx:profile_idx]
    assert "Currently owed" in summary
    assert "$5.00" in summary
    assert "Cards on consignment" in summary


def test_balance_summary_reuses_payout_method_normalization(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, payout_method="Cashapp")
    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    summary_idx = response.text.index('class="order-summary-card"')
    profile_idx = response.text.index("<h2>Profile</h2>")
    assert "Cash App" in response.text[summary_idx:profile_idx]


# --- Inventory section: real status badges, separate from portal mirror ---

def test_inventory_section_shows_status_badges(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db)
    batch = make_batch(db, "CON_jane", consignor_id=consignor.id)
    make_card(db, batch.id, name="Owed Card", status="sold", consignment_value=5,
              sold_price=10, consignment_amount_owed=5, consignment_payout_status="owed")
    make_card(db, batch.id, name="Paid Card", status="sold", consignment_value=5,
              sold_price=10, consignment_amount_owed=5, consignment_payout_status="paid")
    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    inventory_idx = response.text.index("<h2>Inventory</h2>")
    payouts_idx = response.text.index("<h2>Payouts</h2>")
    section = response.text[inventory_idx:payouts_idx]
    assert 'class="badge badge-warning"' in section  # consignment_owed
    assert 'class="badge badge-success"' in section  # consignment_paid
    assert "Owed</span>" in section
    assert "Paid</span>" in section


def test_inventory_section_distinct_from_portal_preview_mirror(tmp_path, monkeypatch):
    # The portal mirror (_portal_card_rows) renders raw lowercase status
    # text ("sold"), never badges -- it must stay an exact, unmodified
    # reflection of what /portal/ actually shows. The Inventory section
    # is the new, richer operator-only view. Both exist, separately.
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db)
    batch = make_batch(db, "CON_jane", consignor_id=consignor.id)
    make_card(db, batch.id, name="Sold Card", status="sold", consignment_value=5,
              sold_price=10, consignment_amount_owed=5, consignment_payout_status="owed")
    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    portal_preview_idx = response.text.index("<h2>Portal Preview</h2>")
    mirror_section = response.text[portal_preview_idx:]
    assert "<td>sold</td>" in mirror_section


def test_inventory_section_empty_state(tmp_path, monkeypatch):
    consignor = make_consignor(setup_db(tmp_path, monkeypatch))
    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    inventory_idx = response.text.index("<h2>Inventory</h2>")
    payouts_idx = response.text.index("<h2>Payouts</h2>")
    assert 'class="data-table-empty"' in response.text[inventory_idx:payouts_idx]


# --- payout actions styled with item 18's report/financial treatment -----

def test_payout_actions_use_report_financial_button_style(tmp_path, monkeypatch):
    consignor = make_consignor(setup_db(tmp_path, monkeypatch))
    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    assert f'<a href="/consignors/{consignor.id}/pay" class="btn-secondary">Record payout</a>' in response.text
    assert f'<a href="/consignors/{consignor.id}/payouts" class="btn-secondary">Payout history</a>' in response.text


# --- Portal Preview: unmistakably read-only ---------------------------------

def test_portal_preview_has_read_only_banner(tmp_path, monkeypatch):
    consignor = make_consignor(setup_db(tmp_path, monkeypatch))
    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    portal_preview_idx = response.text.index("<h2>Portal Preview</h2>")
    snippet = response.text[portal_preview_idx:portal_preview_idx + 500]
    assert 'class="outcome-banner outcome-banner-info"' in snippet
    assert "Read-only" in snippet


def test_portal_preview_mirror_content_unchanged(tmp_path, monkeypatch):
    # Regression guard: the exact pre-existing mirror heading/content
    # this item's own tests (test_consignment_routes.py) already lock
    # in must still be present, byte for byte.
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    assert "What Jane Doe Sees In Their Portal" in response.text
    assert "Currently owed: <strong>$0.00</strong>" in response.text


# --- no functional regression ----------------------------------------------

def test_page_header_breadcrumbs_and_one_h1(tmp_path, monkeypatch):
    consignor = make_consignor(setup_db(tmp_path, monkeypatch), name="Jane Doe")
    response = TestClient(main.app).get(f"/consignors/{consignor.id}/edit")
    assert '<nav class="breadcrumbs" aria-label="Breadcrumb">' in response.text
    assert '<span class="breadcrumb-current">Jane Doe</span>' in response.text
    assert response.text.count("<h1") == 1


def test_save_changes_still_updates_profile(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Old Name")
    client = TestClient(main.app)
    response = client.post(
        f"/consignors/{consignor.id}/edit",
        data={"name": "New Name", "contact_info": "", "payout_method": "", "is_active": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(db) as session:
        updated = session.get(Consignor, consignor.id)
        assert updated.name == "New Name"


def test_portal_credentials_route_still_works_end_to_end(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db)
    client = TestClient(main.app)
    response = client.post(
        f"/consignors/{consignor.id}/portal-credentials",
        data={"portal_username": "jane@example.com", "portal_password": "pw123456"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert 'class="outcome-banner outcome-banner-success"' in response.text
    with Session(db) as session:
        updated = session.get(Consignor, consignor.id)
        assert updated.portal_username == "jane@example.com"
        assert updated.portal_password_hash


def test_consignor_not_found_still_404s(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/consignors/999/edit")
    assert response.status_code == 404
