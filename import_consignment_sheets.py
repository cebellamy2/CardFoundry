"""One-time backfill: import consignor payout history from Google Sheets CSV
exports into CardFoundry, without duplicating anything already tracked.

Each consignor's sheet is a historical record predating CardFoundry's own
consignment tracking (see backfill_consignor_setup.py, which fixed the
missing Consignor/Batch links this script depends on -- run that first).

Per row, the resolution rule (confirmed with the operator, do not change
without re-confirming):

1. If a matching card already exists in the consignor's batch, skip the
   row entirely -- it's already tracked, whatever its current status.
2. If not, the card is assumed sold (this is unconditional -- the sheet's
   own status column does NOT override this; presence in the batch is the
   only "still here" signal). Resolve what it sold for:
   a. A matching shipped Mana Pool order in CardFoundry's own history ->
      use that order's real price.
   b. No matching order -> assume a local/cash sale -> estimate using
      Mana Pool's current lowest seller-excluded competitor listing for
      that card at that condition (or better) -> flagged clearly as an
      ESTIMATE in the card's consignment_note, never treated as confirmed.
   c. Neither resolves -> flagged for manual review, never auto-imported.
3. consignment_amount_owed:
   - Row's sheet status is "paid" -> use the sheet's own consignor-cut
     column value directly (the real historical amount), NOT recomputed.
     A real ConsignorPayout is created for it.
   - Anything else -> computed fresh from CardFoundry's current tier
     table against the resolved sold price (a new amount, to be paid out
     later through the normal payout flow).

Duplicate rows within a sheet are processed independently (no dedup) --
confirmed some really are separate sales at different prices. Quantity > 1
rows are expanded into that many independent units.

Dry-run by default (writes a report, no DB writes, no live Mana Pool
writes -- but DOES make live *read* calls for order-history/price lookups,
since those inform the report). Pass --confirm to actually write.
"""

import argparse
import csv
import json
from datetime import datetime

from sqlalchemy.orm import Session

from consignment_service import (
    get_consignment_tiers,
    record_consignor_payout,
    resolve_consignment_payout,
)
from database import engine
from import_service import normalized_condition_id, normalized_finish_id, parse_price
from inventory_sync_service import inventory_sync_lease
from models import Batch, Consignor, InventoryCard, OrderItem, SalesOrder


DEFAULT_CONDITION_ID = "LP"


# One entry per CSV file. `consign_buy_col`/`consign_value` are only set for
# files that mix in non-consignment rows (CON_KEV2's outright purchases,
# which are skipped entirely, never processed by this script at all).
FILE_CONFIGS = [
    {
        "filename": "CON_CON.csv", "consignor_name": "Connor", "batch_code": "CON_CON",
        "name_col": "Name", "set_code_col": "Set code", "collector_col": "Collector number",
        "finish_col": "Foil", "quantity_col": "Quantity", "condition_col": None,
        "scryfall_id_col": None, "purchase_price_col": "Manapool Price", "sold_price_col": None,
        "consignor_cut_col": "Connor's Cut", "status_col": "Sold",
        "status_map": {"paid": "paid", "unsold": "listed"},
        "payment_reference_col": "Payment Number",
    },
    {
        "filename": "CON_KEV1.csv", "consignor_name": "Kevin", "batch_code": "CON_KEV1",
        "name_col": "Name", "set_code_col": "Set code", "collector_col": "Collector number",
        "finish_col": "Foil", "quantity_col": "Quantity", "condition_col": None,
        "scryfall_id_col": "Scryfall ID", "purchase_price_col": "Purchase price",
        "sold_price_col": "Sold Price", "consignor_cut_col": "Kevin's Cut", "status_col": "Status",
        "status_map": {"listed": "listed", "sold": "sold", "paid": "paid"},
        "payment_reference_col": None,
    },
    {
        "filename": "CON_KEV2.csv", "consignor_name": "Kevin", "batch_code": "CON_KEV1",
        "name_col": "Name", "set_code_col": "Set code", "collector_col": "Collector number",
        "finish_col": "Foil", "quantity_col": "Quantity", "condition_col": "Condition",
        "scryfall_id_col": "Scryfall ID", "purchase_price_col": "Purchase price",
        "sold_price_col": "Sold Price", "consignor_cut_col": "Kevin's Cut", "status_col": "Status",
        "status_map": {"listed": "listed", "sold": "sold", "paid": "paid"},
        "payment_reference_col": None,
        "consign_buy_col": "Consign/Buy", "consign_value": "Consign",
    },
    {
        "filename": "CON_LUC.csv", "consignor_name": "Luca", "batch_code": "CON_LUC",
        "name_col": "Name", "set_code_col": "Set code", "collector_col": "Collector number",
        "finish_col": "Foil", "quantity_col": "Quantity", "condition_col": "Condition",
        "scryfall_id_col": None, "purchase_price_col": "LP+ Market Price",
        "sold_price_col": "Sold Price", "consignor_cut_col": "Luca's Cut", "status_col": "Status",
        "status_map": {"listed": "listed", "sold": "sold", "paid": "paid"},
        "payment_reference_col": "Payment Batch",
    },
    {
        "filename": "CON_NIC.csv", "consignor_name": "Nick", "batch_code": "CON_NIC",
        "name_col": "Name", "set_code_col": "Set code", "collector_col": "Collector number",
        "finish_col": "Foil", "quantity_col": None, "condition_col": None,
        "scryfall_id_col": None, "purchase_price_col": "Purchase price",
        "sold_price_col": "Sold Price", "consignor_cut_col": "Nick's Cut", "status_col": None,
        "status_map": {}, "payment_reference_col": None,
    },
    {
        "filename": "CON_PAT.csv", "consignor_name": "Patrick", "batch_code": "CON_PAT",
        "name_col": "Name", "set_code_col": "Set code", "collector_col": "Collector number",
        "finish_col": "Foil", "quantity_col": "Quantity", "condition_col": None,
        "scryfall_id_col": None, "purchase_price_col": "Purchase price",
        "sold_price_col": "Sold Amt", "consignor_cut_col": "Patrick's Cut", "status_col": "Status",
        "status_map": {"listed": "listed", "sold": "sold", "paid": "paid"},
        "payment_reference_col": "Payment #",
    },
    {
        "filename": "CON_RAN.csv", "consignor_name": "Ransom", "batch_code": "CON_RAN",
        "name_col": "Name", "set_code_col": "Set code", "collector_col": "Collector number",
        "finish_col": "Foil", "quantity_col": "Quantity", "condition_col": "Condition",
        "scryfall_id_col": "Scryfall ID", "purchase_price_col": "Purchase price",
        "sold_price_col": "Sold Price", "consignor_cut_col": None, "status_col": "Status",
        "status_map": {"listed": "listed", "sold": "sold", "paid": "paid"},
        "payment_reference_col": None,
    },
    {
        "filename": "CON_RAN2.csv", "consignor_name": "Ransom", "batch_code": "CON_RAN",
        "name_col": "Name", "set_code_col": None, "collector_col": "Collector number",
        "finish_col": "Foil", "quantity_col": None, "condition_col": None,
        "scryfall_id_col": None, "purchase_price_col": None,
        "sold_price_col": "Sold Price", "consignor_cut_col": "Ransom's Cut", "status_col": "Status",
        "status_map": {"listed": "listed", "sold": "sold", "paid": "paid"},
        "payment_reference_col": None,
    },
    {
        "filename": "CON_RIC1.csv", "consignor_name": "Richard", "batch_code": "CON_RIC1",
        "name_col": "Name", "set_code_col": "Set code", "collector_col": "Collector number",
        "finish_col": "Foil", "quantity_col": "Quantity", "condition_col": None,
        "scryfall_id_col": "Scryfall ID", "purchase_price_col": "Purchase price",
        "sold_price_col": None, "consignor_cut_col": "Richard's Cut", "status_col": "Status",
        "status_map": {"listed": "listed", "sold": "sold", "paid": "paid"},
        "payment_reference_col": "Payment Batch",
    },
    {
        "filename": "CON_CAM.csv", "consignor_name": "Cameron", "batch_code": "CON_CAM",
        "name_col": "Name", "set_code_col": "Set code", "collector_col": "Collector number",
        "finish_col": "Foil", "quantity_col": "Quantity", "condition_col": None,
        "scryfall_id_col": "Scryfall ID", "purchase_price_col": "Purchase price",
        "sold_price_col": "Sold Price", "consignor_cut_col": "Cam's Cut", "status_col": "Status",
        "status_map": {"listed": "listed", "sold": "sold", "paid": "paid"},
        "payment_reference_col": None,
    },
]


def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def canonical_status(config, raw_row):
    status_col = config["status_col"]
    if not status_col:
        return "listed"
    raw = str(raw_row.get(status_col) or "").strip().lower()
    if not raw:
        return "listed"
    return config["status_map"].get(raw, raw)


def build_match_key(config, raw_row):
    """Returns (key_type, key_tuple) or None if there's not enough
    identity data in this row to match anything at all."""
    name = str(raw_row.get(config["name_col"]) or "").strip()
    if not name:
        return None

    scryfall_col = config.get("scryfall_id_col")
    scryfall_id = str(raw_row.get(scryfall_col) or "").strip() if scryfall_col else ""
    finish_id = normalized_finish_id(raw_row.get(config["finish_col"])) or "NF"

    if scryfall_id:
        return ("scryfall", (scryfall_id, finish_id))

    set_code_col = config.get("set_code_col")
    set_code = str(raw_row.get(set_code_col) or "").strip().upper() if set_code_col else ""
    collector = str(raw_row.get(config["collector_col"]) or "").strip().upper()

    if not collector:
        return None

    if set_code:
        return ("identity", (name.strip().lower(), set_code, collector, finish_id))
    # CON_RAN2 has no set code column at all -- match without it rather
    # than fail closed, flagged as lower-confidence in the report.
    return ("identity_no_set", (name.strip().lower(), collector, finish_id))


def card_match_key(card):
    """Same key shape as build_match_key, computed from a real InventoryCard,
    for comparing against sheet rows' keys."""
    finish_id = (card.finish_id or "NF").upper()
    if card.scryfall_id:
        return ("scryfall", (card.scryfall_id, finish_id))
    set_code = (card.set_code or "").upper()
    collector = (card.collector_number or "").upper()
    name = (card.name or "").strip().lower()
    if set_code:
        return ("identity", (name, set_code, collector, finish_id))
    return ("identity_no_set", (name, collector, finish_id))


def order_item_match_key(order_item):
    finish_id = (order_item.finish_id or order_item.finish or "NF")
    finish_id = normalized_finish_id(finish_id) or (finish_id or "NF").upper()
    if order_item.scryfall_id:
        return ("scryfall", (order_item.scryfall_id, finish_id))
    set_code = (order_item.set_code or "").upper()
    collector = (order_item.collector_number or "").upper()
    name = (order_item.name or "").strip().lower()
    if set_code:
        return ("identity", (name, set_code, collector, finish_id))
    return ("identity_no_set", (name, collector, finish_id))


def load_batch_card_index(session, batch_id):
    """{match_key: [InventoryCard, ...]} for every card currently in this
    batch, regardless of status -- sold cards keep their batch_id forever."""
    index = {}
    for card in session.query(InventoryCard).filter(InventoryCard.batch_id == batch_id):
        index.setdefault(card_match_key(card), []).append(card)
    return index


def load_shipped_order_index(session):
    """{match_key: [(OrderItem, SalesOrder), ...]} for every line item on a
    SalesOrder that actually shipped."""
    index = {}
    rows = (
        session.query(OrderItem, SalesOrder)
        .join(SalesOrder, OrderItem.order_id == SalesOrder.id)
        .filter(SalesOrder.status == "shipped")
        .all()
    )
    for order_item, order in rows:
        index.setdefault(order_item_match_key(order_item), []).append((order_item, order))
    return index


def parse_row_identity(config, raw_row):
    name = str(raw_row.get(config["name_col"]) or "").strip()
    set_code_col = config.get("set_code_col")
    set_code = str(raw_row.get(set_code_col) or "").strip().upper() if set_code_col else ""
    collector = str(raw_row.get(config["collector_col"]) or "").strip().upper()
    finish_id = normalized_finish_id(raw_row.get(config["finish_col"])) or "NF"
    condition_col = config.get("condition_col")
    if condition_col:
        condition_id = normalized_condition_id(raw_row.get(condition_col)) or DEFAULT_CONDITION_ID
    else:
        condition_id = DEFAULT_CONDITION_ID
    scryfall_col = config.get("scryfall_id_col")
    scryfall_id = str(raw_row.get(scryfall_col) or "").strip() if scryfall_col else ""
    return {
        "name": name, "set_code": set_code, "collector_number": collector,
        "finish_id": finish_id, "condition_id": condition_id, "scryfall_id": scryfall_id or None,
    }


def process_file(session, config, batch, consignor, source_dir, results,
                  claimed_order_ids, estimate_queue):
    path = f"{source_dir}/{config['filename']}"
    raw_rows = read_csv_rows(path)
    batch_index = load_batch_card_index(session, batch.id)
    order_index = load_shipped_order_index(session)

    consign_buy_col = config.get("consign_buy_col")
    consign_value = config.get("consign_value")

    for source_row_number, raw_row in enumerate(raw_rows, start=2):
        name = str(raw_row.get(config["name_col"]) or "").strip()
        if not name:
            continue  # footer/totals/blank row

        if consign_buy_col and str(raw_row.get(consign_buy_col) or "").strip().lower() != (
            (consign_value or "").lower()
        ):
            continue  # CON_KEV2 "Buy" row -- not a consignment, skip entirely

        quantity_col = config.get("quantity_col")
        try:
            quantity = int(float(raw_row.get(quantity_col))) if quantity_col and raw_row.get(quantity_col) else 1
        except ValueError:
            quantity = 1
        quantity = max(quantity, 1)

        status = canonical_status(config, raw_row)
        identity = parse_row_identity(config, raw_row)
        match_key = build_match_key(config, raw_row)

        purchase_price_col = config.get("purchase_price_col")
        purchase_price = (
            parse_price(raw_row.get(purchase_price_col)) if purchase_price_col else None
        )
        consignor_cut_col = config.get("consignor_cut_col")
        sheet_cut_value = (
            parse_price(raw_row.get(consignor_cut_col)) if consignor_cut_col else None
        )
        payment_ref_col = config.get("payment_reference_col")
        payment_reference = (
            str(raw_row.get(payment_ref_col) or "").strip() if payment_ref_col else ""
        )

        for unit_index in range(quantity):
            row_label = f"{config['filename']}:{source_row_number}" + (
                f" (unit {unit_index + 1}/{quantity})" if quantity > 1 else ""
            )

            if match_key is None:
                results.append({
                    "source": row_label, "name": name, "action": "manual_review",
                    "reason": "Not enough identity data to build a match key.",
                })
                continue

            candidates = batch_index.get(match_key) or []
            if candidates:
                claimed = candidates.pop(0)
                results.append({
                    "source": row_label, "name": name, "action": "already_tracked",
                    "reason": f"Matches existing InventoryCard {claimed.id} in {batch.batch_code}.",
                    "card_id": claimed.id,
                })
                continue

            # Not in the batch -> assumed sold, regardless of sheet status.
            order_matches = [
                pair for pair in (order_index.get(match_key) or [])
                if pair[0].id not in claimed_order_ids
            ]
            if order_matches:
                order_item, order = order_matches[0]
                claimed_order_ids.add(order_item.id)
                sold_price = (order_item.price_cents or 0) / 100
                results.append({
                    "source": row_label, "name": name, "action": "pending_finalize",
                    "identity": identity, "status": status, "purchase_price": purchase_price,
                    "sheet_cut_value": sheet_cut_value, "payment_reference": payment_reference,
                    "sold_price": sold_price, "price_source": "manapool_order",
                    "matched_order_id": order.external_order_id,
                    "batch_id": batch.id, "consignor_id": consignor.id,
                })
                continue

            sheet_sold_price_col = config.get("sold_price_col")
            sheet_sold_price = (
                parse_price(raw_row.get(sheet_sold_price_col)) if sheet_sold_price_col else None
            )
            if sheet_sold_price and status != "listed":
                results.append({
                    "source": row_label, "name": name, "action": "pending_finalize",
                    "identity": identity, "status": status, "purchase_price": purchase_price,
                    "sheet_cut_value": sheet_cut_value, "payment_reference": payment_reference,
                    "sold_price": sheet_sold_price, "price_source": "sheet_recorded",
                    "matched_order_id": None,
                    "batch_id": batch.id, "consignor_id": consignor.id,
                })
                continue

            # No CardFoundry order history and no sheet-recorded price ->
            # queue for the market-estimate fallback (batched at the end).
            estimate_key = f"{row_label}"
            estimate_queue.append({"key": estimate_key, "identity": {
                "name": identity["name"], "set_code": identity["set_code"] or "",
                "collector_number": identity["collector_number"],
                "finish_id": identity["finish_id"], "condition_id": identity["condition_id"],
                "scryfall_id": identity["scryfall_id"],
            }})
            results.append({
                "source": row_label, "name": name, "action": "pending_estimate",
                "estimate_key": estimate_key, "identity": identity, "status": status,
                "purchase_price": purchase_price, "sheet_cut_value": sheet_cut_value,
                "payment_reference": payment_reference,
                "batch_id": batch.id, "consignor_id": consignor.id,
            })


def try_market_estimates(estimate_queue):
    """Live Mana Pool lookup for rows with no confirmed sale anywhere.
    Returns {key: competitor_price_cents or None}. Gracefully reports
    "unavailable" instead of crashing when Mana Pool credentials aren't
    configured in this environment (e.g. a local dry run)."""
    if not estimate_queue:
        return {}, None
    try:
        from competitor_pricing_service import SELLER_EXCLUSION_ID
        from manapool_service import (
            get_inventory_listings_by_ids,
            optimize_exact_variant_batch_with_conflicts,
        )
        from new_listing_pricing_service import price_new_listing_candidates
        # discover_seller_id() derives the seller UUID from /seller/orders,
        # which no longer includes a seller_id field in its response --
        # confirmed broken against Mana Pool's current API shape. The rest
        # of the app's competitor-pricing code already avoids it, defaulting
        # to this same pre-verified constant instead (competitor_pricing_
        # service.py:332) -- match that proven path rather than the broken one.
        seller_id = SELLER_EXCLUSION_ID
        outcome = price_new_listing_candidates(
            estimate_queue, optimize_exact_variant_batch_with_conflicts,
            get_inventory_listings_by_ids, seller_id,
        )
    except RuntimeError as exc:
        return {}, f"Mana Pool credentials unavailable: {exc}"
    except Exception as exc:  # noqa: BLE001 -- report, don't crash the run
        return {}, f"Market estimate lookup failed: {exc}"

    by_key = {}
    for row in outcome["results"]:
        if row["status"] == "priced":
            cents = row.get("competitor_price_cents")
            by_key[row["key"]] = round(cents / 100, 2) if cents else None
        else:
            by_key[row["key"]] = None
    return by_key, None


def finalize_row(session, row, tiers, estimates=None):
    """Turn a pending_finalize/pending_estimate result into either a
    completed import action or a manual_review flag."""
    if row["action"] == "pending_estimate":
        estimated = (estimates or {}).get(row["estimate_key"])
        if estimated is None:
            return {**row, "action": "manual_review", "reason": (
                "No CardFoundry order history and no Mana Pool competitor "
                "listing found -- price genuinely unresolvable."
            )}
        row = {**row, "sold_price": estimated, "price_source": "manapool_estimate"}

    sold_price = row["sold_price"]
    status = row["status"]
    is_paid = status == "paid"

    if is_paid and row.get("sheet_cut_value") is not None:
        owed = row["sheet_cut_value"]
        owed_source = "sheet_recorded_cut"
    else:
        owed = resolve_consignment_payout(tiers, sold_price)
        owed_source = "tier_computed"

    note_parts = [f"Backfilled from Google Sheets import ({row['source']})."]
    if row["price_source"] == "manapool_order":
        note_parts.append(f"Sold price confirmed from Mana Pool order {row['matched_order_id']}.")
    elif row["price_source"] == "sheet_recorded":
        note_parts.append("Sold price taken directly from the sheet's own recorded value.")
    elif row["price_source"] == "manapool_estimate":
        note_parts.append(
            "ESTIMATED sold price from Mana Pool's current lowest competitor "
            "listing -- no confirmed sale record exists. Verify before relying on this."
        )
    if owed_source == "sheet_recorded_cut":
        note_parts.append("Payout amount taken directly from the sheet's own cut column.")

    return {
        **row, "action": "import_sold", "consignment_amount_owed": owed,
        "owed_source": owed_source, "consignment_note": " ".join(note_parts),
        "is_paid": is_paid,
    }


def run(source_dir, confirm):
    with Session(engine) as session:
        tiers = get_consignment_tiers(session)
        results = []
        claimed_order_ids = set()
        estimate_queue = []

        consignor_by_name = {c.name: c for c in session.query(Consignor)}
        batch_by_code = {b.batch_code: b for b in session.query(Batch)}

        for config in FILE_CONFIGS:
            consignor = consignor_by_name.get(config["consignor_name"])
            batch = batch_by_code.get(config["batch_code"])
            if not consignor or not batch:
                results.append({
                    "source": config["filename"], "name": "*", "action": "manual_review",
                    "reason": f"Consignor/batch not found for {config['consignor_name']}/"
                              f"{config['batch_code']} -- run backfill_consignor_setup.py first.",
                })
                continue
            process_file(
                session, config, batch, consignor, source_dir, results,
                claimed_order_ids, estimate_queue,
            )

        estimates, estimate_error = try_market_estimates(estimate_queue)

        finalized = []
        for row in results:
            if row["action"] in ("pending_finalize", "pending_estimate"):
                finalized.append(finalize_row(session, row, tiers, estimates))
            else:
                finalized.append(row)

        summary = {
            "already_tracked": sum(1 for r in finalized if r["action"] == "already_tracked"),
            "manual_review": sum(1 for r in finalized if r["action"] == "manual_review"),
            "import_sold": sum(1 for r in finalized if r["action"] == "import_sold"),
            "import_sold_paid": sum(
                1 for r in finalized if r["action"] == "import_sold" and r.get("is_paid")
            ),
            "import_sold_estimated": sum(
                1 for r in finalized
                if r["action"] == "import_sold" and r.get("price_source") == "manapool_estimate"
            ),
            "total_owed_new": round(sum(
                r["consignment_amount_owed"] for r in finalized if r["action"] == "import_sold"
            ), 2),
            "market_estimate_error": estimate_error,
        }

        if confirm:
            for row in finalized:
                if row["action"] != "import_sold":
                    continue
                card = InventoryCard(
                    batch_id=row["batch_id"], name=row["identity"]["name"],
                    set_code=row["identity"]["set_code"] or None,
                    collector_number=row["identity"]["collector_number"] or None,
                    finish_id=row["identity"]["finish_id"],
                    condition_id=row["identity"]["condition_id"],
                    scryfall_id=row["identity"]["scryfall_id"],
                    status="sold", sold_price=row["sold_price"],
                    consignment_value=row["purchase_price"],
                    consignment_amount_owed=row["consignment_amount_owed"],
                    consignment_payout_status="owed",
                    consignment_note=row["consignment_note"],
                )
                session.add(card)
                session.flush()
                row["created_card_id"] = card.id
                if row["is_paid"]:
                    note = f"Backfilled from Google Sheets ({row['source']})"
                    if row.get("payment_reference"):
                        note += f" -- payment reference: {row['payment_reference']}"
                    payout = record_consignor_payout(
                        session, row["consignor_id"], [card.id],
                        method=None, note=note, paid_at=datetime.now(),
                    )
                    row["created_payout_id"] = payout.id
            session.commit()

        return {"mode": "CONFIRMED" if confirm else "DRY_RUN", "summary": summary, "rows": finalized}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, help="Directory containing the CON_*.csv files.")
    parser.add_argument("--confirm", action="store_true", help="Actually write changes.")
    parser.add_argument("--output", default=None, help="Path to write the full JSON report.")
    args = parser.parse_args()

    with inventory_sync_lease():
        report = run(args.source_dir, args.confirm)

    output_path = args.output or (
        "consignment_import_confirm_report.json" if args.confirm
        else "consignment_import_dryrun_report.json"
    )
    with open(output_path, "w") as handle:
        json.dump(report, handle, indent=2, sort_keys=True, default=str)

    print(json.dumps({"mode": report["mode"], "summary": report["summary"]}, indent=2, sort_keys=True))
    print(f"\nFull report written to {output_path}")


if __name__ == "__main__":
    main()
