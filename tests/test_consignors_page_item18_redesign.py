"""UX/design-system epic, item 18: Consignors List redesign.

Verified current state first, per the item's own instruction: table
overflow (item 4 / v1.84.1 sweep) and the active/inactive status badge
(item 6) were already correctly in place -- confirmed, not touched.
Genuinely new: a page header with New Consignor as the primary action,
What's Owed Report visually distinct as a secondary/report action,
normalized payout-method display, and an explicit "not set" treatment
for missing payout methods.

Real production payout-method distribution measured live (Railway SSH,
read-only) before writing the normalization mapping: 12 consignors, 6
with no payout method at all, and free text with no colon/handle
structure despite the entry form's own placeholder -- Paypal x2, Venmo
x1, Vemo x1 (a real typo), Cashapp x1, CashApp x1. Normalization covers
only exact case-insensitive matches of the same three common apps;
"Vemo" is deliberately left unnormalized (flagged as a data-quality
issue, not silently corrected -- that would be guessing at a typo, not
normalizing a known spelling variant) and anything with extra text
(e.g. "Cash App: @jane") passes through completely untouched.

Search/filtering and an inline owed-balance summary were both
considered and NOT added: 12 consignors in production today doesn't
need search machinery, and owed-balance is real third-party financial
information that this list page might sit open on-screen incidentally
-- the same Section 19 reasoning item 13 already applied to shipping
addresses. It stays one intentional click away at the existing
/consignors/owed report, which also isn't a cheap aggregate query this
list would otherwise be duplicating.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from main import _payout_method_display
from models import Base, Consignor


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'consignors-item18.db'}")
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


# --- payout-method normalization, unit-level ------------------------------

def test_known_variants_normalize_case_insensitively():
    assert _payout_method_display("Paypal") == "PayPal"
    assert _payout_method_display("paypal") == "PayPal"
    assert _payout_method_display("PAYPAL") == "PayPal"
    assert _payout_method_display("Venmo") == "Venmo"
    assert _payout_method_display("Cashapp") == "Cash App"
    assert _payout_method_display("CashApp") == "Cash App"
    assert _payout_method_display("cash app") == "Cash App"


def test_typo_is_not_silently_corrected():
    # Real production data has "Vemo" (a typo for Venmo) -- normalizing
    # it would be guessing at operator intent, not normalizing a known
    # spelling variant. Must render verbatim.
    assert _payout_method_display("Vemo") == "Vemo"


def test_value_with_extra_text_passes_through_untouched():
    # Must not strip the handle -- exact match only.
    assert _payout_method_display("Cash App: @jane") == "Cash App: @jane"
    assert _payout_method_display("Venmo @bob") == "Venmo @bob"


def test_empty_and_none_show_not_set():
    assert _payout_method_display(None) == '<span class="muted">not set</span>'
    assert _payout_method_display("") == '<span class="muted">not set</span>'
    assert _payout_method_display("   ") == '<span class="muted">not set</span>'


def test_unrecognized_free_text_is_escaped_and_passed_through():
    assert _payout_method_display("Zelle: <test>") == "Zelle: &lt;test&gt;"


# --- rendered page ----------------------------------------------------------

def test_payout_method_normalized_on_the_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_consignor(db, name="Alex", payout_method="Cashapp")
    make_consignor(db, name="Cam", payout_method="Paypal")
    response = TestClient(main.app).get("/consignors")
    assert "Cash App" in response.text
    assert "PayPal" in response.text
    assert "Cashapp" not in response.text
    assert "Paypal" not in response.text


def test_missing_payout_method_shows_not_set_not_blank(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_consignor(db, name="Priya", payout_method=None)
    response = TestClient(main.app).get("/consignors")
    assert '<span class="muted">not set</span>' in response.text


def test_free_text_with_handle_still_renders_verbatim(tmp_path, monkeypatch):
    # Regression guard: this exact case is already covered by the
    # existing test_consignors_page_lists_name_payout_method_and_status
    # in test_consignment_routes.py -- repeated here as an explicit
    # item-18 normalization boundary case.
    db = setup_db(tmp_path, monkeypatch)
    make_consignor(db, name="Jane Doe", payout_method="Cash App: @jane")
    response = TestClient(main.app).get("/consignors")
    assert "Cash App: @jane" in response.text


# --- page header: New Consignor primary, What's Owed secondary ------------

def test_new_consignor_is_the_primary_action(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/consignors")
    assert '<a href="/consignors/new" class="btn-primary">New Consignor</a>' in response.text


def test_whats_owed_report_is_visually_distinct_secondary_action(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/consignors")
    assert 'href="/consignors/owed" class="btn-secondary"' in response.text
    # Not styled as the primary CRUD action.
    assert 'href="/consignors/owed" class="btn-primary"' not in response.text


def test_page_header_has_breadcrumbs_and_one_h1(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/consignors")
    assert '<nav class="breadcrumbs" aria-label="Breadcrumb">' in response.text
    assert '<span class="breadcrumb-current">Consignors</span>' in response.text
    assert response.text.count("<h1") == 1


# --- status badge: already covered by item 6, confirmed unchanged ---------

def test_active_badge_reuses_shared_system(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_consignor(db, name="Jane", is_active=True)
    response = TestClient(main.app).get("/consignors")
    assert 'class="badge badge-success"' in response.text
    assert "Active</span>" in response.text


def test_inactive_badge_reuses_shared_system(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_consignor(db, name="Bob", is_active=False)
    response = TestClient(main.app).get("/consignors")
    assert 'class="badge badge-neutral"' in response.text
    assert "Inactive</span>" in response.text


# --- no functional regression ----------------------------------------------

def test_columns_and_links_unchanged(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/consignors")
    for col in ("Name", "Payout Method", "Status"):
        assert f"<th>{col}</th>" in response.text
    assert 'href="/consignors/new"' in response.text
    assert 'href="/consignors/owed"' in response.text


def test_empty_state_unchanged(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/consignors")
    assert "No consignors yet." in response.text
    assert 'class="data-table-empty"' in response.text


def test_name_still_links_to_edit(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    response = TestClient(main.app).get("/consignors")
    assert f'<a href="/consignors/{consignor.id}/edit">Jane Doe</a>' in response.text
