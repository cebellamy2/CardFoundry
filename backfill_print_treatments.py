"""One-time backfill: Scryfall print treatments for every stocked printing.

★ IT DEFAULTS TO A DRY RUN. Writing requires --apply. A file that can
write to production must not be able to do it by being run.

★ THE DRY RUN TAKES THE SAME PATH AS THE WRITE. Both call
backfill_print_treatments, which fetches from Scryfall and builds the same
rows; only the final commit differs. Scryfall is READ-only, so a dry run
costs nothing but politeness, and the counts it reports are the counts an
apply will write. Nothing is sent to Mana Pool by either.

★ IDEMPOTENT AND RESUMABLE BY CONSTRUCTION. The work list is "printings
with no treatment row", so an interrupted run has less to do next time, a
completed run finds nothing, and a re-run rewrites the same values from
the same payloads.

Limits confirmed against the live API on 2026-10-13, not the docs (which
403 a plain fetch): 75 identifiers per POST is a hard cap -- 76 returns
HTTP 400 naming the limit -- and unresolvable ids come back under
not_found rather than failing the call.

    PYTHONPATH=/app /opt/venv/bin/python backfill_print_treatments.py
    PYTHONPATH=/app /opt/venv/bin/python backfill_print_treatments.py --apply
"""

import json
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ACTOR = "script:print_treatment_backfill"


def main() -> int:
    apply = "--apply" in sys.argv
    db = "/data/cardfoundry.db"
    limit = None
    for arg in sys.argv[1:]:
        if arg.startswith("--db="):
            db = arg.split("=", 1)[1]
        if arg.startswith("--limit="):
            limit = int(arg.split("=", 1)[1])

    engine = create_engine(f"sqlite:///{db}")
    import database
    import inventory_sync_service
    database.engine = engine
    inventory_sync_service.engine = engine

    from legacy_import_service import fetch_scryfall_cards
    from models import AppSettingChangeLog, InventoryCard, ScryfallPrintingTreatment
    from print_treatment_service import (
        PRINT_TYPE_LABELS, backfill_print_treatments,
        printing_ids_needing_treatment,
    )

    print(f"PRINT TREATMENTS {'APPLY' if apply else 'DRY RUN'} against {db}")
    print("-" * 70)

    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection) as session:
            total_printings = (
                session.query(InventoryCard.scryfall_id)
                .filter(InventoryCard.scryfall_id.isnot(None),
                        InventoryCard.scryfall_id != "")
                .distinct().count()
            )
            already = session.query(ScryfallPrintingTreatment).count()
            pending = printing_ids_needing_treatment(session, limit=limit)
            print(f"  distinct stocked printings : {total_printings}")
            print(f"  already stored             : {already}")
            print(f"  to fetch this run          : {len(pending)}")
            calls = -(-len(pending) // 75)
            print(f"  Scryfall calls (75 per)    : {calls}")
            print()

            result = backfill_print_treatments(
                session, scryfall_lookup=fetch_scryfall_cards,
                apply=apply, limit=limit,
            )

            print(f"  calls made   : {result['calls']}")
            print(f"  fetched      : {result['fetched']}")
            print(f"  stored       : {result['stored']}")
            print(f"  unresolved   : {len(result['unresolved'])}")
            for scryfall_id in result["unresolved"][:20]:
                print(f"     {scryfall_id}")
            print()
            print("  LABEL COUNTS (printings, not cards):")
            for key, label in PRINT_TYPE_LABELS.items():
                n = result["label_counts"].get(key, 0)
                if n:
                    print(f"     {label:16} {n}")

            if apply:
                session.add(AppSettingChangeLog(
                    key="print_treatment_backfill",
                    actor=ACTOR,
                    change_summary=json.dumps({
                        "stored": result["stored"],
                        "calls": result["calls"],
                        "unresolved": result["unresolved"][:50],
                        "unresolved_count": len(result["unresolved"]),
                        "label_counts": result["label_counts"],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }, sort_keys=True),
                ))
                session.flush()
                transaction.commit()
                print("\nAPPLIED and committed (audited on AppSettingChangeLog).")
            else:
                transaction.rollback()
                print("\nDRY RUN -- rolled back, nothing written.")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
