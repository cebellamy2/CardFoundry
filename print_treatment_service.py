"""Scryfall print treatments: what a printing LOOKS like.

Extended art, borderless, showcase and friends. The data check on
2026-10-13 found nothing local that distinguishes them -- no column, no
binding field (the binding's `requested_variant` is just another copy of
the same identity dict), and Mana Pool's own product object carries no
treatment field either -- so this fetches them from Scryfall once per
printing and stores the answer.

★ EVERY LIMIT HERE WAS CONFIRMED AGAINST THE LIVE API, not the docs
(which 403 a plain fetch). Measured 2026-10-13 with 75 real inventory ids:
  - 75 identifiers per POST is a HARD cap. 76 returns HTTP 400,
    "The `identifiers` list must have at least 1 and no more than 75
    references." So the batch size is not a tunable.
  - the response is {object, data, not_found}; an id Scryfall cannot
    resolve comes back under not_found rather than failing the call.
  - of 75 real cards: border_color, frame, full_art, promo and textless
    were present on all 75; frame_effects on 22; promo_types on 20.

★ WHAT IS NOT A LABEL, AND WHY IT MATTERS. frame_effects also carries
`legendary`, `inverted`, `enchantment` and similar. `legendary` alone was
on 13 of those 75 cards -- it means "has the legendary crown", which every
legendary card has. Labelling those would bury the handful of real
treatments an operator is hunting for under noise that describes most of
the collection. Only the values below become labels.

Scryfall is READ-only here. This writes nothing anywhere except
CardFoundry's own scryfall_printing_treatments table, and makes no Mana
Pool call of any kind.
"""

import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from models import InventoryCard, ScryfallPrintingTreatment

logger = logging.getLogger("cardfoundry")

# Confirmed empirically: 76 identifiers is rejected with HTTP 400.
SCRYFALL_COLLECTION_LIMIT = 75
# Scryfall ask for 50-100ms between requests. 120ms is inside that with
# room to spare; 85 calls costs ~10s of waiting, which is nothing for a
# once-per-printing backfill and is the polite thing to do to a free API.
SCRYFALL_COURTESY_DELAY_SECONDS = 0.12

NORMAL = "normal"
BORDERLESS = "borderless"
EXTENDED_ART = "extendedart"
SHOWCASE = "showcase"
FULL_ART = "fullart"
RETRO_FRAME = "retroframe"
BOOSTER_FUN = "boosterfun"
PROMO = "promo"
TEXTLESS = "textless"

# The label vocabulary, in the order the filter shows it.
PRINT_TYPE_LABELS = {
    NORMAL: "Normal",
    BORDERLESS: "Borderless",
    EXTENDED_ART: "Extended art",
    SHOWCASE: "Showcase",
    FULL_ART: "Full art",
    RETRO_FRAME: "Retro frame",
    BOOSTER_FUN: "Booster fun",
    PROMO: "Promo",
    TEXTLESS: "Textless",
}

# ★ RETRO FRAME IS A STATEMENT ABOUT THE FRAME, NOT ABOUT THE REPRINT.
# Scryfall has no "this is a retro-treatment reprint" flag. `frame` is the
# frame the card is printed with: 1993 and 1997 are the pre-modern ones.
# For a genuine Alpha card that is simply its original frame; for a modern
# set it is the retro treatment. The filter label says "Retro frame" and
# means exactly that -- the card uses the old frame -- rather than
# claiming to know which of the two situations produced it. Deriving it
# from the set's release date would be the real answer and needs data we
# do not store.
RETRO_FRAMES = {"1993", "1997"}


def _as_list(value):
    return value if isinstance(value, list) else []


def derive_labels(card: dict) -> list[str]:
    """The treatment labels for one raw Scryfall card payload.

    A printing can carry SEVERAL -- a borderless showcase is both, and a
    booster-fun extended art is both -- so this returns a list, not one
    answer. `normal` is returned only when nothing else applies, so it
    means "no special treatment" rather than "we did not look".
    """
    frame_effects = {str(v).lower() for v in _as_list(card.get("frame_effects"))}
    promo_types = {str(v).lower() for v in _as_list(card.get("promo_types"))}
    labels = []

    if str(card.get("border_color") or "").lower() == BORDERLESS:
        labels.append(BORDERLESS)
    if EXTENDED_ART in frame_effects:
        labels.append(EXTENDED_ART)
    if SHOWCASE in frame_effects:
        labels.append(SHOWCASE)
    if card.get("full_art") is True:
        labels.append(FULL_ART)
    if str(card.get("frame") or "") in RETRO_FRAMES:
        labels.append(RETRO_FRAME)
    if BOOSTER_FUN in promo_types:
        labels.append(BOOSTER_FUN)
    if card.get("promo") is True:
        labels.append(PROMO)
    if card.get("textless") is True:
        labels.append(TEXTLESS)

    return labels or [NORMAL]


def encode_labels(labels) -> str:
    """Pipe-delimited, with leading and trailing pipes.

    The delimiters on BOTH ends are the point: `%|showcase|%` can then
    only match the whole label, never a label that merely contains it.
    """
    ordered = [l for l in PRINT_TYPE_LABELS if l in set(labels)]
    return "|" + "|".join(ordered) + "|" if ordered else ""


def label_pattern(label: str) -> str:
    return f"%|{label}|%"


def decode_labels(encoded: str) -> list[str]:
    return [part for part in (encoded or "").split("|") if part]


def describe_labels(encoded: str) -> str:
    """Human-readable, for a results row."""
    names = [PRINT_TYPE_LABELS.get(l, l) for l in decode_labels(encoded)]
    return ", ".join(names) if names else "Unknown"


def printing_ids_needing_treatment(session: Session, *, limit: int | None = None) -> list[str]:
    """Distinct scryfall_ids of inventory cards with no treatment row yet.

    Resumable by construction: it asks what is still missing, so an
    interrupted run simply has less to do next time and a completed run
    returns nothing.
    """
    known = session.query(ScryfallPrintingTreatment.scryfall_id)
    query = (
        session.query(InventoryCard.scryfall_id)
        .filter(
            InventoryCard.scryfall_id.isnot(None),
            InventoryCard.scryfall_id != "",
            ~InventoryCard.scryfall_id.in_(known),
        )
        .distinct()
        .order_by(InventoryCard.scryfall_id)
    )
    if limit:
        query = query.limit(limit)
    return [row[0] for row in query.all()]


def store_treatment(session: Session, scryfall_id: str, card: dict) -> ScryfallPrintingTreatment:
    """Upsert one printing's treatment. Idempotent: re-running rewrites
    the same values and the same labels from the same payload."""
    labels = encode_labels(derive_labels(card))
    row = session.get(ScryfallPrintingTreatment, scryfall_id)
    if row is None:
        row = ScryfallPrintingTreatment(scryfall_id=scryfall_id)
        session.add(row)
    row.border_color = card.get("border_color")
    row.frame = str(card.get("frame")) if card.get("frame") is not None else None
    row.full_art = bool(card.get("full_art"))
    row.promo = bool(card.get("promo"))
    row.textless = bool(card.get("textless"))
    row.frame_effects_json = json.dumps(_as_list(card.get("frame_effects")))
    row.promo_types_json = json.dumps(_as_list(card.get("promo_types")))
    row.labels = labels
    row.fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return row


def backfill_print_treatments(
    session: Session, *, scryfall_lookup, apply: bool,
    limit: int | None = None, sleep=time.sleep,
) -> dict:
    """Fetch and store treatments for every printing missing one.

    The dry run and the apply take the SAME path: both fetch (Scryfall is
    read-only, so a dry run costs nothing but politeness) and both build
    the same rows. Only the caller's commit differs, so the counts a dry
    run reports are the counts an apply will write.
    """
    pending = printing_ids_needing_treatment(session, limit=limit)
    result = {
        "pending": len(pending), "fetched": 0, "stored": 0,
        "unresolved": [], "calls": 0, "label_counts": {}, "applied": bool(apply),
    }
    if not pending:
        logger.info("print treatments: nothing to fetch; every printing has a row.")
        return result

    for start in range(0, len(pending), SCRYFALL_COLLECTION_LIMIT):
        chunk = pending[start:start + SCRYFALL_COLLECTION_LIMIT]
        if start:
            sleep(SCRYFALL_COURTESY_DELAY_SECONDS)
        try:
            found = scryfall_lookup(chunk)
        except Exception as exc:  # noqa: BLE001 -- one bad page must not lose the rest
            logger.warning(
                "print treatments: Scryfall lookup failed for %s id(s) starting "
                "at %s (%s: %s); those printings are left for the next run.",
                len(chunk), chunk[0], type(exc).__name__, exc,
            )
            continue
        result["calls"] += 1
        cards_by_id = found[0] if isinstance(found, tuple) else found
        result["fetched"] += len(cards_by_id)
        for scryfall_id, card in cards_by_id.items():
            row = store_treatment(session, scryfall_id, card)
            result["stored"] += 1
            for label in decode_labels(row.labels):
                result["label_counts"][label] = result["label_counts"].get(label, 0) + 1
        result["unresolved"].extend(sorted(set(chunk) - set(cards_by_id)))

    session.flush()
    if result["unresolved"]:
        logger.warning(
            "print treatments: Scryfall could not resolve %s id(s); they keep "
            "no row and will be retried: %s",
            len(result["unresolved"]), ", ".join(result["unresolved"][:10]),
        )
    logger.info(
        "print treatments %s: %s pending, %s calls, %s stored, %s unresolved, "
        "labels=%s",
        "APPLIED" if apply else "DRY RUN", result["pending"], result["calls"],
        result["stored"], len(result["unresolved"]),
        dict(sorted(result["label_counts"].items())),
    )
    return result
