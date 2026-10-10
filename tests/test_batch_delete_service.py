"""Delete batch, slice 2: deleting an all-available batch for real.

The two properties worth more than all the others:

  1. A failure ANYWHERE after the local deletes rolls the whole thing
     back, so a listing left standing on Mana Pool always still has its
     cards here. Tested by failing the push, failing the read-back, and
     reading back a wrong number.
  2. A delete NEVER zeroes a listing just because a batch went away. 271
     available identities in production are stocked by more than one
     batch, so the push carries the recomputed quantity, which is often
     non-zero.
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from batch_delete_service import (
    BatchDeleteFailed, BatchDeleteRefused, SLICE_2_REFUSAL, delete_batch,
    slice_2_refusals,
)
from manapool_quantity_push_service import QuantityPushFailed
from models import (
    Base, Batch, Consignor, ImportRecord, InventoryCard, InventoryChangeLog,
    InventoryListingStatus, InventoryPriceHistory, OrderItem, PickAllocation,
    RemoteProductBinding, SalesOrder,
)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'del2.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


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
    """What a clean bulk write looks like: a LIST of one response per
    chunk, each with nothing skipped."""
    return [{"inventory": [dict(u) for u in updates], "skipped": []}]


def reader_for(expected):
    def read(product_id, product_type="mtg_single"):
        return {"inventory": {"quantity": expected[product_id]}}
    return read


# --- refusals -------------------------------------------------------------

def test_a_batch_with_a_sold_card_is_refused(session):
    batch = make_batch(session, "A1")
    add_card(session, batch, n=1)
    add_card(session, batch, n=2, status="sold", sold_price=3.0)
    session.commit()
    codes = [r.code for r in slice_2_refusals(session, batch)]
    assert SLICE_2_REFUSAL in codes


@pytest.mark.parametrize("status", ["sold", "removed", "unsellable", "reserved"])
def test_every_non_available_status_is_refused(session, status):
    batch = make_batch(session, "A1")
    add_card(session, batch, n=1)
    add_card(session, batch, n=2, status=status)
    session.commit()
    assert SLICE_2_REFUSAL in [r.code for r in slice_2_refusals(session, batch)]


def test_a_released_allocation_refuses_and_names_the_order(session):
    """★ OPERATOR ADDITION 2026-10-10. Allocation rows are never deleted by
    the app: a release keeps the row as "released" with released_from_status,
    which is the ONLY thing uncancel_order has to restore a cancelled order
    from. Deleting it would silently remove the possibility of
    un-cancelling, forever."""
    batch = make_batch(session, "C10")
    card = add_card(session, batch, n=1)
    order = SalesOrder(external_order_id="o-1", external_label="4321-8765",
                       source="manapool", status="cancelled",
                       cancelled_from_status="in_pick_wave")
    session.add(order)
    session.flush()
    item = OrderItem(order_id=order.id, name=card.name, quantity=1,
                     mtgjson_id=card.mtgjson_id, language_id="EN",
                     condition_id="NM", finish_id="NF", price_cents=100)
    session.add(item)
    session.flush()
    session.add(PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id,
        status="released", released_from_status="in_pick_wave"))
    session.commit()

    found = [r for r in slice_2_refusals(session, batch)
             if r.code == "released_allocation"]
    assert found, "a released allocation must refuse"
    assert "4321-8765" in found[0].detail
    assert found[0].orders == ["4321-8765"]


def test_a_released_allocation_row_is_never_deleted(session):
    """The refusal must stop the delete BEFORE anything is touched, so the
    row uncancel_order needs is still there afterwards."""
    batch = make_batch(session, "C10")
    card = add_card(session, batch, n=1)
    order = SalesOrder(external_order_id="o-1", external_label="4321-8765",
                       source="manapool", status="cancelled",
                       cancelled_from_status="in_pick_wave")
    session.add(order)
    session.flush()
    item = OrderItem(order_id=order.id, name=card.name, quantity=1,
                     mtgjson_id=card.mtgjson_id, language_id="EN",
                     condition_id="NM", finish_id="NF", price_cents=100)
    session.add(item)
    session.flush()
    session.add(PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id,
        status="released", released_from_status="in_pick_wave"))
    session.commit()
    batch_id, card_id = batch.id, card.id

    with pytest.raises(BatchDeleteRefused) as exc:
        delete_batch(session, batch_id, apply=True, pusher=ok_push,
                     reader=reader_for({}), live_inventory_reader=lambda: [])
    assert "released_allocation" in [r.code for r in exc.value.refusals]
    session.rollback()

    allocation = session.query(PickAllocation).one()
    assert allocation.status == "released"
    assert allocation.released_from_status == "in_pick_wave"
    assert session.query(InventoryCard).filter_by(id=card_id).count() == 1
    assert session.query(Batch).filter_by(id=batch_id).count() == 1


def test_an_all_available_batch_is_not_refused(session):
    batch = make_batch(session, "A1")
    add_card(session, batch, n=1)
    add_card(session, batch, n=2)
    session.commit()
    assert slice_2_refusals(session, batch) == []


def test_slice_1_refusals_still_apply(session):
    """A consignment batch stays refused even when all its cards are
    available -- slice 2 adds a limit, it does not replace the others."""
    consignor = Consignor(name="Kevin")
    session.add(consignor)
    session.flush()
    batch = make_batch(session, "CON_KEV", is_consignment=True,
                       consignor_id=consignor.id)
    add_card(session, batch, n=1)
    session.commit()
    codes = [r.code for r in slice_2_refusals(session, batch)]
    assert "consigned" in codes


def test_the_con_prefix_refusal_is_kept(session):
    """Decided 2026-10-10, after CON_RAU."""
    batch = make_batch(session, "CON_RAU", is_consignment=False)
    add_card(session, batch, n=1)
    session.commit()
    codes = [r.code for r in slice_2_refusals(session, batch)]
    assert "consignment_code_without_flag" in codes


def test_delete_raises_before_touching_anything_when_refused(session):
    batch = make_batch(session, "A1")
    add_card(session, batch, n=1, status="sold", sold_price=1.0)
    session.commit()
    with pytest.raises(BatchDeleteRefused) as exc:
        delete_batch(session, batch.id, apply=True,
                     pusher=ok_push, reader=reader_for({}),
                     live_inventory_reader=lambda: [])
    assert exc.value.refusals
    session.rollback()
    assert session.query(InventoryCard).count() == 1


# --- the dry run ----------------------------------------------------------

def test_the_dry_run_writes_nothing(session):
    batch = make_batch(session, "A1")
    add_card(session, batch, n=1)
    add_card(session, batch, n=2)
    binding = add_binding(session, card_ids=[1, 2])
    session.commit()
    batch_id, binding_id = batch.id, binding.id

    result = delete_batch(session, batch_id, apply=False, pusher=ok_push,
                          reader=reader_for({"prod-1": 0}),
                          live_inventory_reader=lambda: [])
    session.commit()

    assert result["applied"] is False
    assert result["cards_deleted"] == 2
    assert session.query(Batch).filter_by(id=batch_id).count() == 1
    assert session.query(InventoryCard).filter_by(batch_id=batch_id).count() == 2
    assert json.loads(
        session.get(RemoteProductBinding, binding_id).local_card_ids_json) == [1, 2]


def test_the_dry_run_shows_the_exact_mana_pool_payload(session):
    batch = make_batch(session, "A1")
    add_card(session, batch, n=1)
    add_binding(session, card_ids=[1])
    session.commit()
    result = delete_batch(session, batch.id, apply=False, pusher=ok_push,
                          reader=reader_for({"prod-1": 0}),
                          live_inventory_reader=lambda: [])
    assert result["mana_pool_payload"] == [{
        "product_type": "mtg_single", "product_id": "prod-1",
        "price_cents": None, "quantity": 0,
    }]


def test_the_dry_run_makes_no_real_call(session):
    """The stub is the only thing invoked."""
    calls = []
    batch = make_batch(session, "A1")
    add_card(session, batch, n=1)
    add_binding(session, card_ids=[1])
    session.commit()

    def spy(updates):
        calls.append(updates)
        return ok_push(updates)

    delete_batch(session, batch.id, apply=False, pusher=spy,
                 reader=reader_for({"prod-1": 0}),
                 live_inventory_reader=lambda: [])
    assert len(calls) == 1


# --- the apply ------------------------------------------------------------

def test_the_apply_deletes_the_batch_and_its_rows(session):
    batch = make_batch(session, "A1")
    c1 = add_card(session, batch, n=1)
    c2 = add_card(session, batch, n=2)
    session.add(InventoryPriceHistory(inventory_card_id=c1.id, old_price=1.0, new_price=2.0))
    session.add(InventoryListingStatus(inventory_card_id=c1.id,
                                       listing_status="listed"))
    session.add(InventoryChangeLog(inventory_card_id=c1.id, change_summary="x"))
    add_binding(session, card_ids=[c1.id, c2.id])
    session.commit()
    batch_id = batch.id

    result = delete_batch(session, batch_id, apply=True, pusher=ok_push,
                          reader=reader_for({"prod-1": 0}),
                          live_inventory_reader=lambda: [])
    session.commit()

    assert result["applied"] is True
    assert session.query(Batch).filter_by(id=batch_id).count() == 0
    assert session.query(InventoryCard).count() == 0
    assert session.query(InventoryPriceHistory).count() == 0
    assert session.query(InventoryListingStatus).count() == 0
    assert session.query(ImportRecord).count() == 0


def test_change_logs_survive_the_delete(session):
    """Operator decision 6.1: they are append-only and the only record
    that money changed hands. They now point at a card id that no longer
    resolves, which is accepted."""
    batch = make_batch(session, "A1")
    card = add_card(session, batch, n=1)
    session.add(InventoryChangeLog(inventory_card_id=card.id,
                                   change_summary="sold for $5"))
    add_binding(session, card_ids=[card.id])
    session.commit()
    card_id = card.id

    delete_batch(session, batch.id, apply=True, pusher=ok_push,
                 reader=reader_for({"prod-1": 0}),
                 live_inventory_reader=lambda: [])
    session.commit()
    rows = session.query(InventoryChangeLog).all()
    assert len(rows) == 1
    assert rows[0].inventory_card_id == card_id


def test_binding_membership_is_rewritten_not_left_stale(session):
    """local_card_ids_json has no foreign key, so a deleted card stays
    "held" forever unless the delete rewrites it."""
    doomed = make_batch(session, "A1")
    keeper = make_batch(session, "A2")
    d1 = add_card(session, doomed, n=1)
    k1 = add_card(session, keeper, n=2)
    binding = add_binding(session, card_ids=[d1.id, k1.id])
    session.commit()
    binding_id = binding.id

    delete_batch(session, doomed.id, apply=True, pusher=ok_push,
                 reader=reader_for({"prod-1": 1}),
                 live_inventory_reader=lambda: [])
    session.commit()
    assert json.loads(
        session.get(RemoteProductBinding, binding_id).local_card_ids_json) == [k1.id]


def test_a_shared_identity_keeps_its_surviving_copy_listed(session):
    """★ THE RULE THAT MATTERS MOST. The other batch still stocks this
    printing, so the listing must be requantified to 1, never zeroed."""
    doomed = make_batch(session, "E4")
    other = make_batch(session, "leg_red")
    add_card(session, doomed, n=1, mtgjson="SHARED")
    add_card(session, other, n=2, mtgjson="SHARED")
    add_binding(session, product_id="prod-shared", mtgjson="SHARED")
    session.commit()

    sent = []

    def spy(updates):
        sent.extend(updates)
        return ok_push(updates)

    delete_batch(session, doomed.id, apply=True, pusher=spy,
                 reader=reader_for({"prod-shared": 1}),
                 live_inventory_reader=lambda: [])
    session.commit()
    assert len(sent) == 1
    assert sent[0]["quantity"] == 1, "must NOT be zeroed -- a copy survives"


def test_a_surviving_card_keeps_its_import_record(session):
    """A bulk move changes batch_id and leaves import_id, so 175 cards in
    production cite an import belonging to a different batch. Deleting
    that record would orphan them."""
    doomed = make_batch(session, "leg_land")
    other = make_batch(session, "leg_multi")
    add_card(session, doomed, n=1)
    moved = add_card(session, doomed, n=2)
    record_id = moved.import_id
    moved.batch_id = other.id
    add_binding(session, card_ids=[])
    session.commit()

    result = delete_batch(session, doomed.id, apply=True, pusher=ok_push,
                          reader=reader_for({"prod-1": 1}),
                          live_inventory_reader=lambda: [])
    session.commit()
    assert result["rows"]["import_records_kept_for_survivors"] == 1
    assert session.query(ImportRecord).filter_by(id=record_id).count() == 1
    assert session.get(InventoryCard, moved.id).import_id == record_id


def test_a_surviving_cards_removal_reference_is_cleared(session):
    doomed = make_batch(session, "A1")
    keeper = make_batch(session, "A2")
    gone = add_card(session, doomed, n=1)
    survivor = add_card(session, keeper, n=2)
    survivor.removal_related_inventory_card_id = gone.id
    add_binding(session, card_ids=[gone.id])
    session.commit()
    survivor_id = survivor.id

    delete_batch(session, doomed.id, apply=True, pusher=ok_push,
                 reader=reader_for({"prod-1": 1}),
                 live_inventory_reader=lambda: [])
    session.commit()
    assert session.get(InventoryCard, survivor_id).removal_related_inventory_card_id is None


# --- failure mid-push must roll back EVERYTHING ---------------------------

def _seed_for_failure(session):
    batch = make_batch(session, "A1")
    c1 = add_card(session, batch, n=1)
    session.add(InventoryPriceHistory(inventory_card_id=c1.id, old_price=1.0, new_price=2.0))
    binding = add_binding(session, card_ids=[c1.id])
    session.commit()
    return batch.id, binding.id, c1.id


def test_a_failed_push_rolls_the_whole_delete_back(session):
    """★ THE PROPERTY THAT MAKES THIS SAFE. Nothing local may be deleted
    for a card still live on Mana Pool."""
    batch_id, binding_id, card_id = _seed_for_failure(session)

    def boom(updates):
        raise RuntimeError("mana pool refused")

    with pytest.raises(BatchDeleteFailed) as exc:
        delete_batch(session, batch_id, apply=True, pusher=boom,
                     reader=reader_for({}), live_inventory_reader=lambda: [])
    session.commit()

    assert "mana pool refused" in str(exc.value)
    assert exc.value.failed_bindings == [
        {"binding_id": binding_id, "product_id": "prod-1"}]
    assert session.query(Batch).filter_by(id=batch_id).count() == 1
    assert session.query(InventoryCard).filter_by(id=card_id).count() == 1
    assert session.query(InventoryPriceHistory).count() == 1
    assert json.loads(
        session.get(RemoteProductBinding, binding_id).local_card_ids_json) == [card_id]


def test_a_skipped_item_counts_as_a_failure(session):
    """The bulk endpoint can answer 200 and still decline an item.
    Treating that as success is how a listing silently keeps advertising
    stock we no longer have."""
    batch_id, binding_id, card_id = _seed_for_failure(session)

    def skips(updates):
        return [{"inventory": [], "skipped": [
            {"product_id": "prod-1", "reason": "not_found"}]}]

    with pytest.raises(BatchDeleteFailed, match="skipped"):
        delete_batch(session, batch_id, apply=True, pusher=skips,
                     reader=reader_for({}), live_inventory_reader=lambda: [])
    session.commit()
    assert session.query(InventoryCard).filter_by(id=card_id).count() == 1


def test_a_read_back_that_disagrees_rolls_back(session):
    """A write that reports success and reads back wrong is a failure."""
    batch_id, binding_id, card_id = _seed_for_failure(session)

    with pytest.raises(BatchDeleteFailed, match="did not read back"):
        delete_batch(session, batch_id, apply=True, pusher=ok_push,
                     reader=reader_for({"prod-1": 99}),
                     live_inventory_reader=lambda: [])
    session.commit()
    assert session.query(Batch).filter_by(id=batch_id).count() == 1
    assert session.query(InventoryCard).filter_by(id=card_id).count() == 1


def test_a_failed_read_back_rolls_back(session):
    batch_id, _binding_id, card_id = _seed_for_failure(session)

    def boom(product_id, product_type="mtg_single"):
        raise RuntimeError("read timed out")

    with pytest.raises(BatchDeleteFailed, match="read"):
        delete_batch(session, batch_id, apply=True, pusher=ok_push,
                     reader=boom, live_inventory_reader=lambda: [])
    session.commit()
    assert session.query(InventoryCard).filter_by(id=card_id).count() == 1


def test_an_unexpected_error_also_rolls_back(session):
    batch_id, _binding_id, card_id = _seed_for_failure(session)

    def odd(product_id, product_type="mtg_single"):
        raise ZeroDivisionError("something else entirely")

    with pytest.raises(ZeroDivisionError):
        delete_batch(session, batch_id, apply=True, pusher=ok_push,
                     reader=odd, live_inventory_reader=lambda: [])
    session.commit()
    assert session.query(InventoryCard).filter_by(id=card_id).count() == 1
    assert session.query(Batch).filter_by(id=batch_id).count() == 1


# --- no blanket zero anywhere --------------------------------------------

def test_the_module_contains_no_blanket_zero(session):
    """Pinned by reading the source: a future edit must not reintroduce
    "set it to zero because the batch is gone"."""
    source = open("batch_delete_service.py").read()
    assert '"quantity": 0' not in source
    assert "quantity=0" not in source


# --- the screens ----------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    import main
    engine = create_engine(f"sqlite:///{tmp_path / 'routes.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    import inventory_sync_service
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    import manapool_service
    monkeypatch.setattr(manapool_service, "get_all_seller_inventory",
                        lambda **kw: [])
    from fastapi.testclient import TestClient
    return TestClient(main.app), engine


def test_the_report_offers_a_dry_run_only_when_nothing_refuses(client):
    http, engine = client
    with Session(engine) as s:
        ok = make_batch(s, "A1")
        add_card(s, ok, n=1)
        blocked = make_batch(s, "A2")
        add_card(s, blocked, n=1, status="sold", sold_price=1.0)
        s.commit()
        ok_id, blocked_id = ok.id, blocked.id

    good = http.get(f"/admin/delete-batch?batch_id={ok_id}").text
    assert "Dry-run a delete of this batch" in good

    bad = http.get(f"/admin/delete-batch?batch_id={blocked_id}").text
    assert "Dry-run a delete of this batch" not in bad
    assert "Not deletable yet" in bad


def test_the_confirm_screen_requires_the_dry_run_and_writes_nothing(client):
    http, engine = client
    with Session(engine) as s:
        batch = make_batch(s, "A1")
        add_card(s, batch, n=1)
        add_binding(s, card_ids=[1])
        s.commit()
        batch_id = batch.id

    page = http.post("/admin/delete-batch/confirm", data={"batch_id": batch_id})
    assert page.status_code == 200
    assert "Type <strong>A1</strong> to confirm" in page.text
    assert "cannot be undone" in page.text
    # the $ value, the counts and the listing count are all present
    assert "sold history" in page.text and "available stock" in page.text
    assert "Mana Pool listings requantified" in page.text

    with Session(engine) as s:
        assert s.query(Batch).filter_by(id=batch_id).count() == 1
        assert s.query(InventoryCard).count() == 1


def test_the_confirm_screen_refuses_a_batch_that_is_not_all_available(client):
    http, engine = client
    with Session(engine) as s:
        batch = make_batch(s, "A1")
        add_card(s, batch, n=1, status="sold", sold_price=1.0)
        s.commit()
        batch_id = batch.id
    page = http.post("/admin/delete-batch/confirm", data={"batch_id": batch_id})
    assert page.status_code == 409
    assert "cannot be deleted" in page.text


def test_apply_refuses_a_wrong_typed_name_and_deletes_nothing(client):
    http, engine = client
    with Session(engine) as s:
        batch = make_batch(s, "A1")
        add_card(s, batch, n=1)
        s.commit()
        batch_id = batch.id
    page = http.post("/admin/delete-batch/apply",
                     data={"batch_id": batch_id, "typed_batch_code": "WRONG"})
    assert page.status_code == 400
    assert "did not match" in page.text
    with Session(engine) as s:
        assert s.query(Batch).filter_by(id=batch_id).count() == 1
        assert s.query(InventoryCard).count() == 1
