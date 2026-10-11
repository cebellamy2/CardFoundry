"""Converting available NM cards to LP.

★ THIS IS THE OPERATION THAT CAUSED AN INCIDENT. A 2026-09-07 bulk
condition backfill moved 2,966 cards and told Mana Pool nothing; ~1,900
listings were left live with no cards behind them and six real orders
arrived against them. So the tests that matter most here are the ones
that prove the listing moves with the card, and that a failure converts
nothing.
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import (
    Base, Batch, FulfillmentException, ImportRecord, InventoryCard,
    InventoryChangeLog, InventoryListingStatus, OrderItem, PickAllocation,
    RemoteProductBinding, SalesOrder,
)
from nm_to_lp_conversion import (
    ConversionFailed, ConversionRefused, DEFAULT_CHUNK_SIZE, chunks,
    convert_chunk, eligible_cards, identity_key, refusals_for,
)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'nmlp.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def batch(session, code="A1", **over):
    b = Batch(batch_code=code, **over)
    session.add(b)
    session.flush()
    return b


def card(session, b, *, n=1, condition="NM", status="available", mtgjson="MTG-A"):
    rec = session.query(ImportRecord).filter_by(batch_id=b.id).first()
    if not rec:
        rec = ImportRecord(batch_id=b.id, filename="x.csv",
                           file_hash=f"h{b.id}", card_count=1)
        session.add(rec)
        session.flush()
    c = InventoryCard(
        batch_id=b.id, import_id=rec.id, name=f"Card {n}", set_code="ICE",
        collector_number=str(n), scryfall_id=f"sf-{b.id}-{n}",
        mtgjson_id=mtgjson, language_id="EN", condition_id=condition,
        finish_id="NF", condition="Near Mint" if condition == "NM" else "Lightly Played",
        finish="normal", status=status, current_price=1.0,
    )
    session.add(c)
    session.flush()
    return c


def binding(session, *, product_id="prod-nm", condition="NM", mtgjson="MTG-A", card_ids=()):
    from datetime import datetime as dt
    b = RemoteProductBinding(
        provider="manapool", product_type="mtg_single", product_id=product_id,
        local_card_ids_json=json.dumps(list(card_ids)),
        requested_identity_json=json.dumps({"name": "Card 1"}),
        scryfall_id="sf-x", mtgjson_id=mtgjson, language_id="EN",
        condition_id=condition, finish_id="NF", set_code="ICE",
        collector_number="1", binding_status="validated", validated_at=dt.now(),
        evidence_hash=f"ev-{product_id}", evidence_json="{}",
    )
    session.add(b)
    session.flush()
    return b


def ok_push(updates):
    return [{"inventory": [dict(u) for u in updates], "skipped": []}]


def reader_for(expected):
    def read(product_id, product_type="mtg_single"):
        return {"inventory": {"quantity": expected[product_id]}}
    return read


# --- scope ----------------------------------------------------------------

@pytest.mark.parametrize("status", ["reserved", "sold", "removed", "unsellable"])
def test_only_available_cards_are_eligible(session, status):
    """★ THE OPERATOR'S SCOPE. A reserved card is promised to a buyer at
    the identity they bought; a sold card's record is what the sale was."""
    b = batch(session)
    keep = card(session, b, n=1, status="available")
    card(session, b, n=2, status=status)
    session.commit()
    assert [c.id for c in eligible_cards(session)] == [keep.id]


def test_non_nm_cards_are_not_eligible(session):
    b = batch(session)
    nm = card(session, b, n=1, condition="NM")
    card(session, b, n=2, condition="LP")
    card(session, b, n=3, condition="MP")
    session.commit()
    assert [c.id for c in eligible_cards(session)] == [nm.id]


def test_an_archived_batch_is_excluded(session):
    """Its cards contribute nothing to any listing, so converting them
    would be a local relabel with no remote meaning -- and it would
    inflate every count the operator reads."""
    live = batch(session, "A1")
    archived = batch(session, "A2", is_archived=True)
    keep = card(session, live, n=1)
    card(session, archived, n=2)
    session.commit()
    assert [c.id for c in eligible_cards(session)] == [keep.id]


def test_eligible_order_is_stable_so_chunks_are_reproducible(session):
    b = batch(session)
    ids = [card(session, b, n=i).id for i in range(5)]
    session.commit()
    assert [c.id for c in eligible_cards(session)] == sorted(ids)


def test_chunking_packs_whole_identity_groups(session):
    b = batch(session)
    # two identities, three cards each
    a = [card(session, b, n=i, mtgjson="MTG-A") for i in range(3)]
    z = [card(session, b, n=10 + i, mtgjson="MTG-Z") for i in range(3)]
    session.commit()
    packed = chunks(a + z, 4)
    assert [len(c) for c in packed] == [3, 3], "a group must not be split to hit the size"
    assert chunks([], 2) == []
    assert DEFAULT_CHUNK_SIZE == 250


def test_no_identity_ever_spans_two_chunks(session):
    """★ THE TEST THAT MUST FAIL IF AN IDENTITY SPLITS. The first dry run
    reported 83 NM listings as non-zero purely because chunk N still saw
    chunk N+1's cards -- the preview disagreed with a cumulative apply."""
    b = batch(session)
    cards = []
    for ident in range(7):
        for copy in range(5):
            cards.append(card(session, b, n=ident * 10 + copy,
                              mtgjson=f"MTG-{ident}"))
    session.commit()

    for size in (1, 2, 3, 4, 7, 11, 35, 100):
        packed = chunks(cards, size)
        assert sum(len(c) for c in packed) == len(cards), size
        seen = {}
        for index, chunk in enumerate(packed):
            for c in chunk:
                key = identity_key(c)
                if key in seen:
                    assert seen[key] == index, (
                        f"identity {key} spans chunks {seen[key]} and {index} "
                        f"at size {size}"
                    )
                seen[key] = index


def test_one_identity_larger_than_the_chunk_size_gets_its_own_chunk(session):
    """Correctness beats the size target: the push is bulked, so an
    oversized chunk is still ~2 Mana Pool calls."""
    b = batch(session)
    cards = [card(session, b, n=i, mtgjson="MTG-BIG") for i in range(9)]
    session.commit()
    packed = chunks(cards, 4)
    assert [len(c) for c in packed] == [9]


def test_the_binding_map_is_built_in_one_pass(session):
    """★ SPEED. bindings_backing_card rescans all bindings per card --
    ~2.1M JSON parses per chunk in production, and 20.4 minutes for the
    first dry run. This asserts the map is built with a bounded number of
    statements rather than one scan per card."""
    from sqlalchemy import event
    from nm_to_lp_conversion import binding_map

    b = batch(session)
    # 12 cards but only THREE distinct identities, so a per-card shape and
    # a per-identity shape are clearly distinguishable.
    cards = []
    for ident in range(3):
        for copy in range(4):
            cards.append(card(session, b, n=ident * 10 + copy,
                              mtgjson=f"MTG-{ident}"))
    for ident in range(3):
        binding(session, product_id=f"p{ident}", mtgjson=f"MTG-{ident}",
                card_ids=[c.id for c in cards if c.mtgjson_id == f"MTG-{ident}"])
    session.commit()

    statements = []
    engine = session.get_bind()

    def record(conn, cursor, statement, *a):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        mapped = binding_map(session, cards)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert all(len(mapped[c.id]) == 1 for c in cards), mapped

    binding_queries = [q for q in statements if "remote_product_bindings" in q]
    # The membership pass is the only UNFILTERED read of the table. The
    # old per-card shape did one of these for every card.
    full_scans = [q for q in binding_queries if "upper(remote_product_bindings.mtgjson_id)" not in q]
    assert len(full_scans) == 1, (len(full_scans), full_scans[:2])

    # Identity lookups scale with DISTINCT IDENTITIES (3), not cards (12).
    lookups = [q for q in binding_queries if "upper(remote_product_bindings.mtgjson_id)" in q]
    assert len(lookups) == 3, (len(lookups), len(cards))
    assert len(lookups) < len(cards)


# --- refusals, not skips --------------------------------------------------

def test_a_card_that_gained_an_allocation_refuses_the_chunk(session):
    """★ REFUSE, DON'T SKIP: the counts the operator approved must stay
    the counts that execute."""
    b = batch(session)
    c1 = card(session, b, n=1)
    c2 = card(session, b, n=2)
    order = SalesOrder(external_order_id="o-1", source="manapool", status="ready_to_pick")
    session.add(order)
    session.flush()
    item = OrderItem(order_id=order.id, name=c2.name, quantity=1,
                     mtgjson_id=c2.mtgjson_id, language_id="EN",
                     condition_id="NM", finish_id="NF", price_cents=100)
    session.add(item)
    session.flush()
    session.add(PickAllocation(order_item_id=item.id, inventory_card_id=c2.id,
                               batch_id=b.id, status="allocated"))
    session.commit()

    found = [r for r in refusals_for(session, [c1, c2]) if r["code"] == "has_allocation"]
    assert found
    assert found[0]["card_ids"] == [c2.id]

    with pytest.raises(ConversionRefused) as exc:
        convert_chunk(session, [c1, c2], apply=True, pusher=ok_push,
                      reader=reader_for({}))
    assert c2.id in exc.value.card_ids
    session.rollback()
    assert session.get(InventoryCard, c1.id).condition_id == "NM"


def test_a_card_with_a_fulfillment_exception_refuses(session):
    b = batch(session)
    c1 = card(session, b, n=1)
    order = SalesOrder(external_order_id="o-1", source="manapool", status="needs_review")
    session.add(order)
    session.flush()
    item = OrderItem(order_id=order.id, name=c1.name, quantity=1,
                     mtgjson_id=c1.mtgjson_id, language_id="EN",
                     condition_id="NM", finish_id="NF", price_cents=100)
    session.add(item)
    session.flush()
    alloc = PickAllocation(order_item_id=item.id, inventory_card_id=c1.id,
                           batch_id=b.id, status="exception")
    session.add(alloc)
    session.flush()
    session.add(FulfillmentException(
        sales_order_id=order.id, order_item_id=item.id,
        pick_allocation_id=alloc.id, inventory_card_id=c1.id,
        exception_type="missing", note="x",
        inventory_resolution_state="unresolved"))
    session.commit()
    codes = [r["code"] for r in refusals_for(session, [c1])]
    assert "has_exception" in codes


def test_a_card_already_converted_refuses(session):
    """Re-running must not double-handle a card."""
    b = batch(session)
    c1 = card(session, b, n=1, condition="LP")
    session.commit()
    assert "not_nm" in [r["code"] for r in refusals_for(session, [c1])]


# --- the conversion -------------------------------------------------------

def test_a_chunk_converts_the_cards_and_moves_the_listing(session):
    b = batch(session)
    c1 = card(session, b, n=1)
    c2 = card(session, b, n=2)
    nm = binding(session, card_ids=[c1.id, c2.id])
    session.commit()

    sent = []

    def spy(updates):
        sent.extend(updates)
        return ok_push(updates)

    result = convert_chunk(session, [c1, c2], apply=True, pusher=spy,
                           reader=reader_for({"prod-nm": 0}))
    session.commit()

    assert session.get(InventoryCard, c1.id).condition_id == "LP"
    assert session.get(InventoryCard, c2.id).condition_id == "LP"
    assert result["old_bindings"] == 1
    assert result["read_back_ok"] is True
    # the OLD NM listing was requantified, not left standing
    assert len(sent) == 1
    assert sent[0]["product_id"] == "prod-nm"
    assert sent[0]["quantity"] == 0


def test_the_human_label_moves_with_the_code(session):
    """A packing slip must not say Near Mint for an LP card.

    ★ AND IT MUST BE A LABEL THIS APP CAN READ BACK. CONDITION_LABELS
    says "Lightly Played" for LP, which normalized_condition_id reads as
    LIGHTLY_PLAYED -- not LP. The intake vocabulary's "Light Play" does
    round-trip, so that is what gets written.
    """
    b = batch(session)
    c1 = card(session, b, n=1)
    binding(session, card_ids=[c1.id])
    session.commit()
    convert_chunk(session, [c1], apply=True, pusher=ok_push,
                  reader=reader_for({"prod-nm": 0}))
    session.commit()
    assert session.get(InventoryCard, c1.id).condition == "Light Play"
    from import_service import normalized_condition_id
    assert normalized_condition_id("Light Play") == "LP"
    assert normalized_condition_id("Lightly Played") != "LP"   # the trap


def test_a_shared_nm_listing_is_requantified_never_zeroed(session):
    """★ THE RULE THAT MATTERS MOST. One NM card converts, another stays
    NM because it is reserved. The NM listing must keep serving it."""
    b = batch(session)
    converting = card(session, b, n=1)
    staying = card(session, b, n=2, status="reserved")
    binding(session, card_ids=[converting.id, staying.id])
    session.commit()

    sent = []

    def spy(updates):
        sent.extend(updates)
        return ok_push(updates)

    # only the available one is eligible
    assert [c.id for c in eligible_cards(session)] == [converting.id]
    convert_chunk(session, [converting], apply=True, pusher=spy,
                  reader=reader_for({"prod-nm": 0}))
    session.commit()
    assert len(sent) == 1
    # the reserved card is NOT available, so desired quantity counts only
    # available cards -- 0 here -- but the point is it was RECOMPUTED, and
    # the reserved card itself is untouched.
    assert session.get(InventoryCard, staying.id).condition_id == "NM"
    assert session.get(InventoryCard, staying.id).status == "reserved"


def test_an_available_card_of_the_same_identity_keeps_the_listing_up(session):
    """Two available NM cards, only one in this chunk: the listing must be
    requantified to 1, not zeroed."""
    b = batch(session)
    c1 = card(session, b, n=1)
    c2 = card(session, b, n=2)
    binding(session, card_ids=[c1.id, c2.id])
    session.commit()

    sent = []

    def spy(updates):
        sent.extend(updates)
        return ok_push(updates)

    convert_chunk(session, [c1], apply=True, pusher=spy,
                  reader=reader_for({"prod-nm": 1}))
    session.commit()
    assert sent[0]["quantity"] == 1, "must NOT be zeroed -- c2 is still NM and available"


def test_each_converted_card_gets_an_audit_row(session):
    b = batch(session)
    c1 = card(session, b, n=1)
    binding(session, card_ids=[c1.id])
    session.commit()
    convert_chunk(session, [c1], apply=True, pusher=ok_push,
                  reader=reader_for({"prod-nm": 0}))
    session.commit()

    row = session.query(InventoryChangeLog).filter_by(inventory_card_id=c1.id).one()
    entry = json.loads(row.change_summary)
    assert entry["action_type"] == "condition_bulk_converted"
    assert entry["before"]["condition_id"] == "NM"
    assert entry["after"]["condition_id"] == "LP"
    assert entry["old_product_ids"] == ["prod-nm"]
    assert entry["reason"]
    assert row.actor == "script:nm_to_lp_conversion"


def test_the_stale_listing_status_is_cleared(session):
    """A "listed" row from before the move would be shown as current."""
    b = batch(session)
    c1 = card(session, b, n=1)
    binding(session, card_ids=[c1.id])
    session.add(InventoryListingStatus(inventory_card_id=c1.id,
                                       listing_status="listed"))
    session.commit()
    convert_chunk(session, [c1], apply=True, pusher=ok_push,
                  reader=reader_for({"prod-nm": 0}))
    session.commit()
    assert session.query(InventoryListingStatus).filter_by(
        inventory_card_id=c1.id).count() == 0


def test_a_card_backing_no_listing_still_converts(session):
    """Never listed, nothing to tell Mana Pool."""
    b = batch(session)
    c1 = card(session, b, n=1)
    session.commit()
    result = convert_chunk(session, [c1], apply=True, pusher=ok_push,
                           reader=reader_for({}))
    session.commit()
    assert result["old_bindings"] == 0
    assert session.get(InventoryCard, c1.id).condition_id == "LP"


# --- the dry run ----------------------------------------------------------

def test_the_dry_run_converts_nothing(session):
    b = batch(session)
    c1 = card(session, b, n=1)
    binding(session, card_ids=[c1.id])
    session.commit()
    result = convert_chunk(session, [c1], apply=False, pusher=ok_push,
                           reader=reader_for({"prod-nm": 0}))
    session.commit()
    assert result["applied"] is False
    assert session.get(InventoryCard, c1.id).condition_id == "NM"
    assert session.get(InventoryCard, c1.id).condition == "Near Mint"
    assert session.query(InventoryChangeLog).count() == 0


# --- failure converts nothing --------------------------------------------

def _seed_failure(session):
    b = batch(session)
    c1 = card(session, b, n=1)
    binding(session, card_ids=[c1.id])
    session.commit()
    return c1


def test_a_failed_push_converts_nothing(session):
    """★ Nothing local may change for a card whose listing we could not
    move. This is the 2026-09-07 incident, prevented."""
    c1 = _seed_failure(session)

    def boom(updates):
        raise RuntimeError("mana pool refused")

    with pytest.raises(ConversionFailed) as exc:
        convert_chunk(session, [c1], apply=True, pusher=boom,
                      reader=reader_for({}))
    session.commit()
    assert exc.value.chunk == [c1.id]
    assert exc.value.failed_bindings[0]["product_id"] == "prod-nm"
    assert session.get(InventoryCard, c1.id).condition_id == "NM"
    assert session.query(InventoryChangeLog).count() == 0


def test_a_skipped_item_converts_nothing(session):
    c1 = _seed_failure(session)

    def skips(updates):
        return [{"inventory": [], "skipped": [{"product_id": "prod-nm"}]}]

    with pytest.raises(ConversionFailed, match="skipped"):
        convert_chunk(session, [c1], apply=True, pusher=skips,
                      reader=reader_for({}))
    session.commit()
    assert session.get(InventoryCard, c1.id).condition_id == "NM"


def test_a_read_back_that_disagrees_converts_nothing(session):
    c1 = _seed_failure(session)
    with pytest.raises(ConversionFailed, match="did not read back"):
        convert_chunk(session, [c1], apply=True, pusher=ok_push,
                      reader=reader_for({"prod-nm": 99}))
    session.commit()
    assert session.get(InventoryCard, c1.id).condition_id == "NM"


def test_an_unexpected_error_converts_nothing(session):
    c1 = _seed_failure(session)

    def odd(product_id, product_type="mtg_single"):
        raise ZeroDivisionError("something else")

    with pytest.raises(ZeroDivisionError):
        convert_chunk(session, [c1], apply=True, pusher=ok_push, reader=odd)
    session.commit()
    assert session.get(InventoryCard, c1.id).condition_id == "NM"


def test_no_blanket_zero_in_the_module():
    """A quantity of 0 must only ever come from the recompute, never from
    a literal. `min_quantity=0` is a READ parameter and is not one -- the
    first version of this test matched it, which is why the boundary is
    explicit here."""
    import re

    source = open("nm_to_lp_conversion.py").read()
    assert '"quantity": 0' not in source
    writes = re.findall(r"(?<!min_)quantity\s*=\s*0\b", source)
    assert writes == [], writes


# --- the per-chunk runner: convert + publish, all or nothing -------------

def test_run_chunk_converts_then_publishes(session):
    from nm_to_lp_conversion import run_chunk

    b = batch(session)
    c1 = card(session, b, n=1)
    binding(session, card_ids=[c1.id])
    session.commit()

    seen = {}

    def publish(sess, cards):
        # by now the card must already be LP and flushed
        seen["condition"] = sess.get(InventoryCard, c1.id).condition_id
        return {"published": 1, "approved": 1, "backfill": {}}

    result = run_chunk(session, [c1], apply=True, pusher=ok_push,
                       reader=reader_for({"prod-nm": 0}), publish=publish)
    session.commit()
    assert seen["condition"] == "LP", "publish must see the converted state"
    assert result["publish"]["published"] == 1
    assert session.get(InventoryCard, c1.id).condition_id == "LP"


def test_a_failed_publish_rolls_back_the_conversion_too(session):
    """★ ALL OR NOTHING ACROSS BOTH HALVES. A chunk that converted but
    could not publish would leave cards LP with no LP listing and their
    NM listing already at zero -- invisible stock."""
    from nm_to_lp_conversion import run_chunk

    b = batch(session)
    c1 = card(session, b, n=1)
    binding(session, card_ids=[c1.id])
    session.commit()

    def boom(sess, cards):
        raise RuntimeError("publish refused")

    with pytest.raises(RuntimeError, match="publish refused"):
        run_chunk(session, [c1], apply=True, pusher=ok_push,
                  reader=reader_for({"prod-nm": 0}), publish=boom)
    session.commit()
    assert session.get(InventoryCard, c1.id).condition_id == "NM"
    assert session.query(InventoryChangeLog).count() == 0


def test_a_backfill_that_wants_to_write_stops_the_chunk(session):
    """★ SCOPE GATE. The publish plan runs the REAL MTGJSON backfill,
    which works on the whole inventory and mutates the session we are
    about to commit. The operator approved a condition conversion, not an
    identity backfill, so any backfill write stops the run."""
    from nm_to_lp_conversion import BackfillScopeExceeded, run_chunk

    b = batch(session)
    c1 = card(session, b, n=1)
    binding(session, card_ids=[c1.id])
    session.commit()

    def publish(sess, cards):
        return {"published": 1, "backfill": {"updated_inventory_cards": 3}}

    with pytest.raises(BackfillScopeExceeded, match="3 row"):
        run_chunk(session, [c1], apply=True, pusher=ok_push,
                  reader=reader_for({"prod-nm": 0}), publish=publish)
    session.commit()
    assert session.get(InventoryCard, c1.id).condition_id == "NM"


def test_a_zero_backfill_is_allowed(session):
    from nm_to_lp_conversion import run_chunk

    b = batch(session)
    c1 = card(session, b, n=1)
    binding(session, card_ids=[c1.id])
    session.commit()

    def publish(sess, cards):
        return {"published": 1,
                "backfill": {"updated_inventory_cards": 0, "updated_bindings": 0}}

    run_chunk(session, [c1], apply=True, pusher=ok_push,
              reader=reader_for({"prod-nm": 0}), publish=publish)
    session.commit()
    assert session.get(InventoryCard, c1.id).condition_id == "LP"


def test_run_chunk_dry_run_writes_nothing(session):
    from nm_to_lp_conversion import run_chunk

    b = batch(session)
    c1 = card(session, b, n=1)
    binding(session, card_ids=[c1.id])
    session.commit()
    run_chunk(session, [c1], apply=False, pusher=ok_push,
              reader=reader_for({"prod-nm": 0}),
              publish=lambda sess, cards: {"published": 1, "backfill": {}})
    session.commit()
    assert session.get(InventoryCard, c1.id).condition_id == "NM"
