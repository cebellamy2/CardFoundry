"""Preview the non-English physical-identity fold in inventory_mirror_service.

WHAT CHANGES. The mirror grouped local cards by (card.mtgjson_id, language,
condition, finish) and remote listings by the listing's own mtgjson_id, with no
physical-identity rule of any kind. Mana Pool does not file non-English
printings consistently -- measured live 2026-09-28, of 89 non-English seller
rows 50 carry the ENGLISH Scryfall object's id and 39 their own language's -- so
a non-English card whose id disagreed with its listing's was keyed into a
different group and the two never joined. v2.20.0 folds an unambiguous
one-to-one non-English pair together using physical_identity's one rule.

★ WHY THIS SCRIPT RUNS THE REAL PATH. It calls the real
inventory_sync_workflow._build_mirror_preview_from_snapshot -- the exact
function Perform Sync's own chain uses to build the mirror -- after the real
run_additive_mtgjson_backfill, which is Perform Sync's first step and cannot be
skipped here: a card with a NULL mtgjson_id is invisible to the mirror, and NULL
mtgjson is one of the conditions under test.

OLD vs NEW IN ONE PROCESS. The baseline is the same real function with
fold_non_english_physical_matches stubbed to return {}. That IS the old
behaviour by construction -- the fold is the only behavioural change in the
module (crosscheck now delegates its tuple to physical_identity, which is
behaviour-preserving and pinned by the suite). Both builds are fed the SAME
cards and the SAME single seller-inventory read, so no input can drift between
them.

THE TWO DIFFERENCES FROM THE REAL CHAIN, STATED PLAINLY:
  1. The order re-ingest is skipped. create_inventory_sync_preview opens its
     own sessions and COMMITS them (order ingestion, listing status), so
     calling it would write despite this transaction's rollback. Order ingest
     has no bearing on mirror grouping.
  2. Nothing is persisted: no inventory_sync_jobs row, no listing_status
     write, no quantity push.

NO MANA POOL WRITES. Reads only: the seller inventory walk and the backfill's
catalog reads. The transaction is rolled back regardless of outcome.

Usage, in the container:
    PYTHONPATH=/app /opt/venv/bin/python dry_run_non_english_mirror_fold.py --plan
"""
import argparse
import json
import logging

from sqlalchemy.orm import Session

from actor_context import set_script_actor
from database import engine

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "dry_run_non_english_mirror_fold"

CANDIDATE_CATEGORIES = ("increase_quantity", "decrease_quantity", "zero_candidate")


def _row_key(row):
    """Identify a row across the two builds. Keyed on the REMOTE listing where
    there is one, because that is the thing a quantity write addresses and it
    is stable across a fold; local-only rows are keyed on their identity."""
    if row.get("remote_product_id"):
        return ("remote", row["remote_product_id"])
    identity = row.get("canonical_identity") or {}
    return ("local", tuple(
        identity.get(field) for field in
        ("mtgjson_id", "language_id", "condition_id", "finish_id")
    ))


def _language(row):
    return str((row.get("canonical_identity") or {}).get("language_id") or "")


def diff_previews(old_preview, new_preview):
    old_rows = {_row_key(r): r for r in old_preview.get("rows") or []}
    new_rows = {_row_key(r): r for r in new_preview.get("rows") or []}
    changed = []
    for key in sorted(set(old_rows) | set(new_rows), key=repr):
        old = old_rows.get(key)
        new = new_rows.get(key)
        if old is not None and new is not None:
            comparable_old = {k: v for k, v in old.items() if k != "physical_identity_fold"}
            comparable_new = {k: v for k, v in new.items() if k != "physical_identity_fold"}
            if comparable_old == comparable_new and "physical_identity_fold" not in new:
                continue
        changed.append({
            "key": key,
            "name": (new or old).get("name"),
            "language_id": _language(new or old),
            "product_id": (new or old).get("remote_product_id"),
            "old_category": old.get("category") if old else None,
            "new_category": new.get("category") if new else None,
            "old_desired": old.get("desired_quantity") if old else None,
            "new_desired": new.get("desired_quantity") if new else None,
            "old_cards": old.get("local_contributing_card_ids") if old else None,
            "new_cards": new.get("local_contributing_card_ids") if new else None,
            "remote_quantity": (new or old).get("current_remote_quantity"),
            "fold": (new or {}).get("physical_identity_fold"),
            "reaches_apply_today": (new or old).get("category") in CANDIDATE_CATEGORIES,
        })
    return changed


def english_rows_are_identical(old_preview, new_preview):
    """English must be bit-for-bit unchanged, including dict keys."""
    def english(preview):
        return sorted(
            (json.dumps(row, sort_keys=True, default=str)
             for row in preview.get("rows") or []
             if _language(row).upper() == "EN"),
        )
    old, new = english(old_preview), english(new_preview)
    first_difference = next(
        (pair for pair in zip(old, new) if pair[0] != pair[1]), None,
    )
    return len(old), len(new), old == new, first_difference


def build_both(session, cards, remote_inventory):
    import inventory_mirror_service as mirror
    from inventory_sync_workflow import _build_mirror_preview_from_snapshot

    new_preview = _build_mirror_preview_from_snapshot(
        session, cards, remote_inventory, False,
    )
    real_fold = mirror.fold_non_english_physical_matches
    mirror.fold_non_english_physical_matches = lambda local, remote: {}
    try:
        old_preview = _build_mirror_preview_from_snapshot(
            session, cards, remote_inventory, False,
        )
    finally:
        mirror.fold_non_english_physical_matches = real_fold
    return old_preview, new_preview


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", action="store_true", required=True,
                        help="The only mode. Rolls back; sends nothing.")
    parser.add_argument("--what-if-card", type=int, default=None,
                        help="Also simulate a split by rewriting this card's "
                             "mtgjson_id inside the rolled-back transaction, "
                             "to exercise the fold on real data when "
                             "production currently holds no split.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.setLevel(logging.INFO)
    set_script_actor(SCRIPT_NAME)

    from manapool_service import (get_all_seller_inventory,
                                  get_single_catalog_by_product_ids)
    from models import InventoryCard, RemoteProductBinding
    from mtgjson_backfill_service import run_additive_mtgjson_backfill
    from physical_identity import is_english

    with Session(engine) as session:
        try:
            logger.info("%s: running run_additive_mtgjson_backfill (Perform "
                        "Sync's first step) -- a NULL-mtgjson card is "
                        "invisible to the mirror without it.", SCRIPT_NAME)
            run_additive_mtgjson_backfill(
                session, get_all_seller_inventory,
                get_single_catalog_by_product_ids,
                operator_note=f"{SCRIPT_NAME} dry run",
            )
            session.flush()

            remote_inventory = get_all_seller_inventory(min_quantity=0)
            cards = session.query(InventoryCard).order_by(InventoryCard.id).all()
            print(f"seller inventory rows: {len(remote_inventory)}")
            print(f"local cards:           {len(cards)}")
            print()

            old_preview, new_preview = build_both(session, cards, remote_inventory)
            old_rows = old_preview.get("rows") or []
            new_rows = new_preview.get("rows") or []
            changed = diff_previews(old_preview, new_preview)

            print("=== OLD vs NEW, PRODUCTION AS IT STANDS ===")
            print(f"total rows OLD:   {len(old_rows)}")
            print(f"total rows NEW:   {len(new_rows)}")
            print(f"rows CHANGED:     {len(changed)}")
            print(f"OLD categories:   {old_preview['summary']['categories']}")
            print(f"NEW categories:   {new_preview['summary']['categories']}")
            print(f"local hash OLD:   {old_preview['local_snapshot_hash']}")
            print(f"local hash NEW:   {new_preview['local_snapshot_hash']}")
            for entry in changed:
                print(f"  {entry['name']} ({entry['language_id']}) "
                      f"product={entry['product_id']}")
                print(f"     category {entry['old_category']} -> {entry['new_category']}")
                print(f"     desired  {entry['old_desired']} -> {entry['new_desired']}"
                      f"   remote={entry['remote_quantity']}")
                print(f"     cards    {entry['old_cards']} -> {entry['new_cards']}")
                print(f"     fold     {entry['fold']}")
                print(f"     reaches the apply TODAY: {entry['reaches_apply_today']}")

            total_en, new_en, identical, first_difference = english_rows_are_identical(
                old_preview, new_preview,
            )
            print()
            print("=== ENGLISH CONTROL ===")
            print(f"English rows OLD/NEW: {total_en} / {new_en}")
            print(f"byte-identical:       {identical}")
            if not identical:
                print(f"first difference:     {first_difference}")

            print()
            print("=== TODAY'S NON-ENGLISH BINDINGS ===")
            by_product = {}
            for row in new_rows:
                if row.get("remote_product_id"):
                    by_product[row["remote_product_id"]] = row
            non_en = [
                b for b in session.query(RemoteProductBinding).filter(
                    RemoteProductBinding.provider == "manapool",
                    RemoteProductBinding.binding_status == "validated",
                ).order_by(RemoteProductBinding.id).all()
                if not is_english(b.language_id)
            ]
            print(f"non-English validated bindings: {len(non_en)}")
            off = []
            for b in non_en:
                row = by_product.get(b.product_id)
                category = (row or {}).get("category")
                if category != "hold_equal":
                    off.append((b.id, b.language_id, category,
                                (row or {}).get("desired_quantity"),
                                (row or {}).get("current_remote_quantity")))
            print(f"still hold_equal: {len(non_en) - len(off)} / {len(non_en)}")
            for entry in off:
                print(f"  binding {entry[0]} ({entry[1]}): {entry[2]} "
                      f"desired={entry[3]} remote={entry[4]}")

            if args.what_if_card:
                print()
                print("=== WHAT-IF: A SYNTHETIC SPLIT ON REAL DATA ===")
                print("Rewrites one card's mtgjson_id INSIDE this rolled-back")
                print("transaction, because production currently holds no split.")
                target = session.get(InventoryCard, args.what_if_card)
                if target is None:
                    print(f"  card {args.what_if_card} not found")
                else:
                    was = target.mtgjson_id
                    target.mtgjson_id = "DEADBEEF-0000-0000-0000-000000000000"
                    session.flush()
                    print(f"  card {target.id} {target.name} "
                          f"({target.language_id}/{target.condition_id}/"
                          f"{target.finish_id}) {target.set_code} "
                          f"#{target.collector_number}")
                    print(f"  mtgjson_id {was} -> {target.mtgjson_id} (simulated)")
                    cards_after = session.query(InventoryCard).order_by(
                        InventoryCard.id).all()
                    old_w, new_w = build_both(session, cards_after, remote_inventory)
                    for label, preview_result in (("OLD", old_w), ("NEW", new_w)):
                        rows_for = [
                            r for r in preview_result["rows"]
                            if args.what_if_card in (
                                r.get("local_contributing_card_ids") or [])
                            or r.get("remote_product_id") in {
                                b.product_id for b in non_en
                                if args.what_if_card in json.loads(
                                    b.local_card_ids_json or "[]")
                            }
                        ]
                        print(f"  {label}:")
                        for r in rows_for:
                            print(f"     [{r['category']}] desired="
                                  f"{r.get('desired_quantity')} remote="
                                  f"{r.get('current_remote_quantity')} "
                                  f"cards={r.get('local_contributing_card_ids')} "
                                  f"product={r.get('remote_product_id')}"
                                  + ("  FOLDED" if r.get("physical_identity_fold") else ""))
                    target.mtgjson_id = was
                    session.flush()
        finally:
            session.rollback()
            print()
            print("PLAN ONLY -- transaction rolled back, no Mana Pool request made.")


if __name__ == "__main__":
    main()
