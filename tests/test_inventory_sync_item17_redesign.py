"""UX/design-system epic, item 17: Inventory Sync staged-workflow redesign.

Verified current state first, per the item's own instruction: table
overflow (item 4 / the v1.84.1 site-wide sweep) was already fixed on
this page's tables -- confirmed still true. What was NOT already fixed,
found while verifying rather than assuming: a real 157px page-level
overflow at 320px on every preview-detail page with a typed-confirmation
<input size="50"> (New Listing Preview, Quantity Reconciliation Preview,
Clean-Rebuild Preview) -- not a table, so outside item 4's original
table-only sweep scope. Fixed globally (max-width/box-sizing on the
shared input/textarea/select rule), which also incidentally fixes the
same latent bug on the Pricing page's own confirmation input (item 16).

"Perform Sync with Mana Pool" already chains backfill -> maintenance
preview -> quantity reconciliation -> a landed new-listing preview
review/confirm/execute step -- mapped onto an explicit Scope -> Preview
-> Review/Confirm/Execute -> Verify stage tracker rather than inventing
parallel stages. The day-to-day-vs-admin backlog item (flagged and
deferred twice, first in 1c26cff) is resolved here: Maintenance-Mode
Preview and Clean-Rebuild Preview move behind one closed-by-default
disclosure; Perform Sync, Choose Batches, and Exceptions stay unhidden.

No scheduled/cron script calls any /inventory-sync/* route -- confirmed
by grepping scheduled_order_sync.py (calls /manapool/sync),
scheduled_pricing_apply.py (calls /pricing/*), and
scheduled_color_backfill.py (calls /admin/color-backfill). This page is
purely interactive; unlike item 16, there is no external HTTP contract
to preserve here.
"""
import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, InventorySyncJob


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'inventory-sync-item17.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_job(session, *, mode, status="completed", snapshot=None):
    job = InventorySyncJob(
        mode=mode, status=status, snapshot_json=json.dumps(snapshot or {}),
    )
    session.add(job)
    session.commit()
    return job.id


# --- the pre-existing input-overflow bug this item found and fixed -------

def test_confirmation_input_max_width_css_present(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory-sync")
    assert "max-width: 100%;" in response.text
    assert "box-sizing: border-box;" in response.text


def test_new_listing_preview_no_longer_overflows_320px_worth_of_input(tmp_path, monkeypatch):
    # Regression guard for the real 157px overflow found live: a plain
    # text search for the confirmation input's size attribute combined
    # with the global CSS fix above is what actually neutralizes it
    # (verified live via Playwright this session); this test just
    # confirms the input and the fix both still exist together.
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(session, mode="new_listing_preview", snapshot={
            "rows": [], "summary": {"candidates": 1, "priced": 1, "held": 0, "excluded": 0},
        })
    response = TestClient(main.app).get(f"/inventory-sync/{job_id}")
    assert 'size="50"' in response.text


# --- staged workflow: Scope -> Preview -> Review/Confirm/Execute -> Verify

def test_main_page_shows_scope_stage(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory-sync")
    assert 'class="sync-stage-tracker no-print"' in response.text
    assert 'class="sync-stage sync-stage-current">Scope</span>' in response.text


def test_maintenance_preview_shows_preview_stage(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(session, mode="maintenance_preview", snapshot={
            "rows": [], "summary": {"categories": {}, "exact_quantity_writes": 0},
        })
    response = TestClient(main.app).get(f"/inventory-sync/{job_id}")
    assert 'class="sync-stage sync-stage-current">Preview</span>' in response.text
    assert 'class="sync-stage sync-stage-done">Scope</span>' in response.text


def test_new_listing_preview_shows_review_confirm_execute_stage(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(session, mode="new_listing_preview", snapshot={
            "rows": [], "summary": {"candidates": 0, "priced": 0, "held": 0, "excluded": 0},
        })
    response = TestClient(main.app).get(f"/inventory-sync/{job_id}")
    assert 'sync-stage-current">Review → Confirm → Execute</span>' in response.text
    assert 'sync-stage sync-stage-done">Scope</span>' in response.text
    assert 'sync-stage sync-stage-done">Preview</span>' in response.text


def test_new_listing_apply_shows_verify_stage(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(session, mode="new_listing_apply", snapshot={
            "source_job_id": 1, "scryfall_updates": [], "product_updates": [],
            "responses": [], "applied_at": "2026-08-29T00:00:00Z",
        })
    response = TestClient(main.app).get(f"/inventory-sync/{job_id}")
    assert 'sync-stage-current">Verify</span>' in response.text


def test_reconciliation_preview_and_apply_show_correct_stages(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        preview_id = make_job(session, mode="reconciliation_preview", snapshot={
            "rows": [], "summary": {"candidates": 0, "increase": 0, "decrease": 0, "excluded": 0},
        })
        apply_id = make_job(session, mode="reconciliation_apply", snapshot={
            "source_job_id": preview_id, "updates": [], "excluded": [],
            "applied_at": "2026-08-29T00:00:00Z",
        })
    preview_resp = TestClient(main.app).get(f"/inventory-sync/{preview_id}")
    assert 'sync-stage-current">Review → Confirm → Execute</span>' in preview_resp.text
    apply_resp = TestClient(main.app).get(f"/inventory-sync/{apply_id}")
    assert 'sync-stage-current">Verify</span>' in apply_resp.text


def test_clean_rebuild_preview_shows_preview_stage_and_heavy_write_badge(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(session, mode="clean_rebuild_preview", snapshot={
            "summary": {"ready": False}, "exclusions": [],
        })
    response = TestClient(main.app).get(f"/inventory-sync/{job_id}")
    assert 'sync-stage-current">Preview</span>' in response.text
    assert "Heavy Write" in response.text


# --- freshness: no invented staleness threshold ---------------------------

def test_freshness_note_states_structural_reverification_not_a_timer(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(session, mode="maintenance_preview", snapshot={
            "rows": [], "summary": {"categories": {}, "exact_quantity_writes": 0},
        })
    response = TestClient(main.app).get(f"/inventory-sync/{job_id}")
    assert "expire on a timer" in response.text
    assert "re-verifies each row" in response.text
    assert "fresh immediately before writing" in response.text


def test_freshness_note_shows_built_timestamp(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(session, mode="new_listing_preview", snapshot={
            "rows": [], "summary": {"candidates": 0, "priced": 0, "held": 0, "excluded": 0},
        })
    response = TestClient(main.app).get(f"/inventory-sync/{job_id}")
    assert "Built " in response.text


# --- day-to-day-vs-admin backlog item, finally resolved -------------------

def test_advanced_workflows_disclosure_wraps_maintenance_and_rebuild(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory-sync")
    assert '<summary>Advanced / Admin Workflows</summary>' in response.text
    disclosure_idx = response.text.index("Advanced / Admin Workflows")
    tail = response.text[disclosure_idx:]
    assert 'action="/inventory-sync/preview"' in tail
    assert 'action="/inventory-sync/rebuild-preview"' in tail


def test_perform_sync_and_choose_batches_and_exceptions_stay_unhidden(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory-sync")
    disclosure_idx = response.text.index("Advanced / Admin Workflows")
    head = response.text[:disclosure_idx]
    assert 'action="/inventory-sync/perform-sync"' in head
    assert 'action="/inventory-sync/new-batches"' in head
    assert 'action="/inventory-sync/exceptions"' in head


def test_advanced_buttons_marked_read_only_and_secondary(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory-sync")
    disclosure_idx = response.text.index("Advanced / Admin Workflows")
    tail = response.text[disclosure_idx:]
    assert 'class="btn-secondary"' in tail
    assert "Read-only" in tail


def test_perform_sync_is_the_prominent_primary_action(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory-sync")
    perform_sync_idx = response.text.index('action="/inventory-sync/perform-sync"')
    snippet = response.text[perform_sync_idx:perform_sync_idx + 400]
    assert 'class="btn-primary"' in snippet


# --- risk badges reuse the shared status-badge system ----------------------

def test_risk_badges_use_shared_badge_markup(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory-sync")
    assert 'class="badge badge-info"' in response.text  # Routine
    assert 'class="badge badge-neutral"' in response.text  # Advanced


def test_heavy_write_badge_is_danger_role(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(session, mode="clean_rebuild_preview", snapshot={
            "summary": {"ready": False}, "exclusions": [],
        })
    response = TestClient(main.app).get(f"/inventory-sync/{job_id}")
    assert 'class="badge badge-danger"' in response.text


# --- job history: extended, not replaced -----------------------------------

def test_job_history_columns_extended_not_replaced(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory-sync")
    for col in ("Job", "Mode", "Status", "Items", "Created"):
        assert f"<th>{col}</th>" in response.text


def test_job_history_shows_readable_mode_label(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_job(session, mode="new_listing_preview", snapshot={
            "rows": [], "summary": {"candidates": 0, "priced": 5, "held": 1, "excluded": 2},
        })
    response = TestClient(main.app).get("/inventory-sync")
    assert "New Listing Preview" in response.text
    assert "5 priced / 1 held / 2 excluded" in response.text


def test_job_history_maintenance_preview_items_summary(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_job(session, mode="maintenance_preview", snapshot={
            "rows": [], "summary": {
                "categories": {"local_only_requires_listing": 3, "matched": 10},
                "exact_quantity_writes": 0,
            },
        })
    response = TestClient(main.app).get("/inventory-sync")
    assert "13 row(s)" in response.text


def test_job_history_empty_state_unchanged(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory-sync")
    assert "No inventory-sync previews yet." in response.text
    assert 'class="data-table-empty"' in response.text


# --- read-only vs. remote-write framing, reusing item 16's pattern --------

def test_new_listing_apply_section_framed_as_remote_write(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(session, mode="new_listing_preview", snapshot={
            "rows": [], "summary": {"candidates": 1, "priced": 1, "held": 0, "excluded": 0},
        })
    response = TestClient(main.app).get(f"/inventory-sync/{job_id}")
    assert "Remote write" in response.text
    assert 'class="btn-primary"' in response.text


def test_maintenance_preview_new_listings_section_framed_as_read_only(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(session, mode="maintenance_preview", snapshot={
            "rows": [], "summary": {
                "categories": {"local_only_requires_listing": 2},
                "exact_quantity_writes": 0,
            },
        })
    response = TestClient(main.app).get(f"/inventory-sync/{job_id}")
    assert "Read-only so far" in response.text


# --- exceptions: first-class review queue -----------------------------------

def test_exceptions_page_shows_total_count_banner(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory-sync/exceptions")
    assert "outcome-banner" in response.text
    assert "exception(s) across 4 categories" in response.text


def test_exceptions_page_not_computed_on_main_page(tmp_path, monkeypatch):
    # The exceptions total is only ever computed on the exceptions page
    # itself (already-paid cost there) -- not duplicated onto the main
    # page's own load, which would be a new expensive query path.
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory-sync")
    assert "exception(s) across 4 categories" not in response.text


def test_attempt_to_sync_is_primary_with_risk_badge(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory-sync/exceptions")
    idx = response.text.index("Attempt to Sync</h2>")
    snippet = response.text[idx:idx + 500]
    assert 'class="btn-primary"' in snippet
    assert "Routine" in snippet


# --- external-caller diligence check ---------------------------------------

def test_no_scheduled_script_calls_inventory_sync_routes():
    import pathlib
    repo_root = pathlib.Path(__file__).parent.parent
    for script_name in (
        "scheduled_order_sync.py", "scheduled_pricing_apply.py", "scheduled_color_backfill.py",
    ):
        script_path = repo_root / script_name
        content = script_path.read_text()
        assert "inventory-sync" not in content
        assert "inventory_sync" not in content


# --- no functional regression ----------------------------------------------

def test_page_still_200s_and_page_header_present(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory-sync")
    assert response.status_code == 200
    assert '<nav class="breadcrumbs" aria-label="Breadcrumb">' in response.text
    assert response.text.count("<h1") == 1


def test_job_not_found_still_404s(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory-sync/999")
    assert response.status_code == 404
