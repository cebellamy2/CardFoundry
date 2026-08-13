"""Generate a read-only Mana Pool floor-correction preview."""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from competitor_pricing_service import build_batched_competitor_preview
from database import engine
from floor_correction_service import build_floor_correction_preview
from manapool_service import (
    get_all_seller_inventory, get_inventory_listings_by_ids,
    get_seller_account, get_single_catalog_by_product_ids,
    optimize_exact_variant_batch_with_conflicts,
)
from models import AppSetting, CleanRebuildExecution, ExecutionPricingSeal, PricingJob


def main():
    account = get_seller_account()
    if bool(account.get("singles_live")):
        raise RuntimeError("Mana Pool singles store is LIVE; floor correction preview refused")
    remote = get_all_seller_inventory(min_quantity=0)
    with Session(engine) as session:
        execution = session.scalar(select(CleanRebuildExecution).where(
            CleanRebuildExecution.status == "completed",
        ).order_by(CleanRebuildExecution.id.desc()))
        if not execution:
            raise RuntimeError("No completed clean-rebuild execution found")
        seal = session.scalar(select(ExecutionPricingSeal).where(
            ExecutionPricingSeal.seal_id == execution.pricing_seal_id,
        ))
        plan = json.loads(seal.sealed_plan_json)
        managed_ids = {str(r["product_id"]) for r in plan["republish_rows"]}
        floor = int(session.scalar(select(AppSetting.value).where(
            AppSetting.key == "pricing_floor_cents",
        )) or 65)
        undercut = int(session.scalar(select(AppSetting.value).where(
            AppSetting.key == "pricing_undercut_cents",
        )) or 5)
    affected = [r for r in remote if (
        str(r.get("product_id") or "") in managed_ids
        and int(r.get("quantity") or 0) > 0
        and int(r.get("price_cents") or 0) < floor
    )]
    pricing = build_batched_competitor_preview(
        affected, optimize_exact_variant_batch_with_conflicts,
        get_inventory_listings_by_ids, undercut_cents=undercut,
        floor_cents=floor, market_catalog_call=get_single_catalog_by_product_ids,
    )
    preview = build_floor_correction_preview(remote, managed_ids, pricing, floor)
    preview["account_evidence"] = {"singles_live": False}
    preview["source_execution_id"] = execution.execution_id
    with Session(engine) as session:
        job = PricingJob(
            action="floor_correction_preview", status="completed",
            request_json=json.dumps({
                "floor_cents": floor, "undercut_cents": undercut,
                "source_execution_id": execution.execution_id,
            }, sort_keys=True),
            response_json=json.dumps(preview, sort_keys=True, default=str),
        )
        session.add(job); session.commit(); session.refresh(job)
        print(json.dumps({"pricing_job_id": job.id, **preview["summary"],
                          "seller_snapshot_hash": preview["seller_snapshot_hash"],
                          "target_hash": preview["target_hash"],
                          "payload_hash": preview["payload_hash"]}, indent=2))


if __name__ == "__main__":
    main()
