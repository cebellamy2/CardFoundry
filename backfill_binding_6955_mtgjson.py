"""Give binding 6955 the MTGJSON id its own card already carries.

WHY THIS ONE BINDING. The mirror substitutes a synthetic key into the
mtgjson_id slot for a binding that has none, and reconciliation's
desired-quantity recompute then has nothing real to match on. Binding 6955
(DW, Dwarven Warriors, HOC/93, NM/NF) is the one case where that is simply
missing data rather than a decision: card 10797 under that binding already
carries 943111c2-ded8-515b-90d5-947eb729e932, and Mana Pool's own catalog
gives the identical value for the product. Filling it in removes this
binding from the synthetic-key population entirely.

NOT 6742. That binding's NULL is an OPERATOR DECISION -- override-confirmed
2026-09-17, note "I have no idea" -- and the operator reaffirmed on
2026-10-04 that it stays. This script will not touch it, and refuses any
binding that is override-confirmed.

WHY NOT THE CANONICAL BACKFILL PATH. execute_mtgjson_backfill writes both
card.mtgjson_id and binding.mtgjson_id, but it is driven by a preview whose
candidates are cards with a NULL mtgjson_id. Card 10797 already has one, so
it is not a candidate and that path cannot reach this binding. This is
therefore a targeted correction, and it mirrors that path's audit shape
(InventoryChangeLog, same action_type vocabulary) so one reader can see
both.

★ THE THREE VALUES MUST AGREE OR NOTHING IS WRITTEN:
    the binding's current mtgjson_id      must be NULL
    the card's mtgjson_id                 the value to write
    Mana Pool's catalog for that product  must equal the card's
A disagreement means the premise is wrong, so the script aborts rather than
guessing which source to trust.

READ-ONLY AGAINST MANA POOL. One documented v0.34.0 read,
GET /seller/inventory/product/{type}/{id}, purely to verify the third
value. Nothing is written remotely. The only write is one local column.

UNDOABLE. The before/after is recorded in InventoryChangeLog and printed, so
the reversal is a single UPDATE back to NULL from the logged old value.

Usage, in the container:
    cd /app && PYTHONPATH=/app /opt/venv/bin/python backfill_binding_6955_mtgjson.py
    cd /app && PYTHONPATH=/app /opt/venv/bin/python backfill_binding_6955_mtgjson.py --confirm
"""
import argparse
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from actor_context import current_actor, set_script_actor
from database import engine
from models import InventoryCard, InventoryChangeLog, RemoteProductBinding

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "backfill_binding_6955_mtgjson"
BINDING_ID = 6955
PRODUCT_ID = "44c8137c-cc86-46bf-b97d-babab827e6ff"


class PremiseFailed(RuntimeError):
    """The three sources disagree, so the correction is not safe to make."""


def _catalog_mtgjson(product_id: str) -> str | None:
    """Mana Pool's own MTGJSON id for this product. Read-only."""
    from manapool_service import _get_json
    response = _get_json(f"/seller/inventory/product/mtg_single/{product_id}", params={})
    single = ((response.get("inventory") or {}).get("product") or {}).get("single") or {}
    return (single.get("mtgjson_id") or "").strip() or None


def plan(session: Session, *, catalog_loader=_catalog_mtgjson) -> dict:
    """Verify the premise and return what would change. Writes nothing."""
    binding = session.get(RemoteProductBinding, BINDING_ID)
    if binding is None:
        raise PremiseFailed(f"Binding {BINDING_ID} does not exist.")
    if binding.product_id != PRODUCT_ID:
        raise PremiseFailed(
            f"Binding {BINDING_ID} is on product {binding.product_id}, not {PRODUCT_ID}."
        )
    if binding.mtgjson_override_confirmed_at is not None:
        raise PremiseFailed(
            f"Binding {BINDING_ID} is override-confirmed; its NULL is a decision, "
            "not missing data. Refusing."
        )
    if binding.mtgjson_id:
        raise PremiseFailed(
            f"Binding {BINDING_ID} already has an MTGJSON id ({binding.mtgjson_id}); "
            "nothing to backfill."
        )

    card_ids = json.loads(binding.local_card_ids_json or "[]")
    cards = [c for c in (session.get(InventoryCard, cid) for cid in card_ids) if c is not None]
    values = {(c.mtgjson_id or "").strip().lower() for c in cards if c.mtgjson_id}
    if len(values) != 1:
        raise PremiseFailed(
            f"Binding {BINDING_ID}'s cards do not agree on one MTGJSON id "
            f"(found {sorted(values)}). Refusing."
        )
    card_value = values.pop()

    catalog_value = (catalog_loader(PRODUCT_ID) or "").strip().lower()
    if not catalog_value:
        raise PremiseFailed("Mana Pool's catalog returned no MTGJSON id for this product.")
    if catalog_value != card_value:
        raise PremiseFailed(
            "Mana Pool's catalog MTGJSON does not match the card's "
            f"({catalog_value} vs {card_value}). Refusing."
        )

    return {
        "binding_id": binding.id,
        "product_id": binding.product_id,
        "old_mtgjson_id": binding.mtgjson_id,
        "new_mtgjson_id": card_value,
        "agreed_by": ["card", "manapool_catalog"],
        "card_ids": [c.id for c in cards],
        "audit_card_id": min(c.id for c in cards),
        "language_id": binding.language_id,
        "set_code": binding.set_code,
        "collector_number": binding.collector_number,
    }


def run(session: Session, *, confirm: bool, catalog_loader=_catalog_mtgjson) -> dict:
    """The SAME verification runs in both modes; only the commit differs."""
    report = plan(session, catalog_loader=catalog_loader)
    binding = session.get(RemoteProductBinding, BINDING_ID)

    session.add(InventoryChangeLog(
        actor=current_actor(),
        inventory_card_id=report["audit_card_id"],
        change_summary=json.dumps({
            "action_type": "binding_mtgjson_backfill",
            "binding_id": report["binding_id"],
            "product_id": report["product_id"],
            "old_mtgjson_id": report["old_mtgjson_id"],
            "new_mtgjson_id": report["new_mtgjson_id"],
            "agreed_by": report["agreed_by"],
            "script": SCRIPT_NAME,
            "reason": (
                "Binding had no MTGJSON id, so the inventory mirror keyed it on a "
                "synthetic __mtgjson_override__ sentinel and reconciliation's "
                "desired-quantity recompute had nothing real to match. Card and "
                "Mana Pool catalog agree on this value."
            ),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, sort_keys=True),
    ))
    binding.mtgjson_id = report["new_mtgjson_id"]
    session.flush()

    if confirm:
        session.commit()
        logger.info(
            "%s: binding %s mtgjson_id set (was NULL); audited in "
            "InventoryChangeLog against card %s.",
            SCRIPT_NAME, report["binding_id"], report["audit_card_id"],
        )
    else:
        session.rollback()
    report["mode"] = "CONFIRMED" if confirm else "DRY_RUN"
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm", action="store_true", help="Write. Dry run otherwise.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.setLevel(logging.INFO)
    set_script_actor(SCRIPT_NAME)

    with Session(engine) as session:
        try:
            report = run(session, confirm=args.confirm)
        except PremiseFailed as exc:
            logger.warning("%s: refused -- %s", SCRIPT_NAME, exc)
            raise SystemExit(f"REFUSED: {exc}")

    print(json.dumps(report, indent=2, sort_keys=True))
    if not args.confirm:
        print()
        print("DRY RUN -- nothing was committed.")


if __name__ == "__main__":
    main()
