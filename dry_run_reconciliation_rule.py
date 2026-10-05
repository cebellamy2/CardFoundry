"""Preview the non-English fix to reconciliation's desired-quantity rule.

WHAT IS WRONG. inventory_reconciliation_service._fresh_desired_quantity
carried its own four-key MTGJSON match. For a NON-ENGLISH printing that is
the wrong rule: v2.7.0/v2.11.0 established from a live measurement (50 of
89 non-English seller rows carry the ENGLISH Scryfall object's id, 39 carry
their own language's) that neither id identifies a non-English printing, and
set code plus collector number do. physical_identity holds that one rule and
manapool_quantity_push_service._desired_quantity_for_binding is its
canonical reader.

THE SENTINEL IS THE CONCRETE FAILURE. The mirror keys a binding with no
MTGJSON id of its own on a synthetic "__mtgjson_override__:<product_id>".
Compared against InventoryCard.mtgjson_id that can never match anything, so
the old query returned 0 for a printing holding real available stock.

  decrease / zero_candidate: write_quantity IS fresh_desired_quantity, and
      the row is only excluded when write >= fresh_remote. A wrong 0
      against a remote quantity of 1+ is therefore WRITTEN -- it zeroes a
      listing that should have held stock. This is the destructive case.
  increase: write_quantity is min(fresh_remote + traceable, fresh_desired),
      and a 0 makes write <= fresh_remote, which EXCLUDES the row. That
      direction fails safe: the listing simply never goes up.

★ WHY THIS SCRIPT RUNS THE REAL PATH. A preview that computes its own
answer cannot predict the apply -- that is exactly how v2.12.0 published 59
listings when 8 were approved. So this calls the real
apply_reconciliation_preview, after the real run_additive_mtgjson_backfill
(Perform Sync's own first step: a card with a NULL mtgjson_id is invisible
to the mirror, and NULL mtgjson is precisely the condition under test, so
skipping the backfill would hide the whole population).

THE ONE DIFFERENCE FROM THE WRITE, STATED PLAINLY: product_writer is a
RECORDER. It captures the payload Mana Pool would have received and makes
no request. Everything upstream of the write boundary -- the backfill, the
order re-ingest, the per-row freshness re-checks, the exclusion decisions,
the quantity arithmetic -- is the real code. The transaction is rolled back
regardless.

NO MANA POOL WRITES. Reads only: the order listing, order details (capped
by ORDER_SYNC_MAX_ORDERS_PER_RUN), the seller inventory walk (one request --
limit is 10000) and the backfill's catalog reads.

Usage, in the container:
    cd /app && PYTHONPATH=/app /opt/venv/bin/python dry_run_reconciliation_rule.py --plan
"""
import argparse
import json
import logging

from sqlalchemy.orm import Session

from actor_context import set_script_actor
from database import engine

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "dry_run_reconciliation_rule"


class RecordingProductWriter:
    """Captures what would have been written. Makes no request.

    Mirrors the real writer's return shape -- a LIST of one response per
    chunk, not a single dict -- because the caller stores whatever comes
    back and a wrong shape here would be a difference between the preview
    and the write.
    """

    def __init__(self):
        self.updates = []

    def __call__(self, updates):
        self.updates.extend(updates)
        logger.info(
            "%s: RECORDED (not sent) %s product quantity update(s).",
            SCRIPT_NAME, len(updates),
        )
        return [{"recorded": len(updates), "sent": False}]


def _old_rule(session, identity: dict) -> int:
    """The pre-fix query, kept here only so the plan can show the delta.

    Deliberately a local copy: the point of the change is that the
    production code no longer contains this, and the diff has to be
    against what production USED to do.
    """
    from sqlalchemy import func
    from inventory_mirror_service import SELLABLE_STATUS
    from models import Batch, InventoryCard
    return (
        session.query(InventoryCard)
        .join(Batch, InventoryCard.batch_id == Batch.id)
        .filter(
            InventoryCard.status == SELLABLE_STATUS,
            Batch.is_archived == False,
            func.upper(InventoryCard.mtgjson_id) == str(identity.get("mtgjson_id") or "").upper(),
            func.upper(InventoryCard.language_id) == str(identity.get("language_id") or "").upper(),
            func.upper(InventoryCard.condition_id) == str(identity.get("condition_id") or "").upper(),
            func.upper(InventoryCard.finish_id) == str(identity.get("finish_id") or "").upper(),
        )
        .count()
    )


def diff_mirror_rows(session, mirror_rows: list[dict]) -> list[dict]:
    """Old rule vs new rule for EVERY mirror row, not just today's candidates.

    Candidate categories change as stock and remote quantities move, so a
    row that is hold_equal today can be a decrease tomorrow. Reporting only
    today's candidates would understate the exposure.
    """
    from inventory_reconciliation_service import _fresh_desired_quantity
    CANDIDATE_CATEGORIES = ("increase_quantity", "decrease_quantity", "zero_candidate")
    changed = []
    for row in mirror_rows:
        identity = row.get("canonical_identity") or {}
        product_id = row.get("remote_product_id")
        old = _old_rule(session, identity)
        new = _fresh_desired_quantity(session, identity, product_id)
        if old == new:
            continue
        changed.append({
            "category": row.get("category"),
            "reaches_apply_today": row.get("category") in CANDIDATE_CATEGORIES,
            "name": row.get("name"),
            "language_id": identity.get("language_id"),
            "canonical_mtgjson": identity.get("mtgjson_id"),
            "product_id": product_id,
            "mirror_desired_quantity": row.get("desired_quantity"),
            "remote_quantity": row.get("current_remote_quantity"),
            "old_rule": old,
            "new_rule": new,
            "direction_of_change": "increase" if new > old else "DECREASE",
        })
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", action="store_true", required=True,
                        help="The only mode. Rolls back; sends nothing.")
    parser.add_argument("--mirror-job-id", type=int, default=None,
                        help="Diff against this stored mirror preview instead "
                             "of building a fresh one (no Mana Pool calls).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.setLevel(logging.INFO)
    set_script_actor(SCRIPT_NAME)

    from models import InventorySyncJob

    with Session(engine) as session:
        if args.mirror_job_id:
            job = session.get(InventorySyncJob, args.mirror_job_id)
            if job is None:
                raise SystemExit(f"No inventory_sync_job {args.mirror_job_id}")
            mirror_rows = (json.loads(job.snapshot_json) or {}).get("rows") or []
            source = f"stored mirror job {args.mirror_job_id}"
        else:
            # ★ THE REAL CHAIN, IN PERFORM SYNC'S OWN ORDER AND WITH ITS OWN
            # FUNCTIONS. perform_sync_route does exactly this: the additive
            # backfill (with the PRODUCT-id catalog loader, not the
            # scryfall-id one), then create_inventory_sync_preview. The
            # mirror is NOT assembled here -- reproducing that assembly is
            # the very mistake this script exists to avoid.
            from manapool_service import (get_all_seller_inventory,
                                          get_single_catalog_by_product_ids)
            from mtgjson_backfill_service import run_additive_mtgjson_backfill
            from inventory_sync_workflow import create_inventory_sync_preview
            logger.info("%s: running run_additive_mtgjson_backfill (Perform Sync's "
                        "first step) -- a NULL-mtgjson card is invisible to the "
                        "mirror without it.", SCRIPT_NAME)
            run_additive_mtgjson_backfill(
                session, get_all_seller_inventory, get_single_catalog_by_product_ids,
                operator_note=f"{SCRIPT_NAME} dry run",
            )
            # Flushed, never committed: the backfill's own writes are part of
            # what this transaction rolls back.
            session.flush()
            mirror_preview = create_inventory_sync_preview(
                fail_closed_on_unresolved=False, acquire_lease=True,
            )
            mirror_rows = (mirror_preview or {}).get("rows") or []
            source = "a freshly built mirror preview (real chain)"

        changed = diff_mirror_rows(session, mirror_rows)

        print(f"source:            {source}")
        print(f"mirror rows:       {len(mirror_rows)}")
        print(f"rows whose answer CHANGES: {len(changed)}")
        decreases = [c for c in changed if c["direction_of_change"] == "DECREASE"]
        print(f"rows that would DECREASE:  {len(decreases)}")
        print()
        for c in changed:
            print(f"  [{c['category']}] {c['name']} ({c['language_id']})")
            print(f"     product_id        {c['product_id']}")
            print(f"     canonical mtgjson {c['canonical_mtgjson']}")
            print(f"     mirror desired={c['mirror_desired_quantity']} remote={c['remote_quantity']}")
            print(f"     OLD rule -> {c['old_rule']}   NEW rule -> {c['new_rule']}"
                  f"   ({c['direction_of_change']})")
            print(f"     reaches the apply TODAY: {c['reaches_apply_today']}")
        if decreases:
            print()
            print("!! A DECREASE under the new rule was NOT expected. Each one above "
                  "needs explaining before anything ships.")

        # Nothing is written, whatever happened above.
        session.rollback()
        print()
        print("PLAN ONLY -- transaction rolled back, no Mana Pool request made.")


if __name__ == "__main__":
    main()
