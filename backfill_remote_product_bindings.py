"""One-time backfill: create a RemoteProductBinding for every currently
-listed (Mana Pool matched) identity that doesn't have one yet.

Discovered live at v1.107.0's launch, while verifying the new per-
transition quantity push against production: only 918 of 6,647 currently
-listed identities had a validated RemoteProductBinding at all -- 14%
coverage. The push (manapool_quantity_push_service.py) resolves
product_id exclusively from RemoteProductBinding, by design (zero Mana
Pool reads per transition), so the other 86% hit a silent no-op until
v1.108.0 made that visible on /orders/shipment-sync-issues instead.

Traced why: RemoteProductBinding rows are only ever created by
catalog_resolution_service.persist_validated_bindings, called only for
cards MISSING identity fields at import time (production_import_service.py,
printing_correction_service.py). A card imported with a complete identity
already attached -- the shape of most legacy-migration rows -- never
touches that path, even though it's correctly matched against Mana Pool
today via live remote scanning in inventory_mirror_service.py.

This script closes that gap the other direction: instead of resolving a
NEW catalog match, it takes an identity ALREADY proven to match a real
Mana Pool listing -- a row from create_exceptions_review_preview() in
category increase_quantity/decrease_quantity/zero_candidate/hold_equal,
meaning inventory_mirror_service.build_inventory_mirror_preview already
matched exactly one local identity to exactly one remote listing -- and
simply persists that already-proven match as a binding, same row shape
catalog_resolution_service.persist_validated_bindings already writes
(including the same product-id-conflict guard: refuses to create a
second binding for a product_id something else already claims).

Two identity shapes are deliberately EXCLUDED, not silently mishandled:
- Override-keyed rows (canonical_identity["mtgjson_id"] starting with
  "__mtgjson_override__:") and scryfall-fallback rows (starting with
  "__scryfall__:") are build_inventory_mirror_preview's own synthetic
  grouping keys for cards with no real mtgjson_id, substituted into the
  mtgjson_id slot for grouping purposes only. Writing one of those
  strings into RemoteProductBinding.mtgjson_id would corrupt the column
  -- every other reader of it (this script's own callers included)
  expects either a real UUID or NULL. The override path specifically
  exists because a human operator explicitly confirmed a product_id for
  an undocumented printing; recreating that judgment call automatically
  here would bypass the reason that confirmation step exists. Counted
  and reported separately, never backfilled.

Dry run by default (pass --confirm to write). Safe to re-run: an
identity that already has a validated binding by the time this runs
again is simply skipped.
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from database import engine
from inventory_sync_workflow import create_exceptions_review_preview
from models import RemoteProductBinding


_MATCHED_CATEGORIES = {"increase_quantity", "decrease_quantity", "zero_candidate", "hold_equal"}
_SYNTHETIC_PREFIXES = ("__mtgjson_override__:", "__scryfall__:")


def _is_real_mtgjson_id(value) -> bool:
    return bool(value) and not str(value).startswith(_SYNTHETIC_PREFIXES)


def _identity_key(identity: dict) -> str:
    return "|".join(
        str(identity.get(field) or "").upper()
        for field in ("mtgjson_id", "language_id", "condition_id", "finish_id")
    )


def plan_backfill(session: Session, preview: dict, remote_inventory: list[dict]) -> dict:
    """Read-only: classifies every matched mirror-preview row, but writes
    nothing. Returns everything needed both for the dry-run report and
    for apply_backfill to act on."""
    existing_keys = {
        _identity_key({
            "mtgjson_id": b.mtgjson_id, "language_id": b.language_id,
            "condition_id": b.condition_id, "finish_id": b.finish_id,
        })
        for b in session.query(RemoteProductBinding).filter(
            RemoteProductBinding.provider == "manapool",
            RemoteProductBinding.binding_status == "validated",
        )
    }
    existing_product_ids = {
        b.product_id for b in session.query(RemoteProductBinding).filter(
            RemoteProductBinding.provider == "manapool",
            RemoteProductBinding.binding_status == "validated",
        )
    }
    remote_by_product_id = {
        str(item.get("product_id") or ""): item
        for item in remote_inventory if item.get("product_id")
    }

    matched_rows = [row for row in preview.get("rows") or [] if row.get("category") in _MATCHED_CATEGORIES]

    already_bound = 0
    synthetic_keyed = []
    no_remote_item = []
    product_id_conflict = []
    to_create = []

    for row in matched_rows:
        identity = row.get("canonical_identity") or {}
        mtgjson_id = identity.get("mtgjson_id")
        if not _is_real_mtgjson_id(mtgjson_id):
            synthetic_keyed.append(row)
            continue
        key = _identity_key(identity)
        if key in existing_keys:
            already_bound += 1
            continue
        product_id = row.get("remote_product_id")
        remote_item = remote_by_product_id.get(product_id)
        if not remote_item:
            no_remote_item.append(row)
            continue
        if product_id in existing_product_ids:
            product_id_conflict.append(row)
            continue
        to_create.append((row, remote_item))

    sellable_count = sum(1 for row, _ in to_create if int(row.get("desired_quantity") or 0) > 0)
    not_sellable_count = len(to_create) - sellable_count

    return {
        "matched_rows_total": len(matched_rows),
        "already_bound": already_bound,
        "synthetic_keyed": synthetic_keyed,
        "no_remote_item": no_remote_item,
        "product_id_conflict": product_id_conflict,
        "to_create": to_create,
        "to_create_sellable": sellable_count,
        "to_create_not_sellable": not_sellable_count,
    }


def apply_backfill(session: Session, plan: dict) -> dict:
    """Writes exactly the rows plan_backfill classified as to_create.
    One binding per identity; each is committed independently so one
    row's failure can't roll back any other (same isolation principle
    order-status sync and new-listing publishing already use)."""
    created = 0
    failed = []
    for row, remote_item in plan["to_create"]:
        try:
            identity = row["canonical_identity"]
            single = (remote_item.get("product") or {}).get("single") or {}
            product_id = row["remote_product_id"]
            requested_identity = {
                "name": row.get("name") or single.get("name") or "",
                "set_code": single.get("set") or "",
                "collector_number": single.get("number") or "",
                "scryfall_id": single.get("scryfall_id") or "",
                "mtgjson_id": identity.get("mtgjson_id"),
                "language_id": identity.get("language_id"),
                "condition_id": identity.get("condition_id"),
                "finish_id": identity.get("finish_id"),
            }
            evidence = {
                "source": "backfill_remote_product_bindings",
                "matched_via": "mirror_preview_live_scan",
                "requested_identity": requested_identity,
                "product_type": "mtg_single",
                "product_id": product_id,
            }
            evidence_hash = hashlib.sha256(json.dumps(
                evidence, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
            now = datetime.now(timezone.utc)
            session.add(RemoteProductBinding(
                provider="manapool", product_type="mtg_single", product_id=product_id,
                local_card_ids_json=json.dumps(row.get("local_contributing_card_ids") or []),
                requested_identity_json=json.dumps(requested_identity, sort_keys=True),
                scryfall_id=requested_identity["scryfall_id"],
                mtgjson_id=requested_identity["mtgjson_id"],
                language_id=requested_identity["language_id"],
                condition_id=requested_identity["condition_id"],
                finish_id=requested_identity["finish_id"],
                set_code=requested_identity["set_code"],
                collector_number=requested_identity["collector_number"],
                binding_status="validated", validated_at=now,
                catalog_as_of=None, evidence_hash=evidence_hash,
                evidence_json=json.dumps(evidence, sort_keys=True),
                remote_inventory_id=str(remote_item.get("id") or "") or None,
            ))
            session.commit()
            created += 1
        except Exception as exc:
            session.rollback()
            failed.append({"identity": row.get("canonical_identity"), "error": str(exc)})
    return {"created": created, "failed": failed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm", action="store_true",
        help="Actually write bindings. Default is a dry run (report only).",
    )
    parser.add_argument(
        "--output", default=None,
        help="Where to write the JSON report (default: "
             "remote_product_bindings_backfill_{dryrun,confirm}_report.json).",
    )
    args = parser.parse_args()
    output_path = args.output or (
        "remote_product_bindings_backfill_confirm_report.json" if args.confirm
        else "remote_product_bindings_backfill_dryrun_report.json"
    )

    preview = create_exceptions_review_preview()
    from manapool_service import get_all_seller_inventory
    remote_inventory = get_all_seller_inventory(min_quantity=0)

    with Session(engine) as session:
        plan = plan_backfill(session, preview, remote_inventory)

        summary = {
            "matched_rows_total": plan["matched_rows_total"],
            "already_bound": plan["already_bound"],
            "synthetic_keyed": len(plan["synthetic_keyed"]),
            "no_remote_item": len(plan["no_remote_item"]),
            "product_id_conflict": len(plan["product_id_conflict"]),
            "to_create": len(plan["to_create"]),
            "to_create_sellable": plan["to_create_sellable"],
            "to_create_not_sellable": plan["to_create_not_sellable"],
        }

        if args.confirm:
            outcome = apply_backfill(session, plan)
            report = {"mode": "CONFIRMED", "summary": {**summary, **outcome_summary(outcome)}, "failed": outcome["failed"]}
        else:
            report = {
                "mode": "DRY_RUN", "summary": summary,
                "sample_to_create": [
                    {
                        "name": row.get("name"), "identity": row.get("canonical_identity"),
                        "product_id": row.get("remote_product_id"),
                        "desired_quantity": row.get("desired_quantity"),
                    }
                    for row, _ in plan["to_create"][:25]
                ],
                "product_id_conflicts": [
                    {"name": row.get("name"), "identity": row.get("canonical_identity"),
                     "product_id": row.get("remote_product_id")}
                    for row in plan["product_id_conflict"]
                ],
            }

    with open(output_path, "w") as f:
        json.dump(report, f, indent=2, sort_keys=True, default=str)
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    print(f"Full report written to {output_path}")


def outcome_summary(outcome: dict) -> dict:
    return {"created": outcome["created"], "failed_count": len(outcome["failed"])}


if __name__ == "__main__":
    main()
