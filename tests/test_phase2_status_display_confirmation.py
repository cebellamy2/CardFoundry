"""Phase 2, part 1 of the UX/design-system epic: status badges, the
shared display-value layer, and the shared safety/confirmation pattern.

Uses the v1.76.0 token set exclusively via STATUS_SEMANTIC_ROLES and the
existing --cf-{role}-* tokens; no new colors/spacing/typography invented.
No individual workflow page's content/layout is redesigned here beyond
wiring these three shared components into existing plain-text/raw-value/
ad hoc-confirm sites.
"""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, Consignor, InventoryCard, PickWave


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'phase2.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


# --- status badge component ------------------------------------------

def test_status_badge_renders_role_icon_and_label():
    html = main._status_badge("shipped")
    assert 'class="badge badge-success"' in html
    assert 'class="badge-icon" aria-hidden="true">✓</span>' in html
    assert "Shipped</span>" in html


def test_status_badge_falls_back_to_neutral_for_an_unmapped_key():
    html = main._status_badge("some_totally_new_status")
    assert 'class="badge badge-neutral"' in html
    assert "Some Totally New Status</span>" in html


def test_status_badge_escapes_a_malicious_looking_unmapped_key():
    html = main._status_badge("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;Script&gt;" in html


def test_status_badge_optional_title_attribute():
    html = main._status_badge("shipped", title="Shipped on Aug 1")
    assert 'title="Shipped on Aug 1"' in html


def test_status_badge_uses_the_roles_own_tooltip_when_no_explicit_title():
    html = main._status_badge("short")
    assert "title=" in html
    assert "Not enough matching inventory" in html


def test_explicit_title_overrides_the_roles_own_tooltip():
    html = main._status_badge("short", title="Custom override")
    assert 'title="Custom override"' in html
    assert "Not enough matching inventory" not in html


def test_most_statuses_have_no_tooltip_by_default():
    html = main._status_badge("shipped")
    assert "title=" not in html


def test_status_semantic_roles_covers_every_domain_named_in_the_spec():
    domains = {
        # inventory
        "listed", "not_listed", "reserved", "sold", "unsellable", "removed",
        "fulfillment_missing", "personal_use", "scan_error", "duplicate_record",
        "damaged", "fulfillment_inventory_mismatch", "exception_unresolved",
        # orders
        "new", "needs_review", "short", "ready_to_pick", "in_pick_wave",
        "picked", "packed", "shipped", "cancelled",
        # pick waves / jobs
        "active", "completed", "pending", "running", "failed",
        # consignors
        "consignor_active", "consignor_inactive",
        # fulfillment exceptions
        "missing", "inventory_mismatch", "needs_submission", "submitted",
        "awaiting", "resolved_refunded", "resolved_replaced",
        "review_required", "unresolved", "resolved",
    }
    assert domains.issubset(main.STATUS_SEMANTIC_ROLES.keys())


def test_inventory_status_badge_resolves_available_to_listed():
    card = type("Card", (), {"status": "available", "id": 1})()
    html = main._inventory_status_badge(card, {1: "listed"})
    assert 'badge-success' in html and "Listed</span>" in html


def test_inventory_status_badge_resolves_available_to_not_listed():
    card = type("Card", (), {"status": "available", "id": 1})()
    html = main._inventory_status_badge(card, {})
    assert 'badge-neutral' in html and "Not Listed</span>" in html


def test_inventory_status_badge_non_available_status():
    card = type("Card", (), {"status": "reserved", "id": 1})()
    html = main._inventory_status_badge(card, {})
    assert 'badge-info' in html and "Reserved</span>" in html


# --- integration: badges wired into real pages -------------------------

def test_orders_page_shows_status_badge(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    from models import SalesOrder
    with Session(db) as session:
        session.add(SalesOrder(external_order_id="ord-1", status="ready_to_pick"))
        session.commit()
    html = TestClient(main.app).get("/orders?status=ready_to_pick").text
    assert 'class="badge badge-info"' in html
    assert "Ready to Pick</span>" in html


def test_pick_waves_page_shows_status_badge(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(PickWave(label="Wave 1", status="active"))
        session.commit()
    html = TestClient(main.app).get("/pick-waves").text
    assert 'class="badge badge-info"' in html
    assert "Active</span>" in html


def test_consignors_page_shows_active_badge(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(Consignor(name="Jane", is_active=True))
        session.commit()
    html = TestClient(main.app).get("/consignors").text
    assert 'class="badge badge-success"' in html
    assert "Active</span>" in html


def test_consignors_page_shows_inactive_badge(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(Consignor(name="Bob", is_active=False))
        session.commit()
    html = TestClient(main.app).get("/consignors").text
    assert 'class="badge badge-neutral"' in html
    assert "Inactive</span>" in html


def test_inventory_search_shows_status_badge_not_raw_text(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = Batch(batch_code="B1")
        session.add(batch)
        session.flush()
        session.add(InventoryCard(batch_id=batch.id, name="Bolt", status="reserved"))
        session.commit()
    html = TestClient(main.app).get("/inventory?show_all=true").text
    assert 'class="badge badge-info"' in html
    assert "Reserved</span>" in html


# --- shared display-value layer -----------------------------------------

def test_finish_display_two_letter_codes():
    assert main._finish_display("NF") == "Non-Foil"
    assert main._finish_display("FO") == "Foil"
    assert main._finish_display("EF") == "Etched"


def test_finish_display_free_text_fallback():
    assert main._finish_display("foil") == "Foil"
    assert main._finish_display("etched") == "Etched"


def test_finish_display_empty():
    assert main._finish_display(None) == ""
    assert main._finish_display("") == ""


def test_condition_display_two_letter_codes():
    assert main._condition_display("NM") == "Near Mint"
    assert main._condition_display("LP") == "Lightly Played"
    assert main._condition_display("MP") == "Moderately Played"
    assert main._condition_display("HP") == "Heavily Played"
    assert main._condition_display("DMG") == "Damaged"


def test_condition_display_free_text_fallback():
    assert main._condition_display("mint") == "Mint"


def test_set_code_display_uppercases():
    assert main._set_code_display("woe") == "WOE"
    assert main._set_code_display("PTLA") == "PTLA"
    assert main._set_code_display(None) == ""


def test_format_timestamp_readable_not_iso():
    from datetime import datetime
    value = datetime(2026, 8, 29, 5, 7)
    formatted = main._format_timestamp(value)
    assert formatted == "Aug 29, 2026 5:07 AM"
    assert "T" not in formatted  # not the raw ISO form


def test_format_timestamp_empty():
    assert main._format_timestamp(None) == ""


def test_format_date_readable_not_iso():
    from datetime import datetime
    value = datetime(2026, 8, 29)
    assert main._format_date(value) == "Aug 29, 2026"


def test_format_date_empty():
    assert main._format_date(None) == ""


def test_inventory_search_shows_readable_finish_and_condition(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = Batch(batch_code="B1")
        session.add(batch)
        session.flush()
        session.add(InventoryCard(
            batch_id=batch.id, name="Bolt", status="available",
            finish_id="FO", condition_id="LP", set_code="woe",
        ))
        session.commit()
    html = TestClient(main.app).get("/inventory?show_all=true").text
    assert "Foil" in html
    assert "Lightly Played" in html
    assert ">WOE<" in html or "WOE" in html


# --- shared confirmation/outcome components -----------------------------

def test_confirm_message_basic_shape():
    msg = main._confirm_message("Do the thing", count=3, noun="card")
    assert msg.startswith("Do the thing? ")
    assert "3 cards will be affected." in msg
    assert main.CARDFOUNDRY_ONLY_NOTE in msg


def test_confirm_message_singular_noun():
    msg = main._confirm_message("Do the thing", count=1, noun="card")
    assert "1 card will be affected." in msg
    assert "1 cards" not in msg


def test_confirm_message_custom_system_note_for_manapool_actions():
    msg = main._confirm_message(
        "Sync this wave", count=2, noun="order",
        system_note="This also updates Mana Pool.",
    )
    assert "This also updates Mana Pool." in msg
    assert main.CARDFOUNDRY_ONLY_NOTE not in msg


def test_confirm_message_reversible_and_extra_are_appended():
    msg = main._confirm_message(
        "Mark unavailable", count=1, noun="card",
        reversible="Reversible: use Mark Available to undo.",
        extra="One more thing.",
    )
    assert "Reversible: use Mark Available to undo." in msg
    assert "One more thing." in msg
    # order: action, count, system, reversible, extra
    assert msg.index("Reversible") < msg.index("One more thing")


def test_outcome_banner_renders_the_right_class():
    for kind in ("success", "warning", "danger", "info"):
        html = main._outcome_banner(kind, "Some message")
        assert f'class="outcome-banner outcome-banner-{kind}"' in html
        assert "Some message" in html


def test_outcome_banner_css_classes_are_defined(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert ".outcome-banner {" in html
    for kind in ("success", "warning", "danger", "info"):
        assert f".outcome-banner-{kind} {{" in html


# --- integration: confirmations added where none existed before --------

def test_bulk_card_action_buttons_have_onclick_confirms():
    html = main._bulk_card_action_form("/inventory", "")
    assert "Move Selected" in html
    for action in (
        "/inventory-cards/bulk-move-batch",
        "/inventory-cards/bulk-mark-unavailable",
        "/inventory-cards/bulk-mark-available",
        "/inventory-cards/bulk-remove",
    ):
        segment = html[html.index(f'formaction="{action}"'):]
        segment = segment[:segment.index(">")]
        assert "onclick=\"return confirm(" in segment


def test_consignor_portal_credentials_form_has_confirm(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        consignor = Consignor(name="Jane")
        session.add(consignor)
        session.commit()
        consignor_id = consignor.id
    html = TestClient(main.app).get(f"/consignors/{consignor_id}/edit").text
    form = html[html.index('action="/consignors/{}/portal-credentials"'.format(consignor_id)):]
    assert "onsubmit=\"return confirm(" in form[:form.index("</form>")]


def test_consignor_portal_credentials_success_shows_outcome_banner(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        consignor = Consignor(name="Jane")
        session.add(consignor)
        session.commit()
        consignor_id = consignor.id
    client = TestClient(main.app)
    response = client.post(
        f"/consignors/{consignor_id}/portal-credentials",
        data={"portal_username": "jane@example.com", "portal_password": "pw123456"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert 'class="outcome-banner outcome-banner-success"' in response.text
    assert "Portal login updated." in response.text


def test_consignor_portal_credentials_failure_shows_outcome_banner(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(Consignor(name="Jane", portal_username="jane@example.com"))
        bob = Consignor(name="Bob")
        session.add(bob)
        session.commit()
        bob_id = bob.id
    response = TestClient(main.app).post(
        f"/consignors/{bob_id}/portal-credentials",
        data={"portal_username": "jane@example.com", "portal_password": "pw123456"},
    )
    assert response.status_code == 400
    assert 'class="outcome-banner outcome-banner-danger"' in response.text
