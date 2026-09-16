"""Market-following prices for the WHOLE catalogue, via Mana Pool's own
server-side bulk-price job.

WHY THIS EXISTS. Flow B (/buyer/optimizer) is item-limited: measured on
2026-09-16 it evaluated ~563 of 6,026 listings per run and held the rest,
and because its sort order is stable the same ~5,800 products were never
priced, run after run. A $100 Timeless Lotus sat against a $1.49 market
for two weeks. Mana Pool's bulk-price job priced 6,029 of 6,029 in 13
seconds.

THE SETTINGS ARE THE OPERATOR'S OWN, verbatim from the run he does by hand
in the web UI: price reference "Low Listed" and adjustment "Fixed Amount"
together are strategy market_low_fixed; "Fixed Cent Adjustment -5" is
modifier -5; "Exclude letter-shipping-disabled sellers" is
excludeLetterShippingDisabledSellers. minOtherListings is 1 so an item
with no competitor is skipped rather than priced off a reference with
nothing behind it.

ON THE SUB-FLOOR PRICES. The job will write raw prices below CardFoundry's
$0.65 floor -- 5,436 of 5,986 targets in the measured run, as low as
$0.15. Mana Pool applies the seller's store minimum at SERVE time, not at
write time: the raw price is stored as sent, and buyers are shown $0.65.
Verified live by applying to six listings and checking both views.
Operator decision 2026-09-16: that is acceptable, because sub-floor cards
are filler that exists to be found by the marketplace algorithms, and
market-following coverage matters more than the stored number looking
tidy. So this deliberately does NOT clamp.

WHAT IT STILL PROTECTS. Manual price overrides are re-asserted after every
apply: the bulk job filters by set, price and quantity and cannot exclude
individual products, so an override would otherwise be silently
overwritten by the market.
"""

import csv
import io
import json
import logging
import time

from manapool_service import (
    _get_json,
    _get_text,
    _post_json,
    update_inventory_prices_by_product,
)

logger = logging.getLogger("cardfoundry")

# The operator's four web-UI answers, as documented API parameters.
BULK_FILTERS = {
    "inventoryFilters": {"minQuantity": 1},
    "productFilters": {"productType": "mtg_single"},
}
BULK_PRICING = {
    "strategy": "market_low_fixed",
    "modifier": -5,
    "roundTo": 1,
    "onlyIncrease": False,
    "onlyDecrease": False,
    "minConfidence": 0,
    "minOtherListings": 1,
    "maxAllowedChange": 1000000,
    "maxAllowedChangeCents": 2147483647,
    "minAllowedChangeCents": 1,
}
EXCLUDE_LETTER_SHIPPING_DISABLED = True

JOB_POLL_INTERVAL_SECONDS = 3.0
JOB_TIMEOUT_SECONDS = 300.0


class BulkPricingError(RuntimeError):
    pass


def start_job(*, is_preview: bool) -> str:
    """Start one bulk-price job and return its id.

    is_preview is required rather than defaulted: the difference between a
    report and a catalogue-wide price change should never be a parameter
    somebody forgot to pass.
    """
    body = {
        "filters": BULK_FILTERS,
        "pricing": BULK_PRICING,
        "isPreview": bool(is_preview),
        "excludeLetterShippingDisabledSellers": EXCLUDE_LETTER_SHIPPING_DISABLED,
    }
    response = _post_json("/inventory/bulk-price", body)
    job_id = (response or {}).get("jobId")
    if not job_id:
        raise BulkPricingError(f"Mana Pool did not return a job id: {response!r}")
    logger.info(
        "bulk pricing job started: job_id=%s preview=%s strategy=%s modifier=%s",
        job_id, bool(is_preview), BULK_PRICING["strategy"], BULK_PRICING["modifier"],
    )
    return job_id


def wait_for_job(job_id: str, *, timeout: float = JOB_TIMEOUT_SECONDS,
                 poll_interval: float = JOB_POLL_INTERVAL_SECONDS,
                 sleep=time.sleep) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        payload = _get_json(f"/inventory/bulk-price/jobs/{job_id}")
        job = (payload or {}).get("job") or payload or {}
        status = str(job.get("status") or "")
        if status == "completed":
            return job
        if status == "failed":
            raise BulkPricingError(
                f"Bulk price job {job_id} failed: {job.get('error_message')}"
            )
        if time.monotonic() >= deadline:
            raise BulkPricingError(
                f"Bulk price job {job_id} did not finish within {timeout:.0f}s "
                f"(last status {status!r})"
            )
        sleep(poll_interval)


def parse_export(text: str) -> list[dict]:
    """The per-item rows out of Mana Pool's multi-section audit CSV.

    The file opens with a one-row job summary under its own header, then a
    literal "Individual Item Details" divider, then the rows that matter,
    then a progress log. Reading it with a naive DictReader silently
    returns the summary's columns for every row.
    """
    lines = (text or "").split("\n")
    header = next(
        (i for i, line in enumerate(lines) if line.startswith("Item,Details,Current,")),
        None,
    )
    if header is None:
        raise BulkPricingError("Bulk price export has no item-details section")
    rows = csv.DictReader(io.StringIO("\n".join(lines[header:])))
    return [row for row in rows if (row.get("Item") or "").strip()
            and (row.get("Status") or "").strip() in ("success", "skipped", "failed")]


def fetch_export(job_id: str) -> list[dict]:
    return parse_export(_get_text(f"/inventory/bulk-price/jobs/{job_id}/export"))


def _cents(value) -> int | None:
    text = str(value or "").replace("$", "").replace(",", "").strip()
    try:
        return int(round(float(text) * 100))
    except (TypeError, ValueError):
        return None


def summarise(job: dict, rows: list[dict]) -> dict:
    priced = [r for r in rows if r.get("Status") == "success"]
    current_total = sum(_cents(r.get("Current")) or 0 for r in priced)
    new_total = sum(_cents(r.get("New")) or 0 for r in priced)
    below_floor = sum(1 for r in priced if (_cents(r.get("New")) or 0) < 65)
    return {
        "job_id": job.get("id"),
        "is_preview": job.get("is_preview"),
        "total_items": job.get("total_items"),
        "successful_items": job.get("successful_items"),
        "skipped_items": job.get("skipped_items"),
        "failed_items": job.get("failed_items"),
        "priced_rows": len(priced),
        "current_total_cents": current_total,
        "new_total_cents": new_total,
        "change_cents": new_total - current_total,
        # Reported, never clamped -- see the module docstring.
        "below_floor_rows": below_floor,
    }


def reassert_manual_overrides(session) -> dict:
    """Put manual overrides back after the market has overwritten them.

    The bulk job selects by set, price and quantity; it cannot exclude an
    individual product. Without this an operator-set price is silently
    replaced by the market on the next run, which is the one thing an
    override exists to prevent.
    """
    from models import ManualPriceOverride

    overrides = (
        session.query(ManualPriceOverride)
        .filter(ManualPriceOverride.status == "active")
        .all()
    )
    updates = [
        {
            "product_type": "mtg_single",
            "product_id": row.product_id,
            "price_cents": int(row.manual_price_cents),
            "quantity": None,
        }
        for row in overrides
        if row.product_id and row.manual_price_cents is not None
    ]
    if not updates:
        return {"reasserted": 0, "skipped": len(overrides)}
    update_inventory_prices_by_product(updates)
    logger.info(
        "bulk pricing: re-asserted %s manual price override(s) after the apply",
        len(updates),
    )
    return {"reasserted": len(updates), "skipped": len(overrides) - len(updates)}
