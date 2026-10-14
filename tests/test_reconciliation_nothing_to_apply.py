"""The reconciliation chain no longer stops publishing.

★ WHAT WENT WRONG. `extract_reconciliation_candidates` built its candidate
dicts without `local_contributing_card_ids`, even though the mirror row it
reads carries that key. The apply passes
`row["local_contributing_card_ids"]` into `_fresh_desired_quantity` to
reach its fallback for an identity it cannot otherwise resolve -- so that
argument was ALWAYS None and the fallback was unreachable from the cron
path. A non-English row with no validated binding therefore recomputed
desired = 0, which excluded the row, which emptied `updates`, which RAISED
and failed the whole perform-sync route closed. The new-listing preview was
never built, so nothing published.

Measured on production: the only eligible row in jobs 530 and 543 was The
Ozolith (JA/LP/NF, cards 11598/11599), and 5 of the 10 historic failures
were a single-row preview of exactly that kind. On 2026-10-12 it left 882
newly imported cards unlisted for about six hours.

v2.41.1 shipped the half that removes the whole CLASS of failure:
  (b) an empty valid set is a clean NO-OP, not a raise, so the chain
      continues whatever emptied it;
  (c) a message that says how many rows there were, how many were skipped,
      and why.

The root-cause fix (a) -- carrying the contributing ids -- is now here too,
together with the DECREASE FLOOR the operator made its condition. Making
these listings non-zero is what puts the same unresolvable identity into
decrease shape after its first sale, and in that direction the recomputed
quantity is written straight onto the live listing. The floor is the count
of cards the mirror attributes to the row that are still locally sellable;
a decrease for an unresolvable identity can never write below it, and one
that can count nothing at all is refused rather than written as a 0.

Every test here goes through the REAL preview -> apply shape: the candidate
dict comes from extract_reconciliation_candidates, never hand-written, so
a test cannot pass on a row shape production never produces. That is
precisely how the original defect survived -- the existing fixtures set
`local_contributing_card_ids` by hand.
"""
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from inventory_reconciliation_service import (
    APPLIED, NOTHING_TO_APPLY, apply_reconciliation_preview,
    build_reconciliation_preview, extract_reconciliation_candidates,
)
from models import Base, Batch, InventoryCard, RemoteProductBinding


# The Ozolith's real production identity: Japanese, so the four-key query
# cannot resolve it, and no validated binding exists for it.
JA_IDENTITY = {
    "mtgjson_id": "A5308950-4946-5CF2-808D-26E644D9F30B",
    "language_id": "JA", "condition_id": "LP", "finish_id": "NF",
}
PRODUCT_ID = "0256cd6d-2458-4f26-89d6-687a074c703f"


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recon_noop.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def add_batch(session, code="CON_RIC1"):
    batch = Batch(batch_code=code)
    session.add(batch)
    session.flush()
    return batch


def add_ozolith(session, batch, *, status="available", price=68.76):
    card = InventoryCard(
        batch_id=batch.id, name="The Ozolith", set_code="IKO",
        collector_number="237",
        mtgjson_id=JA_IDENTITY["mtgjson_id"].lower(),
        language_id="JA", condition_id="LP", finish_id="NF",
        condition="Light Play", finish="normal",
        scryfall_id="d0c145b2-7e72-47dd-8483-b7557526a5a6",
        status=status, current_price=price, imported_at=datetime(2026, 8, 1),
    )
    session.add(card)
    session.flush()
    return card


def mirror_increase_row(card_ids, *, desired, remote):
    """A mirror row in the shape inventory_mirror_service really emits."""
    return {
        "category": "increase_quantity",
        "canonical_identity": dict(JA_IDENTITY),
        "name": "The Ozolith",
        "local_contributing_card_ids": sorted(card_ids),
        "desired_quantity": desired,
        "current_remote_quantity": remote,
        "effective_as_of": "2026-06-11T14:07:14.104Z",
        "remote_product_id": PRODUCT_ID,
    }



def add_english(session, batch, *, mtgjson, scryfall, status="available", price=1.0):
    card = InventoryCard(
        batch_id=batch.id, name="Alpha", set_code="ONE", collector_number="1",
        mtgjson_id=mtgjson, language_id="EN", condition_id="LP",
        finish_id="NF", condition="near_mint", finish="normal",
        scryfall_id=scryfall, status=status, current_price=price,
        imported_at=datetime(2026, 8, 1),
    )
    session.add(card)
    session.flush()
    return card


def english_increase_row(card_ids, mtgjson, product_id, *, desired, remote):
    return {
        "category": "increase_quantity",
        "canonical_identity": {
            "mtgjson_id": mtgjson, "language_id": "EN",
            "condition_id": "LP", "finish_id": "NF",
        },
        "name": "Alpha",
        "local_contributing_card_ids": sorted(card_ids),
        "desired_quantity": desired,
        "current_remote_quantity": remote,
        "effective_as_of": "2026-08-01T00:00:00Z",
        "remote_product_id": product_id,
    }


def real_preview(session, mirror_rows):
    """Through the REAL preview builder, not a hand-made dict."""
    return build_reconciliation_preview(session, {"rows": mirror_rows})


def orders_loader(since=None):
    return {"orders": []}


def detail_loader(order_id):
    return {}


def seller(quantity, product_id=PRODUCT_ID):
    return lambda min_quantity: [
        {"product_id": product_id, "quantity": quantity}
    ]


def mirror_decrease_row(card_ids, *, desired, remote):
    """A DECREASE mirror row in the shape the mirror really emits.

    The mirror names every card it attributed to the row; which of them
    are still sellable is counted fresh at apply time, which is what
    makes the floor a floor rather than a stale number.
    """
    return {
        "category": "decrease_quantity",
        "canonical_identity": dict(JA_IDENTITY),
        "name": "The Ozolith",
        "local_contributing_card_ids": sorted(card_ids),
        "desired_quantity": desired,
        "current_remote_quantity": remote,
        "effective_as_of": "2026-06-11T14:07:14.104Z",
        "remote_product_id": PRODUCT_ID,
    }


def english_decrease_row(card_ids, mtgjson, product_id, *, desired, remote):
    return {
        "category": "decrease_quantity",
        "canonical_identity": {
            "mtgjson_id": mtgjson, "language_id": "EN",
            "condition_id": "LP", "finish_id": "NF",
        },
        "name": "Alpha",
        "local_contributing_card_ids": sorted(card_ids),
        "desired_quantity": desired,
        "current_remote_quantity": remote,
        "effective_as_of": "2026-08-01T00:00:00Z",
        "remote_product_id": product_id,
    }


def apply_through_real_path(session, mirror_rows, writer, seller_loader):
    """extract -> preview -> apply, exactly as the perform-sync chain does."""
    return apply_reconciliation_preview(
        session, real_preview(session, mirror_rows),
        orders_loader, detail_loader, "2026-01-01T00:00:00Z",
        seller_loader, writer,
    )


class Writer:
    """Records what would be written. Mirrors the real chunked shape."""

    def __init__(self):
        self.calls = []

    def __call__(self, updates):
        self.calls.append([dict(u) for u in updates])
        return [{"inventory": [dict(u) for u in updates], "skipped": []}]

    @property
    def written(self):
        return [u for call in self.calls for u in call]


# --- the decrease floor -------------------------------------------------
#
# Each test states the quantity Mana Pool must end up holding. None of
# them asserts how the number was reached: no call counts, no helper
# names, no internal flags. The operator's interest is the quantity on
# the listing, so that is what is pinned.


def test_a_decrease_row_now_carries_the_contributing_card_ids(session):
    """The inversion of this file's earlier assertion.

    It previously asserted the key was ABSENT from decrease rows, on the
    parked reasoning that giving them the fallback "would let an
    unresolvable identity write a 0 over a live listing". That was
    backwards: WITHOUT the ids the fallback already returns 0 and this
    direction writes it. Carrying them is what raises that 0 to the real
    count. The outcome tests below are the proof; this one just pins the
    row shape they depend on."""
    batch = add_batch(session)
    first = add_ozolith(session, batch)
    second = add_ozolith(session, batch)
    candidates, _ = extract_reconciliation_candidates(
        session,
        {"rows": [mirror_decrease_row([first.id, second.id], desired=1, remote=2)]},
    )
    assert len(candidates) == 1
    assert candidates[0]["direction"] == "decrease"
    assert candidates[0]["local_contributing_card_ids"] == [first.id, second.id]


def test_one_sale_writes_one_not_zero(session):
    """★ THE HAZARD THIS FLOOR EXISTS FOR, NOW AN OUTCOME.

    The Ozolith's two JA/LP/NF cards are listed at 2. One sells. Mana
    Pool must be told 1 -- the copy still on the shelf. Before the floor
    this wrote 0, silently delisting a card that was for sale, because
    the identity is Japanese with no validated binding so the four-key
    query returned 0 and a 0 is written in this direction."""
    batch = add_batch(session)
    kept = add_ozolith(session, batch)
    sold = add_ozolith(session, batch, status="sold")
    writer = Writer()

    result = apply_through_real_path(
        session,
        [mirror_decrease_row([kept.id, sold.id], desired=1, remote=2)],
        writer, seller(2),
    )

    assert result["outcome"] == APPLIED
    assert [u["quantity"] for u in writer.written] == [1], (
        "Mana Pool must be left holding the one copy still available"
    )
    assert [u["product_id"] for u in writer.written] == [PRODUCT_ID]


def test_both_sold_still_writes_zero_because_zero_is_now_the_truth(session):
    """The floor is a floor, not a refusal to ever reach 0.

    When every traceable card really is gone, 0 is the honest number and
    the listing must come down. A floor that blocked this would leave
    sold-out cards for sale."""
    batch = add_batch(session)
    first = add_ozolith(session, batch, status="sold")
    second = add_ozolith(session, batch, status="sold")
    writer = Writer()

    result = apply_through_real_path(
        session,
        [mirror_decrease_row([first.id, second.id], desired=0, remote=2)],
        writer, seller(2),
    )

    assert result["outcome"] == APPLIED
    assert [u["quantity"] for u in writer.written] == [0]


def test_an_identity_that_can_count_nothing_is_not_written_at_all(session):
    """The never-zero rule. An unresolvable identity naming no cards has
    no defensible quantity, so the listing keeps whatever it holds rather
    than being quietly zeroed."""
    batch = add_batch(session)
    add_ozolith(session, batch)
    writer = Writer()

    result = apply_through_real_path(
        session,
        [mirror_decrease_row([], desired=0, remote=2)],
        writer, seller(2),
    )

    assert writer.written == [], "no quantity may be written for an uncountable identity"
    assert result["outcome"] == NOTHING_TO_APPLY
    assert "Cannot count this identity's local stock" in result["message"]


def test_a_real_english_decrease_is_unchanged(session):
    """English rows carry a real MTGJSON id, which is what the mirror
    matched the local cards on, so their four-key count is authoritative
    and the floor must keep its hands off it. Two of three sold -> 1."""
    batch = add_batch(session, code="A1")
    product = "11111111-2222-3333-4444-555555555555"
    mtgjson = "0F0F0F0F-1111-2222-3333-444444444444"
    kept = add_english(session, batch, mtgjson=mtgjson.lower(), scryfall="s-1")
    gone_a = add_english(session, batch, mtgjson=mtgjson.lower(), scryfall="s-2", status="sold")
    gone_b = add_english(session, batch, mtgjson=mtgjson.lower(), scryfall="s-3", status="sold")
    writer = Writer()

    result = apply_through_real_path(
        session,
        [english_decrease_row(
            [kept.id, gone_a.id, gone_b.id], mtgjson, product,
            desired=1, remote=3,
        )],
        writer, seller(3, product_id=product),
    )

    assert result["outcome"] == APPLIED
    assert [u["quantity"] for u in writer.written] == [1]


def test_an_english_decrease_to_zero_still_reaches_zero(session):
    """The English path's 0 is trustworthy and must still be written --
    the floor is scoped to identities whose count cannot be trusted."""
    batch = add_batch(session, code="A1")
    product = "11111111-2222-3333-4444-555555555555"
    mtgjson = "0F0F0F0F-1111-2222-3333-444444444444"
    gone = add_english(session, batch, mtgjson=mtgjson.lower(), scryfall="s-1", status="sold")
    writer = Writer()

    result = apply_through_real_path(
        session,
        [english_decrease_row([gone.id], mtgjson, product, desired=0, remote=2)],
        writer, seller(2, product_id=product),
    )

    assert result["outcome"] == APPLIED
    assert [u["quantity"] for u in writer.written] == [0]


def test_one_sale_writes_one_via_the_binding_the_increase_created(session):
    """★ WHAT PRODUCTION WILL ACTUALLY DO, proven against the live DB in
    a rolled-back transaction on 2026-10-14.

    The increase does not only raise the listing -- it creates a
    validated RemoteProductBinding for the identity
    (_ensure_binding_for_increase). So by the time the first sale arrives,
    the identity is no longer unresolvable: _fresh_desired_quantity takes
    its BINDING branch and the floor never fires.

    That makes the post-sale quantity depend on a second, different code
    path from the one test_one_sale_writes_one_not_zero covers, and both
    have to be right. This pins the binding path at the same outcome: one
    copy left on the shelf means Mana Pool is told 1.
    """
    batch = add_batch(session)
    kept = add_ozolith(session, batch)
    sold = add_ozolith(session, batch, status="sold")
    session.add(RemoteProductBinding(
        provider="manapool", product_type="mtg_single", product_id=PRODUCT_ID,
        scryfall_id="d0c145b2-7e72-47dd-8483-b7557526a5a6",
        mtgjson_id=JA_IDENTITY["mtgjson_id"], language_id="JA",
        condition_id="LP", finish_id="NF", set_code="IKO",
        collector_number="237", binding_status="validated",
        validated_at=datetime(2026, 10, 14),
        # Exactly what _ensure_binding_for_increase records: the gap cards
        # the increase was justified by. Deliberately the STALE pair -- the
        # binding is written once and never revised, so by the time the
        # sale lands this list still names the sold card. The quantity must
        # come from the identity, not from this membership list.
        local_card_ids_json=json.dumps(sorted([kept.id, sold.id])),
        requested_identity_json=json.dumps({"mtgjson_id": JA_IDENTITY["mtgjson_id"]}),
        evidence_hash="t" * 64,
        evidence_json=json.dumps({"matched_via": "reconciliation_increase"}),
    ))
    session.flush()
    writer = Writer()

    result = apply_through_real_path(
        session,
        [mirror_decrease_row([kept.id, sold.id], desired=1, remote=2)],
        writer, seller(2),
    )

    assert result["outcome"] == APPLIED
    assert [u["quantity"] for u in writer.written] == [1]


# --- fix (a): the increase that was blocked ------------------------------


def test_the_blocked_increase_now_writes_the_real_quantity(session):
    """The production case. Two available JA/LP/NF Ozoliths, listing at
    0. Mana Pool must be told 2. Before (a) the recomputed desired
    quantity was 0, so the row was excluded and the listing stayed at 0
    -- the cards were never for sale."""
    batch = add_batch(session)
    first = add_ozolith(session, batch)
    second = add_ozolith(session, batch)
    writer = Writer()

    result = apply_through_real_path(
        session,
        [mirror_increase_row([first.id, second.id], desired=2, remote=0)],
        writer, seller(0),
    )

    assert result["outcome"] == APPLIED
    assert [u["quantity"] for u in writer.written] == [2]



def test_a_decrease_with_a_resolvable_identity_still_writes_normally(session):
    """Decrease behaviour must be unchanged, not merely safe. An English
    identity resolves through the four-key query as it always did."""
    batch = add_batch(session)
    card = InventoryCard(
        batch_id=batch.id, name="Alpha", set_code="ONE", collector_number="1",
        mtgjson_id="MTG-ALPHA", language_id="EN", condition_id="LP",
        finish_id="NF", condition="near_mint", finish="normal",
        scryfall_id="sf-alpha", status="available", current_price=1.0,
        imported_at=datetime(2026, 8, 1),
    )
    session.add(card)
    session.flush()
    candidates, _ = extract_reconciliation_candidates(
        session,
        {"rows": [{
            "category": "decrease_quantity",
            "canonical_identity": {
                "mtgjson_id": "MTG-ALPHA", "language_id": "EN",
                "condition_id": "LP", "finish_id": "NF",
            },
            "name": "Alpha", "local_contributing_card_ids": [card.id],
            "desired_quantity": 1, "current_remote_quantity": 4,
            "effective_as_of": "2026-08-01T00:00:00Z",
            "remote_product_id": "mp-alpha",
        }]},
    )
    writer = Writer()
    result = apply_reconciliation_preview(
        session, {"rows": [{**candidates[0], "status": "eligible"}]},
        orders_loader, detail_loader, "2026-01-01T00:00:00Z",
        lambda min_quantity: [{"product_id": "mp-alpha", "quantity": 4}],
        writer,
    )
    assert result["outcome"] == APPLIED
    assert writer.written[0]["quantity"] == 1, "one available card locally"
    assert writer.written[0]["price_cents"] is None, "quantity-only, never price"


# --- (b) nothing to apply is a no-op ------------------------------------

def test_an_empty_valid_set_no_ops_instead_of_raising(session):
    batch = add_batch(session)
    card = add_ozolith(session, batch)
    preview = real_preview(
        session, [mirror_increase_row([card.id], desired=1, remote=0)],
    )
    card.status = "sold"
    session.flush()

    writer = Writer()
    result = apply_reconciliation_preview(
        session, preview, orders_loader, detail_loader, "2026-01-01T00:00:00Z",
        seller(0), writer,
    )
    assert result["outcome"] == NOTHING_TO_APPLY
    assert result["updates"] == []
    assert writer.calls == [], "the writer must never be called at all"


def test_the_message_says_how_many_and_why(session):
    """★ (c). The old message -- "None of the reviewed rows are still valid
    to reconcile" -- read as a mass invalidation and cost real diagnosis
    time when the truth was "the single row was skipped"."""
    batch = add_batch(session)
    card = add_ozolith(session, batch)
    preview = real_preview(
        session, [mirror_increase_row([card.id], desired=1, remote=0)],
    )
    card.status = "sold"
    session.flush()

    result = apply_reconciliation_preview(
        session, preview, orders_loader, detail_loader, "2026-01-01T00:00:00Z",
        seller(0), Writer(),
    )
    message = result["message"]
    assert "1 row(s) were reviewed" in message
    assert "all 1 were skipped" in message
    assert "Traced cards are no longer locally available" in message
    assert "None of the reviewed rows" not in message, "the old wording is gone"
    assert result["reviewed"] == 1 and result["skipped"] == 1
    assert result["skipped_reasons"] == {
        "Traced cards are no longer locally available": 1,
    }


def test_a_no_op_logs_through_the_cardfoundry_logger(session, caplog):
    """The cardfoundry logger does NOT propagate, so caplog needs
    attaching or this passes vacuously."""
    import logging

    batch = add_batch(session)
    card = add_ozolith(session, batch)
    preview = real_preview(
        session, [mirror_increase_row([card.id], desired=1, remote=0)],
    )
    card.status = "sold"
    session.flush()

    cardfoundry = logging.getLogger("cardfoundry")
    cardfoundry.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="cardfoundry"):
            apply_reconciliation_preview(
                session, preview, orders_loader, detail_loader,
                "2026-01-01T00:00:00Z", seller(0), Writer(),
            )
    finally:
        cardfoundry.removeHandler(caplog.handler)

    lines = [r.getMessage() for r in caplog.records]
    assert any("nothing to apply" in line for line in lines), lines
    assert any("The chain continues" in line for line in lines), lines


def test_a_still_valid_sibling_is_written_even_when_others_are_skipped(session):
    """Per-row isolation, which the raise used to undo at the aggregate
    level. One good row must still be written.

    Both rows are ENGLISH here on purpose: without fix (a) a non-English
    row is never resolvable, so using one would test the held fix rather
    than this one.
    """
    batch = add_batch(session)
    good = add_english(session, batch, mtgjson="MTG-GOOD", scryfall="sf-good")
    doomed = add_english(session, batch, mtgjson="MTG-DOOMED", scryfall="sf-doomed")
    preview = real_preview(session, [
        english_increase_row([good.id], "MTG-GOOD", "mp-good", desired=1, remote=0),
        english_increase_row([doomed.id], "MTG-DOOMED", "mp-doomed", desired=1, remote=0),
    ])
    assert len([r for r in preview["rows"] if r["status"] == "eligible"]) == 2

    doomed.status = "sold"
    session.flush()

    writer = Writer()
    result = apply_reconciliation_preview(
        session, preview, orders_loader, detail_loader, "2026-01-01T00:00:00Z",
        lambda min_quantity: [
            {"product_id": "mp-good", "quantity": 0},
            {"product_id": "mp-doomed", "quantity": 0},
        ],
        writer,
    )
    assert result["outcome"] == APPLIED
    assert [u["product_id"] for u in writer.written] == ["mp-good"]
    assert len(result["excluded"]) == 1


# --- a real failure still fires the alarm -------------------------------

def test_a_mana_pool_error_still_propagates(session):
    """★ THE ALARM MUST STILL FIRE. Job 447 failed on a Mana Pool 500
    during the order read. That must still raise out of here so the route
    records a failed perform_sync_attempt."""
    import httpx

    batch = add_batch(session)
    card = add_ozolith(session, batch)
    preview = real_preview(
        session, [mirror_increase_row([card.id], desired=1, remote=0)],
    )

    def exploding_orders(since=None):
        raise httpx.HTTPError("Server error '500 Internal Server Error'")

    with pytest.raises(httpx.HTTPError):
        apply_reconciliation_preview(
            session, preview, exploding_orders, detail_loader,
            "2026-01-01T00:00:00Z", seller(0), Writer(),
        )


def test_a_failing_write_still_propagates(session):
    """A VALID row is needed to reach the writer at all -- so this uses an
    English identity, which resolves without fix (a). With only an
    unresolvable row the no-op returns first and the writer is never
    called, which is correct but would not test anything."""
    batch = add_batch(session)
    card = add_english(session, batch, mtgjson="MTG-GOOD", scryfall="sf-good")
    preview = real_preview(
        session,
        [english_increase_row([card.id], "MTG-GOOD", "mp-good",
                              desired=1, remote=0)],
    )

    def exploding_writer(updates):
        raise RuntimeError("Mana Pool rejected the write")

    with pytest.raises(RuntimeError):
        apply_reconciliation_preview(
            session, preview, orders_loader, detail_loader,
            "2026-01-01T00:00:00Z",
            lambda min_quantity: [{"product_id": "mp-good", "quantity": 0}],
            exploding_writer,
        )


def test_a_row_mana_pool_has_already_caught_up_on_no_ops_and_the_chain_continues(session):
    """★ (b)'s PROPERTY, on a row that genuinely still gets excluded.

    INVERSION RECORDED: this test used to use the Ozolith increase row,
    because without fix (a) that row recomputed desired = 0 and excluded
    itself. Fix (a) is now in, so the Ozolith WRITES 2 (see
    test_the_blocked_increase_now_writes_the_real_quantity) and can no
    longer stand in for an empty valid set.

    The property under test is unchanged and still matters: when every
    eligible row is independently excluded at apply time, that is a clean
    no-op and the caller carries on. It used to RAISE, which failed the
    whole perform-sync route closed so the new-listing preview was never
    built and NOTHING published -- 10 times, and on 2026-10-12 it left 882
    cards unlisted for about six hours.

    The exclusion used here is real: the preview saw Mana Pool holding 0,
    and by the time the apply re-reads it fresh the listing already holds
    2, so there is nothing left to raise.
    """
    batch = add_batch(session)
    first = add_ozolith(session, batch)
    second = add_ozolith(session, batch)
    preview = real_preview(
        session, [mirror_increase_row([first.id, second.id], desired=2, remote=0)],
    )
    assert len([r for r in preview["rows"] if r["status"] == "eligible"]) == 1

    writer = Writer()
    result = apply_reconciliation_preview(
        session, preview, orders_loader, detail_loader, "2026-01-01T00:00:00Z",
        seller(2), writer,
    )

    assert result["outcome"] == NOTHING_TO_APPLY, "a no-op, not an exception"
    assert writer.calls == [], "and no Mana Pool write at all"
    assert result["reviewed"] == 1
    assert result["skipped"] == 1
    assert "Mana Pool quantity already reflects this increase" in result["message"]
