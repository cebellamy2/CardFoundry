"""Guarded production entry point for the durable price-only floor correction."""

import argparse
import json

from sqlalchemy.orm import Session

from database import engine
from floor_correction_service import (
    FLOOR_CORRECTION_CONFIRMATION, prepare_floor_correction_execution,
    run_or_resume_floor_correction,
)
from inventory_sync_service import inventory_sync_lease
from manapool_service import (
    get_all_seller_inventory, get_seller_account,
    update_inventory_prices_by_product,
)
from models import Batch, InventoryCard


def _sellable_quantity():
    with Session(engine) as session:
        return session.query(InventoryCard).join(Batch).filter(
            InventoryCard.status == "available", Batch.is_archived == False,  # noqa: E712
        ).count()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview-job", type=int)
    parser.add_argument("--resume-execution")
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()
    if args.confirmation != FLOOR_CORRECTION_CONFIRMATION:
        raise SystemExit("Exact floor-correction confirmation did not match")
    if bool(args.preview_job) == bool(args.resume_execution):
        raise SystemExit("Specify exactly one of --preview-job or --resume-execution")
    with inventory_sync_lease(ttl_seconds=3600, allow_active_cutover=True):
        with Session(engine) as session:
            if args.preview_job:
                execution = prepare_floor_correction_execution(
                    session, args.preview_job, args.confirmation,
                    get_seller_account, get_all_seller_inventory, _sellable_quantity,
                )
                execution_id = execution.execution_id
            else:
                execution_id = args.resume_execution
            result = run_or_resume_floor_correction(
                session, execution_id, update_inventory_prices_by_product,
                get_all_seller_inventory, get_seller_account, _sellable_quantity,
            )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
