"""Correct the stale LOCAL price on cards that can come back on sale but
that no automated path can reprice.

WHAT IS WRONG, AND WHY ONLY THESE CARDS. The pricing cron is Mana Pool's
own bulk job and its filter is `inventoryFilters: {minQuantity: 1}`
(bulk_pricing_service.BULK_FILTERS), so a quantity-0 listing is never
evaluated -- proved on 2026-10-29 by submitting one preview job with
`minQuantity: 0`, which Mana Pool echoed back and still evaluated only
the live listings. The seller-inventory price write-back no longer reads a
quantity-0 listing either (v2.74.0). So for a card whose only Mana Pool
listing is dormant, `InventoryCard.current_price` is whatever intake or an
operator last typed, and nothing will ever correct it.

That is harmless while the card is not for sale. It stops being harmless
the moment the card comes back, because since v2.74.0 the reconciliation
restock sends OUR price with the quantity
(inventory_reconciliation_service.restock_price_cents). Measured against
production on 2026-10-30, of 171 such cards: Snuff Out SLD 1792 EN/LP/FO
held $1.00 against a $14.42 competitor, and The Arkenstone // Seek the
Heart HOB 270 EN/LP/FO held $293.07 against $68.25.

A high price was never a keep-it-from-selling decision -- the operator
confirmed on 2026-10-30 that those were placeholders they expected the
price updater to fix. So both directions are stale, and neither gets a
marker.

WHAT THIS WRITES. One field, `InventoryCard.current_price`, through
local_price_writeback_service.set_card_price, which is the one guarded
local price-write path in this codebase: it writes the
InventoryPriceHistory + InventoryChangeLog pair every other price change
writes, and returns False without writing when the price already matches.
★ NO MANA POOL WRITE OF ANY KIND. Nothing here changes a listing. A
corrected price reaches Mana Pool only later, when a restock or a first
listing carries it, which is the whole point.

SCOPE, AND WHAT IS DELIBERATELY LEFT ALONE. Only a card that is
removed/unsellable/reserved, sits on a dormant-only listing, HAS a local
price, and has a resolved seller-excluded competitor. Everything else is
reported and untouched:

  * The 46 unpriced ones. An unpriced card is refused by every raising
    path (_raisable_gap's `unpriced`, restock_price_cents'
    `unpriced_at_apply`, push_return_to_sellable's `no_price`), which is a
    SAFE state. Writing them a price would make 46 cards restockable that
    are not restockable today -- a behaviour change that is the operator's
    to make, not a side effect of a price correction. Out of scope.
  * Cards Mana Pool named as having no competitor at all.
  * Cards the rate limit left unresolved this run.
  * Cards with no canonical identity (canonical_key returns None), which
    cannot be matched to a listing on either side.
  ★ price_usd is NEVER a fallback. It is the import-time record, the same
  reason restock_price_cents refuses to consult it. No comp, no write.

  The four orphan $100.00 LP listings (ECL 150, ECL 408, ECC 89, SPG 130)
  are untouched by construction: all four have NO local card of any
  status, so there is no row here to correct. They need a Mana Pool write,
  which is a different decision and a different ticket.

THE DRY RUN RUNS THE REAL APPLY AND ROLLS IT BACK -- it does not
recompute an answer in a second code path. That is the
dry_run_new_listings.py shape, and the reason for it is v2.12.0, where a
preview that computed its own answer reported 8 and the apply published
59. set_card_price never commits; the caller does, so a rollback here
undoes everything.

★ WHY THE APPROVED SET IS A FILE AND NOT A ROW. Shrink-only needs the
dry run's card-id set to still exist when --confirm runs in a later
process. The dry run may not write to the database -- that is the one
thing it promises -- so the set is written to a plan file instead, and
--confirm requires it. Between the two, the live set must be a SUBSET of
the approved one and every dropout must be explained; anything new or
unexplained stops the run (CLAUDE.md's shrink-only rule, which matters
here because this population moved 211 -> 217 in a single day).

    # dry run (writes no database row; writes the plan file)
    PYTHONPATH=/app /opt/venv/bin/python correct_dormant_returnable_prices.py

    # apply what was approved
    PYTHONPATH=/app /opt/venv/bin/python correct_dormant_returnable_prices.py \
        --confirm --plan dormant_price_correction_plan.json

    # one step back
    PYTHONPATH=/app /opt/venv/bin/python correct_dormant_returnable_prices.py --undo
"""

import argparse
import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from actor_context import set_script_actor
from competitor_pricing_service import (
    SELLER_EXCLUSION_ID,
    price_exact_variants_excluding_seller,
)
from database import engine
from inventory_mirror_service import canonical_key, remote_key
from inventory_sync_service import inventory_sync_lease
from local_price_writeback_service import PRICING_FLOOR_CENTS, set_card_price
from manapool_service import (
    get_all_seller_inventory,
    get_inventory_listings_by_ids,
    optimize_exact_variant_batch_with_conflicts,
)
from models import AppSetting, InventoryCard

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "correct_dormant_returnable_prices"
# The source label on every InventoryPriceHistory row this script writes.
# Deliberately its own, not reused from local_price_writeback_service's
# three automatic ones: a reader has to be able to tell this correction
# from a cron write, and --undo keys on it.
PRICE_SOURCE = "dormant_price_correction"
# The one AppSetting row this script maintains, so --undo has something to
# read. Written in the SAME transaction as the price writes.
UNDO_SETTING_KEY = "dormant_price_correction_last_run"
DEFAULT_PLAN_PATH = "dormant_price_correction_plan.json"

# A card in one of these statuses is off sale but can be put back by an
# operator action: unsellable -> available (transition_sellability),
# removed -> available (un_remove_card), and reserved via the
# exception-found / substitution routes. `sold` is excluded because it is
# terminal for those routes -- a sold card only returns through
# release_order, which pushes nothing and leaves the next reconciliation
# to send our price.
RETURNABLE_STATUSES = ("removed", "unsellable", "reserved")

# Pacing. Measured on 2026-10-30: batches of 20 at 8s between request
# starts left 65 of 171 cards unpriced, because Mana Pool asked for 36-48s
# of quiet and manapool_service only holds a request open for 30s, so it
# correctly fails fast rather than retrying into a shut window. Batches of
# 10 at 25s resolved 48 of the 61 that were left. Slow is the point.
OPTIMIZER_BATCH_SIZE = 10
OPTIMIZER_REQUEST_INTERVAL_SECONDS = 25.0
OPTIMIZER_PASSES = 3
UNDERCUT_CENTS = 5

SKIP_UNPRICED = "no local price -- out of scope, and a safe state"
SKIP_ON_LIVE_LISTING = "listing holds stock; the pricing cron maintains this price"
SKIP_NO_IDENTITY = "card has no canonical identity, so no listing can be matched"
SKIP_NO_LISTING = "no Mana Pool listing at all"
SKIP_NO_TARGET = "no resolved seller-excluded competitor this run"


class DormantPriceCorrectionError(RuntimeError):
    pass


def _cents(value) -> int | None:
    if value is None:
        return None
    return int(round(float(value) * 100))


def classify_cards(session: Session, seller_inventory: list[dict]) -> dict:
    """Split every returnable card into in-scope and skipped, with a reason.

    Reads only. The identity on both sides is inventory_mirror_service's
    own canonical_key/remote_key -- the same four fields Perform Sync
    matches on -- so this cannot disagree with what a restock would do.
    """
    live_keys = set()
    dormant_items = {}
    for item in seller_inventory:
        key = remote_key(item)
        if not key:
            continue
        if int(item.get("quantity") or 0) >= 1:
            live_keys.add(key)
        else:
            dormant_items[key] = item
    dormant_only = {
        key: item for key, item in dormant_items.items() if key not in live_keys
    }

    in_scope = []
    skipped = []
    for card in (
        session.query(InventoryCard)
        .filter(InventoryCard.status.in_(RETURNABLE_STATUSES))
        .order_by(InventoryCard.id)
        .all()
    ):
        row = {
            "card_id": card.id, "name": card.name, "status": card.status,
            "set_code": card.set_code, "collector_number": card.collector_number,
            "language_id": card.language_id, "condition_id": card.condition_id,
            "finish_id": card.finish_id,
            "current_price_cents": _cents(card.current_price),
        }
        key = canonical_key(card)
        if key is None:
            skipped.append({**row, "skip_reason": SKIP_NO_IDENTITY})
            continue
        row["identity"] = list(key)
        if key in live_keys:
            skipped.append({**row, "skip_reason": SKIP_ON_LIVE_LISTING})
            continue
        item = dormant_only.get(key)
        if item is None:
            skipped.append({**row, "skip_reason": SKIP_NO_LISTING})
            continue
        row["listing_inventory_id"] = str(item.get("id") or "")
        row["listing_product_id"] = str(item.get("product_id") or "")
        row["listing_price_cents"] = int(item.get("price_cents") or 0)
        if row["current_price_cents"] is None or row["current_price_cents"] < 1:
            skipped.append({**row, "skip_reason": SKIP_UNPRICED})
            continue
        in_scope.append({**row, "item": item})
    return {
        "in_scope": in_scope,
        "skipped": skipped,
        "dormant_only_keys": len(dormant_only),
        "live_keys": len(live_keys),
    }


def build_plan(
    session: Session,
    inventory_loader=None,
    optimizer_call=None,
    listings_call=None,
    seller_id: str = SELLER_EXCLUSION_ID,
) -> dict:
    """What this run would change, and what it would leave alone.

    One seller-inventory read at min_quantity=0 -- the dormant listings
    have to be visible, which is exactly why the pricing cron cannot see
    them -- then one exact-variant competitor read per batch.

    ★ The three Mana Pool callables default to None and are resolved from
    this module HERE, not in the signature. A default argument binds once
    at import, so a signature default would ignore anything patched
    afterwards -- which is exactly how the first run of these tests opened
    a real connection to manapool.com and the suite's network guard caught
    it. Same reasoning as tests/conftest.py's note about time.sleep.
    """
    inventory_loader = inventory_loader or get_all_seller_inventory
    optimizer_call = optimizer_call or optimize_exact_variant_batch_with_conflicts
    listings_call = listings_call or get_inventory_listings_by_ids
    seller_inventory = inventory_loader(min_quantity=0)
    split = classify_cards(session, seller_inventory)
    in_scope = split["in_scope"]
    skipped = list(split["skipped"])

    pricing = {"targets": {}, "holds": [], "spread": {}, "calls": 0,
               "failures": 0, "retries": 0, "variants": 0}
    if in_scope:
        pricing = price_exact_variants_excluding_seller(
            [row["item"] for row in in_scope],
            optimizer_call,
            listings_call,
            seller_id=seller_id,
            undercut_cents=UNDERCUT_CENTS,
            floor_cents=PRICING_FLOOR_CENTS,
            batch_limit=OPTIMIZER_BATCH_SIZE,
            passes=OPTIMIZER_PASSES,
            min_request_interval=OPTIMIZER_REQUEST_INTERVAL_SECONDS,
        )

    hold_reason_by_inventory_id = {
        str(hold.get("inventory_id") or ""): hold.get("validation_reason")
        for hold in pricing["holds"]
    }
    changes = []
    unchanged = []
    for row in in_scope:
        item = row.pop("item")
        target = None
        for identity, candidate in pricing["targets"].items():
            if candidate["source_inventory_id"] == row["listing_inventory_id"]:
                target = candidate
                break
        if target is None:
            reason = hold_reason_by_inventory_id.get(
                row["listing_inventory_id"],
            ) or SKIP_NO_TARGET
            skipped.append({**row, "skip_reason": reason})
            continue
        enriched = {
            **row,
            "competitor_price_cents": target["competitor_price_cents"],
            "competitor_condition": target["competitor_condition"],
            "competitor_inventory_id": target["competitor_inventory_id"],
            "competitor_prices_seen": target["competitor_prices_seen"],
            "passes_resolved": target["passes_resolved"],
            "target_price_cents": target["target_price_cents"],
        }
        enriched["change_cents"] = (
            enriched["target_price_cents"] - enriched["current_price_cents"]
        )
        if enriched["change_cents"] == 0:
            unchanged.append(enriched)
        else:
            changes.append(enriched)

    changes.sort(key=lambda row: row["change_cents"])
    increases = [row for row in changes if row["change_cents"] > 0]
    decreases = [row for row in changes if row["change_cents"] < 0]
    return {
        "preview_only": True,
        "built_at": datetime.now().isoformat(),
        "script": SCRIPT_NAME,
        "price_source": PRICE_SOURCE,
        "undercut_cents": UNDERCUT_CENTS,
        "floor_cents": PRICING_FLOOR_CENTS,
        "live_listing_keys": split["live_keys"],
        "dormant_only_listing_keys": split["dormant_only_keys"],
        "changes": changes,
        "unchanged": unchanged,
        "skipped": skipped,
        "approved_card_ids": sorted(row["card_id"] for row in changes),
        "summary": {
            "returnable_cards_seen": len(in_scope) + len(split["skipped"]),
            "in_scope_with_a_price": len(in_scope),
            "variants_requested": pricing["variants"],
            "variants_resolved": len(pricing["targets"]),
            "would_change": len(changes),
            "already_at_target": len(unchanged),
            "skipped": len(skipped),
            "increase_count": len(increases),
            "increase_cents": sum(row["change_cents"] for row in increases),
            "decrease_count": len(decreases),
            "decrease_cents": sum(row["change_cents"] for row in decreases),
            "net_cents": sum(row["change_cents"] for row in changes),
            "optimizer_calls": pricing["calls"],
            "optimizer_retries": pricing["retries"],
            "optimizer_failures": pricing["failures"],
            "variants_disagreeing_across_passes": sum(
                1 for prices in pricing["spread"].values() if len(set(prices)) > 1
            ),
        },
    }


def compare_against_approved(approved_ids, plan: dict) -> dict:
    """Shrink-only. The live set must be a SUBSET of what was approved.

    A dropout is fine and is explained from live state; anything NEW is
    not, because nobody approved it. Same rule, and the same reason, as
    the bulk-run guard in CLAUDE.md: between an approved preview and an
    apply the shop keeps trading, so the set may only shrink.
    """
    approved = set(int(card_id) for card_id in approved_ids)
    live = set(row["card_id"] for row in plan["changes"])
    return {
        "approved": sorted(approved),
        "live": sorted(live),
        "added": sorted(live - approved),
        "dropped": sorted(approved - live),
    }


def explain_dropouts(session: Session, card_ids, plan: dict) -> list[dict]:
    """Why an approved card is no longer in the plan. Read from live state.

    An unexplained dropout is reported as such rather than waved through:
    the point of the check is that every difference has a cause somebody
    can name.
    """
    skipped_by_id = {row["card_id"]: row for row in plan["skipped"]}
    unchanged_ids = {row["card_id"] for row in plan["unchanged"]}
    explained = []
    for card_id in card_ids:
        card = session.get(InventoryCard, card_id)
        if card is None:
            explained.append({"card_id": card_id, "explanation": "card no longer exists"})
            continue
        if card.status == "available":
            explained.append({
                "card_id": card_id, "status": card.status,
                "explanation": "back on sale; the reconciliation path owns it now",
            })
            continue
        if card.status == "sold":
            explained.append({
                "card_id": card_id, "status": card.status,
                "explanation": "sold since the preview",
            })
            continue
        if card_id in unchanged_ids:
            explained.append({
                "card_id": card_id, "status": card.status,
                "explanation": "already at the target price",
            })
            continue
        if card_id in skipped_by_id:
            explained.append({
                "card_id": card_id, "status": card.status,
                "explanation": skipped_by_id[card_id]["skip_reason"],
            })
            continue
        explained.append({
            "card_id": card_id, "status": card.status,
            "explanation": None,
        })
    return explained


def apply_plan(session: Session, plan: dict, *, approved_ids=None) -> dict:
    """Write every change in the plan. Commits nothing -- the caller does.

    This is the one code path that writes, and the dry run calls it too,
    inside a transaction it rolls back. Nothing below may be skipped in a
    dry run, or the dry run stops being evidence about the apply.
    """
    if approved_ids is not None:
        difference = compare_against_approved(approved_ids, plan)
        if difference["added"]:
            raise DormantPriceCorrectionError(
                "Refusing to apply: "
                f"{len(difference['added'])} card(s) are in the live plan but were "
                f"not in the approved preview: {difference['added']}. "
                "Re-run the dry run and have the new preview approved."
            )
        unexplained = [
            row for row in explain_dropouts(session, difference["dropped"], plan)
            if row["explanation"] is None
        ]
        if unexplained:
            raise DormantPriceCorrectionError(
                "Refusing to apply: "
                f"{len(unexplained)} approved card(s) left the plan with no "
                f"explanation: {[row['card_id'] for row in unexplained]}."
            )

    written = []
    unchanged = []
    missing = []
    for row in plan["changes"]:
        card = session.get(InventoryCard, row["card_id"])
        if card is None:
            # Catch-and-continue, so it logs: a card deleted between the
            # read and the write is a real thing that must not take the
            # rest of the run down, and must not vanish silently either.
            logger.warning(
                "%s: card %s is no longer in the database; not priced.",
                SCRIPT_NAME, row["card_id"],
            )
            missing.append(row["card_id"])
            continue
        before_cents = _cents(card.current_price)
        changed = set_card_price(
            session, card, int(row["target_price_cents"]),
            source=PRICE_SOURCE,
            note=(
                "stale local price on a dormant-only listing corrected to "
                f"lowest seller-excluded competitor {row['competitor_price_cents']}c "
                f"minus {UNDERCUT_CENTS}c, floor {PRICING_FLOOR_CENTS}c "
                f"(competitor listing {row['competitor_inventory_id']}, "
                f"condition {row['competitor_condition']})"
            ),
        )
        record = {
            "card_id": card.id, "name": card.name,
            "old_price_cents": before_cents,
            "new_price_cents": int(row["target_price_cents"]),
            "competitor_price_cents": row["competitor_price_cents"],
        }
        (written if changed else unchanged).append(record)

    if written:
        _record_undo_snapshot(session, written)
    logger.info(
        "%s: priced=%s already_at_target=%s missing=%s",
        SCRIPT_NAME, len(written), len(unchanged), len(missing),
    )
    return {"written": written, "unchanged": unchanged, "missing": missing}


def _record_undo_snapshot(session: Session, written: list[dict]) -> None:
    """Store what this run wrote, so --undo has a source of truth.

    In the same transaction as the price writes, so a rolled-back dry run
    leaves no snapshot either -- the snapshot and the writes are one
    event.
    """
    setting = (
        session.query(AppSetting).filter(AppSetting.key == UNDO_SETTING_KEY).first()
    )
    payload = json.dumps({
        "ran_at": datetime.now().isoformat(),
        "undone_at": None,
        "rows": [
            {
                "card_id": row["card_id"],
                "old_price_cents": row["old_price_cents"],
                "new_price_cents": row["new_price_cents"],
            }
            for row in written
        ],
    }, sort_keys=True)
    if setting is None:
        session.add(AppSetting(key=UNDO_SETTING_KEY, value=payload))
    else:
        setting.value = payload
        setting.updated_at = datetime.now()


def undo_last_run(session: Session) -> dict:
    """Put every price this script last wrote back, through the same path.

    One step, like backfill_actor_attribution's --undo. The restore is
    itself a set_card_price, so it gets its own audit rows -- a reversal
    is a price change too, and the history has to show both.

    A card whose price has moved since this run is left alone and
    reported: something else decided that price afterwards, and stepping
    over it would be this script overruling a newer decision.
    """
    setting = (
        session.query(AppSetting).filter(AppSetting.key == UNDO_SETTING_KEY).first()
    )
    if setting is None:
        raise DormantPriceCorrectionError(
            "There is no recorded run to undo."
        )
    snapshot = json.loads(setting.value or "{}")
    if snapshot.get("undone_at"):
        raise DormantPriceCorrectionError(
            f"That run was already undone at {snapshot['undone_at']}."
        )
    restored = []
    skipped = []
    for row in snapshot.get("rows") or []:
        card = session.get(InventoryCard, row["card_id"])
        if card is None:
            logger.warning(
                "%s undo: card %s is no longer in the database.",
                SCRIPT_NAME, row["card_id"],
            )
            skipped.append({**row, "reason": "card no longer exists"})
            continue
        if _cents(card.current_price) != row["new_price_cents"]:
            logger.warning(
                "%s undo: card %s now holds %sc, not the %sc this run wrote; "
                "leaving it alone.",
                SCRIPT_NAME, card.id, _cents(card.current_price),
                row["new_price_cents"],
            )
            skipped.append({**row, "reason": "price changed since this run"})
            continue
        if row["old_price_cents"] is None:
            # Cannot happen from build_plan, which only ever includes a
            # card that already has a price -- recorded rather than
            # guessed, because restoring a card to "no price" through a
            # path that takes an int is not something to improvise.
            logger.warning(
                "%s undo: card %s had no recorded old price; leaving it alone.",
                SCRIPT_NAME, card.id,
            )
            skipped.append({**row, "reason": "no old price recorded"})
            continue
        set_card_price(
            session, card, int(row["old_price_cents"]),
            source=PRICE_SOURCE,
            note=f"undo of {SCRIPT_NAME}: restored the price held before the correction",
        )
        restored.append(row)
    snapshot["undone_at"] = datetime.now().isoformat()
    setting.value = json.dumps(snapshot, sort_keys=True)
    setting.updated_at = datetime.now()
    logger.info(
        "%s undo: restored=%s skipped=%s", SCRIPT_NAME, len(restored), len(skipped),
    )
    return {"restored": restored, "skipped": skipped}


def _report(plan: dict, outcome: dict | None, mode: str) -> dict:
    return {
        "mode": mode,
        "summary": plan["summary"],
        "changes": plan["changes"],
        "unchanged": [row["card_id"] for row in plan["unchanged"]],
        "skipped": plan["skipped"],
        "outcome": outcome,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--confirm", action="store_true",
        help="Write. Dry run otherwise (the real apply, rolled back).",
    )
    parser.add_argument(
        "--undo", action="store_true",
        help="Restore the prices the last confirmed run wrote. One step.",
    )
    parser.add_argument(
        "--plan", default=None,
        help="Approved plan file from a dry run. Required with --confirm.",
    )
    parser.add_argument(
        "--plan-out", default=DEFAULT_PLAN_PATH,
        help=f"Where a dry run writes its plan (default {DEFAULT_PLAN_PATH}).",
    )
    args = parser.parse_args()
    set_script_actor(SCRIPT_NAME)

    if args.undo:
        with inventory_sync_lease():
            with Session(engine) as session:
                try:
                    outcome = undo_last_run(session)
                except DormantPriceCorrectionError as exc:
                    print(json.dumps({"mode": "UNDO_REFUSED", "reason": str(exc)}, indent=2))
                    return 1
                session.commit()
        print(json.dumps({"mode": "UNDONE", **outcome}, indent=2, sort_keys=True))
        return 0

    approved_ids = None
    if args.confirm:
        if not args.plan:
            print(json.dumps({
                "mode": "REFUSED",
                "reason": "--confirm requires --plan <file> from an approved dry run.",
            }, indent=2))
            return 1
        with open(args.plan) as handle:
            approved_ids = json.load(handle)["approved_card_ids"]

    with inventory_sync_lease():
        with Session(engine) as session:
            plan = build_plan(session)
            try:
                outcome = apply_plan(session, plan, approved_ids=approved_ids)
            except DormantPriceCorrectionError as exc:
                session.rollback()
                print(json.dumps({
                    "mode": "REFUSED", "reason": str(exc),
                    "summary": plan["summary"],
                }, indent=2, sort_keys=True))
                return 1
            if args.confirm:
                session.commit()
                report = _report(plan, outcome, "CONFIRMED")
            else:
                # The real apply ran; this throws it away. Everything above
                # is the same code the --confirm path just executed.
                session.rollback()
                report = _report(plan, outcome, "DRY_RUN")
                with open(args.plan_out, "w") as handle:
                    json.dump(plan, handle, indent=2, sort_keys=True, default=str)
                report["plan_written_to"] = args.plan_out
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
