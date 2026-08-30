"""UX/design-system epic, item 20: Admin information-architecture
redesign.

Verified current state first, per the item's own instruction that
Section 10 "Observed" descriptions had already drifted by the time
items 16/17 came up: confirmed the tool list is unchanged from the
original audit -- Batches & Inventory Metrics, Legacy Migration,
Go-Live, Import History, Create Simulated Order, and Color Backfill.
Color Backfill's own text on the page already confirmed a manual admin
trigger still exists alongside its separate hourly Railway Cron Job
automation -- both, not one retired in favor of the other.

This is the second item in the epic with a real, explicitly authorized
functional change (Section 22.4, resolved 2026-08-29, same pattern as
item 19's Section 22.5): Create Simulated Order is now genuinely
blocked in production, not just labeled. No "which environment is
this" signal existed anywhere in this codebase before this item;
Railway's own RAILWAY_ENVIRONMENT_NAME (set automatically, confirmed
live via SSH against the one real deployment to be exactly
"production") is used directly rather than introducing a new
CardFoundry-specific variable to configure.

Last-run info is pulled from whatever's actually already recorded per
tool (ImportRecord for Legacy Migration, the AppSetting value for
Go-Live, SalesOrder rows for Create Simulated Order) and explicitly
says "no record" for Color Backfill, which is genuinely stateless --
no fabricated timestamps anywhere.
"""
import os
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import AppSetting, Base, Batch, ImportRecord, SalesOrder


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'admin-item20.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def as_production(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")


# --- the real tool list, confirmed unchanged from the original audit -----

def test_tool_list_matches_original_audit_no_drift(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/admin")
    assert response.status_code == 200
    for href in (
        "/admin/batches", "/legacy-migration", "/cutover", "/imports",
        "/admin/simulated-order",
    ):
        assert f'href="{href}"' in response.text
    assert 'action="/admin/color-backfill"' in response.text


def test_color_backfill_manual_trigger_exists_alongside_scheduled_job(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/admin")
    assert "hourly" in response.text
    assert "Railway Cron Job" in response.text
    assert 'action="/admin/color-backfill"' in response.text


# --- categories -------------------------------------------------------------

def test_page_organized_into_five_categories(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/admin")
    for heading in (
        "Monitoring &amp; Metrics", "Imports &amp; Migrations", "Data Repair",
        "Environment &amp; Launch Configuration", "Testing / Development",
    ):
        assert heading in response.text


# --- last-run info: real, sourced, or explicitly absent --------------------

def test_legacy_migration_shows_real_last_import(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = Batch(batch_code="leg_a")
        session.add(batch)
        session.flush()
        session.add(ImportRecord(
            batch_id=batch.id, filename="legacy_export.csv", file_hash="h",
            card_count=99, imported_at=datetime(2026, 7, 1, 10, 0),
        ))
        session.commit()
    response = TestClient(main.app).get("/admin")
    assert "99 card(s)" in response.text
    assert "legacy_export.csv" in response.text


def test_legacy_migration_shows_no_record_when_absent(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/admin")
    idx = response.text.index("Legacy Migration")
    end = response.text.index("Import History", idx)
    assert "no record" in response.text[idx:end]


def test_legacy_migration_ignores_non_legacy_import_records(tmp_path, monkeypatch):
    # A regular (non-leg_*) batch import must not be mistaken for a
    # legacy migration run.
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = Batch(batch_code="A7")
        session.add(batch)
        session.flush()
        session.add(ImportRecord(
            batch_id=batch.id, filename="regular_batch.csv", file_hash="h",
            card_count=5, imported_at=datetime(2026, 7, 1, 10, 0),
        ))
        session.commit()
    response = TestClient(main.app).get("/admin")
    idx = response.text.index("Legacy Migration")
    end = response.text.index("Import History", idx)
    assert "regular_batch.csv" not in response.text[idx:end]
    assert "no record" in response.text[idx:end]


def test_go_live_shows_real_current_setting(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(AppSetting(
            key="manapool_go_live_at",
            value=datetime(2026, 6, 15, 9, 30).isoformat(),
        ))
        session.commit()
    response = TestClient(main.app).get("/admin")
    idx = response.text.index("Go-Live")
    assert "Jun 15, 2026" in response.text[idx:idx + 600]


def test_go_live_shows_not_set_when_absent(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/admin")
    idx = response.text.index("Go-Live")
    assert "not set" in response.text[idx:idx + 600]


def test_color_backfill_shows_no_record_stateless(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/admin")
    idx = response.text.index("Color Backfill")
    end = response.text.index("Environment &amp; Launch Configuration", idx)
    snippet = response.text[idx:end]
    assert "no record" in snippet
    assert "stateless" in snippet


def test_simulated_order_shows_real_count_and_most_recent(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(SalesOrder(external_order_id="TEST-001", source="simulation", status="new"))
        session.add(SalesOrder(external_order_id="TEST-002", source="simulation", status="new"))
        session.commit()
    response = TestClient(main.app).get("/admin")
    idx = response.text.index("Create Simulated Order")
    snippet = response.text[idx:]
    assert "2 (most recent: TEST-002)" in snippet


def test_simulated_order_shows_no_record_when_absent(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/admin")
    idx = response.text.index("Create Simulated Order")
    assert "no record" in response.text[idx:]


# --- risk badges + dev-only marking, reusing the shared badge system -----

def test_risk_badges_use_shared_badge_system(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/admin")
    assert 'class="badge badge-info"' in response.text  # low risk
    assert 'class="badge badge-warning"' in response.text  # medium risk
    assert 'class="badge badge-danger"' in response.text  # high risk


def test_simulated_order_marked_dev_only_when_reachable(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/admin")
    idx = response.text.index("Create Simulated Order")
    snippet = response.text[idx:idx + 700]
    assert "Testing / Dev Only</span>" in snippet


# --- "who" placeholder: honest, not fictional RBAC --------------------------

def test_who_placeholder_reflects_real_access_model(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/admin")
    assert "Any operator with the shared admin password" in response.text
    assert "no per-role permissions yet" in response.text


# --- Section 22.4: genuinely blocked in production, not just labeled -----

def test_simulated_order_reachable_outside_production(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    assert client.get("/admin/simulated-order").status_code == 200
    response = client.get("/admin")
    assert 'href="/admin/simulated-order"' in response.text


def test_simulated_order_form_blocked_in_production(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    as_production(monkeypatch)
    response = TestClient(main.app).get("/admin/simulated-order")
    assert response.status_code == 403
    assert "Not available in production" in response.text


def test_simulated_order_submission_blocked_in_production(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    as_production(monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/orders/create",
        data={"order_reference": "TEST-999", "items_text": "Alpha | ONE | 1 | normal | 1"},
    )
    assert response.status_code == 403
    with Session(db) as session:
        assert session.query(SalesOrder).filter_by(external_order_id="TEST-999").first() is None


def test_admin_page_hides_simulated_order_link_in_production(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    as_production(monkeypatch)
    response = TestClient(main.app).get("/admin")
    assert response.status_code == 200
    assert 'href="/admin/simulated-order"' not in response.text
    assert "Blocked in production" in response.text


def test_admin_page_still_shows_other_tools_in_production(tmp_path, monkeypatch):
    # The production block is scoped narrowly to the one testing tool --
    # everything else on the page stays fully reachable.
    setup_db(tmp_path, monkeypatch)
    as_production(monkeypatch)
    response = TestClient(main.app).get("/admin")
    for href in ("/admin/batches", "/legacy-migration", "/cutover", "/imports"):
        assert f'href="{href}"' in response.text
    assert 'action="/admin/color-backfill"' in response.text


def test_production_signal_is_railway_environment_name_exactly(tmp_path, monkeypatch):
    # Confirms the specific env var and value this relies on -- a
    # different Railway environment name (e.g. a future "staging")
    # must NOT trigger the block.
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    response = TestClient(main.app).get("/admin/simulated-order")
    assert response.status_code == 200


def test_production_check_is_case_insensitive(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "Production")
    response = TestClient(main.app).get("/admin/simulated-order")
    assert response.status_code == 403


def test_is_production_environment_helper_unit(monkeypatch):
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    assert main._is_production_environment() is False
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    assert main._is_production_environment() is True
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "development")
    assert main._is_production_environment() is False


# --- no functional regression outside the one authorized change -----------

def test_simulated_order_still_works_outside_production(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/orders/create",
        data={"order_reference": "TEST-003", "items_text": "Alpha | ONE | 1 | normal | 1"},
    )
    # No matching inventory seeded -- correctly blocks and rolls back,
    # same as before this item (this is the existing, unrelated
    # allocation-safety behavior, not the new production block).
    assert response.status_code == 409
    assert "Order allocation blocked" in response.text


def test_color_backfill_route_behavior_unchanged(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).post("/admin/color-backfill")
    assert response.status_code == 200
    assert "Inventory cards backfilled: <strong>0</strong>" in response.text


def test_page_header_and_one_h1(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/admin")
    assert '<nav class="breadcrumbs" aria-label="Breadcrumb">' in response.text
    assert response.text.count("<h1") == 1
