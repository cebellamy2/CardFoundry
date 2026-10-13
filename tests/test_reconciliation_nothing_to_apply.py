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

SHIPPED HERE is the half that removes the whole CLASS of failure:
  (b) an empty valid set is a clean NO-OP, not a raise, so the chain
      continues whatever emptied it;
  (c) a message that says how many rows there were, how many were skipped,
      and why.

The root-cause fix -- (a), carrying the contributing ids on increase rows
-- is PARKED on the branch hold/reconciliation-increase-ids (operator
decision, 2026-10-13). It returns with a decrease floor, because making
these listings non-zero is what puts the same unresolvable identity into
decrease shape after its first sale, where a 0 IS written. So the Ozolith
stays unlisted for now, but it can no longer stop anything else from
publishing.

Every test here goes through the REAL preview -> apply shape: the candidate
dict comes from extract_reconciliation_candidates, never hand-written, so
a test cannot pass on a row shape production never produces. That is
precisely how the original defect survived -- the existing fixtures set
`local_contributing_card_ids` by hand.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from inventory_reconciliation_service import (
    APPLIED, NOTHING_TO_APPLY, apply_reconciliation_preview,
    build_reconciliation_preview, extract_reconciliation_candidates,
)
from models import Base, Batch, InventoryCard


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


def seller(quantity):
    return lambda min_quantity: [
        {"product_id": PRODUCT_ID, "quantity": quantity}
    ]


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


# --- the never-zero rule: decrease is untouched --------------------------

def test_a_decrease_row_still_carries_no_card_ids(session):
    """A decrease row carries no card ids, so _fresh_desired_quantity's
    fallback stays out of its reach.

    True today without fix (a), and it must STAY true when (a) returns:
    a decrease row's write quantity IS the recomputed desired quantity,
    so handing it that fallback would change what gets written to a live
    listing. (a) comes back with a decrease floor for exactly that
    reason."""
    batch = add_batch(session)
    add_ozolith(session, batch)
    candidates, _ = extract_reconciliation_candidates(
        session,
        {"rows": [{
            "category": "decrease_quantity",
            "canonical_identity": dict(JA_IDENTITY),
            "name": "The Ozolith",
            "local_contributing_card_ids": [1],
            "desired_quantity": 0,
            "current_remote_quantity": 5,
            "effective_as_of": "2026-06-11T14:07:14.104Z",
            "remote_product_id": PRODUCT_ID,
        }]},
    )
    assert len(candidates) == 1
    row = candidates[0]
    assert row["direction"] == "decrease"
    assert "local_contributing_card_ids" not in row, (
        "fix (a) must not reach decrease rows"
    )
    assert "gap_card_ids" not in row


def test_KNOWN_HAZARD_a_decrease_for_an_unresolvable_identity_writes_zero(session):
    """★ A PRE-EXISTING DESTRUCTIVE WRITE, FOUND BY THIS TEST AND NOT FIXED
    HERE. It is asserted so it cannot change unnoticed, NOT because it is
    acceptable.

    _fresh_desired_quantity's own docstring already warns about it:
    "decrease / zero_candidate -- write_quantity IS this number, and the
    row is excluded only when write >= fresh_remote. A wrong 0 against a
    remote quantity of 1+ is therefore WRITTEN, zeroing a listing that
    should have held stock."

    This test proves the hazard is live: an unresolvable identity recomputes
    desired = 0, the gate `0 >= 5` is False, and 0 is written over a listing
    holding 5.

    Nothing in fix (a) or (b) causes or worsens this, and the ticket scoped
    decrease behaviour to "exactly as it is today" -- so it stays. But note
    the interaction: once (a) makes these listings non-zero, a single sale
    puts the same identity into DECREASE shape, where this fires. Measured
    2026-10-13: all three affected identities (The Ozolith, Spell Pierce,
    Preordain) are in increase shape today, so it is latent, not live.
    """
    batch = add_batch(session)
    add_ozolith(session, batch)
    candidates, _ = extract_reconciliation_candidates(
        session,
        {"rows": [{
            "category": "decrease_quantity",
            "canonical_identity": dict(JA_IDENTITY),
            "name": "The Ozolith",
            "local_contributing_card_ids": [1],
            "desired_quantity": 0,
            "current_remote_quantity": 5,
            "effective_as_of": "2026-06-11T14:07:14.104Z",
            "remote_product_id": PRODUCT_ID,
        }]},
    )
    writer = Writer()
    apply_reconciliation_preview(
        session, {"rows": [{**candidates[0], "status": "eligible"}]},
        orders_loader, detail_loader, "2026-01-01T00:00:00Z",
        seller(5), writer,
    )
    assert writer.written == [{
        "product_type": "mtg_single", "product_id": PRODUCT_ID,
        "price_cents": None, "quantity": 0,
    }], (
        "DOCUMENTING the known hazard: a 0 derived from an unresolvable "
        "identity is written over a listing holding 5. If this assertion "
        "ever fails because the write became non-zero, the hazard was fixed "
        "and this test should be retired."
    )


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


def test_the_ozolith_row_no_ops_and_the_chain_continues(session):
    """★ THE SHIPPED BEHAVIOUR, and the whole point of (b).

    Without fix (a) the Ozolith row is still excluded -- its non-English
    identity has no validated binding and the row carries no contributing
    ids, so desired recomputes to 0 and `0 <= 0` excludes it. That is
    unchanged and expected.

    What changed is the consequence. This used to RAISE, which failed the
    whole perform-sync route closed so the new-listing preview was never
    built and NOTHING published -- 10 times, and on 2026-10-12 it left 882
    cards unlisted for about six hours. Now it is a clean no-op and the
    caller carries on.
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
        seller(0), writer,
    )

    assert result["outcome"] == NOTHING_TO_APPLY, "a no-op, not an exception"
    assert writer.calls == [], "and no Mana Pool write at all"
    assert result["reviewed"] == 1
    assert result["skipped"] == 1
    assert "Mana Pool quantity already reflects this increase" in result["message"]
