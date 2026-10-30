"""ManaBox CSV as an intake for the buylist / consignment pile flow.

CardSight is paused, so consignments are scanned in ManaBox instead. A
ManaBox export carries everything the pile flow needs, and
production_import_service.parse_production_csv ALREADY parses its headers
unmodified -- verified against a real 4,481-row export, which normalised
to NF/FO/EF finishes, NM/LP conditions and EN/JA/CS/RU/CT/PH languages
with one rejected row (a trailing blank).

So this is an ADAPTER, not a pipeline: it reuses that parser for row
normalisation and turns the result into PendingPileLines. Everything
downstream -- LP+ pricing, buy tiers, the consignment suggestion, review,
the offer PDF, finalize into batches -- is the existing flow, untouched.

★ CONDITION IS TAKEN IN AT LP, NOT AT WHAT THE FILE SAYS (operator,
2026-10-30). ManaBox grades effectively everything near_mint, and a
condition nobody in the shop has looked at is not evidence a card is NM.
So every row at LP or better lands on LP and only a file condition WORSE
than LP survives -- see intake_defaults_service.batch_intake_condition_id
for the rule and why it is a constant rather than the intake-default
setting. A condition the operator hand-picks from a dropdown (the chute's
scan form, or the pile report's own per-line edit) is never touched by
this: that is a human grading a card, and it wins.

★ AND IT STORES THE LABEL, NOT THE CODE. PendingPileLine.condition holds
a label everywhere else -- the pile report's edit route validates against
INTAKE_CONDITION_LABELS and writes one, the chute's pile path writes one,
and the report's condition column and the seller's offer PDF both print
the column raw. This adapter used to write parse_production_csv's CODE
("MP"), which matched no dropdown option, so the pile report's condition
column fell back to its first option and DISPLAYED a Moderate Play card
as "Near Mint", while the seller's PDF showed "MP" on one line and
"Light Play" on the next depending on how the card arrived.

TWO THINGS THIS ROUTE DELIBERATELY DOES NOT DO:

  ManaBox's "Purchase price" is ManaBox's own number, not Mana Pool LP+.
  parse_production_csv auto-detects it as a BOUGHT price (confirmed live:
  bought_price_column == "Purchase price"), which is right for Production
  Batch Import and wrong here. It is carried through for display beside
  LP+ as a cross-check ONLY, and never stored as a cost basis nor used to
  price anything.

  It never guesses an identity. A row with no Scryfall ID is rejected,
  exactly as parse_production_csv already rejects it -- Scryfall ID is
  the matching key, and it is the strongest one available.
"""

import hashlib
import logging

from intake_defaults_service import batch_intake_condition_label
from production_import_service import ProductionImportError, parse_production_csv

# Same shared logger as the rest of the app (v1.155.0).
logger = logging.getLogger("cardfoundry")

# ManaBox's own price column. Named here so the one place that must
# ignore it for pricing is explicit rather than implied.
MANABOX_PRICE_COLUMN = "Purchase price"


class ManaboxImportError(RuntimeError):
    """The CSV could not be read as a ManaBox export."""


def parse_manabox_csv(contents: bytes) -> dict:
    """Normalise a ManaBox export into pile-line-shaped rows.

    Returns {"rows": [...], "errors": [...], "csv_row_count": int,
             "card_count": int, "file_hash": str}. Each row carries the
    fields PendingPileLine needs plus manabox_price_dollars, which is for
    display only.
    """
    try:
        parsed = parse_production_csv(contents)
    except ProductionImportError as exc:
        # Logged rather than swallowed: an unreadable upload is the one
        # failure an operator will ask about later.
        logger.warning("manabox import: CSV could not be parsed: %s", exc)
        raise ManaboxImportError(str(exc)) from exc

    rows = []
    for physical in parsed["physical_rows"]:
        if not physical.get("scryfall_id"):
            # parse_production_csv already rejects these into errors; this
            # is belt-and-braces so a future parser change cannot let an
            # unidentified row through into a priced offer.
            continue
        rows.append({
            "source_row": physical.get("source_row"),
            "scryfall_id": physical["scryfall_id"],
            "name": physical.get("name"),
            "set_code": physical.get("set_code"),
            "collector_number": physical.get("collector_number"),
            "condition": batch_intake_condition_label(
                physical.get("condition_id")) or None,
            "finish": physical.get("finish"),
            "language": physical.get("language_id"),
            # Display-only. See the module docstring.
            "manabox_price_dollars": physical.get("bought_price"),
        })

    return {
        "rows": rows,
        "errors": list(parsed.get("errors") or []),
        "csv_row_count": parsed.get("csv_row_count", 0),
        "card_count": len(rows),
        "file_hash": hashlib.sha256(contents).hexdigest(),
        "manabox_price_column_ignored_for_pricing": MANABOX_PRICE_COLUMN,
    }


def summarise(rows: list[dict], threshold_dollars: float) -> dict:
    """What the operator needs to see before committing: how the pile
    splits either side of the consignment threshold, and how much of it
    could not be priced.

    Priced entirely from what is already on each row, so this makes no
    Mana Pool call of its own -- the caller prices once, in the existing
    batched way, and passes the result in.
    """
    threshold_cents = round(threshold_dollars * 100)
    at_or_over = sum(
        1 for r in rows
        if r.get("price_cents") is not None and r["price_cents"] >= threshold_cents
    )
    under = sum(
        1 for r in rows
        if r.get("price_cents") is not None and r["price_cents"] < threshold_cents
    )
    held = sum(1 for r in rows if r.get("price_cents") is None)
    return {
        "total": len(rows),
        "at_or_over_threshold": at_or_over,
        "under_threshold": under,
        "held_no_lp_plus": held,
        "threshold_dollars": threshold_dollars,
    }
