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


# Status-scope toggle for decklist search (Phase 9): "available" (default)
# matches the app's other sellable-only searches; "extended" additionally
# surfaces reserved/unsellable copies so an operator can tell "reserved or
# unavailable, but in the building" apart from "genuinely absent" --
# deliberately excludes sold/removed, which are gone, not just non-sellable.
# Not the same default as /inventory (all-inventory) -- decklist search
# stays sellable-only unless explicitly widened.
DEFAULT_DECKLIST_STATUS_SCOPE = "available"
DECKLIST_STATUS_SCOPES = {
    "available": ("available",),
    "extended": ("available", "reserved", "unsellable"),
}


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


def _line_match_query(
    session: Session, name: str, set_code: str | None, collector_number: str | None,
    statuses: tuple[str, ...] = DECKLIST_STATUS_SCOPES[DEFAULT_DECKLIST_STATUS_SCOPE],
):
    """Inventory match query for one decklist line, and how it matched --
    shared by search_decklist_inventory and the personal-use marking flow
    so both match cards exactly the same way. `statuses` defaults to
    available-only, preserving every existing caller's behavior (the
    personal-use flow never passes it, and must not -- see
    matching_available_cards_in_batch).

    Name-only matching checks InventoryCard.flavor_name as well as .name --
    a decklist line naming a card by its alternate/flavor name (e.g. "Doom
    Variant" for Roaming Throne) must still find it. This is pure string
    matching against our own stored column; Scryfall does not index flavor
    names, so there is no shortcut through it. Because a bare name or
    flavor name is not unique across different canonical cards, this can
    legitimately return rows for more than one distinct card -- callers
    that aggregate on_hand MUST group by InventoryCard.name first (see
    search_decklist_inventory) rather than summing raw counts, or two
    genuinely different cards get merged into one number."""
    query = session.query(InventoryCard).filter(InventoryCard.status.in_(statuses))
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
                func.lower(InventoryCard.flavor_name) == name.lower(),
            )
        )
        match_mode = "name_only"
    return query, match_mode


def matching_available_cards_in_batch(
    session: Session, name: str, set_code: str | None, collector_number: str | None,
    batch_id: int, foil: bool,
    statuses: tuple[str, ...] = DECKLIST_STATUS_SCOPES[DEFAULT_DECKLIST_STATUS_SCOPE],
) -> list[InventoryCard]:
    """Copies of one decklist line within one batch/finish, oldest first --
    same matching and ordering as the search's own nonfoil_batch/
    foil_batch resolution, just returning the actual rows instead of only
    the containing batch. Re-run fresh rather than reusing objects from an
    earlier request (HTTP is stateless, and a fresh read means marking
    sees current inventory, not a stale page render).

    `statuses` defaults to available-only -- the personal-use marking flow
    relies on this default and must keep calling this without an explicit
    scope, since transition_inventory_removal only accepts an "available"
    starting status; only the new bulk-action resolution route (Phase 9)
    passes a wider scope, matching whatever the operator searched with."""
    query, _ = _line_match_query(session, name, set_code, collector_number, statuses)
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


def _matched_via(card: InventoryCard, term: str) -> str | None:
    """Why `term` matched this specific card: "canonical" (its own name, or
    the front face of a double-faced name) or "alternate" (its flavor
    name). None when neither actually matches `term` -- possible for an
    exact-printing line, where a set+collector# pins identity regardless
    of what name text was typed alongside it."""
    term_lower = term.strip().lower()
    name_lower = (card.name or "").lower()
    if name_lower == term_lower or name_lower.startswith(f"{term_lower} //"):
        return "canonical"
    flavor_lower = (card.flavor_name or "").lower()
    if flavor_lower == term_lower:
        return "alternate"
    return None


def _group_matched_via(cards: list[InventoryCard], term: str) -> str | None:
    """A canonical-name group's members all share the identical card.name
    (that IS the grouping key), and _matched_via's canonical check depends
    only on (card.name, term) -- so if any member matches via canonical,
    every member does, uniformly. When none match via canonical, every
    present member individually satisfied flavor_name == term to be in
    the result set at all, so "alternate" is uniform too. matched_via is
    therefore always a single value across a group, never mixed -- one
    representative member is enough."""
    return _matched_via(cards[0], term) if cards else None


def _group_flavor_name(cards: list[InventoryCard]) -> str | None:
    """A single canonical name can span several printings that don't all
    carry the same (or any) flavor name -- e.g. only the Universes Beyond
    printing of a reprinted card has one. Only shown at the group/row
    level when every card in the group agrees, so the summary line never
    asserts an alternate name that isn't true of every copy it's counting;
    each individual printing's own flavor_name is still shown correctly
    in the per-printing breakdown regardless (see _group_matches_by_printing)."""
    flavor_names = {c.flavor_name for c in cards}
    return flavor_names.pop() if len(flavor_names) == 1 else None


def _batch_info(session: Session, cards: list[InventoryCard], foil: bool) -> dict | None:
    """The batch of the oldest copy (by imported_at, matching the real
    picking precedent -- order_service.allocate_order orders the same way)
    among `cards` for one finish, or None if this finish has no copy.
    List-based counterpart to _first_batch: used wherever the caller
    already has a materialized, pre-scoped group of cards (a single
    canonical-name group) rather than a query it can still filter."""
    finish_cards = [c for c in cards if (c.finish_id == "FO") == foil]
    if not finish_cards:
        return None
    oldest = min(finish_cards, key=lambda c: (c.imported_at, c.id))
    batch = session.get(Batch, oldest.batch_id)
    return {"id": batch.id, "batch_code": batch.batch_code} if batch else None


def _group_matches_by_printing(
    session: Session, matches: list[InventoryCard],
    exact_set_code: str | None, exact_collector_number: str | None,
) -> list[dict]:
    """Break a name's matches down by distinct (set_code, collector_number)
    printing -- the flag-and-nest display (Phase 10) needs this so a
    single opaque on_hand count can be shown as "which printings actually
    make that up", and so the printing the decklist line specifically
    asked for (if any) can be flagged among the others. Purely a read
    grouping of already-fetched rows -- doesn't touch on_hand/fillable/
    found-vs-not_found, which stay driven by the original exact-printing-
    or-name-only query, unchanged."""
    groups: dict[tuple[str, str], list[InventoryCard]] = {}
    for card in matches:
        key = ((card.set_code or "").upper(), (card.collector_number or "").upper())
        groups.setdefault(key, []).append(card)

    batch_ids = {card.batch_id for card in matches}
    batch_codes = {
        batch.id: batch.batch_code
        for batch in (
            session.query(Batch).filter(Batch.id.in_(batch_ids)) if batch_ids else []
        )
    }

    def oldest_batch(cards: list[InventoryCard], foil: bool) -> dict | None:
        finish_cards = [c for c in cards if (c.finish_id == "FO") == foil]
        if not finish_cards:
            return None
        oldest = min(finish_cards, key=lambda c: (c.imported_at, c.id))
        batch_code = batch_codes.get(oldest.batch_id)
        return {"id": oldest.batch_id, "batch_code": batch_code} if batch_code else None

    exact_key = (
        ((exact_set_code or "").upper(), (exact_collector_number or "").upper())
        if exact_set_code and exact_collector_number else None
    )

    printings = [
        {
            "set_code": cards[0].set_code,
            "collector_number": cards[0].collector_number,
            # One (set_code, collector_number) is one physical printing --
            # every card row in this group is the same identity, so its
            # flavor_name (if any) is uniform across the group, unlike the
            # canonical-name group one level up in search_decklist_inventory,
            # which can span several printings with different flavor names.
            "flavor_name": cards[0].flavor_name,
            "on_hand": len(cards),
            "is_exact_match": key == exact_key,
            "nonfoil_batch": oldest_batch(cards, foil=False),
            "foil_batch": oldest_batch(cards, foil=True),
        }
        for key, cards in groups.items()
    ]
    printings.sort(key=lambda p: (
        0 if p["is_exact_match"] else 1, -p["on_hand"],
        p["set_code"] or "", p["collector_number"] or "",
    ))
    return printings


def search_decklist_inventory(
    session: Session, parsed_lines: list[dict],
    statuses: tuple[str, ...] = DECKLIST_STATUS_SCOPES[DEFAULT_DECKLIST_STATUS_SCOPE],
) -> tuple[list[dict], list[dict]]:
    """One result row per parsed line that has at least one match in
    `statuses`, requested quantity vs. on-hand count aggregated across
    every batch. Lines with zero matches are returned separately
    (not_found) rather than as a 0-on-hand result row -- "don't match" is
    reported the same way as "didn't parse", per spec.

    `statuses` defaults to available-only, matching every existing
    caller's behavior; the decklist page's status-scope toggle (Phase 9)
    is the only thing that widens it, to ("available", "reserved",
    "unsellable") -- sold/removed stay excluded even at the widest scope,
    since those are genuinely gone rather than "in the building".

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

    Each found row also carries `printings` (Phase 10): every distinct
    (set_code, collector_number) among this name's matches, each with its
    own on_hand/nonfoil_batch/foil_batch and an is_exact_match flag (true
    only for the printing the decklist line itself named, if any) -- lets
    the caller show which printings actually make up on_hand instead of
    one opaque number, and flag+nest a specifically-requested printing
    among any others already on hand.

    NON-NEGOTIABLE: name-only matching now also checks flavor_name (e.g. a
    line reading "Doom Variant" must find the card locally stored as
    "Roaming Throne"), and a flavor name is not guaranteed unique to one
    canonical card. When one line's matches span more than one distinct
    InventoryCard.name, this returns MULTIPLE found rows for that line --
    one per canonical name, each independently counted -- rather than ever
    merging them into a single on_hand number. Each such row carries
    `matched_name` (that group's canonical name) and `matched_via`
    ("canonical", "alternate", "canonical+alternate", or None for an
    exact-printing line whose typed name matches neither) so the caller
    can label why each row matched. This is the same reason
    matching_available_cards_in_batch must be called with a specific
    row's matched_name, not the original decklist line text, once a line
    has been shown to be ambiguous -- see _decklist_mark_value in main.py.
    """
    found = []
    not_found = []
    for line in parsed_lines:
        query, match_mode = _line_match_query(
            session, line["name"], line["set_code"], line["collector_number"], statuses,
        )
        matches = query.all()
        if not matches:
            not_found.append({
                "raw_line": line["raw_line"],
                "reason": (
                    "No matching card found in inventory"
                    + (
                        f' for {line["set_code"]} #{line["collector_number"]}.'
                        if match_mode == "exact_printing" else "."
                    )
                ),
            })
            continue

        if match_mode == "exact_printing":
            # A set+collector# pins one exact card identity, so there is
            # never more than one canonical name here -- no grouping
            # needed. The "other printings of this card" lookup below uses
            # the RESOLVED canonical name (matches[0].name), not the
            # line's typed text -- the typed text may itself be a flavor
            # name (e.g. "1 Doom Variant (MAR) 099"), and other printings
            # of the same card won't share that flavor name, so matching
            # on it would silently miss them.
            canonical_name = matches[0].name
            all_printings_query, _ = _line_match_query(session, canonical_name, None, None, statuses)
            all_matches = all_printings_query.all()
            found.append({
                "raw_line": line["raw_line"],
                "requested_quantity": line["quantity"],
                "name": line["name"],
                "matched_name": canonical_name,
                "matched_via": _matched_via(matches[0], line["name"]),
                "flavor_name": _group_flavor_name(matches),
                "set_code": line["set_code"],
                "collector_number": line["collector_number"],
                "match_mode": match_mode,
                "on_hand": len(matches),
                "fillable": len(matches) >= line["quantity"],
                "nonfoil_batch": _first_batch(session, query, foil=False),
                "foil_batch": _first_batch(session, query, foil=True),
                "printings": _group_matches_by_printing(
                    session, all_matches, line["set_code"], line["collector_number"],
                ),
            })
            continue

        # name_only: group by canonical name FIRST -- never sum on_hand
        # across two different cards. In the overwhelming common case
        # (one canonical name) this produces exactly one row, identical
        # to the old behavior plus a matched_via label.
        groups: dict[str, list[InventoryCard]] = {}
        for card in matches:
            groups.setdefault(card.name, []).append(card)

        group_rows = [
            {
                "raw_line": line["raw_line"],
                "requested_quantity": line["quantity"],
                "name": line["name"],
                "matched_name": canonical_name,
                "matched_via": _group_matched_via(group_cards, line["name"]),
                "flavor_name": _group_flavor_name(group_cards),
                "set_code": line["set_code"],
                "collector_number": line["collector_number"],
                "match_mode": match_mode,
                "on_hand": len(group_cards),
                "fillable": len(group_cards) >= line["quantity"],
                "nonfoil_batch": _batch_info(session, group_cards, foil=False),
                "foil_batch": _batch_info(session, group_cards, foil=True),
                "printings": _group_matches_by_printing(
                    session, group_cards, line["set_code"], line["collector_number"],
                ),
            }
            for canonical_name, group_cards in groups.items()
        ]
        group_rows.sort(key=lambda row: (
            0 if row["matched_via"] == "canonical" else 1,
            -row["on_hand"], row["matched_name"] or "",
        ))
        found.extend(group_rows)
    return found, not_found
