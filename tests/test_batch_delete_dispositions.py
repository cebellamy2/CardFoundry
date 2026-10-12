"""Delete batch, slice 3: what happens to each card.

The operator's shape (2026-10-11): one APPLY-TO-ALL choice at the top --
archive (default), delete, or move -- with a per-card list below it for
exceptions that override the bulk choice.

★ ARCHIVE IS A BATCH, NOT A FLAG. There is no per-card archive in this
schema, and adding one would mean teaching ten Mana Pool queries a second
meaning of "archived". A reserved batch with is_archived=True gets the
same result for free, because every batch-aware quantity query filters on
Batch.is_archived and nothing else about the batch.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from batch_delete_dispositions import (
    ARCHIVE, ARCHIVE_BATCH_CODE, CHOICES, DEFAULT_CHOICE, DELETE,
    DispositionError, MOVE, archive_batch, resolve_choices,
)
from models import Base, Batch, ImportRecord, InventoryCard


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'disp.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def make_batch(session, code, **over):
    b = Batch(batch_code=code, **over)
    session.add(b)
    session.flush()
    return b


def card(session, b, *, n=1, status="available", sold_price=None):
    rec = session.query(ImportRecord).filter_by(batch_id=b.id).first()
    if not rec:
        rec = ImportRecord(batch_id=b.id, filename="x.csv",
                           file_hash=f"h{b.id}", card_count=1)
        session.add(rec)
        session.flush()
    c = InventoryCard(
        batch_id=b.id, import_id=rec.id, name=f"Card {n}", set_code="ICE",
        collector_number=str(n), mtgjson_id=f"mtg-{n}", language_id="EN",
        condition_id="LP", finish_id="NF", condition="Light Play",
        finish="normal", status=status, sold_price=sold_price,
    )
    session.add(c)
    session.flush()
    return c


# --- the archive batch ----------------------------------------------------

def test_the_archive_batch_is_created_on_first_use_and_is_archived(session):
    assert archive_batch(session, create=False) is None
    b = archive_batch(session)
    assert b.batch_code == ARCHIVE_BATCH_CODE
    assert b.is_archived is True, "it must not put its cards back on sale"
    assert b.is_consignment is False


def test_the_archive_batch_is_reused_not_duplicated(session):
    first = archive_batch(session)
    session.commit()
    second = archive_batch(session)
    assert second.id == first.id
    assert session.query(Batch).filter_by(batch_code=ARCHIVE_BATCH_CODE).count() == 1


def test_an_existing_archive_batch_is_found_case_insensitively(session):
    make_batch(session, "archive")
    session.commit()
    found = archive_batch(session, create=False)
    assert found is not None
    assert session.query(Batch).count() == 1


# --- apply to all ---------------------------------------------------------

def test_the_default_is_archive(session):
    assert DEFAULT_CHOICE == ARCHIVE
    b = make_batch(session, "A1")
    card(session, b, n=1, status="available")
    card(session, b, n=2, status="sold", sold_price=3.0)
    card(session, b, n=3, status="removed")
    session.commit()
    plan = resolve_choices(session, b.id)
    assert len(plan["archive"]) == 2, "sold + removed need a choice"
    assert plan["delete"] == []
    assert plan["move"] == []
    assert plan["needs_choice"] == 2
    assert plan["cards_total"] == 3


def test_apply_to_all_delete(session):
    b = make_batch(session, "A1")
    card(session, b, n=1, status="sold", sold_price=3.50)
    card(session, b, n=2, status="unsellable")
    session.commit()
    plan = resolve_choices(session, b.id, apply_to_all=DELETE)
    assert len(plan["delete"]) == 2
    assert plan["archive"] == []


def test_available_cards_never_need_a_choice(session):
    """They go with the batch; only history-carrying statuses are asked
    about."""
    b = make_batch(session, "A1")
    card(session, b, n=1)
    card(session, b, n=2)
    session.commit()
    plan = resolve_choices(session, b.id)
    assert plan["needs_choice"] == 0
    assert plan["archive"] == [] and plan["delete"] == [] and plan["move"] == []


# --- per-card overrides win ----------------------------------------------

def test_a_per_card_choice_overrides_the_bulk_choice(session):
    b = make_batch(session, "A1")
    keep = card(session, b, n=1, status="sold", sold_price=5.0)
    drop = card(session, b, n=2, status="sold", sold_price=7.0)
    session.commit()
    plan = resolve_choices(session, b.id, apply_to_all=ARCHIVE,
                           per_card={drop.id: DELETE})
    assert plan["archive"] == [keep.id]
    assert plan["delete"] == [drop.id]


def test_overrides_work_in_the_other_direction_too(session):
    b = make_batch(session, "A1")
    a = card(session, b, n=1, status="sold", sold_price=1.0)
    k = card(session, b, n=2, status="sold", sold_price=2.0)
    session.commit()
    plan = resolve_choices(session, b.id, apply_to_all=DELETE,
                           per_card={k.id: ARCHIVE})
    assert plan["delete"] == [a.id]
    assert plan["archive"] == [k.id]


@pytest.mark.parametrize("bad", ["", "burn", "ARCHIVED", None])
def test_an_invalid_bulk_choice_is_refused(session, bad):
    b = make_batch(session, "A1")
    card(session, b, n=1, status="sold", sold_price=1.0)
    session.commit()
    if bad in ("", None):
        # empty means "unspecified", which falls back to the default
        assert resolve_choices(session, b.id, apply_to_all=bad)["apply_to_all"] == ARCHIVE
    else:
        with pytest.raises(DispositionError, match="not a choice"):
            resolve_choices(session, b.id, apply_to_all=bad)


def test_an_invalid_per_card_choice_is_refused_and_named(session):
    b = make_batch(session, "A1")
    c = card(session, b, n=1, status="sold", sold_price=1.0)
    session.commit()
    with pytest.raises(DispositionError, match=f"#{c.id}"):
        resolve_choices(session, b.id, per_card={c.id: "incinerate"})


# --- move needs a real destination ---------------------------------------

def test_moving_without_a_destination_is_refused(session):
    b = make_batch(session, "A1")
    card(session, b, n=1, status="sold", sold_price=1.0)
    session.commit()
    with pytest.raises(DispositionError, match="needs a destination"):
        resolve_choices(session, b.id, apply_to_all=MOVE)


def test_moving_into_the_batch_being_deleted_is_refused(session):
    b = make_batch(session, "A1")
    card(session, b, n=1, status="sold", sold_price=1.0)
    session.commit()
    with pytest.raises(DispositionError, match="cannot be the batch being deleted"):
        resolve_choices(session, b.id, apply_to_all=MOVE, move_target_id=b.id)


def test_moving_to_a_missing_batch_is_refused(session):
    b = make_batch(session, "A1")
    card(session, b, n=1, status="sold", sold_price=1.0)
    session.commit()
    with pytest.raises(DispositionError, match="not found"):
        resolve_choices(session, b.id, apply_to_all=MOVE, move_target_id=9999)


def test_a_move_destination_is_reported(session):
    b = make_batch(session, "A1")
    dest = make_batch(session, "B1")
    c = card(session, b, n=1, status="sold", sold_price=1.0)
    session.commit()
    plan = resolve_choices(session, b.id, apply_to_all=MOVE, move_target_id=dest.id)
    assert plan["move"] == [c.id]
    assert plan["move_target_code"] == "B1"


# --- the money the operator needs to see ---------------------------------

def test_sold_history_being_DELETED_is_reported_separately(session):
    """★ THE NUMBER THAT MATTERS. Money whose record is about to be
    destroyed is not the same as money merely moved out of circulation."""
    b = make_batch(session, "A1")
    gone = card(session, b, n=1, status="sold", sold_price=12.50)
    kept = card(session, b, n=2, status="sold", sold_price=99.00)
    card(session, b, n=3, status="removed")          # no sold_price
    session.commit()
    plan = resolve_choices(session, b.id, apply_to_all=ARCHIVE,
                           per_card={gone.id: DELETE})
    assert plan["sold_history_deleted"] == 12.50
    assert plan["sold_history_archived"] == 99.00
    assert plan["sold_history_moved"] == 0


def test_the_plan_counts_by_status_per_choice(session):
    b = make_batch(session, "A1")
    card(session, b, n=1, status="sold", sold_price=1.0)
    card(session, b, n=2, status="sold", sold_price=2.0)
    card(session, b, n=3, status="removed")
    card(session, b, n=4, status="unsellable")
    session.commit()
    plan = resolve_choices(session, b.id)
    assert plan["by_status"][ARCHIVE] == {"removed": 1, "sold": 2, "unsellable": 1}
    assert plan["by_status"][DELETE] == {}


def test_resolve_writes_nothing(session):
    b = make_batch(session, "A1")
    card(session, b, n=1, status="sold", sold_price=1.0)
    session.commit()
    resolve_choices(session, b.id, apply_to_all=DELETE)
    session.commit()
    assert session.query(InventoryCard).one().status == "sold"
    assert session.query(Batch).filter_by(batch_code=ARCHIVE_BATCH_CODE).count() == 0


def test_the_choices_are_exactly_three(session):
    assert set(CHOICES) == {"archive", "delete", "move"}


# --- the screens ----------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    import main
    engine = create_engine(f"sqlite:///{tmp_path / 'screens.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    import inventory_sync_service
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    import manapool_service
    monkeypatch.setattr(manapool_service, "get_all_seller_inventory",
                        lambda **kw: [])
    import manapool_quantity_push_service as mqps
    monkeypatch.setattr(mqps, "live_product_ids", lambda seller_loader=None: set())
    from fastapi.testclient import TestClient
    return TestClient(main.app), engine


def _seed(session, code="A1"):
    b = make_batch(session, code)
    card(session, b, n=1, status="available")
    sold = card(session, b, n=2, status="sold", sold_price=9.50)
    rem = card(session, b, n=3, status="removed")
    session.commit()
    return b, sold, rem


def test_the_form_defaults_to_archive_and_lists_the_exceptions(client):
    http, engine = client
    with Session(engine) as s:
        b, sold, rem = _seed(s)
        bid, sold_id, rem_id = b.id, sold.id, rem.id
    html = http.get(f"/admin/delete-batch?batch_id={bid}").text
    assert 'value="archive" checked' in html, "archive must be preselected"
    assert 'value="delete" checked' not in html
    # the two history-carrying cards appear as exceptions, the available one does not
    assert f"card_{sold_id}" in html and f"card_{rem_id}" in html
    assert "Exceptions, card by card" in html
    assert "$9.50" in html


def test_the_action_buttons_appear_at_the_top_and_the_bottom(client):
    """The operator's standing request for any page long enough to scroll."""
    http, engine = client
    with Session(engine) as s:
        b, _sold, _rem = _seed(s)
        bid = b.id
    html = http.get(f"/admin/delete-batch?batch_id={bid}").text
    assert html.count("Dry-run a delete of this batch") == 2


def test_the_confirm_screen_shows_the_disposition_outcome(client):
    http, engine = client
    with Session(engine) as s:
        b, _sold, _rem = _seed(s)
        bid = b.id
    page = http.post("/admin/delete-batch/confirm",
                     data={"batch_id": bid, "apply_to_all": "archive"})
    assert page.status_code == 200
    assert "No sold history will be destroyed" in page.text
    assert "Archived" in page.text and "Deleted" in page.text
    assert "Type <strong>A1</strong> to confirm" in page.text


def test_a_per_card_override_wins_on_the_confirm_screen(client):
    http, engine = client
    with Session(engine) as s:
        b, sold, _rem = _seed(s)
        bid, sold_id = b.id, sold.id
    page = http.post("/admin/delete-batch/confirm", data={
        "batch_id": bid, "apply_to_all": "archive",
        f"card_{sold_id}": "delete",
    })
    assert page.status_code == 200
    assert "$9.50 of sold history will be destroyed" in page.text
    # and the choice is carried into the apply as a hidden field
    assert f'name="card_{sold_id}" value="delete"' in page.text


def test_move_without_a_destination_is_refused_by_the_screen(client):
    http, engine = client
    with Session(engine) as s:
        b, _sold, _rem = _seed(s)
        bid = b.id
    page = http.post("/admin/delete-batch/confirm",
                     data={"batch_id": bid, "apply_to_all": "move"})
    assert page.status_code == 400
    assert "needs a destination" in page.text


def test_move_with_a_destination_is_accepted_and_named(client):
    http, engine = client
    with Session(engine) as s:
        b, _sold, _rem = _seed(s)
        dest = make_batch(s, "B9")
        s.commit()
        bid, dest_id = b.id, dest.id
    page = http.post("/admin/delete-batch/confirm", data={
        "batch_id": bid, "apply_to_all": "move", "move_target_id": dest_id,
    })
    assert page.status_code == 200
    assert "Moved to B9" in page.text


def test_an_untouched_per_card_group_submits_nothing_and_keeps_the_default(client):
    """An unchecked radio group sends no field at all, which is what makes
    "leave these alone" the default rather than something to assert."""
    http, engine = client
    with Session(engine) as s:
        b, sold, _rem = _seed(s)
        bid, sold_id = b.id, sold.id
    page = http.post("/admin/delete-batch/confirm", data={
        "batch_id": bid, "apply_to_all": "archive", f"card_{sold_id}": "",
    })
    assert page.status_code == 200
    assert "No sold history will be destroyed" in page.text


def test_apply_refuses_a_wrong_typed_name_even_with_valid_choices(client):
    http, engine = client
    with Session(engine) as s:
        b, _sold, _rem = _seed(s)
        bid = b.id
    page = http.post("/admin/delete-batch/apply", data={
        "batch_id": bid, "apply_to_all": "archive", "typed_batch_code": "NOPE",
    })
    assert page.status_code == 400
    with Session(engine) as s:
        assert s.query(Batch).filter_by(id=bid).count() == 1
        assert s.query(InventoryCard).count() == 3
