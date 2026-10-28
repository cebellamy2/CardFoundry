"""The pricing empty-run alarm, re-based (operator, 2026-10-28).

It used to warn when fewer than 1% of evaluated listings were priced. That
read `successful_items` as "listings priced"; it is "listings CHANGED" --
already-at-target is a skip -- so on a catalogue repriced every five hours
a LOW count is the healthy steady state. Across 46 consecutive production
runs the priced count ran 71 to 465 of ~6,800 (median 3.0%), every run
completed and none failed. The quietest was 1.021%: two hundredths of a
percentage point from a false alarm.

A completed run is now unhealthy only when it evaluated NOTHING, or when
Mana Pool itself reports failed listings. Both are facts the job states,
not inferences from a ratio whose healthy value is near zero.

★ THESE DRIVE WHAT THE OPERATOR SEES -- the /pricing page and the
Attention page -- not just the function, because the signal has three
readers and all three must move together.
"""
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import attention_service as att
import database
import inventory_sync_service
import main
from models import Base, PricingJob


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'emptyrun.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    return engine


def run(db, *, priced, total, failed, hours_ago=1.0, blob=None):
    """One completed bulk apply, in the shape the cron really stores."""
    with Session(db) as session:
        session.query(PricingJob).delete()
        session.add(PricingJob(
            action="bulk_market_price_apply", status="completed", request_json="{}",
            response_json=blob if blob is not None else json.dumps({"summary": {
                "successful_items": priced, "total_items": total,
                "failed_items": failed,
            }}),
            created_at=datetime.now() - timedelta(hours=hours_ago),
        ))
        session.commit()


def pricing_page(db):
    """The Pricing freshness section as the operator sees it, on the
    Attention page -- which is where _pricing_freshness_section renders,
    not /pricing."""
    response = TestClient(main.app).get("/orders/shipment-sync-issues")
    assert response.status_code == 200
    text = response.text
    start = text.index("Pricing freshness")
    return text[start:text.index("<h2", start + 10)]


def attention_items(db):
    with Session(db) as session:
        return [i for i in att.collect(session, drift_rows=[])
                if i.category == att.CATEGORY_PRICING_FRESHNESS]


def badge(db):
    with Session(db) as session:
        return att.badge_count(session)


# ---------------------------------------------------------------------
# (a) low-but-nonzero priced, nothing failed -> NO alarm
# ---------------------------------------------------------------------

@pytest.mark.parametrize("priced,total", [
    (71, 6954),    # the quietest of 46 real runs, 1.021%
    (1, 5975),     # the run the OLD rule was written for
    (317, 6840),   # the run that prompted the investigation, 4.63%
    (465, 6984),   # the busiest of the 46
])
def test_a_low_priced_count_raises_nothing(db, priced, total):
    run(db, priced=priced, total=total, failed=0)
    assert attention_items(db) == [], f"{priced} of {total} must not alarm"
    section = pricing_page(db)
    assert "danger" not in section and "warning" not in section
    assert f"It priced {priced} of {total} listings." in section


def test_zero_priced_with_nothing_failed_is_still_quiet(db):
    """★ The case the old rule shouted loudest about. Nothing needed
    changing, so nothing changed; staleness is what catches a cron that
    has actually stopped."""
    run(db, priced=0, total=6800, failed=0)
    assert attention_items(db) == []
    assert "danger" not in pricing_page(db)


# ---------------------------------------------------------------------
# (b) evaluated zero -> alarm
# ---------------------------------------------------------------------

def test_a_run_that_evaluated_zero_listings_alarms_on_both_surfaces(db):
    run(db, priced=0, total=0, failed=0)
    items = attention_items(db)
    assert len(items) == 1, items
    assert "evaluated zero listings" in items[0].detail
    section = pricing_page(db)
    assert "danger" in section
    assert "evaluated zero listings" in section


# ---------------------------------------------------------------------
# (c) any failed listing -> alarm
# ---------------------------------------------------------------------

def test_any_failed_listing_alarms_on_both_surfaces(db):
    run(db, priced=300, total=6800, failed=2)
    items = attention_items(db)
    assert len(items) == 1, items
    assert "2 failed listing(s)" in items[0].detail
    section = pricing_page(db)
    assert "danger" in section
    assert "2 failed listing(s)" in section
    assert "2 listing(s) failed." in section, "the page also states the count"


def test_one_failed_listing_is_enough(db):
    run(db, priced=6799, total=6800, failed=1)
    assert len(attention_items(db)) == 1


def test_the_badge_counts_the_alarm_exactly_once(db):
    run(db, priced=300, total=6800, failed=2)
    before = badge(db)
    run(db, priced=300, total=6800, failed=0)
    after = badge(db)
    assert before - after == 1, (before, after)


# ---------------------------------------------------------------------
# What must NOT be read as a failure
# ---------------------------------------------------------------------

def test_a_missing_summary_does_not_alarm(db):
    """78 of production's 274 completed applies carry no summary. "We
    cannot tell" must not render as "it evaluated nothing"."""
    run(db, priced=0, total=0, failed=0, blob="{}")
    assert attention_items(db) == []


def test_an_unreadable_blob_does_not_alarm(db):
    run(db, priced=0, total=0, failed=0, blob="not json at all")
    assert attention_items(db) == []


def test_a_chunked_list_response_is_read_not_swallowed(db):
    """Bulk Mana Pool writes return a LIST, one response per chunk."""
    run(db, priced=0, total=0, failed=0, blob=json.dumps(
        [{"summary": {"successful_items": 300, "total_items": 6800,
                      "failed_items": 4}}]))
    items = attention_items(db)
    assert len(items) == 1 and "4 failed listing(s)" in items[0].detail


def test_the_old_one_percent_rule_is_gone(db):
    assert not hasattr(att, "PRICING_EMPTY_RUN_FRACTION")
