"""UX/design-system epic, item 16: Competitive Pricing workflow redesign.

Verified current state first, per the item's own instruction: the page
was already consolidated to a single "Run Bulk Price Adjustment" button
driving Flow B (dd58bc6) -- there is no separate preview-vs-competitor-
only-preview choice to redesign. At the time this item ran, the old Flow
A routes (/pricing/job-preview, /pricing/competitive-job/*) were still
registered but unreachable from any UI entry point (confirmed via
app.routes) -- left untouched, out of this presentation-only item's
scope to remove.

2026-08-30 follow-up: those routes have since been deleted outright
(delink-then-delete, after confirming /pricing/competitive-job/{id} was
NOT actually dead -- 6 historical PricingJob rows still linked to it).
See test_orphaned_legacy_routes_still_registered_but_unlinked below,
which now asserts the opposite of its original finding.

Undercut ($0.05) and floor ($0.65) are confirmed still genuinely locked:
start_full_competitor_preview() hard-rejects any other value server-side.
The rules panel is read-only display, not an editable form.

Critical constraint: scheduled_pricing_apply.py drives this exact page's
routes non-interactively (POST /pricing/full-competitor-preview, GET
.../{id} polled for literal text markers, POST .../{id}/apply with a
hardcoded "confirmation" field). This file's
test_cron_contract_markers_* tests guard those three literal strings
directly; a genuine end-to-end run of the real scheduled_pricing_apply
functions against these routes was also performed manually this session
(TestClient + stubbed remote calls) and passed with exit code 0.
"""
import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from main import _batch_code_group  # noqa: F401  (sanity: no import collision with item 15's helper)
from models import Base, PricingJob


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'pricing-item16.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


MOZILLA_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/605.1.15"


def applyable_change_row():
    return {
        "inventory_id": "inv-1", "product_id": "p-1", "name": "Alpha",
        "set_code": "SET", "collector_number": "1", "language_id": "EN",
        "condition_id": "LP", "finish_id": "NF", "quantity": 1,
        "current_price": 100, "competitor_inventory_id": "comp-1",
        "competitor_product_id": "comp-product-1", "competitor_price": 90,
        "competitor_condition": "LP", "competitor_language": "EN", "competitor_finish": "NF",
        "allowed_conditions": ["LP", "NM"], "target_price": 85,
        "change_cents": -15, "action": "decrease",
        "validation_status": "passed", "validation_reason": "ok",
        "floor_applied": False, "price_classification": "competitor_undercut",
        "price_source": "competitor", "pricing_evidence_hash": "hash", "preview_only": True,
    }


def stub_successful_apply(monkeypatch):
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity: [
        {"product_id": "p-1", "quantity": 1, "product_type": "mtg_single",
         "product": {"single": {"mtgjson_id": "mtg-1", "language_id": "EN",
                                  "condition_id": "LP", "finish_id": "NF"}}},
    ])
    monkeypatch.setattr(main, "sellable_remote_product_ids", lambda session, inv: {"p-1"})
    monkeypatch.setattr(main, "get_inventory_listings_by_ids", lambda ids: [{
        "id": "comp-1", "product_id": "comp-product-1", "quantity": 1, "price_cents": 90,
        "effective_as_of": "2026-08-16T01:00:00Z",
        "product": {"single": {"name": "Alpha", "set": "SET", "number": "1",
                                "language_id": "EN", "condition_id": "LP", "finish_id": "NF"}},
    }])
    monkeypatch.setattr(main, "update_inventory_prices_by_product", lambda updates: [{
        "inventory": [{"product_id": u["product_id"], "price_cents": u["price_cents"],
                       "product": {"single": {"name": "Alpha"}}} for u in updates],
        "skipped": [],
    }])


def make_job(session, *, action, status="completed", triggered_by=None,
             response=None, request_extra=None, created_at=None):
    request_data = {"undercut_cents": 5, "floor_cents": 65}
    if triggered_by is not None:
        request_data["triggered_by"] = triggered_by
    if request_extra:
        request_data.update(request_extra)
    job = PricingJob(
        external_job_id=None,
        action=action,
        status=status,
        request_json=json.dumps(request_data),
        response_json=json.dumps(response or {}),
    )
    if created_at:
        job.created_at = created_at
    session.add(job)
    session.commit()
    return job.id


# --- point 1: the real current flow structure -----------------------------

def test_single_button_flow_confirmed_not_two_flows(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/pricing")
    assert response.status_code == 200
    assert "Run Bulk Price Adjustment" in response.text
    assert response.text.count('action="/pricing/full-competitor-preview"') == 1
    assert 'action="/pricing/job-preview"' not in response.text


def test_legacy_routes_deleted_not_just_unlinked(tmp_path, monkeypatch):
    # 2026-08-30 follow-up to this item's own finding: item 16 confirmed
    # dd58bc6 removed the entry point but not the routes; a later check
    # found /pricing/competitive-job/{id} was NOT actually dead (6
    # historical PricingJob rows still linked to it via
    # _pricing_job_detail_url). Delinked first, then deleted -- both
    # route families are gone now, not merely unreachable from the UI.
    setup_db(tmp_path, monkeypatch)
    paths = {r.path for r in main.app.routes if hasattr(r, "path")}
    assert "/pricing/job-preview" not in paths
    assert "/pricing/competitive-job/{local_job_id}" not in paths
    assert "/pricing/competitive-job/{local_job_id}/verify-search" not in paths
    assert "/pricing/competitive-job/{local_job_id}/verify/{inventory_id}" not in paths
    assert "/pricing/competitive-job/{local_job_id}/apply" not in paths
    response = TestClient(main.app).get("/pricing")
    assert "/pricing/job-preview" not in response.text
    assert "/pricing/competitive-job" not in response.text


# --- point 3: undercut/floor genuinely locked ------------------------------

def test_undercut_and_floor_are_server_enforced_not_editable(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/pricing/full-competitor-preview",
        data={"undercut_dollars": "0.10", "floor_dollars": "0.65"},
    )
    assert response.status_code == 400
    assert "$0.05 undercut and $0.65 floor" in response.text


def test_rules_panel_is_read_only_display_not_a_form(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/pricing")
    assert "Pricing Rules" in response.text
    assert "$0.05" in response.text
    assert "$0.65" in response.text
    assert "Configurable?" in response.text
    assert "No — fixed by CardFoundry policy" in response.text
    # undercut/floor only ever appear as hidden fields (the Run form),
    # never as an editable text/number input a value could be typed into.
    assert 'type="hidden" name="undercut_dollars"' in response.text
    assert 'type="hidden" name="floor_dollars"' in response.text
    assert 'name="undercut_dollars" type="text"' not in response.text
    assert 'name="floor_dollars" type="text"' not in response.text
    assert 'type="number" name="undercut' not in response.text
    assert 'type="number" name="floor' not in response.text


# --- automated vs. manual trigger classification ---------------------------

def test_scheduled_client_classified_as_automated(tmp_path, monkeypatch):
    # scheduled_pricing_apply.py uses a bare httpx.Client with no custom
    # headers -- httpx's own default User-Agent, which TestClient's
    # default ("testclient") also does not start with "Mozilla/".
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "_run_full_competitor_preview", lambda job_id: None)
    client = TestClient(main.app)
    response = client.post(
        "/pricing/full-competitor-preview",
        data={"undercut_dollars": "0.05", "floor_dollars": "0.65"},
        follow_redirects=False,
    )
    job_id = int(response.headers["location"].rsplit("/", 1)[-1])
    with Session(main.engine) as session:
        job = session.get(PricingJob, job_id)
        assert json.loads(job.request_json)["triggered_by"] == "scheduled"
    detail = client.get(f"/pricing/full-competitor-preview/{job_id}")
    assert "Automated" in detail.text


def test_browser_client_classified_as_manual(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "_run_full_competitor_preview", lambda job_id: None)
    client = TestClient(main.app, headers={"user-agent": MOZILLA_UA})
    response = client.post(
        "/pricing/full-competitor-preview",
        data={"undercut_dollars": "0.05", "floor_dollars": "0.65"},
        follow_redirects=False,
    )
    job_id = int(response.headers["location"].rsplit("/", 1)[-1])
    with Session(main.engine) as session:
        job = session.get(PricingJob, job_id)
        assert json.loads(job.request_json)["triggered_by"] == "manual"
    detail = client.get(f"/pricing/full-competitor-preview/{job_id}")
    assert "Manual" in detail.text


def test_apply_trigger_read_independently_of_preview_trigger(tmp_path, monkeypatch):
    # A human can in principle finish applying a cron-started preview by
    # hand -- trigger source for the actual price-changing step must
    # reflect who called apply, not be inherited from preview creation.
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(
            session, action="competitor_only_full_preview", triggered_by="scheduled",
            response={"preview": {
                "changes": [applyable_change_row()],
                "summary": {"increases": 0, "decreases": 1},
            }},
        )

    client = TestClient(main.app, headers={"user-agent": MOZILLA_UA})
    stub_successful_apply(monkeypatch)

    response = client.post(
        f"/pricing/full-competitor-preview/{job_id}/apply",
        data={"confirmation": "APPLY COMPETITIVE PRICES"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    apply_job_id = int(response.headers["location"].rsplit("/", 1)[-1])
    with Session(db) as session:
        apply_job = session.get(PricingJob, apply_job_id)
        assert json.loads(apply_job.request_json)["triggered_by"] == "manual"


def test_missing_trigger_data_shows_unknown_not_a_guess(tmp_path, monkeypatch):
    # Legacy/pre-redesign rows have no triggered_by key at all.
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_job(session, action="competitor_only_full_preview", triggered_by=None)
    response = TestClient(main.app).get("/pricing")
    assert "Automated" not in response.text or "—" in response.text
    # The specific row must render the em-dash fallback, not a badge.
    assert 'class="muted">—</span>' in response.text


# --- job history: inspectable, consistent detail views ---------------------

def test_job_history_columns_extended_not_replaced(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/pricing")
    for col in ("ID", "Action", "Status", "Mana Pool Job ID", "Created", "Trigger", "Items"):
        assert f"<th>{col}</th>" in response.text


def test_history_row_links_to_detail_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(session, action="competitor_only_full_preview", triggered_by="manual")
    response = TestClient(main.app).get("/pricing")
    assert f'<a href="/pricing/full-competitor-preview/{job_id}">' in response.text
    assert "Bulk Price Adjustment — Preview" in response.text


def test_apply_row_links_to_apply_detail_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(
            session, action="competitor_only_full_apply", triggered_by="scheduled",
            response={"updates": [{}, {}], "repriced": [{}], "excluded": []},
        )
    response = TestClient(main.app).get("/pricing")
    assert f'<a href="/pricing/full-competitor-apply/{job_id}">' in response.text
    assert "2 applied / 1 repriced / 0 excluded" in response.text


def test_preview_row_shows_item_counts_without_extra_query(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_job(
            session, action="competitor_only_full_preview", triggered_by="scheduled",
            response={"preview": {"summary": {"increases": 7, "decreases": 3, "holds": 2}}},
        )
    response = TestClient(main.app).get("/pricing")
    assert "7 up / 3 down / 2 held" in response.text


def test_legacy_action_rows_render_readable_not_broken(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_job(session, action="competitive_bidirectional_preview", triggered_by=None)
        make_job(session, action="competitive_bidirectional_apply", triggered_by=None)
    response = TestClient(main.app).get("/pricing")
    assert response.status_code == 200
    assert "Legacy Preview (retired flow)" in response.text
    assert "Legacy Apply (retired flow)" in response.text


def test_legacy_preview_row_no_longer_links_anywhere(tmp_path, monkeypatch):
    """The specific behavior asked for in the 2026-08-30 delink: a
    competitive_bidirectional_preview row must render exactly like a
    competitive_bidirectional_apply row does today -- plain text, no
    <a href> at all -- not a link to a route that no longer exists."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_job(session, action="competitive_bidirectional_preview", triggered_by=None)
    response = TestClient(main.app).get("/pricing")
    assert response.status_code == 200
    assert "Legacy Preview (retired flow)" in response.text
    assert '<a href="/pricing/competitive-job' not in response.text


def test_mixed_row_types_render_correctly_with_a_legacy_row_inside_the_visible_window(
    tmp_path, monkeypatch,
):
    """/pricing only ever shows the last 20 jobs. Deliberately built here
    rather than relying on production's current scroll position (the 6
    real historical rows have since scrolled off screen) -- a linked
    _apply-style row, an unlinked competitor_only_full_preview-shaped
    plain row, and a competitive_bidirectional_preview legacy row all
    coexist in the same visible 20, in the same render, with no crash
    and no stray link on the legacy row."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        # Padding so the real mix of interest sits inside, not merely
        # equal to, the last-20 window.
        for _ in range(15):
            make_job(session, action="competitor_only_full_preview", triggered_by="scheduled")
        make_job(
            session, action="competitor_only_full_apply", triggered_by="scheduled",
            response={"updates": [{}], "repriced": [], "excluded": []},
        )
        legacy_preview_id = make_job(
            session, action="competitive_bidirectional_preview", triggered_by=None,
        )
        make_job(session, action="competitive_bidirectional_apply", triggered_by=None)
        make_job(session, action="competitor_only_full_preview", triggered_by="manual")

    response = TestClient(main.app).get("/pricing")
    assert response.status_code == 200
    assert f"<td>{legacy_preview_id}</td>" in response.text
    assert "Legacy Preview (retired flow)" in response.text
    assert "Legacy Apply (retired flow)" in response.text
    assert "Bulk Price Adjustment — Preview" in response.text
    assert "Bulk Price Adjustment — Applied" in response.text
    assert '<a href="/pricing/full-competitor-apply/' in response.text
    assert '<a href="/pricing/competitive-job' not in response.text


def test_in_flight_job_shows_em_dash_items_not_a_crash(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_job(
            session, action="competitor_only_full_preview", status="running",
            triggered_by="scheduled", response={"progress": {"stage": "pricing"}},
        )
    response = TestClient(main.app).get("/pricing")
    assert response.status_code == 200


# --- status/trigger badges reuse the shared system, not a new one ---------

def test_trigger_badges_use_shared_status_badge_markup(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_job(session, action="competitor_only_full_preview", triggered_by="scheduled")
        make_job(session, action="competitor_only_full_preview", triggered_by="manual")
    response = TestClient(main.app).get("/pricing")
    assert 'class="badge badge-info"' in response.text
    assert 'class="badge badge-success"' in response.text


def test_preview_detail_shows_status_and_trigger_badges(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(
            session, action="competitor_only_full_preview", status="running",
            triggered_by="manual", response={"progress": {"stage": "pricing"}},
        )
    response = TestClient(main.app).get(f"/pricing/full-competitor-preview/{job_id}")
    assert "Running</span>" in response.text
    assert "Manual</span>" in response.text


# --- read-only vs. remote-price-changing distinction -----------------------

def test_run_action_framed_as_read_only(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/pricing")
    assert "Read-only so far" in response.text
    assert 'class="btn-secondary"' in response.text


def test_apply_action_framed_as_remote_write(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(
            session, action="competitor_only_full_preview", triggered_by="manual",
            response={"preview": {
                "changes": [{
                    "inventory_id": "inv-1", "product_id": "p-1", "name": "Alpha",
                    "set_code": "SET", "collector_number": "1", "language_id": "EN",
                    "condition_id": "LP", "finish_id": "NF", "current_price": 100,
                    "competitor_price": 90, "competitor_condition": "LP",
                    "target_price": 85, "action": "decrease",
                }],
                "summary": {"increases": 0, "decreases": 1, "holds": 0},
            }},
        )
    response = TestClient(main.app).get(f"/pricing/full-competitor-preview/{job_id}")
    assert "Remote write" in response.text
    assert 'class="btn-primary"' in response.text


# --- point 2 (critical): the cron script's exact HTTP contract is unchanged

def test_cron_contract_markers_failed(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(
            session, action="competitor_only_full_preview", status="failed",
            triggered_by="scheduled", response={"error": "boom"},
        )
    response = TestClient(main.app).get(f"/pricing/full-competitor-preview/{job_id}")
    assert "Full Competitor-Only Preview Failed" in response.text


def test_cron_contract_markers_nothing_to_apply(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(
            session, action="competitor_only_full_preview", status="completed",
            triggered_by="scheduled",
            response={"preview": {"changes": [], "summary": {"increases": 0, "decreases": 0}}},
        )
    response = TestClient(main.app).get(f"/pricing/full-competitor-preview/{job_id}")
    assert "Nothing to apply" in response.text


def test_cron_contract_markers_apply_form_action_url(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(
            session, action="competitor_only_full_preview", status="completed",
            triggered_by="scheduled",
            response={"preview": {
                "changes": [{
                    "inventory_id": "inv-1", "product_id": "p-1", "name": "Alpha",
                    "set_code": "SET", "collector_number": "1", "language_id": "EN",
                    "condition_id": "LP", "finish_id": "NF", "current_price": 100,
                    "competitor_price": 90, "competitor_condition": "LP",
                    "target_price": 85, "action": "decrease",
                }],
                "summary": {"increases": 0, "decreases": 1, "holds": 0},
            }},
        )
    response = TestClient(main.app).get(f"/pricing/full-competitor-preview/{job_id}")
    apply_marker = f'/pricing/full-competitor-preview/{job_id}/apply"'
    assert apply_marker in response.text


def test_cron_contract_apply_field_name_and_value_unchanged(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(session, action="competitor_only_full_preview")
    client = TestClient(main.app)
    response = client.post(
        f"/pricing/full-competitor-preview/{job_id}/apply",
        data={"confirmation": "wrong phrase"},
    )
    assert response.status_code == 400
    response = client.post(
        f"/pricing/full-competitor-preview/{job_id}/apply",
        data={"not_confirmation": "APPLY COMPETITIVE PRICES"},
    )
    assert response.status_code == 422  # missing required "confirmation" field


def test_cron_contract_start_route_unchanged_shape(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "_run_full_competitor_preview", lambda job_id: None)
    response = TestClient(main.app).post(
        "/pricing/full-competitor-preview",
        data={"undercut_dollars": "0.05", "floor_dollars": "0.65"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/pricing/full-competitor-preview/")


def test_cron_contract_apply_success_still_303(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        job_id = make_job(
            session, action="competitor_only_full_preview", status="completed",
            response={"preview": {
                "changes": [applyable_change_row()],
                "summary": {"increases": 0, "decreases": 1},
            }},
        )
    stub_successful_apply(monkeypatch)
    response = TestClient(main.app).post(
        f"/pricing/full-competitor-preview/{job_id}/apply",
        data={"confirmation": "APPLY COMPETITIVE PRICES"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/pricing/full-competitor-apply/")
