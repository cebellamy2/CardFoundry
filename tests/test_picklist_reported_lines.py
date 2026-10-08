"""A reported card must stay visible on the pick list.

WHY. mark_fulfillment_exception moves the allocation straight to
"exception", and get_wave_picklist filtered on ("allocated", "picked") --
so the moment the operator reported a card, the line vanished from the
list he was working from. His own words: on a reopened pick list he must
SEE everything, including which cards were reported.

READ-ONLY, DELIBERATELY. The line is not an instruction to go and get
anything, so it is muted and carries its state instead of the Report
form. ACTING on a reported line is a separate question -- once an
exception has been reported, auto-resolve-on-submission closes its
inventory record and both revert-mark and substitution refuse, so there is
currently no reachable path at all. Nothing here invents one.

NOT HIDDEN, though. A line that vanished is what sent him looking for the
card in the first place, so these rows print on the Master Pick List too:
on paper they are the record of why a line is absent.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
from fulfillment_exception_service import mark_fulfillment_exception
from models import Base, PickWave, PickWaveOrder
from pick_wave_service import (
    PICKLIST_ALLOCATION_STATUSES,
    get_wave_picklist,
    mark_wave_picked,
)
from tests.test_fulfillment_exception_service import seed
from tests.test_pick_wave_detail_item15_redesign import (
    add_order_with_card,
    make_wave,
    setup_db,
)

import pytest


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reported.db'}")
    Base.metadata.create_all(engine)
    return engine


def _wave_with_a_reported_card(session):
    order, item, card, allocation = seed(session)
    wave = PickWave(label="wave", status="active")
    session.add(wave)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id))
    session.commit()
    exception = mark_fulfillment_exception(session, allocation.id, "missing")
    session.commit()
    return wave, order, allocation, exception


# --- the service ---------------------------------------------------------

def test_exception_is_one_of_the_picklist_allocation_statuses():
    assert PICKLIST_ALLOCATION_STATUSES == (
        "allocated", "picked", "exception", "packed",
    )


def test_a_reported_line_stays_on_the_picklist(db):
    with Session(db) as session:
        wave, _, allocation, _ = _wave_with_a_reported_card(session)
        grouped = get_wave_picklist(session, wave.id)
        entries = [e for entries in grouped.values() for e in entries]
        assert [e["allocation"].id for e in entries] == [allocation.id]
        assert entries[0]["allocation"].status == "exception"


def test_a_reported_line_survives_the_picked_transition_too(db):
    with Session(db) as session:
        wave, _, _, _ = _wave_with_a_reported_card(session)
        mark_wave_picked(session, wave)
        session.commit()
        grouped = get_wave_picklist(session, wave.id)
        assert [e["allocation"].status
                for entries in grouped.values() for e in entries] == ["exception"]


def test_reading_the_picklist_does_not_mutate_the_exception(db):
    with Session(db) as session:
        wave, _, allocation, exception = _wave_with_a_reported_card(session)
        before = (
            allocation.status, exception.submission_state,
            exception.inventory_resolution_state, exception.remote_resolution_state,
        )
        get_wave_picklist(session, wave.id)
        assert (
            allocation.status, exception.submission_state,
            exception.inventory_resolution_state, exception.remote_resolution_state,
        ) == before


# --- the page ------------------------------------------------------------

def test_a_reported_row_is_muted_and_offers_no_report_form(tmp_path, monkeypatch):
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session, wave_status="active")
        add_order_with_card(
            session, wave, batch_code="A1",
            allocation_status="exception", with_exception=True,
        )
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "Lightning Bolt" in html                    # still visible
    assert 'class="pick-row-inactive"' in html          # and muted
    assert "Report Fulfillment Exception" not in html  # read-only


def test_a_reported_row_says_what_happened_in_plain_words(tmp_path, monkeypatch):
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session, wave_status="active")
        add_order_with_card(
            session, wave, batch_code="A1",
            allocation_status="exception", with_exception=True,
        )
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "Reported missing" in html
    assert "not reported to Mana Pool yet" in html
    # The stored vocabulary means nothing to someone holding a box.
    assert "needs_submission" not in html


def test_a_submitted_exception_shows_what_mana_pool_said(tmp_path, monkeypatch):
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session, wave_status="active")
        _, allocation = add_order_with_card(
            session, wave, batch_code="A1",
            allocation_status="exception", with_exception=True,
        )
        from models import FulfillmentException
        exception = session.query(FulfillmentException).one()
        exception.submission_state = "submitted"
        exception.remote_resolution_state = "resolved_replaced"
        session.commit()
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "reported to Mana Pool" in html
    assert "Mana Pool replaced it" in html


def test_a_reported_line_does_not_count_as_picked_in_batch_progress(
    tmp_path, monkeypatch,
):
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session, wave_status="active")
        add_order_with_card(session, wave, batch_code="A1",
                            allocation_status="picked")
        add_order_with_card(session, wave, batch_code="A1",
                            allocation_status="exception", with_exception=True)
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "2 card(s), 1/2 picked" in html


def test_an_ordinary_line_still_offers_the_report_form(tmp_path, monkeypatch):
    """The guard is scoped to reported lines -- a pickable line is
    unchanged."""
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session, wave_status="active")
        add_order_with_card(session, wave, batch_code="A1")
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "Report Fulfillment Exception" in html
    assert 'class="pick-row-inactive"' not in html


# --- v2.26.0: a packed line stays visible too, and is not an instruction --

def test_a_packed_line_stays_on_the_picklist_muted_and_unprinted(
    tmp_path, monkeypatch,
):
    """Q5: on a wave sent Back to Picking, packed orders' lines stay on the
    pick list, greyed, labelled "Packed", read-only, and excluded from the
    Master Pick List print."""
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session, wave_status="active")
        add_order_with_card(session, wave, batch_code="A1",
                            allocation_status="packed")
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "Lightning Bolt" in html                       # still visible
    assert 'badge badge-info">Packed<' in html            # labelled
    assert "pick-row-inactive no-print" in html           # muted + unprinted
    assert "Report Fulfillment Exception" not in html     # read-only


def test_a_packed_line_counts_as_picked_in_batch_progress(tmp_path, monkeypatch):
    """A packed card was definitely picked -- counting it as unpicked would
    make a reopened wave look like it had lost work."""
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session, wave_status="active")
        add_order_with_card(session, wave, batch_code="A1",
                            allocation_status="packed")
        add_order_with_card(session, wave, batch_code="A1",
                            allocation_status="allocated")
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "2 card(s), 1/2 picked" in html


def test_a_reported_line_still_prints(tmp_path, monkeypatch):
    """Reported lines are muted but NOT no-print: on paper they are the
    record of why a line is absent. Only packed lines leave the sheet."""
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session, wave_status="active")
        add_order_with_card(session, wave, batch_code="A1",
                            allocation_status="exception", with_exception=True)
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert 'class="pick-row-inactive"' in html
    assert "pick-row-inactive no-print" not in html
