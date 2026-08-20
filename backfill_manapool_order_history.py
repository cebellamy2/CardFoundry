"""One-time backfill: pull CardFoundry's full historical Mana Pool order
record down locally.

Local sync only ever ran through the live order-processing path
(order_service.ingest_manapool_orders), which only ever gets invoked for
orders the operator's own routes touch (the "Sync Mana Pool Orders" button,
inventory-sync previews, etc). Confirmed live: of ~3,835 real Mana Pool
orders on the account, only 143 existed in CardFoundry's local sales_orders
table -- under 4% coverage. This was discovered because a real, shipped,
delivered December 2025 sale ("Polluted Bonds," order 142460-521726) was
completely invisible to the consignment-sheets-import script's local
order-history lookup despite genuinely existing on Mana Pool.

This script inserts historical orders directly, reusing
order_service._build_remote_items/_apply_shipping_address/
_apply_shipping_cost for the exact same field-mapping logic the live sync
path uses -- but deliberately does NOT call
order_service.ingest_manapool_orders/allocate_order. Every order here is
real, historical, and already fulfilled by definition; running it through
the live allocation path would try to reserve today's real, currently
-available inventory against a sale that happened months ago using
inventory that's long since gone, incorrectly locking up live stock.

Mana Pool's `latest_fulfillment_status` for a real order is one of
delivered / shipped / refunded / replaced / null (confirmed live across
all 3,835 orders). Mapped to the local SalesOrder.status CardFoundry's own
rule 2 (in import_consignment_sheets.py) actually checks:
    delivered, shipped, replaced -> "shipped" (a genuine completed sale;
        "replaced" is a logistics reship, not a reversed sale)
    refunded                     -> "cancelled" (money returned, not a
        real completed sale -- must not be treated as one)
    null                         -> skipped entirely (order not yet
        fulfilled -- always a very recent/in-flight order, which the live
        sync path already owns; this is a one-time historical backfill,
        not an ongoing sync mechanism)

Dedup: an order already present locally (source="manapool" +
external_order_id match -- the same check order_service.py's own sync
uses) is skipped before any detail fetch. Safe to re-run.

Dry-run by default (one full read-only pass: walks the order list, fetches
per-order detail, validates every line builds, writes a report -- zero DB
writes). Pass --confirm to actually insert.
"""

import argparse
import json
import time
from datetime import datetime

from sqlalchemy.orm import Session

from database import engine
from inventory_sync_service import inventory_sync_lease
from manapool_service import _get_json, get_seller_order
from models import SalesOrder
from order_service import (
    InventoryAllocationError,
    _apply_shipping_address,
    _apply_shipping_cost,
    _build_remote_items,
)


STATUS_MAP = {
    "delivered": "shipped",
    "shipped": "shipped",
    "replaced": "shipped",
    "refunded": "cancelled",
}


def list_all_order_summaries():
    """Walk /seller/orders via real cursor pagination. manapool_service's
    get_seller_orders_any() only ever fetches a single capped 500-order
    page -- exactly how this whole gap was found -- so this bypasses it and
    talks to the paginated endpoint directly."""
    cursor = None
    summaries = []
    while True:
        params = {"limit": 500}
        if cursor:
            params["cursor"] = cursor
        resp = _get_json("/seller/orders", params=params)
        orders = resp.get("orders", [])
        summaries.extend(orders)
        cursor = resp.get("pagination", {}).get("next_cursor")
        if not cursor or not orders:
            break
    return summaries


def _order_report_row(remote_id, summary, detail, local_status, items):
    return {
        "remote_id": remote_id,
        "label": detail.get("label") or summary.get("label"),
        "created_at": detail.get("created_at") or summary.get("created_at"),
        "local_status": local_status,
        "remote_fulfillment_status": detail.get("latest_fulfillment_status"),
        "item_count": len(items),
        "total_price_cents": sum((item.price_cents or 0) for item in items),
        "items": [
            {
                "name": item.name, "set_code": item.set_code,
                "collector_number": item.collector_number,
                "finish_id": item.finish_id, "condition_id": item.condition_id,
                "price_cents": item.price_cents, "quantity": item.quantity,
            }
            for item in items
        ],
    }


def plan_backfill(session, detail_loader, pace_seconds=0.0, summaries=None):
    """One full pass: enumerate every remote order, skip what's already
    local or not yet fulfilled, fetch+validate detail for the rest. Returns
    everything needed to either just report (dry run) or also insert
    (confirm) without a second round of live API calls."""
    summaries = list_all_order_summaries() if summaries is None else summaries

    existing_ids = {
        row[0] for row in session.query(SalesOrder.external_order_id).filter(
            SalesOrder.source == "manapool",
        )
    }

    to_import = []
    report_rows = []
    skipped_existing = 0
    skipped_unfulfilled = 0
    skipped_unmapped_status = 0
    detail_errors = []

    for summary in summaries:
        remote_id = str(summary.get("id") or "").strip()
        if not remote_id:
            continue
        if remote_id in existing_ids:
            skipped_existing += 1
            continue

        raw_status = summary.get("latest_fulfillment_status")
        if raw_status is None:
            skipped_unfulfilled += 1
            continue
        local_status = STATUS_MAP.get(raw_status)
        if local_status is None:
            skipped_unmapped_status += 1
            detail_errors.append({
                "order_id": remote_id,
                "error": f"Unmapped fulfillment status: {raw_status!r}",
            })
            continue

        try:
            response = detail_loader(remote_id)
        except Exception as exc:
            detail_errors.append({"order_id": remote_id, "error": f"Detail fetch failed: {exc}"})
            continue
        detail = response.get("order") or response

        try:
            items = _build_remote_items(detail, remote_id, order_id=0)
        except InventoryAllocationError as exc:
            detail_errors.append({"order_id": remote_id, "error": str(exc)})
            continue

        to_import.append({
            "remote_id": remote_id, "summary": summary, "detail": detail,
            "local_status": local_status,
        })
        report_rows.append(_order_report_row(remote_id, summary, detail, local_status, items))

        if pace_seconds:
            time.sleep(pace_seconds)

    return {
        "to_import": to_import,
        "report_rows": report_rows,
        "skipped_existing": skipped_existing,
        "skipped_unfulfilled": skipped_unfulfilled,
        "skipped_unmapped_status": skipped_unmapped_status,
        "detail_errors": detail_errors,
        "total_remote_orders": len(summaries),
    }


def apply_backfill(session, plan):
    """Insert every order in plan['to_import']. Caller commits per order,
    matching order_service.ingest_manapool_orders' isolation -- one order's
    problem never blocks or rolls back any other."""
    imported = 0
    failed = []
    for record in plan["to_import"]:
        remote_id = record["remote_id"]
        summary = record["summary"]
        detail = record["detail"]
        try:
            order = SalesOrder(
                external_order_id=remote_id,
                source="manapool",
                status=record["local_status"],
                external_label=detail.get("label") or summary.get("label"),
                remote_fulfillment_status=detail.get("latest_fulfillment_status"),
                shipping_method=detail.get("shipping_method") or summary.get("shipping_method"),
            )
            if record["local_status"] == "shipped":
                # mana_pool_shipment_synced_at marks "no outbound push to
                # Mana Pool is outstanding" -- true here by construction,
                # since this order was fulfilled on Mana Pool's own site
                # long before CardFoundry ever knew about it, never through
                # CardFoundry's own pack/ship flow. Leaving it null makes
                # main.py's shipment-sync-stuck banner (main.py:8577-8583)
                # flag every one of these as a failed push needing retry,
                # which is a real bug this caused live in production.
                order.mana_pool_shipment_synced_at = datetime.now()
            session.add(order)
            session.flush()

            items = _build_remote_items(detail, remote_id, order.id)
            session.add_all(items)

            _apply_shipping_address(order, detail)
            _apply_shipping_cost(order, detail)

            fulfillments = detail.get("fulfillments") or []
            if fulfillments:
                tracking = fulfillments[0].get("tracking_number")
                if tracking:
                    order.tracking_number = str(tracking).strip() or None

            session.commit()
            imported += 1
        except Exception as exc:
            session.rollback()
            failed.append({"order_id": remote_id, "error": str(exc)})

    return {"imported": imported, "failed": failed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm", action="store_true",
        help="Actually write changes. Default is a dry run (report only).",
    )
    parser.add_argument(
        "--pace-seconds", type=float, default=0.05,
        help="Delay between per-order Mana Pool detail calls (default 0.05s).",
    )
    parser.add_argument(
        "--output", default=None,
        help="Where to write the JSON report (default: "
             "manapool_order_backfill_{dryrun,confirm}_report.json).",
    )
    args = parser.parse_args()
    output_path = args.output or (
        "manapool_order_backfill_confirm_report.json" if args.confirm
        else "manapool_order_backfill_dryrun_report.json"
    )

    with inventory_sync_lease():
        with Session(engine) as session:
            plan = plan_backfill(session, get_seller_order, pace_seconds=args.pace_seconds)

            summary = {
                "total_remote_orders": plan["total_remote_orders"],
                "skipped_existing": plan["skipped_existing"],
                "skipped_unfulfilled": plan["skipped_unfulfilled"],
                "skipped_unmapped_status": plan["skipped_unmapped_status"],
                "detail_errors": len(plan["detail_errors"]),
                "to_import": len(plan["to_import"]),
            }

            if args.confirm:
                # apply_backfill commits per order itself (one order's
                # failure must never roll back any other), so it must not
                # also be wrapped in an outer session.begin() -- the two
                # would fight over the same transaction boundary.
                outcome = apply_backfill(session, plan)
                report = {
                    "mode": "CONFIRMED",
                    "summary": {**summary, "imported": outcome["imported"]},
                    "failed": outcome["failed"], "detail_errors": plan["detail_errors"],
                }
            else:
                report = {
                    "mode": "DRY_RUN", "summary": summary,
                    "rows": plan["report_rows"], "detail_errors": plan["detail_errors"],
                }

    with open(output_path, "w") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)

    print(json.dumps({"mode": report["mode"], "summary": report["summary"]}, indent=2, sort_keys=True))
    print(f"Full report written to {output_path}")


if __name__ == "__main__":
    main()
