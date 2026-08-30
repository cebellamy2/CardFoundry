from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, SalesOrder


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'orders-page-ui.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_order(session, external_order_id, status):
    order = SalesOrder(external_order_id=external_order_id, status=status)
    session.add(order)
    session.flush()
    return order


# --- default visibility ---

def test_bare_load_defaults_to_ready_to_pick_only(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "ready-1", "ready_to_pick")
        make_order(session, "needs-review-1", "needs_review")
        make_order(session, "cancelled-1", "cancelled")
        make_order(session, "shipped-1", "shipped")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert "ready-1" in response.text
    assert "needs-review-1" not in response.text
    assert "cancelled-1" not in response.text
    assert "shipped-1" not in response.text


def test_explicit_status_all_shows_every_order(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "ready-1", "ready_to_pick")
        make_order(session, "needs-review-1", "needs_review")
        make_order(session, "cancelled-1", "cancelled")
        make_order(session, "shipped-1", "shipped")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=all")
    assert response.status_code == 200
    assert "ready-1" in response.text
    assert "needs-review-1" in response.text
    assert "cancelled-1" in response.text
    assert "shipped-1" in response.text


def test_explicit_cancelled_filter_still_shows_cancelled_orders(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "ready-1", "ready_to_pick")
        make_order(session, "cancelled-1", "cancelled")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=cancelled")
    assert response.status_code == 200
    assert "cancelled-1" in response.text
    assert "ready-1" not in response.text


def test_explicit_shipped_filter_still_shows_shipped_orders(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "shipped-1", "shipped")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=shipped")
    assert response.status_code == 200
    assert "shipped-1" in response.text


def test_all_tab_count_includes_every_status(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "needs-review-1", "needs_review")
        make_order(session, "short-1", "short")
        make_order(session, "cancelled-1", "cancelled")
        make_order(session, "shipped-1", "shipped")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert "All (4)" in response.text


def test_all_tab_href_uses_explicit_status_all(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert 'href="/orders?status=all"' in response.text


def test_cancelled_and_shipped_tabs_still_appear_for_pulling_back_up(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "cancelled-1", "cancelled")
        make_order(session, "shipped-1", "shipped")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert 'href="/orders?status=cancelled"' in response.text
    assert 'href="/orders?status=shipped"' in response.text


def test_ready_to_pick_tab_is_active_on_bare_load(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "ready-1", "ready_to_pick")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    active_tab_start = response.text.index('href="/orders?status=ready_to_pick"')
    preceding_text = response.text[max(0, active_tab_start - 80):active_tab_start]
    assert "active" in preceding_text
    assert '<a class="status-tab active" href="/orders?status=all">' not in response.text


# --- status label formatting ---

def test_tab_labels_are_human_readable(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "ready-1", "ready_to_pick")
        make_order(session, "shipped-1", "shipped")
        make_order(session, "cancelled-1", "cancelled")
        make_order(session, "review-1", "needs_review")
        make_order(session, "wave-1", "in_pick_wave")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=all")
    assert response.status_code == 200
    assert "Ready to Pick" in response.text
    assert "Shipped" in response.text
    assert "Cancelled" in response.text
    assert "Needs Review" in response.text
    assert "In Pick Wave" in response.text
    assert "ready_to_pick (" not in response.text
    assert "shipped (" not in response.text
    assert "cancelled (" not in response.text


# --- pill tab styling ---

def test_status_tabs_use_pill_tab_classes(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "ready-1", "ready_to_pick")
        make_order(session, "needs-review-1", "needs_review")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert 'class="status-tabs no-print"' in response.text
    assert 'class="status-tab active"' in response.text
    assert 'class="status-tab"' in response.text


def test_active_tab_reflects_current_status_filter(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "needs-review-1", "needs_review")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=needs_review")
    assert response.status_code == 200
    assert 'href="/orders?status=needs_review"' in response.text
    active_tab_start = response.text.index('href="/orders?status=needs_review"')
    preceding_text = response.text[max(0, active_tab_start - 80):active_tab_start]
    assert "active" in preceding_text
    # The "All" tab must not also be marked active when a specific status filter is applied.
    assert '<a class="status-tab active" href="/orders?status=all">' not in response.text


# --- removed headings ---

def test_fulfillment_queue_heading_is_removed(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert "Fulfillment Queue" not in response.text


def test_existing_orders_heading_is_removed(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert "Existing Orders" not in response.text


def test_orders_h1_is_still_present(tmp_path, monkeypatch):
    """Orders now renders its title via the shared _page_header()
    component (v1.76.0-continued), so the h1 carries a class attribute
    rather than being bare -- still a real <h1>, just no longer an exact
    "<h1>" substring match."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert "<h1" in response.text and "Orders" in response.text


def test_mana_pool_heading_is_removed(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert "<h2>" not in response.text or "Mana Pool" not in response.text


# --- round 2: view pick waves link removed ---

def test_view_pick_waves_link_is_removed(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert "View Pick Waves" not in response.text


# --- round 2: select-all controls are buttons, not links ---

def test_select_all_ready_is_a_button_not_a_link(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "ready-1", "ready_to_pick")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert 'name="select_all_ready" value="1"' in response.text
    assert "Select all 1 Ready to Pick order(s)" in response.text
    assert '<a href="/orders?status=ready_to_pick&select_all_ready=1">' not in response.text


def test_select_all_picked_is_a_button_not_a_link(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "picked-1", "picked")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=picked")
    assert response.status_code == 200
    assert 'name="select_all_picked" value="1"' in response.text
    assert "Select all 1 Picked order(s)" in response.text
    assert '<a href="/orders?status=picked&select_all_picked=1">' not in response.text


def test_select_all_ready_button_still_selects_ready_orders(tmp_path, monkeypatch):
    """The button converted from a link must still drive the same
    select-all behavior via its hidden form fields."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_order(session, "ready-1", "ready_to_pick")
        session.commit()
        order_id = order.id
    client = TestClient(main.app)
    response = client.get(
        "/orders", params={"status": "ready_to_pick", "select_all_ready": "1"},
    )
    assert response.status_code == 200
    import re
    checkbox_pattern = re.compile(
        rf'value="{order_id}"\s+form="create-wave-form"\s+aria-label="[^"]*"\s*(checked)?\s*>'
    )
    match = checkbox_pattern.search(response.text)
    assert match and match.group(1) == "checked"


# --- round 2: tooltips instead of standing explanatory text ---

def test_wave_explanatory_text_is_a_tooltip_not_standing_text(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert 'title="Check the orders below to include in a new pick wave' in response.text
    assert '<p class="muted">\n            Check the orders below to include' not in response.text


def test_pack_explanatory_text_is_a_tooltip_not_standing_text(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert 'title="Check the orders below to pack together' in response.text


def test_sync_explanatory_text_is_a_tooltip(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert 'title="Asks Mana Pool specifically for orders that still need shipping."' in response.text


# --- round 2: control ordering matches the real process sequence ---

def test_top_controls_are_ordered_sync_then_wave_then_pack(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    sync_index = response.text.index("Sync Mana Pool Orders")
    wave_index = response.text.index("Create Pick Wave from Selected Orders")
    pack_index = response.text.index("Mark Packed (Selected Orders)")
    assert sync_index < wave_index < pack_index
