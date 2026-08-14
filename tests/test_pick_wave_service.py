import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Base, PickWave, PickWaveOrder, SalesOrder
from pick_wave_service import (
    PickWaveSelectionError,
    cancel_pick_wave,
    create_pick_wave,
    remove_order_from_wave,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pick_wave.db'}")
    Base.metadata.create_all(engine)
    return engine


def make_order(session, *, status="ready_to_pick"):
    order = SalesOrder(
        external_order_id=f"order-{session.query(SalesOrder).count() + 1}",
        status=status,
    )
    session.add(order)
    session.flush()
    return order


def test_create_pick_wave_includes_only_selected_orders(db):
    with Session(db) as session:
        selected = make_order(session)
        other_ready = make_order(session)
        session.commit()

        wave = create_pick_wave(session, [selected.id], "Wave A")
        session.commit()

        member_ids = {
            row.order_id
            for row in session.query(PickWaveOrder).filter(PickWaveOrder.wave_id == wave.id)
        }
        assert member_ids == {selected.id}
        assert selected.status == "in_pick_wave"
        assert other_ready.status == "ready_to_pick"


def test_create_pick_wave_requires_at_least_one_order(db):
    with Session(db) as session:
        with pytest.raises(PickWaveSelectionError):
            create_pick_wave(session, [])
        assert session.query(PickWave).count() == 0


def test_create_pick_wave_fails_whole_batch_on_missing_order(db):
    with Session(db) as session:
        real_order = make_order(session)
        session.commit()
        missing_id = real_order.id + 999

        with pytest.raises(PickWaveSelectionError):
            create_pick_wave(session, [real_order.id, missing_id])

        session.rollback()
        assert session.query(PickWave).count() == 0
        session.refresh(real_order)
        assert real_order.status == "ready_to_pick"


def test_create_pick_wave_fails_whole_batch_on_ineligible_status(db):
    with Session(db) as session:
        ready = make_order(session)
        not_ready = make_order(session, status="needs_review")
        session.commit()

        with pytest.raises(PickWaveSelectionError) as excinfo:
            create_pick_wave(session, [ready.id, not_ready.id])
        assert "needs_review" in str(excinfo.value)

        session.rollback()
        assert session.query(PickWave).count() == 0
        session.refresh(ready)
        assert ready.status == "ready_to_pick"


def test_order_already_in_active_wave_is_not_reselectable(db):
    with Session(db) as session:
        order = make_order(session)
        session.commit()

        create_pick_wave(session, [order.id])
        session.commit()

        other_order = make_order(session)
        session.commit()

        with pytest.raises(PickWaveSelectionError):
            create_pick_wave(session, [order.id, other_order.id])


def test_db_enforces_one_active_wave_per_order(db):
    with Session(db) as session:
        order = make_order(session)
        wave_one = PickWave(label="one", status="active")
        wave_two = PickWave(label="two", status="active")
        session.add_all([wave_one, wave_two])
        session.flush()

        session.add(PickWaveOrder(wave_id=wave_one.id, order_id=order.id, status="active"))
        session.flush()

        session.add(PickWaveOrder(wave_id=wave_two.id, order_id=order.id, status="active"))
        with pytest.raises(IntegrityError):
            session.flush()


def test_remove_order_from_wave_returns_it_to_ready_to_pick(db):
    with Session(db) as session:
        order = make_order(session)
        other = make_order(session)
        session.commit()

        wave = create_pick_wave(session, [order.id, other.id])
        session.commit()

        remove_order_from_wave(session, wave, order)
        session.commit()

        assert order.status == "ready_to_pick"
        membership = (
            session.query(PickWaveOrder)
            .filter(PickWaveOrder.wave_id == wave.id, PickWaveOrder.order_id == order.id)
            .one()
        )
        assert membership.status == "removed"
        assert other.status == "in_pick_wave"


def test_removed_order_is_selectable_for_a_new_wave(db):
    with Session(db) as session:
        order = make_order(session)
        session.commit()

        wave = create_pick_wave(session, [order.id])
        session.commit()

        remove_order_from_wave(session, wave, order)
        session.commit()

        new_wave = create_pick_wave(session, [order.id])
        session.commit()

        assert new_wave.id != wave.id
        assert order.status == "in_pick_wave"


def test_remove_order_from_wave_requires_active_wave(db):
    with Session(db) as session:
        order = make_order(session)
        session.commit()
        wave = create_pick_wave(session, [order.id])
        session.commit()

        cancel_pick_wave(session, wave)
        session.commit()

        with pytest.raises(PickWaveSelectionError):
            remove_order_from_wave(session, wave, order)


def test_remove_order_from_wave_requires_active_membership(db):
    with Session(db) as session:
        order = make_order(session)
        unrelated_wave = PickWave(label="unrelated", status="active")
        session.add(unrelated_wave)
        session.commit()

        with pytest.raises(PickWaveSelectionError):
            remove_order_from_wave(session, unrelated_wave, order)


def test_cancel_pick_wave_frees_order_for_a_new_wave(db):
    with Session(db) as session:
        order = make_order(session)
        session.commit()

        wave = create_pick_wave(session, [order.id])
        session.commit()

        cancel_pick_wave(session, wave)
        session.commit()

        assert order.status == "ready_to_pick"

        new_wave = create_pick_wave(session, [order.id])
        session.commit()
        assert new_wave.id != wave.id
