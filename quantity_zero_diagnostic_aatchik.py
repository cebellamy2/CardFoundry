"""Single-use approved Mana Pool 2 -> 0 -> 2 quantity diagnostic."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from manapool_service import (
    _post_json,
    get_all_seller_inventory,
    get_inventory_listings_by_ids,
)


INVENTORY_ID = "51ff0a6b-c5dc-452b-abbd-f648ef776767"
PRODUCT_ID = "1ee62dad-8005-4905-9aee-bc4340f9a1f3"
ORIGINAL_QUANTITY = 2
ORIGINAL_PRICE_CENTS = 15
EXPECTED_SINGLE = {
    "name": "Aatchik, Emerald Radian",
    "set": "DFT",
    "number": "187",
    "language_id": "EN",
    "condition_id": "LP",
    "finish_id": "NF",
    "mtgjson_id": "7b00c266-61f7-5222-9251-6a2e1a7bb5b9",
}
LOG_PATH = Path("quantity_zero_diagnostic_aatchik_20260813_rerun.json")


audit = {
    "diagnostic": "approved_quantity_zero_round_trip",
    "started_at": datetime.now(timezone.utc).isoformat(),
    "target_inventory_id": INVENTORY_ID,
    "target_product_id": PRODUCT_ID,
    "events": [],
    "success": False,
}


def record(event: str, **details):
    audit["events"].append({
        "at": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **details,
    })
    LOG_PATH.write_text(json.dumps(audit, indent=2, default=str) + "\n")


def validate_identity(item: dict):
    single = ((item.get("product") or {}).get("single") or {})
    actual = {key: str(single.get(key) or "") for key in EXPECTED_SINGLE}
    if actual != EXPECTED_SINGLE:
        raise RuntimeError(f"Target identity mismatch: {actual!r}")
    if str(item.get("id") or "") != INVENTORY_ID:
        raise RuntimeError("Target inventory_id changed")
    if str(item.get("product_id") or "") != PRODUCT_ID:
        raise RuntimeError("Target product_id changed")


def seller_snapshot() -> dict:
    matches = [
        item for item in get_all_seller_inventory(min_quantity=0)
        if str(item.get("id") or "") == INVENTORY_ID
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one seller record, found {len(matches)}")
    item = matches[0]
    validate_identity(item)
    return {
        "inventory_id": str(item.get("id") or ""),
        "product_id": str(item.get("product_id") or ""),
        "quantity": int(item.get("quantity") or 0),
        "price_cents": int(item.get("price_cents") or 0),
        "effective_as_of": item.get("effective_as_of"),
        "single": EXPECTED_SINGLE,
    }


def buyer_snapshot() -> list[dict]:
    return get_inventory_listings_by_ids([INVENTORY_ID])


def payload(quantity: int) -> list[dict]:
    return [{
        "product_type": "mtg_single",
        "product_id": PRODUCT_ID,
        "price_cents": None,
        "quantity": quantity,
    }]


def main():
    if LOG_PATH.exists():
        raise RuntimeError(f"Refusing to overwrite existing audit log: {LOG_PATH}")

    write_attempted = False
    diagnostic_error = None
    try:
        before = seller_snapshot()
        record("before", seller_snapshot=before)
        if before["quantity"] != ORIGINAL_QUANTITY:
            raise RuntimeError(f"Pre-write quantity is {before['quantity']}, expected 2")
        if before["price_cents"] != ORIGINAL_PRICE_CENTS:
            raise RuntimeError(f"Pre-write price is {before['price_cents']}, expected 15")

        zero_payload = payload(0)
        record("zero_write_planned", endpoint="/seller/inventory/product", payload=zero_payload)
        write_attempted = True
        response = _post_json("/seller/inventory/product", zero_payload)
        record("zero_write_response", response=response)

        zero_seller = seller_snapshot()
        record("zero_seller_readback", seller_snapshot=zero_seller)
        if zero_seller["quantity"] != 0:
            raise RuntimeError(f"Zero seller quantity is {zero_seller['quantity']}, expected 0")
        if zero_seller["price_cents"] != ORIGINAL_PRICE_CENTS:
            raise RuntimeError(f"Zero seller price is {zero_seller['price_cents']}, expected 15")

        zero_buyer = buyer_snapshot()
        record("zero_buyer_readback", listings=zero_buyer)
        zero_matches = [item for item in zero_buyer if str(item.get("id") or "") == INVENTORY_ID]
        record(
            "zero_buyer_behavior",
            same_record_present=len(zero_matches) == 1,
            returned_quantity=(int(zero_matches[0].get("quantity") or 0) if len(zero_matches) == 1 else None),
            note="Buyer listings may expose zero records and may lag seller inventory; seller readback is authoritative for this diagnostic.",
        )
    except Exception as exc:
        diagnostic_error = f"{type(exc).__name__}: {exc}"
        record("diagnostic_error", error=diagnostic_error)
    finally:
        if write_attempted:
            restore_payload = payload(ORIGINAL_QUANTITY)
            record("restore_write_planned", endpoint="/seller/inventory/product", payload=restore_payload)
            try:
                response = _post_json("/seller/inventory/product", restore_payload)
                record("restore_write_response", response=response)
                restored = seller_snapshot()
                record("restore_seller_readback", seller_snapshot=restored)
                if restored["quantity"] != ORIGINAL_QUANTITY:
                    raise RuntimeError(f"Restored quantity is {restored['quantity']}, expected 2")
                if restored["price_cents"] != ORIGINAL_PRICE_CENTS:
                    raise RuntimeError(f"Restored price is {restored['price_cents']}, expected 15")

                buyer_observations = []
                buyer_reactivated = False
                for attempt in range(1, 6):
                    restored_buyer = buyer_snapshot()
                    matches = [item for item in restored_buyer if str(item.get("id") or "") == INVENTORY_ID]
                    observation = {
                        "attempt": attempt,
                        "match_count": len(matches),
                        "quantity": int(matches[0].get("quantity") or 0) if len(matches) == 1 else None,
                        "price_cents": int(matches[0].get("price_cents") or 0) if len(matches) == 1 else None,
                    }
                    buyer_observations.append(observation)
                    if len(matches) == 1:
                        validate_identity(matches[0])
                        if observation["quantity"] == ORIGINAL_QUANTITY and observation["price_cents"] == ORIGINAL_PRICE_CENTS:
                            buyer_reactivated = True
                            break
                    time.sleep(2)
                record("restore_buyer_readback", observations=buyer_observations, reactivated=buyer_reactivated)
                audit["success"] = diagnostic_error is None
            except Exception as exc:
                restore_error = f"{type(exc).__name__}: {exc}"
                audit["restore_error"] = restore_error
                record("restore_error", error=restore_error)

        audit["finished_at"] = datetime.now(timezone.utc).isoformat()
        LOG_PATH.write_text(json.dumps(audit, indent=2, default=str) + "\n")

    print(json.dumps(audit, indent=2, default=str))
    if not audit["success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
