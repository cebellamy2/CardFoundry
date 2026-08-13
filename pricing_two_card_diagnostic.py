"""Run the read-only seller-excluded two-card optimizer proof."""

import argparse
import json
import sqlite3

from manapool_service import (
    get_all_seller_inventory,
    get_inventory_listings_by_ids,
    optimize_exact_variant_batch_excluding_seller,
)
from pricing_diagnostic_service import run_two_card_proof


SELLER_ID = "69340688-c3a9-451d-93e6-031a0e3a73ad"
URZA_INVENTORY_ID = "b4031768-a410-4b61-b817-dc1fc50f0afc"
SECOND_INVENTORY_ID = "00027a61-78e0-4d96-b709-3dd4b1066811"


def preview_contains_ids(database_path: str, job_id: int | None, ids: set[str]) -> int:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        if job_id is None:
            row = connection.execute(
                "SELECT id, response_json FROM pricing_jobs "
                "WHERE action = ? AND status = ? AND response_json IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                ("competitive_bidirectional_preview", "completed"),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT id, response_json FROM pricing_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
    finally:
        connection.close()

    if not row:
        raise ValueError("Completed pricing preview not found.")
    stored = json.loads(row[1] or "{}")
    preview = stored.get("preview") or {}
    preview_ids = {
        str(item.get("inventory_id") or "")
        for bucket in ("changes", "holds", "skipped")
        for item in preview.get(bucket, [])
    }
    missing = ids - preview_ids
    if missing:
        raise ValueError("Diagnostic cards are absent from preview: " + ", ".join(sorted(missing)))
    return int(row[0])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only two-card competitor mapping diagnostic.",
    )
    parser.add_argument("--database", default="cardfoundry.db")
    parser.add_argument("--job-id", type=int)
    args = parser.parse_args()

    selected_ids = {URZA_INVENTORY_ID, SECOND_INVENTORY_ID}
    preview_id = preview_contains_ids(args.database, args.job_id, selected_ids)
    inventory = get_all_seller_inventory(min_quantity=1)
    by_id = {str(item.get("id") or ""): item for item in inventory}
    missing = selected_ids - set(by_id)
    if missing:
        raise ValueError("Diagnostic cards are no longer live: " + ", ".join(sorted(missing)))

    proof = run_two_card_proof(
        [by_id[URZA_INVENTORY_ID], by_id[SECOND_INVENTORY_ID]],
        SELLER_ID,
        optimize_exact_variant_batch_excluding_seller,
        get_inventory_listings_by_ids,
    )
    print(json.dumps({"preview_job_id": preview_id, **proof}, indent=2))
    return 0 if proof["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
