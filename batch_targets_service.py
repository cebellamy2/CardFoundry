"""Which batches can take more cards, and how many each still has.

★ THE RULE (operator decision, 2026-10-27): ARCHIVED IS THE ONLY REASON A
BATCH CANNOT TAKE CARDS. A batch that has thinned out -- every card sold,
removed, or reserved -- is still a perfectly good place to put more stock,
and refilling it is normal shop practice rather than an edge case.

Before this, two dropdowns offered only batches with ZERO InventoryCard
rows of any status. Measured against production on 2026-10-27, that left
the pile-finalize purchase picker offering NOTHING AT ALL: of 86
non-archived purchase batches, not one had zero rows. C13 -- the
operator's example, 6 rows all `removed` -- was not a special case, it was
the whole list.

The type rules are NOT relaxed here, because they are about what a card
IS rather than how full a batch is:

  * a bought card never goes into a consignment batch, and
  * a consigned card only goes into a batch belonging to the SAME
    consignor, since the batch is what decides whose payout it feeds.

`available_count` is returned alongside each batch so a picker can say
"C13 (0 available)" -- the operator asked to see how thin a batch is at
the moment they choose it, which is the information the old empty-only
rule was destroying in order to enforce itself. It is one grouped query,
not one per batch: the builder it replaces ran a COUNT per batch.
"""

from sqlalchemy import func

from models import Batch, InventoryCard
from natural_sort import natural_sort_key

PURCHASE = "purchase"
CONSIGNMENT = "consignment"


def available_counts(session) -> dict[int, int]:
    """{batch_id: available card count}, in one grouped query."""
    return dict(
        session.query(InventoryCard.batch_id, func.count(InventoryCard.id))
        .filter(InventoryCard.status == "available")
        .group_by(InventoryCard.batch_id)
        .all()
    )


def selectable_batches(
    session, *, kind: str, consignor_id: int | None = None,
) -> list[tuple[Batch, int]]:
    """Every batch that may receive cards of `kind`, naturally sorted.

    Returns (batch, available_count) pairs. Natural sort throughout, or
    A10 sorts before A2 in every picker this feeds.
    """
    if kind not in (PURCHASE, CONSIGNMENT):
        raise ValueError(f"unknown batch kind: {kind!r}")

    query = session.query(Batch).filter(Batch.is_archived == False)  # noqa: E712
    if kind == PURCHASE:
        query = query.filter(Batch.is_consignment == False)  # noqa: E712
    else:
        query = query.filter(Batch.is_consignment == True)  # noqa: E712
        if consignor_id is not None:
            # A consigned card may only land in ITS consignor's batch --
            # the batch is what decides whose payout the sale feeds, so
            # offering someone else's would pay the wrong person.
            query = query.filter(Batch.consignor_id == consignor_id)

    counts = available_counts(session)
    return sorted(
        ((batch, counts.get(batch.id, 0)) for batch in query.all()),
        key=lambda pair: natural_sort_key(pair[0].batch_code),
    )
