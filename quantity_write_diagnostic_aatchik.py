"""Single-use approved Mana Pool quantity round-trip diagnostic."""

import json
from datetime import datetime, timezone
from pathlib import Path

from manapool_service import _post_json, get_all_seller_inventory


INVENTORY_ID = "51ff0a6b-c5dc-452b-abbd-f648ef776767"
PRODUCT_ID = "1ee62dad-8005-4905-9aee-bc4340f9a1f3"
ORIGINAL_QUANTITY = 2
TEMPORARY_QUANTITY = 1
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
LOG_PATH = Path("quantity_write_diagnostic_aatchik_20260813.json")


audit = {
    "diagnostic": "approved_quantity_round_trip",
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


def snapshot() -> dict:
    matches = [
        item for item in get_all_seller_inventory(min_quantity=0)
        if str(item.get("id") or "") == INVENTORY_ID
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one target listing, found {len(matches)}")
    item = matches[0]
    single = ((item.get("product") or {}).get("single") or {})
    actual = {key: str(single.get(key) or "") for key in EXPECTED_SINGLE}
    if actual != EXPECTED_SINGLE:
        raise RuntimeError(f"Target identity mismatch: {actual!r}")
    if str(item.get("product_id") or "") != PRODUCT_ID:
        raise RuntimeError("Target product_id changed")
    return {
        "inventory_id": str(item.get("id") or ""),
        "product_id": str(item.get("product_id") or ""),
        "quantity": int(item.get("quantity") or 0),
        "price_cents": int(item.get("price_cents") or 0),
        "effective_as_of": item.get("effective_as_of"),
        "single": actual,
    }


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
        before = snapshot()
        record("before", snapshot=before)
        if before["quantity"] != ORIGINAL_QUANTITY:
            raise RuntimeError(f"Pre-write quantity is {before['quantity']}, expected 2")
        if before["price_cents"] != ORIGINAL_PRICE_CENTS:
            raise RuntimeError(f"Pre-write price is {before['price_cents']}, expected 15")

        temporary_payload = payload(TEMPORARY_QUANTITY)
        record("temporary_write_planned", endpoint="/seller/inventory/product", payload=temporary_payload)
        write_attempted = True
        response = _post_json("/seller/inventory/product", temporary_payload)
        record("temporary_write_response", response=response)

        changed = snapshot()
        record("temporary_readback", snapshot=changed)
        if changed["quantity"] != TEMPORARY_QUANTITY:
            raise RuntimeError(f"Temporary quantity is {changed['quantity']}, expected 1")
        if changed["price_cents"] != ORIGINAL_PRICE_CENTS:
            raise RuntimeError(f"Temporary price is {changed['price_cents']}, expected 15")
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
                restored = snapshot()
                record("restore_readback", snapshot=restored)
                if restored["quantity"] != ORIGINAL_QUANTITY:
                    raise RuntimeError(f"Restored quantity is {restored['quantity']}, expected 2")
                if restored["price_cents"] != ORIGINAL_PRICE_CENTS:
                    raise RuntimeError(f"Restored price is {restored['price_cents']}, expected 15")
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
