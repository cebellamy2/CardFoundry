"""Benchmark bounded concurrency with audited requests from pricing job 17."""

import argparse
import json
import sqlite3

from competitor_pricing_service import (
    DEFAULT_OPTIMIZER_BATCH_SIZE,
    SELLER_EXCLUSION_ID,
    deduplicate_competitor_requests,
    partition_optimizer_requests,
)
from manapool_service import (
    get_all_seller_inventory,
    optimize_exact_variant_batch_with_conflicts,
)
from optimizer_benchmark_service import run_concurrency_level


def audited_inventory_ids(database: str, job_id: int) -> set[str]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT status, response_json FROM pricing_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    finally:
        connection.close()
    if not row or row[0] != "completed":
        raise ValueError(f"Completed pricing job {job_id} was not found.")
    preview = (json.loads(row[1] or "{}").get("preview") or {})
    return {
        str(item.get("inventory_id") or "")
        for item in preview.get("audit_rows", [])
        if item.get("inventory_id")
    }


def representative_batches(all_batches: list[list[dict]], count: int) -> list[list[dict]]:
    if count < 1 or count > len(all_batches):
        raise ValueError("sample batch count is outside the available range")
    if count == 1:
        return [all_batches[len(all_batches) // 2]]
    indexes = [round(index * (len(all_batches) - 1) / (count - 1)) for index in range(count)]
    return [all_batches[index] for index in indexes]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="cardfoundry.db")
    parser.add_argument("--job-id", type=int, default=17)
    parser.add_argument("--sample-batches", type=int, default=8)
    args = parser.parse_args()

    audited_ids = audited_inventory_ids(args.database, args.job_id)
    live = get_all_seller_inventory(min_quantity=1)
    audited_live = [item for item in live if str(item.get("id") or "") in audited_ids]
    requests, _ = deduplicate_competitor_requests(audited_live)
    planned = partition_optimizer_requests(
        requests,
        batch_limit=DEFAULT_OPTIMIZER_BATCH_SIZE,
    )
    sample = representative_batches(planned, args.sample_batches)

    report = {
        "source_job_id": args.job_id,
        "batch_size_limit": DEFAULT_OPTIMIZER_BATCH_SIZE,
        "sample_batches": len(sample),
        "sample_requests": sum(len(batch) for batch in sample),
        "results": [],
    }
    for concurrency in (1, 2, 4, 6):
        result = run_concurrency_level(
            sample,
            concurrency,
            SELLER_EXCLUSION_ID,
            optimize_exact_variant_batch_with_conflicts,
        )
        report["results"].append(result)
        print(json.dumps(result, indent=2), flush=True)
        if result["stop_recommended"]:
            report["stopped_before_concurrency"] = next(
                (level for level in (1, 2, 4, 6) if level > concurrency),
                None,
            )
            break

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
