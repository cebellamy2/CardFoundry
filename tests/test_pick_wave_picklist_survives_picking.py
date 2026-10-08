"""The wave's pick list must survive "Complete".

WHY. get_wave_picklist filtered PickWaveOrder.status == "active", and
complete_pick_wave closes every membership (_close_active_memberships), so
the pick list -- and with it the Master Pick List print -- went blank the
moment the operator pressed Complete. He packs from that list. The orders
section and the exception table never had the problem; they read
get_wave_orders(active_only=False).

★ THE EDGE CASE THESE TESTS EXIST FOR. remove_order_from_wave hands an
order straight back to ready_to_pick and LEAVES ITS ALLOCATIONS ALONE, so
that order can join another wave while its allocations still read
"allocated". Keyed on the wave alone, its lines would show on both waves'
pick lists at once -- which is how one physical card gets picked twice.
Two independent guards, because they fail differently: the membership
filter excludes the ordinary removal, and the active-elsewhere exclusion
catches any other route into the same shape.

Membership semantics are deliberately NOT changed here (whether a picked
order may be re-waved is an open operator question) -- this slice only
changes what is READ.
"""
import logging
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
from models import Base, PickAllocation, PickWave, PickWaveOrder
from pick_wave_service import (
    cancel_pick_wave,
    complete_pick_wave,
    get_wave_picklist,
    remove_order_from_wave,
)
from tests.test_fulfillment_exception_service import seed
from tests.test_pick_wave_detail_item15_redesign import (
    add_order_with_card,
    make_wave,
    setup_db,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'picklist.db'}")
    Base.metadata.create_all(engine)
    return engine


@contextmanager
def _capturing(caplog, level=logging.WARNING):
    """★ caplog ALONE CANNOT SEE THIS LOGGER -- main.py sets
    cardfoundry.propagate = False, so an assertion on caplog.text would
    pass vacuously whatever the code did."""
    cardfoundry = logging.getLogger("cardfoundry")
    caplog.set_level(level, logger="cardfoundry")
    cardfoundry.addHandler(caplog.handler)
    try:
        yield
    finally:
        cardfoundry.removeHandler(caplog.handler)


def wave_with_orders(session, *, count=2, label="wave"):
    wave = PickWave(label=label, status="active")
    session.add(wave)
    session.flush()
    orders = []
    for _ in range(count):
        order, _, _, _ = seed(session)
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id))
        orders.append(order)
    session.commit()
    return wave, orders


def line_count(grouped):
    return sum(len(entries) for entries in grouped.values())


# --------------------------------------------------------------------------
# The regression itself.
# --------------------------------------------------------------------------

def test_the_picklist_survives_completion(db):
    with Session(db) as session:
        wave, orders = wave_with_orders(session, count=2)
        before = get_wave_picklist(session, wave.id)
        assert line_count(before) == 2

        complete_pick_wave(session, wave)
        session.commit()

        assert wave.status == "completed"
        # Membership semantics are untouched: this slice changed the read,
        # not the write.
        assert {m.status for m in session.query(PickWaveOrder).all()} == {"closed"}

        after = get_wave_picklist(session, wave.id)
        assert line_count(after) == 2
        assert set(after) == set(before)


def test_a_completed_waves_lines_show_their_picked_allocation_status(db):
    with Session(db) as session:
        wave, _ = wave_with_orders(session, count=1)
        complete_pick_wave(session, wave)
        session.commit()
        entries = [e for entries in get_wave_picklist(session, wave.id).values()
                   for e in entries]
        assert [e["allocation"].status for e in entries] == ["picked"]


def test_the_picklist_is_unchanged_while_the_wave_is_active(db):
    with Session(db) as session:
        wave, _ = wave_with_orders(session, count=3)
        grouped = get_wave_picklist(session, wave.id)
        assert line_count(grouped) == 3
        assert all(
            entry["allocation"].status == "allocated"
            for entries in grouped.values() for entry in entries
        )


def test_a_cancelled_wave_still_has_no_picklist(db):
    """Cancellation routes every order back to ready_to_pick, so a list
    here would invite picking against an abandoned wave. Membership is
    left in the identical "closed" state by both cancel and complete, so
    this has to key on the wave's own status."""
    with Session(db) as session:
        wave, _ = wave_with_orders(session, count=2)
        cancel_pick_wave(session, wave)
        session.commit()
        assert wave.status == "cancelled"
        assert {m.status for m in session.query(PickWaveOrder).all()} == {"closed"}
        assert get_wave_picklist(session, wave.id) == {}


def test_a_missing_wave_returns_an_empty_picklist_rather_than_raising(db):
    with Session(db) as session:
        assert get_wave_picklist(session, 999999) == {}


# --------------------------------------------------------------------------
# ★ THE EDGE CASE: never pickable on two waves at once.
# --------------------------------------------------------------------------

def test_an_order_removed_from_the_wave_leaves_its_picklist(db):
    """remove_order_from_wave marks the membership "removed" and leaves the
    allocations alone, so the order is genuinely somebody else's to pick."""
    with Session(db) as session:
        wave, orders = wave_with_orders(session, count=2)
        remove_order_from_wave(session, wave, orders[0])
        session.commit()

        grouped = get_wave_picklist(session, wave.id)
        assert line_count(grouped) == 1
        remaining = [e for entries in grouped.values() for e in entries]
        assert remaining[0]["order"].id == orders[1].id
        # Untouched, which is exactly why the line must not show here.
        allocations = session.query(PickAllocation).all()
        assert {a.status for a in allocations} == {"allocated"}


def test_a_line_actively_picked_on_another_wave_is_withheld(db):
    with Session(db) as session:
        first, orders = wave_with_orders(session, count=2, label="first")
        complete_pick_wave(session, first)
        session.commit()
        assert line_count(get_wave_picklist(session, first.id)) == 2

        second = PickWave(label="second", status="active")
        session.add(second)
        session.flush()
        session.add(PickWaveOrder(wave_id=second.id, order_id=orders[0].id))
        session.commit()

        # Withheld from the old wave...
        old = get_wave_picklist(session, first.id)
        assert line_count(old) == 1
        assert [e["order"].id for entries in old.values() for e in entries] == [
            orders[1].id,
        ]
        # ...and present on exactly one wave, the live one.
        new = get_wave_picklist(session, second.id)
        assert [e["order"].id for entries in new.values() for e in entries] == [
            orders[0].id,
        ]


def test_a_closed_membership_elsewhere_does_not_withhold_anything(db):
    """Only an ACTIVE membership on another wave means someone else is
    picking it. A historical closed one must not hide the line forever."""
    with Session(db) as session:
        first, orders = wave_with_orders(session, count=1, label="first")
        complete_pick_wave(session, first)
        session.commit()
        second = PickWave(label="second", status="completed")
        session.add(second)
        session.flush()
        session.add(PickWaveOrder(
            wave_id=second.id, order_id=orders[0].id, status="closed",
        ))
        session.commit()
        assert line_count(get_wave_picklist(session, first.id)) == 1


def test_withheld_lines_are_logged_rather_than_silently_dropped(db, caplog):
    with Session(db) as session:
        first, orders = wave_with_orders(session, count=1, label="first")
        complete_pick_wave(session, first)
        session.commit()
        second = PickWave(label="second", status="active")
        session.add(second)
        session.flush()
        session.add(PickWaveOrder(wave_id=second.id, order_id=orders[0].id))
        session.commit()

        with _capturing(caplog):
            assert get_wave_picklist(session, first.id) == {}
    assert "withheld 1 pick-list line(s)" in caplog.text
    assert "picked twice" in caplog.text


# --------------------------------------------------------------------------
# The page: what the operator actually sees.
# --------------------------------------------------------------------------

def test_master_pick_list_print_is_offered_on_a_completed_wave(tmp_path, monkeypatch):
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session, wave_status="completed")
        add_order_with_card(
            session, wave, batch_code="A1",
            allocation_status="picked", membership_status="closed",
        )
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.status_code == 200
    assert 'onclick="window.print()"' in response.text
    assert "nothing to print" not in response.text
    # The pick list itself is there to print.
    assert "Lightning Bolt" in response.text


def test_master_pick_list_print_is_withheld_when_there_is_nothing_to_print(
    tmp_path, monkeypatch,
):
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session, wave_status="cancelled")
        add_order_with_card(
            session, wave, batch_code="A1", membership_status="closed",
        )
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.status_code == 200
    assert 'onclick="window.print()"' not in response.text
    assert "nothing to print" in response.text


def test_report_exception_is_offered_while_the_wave_is_active(tmp_path, monkeypatch):
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session, wave_status="active")
        add_order_with_card(session, wave, batch_code="A1")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert "Report Fulfillment Exception" in response.text


def test_report_exception_is_not_offered_once_the_wave_is_completed(
    tmp_path, monkeypatch,
):
    """The route behind that form refuses on a non-active wave, so now the
    pick list survives completion the button must not."""
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session, wave_status="completed")
        add_order_with_card(
            session, wave, batch_code="A1",
            allocation_status="picked", membership_status="closed",
        )
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert "Lightning Bolt" in response.text          # the line is visible
    assert "Report Fulfillment Exception" not in response.text
