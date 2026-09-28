"""One-off backfill: populate `type_line` on every existing InventoryCard
and OrderItem row.

The packing slip's marker has to tell a LAND from a COLOURLESS card, and
`color` stores '' for both. `type_line` is the new field that separates
them; every row created before it existed has NULL, which renders as no
marker at all (never a confident, wrong "(C)").

WHY A SEPARATE SCRIPT FROM backfill_color. That one is the recurring
hourly cron and now covers a missing type line too -- at zero extra API
cost, since it already fetches whole Scryfall cards. But it resolves every
outstanding id in ONE pass, and on a real production database that is
~12,412 distinct ids, roughly 166 batched Scryfall calls. A previous
attempt at 88 batched calls tripped a 429 EVEN WITH the shared pacer, so a
single pass is not a safe shape for the one-off catch-up.

THIS SCRIPT IS THEREFORE CHUNKED, PACED, RESUMABLE AND RE-RUNNABLE:
  * --limit bounds how many distinct ids one run resolves, so the catch-up
    can be spread over several runs in quiet windows.
  * It only ever fills NULLs, so re-running cannot overwrite anything and a
    half-finished run simply continues.
  * It uses fetch_scryfall_cards, which goes through the SAME shared pacer
    every other Scryfall call in this app uses -- no separate rate limiter.
  * On a 429 it STOPS CLEANLY, commits what it already resolved, reports
    how far it got, and exits non-zero. It never retries in a loop; the
    next run picks up where it stopped.

Dry run by default. Pass --confirm to write.

Usage (in the container, always via the venv python):
    cd /app && PYTHONPATH=/app /opt/venv/bin/python backfill_type_line.py
    cd /app && PYTHONPATH=/app /opt/venv/bin/python backfill_type_line.py --limit 1500 --confirm
"""

import argparse
import json
import logging

import httpx
from sqlalchemy import or_
from sqlalchemy.orm import Session

from actor_context import set_script_actor
from database import engine
from legacy_import_service import fetch_scryfall_cards
from models import InventoryCard, OrderItem

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "backfill_type_line"

# Scryfall's own batch size, matching legacy_import_service.
BATCH_SIZE = 75
# One run's default ceiling on distinct ids: ~20 batched calls, comfortably
# under the ~88 that tripped a 429 before.
DEFAULT_LIMIT = 1500


def outstanding_scryfall_ids(session: Session) -> list[str]:
    """Distinct scryfall_ids on rows that still have no type line, across
    both tables, ordered so successive runs make steady progress."""
    card_ids = (
        session.query(InventoryCard.scryfall_id)
        .filter(InventoryCard.scryfall_id.isnot(None), InventoryCard.type_line.is_(None))
        .distinct()
    )
    item_ids = (
        session.query(OrderItem.scryfall_id)
        .filter(OrderItem.scryfall_id.isnot(None), OrderItem.type_line.is_(None))
        .distinct()
    )
    return sorted({row[0] for row in card_ids} | {row[0] for row in item_ids})


def counts(session: Session) -> dict:
    return {
        "inventory_cards_missing": session.query(InventoryCard).filter(
            InventoryCard.scryfall_id.isnot(None), InventoryCard.type_line.is_(None),
        ).count(),
        "order_items_missing": session.query(OrderItem).filter(
            OrderItem.scryfall_id.isnot(None), OrderItem.type_line.is_(None),
        ).count(),
        "distinct_ids_outstanding": len(outstanding_scryfall_ids(session)),
    }


def _apply(session: Session, resolved: dict[str, str]) -> dict:
    """Fill type_line where it is NULL. Only NULLs, so this is idempotent
    and a re-run is free."""
    filled_cards = 0
    for card in session.query(InventoryCard).filter(
        InventoryCard.scryfall_id.in_(resolved), InventoryCard.type_line.is_(None),
    ):
        value = resolved.get(card.scryfall_id)
        if value:
            card.type_line = value
            filled_cards += 1
    filled_items = 0
    for item in session.query(OrderItem).filter(
        OrderItem.scryfall_id.in_(resolved), OrderItem.type_line.is_(None),
    ):
        value = resolved.get(item.scryfall_id)
        if value:
            item.type_line = value
            filled_items += 1
    return {"inventory_cards_filled": filled_cards, "order_items_filled": filled_items}


def run(session: Session, *, limit: int, confirm: bool, scryfall_lookup=fetch_scryfall_cards) -> dict:
    before = counts(session)
    outstanding = outstanding_scryfall_ids(session)
    targeted = outstanding[:limit]
    report = {
        "mode": "CONFIRMED" if confirm else "DRY_RUN",
        "before": before,
        "distinct_ids_targeted_this_run": len(targeted),
        "batches_planned": (len(targeted) + BATCH_SIZE - 1) // BATCH_SIZE,
        "batches_made": 0,
        "rate_limited": False,
        "unresolved_ids": 0,
    }
    if not targeted:
        report["note"] = "Nothing outstanding."
        return report

    resolved: dict[str, str] = {}
    stopped_early = False
    for start in range(0, len(targeted), BATCH_SIZE):
        chunk = targeted[start:start + BATCH_SIZE]
        try:
            result = scryfall_lookup(chunk)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                # STOP CLEANLY. Keep what we already have, report it, and
                # let the next run continue -- never retry in a loop.
                logger.warning(
                    "%s: Scryfall rate-limited after %d batch(es); stopping "
                    "cleanly with %d id(s) resolved. Re-run later to continue.",
                    SCRIPT_NAME, report["batches_made"], len(resolved),
                )
                report["rate_limited"] = True
                stopped_early = True
                break
            raise
        except httpx.HTTPError as exc:
            logger.warning(
                "%s: Scryfall request failed (%s: %s) after %d batch(es); "
                "stopping cleanly with %d id(s) resolved.",
                SCRIPT_NAME, type(exc).__name__, exc,
                report["batches_made"], len(resolved),
            )
            report["network_error"] = type(exc).__name__
            stopped_early = True
            break
        cards_by_id = result[0] if isinstance(result, tuple) else (result or {})
        for scryfall_id, card in cards_by_id.items():
            type_line = card.get("type_line")
            if type_line:
                resolved[scryfall_id] = type_line
        report["batches_made"] += 1

    report["ids_resolved"] = len(resolved)
    report["unresolved_ids"] = len([i for i in targeted if i not in resolved]) if not stopped_early else None
    report["stopped_early"] = stopped_early

    if confirm and resolved:
        report.update(_apply(session, resolved))
        session.commit()
        report["after"] = counts(session)
    elif confirm:
        report["inventory_cards_filled"] = 0
        report["order_items_filled"] = 0
        report["after"] = before
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm", action="store_true", help="Write. Dry run otherwise.")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=f"Distinct scryfall_ids to resolve in this run (default {DEFAULT_LIMIT}).",
    )
    args = parser.parse_args()

    set_script_actor(SCRIPT_NAME)
    with Session(engine) as session:
        report = run(session, limit=args.limit, confirm=args.confirm)
    print(json.dumps(report, indent=2, sort_keys=True))
    if report.get("rate_limited") or report.get("network_error"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
