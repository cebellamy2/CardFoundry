import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import main
from models import PricingJob
from tests.test_competitor_pricing_apply_routes import (
    change_row,
    fresh_competitor_listing,
    make_completed_preview_job,
    setup_db,
)


def make_apply_job(tmp_path, monkeypatch, *, changes=None, applied_product_ids=None):
    """Runs the real apply route end to end so the resulting apply job's
    shape (and its link back to the source preview job) is exactly what
    production would produce -- not a hand-built stand-in."""
    db = setup_db(tmp_path, monkeypatch)
    changes = changes if changes is not None else [change_row()]
    with Session(db) as session:
        job = make_completed_preview_job(session, changes=changes, decreases=len(changes))
        session.commit()
        preview_job_id = job.id

    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity: [])
    monkeypatch.setattr(
        main, "sellable_remote_product_ids",
        lambda session, inv: {row["product_id"] for row in changes},
    )
    monkeypatch.setattr(
        main, "get_inventory_listings_by_ids",
        lambda ids: [
            fresh_competitor_listing(
                inventory_id=row["competitor_inventory_id"],
                product_id=row["competitor_product_id"],
                price_cents=row["competitor_price"],
            )
            for row in changes
        ],
    )

    def fake_writer(updates):
        return [{"inventory": [
            {"product_id": u["product_id"], "price_cents": u["price_cents"],
             "product": {"single": {"name": "Alpha"}}}
            for u in updates
        ], "skipped": []}]

    monkeypatch.setattr(main, "update_inventory_prices_by_product", fake_writer)

    client = TestClient(main.app)
    response = client.post(
        f"/pricing/full-competitor-preview/{preview_job_id}/apply",
        data={"confirmation": "APPLY COMPETITIVE PRICES"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    apply_job_id = int(response.headers["location"].rsplit("/", 1)[-1])
    return db, client, preview_job_id, apply_job_id


def test_apply_detail_page_shows_revert_section_with_prior_price(tmp_path, monkeypatch):
    db, client, _, apply_job_id = make_apply_job(tmp_path, monkeypatch)
    detail = client.get(f"/pricing/full-competitor-apply/{apply_job_id}")
    assert detail.status_code == 200
    assert "Revert to Previous Price" in detail.text
    assert 'name="product_ids" value="p-1" checked' in detail.text
    # Applied price ($0.85, from change_row's default target_price=85) and
    # the prior price to revert to ($1.00, current_price=100) both show.
    assert "$0.85" in detail.text and "$1.00" in detail.text
    assert "does not and cannot un-send" in detail.text


def test_revert_route_pushes_prior_price_and_creates_job(tmp_path, monkeypatch):
    db, client, preview_job_id, apply_job_id = make_apply_job(tmp_path, monkeypatch)

    written = {}

    def revert_writer(updates):
        written["updates"] = updates
        return [{"inventory": [
            {"product_id": u["product_id"], "price_cents": u["price_cents"],
             "product": {"single": {"name": "Alpha"}}}
            for u in updates
        ], "skipped": []}]

    monkeypatch.setattr(main, "update_inventory_prices_by_product", revert_writer)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity: [])
    monkeypatch.setattr(main, "sellable_remote_product_ids", lambda session, inv: {"p-1"})

    response = client.post(
        f"/pricing/full-competitor-apply/{apply_job_id}/revert",
        data={"product_ids": ["p-1"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert written["updates"] == [
        {"product_type": "mtg_single", "product_id": "p-1", "price_cents": 100, "quantity": None},
    ]

    revert_job_id = int(response.headers["location"].rsplit("/", 1)[-1])
    with Session(db) as session:
        revert_job = session.get(PricingJob, revert_job_id)
        assert revert_job.action == "competitor_only_full_revert"
        result = json.loads(revert_job.response_json)
        assert result["source_apply_job_id"] == apply_job_id
        assert result["reverts"] == written["updates"]

    detail = client.get(f"/pricing/full-competitor-revert/{revert_job_id}")
    assert detail.status_code == 200
    assert "Alpha" in detail.text
    assert "$1.00" in detail.text
    assert f'href="/pricing/full-competitor-apply/{apply_job_id}"' in detail.text


def test_revert_refused_when_no_items_selected(tmp_path, monkeypatch):
    _, client, _, apply_job_id = make_apply_job(tmp_path, monkeypatch)
    response = client.post(f"/pricing/full-competitor-apply/{apply_job_id}/revert", data={})
    assert response.status_code == 409
    assert "No items were selected" in response.text


def test_revert_refused_for_product_not_part_of_apply(tmp_path, monkeypatch):
    _, client, _, apply_job_id = make_apply_job(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity: [])
    monkeypatch.setattr(main, "sellable_remote_product_ids", lambda session, inv: {"p-999"})
    response = client.post(
        f"/pricing/full-competitor-apply/{apply_job_id}/revert",
        data={"product_ids": ["p-999"]},
    )
    assert response.status_code == 200
    assert "Not part of this apply job" in response.text


def test_revert_excludes_item_no_longer_locally_sellable(tmp_path, monkeypatch):
    _, client, _, apply_job_id = make_apply_job(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity: [])
    monkeypatch.setattr(main, "sellable_remote_product_ids", lambda session, inv: set())
    response = client.post(
        f"/pricing/full-competitor-apply/{apply_job_id}/revert",
        data={"product_ids": ["p-1"]},
    )
    assert response.status_code == 200
    assert "None of the selected items are still valid to revert" in response.text


def _revert(client, monkeypatch, apply_job_id):
    written = {}

    def revert_writer(updates):
        written["updates"] = updates
        return [{"inventory": [
            {"product_id": u["product_id"], "price_cents": u["price_cents"],
             "product": {"single": {"name": "Alpha"}}}
            for u in updates
        ], "skipped": []}]

    monkeypatch.setattr(main, "update_inventory_prices_by_product", revert_writer)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity: [])
    monkeypatch.setattr(main, "sellable_remote_product_ids", lambda session, inv: {"p-1"})
    response = client.post(
        f"/pricing/full-competitor-apply/{apply_job_id}/revert",
        data={"product_ids": ["p-1"]}, follow_redirects=False,
    )
    return response, written.get("updates")


# -- Job-JSON retention: revert must not depend on the source preview ------

def test_apply_job_records_previous_prices_alongside_updates(tmp_path, monkeypatch):
    """The apply result carries its own product_id -> pre-apply price map,
    as a sibling of updates[] (never inside those items -- that list is
    the exact request body sent to Mana Pool)."""
    db, _, _, apply_job_id = make_apply_job(tmp_path, monkeypatch)
    with Session(db) as session:
        result = json.loads(session.get(PricingJob, apply_job_id).response_json)
    assert result["previous_prices"] == {"p-1": 100}
    assert "previous_price" not in result["updates"][0]


def test_revert_uses_the_apply_jobs_own_previous_prices_even_without_the_source_preview(tmp_path, monkeypatch):
    db, client, preview_job_id, apply_job_id = make_apply_job(tmp_path, monkeypatch)
    with Session(db) as session:
        session.delete(session.get(PricingJob, preview_job_id))
        session.commit()

    response, updates = _revert(client, monkeypatch, apply_job_id)
    assert response.status_code == 303
    assert updates == [
        {"product_type": "mtg_single", "product_id": "p-1", "price_cents": 100, "quantity": None},
    ]


def test_revert_of_a_legacy_apply_job_falls_back_to_the_source_previews_rows(tmp_path, monkeypatch):
    """Apply jobs recorded before previous_prices existed have no map of
    their own -- the source preview's changes[].current_price still works."""
    db, client, _, apply_job_id = make_apply_job(tmp_path, monkeypatch)
    with Session(db) as session:
        apply_job = session.get(PricingJob, apply_job_id)
        stored = json.loads(apply_job.response_json)
        del stored["previous_prices"]
        apply_job.response_json = json.dumps(stored)
        session.commit()

    response, updates = _revert(client, monkeypatch, apply_job_id)
    assert response.status_code == 303
    assert updates[0]["price_cents"] == 100


def test_revert_of_a_legacy_apply_job_falls_back_to_a_trimmed_previews_prior_prices(tmp_path, monkeypatch):
    """Once the source preview has been trimmed to its compact summary
    (row arrays gone), the summary's prior_prices map is what keeps an
    old apply revertible."""
    db, client, preview_job_id, apply_job_id = make_apply_job(tmp_path, monkeypatch)
    with Session(db) as session:
        apply_job = session.get(PricingJob, apply_job_id)
        stored = json.loads(apply_job.response_json)
        del stored["previous_prices"]
        apply_job.response_json = json.dumps(stored)
        preview_job = session.get(PricingJob, preview_job_id)
        preview_job.response_json = json.dumps({
            "_trimmed_at": "2026-09-30T03:15:00+00:00", "_retention_days": 14,
            "preview": {"summary": {"decreases": 1}},
            "prior_prices": {"p-1": 100},
        })
        session.commit()

    response, updates = _revert(client, monkeypatch, apply_job_id)
    assert response.status_code == 303
    assert updates[0]["price_cents"] == 100

    detail = client.get(f"/pricing/full-competitor-apply/{apply_job_id}")
    assert detail.status_code == 200
    assert "$1.00" in detail.text


def test_revert_excludes_item_whose_prior_price_is_unknown_everywhere(tmp_path, monkeypatch):
    db, client, preview_job_id, apply_job_id = make_apply_job(tmp_path, monkeypatch)
    with Session(db) as session:
        apply_job = session.get(PricingJob, apply_job_id)
        stored = json.loads(apply_job.response_json)
        del stored["previous_prices"]
        apply_job.response_json = json.dumps(stored)
        session.delete(session.get(PricingJob, preview_job_id))
        session.commit()

    response, updates = _revert(client, monkeypatch, apply_job_id)
    # Nothing revertible -> refused with the service's own reason, no write.
    assert response.status_code == 200
    assert updates is None
    assert "no prior price is known" in response.text


def test_revert_route_404_for_missing_apply_job(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/pricing/full-competitor-apply/999/revert", data={"product_ids": ["p-1"]},
    )
    assert response.status_code == 404
