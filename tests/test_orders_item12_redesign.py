"""UX/design-system epic, item 12: Orders list redesign.

Covers what was newly built this item -- most of Section 10.D's ask was
already shipped by items 6-8 (status-badge CardFoundry Status column,
pill-style status tabs with counts, tooltips) and is left to the existing
test files. This file covers: the new Mana Pool Status badge mapping,
accessible status-tabs (nav landmark + aria-current, not a bare div),
disabled-state explanations, the pick-wave-creation confirmation that
didn't exist before, the dual-toolbar stacking fix, the empty-state split,
and the sync route's three response states (previously untested at all).
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import AppSetting, Base, SalesOrder


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'orders-item12.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_order(session, external_order_id, status, **kwargs):
    order = SalesOrder(external_order_id=external_order_id, status=status, **kwargs)
    session.add(order)
    session.flush()
    return order


# --- Mana Pool Status badge -------------------------------------------

def test_mana_pool_status_renders_as_a_remote_badge(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "o1", "shipped", remote_fulfillment_status="delivered")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=all")
    assert response.status_code == 200
    assert 'class="badge badge-success badge-remote"' in response.text
    assert "Delivered" in response.text


def test_mana_pool_status_processing_maps_to_info(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "o1", "ready_to_pick", remote_fulfillment_status="processing")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=all")
    assert 'class="badge badge-info badge-remote"' in response.text
    assert "Processing" in response.text


def test_mana_pool_status_replaced_maps_to_warning_with_tooltip(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "o1", "shipped", remote_fulfillment_status="replaced")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=all")
    assert 'class="badge badge-warning badge-remote"' in response.text
    assert "Replaced" in response.text
    assert "likely had a problem" in response.text


def test_mana_pool_status_refunded_maps_to_danger(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "o1", "cancelled", remote_fulfillment_status="refunded")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=all")
    assert 'class="badge badge-danger badge-remote"' in response.text
    assert "Refunded" in response.text


def test_mana_pool_status_blank_shows_not_synced_badge(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "o1", "ready_to_pick", remote_fulfillment_status=None)
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=all")
    assert 'class="badge badge-neutral badge-remote"' in response.text
    assert "Not Synced" in response.text


def test_cardfoundry_status_badge_is_not_the_outlined_remote_variant(tmp_path, monkeypatch):
    """The local/remote visual distinction must actually distinguish --
    CardFoundry's own status badge stays filled, not outlined."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "o1", "ready_to_pick")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=all")
    assert 'class="badge badge-info">' in response.text


# --- accessible status-tabs ---------------------------------------------

def test_status_tabs_are_a_labeled_nav_landmark(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert '<nav class="status-tabs no-print" aria-label="Filter orders by status">' in response.text


def test_active_status_tab_carries_aria_current(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "o1", "ready_to_pick")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert 'aria-current="page" href="/orders?status=ready_to_pick"' in response.text
    # Only one tab should carry it.
    assert response.text.count('aria-current="page"') == 1


def test_inactive_status_tabs_have_no_aria_current(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "o1", "ready_to_pick")
        make_order(session, "o2", "shipped")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=all")
    assert 'aria-current="page" href="/orders?status=all"' in response.text
    assert 'aria-current="page" href="/orders?status=ready_to_pick"' not in response.text
    assert 'aria-current="page" href="/orders?status=shipped"' not in response.text


# --- disabled-state explanations ----------------------------------------

def test_zero_ready_orders_explains_why_wave_creation_is_unavailable(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "o1", "shipped")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=all")
    assert "No orders are currently Ready to Pick" in response.text
    assert "nothing to add to a new wave right now" in response.text


def test_zero_picked_orders_explains_why_packing_is_unavailable(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "o1", "shipped")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=all")
    assert "No orders are currently Picked" in response.text
    assert "nothing to mark as packed right now" in response.text


def test_nonzero_ready_orders_shows_the_select_all_button_not_the_explanation(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "o1", "ready_to_pick")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=all")
    assert "Select all 1 Ready to Pick order(s)" in response.text
    assert "nothing to add to a new wave right now" not in response.text


# --- pick-wave creation confirmation (previously missing entirely) -----

def test_create_wave_form_now_has_a_confirmation(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert 'id="create-wave-form"' in response.text
    form_start = response.text.index('id="create-wave-form"')
    form_region = response.text[form_start:form_start + 700]
    assert "onsubmit=\"return confirm(" in form_region
    assert "Create a new pick wave" in form_region
    assert "This only changes CardFoundry" in form_region
    assert "cancel the wave afterward" in form_region


def test_create_wave_route_still_works_with_confirmation_added(tmp_path, monkeypatch):
    """confirm() is client-side JS -- TestClient posts directly, so this
    proves the server-side route itself is unaffected."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_order(session, "o1", "ready_to_pick")
        session.commit()
        order_id = order.id
    client = TestClient(main.app)
    response = client.post(
        "/pick-waves/create", data={"order_ids": [str(order_id)], "label": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303


# --- dual-toolbar stacking fix -------------------------------------------

def test_both_toolbars_are_wrapped_in_one_sticky_stack(tmp_path, monkeypatch):
    """Regression: two independently `position: sticky` toolbars stuck to
    the same offset when both were visible (a mixed ready_to_pick +
    picked selection on the All filter), and the later one completely
    covered the earlier one's own count and its "Optional wave name"
    field. One shared sticky wrapper, individual forms back in normal
    flow inside it, fixes this structurally."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    stack_start = response.text.index('<div class="bulk-toolbar-stack no-print">')
    stack_region = response.text[stack_start:]
    wave_idx = stack_region.index('id="create-wave-form"')
    pack_idx = stack_region.index('id="bulk-pack-form"')
    assert wave_idx < pack_idx
    close_idx = stack_region.index("</form>", pack_idx)
    assert close_idx < stack_region.index("</div>")


def test_bulk_toolbar_stack_css_is_sticky_and_toolbars_are_not(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    html = client.get("/orders").text
    stack_rule = html[html.index(".bulk-toolbar-stack {"):]
    stack_rule = stack_rule[:stack_rule.index("}") + 1]
    assert "position: sticky;" in stack_rule
    override_rule = html[html.index(".bulk-toolbar-stack .bulk-toolbar {"):]
    override_rule = override_rule[:override_rule.index("}") + 1]
    assert "position: static;" in override_rule


def test_wave_and_pack_toolbars_have_distinct_accent_borders(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    html = client.get("/orders").text
    accent_start = html.index(".bulk-toolbar-stack {")
    wave_rule_start = html.index(".bulk-toolbar-wave {", accent_start)
    wave_rule = html[wave_rule_start:]
    wave_rule = wave_rule[:wave_rule.index("}") + 1]
    pack_rule_start = html.index(".bulk-toolbar-pack {", accent_start)
    pack_rule = html[pack_rule_start:]
    pack_rule = pack_rule[:pack_rule.index("}") + 1]
    assert "border-left" in wave_rule
    assert "border-left" in pack_rule
    assert wave_rule != pack_rule


# --- empty state: genuinely empty vs. filtered to zero -------------------

def test_genuinely_empty_database_points_at_sync(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders?status=all")
    assert response.status_code == 200
    assert "No orders yet." in response.text
    assert 'href="#sync-mana-pool-orders"' in response.text


def test_filtered_to_zero_uses_the_generic_message(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "o1", "shipped")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=cancelled")
    assert response.status_code == 200
    assert "No orders match this filter." in response.text
    assert "No orders yet." not in response.text


# --- loading-state expectation copy --------------------------------------

def test_sync_button_sets_an_honest_wait_time_expectation(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert "Can take a few minutes for a large order backlog" in response.text


# --- sync route: three response states, previously entirely untested ----

def test_sync_without_go_live_shows_warning_outcome_banner(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/manapool/sync")
    assert response.status_code == 200
    assert 'class="outcome-banner outcome-banner-warning"' in response.text
    assert "Go-live timestamp not set" in response.text
    assert 'href="/cutover"' in response.text


def test_sync_failure_shows_danger_outcome_banner(tmp_path, monkeypatch):
    import httpx

    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(AppSetting(key=main.GO_LIVE_SETTING_KEY, value="2026-01-01T00:00:00Z"))
        session.commit()

    def raise_unreachable(since):
        raise httpx.ConnectError("connection failed")

    monkeypatch.setattr(main, "get_seller_orders", raise_unreachable)
    client = TestClient(main.app)
    response = client.post("/manapool/sync")
    assert response.status_code == 200
    assert 'class="outcome-banner outcome-banner-danger"' in response.text
    assert "connection failed" in response.text


def test_sync_clean_success_shows_success_outcome_banner(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(AppSetting(key=main.GO_LIVE_SETTING_KEY, value="2026-01-01T00:00:00Z"))
        session.commit()

    monkeypatch.setattr(main, "get_seller_orders", lambda since: {"orders": []})
    monkeypatch.setattr(
        main, "ingest_manapool_orders",
        lambda *a, **k: {"imported": 3, "already_known": 5, "failed": []},
    )
    client = TestClient(main.app)
    response = client.post("/manapool/sync")
    assert response.status_code == 200
    assert 'class="outcome-banner outcome-banner-success"' in response.text
    assert "New orders imported: <strong>3</strong>" in response.text
    assert "Already known: <strong>5</strong>" in response.text
    assert 'class="outcome-banner outcome-banner-warning"' not in response.text


def test_sync_partial_failure_shows_warning_not_success(tmp_path, monkeypatch):
    """The partially-synchronized state: real, and worth its own visual
    weight (UX epic item 12), not a footnote under a green banner."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(AppSetting(key=main.GO_LIVE_SETTING_KEY, value="2026-01-01T00:00:00Z"))
        session.commit()

    monkeypatch.setattr(main, "get_seller_orders", lambda since: {"orders": []})
    monkeypatch.setattr(
        main, "ingest_manapool_orders",
        lambda *a, **k: {
            "imported": 2, "already_known": 1,
            "failed": ["order xyz: allocation error"],
        },
    )
    client = TestClient(main.app)
    response = client.post("/manapool/sync")
    assert response.status_code == 200
    assert response.text.count('class="outcome-banner outcome-banner-warning"') == 2
    assert "Some orders failed to sync" in response.text
    assert "order xyz: allocation error" in response.text
    assert 'class="outcome-banner outcome-banner-success"' not in response.text


# --- site-wide shipment-sync banner, upgraded for consistency -----------

def test_shipment_sync_banner_uses_shared_outcome_banner_component(tmp_path, monkeypatch):
    from datetime import datetime

    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(SalesOrder(
            external_order_id="mp-stuck", source="manapool", status="shipped",
            tracking_number="1Z1", shipped_at=datetime(2026, 8, 15, 9, 0, 0),
            mana_pool_shipment_failure_detail="network down",
        ))
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders")
    assert 'class="outcome-banner outcome-banner-danger"' in response.text
    assert "1 order failed to sync to Mana Pool." in response.text
