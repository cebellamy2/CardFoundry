"""The manual surface for whole-catalogue market pricing.

Preview is read-only and apply is typed-confirmation, matching the
competitor-apply route next door: this moves every listing's price in one
call and there is no undo.
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import bulk_pricing_service
import inventory_sync_service
import main
from models import Base, PricingJob

JOB = {"id": "job-1", "is_preview": True, "total_items": 3,
       "successful_items": 2, "skipped_items": 1, "failed_items": 0}
ROWS = [
    {"Item": "Thunder Dragon", "Set Code": "DDG", "Collector Number": "61",
     "Condition": "LP", "Finish": "NF", "Current": "$0.65", "Low": "$0.15",
     "New": "$0.15", "Status": "success", "Anomaly Type": "normal"},
    {"Item": "Sheoldred", "Set Code": "DMU", "Collector Number": "435",
     "Condition": "LP", "Finish": "NF", "Current": "$158.60", "Low": "$129.98",
     "New": "$129.93", "Status": "success", "Anomaly Type": "underpriced"},
]


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'bulkroutes.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


@pytest.fixture
def stub(monkeypatch):
    calls = {"starts": []}

    def start(*, is_preview):
        calls["starts"].append(is_preview)
        return "job-1"

    monkeypatch.setattr(bulk_pricing_service, "start_job", start)
    monkeypatch.setattr(bulk_pricing_service, "wait_for_job",
                        lambda job_id, **kw: dict(JOB, is_preview=calls["starts"][-1]))
    monkeypatch.setattr(bulk_pricing_service, "fetch_export", lambda job_id: ROWS)
    return calls


def test_preview_runs_a_preview_and_writes_nothing(db, stub):
    response = TestClient(main.app).post("/pricing/bulk-market-price/preview")
    assert response.status_code == 200
    assert stub["starts"] == [True], "preview must never start a real job"
    assert "Bulk Market Price Preview" in response.text


def test_preview_records_the_job_for_audit(db, stub):
    TestClient(main.app).post("/pricing/bulk-market-price/preview")
    with Session(db) as session:
        job = session.query(PricingJob).one()
        assert job.action == "bulk_market_price_preview"
        assert job.external_job_id == "job-1"
        stored = json.loads(job.response_json)
        assert stored["summary"]["successful_items"] == 2
        assert len(stored["rows"]) == 2


def test_preview_states_the_settings_in_plain_words(db, stub):
    text = TestClient(main.app).post("/pricing/bulk-market-price/preview").text
    assert "Low listed price minus 5" in text
    assert "letter-shipping-disabled sellers" in text
    assert "no competing listing skipped" in text


def test_preview_says_how_many_land_below_the_store_minimum(db, stub):
    """Reported, not clamped -- and the page has to explain that buyers
    still see $0.65 or the number reads as a disaster."""
    text = TestClient(main.app).post("/pricing/bulk-market-price/preview").text
    assert "below the $0.65 store minimum" in text
    assert "still shows buyers $0.65" in text


# --- apply is guarded ----------------------------------------------------

def test_apply_refuses_without_the_typed_confirmation(db, stub):
    response = TestClient(main.app).post("/pricing/bulk-market-price/apply",
                                         data={"confirmation": "yes"})
    assert response.status_code == 400
    assert stub["starts"] == [], "nothing may run without confirmation"
    assert "Nothing was changed" in response.text


def test_apply_refuses_an_empty_confirmation(db, stub):
    response = TestClient(main.app).post("/pricing/bulk-market-price/apply")
    assert response.status_code == 400
    assert stub["starts"] == []


def test_apply_runs_a_real_job(db, stub):
    response = TestClient(main.app).post(
        "/pricing/bulk-market-price/apply",
        data={"confirmation": main.BULK_PRICE_APPLY_CONFIRMATION},
    )
    assert response.status_code == 200
    assert stub["starts"] == [False], "apply must start a non-preview job"

    with Session(db) as session:
        job = session.query(PricingJob).one()
        assert job.action == "bulk_market_price_apply"
        stored = json.loads(job.response_json)
        assert stored["summary"]["successful_items"] == 2
        assert len(stored["rows"]) == 2


def test_apply_never_touches_manual_price_overrides(db, stub, monkeypatch):
    """Operator decision 2026-09-16: nothing is pinned. ManualPriceOverride
    keeps its one real job -- a NEW listing's starting tier -- and the apply
    path must not read it, write it, or push its prices back."""
    touched = []
    monkeypatch.setattr(
        main, "_active_manual_price_overrides",
        lambda session: touched.append("read") or [],
    )
    response = TestClient(main.app).post(
        "/pricing/bulk-market-price/apply",
        data={"confirmation": main.BULK_PRICE_APPLY_CONFIRMATION},
    )
    assert response.status_code == 200
    assert touched == []
    # No counter that always reads 0, and no claim that anything was pinned.
    assert "price override" not in response.text.lower()
    assert "re-assert" not in response.text.lower()


def test_a_run_whose_export_is_lost_is_recorded_as_applied_not_failed(db, stub, monkeypatch):
    """The job completed, so Mana Pool has already written the prices.
    Filing that as a failure would send an operator looking for prices
    that did move -- and could invite a second run over the first."""
    def no_export(job_id):
        raise bulk_pricing_service.BulkPricingError("export 502")

    monkeypatch.setattr(bulk_pricing_service, "fetch_export", no_export)
    response = TestClient(main.app).post(
        "/pricing/bulk-market-price/apply",
        data={"confirmation": main.BULK_PRICE_APPLY_CONFIRMATION},
    )
    assert response.status_code == 200
    assert "Bulk Market Prices Applied" in response.text
    assert "prices are live on Mana Pool" in response.text
    assert "export 502" in response.text

    with Session(db) as session:
        job = session.query(PricingJob).one()
        assert job.status == "completed", "the prices were applied"
        stored = json.loads(job.response_json)
        assert stored["outcome"] == "applied_export_unavailable"
        assert stored["summary"]["successful_items"] == 2
        assert "rows" not in stored


def test_a_job_that_never_confirms_is_recorded_as_failed_and_says_so(db, stub, monkeypatch):
    """Different case: Mana Pool never told us the job finished, so we
    cannot claim the catalogue was priced -- but we also cannot claim
    nothing happened, and the operator has to be told both halves."""
    def never(job_id, **kw):
        raise bulk_pricing_service.BulkPricingError("job j1 did not finish within 300s")

    monkeypatch.setattr(bulk_pricing_service, "wait_for_job", never)
    response = TestClient(main.app).post(
        "/pricing/bulk-market-price/apply",
        data={"confirmation": main.BULK_PRICE_APPLY_CONFIRMATION},
    )
    assert response.status_code == 502
    assert "may already have been written" in response.text

    with Session(db) as session:
        job = session.query(PricingJob).one()
        assert job.status == "failed"
        assert json.loads(job.response_json)["outcome"] == "not_confirmed"


def test_nothing_is_recorded_when_the_job_never_started(db, monkeypatch):
    """No job, no prices, no history row to explain."""
    def boom(*, is_preview):
        raise bulk_pricing_service.BulkPricingError("Mana Pool said no")

    monkeypatch.setattr(bulk_pricing_service, "start_job", boom)
    response = TestClient(main.app).post(
        "/pricing/bulk-market-price/apply",
        data={"confirmation": main.BULK_PRICE_APPLY_CONFIRMATION},
    )
    assert response.status_code == 502
    with Session(db) as session:
        assert session.query(PricingJob).count() == 0


def test_a_failed_job_is_reported_not_swallowed(db, monkeypatch):
    def boom(*, is_preview):
        raise bulk_pricing_service.BulkPricingError("Mana Pool said no")

    monkeypatch.setattr(bulk_pricing_service, "start_job", boom)
    response = TestClient(main.app).post("/pricing/bulk-market-price/preview")
    assert response.status_code == 502
    assert "Mana Pool said no" in response.text


def test_the_biggest_movers_lead_the_table(db, stub):
    """A 6,000-row table is unreadable; the rows worth an eye are the ones
    that moved most."""
    text = TestClient(main.app).post("/pricing/bulk-market-price/preview").text
    assert text.index("Sheoldred") < text.index("Thunder Dragon")
