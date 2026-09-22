"""The picking flow returns the operator to where they were standing.

2026-09-22. Reporting an exception reloaded the pick wave at the top, so
on a long pick list the operator lost their place every time. Two of the
four actions in this flow were worse: "Submitted to ManaPool" and "Undo
Exception Mark" redirect to the ORDER page, throwing the operator off the
wave entirely mid-pick.

Browser-native fragment anchoring only -- no JavaScript, per the standing
rule.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, FulfillmentException, PickWave, PickWaveOrder
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'wave_anchor.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def wave_with_allocation(session):
    order, item, card, allocation = seed(session)
    wave = PickWave(label="W1", status="active")
    session.add(wave)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
    session.commit()
    return wave, order, allocation


# --- the helper itself ---------------------------------------------------

def test_the_return_url_is_built_from_ints_not_trusted_as_a_string():
    """A tampered field can only ever produce a different pick-wave URL --
    never an open redirect, which is why this does not allowlist a string."""
    assert main._pick_wave_return_url(7, 12) == "/pick-waves/7#exception-12"
    assert main._pick_wave_return_url("7", "12") == "/pick-waves/7#exception-12"
    # no exception id -> the section, still on the wave
    assert main._pick_wave_return_url(7) == "/pick-waves/7#fulfillment-exceptions"
    assert main._pick_wave_return_url(7, "nope") == "/pick-waves/7#fulfillment-exceptions"
    # unusable wave id -> empty, and callers fall back to their old target
    for bad in ("", None, "evil.com", "//evil.com", "3; DROP TABLE"):
        assert main._pick_wave_return_url(bad, 1) == ""


def test_the_hidden_field_is_absent_when_there_is_no_wave():
    """Which is how the order page keeps its existing behaviour."""
    assert main._pick_wave_return_field(None) == ""
    assert main._pick_wave_return_field("") == ""
    assert 'value="4"' in main._pick_wave_return_field(4)


# --- the four actions ----------------------------------------------------

def test_reporting_an_exception_returns_to_the_new_exception_row(db):
    with Session(db) as session:
        wave, _, allocation = wave_with_allocation(session)
        wave_id, allocation_id = wave.id, allocation.id

    response = TestClient(main.app).post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(db) as session:
        exception_id = session.query(FulfillmentException).one().id
    assert response.headers["location"] == (
        f"/pick-waves/{wave_id}#exception-{exception_id}"
    )


def test_the_anchor_target_actually_exists_on_the_reloaded_page(db):
    """An anchor pointing at nothing is the same as no anchor at all --
    which is exactly why the pick-list row is the wrong target: it is
    filtered out the moment the exception is recorded."""
    with Session(db) as session:
        wave, _, allocation = wave_with_allocation(session)
        wave_id, allocation_id = wave.id, allocation.id

    client = TestClient(main.app)
    redirect = client.post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
        follow_redirects=False,
    )
    fragment = redirect.headers["location"].split("#", 1)[1]
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert f'id="{fragment}"' in page.text


def test_submitting_from_the_wave_returns_to_the_wave_not_the_order(db):
    with Session(db) as session:
        wave, order, allocation = wave_with_allocation(session)
        wave_id, allocation_id, order_id = wave.id, allocation.id, order.id

    client = TestClient(main.app)
    client.post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
    )
    with Session(db) as session:
        exception_id = session.query(FulfillmentException).one().id

    response = client.post(
        f"/fulfillment-exceptions/{exception_id}/submitted",
        data={"note": "reported", "return_wave_id": str(wave_id)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/pick-waves/{wave_id}#exception-{exception_id}"
    )


def test_submitting_from_the_order_page_still_returns_to_the_order(db):
    """The shared route is used from two places; only the wave sends the
    field, so the order page is untouched."""
    with Session(db) as session:
        wave, order, allocation = wave_with_allocation(session)
        wave_id, allocation_id, order_id = wave.id, allocation.id, order.id

    client = TestClient(main.app)
    client.post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
    )
    with Session(db) as session:
        exception_id = session.query(FulfillmentException).one().id

    response = client.post(
        f"/fulfillment-exceptions/{exception_id}/submitted",
        data={"note": "reported"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/orders/{order_id}"


def test_the_wave_page_carries_the_return_field_on_its_exception_forms(db):
    with Session(db) as session:
        wave, _, allocation = wave_with_allocation(session)
        wave_id, allocation_id = wave.id, allocation.id

    client = TestClient(main.app)
    client.post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
    )
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert 'name="return_wave_id"' in page.text
    assert f'value="{wave_id}"' in page.text


def test_undo_from_the_wave_leads_back_to_the_wave(db):
    """This one returns an interstitial rather than a redirect, so the
    wave has to reach the BACK LINK."""
    with Session(db) as session:
        wave, _, allocation = wave_with_allocation(session)
        wave_id, allocation_id = wave.id, allocation.id

    client = TestClient(main.app)
    client.post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
    )
    with Session(db) as session:
        exception_id = session.query(FulfillmentException).one().id

    response = client.post(
        f"/fulfillment-exceptions/{exception_id}/revert-mark",
        data={"note": "mistake", "return_wave_id": str(wave_id)},
    )
    assert response.status_code == 200
    assert f"/pick-waves/{wave_id}#exception-{exception_id}" in response.text
    assert "Back to pick wave" in response.text


def test_undo_from_the_order_page_still_says_back_to_order(db):
    with Session(db) as session:
        wave, _, allocation = wave_with_allocation(session)
        wave_id, allocation_id = wave.id, allocation.id

    client = TestClient(main.app)
    client.post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
    )
    with Session(db) as session:
        exception_id = session.query(FulfillmentException).one().id

    response = client.post(
        f"/fulfillment-exceptions/{exception_id}/revert-mark",
        data={"note": "mistake"},
    )
    assert response.status_code == 200
    assert "Back to order" in response.text
    assert "#exception-" not in response.text


def test_no_javascript_was_used_to_do_any_of_this(db):
    """Standing rule. Fragment anchoring is browser-native."""
    with Session(db) as session:
        wave, _, allocation = wave_with_allocation(session)
        wave_id, allocation_id = wave.id, allocation.id

    client = TestClient(main.app)
    client.post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
    )
    page = client.get(f"/pick-waves/{wave_id}").text
    assert "scrollIntoView" not in page
    assert "window.scroll" not in page
