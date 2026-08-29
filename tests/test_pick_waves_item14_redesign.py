"""UX/design-system epic, item 14: Pick Waves List redesign.

Verified current state first, per the item's own instruction: status
badges (item 6) and .data-table-scroll containment (item 4, re-confirmed
by the site-wide sweep in v1.84.1) were already live on this page --
confirmed via direct investigation before writing any code, not
assumed. This file covers what was newly built: a status filter (tabs
reusing item 12's exact nav-landmark + aria-current pattern), summary
metadata (Orders/Progress/Exceptions) computed via aggregate queries
instead of a per-wave loop, visual de-emphasis for terminal-status
rows, and a filter-aware empty state.

Pick Wave status has no Mana Pool-side equivalent (confirmed: no
remote_status-like field on the PickWave model) -- there is no
outlined/filled badge pair to build here, only the filled/local style
item 6 already provides.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import (
    Base, Batch, FulfillmentException, InventoryCard, OrderItem,
    PickAllocation, PickWave, PickWaveOrder, SalesOrder,
)


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'pick-waves-item14.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_wave_with_card(
    session, *, label="Wave 1", wave_status="active", allocation_status="allocated",
    membership_status="active", with_exception=False,
):
    batch = session.query(Batch).filter_by(batch_code="B1").one_or_none()
    if not batch:
        batch = Batch(batch_code="B1")
        session.add(batch)
        session.flush()
    wave = PickWave(label=label, status=wave_status)
    session.add(wave)
    session.flush()
    order = SalesOrder(external_order_id=f"o-{wave.id}", source="manapool", status="in_pick_wave")
    session.add(order)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status=membership_status))
    card = InventoryCard(
        batch_id=batch.id, name="Lightning Bolt", set_code="LEA", collector_number="1",
        finish_id="NF", condition_id="LP", status="reserved",
    )
    session.add(card)
    session.flush()
    item = OrderItem(order_id=order.id, name=card.name, quantity=1)
    session.add(item)
    session.flush()
    allocation = PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id,
        status=allocation_status,
    )
    session.add(allocation)
    session.flush()
    if with_exception:
        session.add(FulfillmentException(
            sales_order_id=order.id, order_item_id=item.id, pick_allocation_id=allocation.id,
            inventory_card_id=card.id, exception_type="missing",
            submission_state="needs_submission", remote_resolution_state="awaiting",
            inventory_resolution_state="unresolved", note="Missing at pick.",
        ))
    session.commit()
    return wave, order, allocation


# --- already covered by items 4/6, confirmed still true ------------------

def test_status_badge_already_present_unchanged(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_wave_with_card(session, wave_status="active")
    response = TestClient(main.app).get("/pick-waves")
    assert response.status_code == 200
    assert 'class="badge badge-info"' in response.text
    assert "Active</span>" in response.text


def test_scroll_containment_already_present_unchanged(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_wave_with_card(session)
    response = TestClient(main.app).get("/pick-waves")
    assert '<div class="data-table-scroll">' in response.text
    assert '<table class="data-table density-comfortable">' in response.text


# --- status filter tabs, mirroring item 12's exact pattern ---------------

def test_status_tabs_are_a_labeled_nav_landmark(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/pick-waves")
    assert response.status_code == 200
    assert '<nav class="status-tabs no-print" aria-label="Filter pick waves by status">' in response.text


def test_default_filter_is_active(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_wave_with_card(session, label="Active Wave", wave_status="active")
        make_wave_with_card(session, label="Done Wave", wave_status="completed")
    response = TestClient(main.app).get("/pick-waves")
    assert "Active Wave" in response.text
    assert "Done Wave" not in response.text
    assert 'aria-current="page" href="/pick-waves?status=active"' in response.text


def test_all_filter_shows_every_status(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_wave_with_card(session, label="Active Wave", wave_status="active")
        make_wave_with_card(session, label="Done Wave", wave_status="completed")
        make_wave_with_card(session, label="Dead Wave", wave_status="cancelled")
    response = TestClient(main.app).get("/pick-waves?status=all")
    assert response.status_code == 200
    assert "Active Wave" in response.text
    assert "Done Wave" in response.text
    assert "Dead Wave" in response.text
    assert "All (3)" in response.text
    assert 'aria-current="page" href="/pick-waves?status=all"' in response.text


def test_tab_counts_reflect_every_wave_regardless_of_current_filter(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_wave_with_card(session, wave_status="active")
        make_wave_with_card(session, wave_status="completed")
        make_wave_with_card(session, wave_status="completed")
    response = TestClient(main.app).get("/pick-waves")
    assert "Active (1)" in response.text
    assert "Completed (2)" in response.text


def test_only_one_tab_carries_aria_current(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_wave_with_card(session, wave_status="completed")
    response = TestClient(main.app).get("/pick-waves?status=completed")
    assert response.text.count('aria-current="page"') == 1


# --- summary metadata: Orders/Progress/Exceptions -------------------------

def test_orders_progress_and_exceptions_columns_present(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_wave_with_card(session, allocation_status="picked")
    response = TestClient(main.app).get("/pick-waves")
    assert response.status_code == 200
    assert "<th>Orders</th>" in response.text
    assert "<th>Progress</th>" in response.text
    assert "<th>Exceptions</th>" in response.text
    assert "1 of 1 picked" in response.text


def test_progress_reflects_unpicked_allocations(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_wave_with_card(session, allocation_status="allocated")
    response = TestClient(main.app).get("/pick-waves")
    assert "0 of 1 picked" in response.text


def test_exception_count_shows_as_a_warning_badge_when_nonzero(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_wave_with_card(session, allocation_status="allocated", with_exception=True)
    response = TestClient(main.app).get("/pick-waves")
    assert response.status_code == 200
    assert '<span class="badge badge-warning">1</span>' in response.text


def test_exception_count_shows_a_dash_when_zero(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_wave_with_card(session, allocation_status="allocated", with_exception=False)
    response = TestClient(main.app).get("/pick-waves")
    assert '<span class="badge badge-warning">' not in response.text


def test_removed_membership_still_counted_in_orders_column_matching_prior_behavior(tmp_path, monkeypatch):
    """The pre-existing order_count query had no status filter at all --
    the aggregate replacement must reproduce that exactly, not silently
    "fix" it into a behavior change."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order, _ = make_wave_with_card(session, membership_status="removed")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves?status=active")
    # The wave itself is still active (status filter matches on PickWave.status,
    # not membership), and its Orders count must still include the removed membership.
    assert response.status_code == 200
    row_start = response.text.index(f'href="/pick-waves/{wave_id}"')
    row_region = response.text[row_start:row_start + 400]
    assert "<td>1</td>" in row_region


def test_progress_and_exceptions_exclude_removed_memberships(tmp_path, monkeypatch):
    """Unlike the preserved Orders count, Progress/Exceptions are new
    metrics with no prior behavior to match -- they use the same
    "still meaningfully belongs to the wave" definition get_wave_orders
    already uses (active or closed, not removed)."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order, _ = make_wave_with_card(
            session, allocation_status="allocated", membership_status="removed",
        )
        wave_id = wave.id
    response = TestClient(main.app).get("/pick-waves?status=active")
    row_start = response.text.index(f'href="/pick-waves/{wave_id}"')
    row_region = response.text[row_start:row_start + 500]
    assert "&mdash;" in row_region  # Progress column shows the empty dash, not "0 of 1"


# --- visual de-emphasis for terminal-status rows --------------------------

def test_completed_wave_row_gets_the_terminal_class(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_wave_with_card(session, wave_status="completed")
    response = TestClient(main.app).get("/pick-waves?status=completed")
    assert '<tr class="pick-wave-row-terminal">' in response.text


def test_active_wave_row_has_no_terminal_class(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_wave_with_card(session, wave_status="active")
    response = TestClient(main.app).get("/pick-waves")
    assert '<tr class="pick-wave-row-terminal">' not in response.text
    assert "<tr>" in response.text


def test_terminal_row_css_dims_text_not_active(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/pick-waves").text
    rule_start = html.index("tr.pick-wave-row-terminal td {")
    rule = html[rule_start:html.index("}", rule_start) + 1]
    assert "color: var(--cf-text-muted)" in rule


# --- filter-aware empty state ---------------------------------------------

def test_genuinely_empty_database_shows_generic_message(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/pick-waves")
    assert response.status_code == 200
    assert "No pick waves yet." in response.text
    assert 'class="data-table-empty"' in response.text


def test_filtered_to_zero_shows_status_specific_message(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_wave_with_card(session, wave_status="completed")
    response = TestClient(main.app).get("/pick-waves?status=cancelled")
    assert response.status_code == 200
    assert "No cancelled pick waves." in response.text
    assert "No pick waves yet." not in response.text


def test_empty_state_colspan_matches_six_columns(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/pick-waves")
    assert '<td colspan="6" class="data-table-empty">' in response.text


# --- no functional regression: creation/filtering/navigation -------------

def test_wave_detail_link_unchanged(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, *_ = make_wave_with_card(session, label="Batch A1 Wave")
        wave_id = wave.id
    response = TestClient(main.app).get("/pick-waves")
    assert f'<a href="/pick-waves/{wave_id}">' in response.text
    assert "Batch A1 Wave" in response.text


def test_page_header_and_breadcrumbs_unchanged(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/pick-waves")
    assert '<header class="page-header">' in response.text
    assert "Pick waves combine fully allocated orders" in response.text
