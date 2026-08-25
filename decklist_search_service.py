"""Decklist batch search: parse a pasted decklist and check sellable
on-hand inventory for every line at once, instead of one card per search.
Read-only -- never writes anything.

InventoryCard has no quantity column (one row per physical card, the
convention used throughout the rest of this app -- see
import_consignment_sheets.py), so "on-hand" here means a COUNT of
matching available rows, not a summed field.
"""

import re

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models import Batch, InventoryCard


# "4 Lightning Bolt", "4x Lightning Bolt", "1 Sol Ring (LEA) 233".
# Set code + collector number are optional and only meaningful together --
# a set code alone (no collector number) doesn't identify an exact
# printing, so it's treated as part of the name instead of a partial match.
_LINE_PATTERN = re.compile(
    r"""
    ^\s*
    (?P<quantity>\d+)\s*x?\s+
    (?P<name>.+?)
    (?:\s*\(\s*(?P<set_code>[A-Za-z0-9]{2,6})\s*\)\s*(?P<collector_number>[A-Za-z0-9\-★]+))?
    \s*$
    """,
    re.VERBOSE,
)


def parse_decklist_line(raw_line: str) -> dict | None:
    """Parse one decklist line. None if it doesn't match the expected
    "<quantity> <card name>[ (SET) COLLECTOR#]" shape at all."""
    line = raw_line.strip()
    if not line:
        return None
    match = _LINE_PATTERN.match(line)
    if not match:
        return None
    quantity = int(match.group("quantity"))
    if quantity < 1:
        return None
    name = match.group("name").strip()
    if not name:
        return None
    return {
        "raw_line": raw_line,
        "quantity": quantity,
        "name": name,
        "set_code": (match.group("set_code") or "").upper() or None,
        "collector_number": (match.group("collector_number") or "").upper() or None,
    }


def parse_decklist(text: str) -> tuple[list[dict], list[dict]]:
    """Returns (parsed, unparsed) -- unparsed lines are never silently
    dropped, they're reported alongside the results with a reason."""
    parsed = []
    unparsed = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        line = parse_decklist_line(raw_line)
        if line is None:
            unparsed.append({
                "raw_line": raw_line,
                "reason": (
                    'Could not parse -- expected "<quantity> <card name>", '
                    'optionally followed by "(SET) COLLECTOR#".'
                ),
            })
            continue
        parsed.append(line)
    return parsed, unparsed


def _line_match_query(session: Session, name: str, set_code: str | None, collector_number: str | None):
    """Sellable-inventory match query for one decklist line, and how it
    matched -- shared by search_decklist_inventory and the personal-use
    marking flow so both match cards exactly the same way."""
    query = session.query(InventoryCard).filter(InventoryCard.status == "available")
    if set_code and collector_number:
        query = query.filter(
            func.upper(InventoryCard.set_code) == set_code,
            func.upper(InventoryCard.collector_number) == collector_number,
        )
        match_mode = "exact_printing"
    else:
        query = query.filter(
            or_(
                func.lower(InventoryCard.name) == name.lower(),
                InventoryCard.name.ilike(f"{name} //%"),
            )
        )
        match_mode = "name_only"
    return query, match_mode


def matching_available_cards_in_batch(
    session: Session, name: str, set_code: str | None, collector_number: str | None,
    batch_id: int, foil: bool,
) -> list[InventoryCard]:
    """Sellable copies of one decklist line within one batch/finish, oldest
    first -- same matching and ordering as the search's own nonfoil_batch/
    foil_batch resolution, just returning the actual rows instead of only
    the containing batch. Re-run fresh rather than reusing objects from an
    earlier request (HTTP is stateless, and a fresh read means marking
    sees current inventory, not a stale page render)."""
    query, _ = _line_match_query(session, name, set_code, collector_number)
    finish_filter = InventoryCard.finish_id == "FO" if foil else InventoryCard.finish_id != "FO"
    return (
        query.filter(InventoryCard.batch_id == batch_id, finish_filter)
        .order_by(InventoryCard.imported_at, InventoryCard.id)
        .all()
    )


def _first_batch(session: Session, query, *, foil: bool) -> dict | None:
    """The batch of the oldest available match for one finish group,
    "oldest" meaning InventoryCard.imported_at -- matching the real
    picking precedent (order_service.allocate_order orders the same way),
    not Batch.created_at, which can lag behind when a card is added to a
    long-standing batch later (e.g. via /inventory/add). Foil is exactly
    finish_id == "FO"; every other finish (including the rare etched "EF")
    groups into non-foil for this split, per the operator's own call.
    """
    finish_filter = InventoryCard.finish_id == "FO" if foil else InventoryCard.finish_id != "FO"
    card = (
        query.filter(finish_filter)
        .order_by(InventoryCard.imported_at, InventoryCard.id)
        .first()
    )
    if not card:
        return None
    batch = session.get(Batch, card.batch_id)
    return {"id": batch.id, "batch_code": batch.batch_code} if batch else None


def search_decklist_inventory(session: Session, parsed_lines: list[dict]) -> tuple[list[dict], list[dict]]:
    """One result row per parsed line that has at least one sellable match,
    requested quantity vs. on-hand count aggregated across every batch.
    Lines with zero matches are returned separately (not_found) rather
    than as a 0-on-hand result row -- "don't match" is reported the same
    way as "didn't parse", per spec.

    Sellable/available inventory only (InventoryCard.status == "available")
    -- no reserved/sold toggle in v1.

    A set code + collector number pins an exact printing and is
    authoritative on its own (matching how set+collector already works as
    an identity elsewhere in this app); name-only matching is exact
    (case-insensitive), with one deliberate concession for double-faced
    cards named by their front face only in a decklist (e.g. "Fable of
    the Mirror-Breaker" for a card locally stored as "Fable of the
    Mirror-Breaker // Reflection of Kiki-Jiki") -- a very common real
    decklist convention, not a loose substring match.

    Alongside the aggregated on_hand count (unchanged, spans every finish),
    each found row also carries nonfoil_batch/foil_batch -- the batch of
    the first available copy in that finish, or None when no copy in that
    finish exists (rendered as a blank, never an error).
    """
    found = []
    not_found = []
    for line in parsed_lines:
        query, match_mode = _line_match_query(
            session, line["name"], line["set_code"], line["collector_number"],
        )
        matches = query.all()
        if not matches:
            not_found.append({
                "raw_line": line["raw_line"],
                "reason": (
                    "No matching sellable card found in inventory"
                    + (
                        f' for {line["set_code"]} #{line["collector_number"]}.'
                        if match_mode == "exact_printing" else "."
                    )
                ),
            })
            continue

        found.append({
            "raw_line": line["raw_line"],
            "requested_quantity": line["quantity"],
            "name": line["name"],
            "matched_name": matches[0].name,
            "set_code": line["set_code"],
            "collector_number": line["collector_number"],
            "match_mode": match_mode,
            "on_hand": len(matches),
            "fillable": len(matches) >= line["quantity"],
            "nonfoil_batch": _first_batch(session, query, foil=False),
            "foil_batch": _first_batch(session, query, foil=True),
        })
    return found, not_found
