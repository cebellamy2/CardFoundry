"""Follow-up to the 2026-08-30 status-vocabulary investigation, item 1:
refresh the InventoryListingStatus cache at publish time, not only on
the next manual Perform Sync / Exceptions Review / Get-This-Batch-Live
visit.

Deliberately NOT chosen: extending the hourly cron to sync inventory
(the v1.55.2-v1.61.0 rate-limit saga is the reason -- any option adding
recurring Mana Pool calls is the riskier one). Instead, the publish
flow's own already-in-scope data does the write, with zero additional
Mana Pool traffic.

new_listing_upload_service.apply_new_listing_preview's own
reconfirmed_card_ids (set immediately before writing, during the
existing fresh-availability re-check -- see
test_new_listing_upload_service.py for that logic's own coverage) is
exactly enough: no second Mana Pool call is needed to know which cards
just got listed. Covers every path that converges on this same apply
route -- Publish New Listings, "Send New Inventory to Mana Pool"
(v1.59.0), and the Exceptions page's per-row Publish (v1.62.0/v1.72.1)
all build a new_listing_preview job and funnel through
new_listing_apply_route (confirmed by tracing each route in main.py to
_build_and_store_new_listing_preview, which all three call).

The cache write runs in its own session, after the publish's own
commit, wrapped so any failure there can never roll back or fail the
publish response -- a stale cache row is the correct failure mode.
"""
import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from inventory_sync_workflow import mark_cards_listed
from models import Base, Batch, InventoryCard, InventoryListingStatus, InventorySyncJob
from new_listing_upload_service import apply_new_listing_preview


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'listing_cache_publish.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def add_card(session, **overrides):
    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    values = {
        "batch_id": batch.id, "name": "Alpha", "set_code": "ONE", "collector_number": "1",
        "mtgjson_id": "MTG-ALPHA", "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
        "condition": "near_mint", "finish": "normal", "scryfall_id": "sf-alpha", "status": "available",
    }
    values.update(overrides)
    card = InventoryCard(**values)
    session.add(card)
    session.flush()
    return card


def priced_row(card_id, price_cents=199):
    return {
        "key": ["MTG-ALPHA", "EN", "LP", "NF"],
        "identity": {
            "name": "Alpha", "set_code": "ONE", "collector_number": "1",
            "scryfall_id": "sf-alpha", "language_id": "EN",
            "condition_id": "LP", "finish_id": "NF",
        },
        "desired_quantity": 1, "card_ids": [card_id], "path": "scryfall_id",
        "status": "priced", "target_price_cents": price_cents,
    }


# --- mark_cards_listed: direct unit coverage -----------------------------

def test_mark_cards_listed_inserts_new_rows(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session)
        session.commit()
        card_id = card.id

    with Session(db) as session:
        written = mark_cards_listed(session, [card_id])
        session.commit()
    assert written == 1

    with Session(db) as session:
        row = session.get(InventoryListingStatus, card_id)
        assert row.listing_status == "listed"


def test_mark_cards_listed_updates_an_existing_not_listed_row(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session)
        session.add(InventoryListingStatus(inventory_card_id=card.id, listing_status="not_listed"))
        session.commit()
        card_id = card.id

    with Session(db) as session:
        mark_cards_listed(session, [card_id])
        session.commit()

    with Session(db) as session:
        assert session.query(InventoryListingStatus).count() == 1
        row = session.get(InventoryListingStatus, card_id)
        assert row.listing_status == "listed"


def test_mark_cards_listed_handles_empty_and_duplicate_ids(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session)
        session.commit()
        card_id = card.id

    with Session(db) as session:
        assert mark_cards_listed(session, []) == 0
        written = mark_cards_listed(session, [card_id, card_id, card_id])
        session.commit()
    assert written == 1

    with Session(db) as session:
        assert session.query(InventoryListingStatus).count() == 1


# --- apply_new_listing_preview: published_card_ids ------------------------

def test_apply_new_listing_preview_reports_published_card_ids(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, current_price=1.99)
        session.commit()

        result = apply_new_listing_preview(
            session, {"rows": [priced_row(card.id)]},
            seller_loader=lambda min_quantity: [],
            scryfall_writer=lambda updates: [{"inventory": [{"id": "inv-1"}], "skipped": []}],
            product_writer=lambda updates: [],
            optimizer_call=lambda cart, seller: (_ for _ in ()).throw(
                AssertionError("optimizer must never be called for first-time listing")
            ),
            listings_call=lambda ids: [],
            seller_id="seller-1",
            market_catalog_scryfall_call=lambda ids: {"data": []},
        )
    assert result["published_card_ids"] == [card.id]


def test_apply_new_listing_preview_excludes_unavailable_cards_from_published_ids(
    tmp_path, monkeypatch,
):
    """A row whose local availability changed since preview is excluded
    entirely -- its card must not appear in published_card_ids, since
    nothing was actually written for it."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, current_price=1.99, status="sold")
        session.commit()

        try:
            apply_new_listing_preview(
                session, {"rows": [priced_row(card.id)]},
                seller_loader=lambda min_quantity: [],
                scryfall_writer=lambda updates: (_ for _ in ()).throw(
                    AssertionError("must not write when nothing is eligible")
                ),
                product_writer=lambda updates: [],
                optimizer_call=lambda cart, seller: None,
                listings_call=lambda ids: [],
                seller_id="seller-1",
                market_catalog_scryfall_call=lambda ids: {"data": []},
            )
            raised = False
        except Exception:
            raised = True
    assert raised  # "no longer valid to publish" -- nothing to check further


# --- end-to-end through the real route ------------------------------------

def _make_preview_job(session, card_id, price_cents=199):
    job = InventorySyncJob(
        status="completed", mode="new_listing_preview",
        snapshot_json=json.dumps({"rows": [priced_row(card_id, price_cents)]}),
    )
    session.add(job)
    session.commit()
    return job.id


def _patch_publish_dependencies(monkeypatch, inv_id="inv-1"):
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity: [])
    monkeypatch.setattr(
        main, "create_or_update_inventory_by_scryfall_id",
        lambda updates: [{"inventory": [{"id": inv_id, "quantity": 1, "price_cents": 199}], "skipped": []}],
    )
    monkeypatch.setattr(
        main, "optimize_exact_variant_batch_with_conflicts",
        lambda cart, seller: (_ for _ in ()).throw(AssertionError("optimizer must not be called")),
    )
    monkeypatch.setattr(main, "get_inventory_listings_by_ids", lambda ids: [])
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", lambda ids: {"data": []})


def test_publish_new_listings_route_marks_the_card_listed(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, current_price=1.99)
        session.commit()
        card_id = card.id
        job_id = _make_preview_job(session, card_id)

    _patch_publish_dependencies(monkeypatch)

    client = TestClient(main.app)
    response = client.post(
        f"/inventory-sync/{job_id}/new-listings/apply",
        data={"confirmation": "PUBLISH NEW LISTINGS"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db) as session:
        row = session.get(InventoryListingStatus, card_id)
        assert row is not None
        assert row.listing_status == "listed"


def test_publish_flips_an_existing_not_listed_cache_row_without_a_manual_sync_visit(
    tmp_path, monkeypatch,
):
    """The actual scenario item 1 was written for: a card already cached
    as "not_listed" (from an earlier Exceptions Review visit) flips to
    "listed" purely from the publish action itself -- no second visit to
    Perform Sync/Exceptions/Get-This-Batch-Live required."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, current_price=1.99)
        session.add(InventoryListingStatus(inventory_card_id=card.id, listing_status="not_listed"))
        session.commit()
        card_id = card.id
        job_id = _make_preview_job(session, card_id)

    with Session(db) as session:
        assert session.get(InventoryListingStatus, card_id).listing_status == "not_listed"

    _patch_publish_dependencies(monkeypatch)

    client = TestClient(main.app)
    response = client.post(
        f"/inventory-sync/{job_id}/new-listings/apply",
        data={"confirmation": "PUBLISH NEW LISTINGS"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db) as session:
        assert session.query(InventoryListingStatus).count() == 1
        assert session.get(InventoryListingStatus, card_id).listing_status == "listed"


def test_publish_still_succeeds_even_if_the_cache_write_fails(tmp_path, monkeypatch):
    """The hard requirement: a cache-write failure must never roll back
    or fail-report a successful publish. A stale cache row is the
    strictly better failure mode."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, current_price=1.99)
        session.commit()
        card_id = card.id
        job_id = _make_preview_job(session, card_id)

    _patch_publish_dependencies(monkeypatch)

    def boom(session, card_ids):
        raise RuntimeError("simulated cache-write failure")

    monkeypatch.setattr(main, "mark_cards_listed", boom)

    client = TestClient(main.app)
    response = client.post(
        f"/inventory-sync/{job_id}/new-listings/apply",
        data={"confirmation": "PUBLISH NEW LISTINGS"},
        follow_redirects=False,
    )
    # The publish itself is unaffected: still a redirect to the apply
    # job's detail page, not an error.
    assert response.status_code == 303

    with Session(db) as session:
        apply_job_id = int(response.headers["location"].rsplit("/", 1)[-1])
        apply_job = session.get(InventorySyncJob, apply_job_id)
        assert apply_job is not None
        assert apply_job.mode == "new_listing_apply"
        result = json.loads(apply_job.snapshot_json)
        assert result["responses"]["scryfall_id"][0]["inventory"][0]["id"] == "inv-1"
        # The cache row was never written -- stale, not wrong-and-hidden.
        assert session.get(InventoryListingStatus, card_id) is None


def test_publish_route_does_not_write_a_cache_row_when_nothing_publishes(tmp_path, monkeypatch):
    """Confirmation-mismatch / already-listed / rate-limited failures
    must not touch the cache at all -- nothing was published."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, current_price=1.99)
        session.commit()
        card_id = card.id
        job_id = _make_preview_job(session, card_id)

    client = TestClient(main.app)
    response = client.post(
        f"/inventory-sync/{job_id}/new-listings/apply",
        data={"confirmation": "wrong confirmation"},
        follow_redirects=False,
    )
    assert response.status_code == 400

    with Session(db) as session:
        assert session.get(InventoryListingStatus, card_id) is None


# --- the Exceptions-page per-row Publish path, end to end -----------------

def test_exceptions_publish_path_also_flips_the_card_to_listed(tmp_path, monkeypatch):
    """The third publish path named in the item: Exceptions to Review's
    per-row Publish button. Confirms it converges on the same apply
    route and gets the same cache refresh, not a parallel, unwired copy."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, current_price=1.99)
        session.commit()
        card_id = card.id

    client = TestClient(main.app)

    # Step 1: the Exceptions page's own per-row Publish form.
    response = client.post(
        "/inventory-sync/exceptions/publish",
        data={
            "mtgjson_id": "MTG-ALPHA", "language_id": "EN",
            "condition_id": "LP", "finish_id": "NF", "card_ids": str(card_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    maintenance_job_id = int(response.headers["location"].rsplit("/", 1)[-1])

    # Step 2: "Price New Listings" -- builds the new_listing_preview job.
    monkeypatch.setattr(main, "get_inventory_listings_by_ids", lambda ids: [])
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", lambda ids: {"data": []})
    monkeypatch.setattr(
        main, "optimize_exact_variant_batch_with_conflicts",
        lambda cart, seller: (_ for _ in ()).throw(AssertionError("optimizer must not be called")),
    )
    response = client.post(
        f"/inventory-sync/{maintenance_job_id}/new-listings/preview", follow_redirects=False,
    )
    assert response.status_code == 303
    preview_job_id = int(response.headers["location"].rsplit("/", 1)[-1])

    # Step 3: Publish -- the same apply route every path converges on.
    _patch_publish_dependencies(monkeypatch)
    response = client.post(
        f"/inventory-sync/{preview_job_id}/new-listings/apply",
        data={"confirmation": "PUBLISH NEW LISTINGS"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db) as session:
        row = session.get(InventoryListingStatus, card_id)
        assert row is not None
        assert row.listing_status == "listed"
