"""Move a card between batches without stranding its pick allocations.

★ WHY THIS EXISTS (v2.39.0 bug, 2026-10-12). `PickAllocation` carries its
OWN `batch_id`, written once when the allocation is created and never
since. v2.39.0 taught the batch delete to move a `reserved` card to
another batch so its order could still ship -- it updated the card and
left the allocation pointing at the batch the same delete then removed.

Three INNER joins read that column:
  - pick_wave_service.get_wave_picklist  (the picker's list)
  - order_service.get_picklist           (the order's pick list)
  - main._order_line_batch_codes         (the Orders batch column)

An inner join does not show a stale batch, it DROPS THE ROW. Seven live
orders each lost a line and order 4526, a one-line order, rendered an
empty pick list. The cards were never in danger -- present, `reserved`,
correctly in A1 -- but nothing would tell a picker to fetch them.

★ ONLY ACTIVE ALLOCATIONS FOLLOW THE CARD. `allocated`, `picked` and
`packed` are work still to be done, and the pick list has to point at
where the card actually is. `shipped` and `released` are history: a
shipped allocation records the batch a card shipped FROM, and rewriting
that to a batch it was moved to afterwards would make the record claim
something that never happened. Read from the shared
ACTIVE_ALLOCATION_STATUSES rather than re-listed here, so this cannot
drift from the rest of the codebase.
"""

import logging

from sqlalchemy.orm import Session

from inventory_mirror_service import ACTIVE_ALLOCATION_STATUSES
from models import Batch, InventoryCard, PickAllocation

logger = logging.getLogger("cardfoundry")


def move_card_to_batch(
    session: Session, card: InventoryCard, target: Batch,
) -> list[int]:
    """Point `card` at `target` and bring its active allocations along.

    Returns the ids of the allocations re-pointed. Writes no audit row --
    callers already write their own, in their own vocabulary, and a second
    one here would double-report every move. Flushes nothing; the caller
    owns the transaction.
    """
    card.batch_id = target.id
    allocations = (
        session.query(PickAllocation)
        .filter(
            PickAllocation.inventory_card_id == card.id,
            PickAllocation.status.in_(tuple(ACTIVE_ALLOCATION_STATUSES)),
            PickAllocation.batch_id != target.id,
        )
        .all()
    )
    for allocation in allocations:
        previous = allocation.batch_id
        allocation.batch_id = target.id
        logger.info(
            "card %s moved to batch %s (%s): allocation %s re-pointed from "
            "batch %s so its order's pick list still shows the card.",
            card.id, target.id, target.batch_code, allocation.id, previous,
        )
    return [allocation.id for allocation in allocations]
