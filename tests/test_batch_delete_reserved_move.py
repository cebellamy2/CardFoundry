"""Delete batch: moving the cards already promised to a buyer.

★ WHY THIS EXISTS (operator, 2026-10-11). Batch E4 was imported from a
CSV holding cards he never meant to list. The unsold ones must come off
Mana Pool; the handful already reserved for unshipped orders must still
ship. So a `reserved` card no longer blocks the delete outright -- it is
MOVED to another live batch, keeping its allocation, while the rest of
the batch goes.

The properties worth more than the rest:

  1. A reserved card can ONLY be moved. Archive and delete stay refused
     for it, whatever the operator picks for everything else, because
     somebody has paid for it.
  2. The allocation is untouched -- same row, same card id, still
     `allocated` -- so the order keeps the same physical card and its
     pick list simply reads the new batch.
  3. A reserved card that CANNOT honestly be moved is still refused, by
     name, rather than silently skipped. A silent skip would delete it
     with the batch.
  4. An ACTIVE pick wave outranks all of it. Vacuous in production today
     (all 54 waves are cancelled or completed), so it is tested
     synthetically here -- which is the only reason it is tested at all.
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from batch_delete_dispositions import (
    DELETE, DispositionError, MOVE, reserved_rows, resolve_choices,
    validate_move_target,
)
from batch_delete_service import (
    ACTIVE_WAVE_REFUSAL, SLICE_2_REFUSAL, BatchDeleteRefused, delete_batch,
    slice_2_refusals,
)
from models import (
    Base, Batch, ImportRecord, InventoryCard, InventoryChangeLog, OrderItem,
    PickAllocation, PickWave, PickWaveOrder, RemoteProductBinding, SalesOrder,
)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'resmove.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


_LIVE_IDS = {"prod-1", "prod-2", "prod-shared"}


@pytest.fixture(autouse=True)
def _listings_are_live(monkeypatch):
    import manapool_quantity_push_service as mqps
    monkeypatch.setattr(mqps, "live_product_ids", lambda seller_loader=None: _LIVE_IDS)


def make_batch(session, code, **over):
    batch = Batch(batch_code=code, **over)
    session.add(batch)
    session.flush()
    return batch


def add_card(session, batch, *, n=1, status="available", mtgjson="MTG-A", **over):
    rec = session.query(ImportRecord).filter_by(batch_id=batch.id).first()
    if not rec:
        rec = ImportRecord(batch_id=batch.id, filename="x.csv",
                           file_hash=f"h{batch.id}", card_count=1)
        session.add(rec)
        session.flush()
    values = {
        "batch_id": batch.id, "import_id": rec.id, "name": f"Card {n}",
        "set_code": "ICE", "collector_number": str(n),
        "scryfall_id": f"sf-{batch.id}-{n}", "mtgjson_id": mtgjson,
        "language_id": "EN", "condition_id": "NM", "finish_id": "NF",
        "condition": "NM", "finish": "normal", "status": status,
        "current_price": 1.00,
    }
    values.update(over)
    card = InventoryCard(**values)
    session.add(card)
    session.flush()
    return card


def reserve(session, batch, card, *, alloc_status="allocated",
            order_status="ready_to_pick", label="4321-8765"):
    """A reserved card with a real order behind it."""
    card.status = "reserved"
    order = SalesOrder(external_order_id=f"o-{card.id}", external_label=label,
                       source="manapool", status=order_status)
    session.add(order)
    session.flush()
    item = OrderItem(order_id=order.id, name=card.name, quantity=1,
                     mtgjson_id=card.mtgjson_id, language_id="EN",
                     condition_id="NM", finish_id="NF", price_cents=100)
    session.add(item)
    session.flush()
    allocation = PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id,
        batch_id=batch.id, status=alloc_status)
    session.add(allocation)
    session.flush()
    return order, item, allocation


def add_binding(session, *, product_id="prod-1", mtgjson="MTG-A", card_ids=()):
    binding = RemoteProductBinding(
        provider="manapool", product_type="mtg_single", product_id=product_id,
        local_card_ids_json=json.dumps(list(card_ids)),
        requested_identity_json=json.dumps({"name": "Card 1"}),
        scryfall_id="sf-x", mtgjson_id=mtgjson, language_id="EN",
        condition_id="NM", finish_id="NF", set_code="ICE",
        collector_number="1", binding_status="validated",
        validated_at=__import__("datetime").datetime.now(),
        evidence_hash=f"ev-{product_id}", evidence_json="{}",
    )
    session.add(binding)
    session.flush()
    return binding


def ok_push(updates):
    return [{"inventory": [dict(u) for u in updates], "skipped": []}]


def reader_for(expected):
    def read(product_id, product_type="mtg_single"):
        return {"inventory": {"quantity": expected[product_id]}}
    return read


# --- which reserved cards may move ---------------------------------------

def test_a_reserved_card_with_a_live_allocated_order_no_longer_refuses(session):
    """★ THE WHOLE POINT. This is exactly E4's situation."""
    batch = make_batch(session, "E4")
    add_card(session, batch, n=1)
    card = add_card(session, batch, n=2)
    reserve(session, batch, card)
    session.commit()

    codes = [r.code for r in slice_2_refusals(session, batch)]
    assert SLICE_2_REFUSAL not in codes, (
        "a reserved card that can be moved out of the way must not block"
    )


def test_a_reserved_card_with_no_allocation_is_still_refused_by_name(session):
    """It cannot be moved honestly -- there is no order to keep shipping --
    and a silent skip would delete it with the batch."""
    batch = make_batch(session, "E4")
    add_card(session, batch, n=1)
    add_card(session, batch, n=2, status="reserved")
    session.commit()

    found = [r for r in slice_2_refusals(session, batch) if r.code == SLICE_2_REFUSAL]
    assert found
    detail = " ".join(r.detail for r in found)
    assert "no live allocation" in detail
    assert "#2" in detail or "Card 2" in detail


@pytest.mark.parametrize("alloc_status", ["picked", "exception", "released"])
def test_a_reserved_card_whose_allocation_is_not_allocated_is_refused(
    session, alloc_status,
):
    """`allocated` is the only state a move is honest for: picked means
    somebody has physically handled it, exception means it is already
    mid-resolution, released is a cancelled order's only restore path."""
    batch = make_batch(session, "E4")
    add_card(session, batch, n=1)
    card = add_card(session, batch, n=2)
    reserve(session, batch, card, alloc_status=alloc_status)
    session.commit()

    codes = [r.code for r in slice_2_refusals(session, batch)]
    assert SLICE_2_REFUSAL in codes or "released_allocation" in codes


def test_a_reserved_card_on_a_finished_order_is_refused(session):
    batch = make_batch(session, "E4")
    add_card(session, batch, n=1)
    card = add_card(session, batch, n=2)
    reserve(session, batch, card, order_status="shipped")
    session.commit()

    found = [r for r in slice_2_refusals(session, batch) if r.code == SLICE_2_REFUSAL]
    assert found
    assert "shipped" in " ".join(r.detail for r in found)


def test_reserved_rows_reports_card_order_and_allocation(session):
    """The screen, the refusal and resolve_choices all read this one
    function, so they cannot disagree about what is movable."""
    batch = make_batch(session, "E4")
    card = add_card(session, batch, n=1)
    order, item, allocation = reserve(session, batch, card, label="9999-1111")
    session.commit()

    rows = reserved_rows(session, batch.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["card_id"] == card.id
    assert row["order_id"] == order.id
    assert row["order_label"] == "9999-1111"
    assert row["allocation_id"] == allocation.id
    assert row["allocation_status"] == "allocated"
    assert row["movable"] is True
    assert row["reason"] is None


# --- the destination rules ------------------------------------------------

def test_a_reserved_card_requires_a_destination(session):
    batch = make_batch(session, "E4")
    card = add_card(session, batch, n=1)
    reserve(session, batch, card)
    session.commit()

    with pytest.raises(DispositionError) as exc:
        resolve_choices(session, batch.id)
    assert "destination" in str(exc.value)


def test_the_destination_cannot_be_the_batch_being_deleted(session):
    batch = make_batch(session, "E4")
    with pytest.raises(DispositionError) as exc:
        validate_move_target(session, batch.id, batch.id, what="X")
    assert "being deleted" in str(exc.value)


def test_the_destination_cannot_be_archived(session):
    batch = make_batch(session, "E4")
    gone = make_batch(session, "OLD", is_archived=True)
    with pytest.raises(DispositionError) as exc:
        validate_move_target(session, batch.id, gone.id, what="X")
    assert "archived" in str(exc.value)


def test_the_destination_cannot_be_a_consignment_batch(session):
    batch = make_batch(session, "E4")
    theirs = make_batch(session, "SHELF9", is_consignment=True)
    with pytest.raises(DispositionError) as exc:
        validate_move_target(session, batch.id, theirs.id, what="X")
    assert "consignment" in str(exc.value)


def test_the_destination_cannot_be_named_like_a_consignment_batch(session):
    """CON_RAU in production carries is_consignment=0, so the name has to
    be checked as well as the flag."""
    batch = make_batch(session, "E4")
    named = make_batch(session, "CON_RAU", is_consignment=False)
    with pytest.raises(DispositionError) as exc:
        validate_move_target(session, batch.id, named.id, what="X")
    assert "named like a consignment" in str(exc.value)


def test_a_missing_destination_is_refused(session):
    batch = make_batch(session, "E4")
    with pytest.raises(DispositionError) as exc:
        validate_move_target(session, batch.id, 999999, what="X")
    assert "not found" in str(exc.value)


# --- reserved cards may only ever move -----------------------------------

def test_a_reserved_card_is_moved_even_when_everything_else_is_deleted(session):
    """Archive and delete must not reach a card somebody has paid for,
    whatever the apply-to-all choice says."""
    batch = make_batch(session, "E4")
    target = make_batch(session, "E2")
    sold = add_card(session, batch, n=1, status="sold", sold_price=5.0)
    card = add_card(session, batch, n=2)
    reserve(session, batch, card)
    session.commit()

    plan = resolve_choices(
        session, batch.id, apply_to_all=DELETE, reserved_target_id=target.id,
    )
    assert plan["reserved_move"] == [card.id]
    assert plan["delete"] == [sold.id]
    assert card.id not in plan["delete"]
    assert card.id not in plan["archive"]
    assert plan["reserved_target_code"] == "E2"


def test_the_history_destination_and_the_reserved_destination_are_separate(session):
    """Filing sold history is bookkeeping; a promised card's batch is a
    shelf somebody walks to. They are different questions."""
    batch = make_batch(session, "E4")
    history_to = make_batch(session, "E1")
    reserved_to = make_batch(session, "E2")
    sold = add_card(session, batch, n=1, status="sold", sold_price=5.0)
    card = add_card(session, batch, n=2)
    reserve(session, batch, card)
    session.commit()

    plan = resolve_choices(
        session, batch.id, apply_to_all=MOVE,
        move_target_id=history_to.id, reserved_target_id=reserved_to.id,
    )
    assert plan["move"] == [sold.id]
    assert plan["move_target_code"] == "E1"
    assert plan["reserved_move"] == [card.id]
    assert plan["reserved_target_code"] == "E2"


# --- the apply ------------------------------------------------------------

def test_the_apply_moves_the_reserved_card_and_deletes_the_rest(session):
    batch = make_batch(session, "E4")
    target = make_batch(session, "E2")
    doomed = add_card(session, batch, n=1)
    card = add_card(session, batch, n=2, mtgjson="MTG-B")
    reserve(session, batch, card)
    add_binding(session, product_id="prod-1", mtgjson="MTG-A",
                card_ids=[doomed.id])
    session.commit()
    batch_id, card_id, doomed_id, target_id = (
        batch.id, card.id, doomed.id, target.id)

    plan = resolve_choices(session, batch_id, reserved_target_id=target_id)
    result = delete_batch(
        session, batch_id, apply=True, pusher=ok_push,
        reader=reader_for({"prod-1": 0}), dispositions=plan,
    )
    session.commit()

    assert result["disposed"]["reserved_move"] == 1
    survivor = session.get(InventoryCard, card_id)
    assert survivor is not None, "a card promised to a buyer must survive"
    assert survivor.batch_id == target_id
    assert survivor.status == "reserved"
    assert session.get(InventoryCard, doomed_id) is None
    assert session.get(Batch, batch_id) is None


def test_the_allocation_keeps_its_card_and_status_but_follows_the_batch(session):
    """★ REWRITTEN 2026-10-12 after this test's original form caused a
    production bug.

    It used to be called test_the_allocation_is_left_completely_untouched
    and asserted exactly that -- which is what shipped v2.39.0's defect.
    "Untouched" is right for `inventory_card_id` and `status`: the order
    must keep the same physical card and the same place in the workflow,
    and re-pointing or re-creating the allocation would break its history
    for no gain. It is WRONG for `batch_id`, which PickAllocation keeps
    its own copy of and which three inner joins read -- so leaving it
    behind dropped the row from seven live orders' pick lists instead of
    merely showing a stale batch.

    The lesson, and the reason the next three tests exist: the ticket
    asked that "its pick list shows the new batch", and this asserted the
    mechanism it believed produced that instead of the outcome itself."""
    batch = make_batch(session, "E4")
    target = make_batch(session, "E2")
    add_card(session, batch, n=1)
    card = add_card(session, batch, n=2, mtgjson="MTG-B")
    order, item, allocation = reserve(session, batch, card)
    add_binding(session, product_id="prod-1", mtgjson="MTG-A",
                card_ids=[1])
    session.commit()
    batch_id, target_id = batch.id, target.id
    alloc_id, card_id, order_id = allocation.id, card.id, order.id

    plan = resolve_choices(session, batch_id, reserved_target_id=target_id)
    delete_batch(session, batch_id, apply=True, pusher=ok_push,
                 reader=reader_for({"prod-1": 0}), dispositions=plan)
    session.commit()

    fresh = session.get(PickAllocation, alloc_id)
    assert fresh is not None, "the allocation must not be deleted"
    assert fresh.inventory_card_id == card_id, "same physical card"
    assert fresh.status == "allocated", "still waiting to be picked"
    assert session.get(SalesOrder, order_id).status == "ready_to_pick", (
        "the order must still be ready to pick"
    )
    assert fresh.batch_id == target_id, (
        "and its batch MUST follow the card -- three inner joins read this "
        "column, and a stale value drops the row from the pick list entirely"
    )
    assert fresh.batch_id == session.get(InventoryCard, card_id).batch_id, (
        "allocation and card must never disagree about where the card is"
    )


def test_the_move_is_audited_per_card_naming_both_batches(session):
    batch = make_batch(session, "E4")
    target = make_batch(session, "E2")
    add_card(session, batch, n=1)
    card = add_card(session, batch, n=2, mtgjson="MTG-B")
    reserve(session, batch, card)
    add_binding(session, product_id="prod-1", mtgjson="MTG-A", card_ids=[1])
    session.commit()
    batch_id, target_id, card_id = batch.id, target.id, card.id

    plan = resolve_choices(session, batch_id, reserved_target_id=target_id)
    delete_batch(session, batch_id, apply=True, pusher=ok_push,
                 reader=reader_for({"prod-1": 0}), dispositions=plan)
    session.commit()

    logs = session.query(InventoryChangeLog).filter(
        InventoryChangeLog.inventory_card_id == card_id).all()
    assert logs, "the move must be audited"
    summary = " ".join(log.change_summary or "" for log in logs)
    assert "'E4'" in summary and "'E2'" in summary
    assert "reserved for an unshipped order" in summary


def test_a_moved_reserved_card_keeps_its_place_in_the_binding(session):
    """Bindings hold a JSON list of card ids with no foreign key. The
    delete strips the ids that actually went; a moved card is still here,
    so stripping it would under-list its identity the moment it came back
    to available."""
    batch = make_batch(session, "E4")
    target = make_batch(session, "E2")
    doomed = add_card(session, batch, n=1)
    card = add_card(session, batch, n=2)
    reserve(session, batch, card)
    binding = add_binding(session, product_id="prod-1", mtgjson="MTG-A",
                          card_ids=[doomed.id, card.id])
    session.commit()
    batch_id, target_id, card_id, doomed_id, binding_id = (
        batch.id, target.id, card.id, doomed.id, binding.id)

    plan = resolve_choices(session, batch_id, reserved_target_id=target_id)
    delete_batch(session, batch_id, apply=True, pusher=ok_push,
                 reader=reader_for({"prod-1": 0}), dispositions=plan)
    session.commit()

    held = json.loads(session.get(RemoteProductBinding, binding_id).local_card_ids_json)
    assert card_id in held, "the moved card is still ours"
    assert doomed_id not in held, "the deleted one is not"


def test_only_the_deleted_available_cards_reduce_the_quantity(session):
    """A reserved card counts zero toward a listing either way -- only
    `available` cards do -- so the move is quantity-neutral and the number
    pushed is driven entirely by what was actually deleted."""
    batch = make_batch(session, "E4")
    elsewhere = make_batch(session, "A1")
    target = make_batch(session, "E2")
    add_card(session, batch, n=1, mtgjson="MTG-A")
    add_card(session, elsewhere, n=9, mtgjson="MTG-A")
    card = add_card(session, batch, n=2, mtgjson="MTG-A")
    reserve(session, batch, card)
    add_binding(session, product_id="prod-shared", mtgjson="MTG-A")
    session.commit()
    batch_id, target_id = batch.id, target.id

    plan = resolve_choices(session, batch_id, reserved_target_id=target_id)
    result = delete_batch(
        session, batch_id, apply=True, pusher=ok_push,
        reader=reader_for({"prod-shared": 1}), dispositions=plan,
    )
    session.commit()

    written = {b["product_id"]: b["quantity_written"] for b in result["bindings"]}
    assert written["prod-shared"] == 1, (
        "one available copy survives in A1, so this must NOT go to zero"
    )


def test_the_apply_revalidates_the_destination_it_is_handed(session):
    """The plan reaches the apply through a form the operator could have
    been sitting on while the destination was archived."""
    batch = make_batch(session, "E4")
    target = make_batch(session, "E2")
    add_card(session, batch, n=1)
    card = add_card(session, batch, n=2)
    reserve(session, batch, card)
    add_binding(session, product_id="prod-1", mtgjson="MTG-A", card_ids=[1])
    session.commit()
    batch_id, target_id = batch.id, target.id

    plan = resolve_choices(session, batch_id, reserved_target_id=target_id)
    session.get(Batch, target_id).is_archived = True
    session.commit()

    with pytest.raises(DispositionError) as exc:
        delete_batch(session, batch_id, apply=True, pusher=ok_push,
                     reader=reader_for({"prod-1": 0}), dispositions=plan)
    assert "archived" in str(exc.value)
    session.rollback()
    assert session.get(Batch, batch_id) is not None, "nothing was deleted"


def test_the_dry_run_rolls_the_move_back(session):
    """Same code path as the write, so the numbers are real -- and then
    nothing is left behind."""
    batch = make_batch(session, "E4")
    target = make_batch(session, "E2")
    add_card(session, batch, n=1)
    card = add_card(session, batch, n=2)
    reserve(session, batch, card)
    add_binding(session, product_id="prod-1", mtgjson="MTG-A", card_ids=[1])
    session.commit()
    batch_id, target_id, card_id = batch.id, target.id, card.id

    plan = resolve_choices(session, batch_id, reserved_target_id=target_id)
    result = delete_batch(session, batch_id, apply=False, pusher=ok_push,
                          reader=reader_for({"prod-1": 0}), dispositions=plan)
    session.commit()

    assert result["disposed"]["reserved_move"] == 1, "the dry run really moved it"
    assert result["applied"] is False
    assert session.get(InventoryCard, card_id).batch_id == batch_id, (
        "and then put it back"
    )
    assert session.get(Batch, batch_id) is not None


# --- the active pick wave -------------------------------------------------

def test_an_active_pick_wave_refuses_and_names_the_wave(session):
    """★ VACUOUS IN PRODUCTION TODAY -- all 54 waves are cancelled or
    completed -- so this synthetic wave is the only thing that exercises
    it. A card being picked right now is plain `allocated` and would
    otherwise be moved out from under the picker."""
    batch = make_batch(session, "E4")
    card = add_card(session, batch, n=1)
    order, item, allocation = reserve(session, batch, card)
    wave = PickWave(label="Wave 99", status="active")
    session.add(wave)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
    session.commit()

    found = [r for r in slice_2_refusals(session, batch)
             if r.code == ACTIVE_WAVE_REFUSAL]
    assert found, "an active wave must refuse"
    assert "Wave 99" in found[0].detail
    assert found[0].orders == ["Wave 99"]


def test_an_active_wave_stops_the_delete_before_anything_moves(session):
    batch = make_batch(session, "E4")
    target = make_batch(session, "E2")
    card = add_card(session, batch, n=1)
    order, item, allocation = reserve(session, batch, card)
    wave = PickWave(label="Wave 99", status="active")
    session.add(wave)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
    session.commit()
    batch_id, target_id, card_id = batch.id, target.id, card.id

    with pytest.raises(BatchDeleteRefused) as exc:
        delete_batch(session, batch_id, apply=True, pusher=ok_push,
                     reader=reader_for({}),
                     dispositions=resolve_choices(
                         session, batch_id, reserved_target_id=target_id))
    assert ACTIVE_WAVE_REFUSAL in [r.code for r in exc.value.refusals]
    session.rollback()
    assert session.get(InventoryCard, card_id).batch_id == batch_id


@pytest.mark.parametrize("wave_status", ["completed", "cancelled", "packed", "shipped"])
def test_a_finished_wave_does_not_refuse(session, wave_status):
    """Only an ACTIVE wave is somebody standing at the shelf."""
    batch = make_batch(session, "E4")
    card = add_card(session, batch, n=1)
    order, item, allocation = reserve(session, batch, card)
    wave = PickWave(label="Wave 1", status=wave_status)
    session.add(wave)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id,
                              status=wave_status))
    session.commit()

    codes = [r.code for r in slice_2_refusals(session, batch)]
    assert ACTIVE_WAVE_REFUSAL not in codes


def test_the_packed_refusal_still_fires(session):
    """Everything slice 1 and 2 refused must keep refusing."""
    batch = make_batch(session, "E4")
    card = add_card(session, batch, n=1)
    reserve(session, batch, card, alloc_status="packed", label="7777-2222")
    session.commit()

    codes = [r.code for r in slice_2_refusals(session, batch)]
    assert "packed_allocation" in codes


def test_an_unmovable_reserved_card_is_planned_for_nothing(session):
    """★ THE ORDERING CHOICE, pinned. resolve_choices deliberately does
    NOT raise for a reserved card it cannot move: slice_2_refusals already
    blocks the whole delete for it, and that screen lists every reason at
    once. Raising here too would pre-empt it with a narrower message.

    What makes the omission safe is that the card is planned for NOTHING,
    so it just stays in the batch -- and the refusal then stops the batch.
    If that refusal were ever weakened, this card would be deleted, which
    is why both halves are asserted together."""
    batch = make_batch(session, "E4")
    add_card(session, batch, n=1)
    orphan = add_card(session, batch, n=2, status="reserved")
    session.commit()

    plan = resolve_choices(session, batch.id)
    assert plan["reserved_move"] == [], "nothing is planned for it"
    assert orphan.id not in plan["delete"]
    assert orphan.id not in plan["archive"]
    assert orphan.id not in plan["move"]
    assert plan["reserved_target_id"] is None, "and no destination is demanded"
    assert [r["card_id"] for r in plan["reserved_rows"]] == [orphan.id], (
        "but it is still reported, so the screen can show it"
    )
    assert SLICE_2_REFUSAL in [r.code for r in slice_2_refusals(session, batch)], (
        "and the refusal is what actually protects it"
    )


# --- the screen -----------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    import main
    engine = create_engine(f"sqlite:///{tmp_path / 'routes_resmove.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    import inventory_sync_service
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    import manapool_service
    monkeypatch.setattr(manapool_service, "get_all_seller_inventory",
                        lambda **kw: [])
    from fastapi.testclient import TestClient
    return TestClient(main.app), engine


def test_the_screen_names_the_card_the_order_and_the_allocation(client):
    http, engine = client
    with Session(engine) as s:
        batch = make_batch(s, "E4")
        make_batch(s, "E2")
        add_card(s, batch, n=1)
        card = add_card(s, batch, n=2)
        order, item, allocation = reserve(s, batch, card, label="4321-8765")
        s.commit()
        batch_id, card_id, alloc_id = batch.id, card.id, allocation.id

    page = http.get(f"/admin/delete-batch?batch_id={batch_id}")
    assert page.status_code == 200
    assert "already promised to a buyer" in page.text
    assert f"#{card_id}" in page.text
    assert "4321-8765" in page.text, "the order must be named"
    assert f"#{alloc_id}" in page.text, "the allocation must be named"
    assert 'name="reserved_target_id"' in page.text
    assert "required" in page.text, "a destination is not optional"
    assert "physical move" in page.text, (
        "the picker is sent to the new batch, so the cards must follow"
    )


def test_the_destination_dropdown_offers_no_batch_the_apply_would_refuse(client):
    http, engine = client
    with Session(engine) as s:
        batch = make_batch(s, "E4")
        make_batch(s, "E2")
        make_batch(s, "SHELF9", is_consignment=True)
        make_batch(s, "CON_RAU", is_consignment=False)
        make_batch(s, "OLDSTOCK", is_archived=True)
        card = add_card(s, batch, n=1)
        reserve(s, batch, card)
        s.commit()
        batch_id = batch.id

    page = http.get(f"/admin/delete-batch?batch_id={batch_id}")
    # Scoped to the destination select itself. The batch PICKER at the top
    # of the page legitimately lists every batch, including consignment
    # ones, so asserting against the whole page would prove nothing.
    import re
    select = re.search(
        r'<select id="reserved_target_id".*?</select>', page.text, re.S,
    )
    assert select, "the destination select must be on the page"
    options = select.group(0)
    assert "E2" in options, "a valid destination is offered"
    assert "E4" not in options, "not the batch being deleted"
    assert "SHELF9" not in options, "a consignment batch is not"
    assert "CON_RAU" not in options, "nor one merely named like one"
    assert "OLDSTOCK" not in options, "nor an archived one"


def test_the_screen_shows_no_reserved_section_when_there_are_none(client):
    http, engine = client
    with Session(engine) as s:
        batch = make_batch(s, "E4")
        add_card(s, batch, n=1)
        s.commit()
        batch_id = batch.id

    page = http.get(f"/admin/delete-batch?batch_id={batch_id}")
    assert "already promised to a buyer" not in page.text


def test_the_screen_has_a_button_at_the_top_and_the_bottom(client):
    """The operator's standing request for any page long enough to
    scroll, and a reserved list plus an exception list is exactly that."""
    http, engine = client
    with Session(engine) as s:
        batch = make_batch(s, "E4")
        make_batch(s, "E2")
        card = add_card(s, batch, n=1)
        reserve(s, batch, card)
        s.commit()
        batch_id = batch.id

    page = http.get(f"/admin/delete-batch?batch_id={batch_id}")
    assert page.text.count("Dry-run a delete of this batch") >= 2


def test_the_confirm_screen_says_where_the_reserved_cards_went(client):
    http, engine = client
    with Session(engine) as s:
        batch = make_batch(s, "E4")
        target = make_batch(s, "E2")
        add_card(s, batch, n=1)
        card = add_card(s, batch, n=2)
        reserve(s, batch, card)
        s.commit()
        batch_id, target_id = batch.id, target.id

    page = http.post("/admin/delete-batch/confirm", data={
        "batch_id": batch_id, "reserved_target_id": target_id,
    })
    assert page.status_code == 200, page.text[:400]
    assert "will move to E2" in page.text
    assert f'name="reserved_target_id" value="{target_id}"' in page.text, (
        "and the choice must be carried into the apply"
    )


def test_the_confirm_screen_refuses_a_reserved_card_with_no_destination(client):
    http, engine = client
    with Session(engine) as s:
        batch = make_batch(s, "E4")
        make_batch(s, "E2")
        card = add_card(s, batch, n=1)
        reserve(s, batch, card)
        s.commit()
        batch_id = batch.id

    page = http.post("/admin/delete-batch/confirm", data={"batch_id": batch_id})
    assert page.status_code == 400
    assert "needs a destination batch" in page.text


# --- the OUTCOME: what the operator and the picker actually see ----------
#
# ★ THESE ARE THE TESTS THAT WERE MISSING. v2.39.0 was verified by
# asserting the allocation was untouched, which is the defect itself. The
# requirement was always "its pick list shows the new batch/location", so
# these call the real reader functions and assert what comes back.

def test_get_picklist_shows_the_moved_card_under_its_new_batch(session):
    """order_service.get_picklist INNER JOINs Batch on
    PickAllocation.batch_id, so a stale pointer to a deleted batch drops
    the row rather than showing it wrong. Order 4526 in production was a
    one-line order whose pick list rendered EMPTY."""
    from order_service import get_picklist

    batch = make_batch(session, "E4")
    target = make_batch(session, "A1")
    add_card(session, batch, n=1)
    card = add_card(session, batch, n=2, mtgjson="MTG-B")
    order, item, allocation = reserve(session, batch, card)
    add_binding(session, product_id="prod-1", mtgjson="MTG-A", card_ids=[1])
    session.commit()
    batch_id, target_id, card_id, order_id = (
        batch.id, target.id, card.id, order.id)

    plan = resolve_choices(session, batch_id, reserved_target_id=target_id)
    delete_batch(session, batch_id, apply=True, pusher=ok_push,
                 reader=reader_for({"prod-1": 0}), dispositions=plan)
    session.commit()

    picklist = get_picklist(session, order_id)
    # It returns {batch_code: [{"allocation", "item", "card"}, ...]}
    rows = [row for group in picklist.values() for row in group]
    assert rows, "the order's pick list must not be empty"
    assert any(row["card"].id == card_id for row in rows), (
        "the moved card must still appear on its order's pick list"
    )
    assert "A1" in picklist, "and it must be listed under its NEW batch"
    assert "E4" not in picklist, "never under the batch that was deleted"
    assert [row["card"].id for row in picklist["A1"]] == [card_id]


def test_the_order_allocations_view_shows_the_moved_card(session):
    """_active_allocations does NOT join Batch, so it survived the bug --
    asserted here so a future refactor that adds such a join is caught."""
    from order_service import _active_allocations

    batch = make_batch(session, "E4")
    target = make_batch(session, "A1")
    add_card(session, batch, n=1)
    card = add_card(session, batch, n=2, mtgjson="MTG-B")
    order, item, allocation = reserve(session, batch, card)
    add_binding(session, product_id="prod-1", mtgjson="MTG-A", card_ids=[1])
    session.commit()
    batch_id, target_id, card_id, order_id = (
        batch.id, target.id, card.id, order.id)

    plan = resolve_choices(session, batch_id, reserved_target_id=target_id)
    delete_batch(session, batch_id, apply=True, pusher=ok_push,
                 reader=reader_for({"prod-1": 0}), dispositions=plan)
    session.commit()

    # It returns a plain list of PickAllocation objects.
    active = _active_allocations(session, order_id)
    assert [a.inventory_card_id for a in active] == [card_id]
    assert [a.status for a in active] == ["allocated"]
    assert [a.batch_id for a in active] == [target_id], (
        "and the allocation's own batch must be the new one"
    )


def test_the_orders_batch_column_shows_the_new_batch(session):
    """main._order_line_batch_codes joins the same column. In production
    it rendered blank for all seven lines."""
    import main

    batch = make_batch(session, "E4")
    target = make_batch(session, "A1")
    add_card(session, batch, n=1)
    card = add_card(session, batch, n=2, mtgjson="MTG-B")
    order, item, allocation = reserve(session, batch, card)
    add_binding(session, product_id="prod-1", mtgjson="MTG-A", card_ids=[1])
    session.commit()
    batch_id, target_id, item_id = batch.id, target.id, item.id

    plan = resolve_choices(session, batch_id, reserved_target_id=target_id)
    delete_batch(session, batch_id, apply=True, pusher=ok_push,
                 reader=reader_for({"prod-1": 0}), dispositions=plan)
    session.commit()

    codes = main._order_line_batch_codes(session, [item_id])
    assert codes.get(item_id) == "A1", (
        f"the batch column must read the new batch, got {codes.get(item_id)!r}"
    )


def test_the_wave_picklist_shows_the_moved_card(session):
    """pick_wave_service.get_wave_picklist joins the same column. In
    production a new wave would have shown 0 of the 7 cards."""
    from pick_wave_service import get_wave_picklist

    batch = make_batch(session, "E4")
    target = make_batch(session, "A1")
    add_card(session, batch, n=1)
    card = add_card(session, batch, n=2, mtgjson="MTG-B")
    order, item, allocation = reserve(session, batch, card)
    add_binding(session, product_id="prod-1", mtgjson="MTG-A", card_ids=[1])
    session.commit()
    batch_id, target_id, card_id, order_id = (
        batch.id, target.id, card.id, order.id)

    plan = resolve_choices(session, batch_id, reserved_target_id=target_id)
    delete_batch(session, batch_id, apply=True, pusher=ok_push,
                 reader=reader_for({"prod-1": 0}), dispositions=plan)
    session.commit()

    wave = PickWave(label="Wave 1", status="active")
    session.add(wave)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order_id, status="active"))
    session.commit()

    # It returns {batch_code: [{"allocation", "item", "card", "order"}, ...]}
    grouped = get_wave_picklist(session, wave.id)
    assert "A1" in grouped, (
        f"the moved card must be listed under its new batch; got {sorted(grouped)}"
    )
    assert "E4" not in grouped, "never under the batch that was deleted"
    assert [row["card"].id for row in grouped["A1"]] == [card_id]
    assert grouped["A1"][0]["order"].id == order_id


def test_no_active_allocation_may_ever_disagree_with_its_card(session):
    """The invariant the bug violated, asserted directly over the whole
    database after a move -- the cheapest possible guard against any future
    path that moves a card without bringing its allocations."""
    batch = make_batch(session, "E4")
    target = make_batch(session, "A1")
    add_card(session, batch, n=1)
    card = add_card(session, batch, n=2, mtgjson="MTG-B")
    reserve(session, batch, card)
    add_binding(session, product_id="prod-1", mtgjson="MTG-A", card_ids=[1])
    session.commit()
    batch_id, target_id = batch.id, target.id

    plan = resolve_choices(session, batch_id, reserved_target_id=target_id)
    delete_batch(session, batch_id, apply=True, pusher=ok_push,
                 reader=reader_for({"prod-1": 0}), dispositions=plan)
    session.commit()

    from inventory_mirror_service import ACTIVE_ALLOCATION_STATUSES
    disagreeing = (
        session.query(PickAllocation.id)
        .join(InventoryCard, InventoryCard.id == PickAllocation.inventory_card_id)
        .filter(
            PickAllocation.batch_id != InventoryCard.batch_id,
            PickAllocation.status.in_(tuple(ACTIVE_ALLOCATION_STATUSES)),
        ).all()
    )
    assert disagreeing == [], (
        f"active allocations disagreeing with their card: {disagreeing}"
    )
    orphaned = (
        session.query(PickAllocation.id)
        .outerjoin(Batch, Batch.id == PickAllocation.batch_id)
        .filter(Batch.id.is_(None)).all()
    )
    assert orphaned == [], f"allocations pointing at a dead batch: {orphaned}"
