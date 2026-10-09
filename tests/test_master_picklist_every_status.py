"""The Master Pick List must be viewable for EVERY wave status.

★ THE CAUSE, found rather than assumed (2026-10-09).
order_service.mark_shipped sets allocation.status = "shipped", and
"shipped" was absent from pick_wave_service.PICKLIST_ALLOCATION_STATUSES --
so the moment a wave shipped, every one of its lines dropped out of
get_wave_picklist and the Master Pick List went blank. The operator's
words: "for picklists that have been shipped, it removes the picklist
entries from the master list. I want to make sure that the master picklist
is always viewable no matter the status of the pickwave."

A SECOND cause for cancelled waves: get_wave_picklist returned {} outright
for a cancelled wave (slice 1's own decision, reasoning that a list would
invite picking against an abandoned wave). Superseded -- the list is the
wave's RECORD, and picking is prevented by the page, where every action is
gated on the wave being active, not by hiding the evidence.

★ "released" IS STILL ABSENT, deliberately. release_order sets it when an
ORDER is cancelled and its cards go back to stock, so the line is no longer
any part of this wave's work -- and uncancel_order restores the allocation
to its recorded released_from_status, at which point it reappears on its
own.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
from models import Base, PickAllocation, PickWave, PickWaveOrder
from pick_wave_service import PICKLIST_ALLOCATION_STATUSES, get_wave_picklist
from tests.test_fulfillment_exception_service import seed
from tests.test_pick_wave_detail_item15_redesign import (
    add_order_with_card,
    make_wave,
    setup_db,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'master.db'}")
    Base.metadata.create_all(engine)
    return engine


def wave_at(session, *, wave_status, allocation_status, label="wave"):
    order, item, card, allocation = seed(session)
    allocation.status = allocation_status
    wave = PickWave(label=label, status=wave_status)
    session.add(wave)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id))
    session.commit()
    return wave, order, allocation


def line_count(grouped):
    return sum(len(entries) for entries in grouped.values())


# --- the cause, pinned ---------------------------------------------------

def test_shipped_is_one_of_the_picklist_allocation_statuses():
    """The one-line cause of the disappearing list."""
    assert "shipped" in PICKLIST_ALLOCATION_STATUSES
    assert PICKLIST_ALLOCATION_STATUSES == (
        "allocated", "picked", "exception", "packed", "shipped",
    )


def test_a_released_allocation_is_still_excluded():
    """An order cancelled back to stock is not a line of this wave's work,
    and uncancel_order restores it to its recorded prior status anyway."""
    assert "released" not in PICKLIST_ALLOCATION_STATUSES


# --- every wave status has a list ----------------------------------------

@pytest.mark.parametrize("wave_status,allocation_status", [
    ("active", "allocated"),
    ("picked", "picked"),
    ("packed", "packed"),
    ("shipped", "shipped"),
    ("cancelled", "allocated"),
    ("completed", "picked"),      # the legacy stored status
])
def test_the_picklist_is_non_empty_for_every_wave_status(
    db, wave_status, allocation_status,
):
    with Session(db) as session:
        wave, _, _ = wave_at(
            session, wave_status=wave_status,
            allocation_status=allocation_status,
        )
        assert line_count(get_wave_picklist(session, wave.id)) == 1, wave_status


def test_a_missing_wave_is_still_empty_rather_than_raising(db):
    with Session(db) as session:
        assert get_wave_picklist(session, 999999) == {}


# --- the slice-1 guard still holds ---------------------------------------

def test_a_cancelled_waves_line_is_withheld_once_another_wave_claims_it(db):
    """★ THE ANSWER TO "how do a cancelled wave's lines look given that
    guard". Cancel leaves allocations alone and frees the ORDER, so the
    order can join a new wave -- and then the line belongs to whichever
    wave is actually picking it. The cancelled wave's own row stays visible
    in its Orders section either way; only the pickable LINE moves."""
    with Session(db) as session:
        wave, order, _ = wave_at(
            session, wave_status="cancelled", allocation_status="allocated",
            label="cancelled",
        )
        assert line_count(get_wave_picklist(session, wave.id)) == 1

        # Cancel closed the membership, so a new wave may claim the order.
        membership = session.query(PickWaveOrder).filter(
            PickWaveOrder.wave_id == wave.id,
        ).one()
        membership.status = "closed"
        live = PickWave(label="live", status="active")
        session.add(live)
        session.flush()
        session.add(PickWaveOrder(wave_id=live.id, order_id=order.id))
        session.commit()

        assert get_wave_picklist(session, wave.id) == {}
        assert line_count(get_wave_picklist(session, live.id)) == 1


def test_a_removed_order_still_leaves_the_list(db):
    with Session(db) as session:
        wave, order, _ = wave_at(
            session, wave_status="shipped", allocation_status="shipped",
        )
        membership = session.query(PickWaveOrder).filter(
            PickWaveOrder.wave_id == wave.id,
        ).one()
        membership.status = "removed"
        session.commit()
        assert get_wave_picklist(session, wave.id) == {}


# --- the page: labelled, read-only, printable ----------------------------

def _page(tmp_path, monkeypatch, *, wave_status, allocation_status):
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session, wave_status=wave_status)
        add_order_with_card(
            session, wave, batch_code="A1",
            allocation_status=allocation_status,
            membership_status="closed" if wave_status in (
                "cancelled", "shipped",
            ) else "active",
        )
        return engine, wave.id


@pytest.mark.parametrize("wave_status,allocation_status,expected", [
    ("picked", "picked", "This wave has been picked"),
    ("packed", "packed", "This wave has been packed"),
    ("shipped", "shipped", "This wave has SHIPPED"),
    ("cancelled", "allocated", "This wave was CANCELLED"),
    ("completed", "picked", "This wave has been picked"),
])
def test_the_sheet_says_which_wave_status_it_is(
    tmp_path, monkeypatch, wave_status, allocation_status, expected,
):
    """A printed pick list with no status on it is exactly the thing
    somebody picks from by mistake, so the note is NOT no-print."""
    _, wave_id = _page(tmp_path, monkeypatch, wave_status=wave_status,
                       allocation_status=allocation_status)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert expected in html
    assert "Lightning Bolt" in html
    # the status note prints with the sheet
    assert f'<p class="muted"><strong>{expected}' in html


def test_an_active_wave_has_no_status_note(tmp_path, monkeypatch):
    """It is a worksheet, not a record -- nothing to disclaim."""
    _, wave_id = _page(tmp_path, monkeypatch, wave_status="active",
                       allocation_status="allocated")
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "Read-only record" not in html


def test_a_shipped_line_is_labelled_muted_and_printed(tmp_path, monkeypatch):
    """Muted and read-only like a packed line, but it PRINTS: on a shipped
    wave the sheet IS the record of what went out."""
    _, wave_id = _page(tmp_path, monkeypatch, wave_status="shipped",
                       allocation_status="shipped")
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert 'badge badge-success">Shipped<' in html
    assert 'class="pick-row-inactive"' in html
    assert "pick-row-inactive no-print" not in html
    assert "Report Fulfillment Exception" not in html


def test_a_packed_line_is_still_the_one_kept_off_the_sheet(tmp_path, monkeypatch):
    """Unchanged from v2.26.0: a wave sent Back to Picking must not reprint
    work already boxed."""
    _, wave_id = _page(tmp_path, monkeypatch, wave_status="active",
                       allocation_status="packed")
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "pick-row-inactive no-print" in html


def test_a_shipped_line_counts_as_picked_in_batch_progress(tmp_path, monkeypatch):
    _, wave_id = _page(tmp_path, monkeypatch, wave_status="shipped",
                       allocation_status="shipped")
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "1 card(s), 1/1 picked" in html


def test_a_reported_line_keeps_its_slice_5_wording_on_a_shipped_wave(
    tmp_path, monkeypatch,
):
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session, wave_status="shipped")
        add_order_with_card(
            session, wave, batch_code="A1", allocation_status="exception",
            membership_status="closed", with_exception=True,
        )
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "Reported missing" in html
    assert "not reported to Mana Pool yet" in html
    # read-only: no found/substitute actions on a terminal wave
    assert "I found this card" not in html
